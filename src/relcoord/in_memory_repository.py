# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from datetime import datetime

from relcoord.errors import TimestampConflictError
from relcoord.models import RegisterResult, StoredVersion
from relcoord.repository import ImageVersionRepository


class InMemoryImageVersionRepository(ImageVersionRepository):
    def __init__(self) -> None:
        self._versions_by_image: dict[str, dict[str, StoredVersion]] = {}
        self._timestamp_index: dict[str, dict[datetime, str]] = {}

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        image_versions = self._versions_by_image.setdefault(image, {})
        if version in image_versions:
            stored = image_versions[version]
            return RegisterResult(
                image=image,
                version=stored.version,
                timestamp=stored.timestamp,
                created=False,
            )

        timestamp_index = self._timestamp_index.setdefault(image, {})
        existing_version = timestamp_index.get(timestamp)
        if existing_version is not None:
            raise TimestampConflictError(
                image=image,
                existing_version=existing_version,
                requested_version=version,
            )

        image_versions[version] = StoredVersion(
            image=image,
            version=version,
            timestamp=timestamp,
        )
        timestamp_index[timestamp] = version
        return RegisterResult(
            image=image, version=version, timestamp=timestamp, created=True
        )

    async def latest_for_image(self, image: str) -> str | None:
        image_versions = self._versions_by_image.get(image)
        if not image_versions:
            return None
        latest = max(
            image_versions.values(),
            key=lambda stored: stored.timestamp,
        )
        return latest.version
