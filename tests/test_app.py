# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import json
import logging
import threading
from collections.abc import Callable, MutableMapping
from datetime import datetime
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from relcoord import app
from relcoord.app import (
    NoopChangeProcessor,
    NoopTokenValidator,
    RequestTokenValidator,
    create_app,
)
from relcoord.change import (
    ChangeProgress,
    CredentialError,
    DeployConfigError,
    GitTransportError,
    ProgressSink,
    ignore_progress,
)
from relcoord.errors import PersistenceUnavailableError
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.models import RegisterResult
from relcoord.store import ImageInfoStore


@pytest.fixture
def client() -> TestClient:
    store = InMemoryImageInfoStore()
    return TestClient(
        create_app(
            store,
            token_validator=NoopTokenValidator(),
            change_processor=NoopChangeProcessor(),
        )
    )


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_healthz_reports_unavailable_persistence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(
        create_app(
            UnavailableStore("persistence health check"),
            token_validator=NoopTokenValidator(),
            change_processor=NoopChangeProcessor(),
        )
    )

    with caplog.at_level(logging.WARNING, logger="relcoord.app"):
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "checks": {"database": "unavailable"},
    }
    assert (
        "Health check failed for persistence operation persistence health check"
        in caplog.text
    )


def test_logs_requests(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="relcoord.app")

    response = client.get("/healthz")

    assert response.status_code == 200
    assert "HTTP request GET /healthz completed with status 200" in caplog.text


def test_register_and_resolve_latest_version(client: TestClient) -> None:
    created = client.post(
        "/v1/image-versions",
        json={
            "image": "registry.example.com/team/api",
            "version": "1.2.3",
            "timestamp": "2026-05-17T10:15:30+00:00",
        },
    )
    latest = client.post(
        "/v1/images/latest",
        json={
            "images": [
                "registry.example.com/team/api",
                "registry.example.com/team/worker",
            ]
        },
    )

    assert created.status_code == 201
    assert created.json() == {
        "image": "registry.example.com/team/api",
        "version": "1.2.3",
        "timestamp": "2026-05-17T10:15:30Z",
        "created": True,
    }
    assert latest.status_code == 200
    assert latest.json() == {
        "versions": {
            "registry.example.com/team/api": "1.2.3",
            "registry.example.com/team/worker": None,
        }
    }


def test_register_reports_unavailable_persistence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(
        create_app(
            UnavailableStore("register image version"),
            token_validator=NoopTokenValidator(),
            change_processor=NoopChangeProcessor(),
        )
    )

    with caplog.at_level(logging.ERROR, logger="relcoord.app"):
        response = client.post(
            "/v1/image-versions",
            json={"image": "registry.example.com/team/api", "version": "1.2.3"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "message": "persistence backend unavailable",
    }
    assert (
        "Persistence operation register image version failed while handling "
        "POST /v1/image-versions"
    ) in caplog.text


def test_latest_reports_unavailable_persistence() -> None:
    client = TestClient(
        create_app(
            UnavailableStore("fetch latest image versions"),
            token_validator=NoopTokenValidator(),
            change_processor=NoopChangeProcessor(),
        )
    )

    response = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api"]},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "message": "persistence backend unavailable",
    }


def test_register_accepts_opaque_version(client: TestClient) -> None:
    response = client.post(
        "/v1/image-versions",
        json={
            "image": "registry.example.com/team/api",
            "version": "release-2026-05-17",
        },
    )

    body = response.json()
    assert response.status_code == 201
    assert body["image"] == "registry.example.com/team/api"
    assert body["version"] == "release-2026-05-17"
    assert body["created"] is True
    assert datetime.fromisoformat(body["timestamp"])


@pytest.mark.parametrize(
    ("json", "expected_error", "expected_message"),
    [
        (
            {"version": "1.2.3"},
            "invalid_image",
            "image must be a non-empty string",
        ),
        (
            {"image": 123, "version": "1.2.3"},
            "invalid_image",
            "image must be a non-empty string",
        ),
        (
            {"image": "registry.example.com/team/api"},
            "invalid_version",
            "version must be a non-empty string",
        ),
        (
            {"image": "registry.example.com/team/api", "version": 123},
            "invalid_version",
            "version must be a non-empty string",
        ),
    ],
)
def test_reject_invalid_register_request_fields(
    client: TestClient,
    json: dict[str, object],
    expected_error: str,
    expected_message: str,
) -> None:
    response = client.post("/v1/image-versions", json=json)

    assert response.status_code == 400
    assert response.json() == {
        "error": expected_error,
        "message": expected_message,
    }


