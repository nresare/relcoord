# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime

from relcoord.models import RegisterResult


class ImageInfoStore(ABC):
    transient_exceptions: tuple[type[BaseException], ...] = ()

    @abstractmethod
    async def health_check(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        raise NotImplementedError

    @abstractmethod
    async def latest_for_image(self, image: str) -> str | None:
        raise NotImplementedError

    async def latest_for_images(self, images: Iterable[str]) -> dict[str, str | None]:
        results: dict[str, str | None] = {}
        for image in images:
            if image not in results:
                results[image] = await self.latest_for_image(image)
        return results
