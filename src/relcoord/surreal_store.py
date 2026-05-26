# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import json
import logging
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import surrealdb
from surrealdb import AsyncSurreal

from relcoord.config import IdmouseSettings, PersistenceSettings
from relcoord.errors import TimestampConflictError
from relcoord.models import RegisterResult
from relcoord.store import ImageInfoStore

logger = logging.getLogger(__name__)

RENEW_MARGIN_SECONDS = 10
INITIAL_RENEW_RETRY_SECONDS = 1
MAX_RETRIES = 5


@dataclass(frozen=True)
class IdmouseTokenLease:
    access_token: str
    expires_in: int


class IdmouseClient:
    def __init__(
        self,
        config: IdmouseSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def fetch_token_lease(self) -> IdmouseTokenLease:
        bearer_token = self._config.bearer_token()
        logger.info(
            "Requesting SurrealDB authentication token from idmouse endpoint %s",
            self._config.url,
        )
        response = await self._client.post(
            self._config.url,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        response.raise_for_status()
        payload = response.json()

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("idmouse returned an empty access_token")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise ValueError("idmouse returned an invalid expires_in")

        logger.info(
            "Received SurrealDB authentication token from idmouse endpoint %s",
            self._config.url,
        )
        claims = jwt_claims(access_token)
        if claims is not None:
            logger.debug(
                "Authenticating to SurrealDB with idmouse JWT claims %s", claims
            )
        return IdmouseTokenLease(access_token=access_token, expires_in=expires_in)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class SurrealImageInfoStore(ImageInfoStore):
    def __init__(
        self,
        db: Any,
        idmouse_client: IdmouseClient | None = None,
    ) -> None:
        self._db = db
        self._idmouse_client = idmouse_client
        self._renew_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, config: PersistenceSettings) -> "SurrealImageInfoStore":
        if config.uri is None or config.idmouse is None:
            raise ValueError("SurrealDB persistence requires uri and idmouse settings")
        db = AsyncSurreal(config.uri)
        await db.connect(config.uri)

        idmouse_client = IdmouseClient(config.idmouse)
        initial_lease = await idmouse_client.fetch_token_lease()
        await db.authenticate(initial_lease.access_token)
        logger.info(
            "Authenticated to SurrealDB using idmouse token from %s",
            config.idmouse.url,
        )

        await db.use(config.namespace, config.database)
        store = cls(db, idmouse_client)
        await store.setup_db()
        logger.info(
            "Connected to SurrealDB at %s using namespace %s and database %s",
            config.uri,
            config.namespace,
            config.database,
        )
        store._start_token_renewal(idmouse_client, initial_lease)
        return store

    async def setup_db(self) -> None:
        await self._db.query(
            """
            DEFINE INDEX IF NOT EXISTS imageVersion
                ON image_version FIELDS image, version UNIQUE;
            DEFINE INDEX IF NOT EXISTS imageTimestamp
                ON image_version FIELDS image, timestamp UNIQUE;
            """
        )

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        existing = await self._fetch_by_image_and_version(image, version)
        if existing is not None:
            return RegisterResult(
                image=image,
                version=existing["version"],
                timestamp=_as_datetime(existing["timestamp"]),
                created=False,
            )

        timestamp_iso = _timestamp_param(timestamp)
        try:
            created = await self._db.query(
                """
                CREATE image_version CONTENT {
                    image: $image,
                    version: $version,
                    timestamp: <datetime>$timestamp
                };
                """,
                {"image": image, "version": version, "timestamp": timestamp_iso},
            )
        except surrealdb.SurrealError as exc:
            if _is_duplicate_image_version_error(exc):
                existing = await self._fetch_by_image_and_version(image, version)
                if existing is not None:
                    return RegisterResult(
                        image=image,
                        version=existing["version"],
                        timestamp=_as_datetime(existing["timestamp"]),
                        created=False,
                    )
            if _is_duplicate_timestamp_error(exc):
                existing = await self._fetch_by_image_and_timestamp(image, timestamp)
                if existing is not None:
                    raise TimestampConflictError(
                        image=image,
                        existing_version=existing["version"],
                        requested_version=version,
                    ) from exc
            raise

        record = _first_record(created)
        return RegisterResult(
            image=image,
            version=record["version"],
            timestamp=_as_datetime(record["timestamp"]),
            created=True,
        )

    async def latest_for_image(self, image: str) -> str | None:
        result = await self._db.query(
            """
            SELECT version, timestamp FROM image_version
            WHERE image = $image
            ORDER BY timestamp DESC
            LIMIT 1;
            """,
            {"image": image},
        )
        record = _first_record_or_none(result)
        if record is None:
            return None
        version = record.get("version")
        return version if isinstance(version, str) else None

    async def close(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
        if self._idmouse_client is not None:
            await self._idmouse_client.close()
        await self._db.close()

    async def _fetch_by_image_and_version(
        self, image: str, version: str
    ) -> dict[str, Any] | None:
        result = await self._db.query(
            """
            SELECT * FROM image_version
            WHERE image = $image AND version = $version
            LIMIT 1;
            """,
            {"image": image, "version": version},
        )
        return _first_record_or_none(result)

    async def _fetch_by_image_and_timestamp(
        self, image: str, timestamp: datetime
    ) -> dict[str, Any] | None:
        result = await self._db.query(
            """
            SELECT * FROM image_version
            WHERE image = $image AND timestamp = <datetime>$timestamp
            LIMIT 1;
            """,
            {"image": image, "timestamp": _timestamp_param(timestamp)},
        )
        return _first_record_or_none(result)

    def _start_token_renewal(
        self, idmouse_client: IdmouseClient, initial_lease: IdmouseTokenLease
    ) -> None:
        self._renew_task = asyncio.create_task(
            self._renew_authentication(idmouse_client, initial_lease)
        )

    async def _renew_authentication(
        self, idmouse_client: IdmouseClient, lease: IdmouseTokenLease
    ) -> None:
        while True:
            delay = max(lease.expires_in - RENEW_MARGIN_SECONDS, 1)
            await asyncio.sleep(delay)

            retry = 0
            while True:
                try:
                    next_lease = await idmouse_client.fetch_token_lease()
                    await self._db.authenticate(next_lease.access_token)
                except Exception:
                    if retry >= MAX_RETRIES:
                        logger.warning(
                            "Failed to renew SurrealDB authentication; giving up",
                            exc_info=True,
                        )
                        return
                    await asyncio.sleep(INITIAL_RENEW_RETRY_SECONDS * (2**retry))
                    retry += 1
                    continue
                break

            logger.info("Renewed SurrealDB authentication from idmouse")
            lease = next_lease


def jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 3:
        return None
    claims = parts[1]
    padding = "=" * (-len(claims) % 4)
    try:
        decoded = urlsafe_b64decode(claims + padding)
        parsed = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_record(result: Any) -> dict[str, Any]:
    record = _first_record_or_none(result)
    if record is None:
        raise RuntimeError("SurrealDB query returned no record")
    return record


def _first_record_or_none(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, list) or not result:
        return None
    record = result[0]
    return record if isinstance(record, dict) else None


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise TypeError(f"Expected datetime-compatible value, got {type(value)!r}")


def _timestamp_param(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_duplicate_image_version_error(exc: Exception) -> bool:
    return "Database index `imageVersion` already contains" in str(exc)


def _is_duplicate_timestamp_error(exc: Exception) -> bool:
    return "Database index `imageTimestamp` already contains" in str(exc)
