# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from relcoord.app import (
    NoopChangeProcessor,
    NoopTokenValidator,
    RequestTokenValidator,
    create_app,
)
from relcoord.change import CredentialError, DeployConfigError, GitTransportError
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
