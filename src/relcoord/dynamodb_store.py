# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ProxyConnectionError,
    ReadTimeoutError,
)

from relcoord.config import PersistenceSettings
from relcoord.errors import TimestampConflictError
from relcoord.models import RegisterResult
from relcoord.store import ImageInfoStore

logger = logging.getLogger(__name__)

VERSION_PREFIX = "VERSION#"
TIMESTAMP_PREFIX = "TIMESTAMP#"


class DynamoDBImageInfoStore(ImageInfoStore):
    transient_exceptions = (
        ConnectTimeoutError,
        ConnectionClosedError,
        EndpointConnectionError,
        ProxyConnectionError,
        ReadTimeoutError,
    )

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    @classmethod
    async def connect(cls, config: PersistenceSettings) -> "DynamoDBImageInfoStore":
        if config.table_name is None:
            raise ValueError("DynamoDB persistence requires table-name")
        client = boto3.client(
            "dynamodb",
            region_name=config.region_name,
            endpoint_url=config.endpoint_url,
        )
        logger.info("Using DynamoDB table %s for persistence", config.table_name)
        return cls(client, config.table_name)

    async def health_check(self) -> None:
        await asyncio.to_thread(
            self._client.describe_table,
            TableName=self._table_name,
        )

    async def register(
        self, image: str, version: str, timestamp: datetime
    ) -> RegisterResult:
        existing = await self._fetch_version_item(image, version)
        if existing is not None:
            return _register_result(image, existing, created=False)

        timestamp_iso = _timestamp_param(timestamp)
        try:
            await asyncio.to_thread(
                self._client.transact_write_items,
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _version_item(image, version, timestamp_iso),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _timestamp_item(image, version, timestamp_iso),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ],
            )
        except ClientError as exc:
            if not _is_transaction_cancelled(exc):
                raise

            existing = await self._fetch_version_item(image, version)
            if existing is not None:
                return _register_result(image, existing, created=False)

            timestamp_item = await self._fetch_timestamp_item(image, timestamp_iso)
            if timestamp_item is not None:
                raise TimestampConflictError(
                    image=image,
                    existing_version=_string_attr(timestamp_item, "version"),
                    requested_version=version,
                ) from exc
            raise

        return RegisterResult(
            image=image,
            version=version,
            timestamp=_as_datetime(timestamp_iso),
            created=True,
        )

    async def latest_for_image(self, image: str) -> str | None:
        response = await asyncio.to_thread(
            self._client.query,
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": _image_pk(image)},
                ":sk_prefix": {"S": TIMESTAMP_PREFIX},
            },
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )
        items = response.get("Items", [])
        if not items:
            return None
        return _string_attr(items[0], "version")

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    async def _fetch_version_item(
        self, image: str, version: str
    ) -> dict[str, Any] | None:
        return await self._get_item(_image_pk(image), _version_sk(version))

    async def _fetch_timestamp_item(
        self, image: str, timestamp_iso: str
    ) -> dict[str, Any] | None:
        return await self._get_item(_image_pk(image), _timestamp_sk(timestamp_iso))

    async def _get_item(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = await asyncio.to_thread(
            self._client.get_item,
            TableName=self._table_name,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return item if isinstance(item, dict) else None


def _version_item(image: str, version: str, timestamp_iso: str) -> dict[str, Any]:
    return {
        "pk": {"S": _image_pk(image)},
        "sk": {"S": _version_sk(version)},
        "kind": {"S": "version"},
        "image": {"S": image},
        "version": {"S": version},
        "timestamp": {"S": timestamp_iso},
    }


def _timestamp_item(image: str, version: str, timestamp_iso: str) -> dict[str, Any]:
    return {
        "pk": {"S": _image_pk(image)},
        "sk": {"S": _timestamp_sk(timestamp_iso)},
        "kind": {"S": "timestamp"},
        "image": {"S": image},
        "version": {"S": version},
        "timestamp": {"S": timestamp_iso},
    }


def _image_pk(image: str) -> str:
    return f"IMAGE#{image}"


def _version_sk(version: str) -> str:
    return f"{VERSION_PREFIX}{version}"


def _timestamp_sk(timestamp_iso: str) -> str:
    return f"{TIMESTAMP_PREFIX}{timestamp_iso}"


def _register_result(
    image: str, item: dict[str, Any], *, created: bool
) -> RegisterResult:
    return RegisterResult(
        image=image,
        version=_string_attr(item, "version"),
        timestamp=_as_datetime(_string_attr(item, "timestamp")),
        created=created,
    )


def _string_attr(item: dict[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("S"), str):
        raise TypeError(f"DynamoDB item attribute {name!r} must be a string")
    return value["S"]


def _timestamp_param(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _is_transaction_cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"