@pytest.mark.parametrize(
    "json",
    [
        {},
        {"images": "registry.example.com/team/api"},
        {"images": ["registry.example.com/team/api", 123]},
        {"images": ["registry.example.com/team/api", ""]},
    ],
)
def test_reject_invalid_latest_request_fields(
    client: TestClient, json: dict[str, object]
) -> None:
    response = client.post("/v1/images/latest", json=json)

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_images",
        "message": "images must be an array of non-empty strings",
    }


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-05-17T10:15:30", None])
def test_reject_invalid_timestamp(client: TestClient, timestamp: str | None) -> None:
    response = client.post(
        "/v1/image-versions",
        json={
            "image": "registry.example.com/team/api",
            "version": "1.2.3",
            "timestamp": timestamp,
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_timestamp",
        "message": "timestamp must be a valid RFC 3339 timestamp with timezone",
    }


def test_change_registers_image_version_when_image_and_tag_present(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/api",
            "commit": "abc123",
            "image_repo": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["config_repo"] == "acme/api"
    assert body["commit"] == "abc123"
    assert body["registered"]["image"] == "registry.example.com/team/api"
    assert body["registered"]["version"] == "1.2.3"
    assert body["registered"]["created"] is True

    latest = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api"]},
    )
    assert latest.json() == {"versions": {"registry.example.com/team/api": "1.2.3"}}


def test_change_passes_image_reference_to_processor() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

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
            self.calls.append((repo, commit, image))
            return type("Result", (), {"generated_count": 1})()

    processor = Processor()
    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=processor,
        )
    )

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/api",
            "commit": "abc123",
            "image_repo": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
    )

    assert response.status_code == 202
    assert processor.calls == [
        ("acme/api", "abc123", "registry.example.com/team/api:1.2.3")
    ]


def test_change_without_image_and_tag_acknowledges_without_registering(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="relcoord.app"):
        response = client.post(
            "/v1/change",
            json={"config_repo": "acme/config", "commit": "deadbeef"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "config_repo": "acme/config",
        "commit": "deadbeef",
        "registered": None,
        "processed": {"generated": 0},
    }
    assert (
        "change processing disabled: no manifests_repository configured; skipping "
        "source checkout, manifest-builder invocation, manifests commit, and push "
        "for repo acme/config at commit deadbeef"
    ) in caplog.text
    assert (
        "Processed change for repo acme/config at commit deadbeef: generated 0 "
        "manifest file(s)"
    ) in caplog.text


def test_change_processes_deploy_config_when_processor_is_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

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
            self.calls.append((repo, commit, image))
            return type("Result", (), {"generated_count": 3})()

    processor = Processor()
    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=processor,
        )
    )

    with caplog.at_level(logging.INFO, logger="relcoord.app"):
        response = client.post(
            "/v1/change",
            json={
                "config_repo": "https://github.com/acme/config.git",
                "commit": "deadbeef",
            },
        )

    assert response.status_code == 202
    assert processor.calls == [("https://github.com/acme/config.git", "deadbeef", None)]
    assert response.json() == {
        "config_repo": "https://github.com/acme/config.git",
        "commit": "deadbeef",
        "registered": None,
        "processed": {"generated": 3},
    }
    assert (
        "Processing change for repo https://github.com/acme/config.git at commit "
        "deadbeef with image None"
    ) in caplog.text
    assert (
        "Processed change for repo https://github.com/acme/config.git at commit "
        "deadbeef: generated 3 manifest file(s)"
    ) in caplog.text


def test_change_processor_logs_from_worker_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Processor:
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
            logging.getLogger("relcoord.change").info(
                "processor logged for %s at %s with image %s", repo, commit, image
            )
            return type("Result", (), {"generated_count": 1})()

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=Processor(),
        )
    )

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/v1/change",
            json={
                "config_repo": "https://github.com/acme/config.git",
                "commit": "deadbeef",
            },
        )

    assert response.status_code == 202
    assert (
        "processor logged for https://github.com/acme/config.git at deadbeef "
        "with image None"
    ) in caplog.text


