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
    CommentPostError,
    CredentialError,
    DeployConfigError,
    GitTransportError,
    ProgressSink,
    ignore_progress,
    object_ref_payloads,
)
from relcoord.errors import (
    PersistenceUnavailableError,
    TimestampConflictError,
    ValidationError,
)
from relcoord.git import (
    github_https_url_from_ssh_style_uri,
    github_repo_from_url,
    is_ssh_style_git_uri,
)
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


class DiffProcessor(Protocol):
    def diff(
        self,
        repo: str,
        commit: str,
        config_path: str = ...,
        system: bool = ...,
        *,
        pull_request: int | None = ...,
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


@dataclass(frozen=True)
class _DiffPlan:
    """A validated diff comment request, ready to hand to the diff processor."""

    repo: str
    commit: str
    config_path: str
    system: bool
    pull_request: int | None


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
    diff_processor: DiffProcessor | None = None,
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

    async def diffcomment(request: Request) -> Response:
        unauthorized, principal = _require_auth(request)
        if unauthorized is not None:
            return unauthorized
        if diff_processor is None:
            logger.warning(
                "Rejected diff comment request: no diff processor is configured"
            )
            return _json_error(
                status_code=501,
                error="diffcomment_unavailable",
                message="manifest diff comments are not configured",
            )
        try:
            plan = _plan_diff(await _read_json(request), principal)
        except ValidationError as exc:
            return _bad_request(request, error=exc.error, message=exc.message)
        except _SystemNotAllowedError:
            return _json_error(
                status_code=403,
                error="system_not_allowed",
                message="the authenticated role is not permitted to "
                "request system-mode diffs",
            )

        logger.info(
            "Diffing change for repo %s at commit %s (pull request %s)",
            plan.repo,
            plan.commit,
            plan.pull_request,
        )
        if _wants_event_stream(request):
            return StreamingResponse(
                _diff_events(request, plan, diff_processor),
                media_type=EVENT_STREAM_MEDIA_TYPE,
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            result = await asyncio.to_thread(
                diff_processor.diff,
                plan.repo,
                plan.commit,
                plan.config_path,
                plan.system,
                pull_request=plan.pull_request,
            )
        except ChangeProcessingError as exc:
            status_code, error, message = _report_diff_failure(request, plan, exc)
            return _json_error(status_code=status_code, error=error, message=message)

        return JSONResponse(_diff_completion(plan, result))

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
            Route("/v1/diffcomment", diffcomment, methods=["POST"]),
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


def _plan_diff(payload: dict[str, Any], principal: object) -> _DiffPlan:
    repo = _normalize_change_repo(
        ensure_string(
            payload,
            "config_repo",
            error="invalid_config_repo",
            message="config_repo must be a non-empty string",
        )
    )
    commit = ensure_string(payload, "commit")
    system = _change_system_flag(payload)
    if system and not _principal_allows_system(principal):
        logger.warning(
            "Rejected system-mode diff for repo %s: role not permitted", repo
        )
        raise _SystemNotAllowedError
    if system and "config_path" in payload:
        raise ValidationError(
            error="invalid_system_config_path",
            message="config_path cannot be combined with system mode",
        )
    if "image_repo" in payload or "tag" in payload:
        raise ValidationError(
            error="invalid_diff_image",
            message="image_repo and tag are not supported for diffs; "
            "a diff reports what a config commit would generate",
        )
    pull_request = _diff_pull_request(payload)
    # A comment needs somewhere to go, so a pull request request only makes sense
    # for a repository the GitHub API knows.
    if pull_request is not None and github_repo_from_url(repo) is None:
        raise ValidationError(
            error="unsupported_comment_repo",
            message="commenting on a pull request requires an https github.com "
            "config_repo URL",
        )
    return _DiffPlan(
        repo=repo,
        commit=commit,
        config_path=_change_config_path(payload),
        system=system,
        pull_request=pull_request,
    )


def _diff_pull_request(payload: dict[str, Any]) -> int | None:
    if "pull_request" not in payload:
        return None
    value = payload["pull_request"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(
            error="invalid_pull_request",
            message="pull_request must be a positive integer",
        )
    return value


def _diff_completion(plan: _DiffPlan, result: object) -> dict[str, Any]:
    """Log a completed diff and build the body describing it."""
    payload = _diff_result_payload(result)
    comment = payload["comment"]
    logger.info(
        "Diffed change for repo %s at commit %s: generated %s manifest file(s), "
        "comment posted: %s",
        plan.repo,
        plan.commit,
        payload["generated"],
        comment["posted"] if comment is not None else False,
    )
    return {
        "config_repo": plan.repo,
        "commit": plan.commit,
        "pull_request": plan.pull_request,
        **payload,
    }


def _diff_result_payload(result: object) -> dict[str, Any]:
    return {
        "generated": getattr(result, "generated_count", None),
        "outputs": [
            {
                "name": output.name,
                "repository": output.repository,
                "directory": str(output.directory),
                "generated": output.generated_count,
            }
            for output in getattr(result, "outputs", ())
        ],
        "diffs": [
            {
                "repository": entry.repository,
                "stat": entry.manifest_diff.stat,
                "summary": entry.manifest_diff.summary,
                "diff": entry.manifest_diff.diff,
            }
            for entry in getattr(result, "diffs", ())
        ],
        "comment": _comment_payload(getattr(result, "comment", None)),
    }


def _comment_payload(comment: object) -> dict[str, Any] | None:
    if comment is None:
        return None
    return {
        "posted": getattr(comment, "posted", False),
        "url": getattr(comment, "url", None),
        "body": getattr(comment, "body", ""),
    }


def _change_result_payload(result: object) -> dict[str, Any]:
    generated_count = getattr(result, "generated_count", None)
    return {
        "generated": generated_count,
        "outputs": [
            {
                "name": output.name,
                "repository": output.repository,
                "directory": str(output.directory),
                "generated": output.generated_count,
                "cluster": output.cluster,
                "deploy_id": output.deploy_id,
                "created_or_modified": object_ref_payloads(output.created_or_modified),
                "removed": object_ref_payloads(output.removed),
            }
            for output in getattr(result, "outputs", ())
        ],
    }


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
    return _report_processing_failure(
        request, plan.repo, plan.commit, exc, action="change"
    )


def _report_diff_failure(
    request: Request, plan: _DiffPlan, exc: ChangeProcessingError
) -> tuple[int, str, str]:
    """Log a diff processing failure and map it to a status code and error."""
    return _report_processing_failure(
        request, plan.repo, plan.commit, exc, action="diff"
    )


def _report_processing_failure(
    request: Request,
    repo: str,
    commit: str,
    exc: ChangeProcessingError,
    *,
    action: str,
) -> tuple[int, str, str]:
    if isinstance(exc, DeployConfigError):
        _log_bad_request(request, error="invalid_deploy_config", message=str(exc))
        return 400, "invalid_deploy_config", str(exc)
    if isinstance(exc, CommentPostError):
        logger.warning(
            "Failed to post a manifest diff comment for repo %s at commit %s: %s",
            repo,
            commit,
            exc,
        )
        return 502, "comment_post_failed", str(exc)
    if isinstance(exc, CredentialError):
        logger.warning(
            "Insufficient git credentials to process %s for repo %s at commit %s: %s",
            action,
            repo,
            commit,
            exc,
        )
        return 502, "git_credentials_unavailable", str(exc)
    if isinstance(exc, GitTransportError):
        logger.warning(
            "Git transport failure while processing %s for repo %s at commit %s: %s",
            action,
            repo,
            commit,
            exc,
        )
        return 502, "git_transport_failed", str(exc)
    logger.error(
        "Failed to process %s for repo %s at commit %s",
        action,
        repo,
        commit,
        exc_info=exc,
    )
    return 500, f"{action}_processing_failed", str(exc)


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


def _change_events(
    request: Request, plan: _ChangePlan, change_processor: ChangeProcessor
) -> AsyncIterator[str]:
    return _work_events(
        _StreamedWork(
            accepted={
                "config_repo": plan.repo,
                "commit": plan.commit,
                "registered": plan.registered,
            },
            run=lambda progress: change_processor.process(
                plan.repo,
                plan.commit,
                plan.manifest_image,
                plan.config_path,
                plan.system,
                progress=progress,
            ),
            complete=lambda result: _change_completion(plan, result),
            failure=lambda exc: _report_change_failure(request, plan, exc),
            unobserved=_log_unobserved_work("Change", plan.repo, plan.commit),
        )
    )


def _diff_events(
    request: Request, plan: _DiffPlan, diff_processor: DiffProcessor
) -> AsyncIterator[str]:
    return _work_events(
        _StreamedWork(
            accepted={
                "config_repo": plan.repo,
                "commit": plan.commit,
                "pull_request": plan.pull_request,
            },
            run=lambda progress: diff_processor.diff(
                plan.repo,
                plan.commit,
                plan.config_path,
                plan.system,
                pull_request=plan.pull_request,
                progress=progress,
            ),
            complete=lambda result: _diff_completion(plan, result),
            failure=lambda exc: _report_diff_failure(request, plan, exc),
            unobserved=_log_unobserved_work("Diff", plan.repo, plan.commit),
        )
    )


@dataclass(frozen=True)
class _StreamedWork:
    """Processor work to run in a worker thread and report as an event stream."""

    accepted: dict[str, Any]
    run: Callable[[ProgressSink], object]
    complete: Callable[[object], dict[str, Any]]
    failure: Callable[[ChangeProcessingError], tuple[int, str, str]]
    unobserved: Callable[[asyncio.Task[object]], None]


async def _work_events(work: _StreamedWork) -> AsyncIterator[str]:
    """Stream a processor's steps as server-sent events.

    The processor is synchronous and runs in a worker thread, so its progress
    callback hops back onto the event loop through a queue. A client that goes
    away does not cancel the work: a half pushed manifests repository is worse
    than an unobserved one.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ChangeProgress | None] = asyncio.Queue()

    def sink(event: ChangeProgress) -> None:
        # Called on the worker thread running the processor.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def process_then_close() -> object:
        try:
            return work.run(sink)
        finally:
            # The sentinel has to be posted from the worker thread through the
            # same call_soon_threadsafe path as the progress events: those are
            # then strictly ordered ahead of it. Closing the queue from the
            # awaiting coroutine instead would race, because resolving the
            # to_thread future can overtake callbacks the worker thread queued
            # before it.
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(asyncio.to_thread(process_then_close))
    outcome_reported = False
    try:
        yield _sse_event("accepted", work.accepted)
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
            status_code, error, message = work.failure(exc)
            yield _sse_event(
                "error",
                {"status": status_code, "error": error, "message": message},
            )
            return
        outcome_reported = True
        yield _sse_event("complete", work.complete(result))
    finally:
        if not outcome_reported:
            # The stream was closed before the work finished, most likely
            # because the client disconnected. Report the outcome to the log
            # instead, and retrieve any exception so asyncio does not warn.
            task.add_done_callback(work.unobserved)


def _log_unobserved_work(
    action: str, repo: str, commit: str
) -> Callable[[asyncio.Task[object]], None]:
    def log_outcome(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            logger.info(
                "%s for repo %s at commit %s completed after its event "
                "stream was closed",
                action,
                repo,
                commit,
            )
            return
        logger.warning(
            "%s for repo %s at commit %s failed after its event stream was closed: %s",
            action,
            repo,
            commit,
            exc,
        )

    return log_outcome
