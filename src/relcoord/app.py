# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from relcoord.auth import AuthError, TokenValidator, extract_bearer_token
from relcoord.change import (
    ChangeProcessingError,
    ChangeProgress,
    CredentialError,
    DeployConfigError,
    GitTransportError,
    ProgressSink,
    ignore_progress,
)
from relcoord.errors import (
    PersistenceUnavailableError,
    TimestampConflictError,
    ValidationError,
)
from relcoord.git import github_https_url_from_ssh_style_uri, is_ssh_style_git_uri
from relcoord.service import ImageVersionService
from relcoord.store import ImageInfoStore

logger = logging.getLogger(__name__)

EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

# Idle event streams need traffic often enough that proxies between relcoord and
# the client do not treat a long running step (a clone, a rollout) as a dead
# connection.
_HEARTBEAT_INTERVAL_SECONDS = 15.0


class ChangeProcessor(Protocol):
    def process(
        self,
        repo: str,
        commit: str,
        image: str | None,
        config_path: str = ...,
        system: bool = ...,
        *,
        progress: ProgressSink = ...,
    ) -> object: ...


class RequestTokenValidator(Protocol):
    def validate(self, authorization_header: str | None) -> object: ...


class BearerTokenValidator:
    def __init__(self, token_validator: TokenValidator) -> None:
        self._token_validator = token_validator

    def validate(self, authorization_header: str | None) -> object:
        token = extract_bearer_token(authorization_header)
        return self._token_validator.validate(token)


class NoopTokenValidator:
    def validate(self, authorization_header: str | None) -> object:
        return None


class NoopChangeResult:
    generated_count = 0


@dataclass(frozen=True)
class _ChangePlan:
    """A validated change request, ready to hand to the change processor."""

    repo: str
    commit: str
    config_path: str
    system: bool
    manifest_image: str | None
    registered: dict[str, Any] | None = field(default=None)


class _SystemNotAllowedError(Exception):
    """Raised when a principal requests system mode without the role for it."""


class NoopChangeProcessor:
    def process(
        self,
        repo: str,
        commit: str,
        image: str | None,
        config_path: str = ".deploy",
        system: bool = False,
        *,
        progress: ProgressSink = ignore_progress,
    ) -> object:
        message = (
            "change processing disabled: no manifests_repository configured; "
            "skipping source checkout, manifest-builder invocation, manifests commit, "
            f"and push for repo {repo} at commit {commit}"
        )
        logger.warning("%s", message)
        progress(ChangeProgress(phase="disabled", message=message))
        return NoopChangeResult()