def test_change_converts_github_ssh_style_repo_uri() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

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
            self.calls.append((repo, commit, image))
            return type("Result", (), {"generated_count": 0})()

    processor = Processor()
    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=processor,
        )
    )

    response = client.post(
        "/v1/change",
        json={"config_repo": "git@github.com:acme/config.git", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert processor.calls == [("https://github.com/acme/config.git", "deadbeef", None)]
    assert response.json()["config_repo"] == "https://github.com/acme/config.git"


def test_change_rejects_non_github_ssh_style_repo_uri(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=NoopChangeProcessor(),
        )
    )
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "git@gitlab.example.com:acme/config.git",
            "commit": "deadbeef",
            "image_repo": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "unsupported_ssh_git_uri",
        "message": "ssh style git URIs are only supported for github.com repositories",
    }
    assert (
        "Bad request POST /v1/change: unsupported_ssh_git_uri: "
        "ssh style git URIs are only supported for github.com repositories"
        in caplog.text
    )

    latest = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api"]},
    )
    assert latest.json() == {"versions": {"registry.example.com/team/api": None}}


def test_change_reports_missing_deploy_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Processor:
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
            raise DeployConfigError("missing .deploy")

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=Processor(),
        )
    )
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "https://github.com/acme/config.git",
            "commit": "deadbeef",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_deploy_config",
        "message": "missing .deploy",
    }
    assert (
        "Bad request POST /v1/change: invalid_deploy_config: missing .deploy"
        in caplog.text
    )


def test_change_reports_credential_error_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Processor:
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
            raise CredentialError(
                "failed to obtain git credentials while checking out source repo "
                "https://github.com/acme/config.git: idcat returned HTTP 401"
            )

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=Processor(),
        )
    )
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "https://github.com/acme/config.git",
            "commit": "deadbeef",
        },
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "git_credentials_unavailable",
        "message": (
            "failed to obtain git credentials while checking out source repo "
            "https://github.com/acme/config.git: idcat returned HTTP 401"
        ),
    }
    assert (
        "Insufficient git credentials to process change for repo "
        "https://github.com/acme/config.git at commit deadbeef" in caplog.text
    )
    # The expected condition must not be logged with a stack trace.
    assert "Traceback (most recent call last)" not in caplog.text


def test_change_reports_git_transport_error_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Processor:
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
            raise GitTransportError(
                "dulwich clone failed: dulwich.errors.NotGitRepository"
            )

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=Processor(),
        )
    )
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "https://github.com/acme/config.git",
            "commit": "deadbeef",
        },
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "git_transport_failed",
        "message": "dulwich clone failed: dulwich.errors.NotGitRepository",
    }
    assert (
        "Git transport failure while processing change for repo "
        "https://github.com/acme/config.git at commit deadbeef" in caplog.text
    )
    # The error must be reported without dumping a stack trace.
    assert "Traceback (most recent call last)" not in caplog.text


def _config_path_recording_client() -> tuple[TestClient, list[str]]:
    config_paths: list[str] = []

    class Processor:
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
            config_paths.append(config_path)
            return type("Result", (), {"generated_count": 0})()

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=Processor(),
        )
    )
    return client, config_paths


def test_change_defaults_config_path_to_deploy() -> None:
    client, config_paths = _config_path_recording_client()

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert config_paths == [".deploy"]


def test_change_passes_custom_config_path_to_processor() -> None:
    client, config_paths = _config_path_recording_client()

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/config",
            "commit": "deadbeef",
            "config_path": "deploy/system",
        },
    )

    assert response.status_code == 202
    assert config_paths == ["deploy/system"]


@pytest.mark.parametrize(
    "config_path",
    ["", "   ", "/etc/passwd", "../escape", "deploy/../../etc"],
)
def test_change_rejects_invalid_config_path(config_path: str) -> None:
    client, config_paths = _config_path_recording_client()

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/config",
            "commit": "deadbeef",
            "config_path": config_path,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_config_path"
    assert config_paths == []


