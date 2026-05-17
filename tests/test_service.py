# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
from datetime import UTC, datetime

import pytest

from relcoord.errors import TimestampConflictError, ValidationError
from relcoord.in_memory_repository import InMemoryImageVersionRepository
from relcoord.service import ImageVersionService


def test_register_version_is_idempotent() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    created = asyncio.run(
        service.register_version(
            "registry.example.com/team/api",
            "1.2.3",
            "2026-05-17T10:15:30Z",
        )
    )
    duplicate = asyncio.run(
        service.register_version(
            "registry.example.com/team/api",
            "1.2.3",
            "2026-05-18T10:15:30Z",
        )
    )

    assert created.created is True
    assert duplicate.created is False
    assert duplicate.timestamp == datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC)


def test_register_uses_call_time_when_timestamp_is_omitted() -> None:
    now = datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC)
    service = ImageVersionService(InMemoryImageVersionRepository(), clock=lambda: now)

    result = asyncio.run(
        service.register_version("registry.example.com/team/api", "release-2026-05-17")
    )

    assert result.timestamp == now


def test_register_rejects_timestamp_conflict() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    asyncio.run(
        service.register_version(
            "registry.example.com/team/api",
            "1.2.3",
            "2026-05-17T10:15:30Z",
        )
    )

    with pytest.raises(TimestampConflictError):
        asyncio.run(
            service.register_version(
                "registry.example.com/team/api",
                "2026.05.17",
                "2026-05-17T10:15:30Z",
            )
        )


def test_register_rejects_timestamp_without_timezone() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    with pytest.raises(ValidationError) as exc_info:
        asyncio.run(
            service.register_version(
                "registry.example.com/team/api",
                "1.2.3",
                "2026-05-17T10:15:30",
            )
        )

    assert exc_info.value.error == "invalid_timestamp"


def test_latest_versions_returns_latest_timestamp() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    asyncio.run(
        service.register_version(
            "registry.example.com/team/api",
            "2.0.0",
            "2026-05-17T10:15:30Z",
        )
    )
    asyncio.run(
        service.register_version(
            "registry.example.com/team/api",
            "1.0.0",
            "2026-05-18T10:15:30Z",
        )
    )

    versions = asyncio.run(service.latest_versions(["registry.example.com/team/api"]))

    assert versions == {"registry.example.com/team/api": "1.0.0"}


def test_latest_versions_allows_empty_image_lists() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    assert asyncio.run(service.latest_versions([])) == {}
