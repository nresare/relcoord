# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from relcoord.dynamodb_store import DynamoDBImageInfoStore
from relcoord.errors import TimestampConflictError


def test_dynamodb_store_registers_and_resolves_latest_version() -> None:
    async def run() -> tuple[bool, bool, datetime, str | None, str | None]:
        store = DynamoDBImageInfoStore(FakeDynamoDBClient(), "image-versions")

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

        return (
            created.created,
            duplicate.created,
            duplicate.timestamp,
            latest,
            missing,
        )

    created, duplicate, duplicate_timestamp, latest, missing = asyncio.run(run())

    assert created is True
    assert duplicate is False
    assert duplicate_timestamp == datetime(2026, 5, 17, 10, 15, 30, tzinfo=UTC)
    assert latest == "2.0.0"
    assert missing is None


def test_dynamodb_store_rejects_timestamp_conflict() -> None:
    async def run() -> None:
        store = DynamoDBImageInfoStore(FakeDynamoDBClient(), "image-versions")

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

    asyncio.run(run())


def test_dynamodb_store_health_check_describes_table() -> None:
    async def run() -> FakeDynamoDBClient:
        client = FakeDynamoDBClient()
        store = DynamoDBImageInfoStore(client, "image-versions")

        await store.health_check()

        return client

    client = asyncio.run(run())

    assert client.described_tables == ["image-versions"]


class FakeDynamoDBClient:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self.described_tables: list[str] = []

    def describe_table(self, **kwargs: Any) -> dict[str, Any]:
        self.described_tables.append(kwargs["TableName"])
        return {"Table": {"TableName": kwargs["TableName"]}}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self._items.get((_string_value(key["pk"]), _string_value(key["sk"])))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, **kwargs: Any) -> None:
        puts = [operation["Put"] for operation in kwargs["TransactItems"]]
        keys = [self._key(put["Item"]) for put in puts]
        if any(key in self._items for key in keys):
            raise ClientError(
                error_response={
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "transaction cancelled",
                    }
                },
                operation_name="TransactWriteItems",
            )
        for put in puts:
            self._items[self._key(put["Item"])] = put["Item"].copy()

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        pk = _string_value(values[":pk"])
        sk_prefix = _string_value(values[":sk_prefix"])
        items = [
            item
            for (item_pk, item_sk), item in self._items.items()
            if item_pk == pk and item_sk.startswith(sk_prefix)
        ]
        items.sort(key=lambda item: _string_value(item["sk"]), reverse=True)
        return {"Items": items[: kwargs["Limit"]]}

    def _key(self, item: dict[str, Any]) -> tuple[str, str]:
        return (_string_value(item["pk"]), _string_value(item["sk"]))


def _string_value(value: dict[str, Any]) -> str:
    attr = value["S"]
    assert isinstance(attr, str)
    return attr
