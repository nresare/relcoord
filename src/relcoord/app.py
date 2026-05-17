# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from relcoord.errors import TimestampConflictError, ValidationError
from relcoord.repository import ImageVersionRepository
from relcoord.service import ImageVersionService


def create_app(repository: ImageVersionRepository) -> Starlette:
    service = ImageVersionService(repository=repository)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def register_image_version(request: Request) -> Response:
        try:
            payload = await _read_json(request)
            image = _required_non_empty_string(
                payload,
                "image",
                error="invalid_image",
                message="image must be a non-empty string",
            )
            version = _required_non_empty_string(
                payload,
                "version",
                error="invalid_version",
                message="version must be a non-empty string",
            )
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
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/image-versions", register_image_version, methods=["POST"]),
            Route("/v1/images/latest", latest_versions, methods=["POST"]),
        ]
    )


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


def _required_non_empty_string(
    payload: dict[str, Any], field: str, *, error: str, message: str
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(error=error, message=message)
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