def create_app(
    store: ImageInfoStore,
    token_validator: RequestTokenValidator,
    change_processor: ChangeProcessor,
) -> Starlette:
    service = ImageVersionService(store=store)

    def _require_auth(request: Request) -> tuple[Response | None, object | None]:
        try:
            header = request.headers.get("authorization")
            principal = token_validator.validate(header)
        except AuthError as exc:
            logger.warning(
                "Unauthorized request %s %s: %s",
                request.method,
                request.url.path,
                exc,
            )
            return (
                _json_error(status_code=401, error="unauthorized", message=str(exc)),
                None,
            )
        return None, principal

    async def health(request: Request) -> Response:
        try:
            await store.health_check()
        except PersistenceUnavailableError as exc:
            logger.warning(
                "Health check failed for persistence operation %s",
                exc.operation,
                exc_info=True,
            )
            return JSONResponse(
                {"status": "unhealthy", "checks": {"database": "unavailable"}},
                status_code=503,
            )
        except Exception:
            logger.exception("Health check failed for persistence backend")
            return JSONResponse(
                {"status": "unhealthy", "checks": {"database": "unavailable"}},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "checks": {"database": "ok"}})

    async def register_image_version(request: Request) -> Response:
        unauthorized, _principal = _require_auth(request)
        if unauthorized is not None:
            return unauthorized
        try:
            payload = await _read_json(request)
            image = ensure_string(payload, "image")
            version = ensure_string(payload, "version")
            timestamp = payload.get("timestamp")
            if "timestamp" in payload and timestamp is None:
                raise ValidationError(
                    error="invalid_timestamp",
                    message="timestamp must be a valid RFC 3339 timestamp with timezone",
                )
            result = await service.register_version(
                image=image, version=version, timestamp=timestamp
            )
        except ValidationError as exc:
            return _bad_request(request, error=exc.error, message=exc.message)
        except TimestampConflictError as exc:
            return _bad_request(
                request,
                error="timestamp_conflict",
                message=str(exc),
            )
        except PersistenceUnavailableError as exc:
            return _persistence_unavailable(request, exc)

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

    async def _plan_change(request: Request, principal: object) -> _ChangePlan:
        payload = await _read_json(request)
        repo = ensure_string(
            payload,
            "config_repo",
            error="invalid_config_repo",
            message="config_repo must be a non-empty string",
        )
        repo = _normalize_change_repo(repo)
        commit = ensure_string(payload, "commit")
        system = _change_system_flag(payload)
        if system and not _principal_allows_system(principal):
            logger.warning(
                "Rejected system-mode change for repo %s: role not permitted",
                repo,
            )
            raise _SystemNotAllowedError
        if system and "config_path" in payload:
            raise ValidationError(
                error="invalid_system_config_path",
                message="config_path cannot be combined with system mode",
            )
        config_path = _change_config_path(payload)
        image = (
            ensure_string(
                payload,
                "image_repo",
                error="invalid_image_repo",
                message="image_repo must be a non-empty string",
            )
            if "image_repo" in payload
            else None
        )
        tag = ensure_string(payload, "tag") if "tag" in payload else None
        if (image is None) != (tag is None):
            raise ValidationError(
                error="invalid_image_repo_tag_pairing",
                message="image_repo and tag must be provided together",
            )
        if system and image is not None:
            raise ValidationError(
                error="invalid_system_image",
                message="image_repo and tag cannot be combined with system mode",
            )

        registered: dict[str, Any] | None = None
        manifest_image = None
        if image is not None and tag is not None:
            result = await service.register_version(image=image, version=tag)
            manifest_image = f"{image}:{tag}"
            registered = {
                "image": result.image,
                "version": result.version,
                "timestamp": _format_timestamp(result.timestamp),
                "created": result.created,
            }
        return _ChangePlan(
            repo=repo,
            commit=commit,
            config_path=config_path,
            system=system,
            manifest_image=manifest_image,
            registered=registered,
        )

    async def change(request: Request) -> Response:
        unauthorized, principal = _require_auth(request)
        if unauthorized is not None:
            return unauthorized
        # Everything that can be rejected outright happens before any manifest
        # work starts, so a streaming response only ever has to report failures
        # that the change processor itself raises.
        try:
            plan = await _plan_change(request, principal)
        except ValidationError as exc:
            return _bad_request(request, error=exc.error, message=exc.message)
        except _SystemNotAllowedError:
            return _json_error(
                status_code=403,
                error="system_not_allowed",
                message="the authenticated role is not permitted to "
                "request system-mode changes",
            )
        except TimestampConflictError as exc:
            return _bad_request(
                request,
                error="timestamp_conflict",
                message=str(exc),
            )
        except PersistenceUnavailableError as exc:
            return _persistence_unavailable(request, exc)

        logger.info(
            "Processing change for repo %s at commit %s with image %s",
            plan.repo,
            plan.commit,
            plan.manifest_image,
        )
        if _wants_event_stream(request):
            return StreamingResponse(
                _change_events(request, plan, change_processor),
                status_code=202,
                media_type=EVENT_STREAM_MEDIA_TYPE,
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            result = await asyncio.to_thread(
                change_processor.process,
                plan.repo,
                plan.commit,
                plan.manifest_image,
                plan.config_path,
                plan.system,
            )
        except ChangeProcessingError as exc:
            status_code, error, message = _report_change_failure(request, plan, exc)
            return _json_error(status_code=status_code, error=error, message=message)

        return JSONResponse(_change_completion(plan, result), status_code=202)

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
            return _bad_request(request, error=exc.error, message=exc.message)
        except PersistenceUnavailableError as exc:
            return _persistence_unavailable(request, exc)

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
    except ValueError:
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


def ensure_string(
    payload: dict[str, Any],
    field: str,
    *,
    error: str | None = None,
    message: str | None = None,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            error=error or f"invalid_{field}",
            message=message or f"{field} must be a non-empty string",
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


def _log_bad_request(request: Request, *, error: str, message: str) -> None:
    logger.warning(
        "Bad request %s %s: %s: %s",
        request.method,
        request.url.path,
        error,
        message,
    )


def _bad_request(request: Request, *, error: str, message: str) -> JSONResponse:
    _log_bad_request(request, error=error, message=message)
    return _json_error(status_code=400, error=error, message=message)


def _persistence_unavailable(
    request: Request, exc: PersistenceUnavailableError
) -> JSONResponse:
    logger.error(
        "Persistence operation %s failed while handling %s %s",
        exc.operation,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return _json_error(
        status_code=503,
        error="persistence_unavailable",
        message="persistence backend unavailable",
    )


def _format_timestamp(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_change_repo(repo: str) -> str:
    if not is_ssh_style_git_uri(repo):
        return repo

    normalized = github_https_url_from_ssh_style_uri(repo)
    if normalized is not None:
        return normalized

    raise ValidationError(
        error="unsupported_ssh_git_uri",
        message="ssh style git URIs are only supported for github.com repositories",
    )


def _change_config_path(payload: dict[str, Any]) -> str:
    if "config_path" not in payload:
        return ".deploy"
    value = ensure_string(
        payload,
        "config_path",
        error="invalid_config_path",
        message="config_path must be a non-empty string",
    )
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValidationError(
            error="invalid_config_path",
            message="config_path must be a relative path within the repository",
        )
    return value


def _principal_allows_system(principal: object) -> bool:
    # A None principal means authentication is disabled (NoopTokenValidator), in
    # which case there is no access control to enforce and system mode is allowed.
    if principal is None:
        return True
    return bool(getattr(principal, "allow_system", False))


def _change_system_flag(payload: dict[str, Any]) -> bool:
    if "system" not in payload:
        return False
    value = payload["system"]
    if not isinstance(value, bool):
        raise ValidationError(
            error="invalid_system",
            message="system must be a boolean",
        )
    return value


def _change_result_payload(result: object) -> dict[str, Any]:
    generated_count = getattr(result, "generated_count", None)
    return {"generated": generated_count}


def _change_completion(plan: _ChangePlan, result: object) -> dict[str, Any]:
    """Log a completed change and build the body describing it."""
    processed = _change_result_payload(result)
    logger.info(
        "Processed change for repo %s at commit %s: generated %s manifest file(s)",
        plan.repo,
        plan.commit,
        processed["generated"],
    )
    logger.info("Accepted change for repo %s at commit %s", plan.repo, plan.commit)
    return {
        "config_repo": plan.repo,
        "commit": plan.commit,
        "registered": plan.registered,
        "processed": processed,
    }


def _report_change_failure(
    request: Request, plan: _ChangePlan, exc: ChangeProcessingError
) -> tuple[int, str, str]:
    """Log a change processing failure and map it to a status code and error."""
    if isinstance(exc, DeployConfigError):
        _log_bad_request(request, error="invalid_deploy_config", message=str(exc))
        return 400, "invalid_deploy_config", str(exc)
    if isinstance(exc, CredentialError):
        logger.warning(
            "Insufficient git credentials to process change for repo %s "
            "at commit %s: %s",
            plan.repo,
            plan.commit,
            exc,
        )
        return 502, "git_credentials_unavailable", str(exc)
    if isinstance(exc, GitTransportError):
        logger.warning(
            "Git transport failure while processing change for repo %s "
            "at commit %s: %s",
            plan.repo,
            plan.commit,
            exc,
        )
        return 502, "git_transport_failed", str(exc)
    logger.error(
        "Failed to process change for repo %s at commit %s",
        plan.repo,
        plan.commit,
        exc_info=exc,
    )
    return 500, "change_processing_failed", str(exc)


def _wants_event_stream(request: Request) -> bool:
    """Report whether the client explicitly asked for an SSE response.

    A wildcard ``Accept`` does not count: clients have to opt in, so that the
    existing single JSON response stays the default.
    """
    for entry in request.headers.get("accept", "").split(","):
        media_type = entry.split(";", maxsplit=1)[0].strip().lower()
        if media_type == EVENT_STREAM_MEDIA_TYPE:
            return True
    return False


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _change_events(
    request: Request, plan: _ChangePlan, change_processor: ChangeProcessor
) -> AsyncIterator[str]:
    """Stream the change processor's steps as server-sent events.

    The processor is synchronous and runs in a worker thread, so its progress
    callback hops back onto the event loop through a queue. A client that goes
    away does not cancel the change: a half pushed manifests repository is worse
    than an unobserved one.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ChangeProgress | None] = asyncio.Queue()

    def sink(event: ChangeProgress) -> None:
        # Called on the worker thread running the change processor.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def run() -> object:
        try:
            return await asyncio.to_thread(
                change_processor.process,
                plan.repo,
                plan.commit,
                plan.manifest_image,
                plan.config_path,
                plan.system,
                progress=sink,
            )
        finally:
            # asyncio runs the callbacks scheduled by sink() before the one that
            # resolves the to_thread future, so every progress event is already
            # queued by the time this sentinel lands behind them.
            queue.put_nowait(None)

    task = asyncio.create_task(run())
    outcome_reported = False
    try:
        yield _sse_event(
            "accepted",
            {
                "config_repo": plan.repo,
                "commit": plan.commit,
                "registered": plan.registered,
            },
        )
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), _HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            yield _sse_event(
                "progress",
                {
                    "phase": event.phase,
                    "message": event.message,
                    "detail": event.detail,
                },
            )

        try:
            result = await task
        except ChangeProcessingError as exc:
            outcome_reported = True
            status_code, error, message = _report_change_failure(request, plan, exc)
            yield _sse_event(
                "error",
                {"status": status_code, "error": error, "message": message},
            )
            return
        outcome_reported = True
        yield _sse_event("complete", _change_completion(plan, result))
    finally:
        if not outcome_reported:
            # The stream was closed before the change finished, most likely
            # because the client disconnected. Report the outcome to the log
            # instead, and retrieve any exception so asyncio does not warn.
            task.add_done_callback(_log_unobserved_change(plan))


def _log_unobserved_change(plan: _ChangePlan) -> Callable[[asyncio.Task[object]], None]:
    def log_outcome(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            logger.info(
                "Change for repo %s at commit %s completed after its event "
                "stream was closed",
                plan.repo,
                plan.commit,
            )
            return
        logger.warning(
            "Change for repo %s at commit %s failed after its event stream "
            "was closed: %s",
            plan.repo,
            plan.commit,
            exc,
        )

    return log_outcome
