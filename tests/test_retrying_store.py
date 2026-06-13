# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import logging
from datetime import UTC, datetime

import pytest

from relcoord.errors import PersistenceUnavailableError, TimestampConflictError
from relcoord.models import RegisterResult
from relcoord.retrying_store import RetryingImageInfoStore
from relcoord.store import ImageInfoStore


def test_retrying_store_retries_transient_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> RegisterResult:
        sleep = RecordingSleep()
        store = FlakyStore(failures_before_success=1)
        retrying = RetryingImageInfoStore(store, sleep=sleep)

        with caplog.at_level(logging.WARNING, logger="relcoord.retrying_store"):
            result = await retrying.register(
                "registry.example.com/team/api",
                "1.0.0",
                datetime(2026, 5, 17, tzinfo=UTC),
            )

        assert sleep.delays == [0.1]
        return result

    result = asyncio.run(run())

    assert result.created is True
    assert "register image version failed on attempt 1/3" in caplog.text


def test_retrying_store_raises_persistence_unavailable_after_exhaustion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> list[float]:
        sleep = RecordingSleep()
        store = FlakyStore(failures_before_success=3)
        retrying = RetryingImageInfoStore(store, sleep=sleep)

        with pytest.raises(PersistenceUnavailableError) as exc_info:
            with caplog.at_level(logging.ERROR, logger="relcoord.retrying_store"):
                await retrying.latest_for_image("registry.example.com/team/api")

        assert exc_info.value.operation == "fetch latest image version"
        return sleep.delays

    delays = asyncio.run(run())

    assert delays == [0.1, 0.2]
    assert "fetch latest image version failed after 3 attempt(s)" in caplog.text


def test_retrying_store_does_not_retry_timestamp_conflicts() -> None:
    async def run() -> int:
        sleep = RecordingSleep()
        store = TimestampConflictStore()
        retrying = RetryingImageInfoStore(store, sleep=sleep)

        with pytest.raises(TimestampConflictError):
            await retrying.register(
                "registry.example.com/team/api",
                "2.0.0",
                datetime(2026, 5, 17, tzinfo=UTC),
            )

        assert sleep.delays == []
        return store.calls

    assert asyncio.run(run()) == 1


def test_retrying_store_caps_exponential_backoff() -> None:
    async def run() -> list[float]:
        sleep = RecordingSleep()
        store = FlakyStore(failures_before_success=4)
        retrying = RetryingImageInfoStore(
            store,
            attempts=5,
            initial_delay_seconds=0.4,
            backoff_multiplier=2,
            max_delay_seconds=0.5,
            sleep=sleep,
        )

        await retrying.health_check()
        return sleep.delays

    assert asyncio.run(run()) == [0.4, 0.5, 0.5, 0.5]


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FlakyStore(ImageInfoStore):
    def __init__(self, failures_before_success: int) -> None:
        self._failures_before_success = failures_before_success
        self._calls = 0

    async def health_check(self) -> None:
        await self._maybe_fail()

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        await self._maybe_fail()
        return RegisterResult(
            image=image,
            version=version,
            timestamp=timestamp,
            created=True,
        )

    async def latest_for_image(self, image: str) -> str | None:
        await self._maybe_fail()
        return "1.0.0"

    async def _maybe_fail(self) -> None:
        self._calls += 1
        if self._calls <= self._failures_before_success:
            raise ConnectionError("connection dropped")


class TimestampConflictStore(ImageInfoStore):
    def __init__(self) -> None:
        self.calls = 0

    async def health_check(self) -> None:
        return None

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        self.calls += 1
        raise TimestampConflictError(
            image=image,
            existing_version="1.0.0",
            requested_version=version,
        )

    async def latest_for_image(self, image: str) -> str | None:
        return None
