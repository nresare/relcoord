# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from relcoord.auth import AuthError, TokenValidator, extract_bearer_token
from relcoord.change import ChangeProcessingError, DeployConfigError
from relcoord.errors import TimestampConflictError, ValidationError
from relcoord.service import ImageVersionService
from relcoord.store import ImageInfoStore

logger = logging.getLogger(__name__)


class ChangeProcessor(Protocol):
    def process(self, repo: str, commit: str) -> object: ...


def create_app(
    store: ImageInfoStore,
    token_validator: TokenValidator | None = None,
    change_processor: ChangeProcessor | None = None,
) -> Starlette:
    service = ImageVersionService(store=store)

    def _require_auth(request: Request) -> Response | None:
        if token_validator is None:
            return None
        try:
            header = request.headers.get("authorization")
            token = extract_bearer_token(header)
            token_validator.validate(token)
        except AuthError as exc:
            logger.warning(
                "Unauthorized request %s %s: %s",
                request.method,
                request.url.path,
                exc,
            )
            return _json_error(status_code=401, error="unauthorized", message=str(exc))
        return None

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def register_image_version(request: Request) -> Response:
        unauthorized = _require_auth(request)
        if unauthorized is not None:
            return unauthorized
        try:
            payload = await _read_json(request)
            image = ensure_string(payload, "image")
            version = ensure_string(payload, "version")
            timestamp = payload["timestamp"] if "timestamp" in payload else None
            if "timestamp" in payload and timestamp is None:
                raise ValidationError(
                    error="invalid_timestamp",
                    message="timestamp must be a valid RFC 3339 timestamp with timezone",
                )
            result = await service.register_version(
                image=image, version=version, timestamp=timestamp
            )
        except ValidationError as exc:
            return _json_error(status_code=400, error=exc.error, message=exc.message)
        except TimestampConflictError as exc:
            return _json_error(
                status_code=400,
                error="timestamp_conflict",
                message=str(exc),
            )

        status_code = 201 if result.created else 200
        return JSONResponse(
            {
                "image": result.image,
                "version": result.version,
                "timestamp": _format_timestamp(result.timestamp),
                "created": result.created,
            },
            status_code=status_code,
        )

    async def change(request: Request) -> Response:
        unauthorized = _require_auth(request)
        if unauthorized is not None:
            return unauthorized
        try:
            payload = await _read_json(request)
            repo = ensure_string(payload, "repo")
            commit = ensure_string(payload, "commit")
            image = ensure_string(payload, "image") if "image" in payload else None
            tag = ensure_string(payload, "tag") if "tag" in payload else None
            if (image is None) != (tag is None):
                raise ValidationError(
                    error="invalid_image_tag_pairing",
                    message="image and tag must be provided together",
                )

            registered: dict[str, Any] | None = None
            if image is not None and tag is not None:
                result = await service.register_version(image=image, version=tag)
                registered = {
                    "image": result.image,
                    "version": result.version,
                    "timestamp": _format_timestamp(result.timestamp),
                    "created": result.created,
                }
            processed = None
            if change_processor is not None:
                result = await asyncio.to_thread(change_processor.process, repo, commit)
                processed = _change_result_payload(result)
        except ValidationError as exc:
            return _json_error(status_code=400, error=exc.error, message=exc.message)
        except TimestampConflictError as exc:
            return _json_error(
                status_code=400,
                error="timestamp_conflict",
                message=str(exc),
            )
        except DeployConfigError as exc:
            return _json_error(
                status_code=400,
                error="invalid_deploy_config",
                message=str(exc),
            )
        except ChangeProcessingError as exc:
            logger.exception(
                "Failed to process change for repo %s at commit %s", repo, commit
            )
            return _json_error(
                status_code=500,
                error="change_processing_failed",
                message=str(exc),
            )

        logger.info("Accepted change for repo %s at commit %s", repo, commit)
        body: dict[str, Any] = {
            "repo": repo,
            "commit": commit,
            "registered": registered,
        }
        if change_processor is not None:
            body["processed"] = processed
        return JSONResponse(body, status_code=202)

    async def latest_versions(request: Request) -> Response:
        try:
            payload = await _read_json(request)
            images = _required_non_empty_string_list(
                payload,
                "images",
                error="invalid_images",
                message="images must be an array of non-empty strings",
            )
            versions = await service.latest_versions(images=images)
        except ValidationError as exc:
            return _json_error(status_code=400, error=exc.error, message=exc.message)

        return JSONResponse({"versions": versions})

    return Starlette(
        middleware=[Middleware(RequestLoggingMiddleware)],
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/image-versions", register_image_version, methods=["POST"]),
            Route("/v1/images/latest", latest_versions, methods=["POST"]),
            Route("/v1/change", change, methods=["POST"]),
        ],
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (perf_counter() - start) * 1000
            logger.exception(
                "HTTP request %s %s failed after %.2f ms",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise

        elapsed_ms = (perf_counter() - start) * 1000
        logger.info(
            "HTTP request %s %s completed with status %s in %.2f ms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise ValidationError(
            error="invalid_json",
            message="request body must be valid JSON",
        )

    if not isinstance(payload, dict):
        raise ValidationError(
            error="invalid_json",
            message="request body must be a JSON object",
        )
    return payload


def ensure_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            error=f"invalid_{field}",
            message=f"{field} must be a non-empty string",
        )
    return value


def _required_non_empty_string_list(
    payload: dict[str, Any], field: str, *, error: str, message: str
) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise ValidationError(error=error, message=message)

    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError(error=error, message=message)
    return value


def _json_error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "message": message},
        status_code=status_code,
    )


def _format_timestamp(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _change_result_payload(result: object) -> dict[str, Any]:
    generated_count = getattr(result, "generated_count", None)
    return {"generated": generated_count}