class _StubPrincipal:
    def __init__(self, allow_system: bool) -> None:
        self.allow_system = allow_system


class _StubValidator:
    def __init__(self, allow_system: bool) -> None:
        self._allow_system = allow_system

    def validate(self, authorization_header: str | None) -> object:
        return _StubPrincipal(self._allow_system)


def _system_recording_client(
    token_validator: RequestTokenValidator | None = None,
) -> tuple[TestClient, list[bool]]:
    systems: list[bool] = []

    class Processor:
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
            systems.append(system)
            return type("Result", (), {"generated_count": 0})()

    client = TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=(
                token_validator if token_validator is not None else NoopTokenValidator()
            ),
            change_processor=Processor(),
        )
    )
    return client, systems


def test_change_defaults_system_to_false() -> None:
    client, systems = _system_recording_client()

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert systems == [False]


def test_change_system_mode_passes_through_to_processor() -> None:
    client, systems = _system_recording_client()

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/system", "commit": "deadbeef", "system": True},
    )

    assert response.status_code == 202
    assert systems == [True]


@pytest.mark.parametrize("value", ["true", 1, "yes", 0])
def test_change_rejects_non_boolean_system(value: object) -> None:
    client, systems = _system_recording_client()

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef", "system": value},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_system"
    assert systems == []


def test_change_rejects_system_with_config_path() -> None:
    client, systems = _system_recording_client()

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/system",
            "commit": "deadbeef",
            "system": True,
            "config_path": "deploy",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_system_config_path"
    assert systems == []


def test_change_rejects_system_with_image() -> None:
    client, systems = _system_recording_client()

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/system",
            "commit": "deadbeef",
            "system": True,
            "image_repo": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_system_image"
    assert systems == []


def test_change_system_mode_rejected_when_role_disallows() -> None:
    client, systems = _system_recording_client(_StubValidator(allow_system=False))

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/system", "commit": "deadbeef", "system": True},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "system_not_allowed"
    assert systems == []


def test_change_system_mode_allowed_when_role_permits() -> None:
    client, systems = _system_recording_client(_StubValidator(allow_system=True))

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/system", "commit": "deadbeef", "system": True},
    )

    assert response.status_code == 202
    assert systems == [True]


def test_change_non_system_allowed_when_role_disallows_system() -> None:
    # A role without allow_system can still make ordinary (non-system) changes.
    client, systems = _system_recording_client(_StubValidator(allow_system=False))

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert systems == [False]


def test_git_clone_endpoint_is_not_registered(client: TestClient) -> None:
    response = client.post("/v1/git/clone", json={"source": "https://example.com"})

    assert response.status_code == 404


class UnavailableStore(ImageInfoStore):
    def __init__(self, operation: str) -> None:
        self._operation = operation

    async def health_check(self) -> None:
        raise PersistenceUnavailableError(self._operation)

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        raise PersistenceUnavailableError(self._operation)

    async def latest_for_image(self, image: str) -> str | None:
        raise PersistenceUnavailableError(self._operation)


