# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from surrealdb import AsyncSurreal

from relcoord.config import IdmouseSettings
from relcoord.errors import TimestampConflictError
from relcoord.surreal_store import (
    IdmouseClient,
    IdmouseTokenLease,
    SurrealImageInfoStore,
    jwt_claims,
)


def test_surreal_store_registers_and_resolves_latest_version() -> None:
    async def run() -> None:
        db = AsyncSurreal("mem://")
        await db.connect("mem://")
        await db.use("test", "test")
        store = SurrealImageInfoStore(db)
        await store.setup_db()
        try:
            created = await store.register(
                "registry.example.com/team/api",
                "1.0.0",
                datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC),
            )
            await store.register(
                "registry.example.com/team/api",
                "2.0.0",
                datetime(2026, 5, 18, 10, 15, 30, tzinfo=UTC),
            )
            duplicate = await store.register(
                "registry.example.com/team/api",
                "1.0.0",
                datetime(2026, 5, 19, 10, 15, 30, tzinfo=UTC),
            )
            latest = await store.latest_for_image("registry.example.com/team/api")
            missing = await store.latest_for_image("registry.example.com/team/worker")
        finally:
            await store.close()

        assert created.created is True
        assert duplicate.created is False
        assert duplicate.timestamp == datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC)
        assert latest == "2.0.0"
        assert missing is None

    asyncio.run(run())


def test_surreal_store_rejects_timestamp_conflict() -> None:
    async def run() -> None:
        db = AsyncSurreal("mem://")
        await db.connect("mem://")
        await db.use("test", "test")
        store = SurrealImageInfoStore(db)
        await store.setup_db()
        try:
            await store.register(
                "registry.example.com/team/api",
                "1.0.0",
                datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC),
            )
            with pytest.raises(TimestampConflictError):
                await store.register(
                    "registry.example.com/team/api",
                    "2.0.0",
                    datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC),
                )
        finally:
            await store.close()

    asyncio.run(run())


def test_surreal_store_health_check_queries_database() -> None:
    async def run() -> FakeSurrealDb:
        db = FakeSurrealDb()
        store = SurrealImageInfoStore(db)

        await store.health_check()

        return db

    db = asyncio.run(run())

    assert db.queries == ["INFO FOR DB;"]


def test_idmouse_client_fetches_token_with_local_bearer_token(tmp_path: Path) -> None:
    async def run() -> IdmouseTokenLease:
        token_file = tmp_path / "idmouse-bearer-token"
        token_file.write_text("local-bearer-token\n")

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer local-bearer-token"
            return httpx.Response(
                200,
                json={
                    "access_token": _jwt({"ns": "default", "db": "relcoord"}),
                    "expires_in": 60,
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = IdmouseClient(
            IdmouseSettings(
                url="http://idmouse.example.test/token",
                token_path=token_file,
            ),
            client=http_client,
        )
        try:
            return await client.fetch_token_lease()
        finally:
            await client.close()
            await http_client.aclose()

    lease = asyncio.run(run())

    assert lease.access_token == _jwt({"ns": "default", "db": "relcoord"})
    assert lease.expires_in == 60


def test_jwt_claims_decodes_middle_segment() -> None:
    assert jwt_claims(_jwt({"ns": "default", "db": "relcoord"})) == {
        "ns": "default",
        "db": "relcoord",
    }


def _jwt(claims: dict[str, str]) -> str:
    header = _b64({"alg": "none"})
    payload = _b64(claims)
    return f"{header}.{payload}.signature"


def _b64(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")


class FakeSurrealDb:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(
        self, query: str, vars: dict[str, object] | None = None
    ) -> list[object]:
        self.queries.append(query)
        return []
