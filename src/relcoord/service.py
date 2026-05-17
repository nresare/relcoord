# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from relcoord.errors import ValidationError
from relcoord.models import RegisterResult
from relcoord.repository import ImageVersionRepository


class ImageVersionService:
    def __init__(
        self,
        repository: ImageVersionRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def register_version(
        self, image: str, version: str, timestamp: str | None = None
    ) -> RegisterResult:
        validated_image = self._validate_image(image)
        validated_version = self._validate_version(version)
        validated_timestamp = self._validate_timestamp(timestamp)
        return await self._repository.register(
            validated_image, validated_version, validated_timestamp
        )

    async def latest_versions(self, images: list[str]) -> dict[str, str | None]:
        if not isinstance(images, list):
            raise ValidationError(
                error="invalid_images",
                message="images must be an array of non-empty strings",
            )

        validated_images = [self._validate_image(image) for image in images]
        return await self._repository.latest_for_images(validated_images)

    def _validate_image(self, image: str) -> str:
        if not isinstance(image, str) or not image.strip():
            raise ValidationError(
                error="invalid_image",
                message="image must be a non-empty string",
            )
        return image

    def _validate_version(self, version: str) -> str:
        if not isinstance(version, str) or not version.strip():
            raise ValidationError(
                error="invalid_version",
                message="version must be a non-empty string",
            )
        return version

    def _validate_timestamp(self, timestamp: str | None) -> datetime:
        if timestamp is None:
            return self._normalize_timestamp(self._clock())

        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValidationError(
                error="invalid_timestamp",
                message="timestamp must be a valid RFC 3339 timestamp with timezone",
            )

        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(
                error="invalid_timestamp",
                message="timestamp must be a valid RFC 3339 timestamp with timezone",
            ) from exc

        return self._normalize_timestamp(parsed)

    def _normalize_timestamp(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValidationError(
                error="invalid_timestamp",
                message="timestamp must be a valid RFC 3339 timestamp with timezone",
            )
        return timestamp.astimezone(UTC)