@pytest.mark.parametrize(
    ("json", "expected_error", "expected_message"),
    [
        (
            {"commit": "abc123"},
            "invalid_config_repo",
            "config_repo must be a non-empty string",
        ),
        (
            {"config_repo": "acme/api"},
            "invalid_commit",
            "commit must be a non-empty string",
        ),
        (
            {
                "config_repo": "acme/api",
                "commit": "abc123",
                "image_repo": "registry.example.com/x",
            },
            "invalid_image_repo_tag_pairing",
            "image_repo and tag must be provided together",
        ),
        (
            {"config_repo": "acme/api", "commit": "abc123", "tag": "1.2.3"},
            "invalid_image_repo_tag_pairing",
            "image_repo and tag must be provided together",
        ),
        (
            {
                "config_repo": "acme/api",
                "commit": "abc123",
                "image_repo": "",
                "tag": "1.2.3",
            },
            "invalid_image_repo",
            "image_repo must be a non-empty string",
        ),
        (
            {
                "config_repo": "acme/api",
                "commit": "abc123",
                "image_repo": "registry.example.com/x",
                "tag": "",
            },
            "invalid_tag",
            "tag must be a non-empty string",
        ),
    ],
)
def test_change_rejects_invalid_payloads(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    json: dict[str, object],
    expected_error: str,
    expected_message: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post("/v1/change", json=json)

    assert response.status_code == 400
    assert response.json() == {
        "error": expected_error,
        "message": expected_message,
    }
    assert (
        f"Bad request POST /v1/change: {expected_error}: {expected_message}"
        in caplog.text
    )


def test_reject_timestamp_conflict(client: TestClient) -> None:
    first = client.post(
        "/v1/image-versions",
        json={
            "image": "registry.example.com/team/api",
            "version": "1.2.3",
            "timestamp": "2026-05-17T10:15:30Z",
        },
    )
    second = client.post(
        "/v1/image-versions",
        json={
            "image": "registry.example.com/team/api",
            "version": "2.0.0",
            "timestamp": "2026-05-17T10:15:30Z",
        },
    )

    assert first.status_code == 201
    assert second.status_code == 400
    assert second.json()["error"] == "timestamp_conflict"


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into (event name, decoded data) pairs, ignoring comments."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        name: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if name is not None and data is not None:
            events.append((name, json.loads(data)))
    return events


class ReportingProcessor:
    """A processor that reports the progress steps it is constructed with."""

    def __init__(
        self, steps: list[ChangeProgress], failure: Exception | None = None
    ) -> None:
        self._steps = steps
        self._failure = failure

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
        for step in self._steps:
            progress(step)
        if self._failure is not None:
            raise self._failure
        return type("Result", (), {"generated_count": len(self._steps)})()


def streaming_client(
    steps: list[ChangeProgress], failure: Exception | None = None
) -> TestClient:
    return TestClient(
        create_app(
            InMemoryImageInfoStore(),
            token_validator=NoopTokenValidator(),
            change_processor=ReportingProcessor(steps, failure),
        )
    )


def test_change_streams_progress_when_client_accepts_event_stream() -> None:
    client = streaming_client(
        [
            ChangeProgress(
                phase="source-checkout",
                message="checking out source repo acme/config at commit deadbeef",
                detail={"repo": "acme/config", "commit": "deadbeef"},
            ),
            ChangeProgress(
                phase="generated",
                message="manifest-builder generated 2 file(s) for output manifests",
                detail={"generated": 2},
            ),
        ]
    )

    response = client.post(
        "/v1/change",
        json={
            "config_repo": "acme/config",
            "commit": "deadbeef",
            "image_repo": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
        headers={"accept": "text/event-stream"},
    )

    assert response.status_code == 202
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "accepted",
        "progress",
        "progress",
        "complete",
    ]

    accepted = events[0][1]
    assert accepted["config_repo"] == "acme/config"
    assert accepted["commit"] == "deadbeef"
    registered = accepted["registered"]
    assert registered["version"] == "1.2.3"

    assert events[1][1] == {
        "phase": "source-checkout",
        "message": "checking out source repo acme/config at commit deadbeef",
        "detail": {"repo": "acme/config", "commit": "deadbeef"},
    }
    assert events[2][1]["phase"] == "generated"
    assert events[3][1] == {
        "config_repo": "acme/config",
        "commit": "deadbeef",
        "registered": registered,
        "processed": {"generated": 2},
    }


class BlockingProcessor:
    """A processor that waits for the test to release it mid-change."""

    def __init__(self, released: threading.Event) -> None:
        self._released = released

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
        progress(ChangeProgress(phase="workspace", message="created workspace"))
        if not self._released.wait(timeout=10):
            raise AssertionError("the event stream never observed the first step")
        progress(ChangeProgress(phase="pushed", message="pushed manifests commit"))
        return type("Result", (), {"generated_count": 1})()


async def collect_change_chunks(
    application: Starlette,
    payload: dict[str, object],
    release: threading.Event,
    release_when: Callable[[list[bytes]], bool],
) -> tuple[int, list[bytes]]:
    """Drive the ASGI app directly, collecting body chunks as they are sent.

    Starlette's TestClient buffers the whole response before handing it back, so
    observing genuinely incremental delivery means speaking ASGI. ``release`` is
    set as soon as ``release_when`` accepts the chunks received so far, which
    lets a test unblock the change processor only after the stream has already
    delivered something.
    """
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/change",
        "raw_path": b"/v1/change",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"accept", b"text/event-stream"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    request_messages: list[MutableMapping[str, Any]] = [
        {"type": "http.request", "body": body, "more_body": False}
    ]
    chunks: list[bytes] = []
    status = 0

    async def receive() -> MutableMapping[str, Any]:
        if request_messages:
            return request_messages.pop(0)
        # Never disconnect; the response ends when the stream does.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                chunks.append(chunk)
                if not release.is_set() and release_when(chunks):
                    release.set()

    await application(scope, receive, send)
    return status, chunks


def test_change_streams_progress_before_the_change_completes() -> None:
    released = threading.Event()
    application = create_app(
        InMemoryImageInfoStore(),
        token_validator=NoopTokenValidator(),
        change_processor=BlockingProcessor(released),
    )

    status, chunks = asyncio.run(
        collect_change_chunks(
            application,
            {"config_repo": "acme/config", "commit": "deadbeef"},
            released,
            # The processor stays blocked until its first step has been sent, so
            # reaching the end at all proves the response is not buffered.
            lambda chunks: any(b"created workspace" in chunk for chunk in chunks),
        )
    )

    assert status == 202
    assert [name for name, _ in parse_sse(b"".join(chunks).decode())] == [
        "accepted",
        "progress",
        "progress",
        "complete",
    ]
    # Each event is flushed on its own rather than coalesced into one body.
    assert len(chunks) >= 4


def test_change_stream_sends_heartbeats_while_a_step_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    released = threading.Event()
    application = create_app(
        InMemoryImageInfoStore(),
        token_validator=NoopTokenValidator(),
        change_processor=BlockingProcessor(released),
    )

    _status, chunks = asyncio.run(
        collect_change_chunks(
            application,
            {"config_repo": "acme/config", "commit": "deadbeef"},
            released,
            lambda chunks: sum(chunk.startswith(b":") for chunk in chunks) >= 2,
        )
    )

    assert sum(chunk.startswith(b": keep-alive") for chunk in chunks) >= 2


def test_change_streams_terminal_error_event_when_processing_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = streaming_client(
        [ChangeProgress(phase="workspace", message="created temporary workspace")],
        failure=GitTransportError("dulwich clone failed: NotGitRepository"),
    )
    caplog.set_level(logging.WARNING, logger="relcoord.app")

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
        headers={"accept": "text/event-stream"},
    )

    # The status is committed before the change runs, so the failure is in band.
    assert response.status_code == 202
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["accepted", "progress", "error"]
    assert events[2][1] == {
        "status": 502,
        "error": "git_transport_failed",
        "message": "dulwich clone failed: NotGitRepository",
    }
    assert (
        "Git transport failure while processing change for repo acme/config "
        "at commit deadbeef" in caplog.text
    )


