# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from relcoord.app import create_app
from relcoord.change import DeployConfigError
from relcoord.in_memory_store import InMemoryImageInfoStore


@pytest.fixture
def client() -> TestClient:
    store = InMemoryImageInfoStore()
    return TestClient(create_app(store))


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    assert datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))


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
            "repo": "acme/api",
            "commit": "abc123",
            "image": "registry.example.com/team/api",
            "tag": "1.2.3",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["repo"] == "acme/api"
    assert body["commit"] == "abc123"
    assert body["registered"]["image"] == "registry.example.com/team/api"
    assert body["registered"]["version"] == "1.2.3"
    assert body["registered"]["created"] is True

    latest = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api"]},
    )
    assert latest.json() == {"versions": {"registry.example.com/team/api": "1.2.3"}}


def test_change_without_image_and_tag_acknowledges_without_registering(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/change",
        json={"repo": "acme/config", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "repo": "acme/config",
        "commit": "deadbeef",
        "registered": None,
    }


def test_change_processes_deploy_config_when_processor_is_configured() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def process(self, repo: str, commit: str) -> object:
            self.calls.append((repo, commit))
            return type("Result", (), {"generated_count": 3})()

    processor = Processor()
    client = TestClient(
        create_app(InMemoryImageInfoStore(), change_processor=processor)
    )

    response = client.post(
        "/v1/change",
        json={"repo": "https://github.com/acme/config.git", "commit": "deadbeef"},
    )

    assert response.status_code == 202
    assert processor.calls == [("https://github.com/acme/config.git", "deadbeef")]
    assert response.json() == {
        "repo": "https://github.com/acme/config.git",
        "commit": "deadbeef",
        "registered": None,
        "processed": {"generated": 3},
    }


def test_change_reports_missing_deploy_config() -> None:
    class Processor:
        def process(self, repo: str, commit: str) -> object:
            raise DeployConfigError("missing .deploy")

    client = TestClient(
        create_app(InMemoryImageInfoStore(), change_processor=Processor())
    )

    response = client.post(
        "/v1/change",
        json={"repo": "https://github.com/acme/config.git", "commit": "deadbeef"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_deploy_config",
        "message": "missing .deploy",
    }


def test_git_clone_endpoint_is_not_registered(client: TestClient) -> None:
    response = client.post("/v1/git/clone", json={"source": "https://example.com"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("json", "expected_error"),
    [
        ({"commit": "abc123"}, "invalid_repo"),
        ({"repo": "acme/api"}, "invalid_commit"),
        (
            {"repo": "acme/api", "commit": "abc123", "image": "registry.example.com/x"},
            "invalid_image_tag_pairing",
        ),
        (
            {"repo": "acme/api", "commit": "abc123", "tag": "1.2.3"},
            "invalid_image_tag_pairing",
        ),
        (
            {"repo": "acme/api", "commit": "abc123", "image": "", "tag": "1.2.3"},
            "invalid_image",
        ),
        (
            {
                "repo": "acme/api",
                "commit": "abc123",
                "image": "registry.example.com/x",
                "tag": "",
            },
            "invalid_tag",
        ),
    ],
)
def test_change_rejects_invalid_payloads(
    client: TestClient, json: dict[str, object], expected_error: str
) -> None:
    response = client.post("/v1/change", json=json)

    assert response.status_code == 400
    assert response.json()["error"] == expected_error


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
