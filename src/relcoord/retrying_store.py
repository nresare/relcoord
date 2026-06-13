# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import TypeVar

from relcoord.errors import PersistenceUnavailableError, TimestampConflictError
from relcoord.models import RegisterResult
from relcoord.store import ImageInfoStore

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_INITIAL_DELAY_SECONDS = 0.1
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_MAX_DELAY_SECONDS = 1.0

GENERIC_TRANSIENT_EXCEPTIONS = (OSError, TimeoutError, ConnectionError)

T = TypeVar("T")


class RetryingImageInfoStore(ImageInfoStore):
    def __init__(
        self,
        store: ImageInfoStore,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._store = store
        self._attempts = attempts
        self._initial_delay_seconds = initial_delay_seconds
        self._backoff_multiplier = backoff_multiplier
        self._max_delay_seconds = max_delay_seconds
        self._sleep = sleep
        self._transient_exceptions = (
            *GENERIC_TRANSIENT_EXCEPTIONS,
            *store.transient_exceptions,
        )

    @property
    def wrapped_store(self) -> ImageInfoStore:
        return self._store

    async def health_check(self) -> None:
        await self._run("persistence health check", self._store.health_check)

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        return await self._run(
            "register image version",
            lambda: self._store.register(image, version, timestamp),
        )

    async def latest_for_image(self, image: str) -> str | None:
        return await self._run(
            "fetch latest image version",
            lambda: self._store.latest_for_image(image),
        )

    async def latest_for_images(self, images: Iterable[str]) -> dict[str, str | None]:
        return await self._run(
            "fetch latest image versions",
            lambda: self._store.latest_for_images(images),
        )

    async def close(self) -> None:
        close = getattr(self._store, "close", None)
        if close is not None:
            await close()

    async def _run(self, operation: str, callback: Callable[[], Awaitable[T]]) -> T:
        delay = self._initial_delay_seconds
        for attempt in range(1, self._attempts + 1):
            try:
                return await callback()
            except TimestampConflictError:
                raise
            except self._transient_exceptions as exc:
                if attempt >= self._attempts:
                    logger.error(
                        "Persistence operation %s failed after %s attempt(s) "
                        "against backend %s",
                        operation,
                        attempt,
                        type(self._store).__name__,
                        exc_info=True,
                    )
                    raise PersistenceUnavailableError(operation) from exc

                retry_delay = min(delay, self._max_delay_seconds)
                logger.warning(
                    "Persistence operation %s failed on attempt %s/%s against "
                    "backend %s; retrying in %.3f seconds",
                    operation,
                    attempt,
                    self._attempts,
                    type(self._store).__name__,
                    retry_delay,
                    exc_info=True,
                )
                await self._sleep(retry_delay)
                delay *= self._backoff_multiplier
        raise AssertionError("retry loop exhausted without returning or raising")