def test_change_rejects_invalid_payload_with_status_code_when_streaming(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/change",
        json={"commit": "abc123"},
        headers={"accept": "text/event-stream"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_config_repo"


def test_change_returns_json_when_event_stream_is_not_accepted() -> None:
    client = streaming_client(
        [ChangeProgress(phase="workspace", message="created temporary workspace")]
    )

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
        headers={"accept": "*/*"},
    )

    assert response.status_code == 202
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "config_repo": "acme/config",
        "commit": "deadbeef",
        "registered": None,
        "processed": {"generated": 1},
    }


@pytest.mark.parametrize(
    ("accept", "streams"),
    [
        ("text/event-stream", True),
        ("text/event-stream;q=0.9", True),
        ("application/json, text/event-stream", True),
        ("TEXT/EVENT-STREAM", True),
        ("*/*", False),
        ("application/json", False),
        ("", False),
    ],
)
def test_change_negotiates_event_stream_on_accept_header(
    accept: str, streams: bool
) -> None:
    client = streaming_client([])

    response = client.post(
        "/v1/change",
        json={"config_repo": "acme/config", "commit": "deadbeef"},
        headers={"accept": accept},
    )

    assert response.status_code == 202
    is_stream = response.headers["content-type"].startswith("text/event-stream")
    assert is_stream is streams
