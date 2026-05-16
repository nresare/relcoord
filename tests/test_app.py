# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import pytest
from starlette.testclient import TestClient

from relcoord.app import create_app
from relcoord.in_memory_repository import InMemoryImageVersionRepository


@pytest.fixture
def client() -> TestClient:
    repository = InMemoryImageVersionRepository()
    return TestClient(create_app(repository))


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_and_resolve_latest_version(client: TestClient) -> None:
    created = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3"},
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
        "created": True,
    }
    assert latest.status_code == 200
    assert latest.json() == {
        "versions": {
            "registry.example.com/team/api": "1.2.3",
            "registry.example.com/team/worker": None,
        }
    }


def test_reject_invalid_version(client: TestClient) -> None:
    response = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "not-semver"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_version",
        "message": "version must be valid Semantic Versioning 2.0.0",
    }


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


def test_reject_build_metadata_only_variant(client: TestClient) -> None:
    first = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3+build1"},
    )
    second = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3+build2"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "conflicting_version"
