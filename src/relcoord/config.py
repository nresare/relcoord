# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from relcoord.auth import RoleConfig


def _read_secret_file(path: Path) -> str:
    return path.read_text().rstrip("\r\n")


@dataclass(frozen=True)
class IdmouseSettings:
    url: str
    token_path: Path

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "IdmouseSettings":
        token_path = data.get("token-path")
        if not isinstance(data.get("url"), str) or not data["url"].strip():
            raise ValueError("persistence.idmouse.url must be a non-empty string")
        if not isinstance(token_path, str) or not token_path.strip():
            raise ValueError(
                "persistence.idmouse.token-path must be a non-empty string"
            )
        return cls(url=data["url"], token_path=Path(token_path))

    def bearer_token(self) -> str:
        return _read_secret_file(self.token_path)


@dataclass(frozen=True)
class PersistenceSettings:
    backend: Literal["in-memory", "surrealdb", "dynamodb"] = "surrealdb"
    uri: str | None = None
    idmouse: IdmouseSettings | None = None
    namespace: str = "default"
    database: str = "relcoord"
    table_name: str | None = None
    region_name: str | None = None
    endpoint_url: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PersistenceSettings":
        backend_value = _string_or_default(data, "backend", cls.backend)
        if backend_value not in ("in-memory", "surrealdb", "dynamodb"):
            raise ValueError(
                "persistence.backend must be one of "
                "'in-memory', 'surrealdb', or 'dynamodb'"
            )
        backend = cast(Literal["in-memory", "surrealdb", "dynamodb"], backend_value)

        if backend == "in-memory":
            return cls(
                backend=backend,
                uri=_optional_persistence_string(data, "uri"),
                namespace=_string_or_default(data, "namespace", cls.namespace),
                database=_string_or_default(data, "database", cls.database),
                idmouse=(
                    IdmouseSettings.from_mapping(data["idmouse"])
                    if "idmouse" in data
                    else None
                ),
                table_name=_optional_persistence_string(data, "table-name"),
                region_name=_optional_persistence_string(data, "region-name"),
                endpoint_url=_optional_persistence_string(data, "endpoint-url"),
            )

        if backend == "dynamodb":
            table_name = data.get("table-name")
            if not isinstance(table_name, str) or not table_name.strip():
                raise ValueError("persistence.table-name must be a non-empty string")
            return cls(
                backend=backend,
                table_name=table_name,
                region_name=_optional_persistence_string(data, "region-name"),
                endpoint_url=_optional_persistence_string(data, "endpoint-url"),
            )

        if not isinstance(data.get("uri"), str) or not data["uri"].strip():
            raise ValueError("persistence.uri must be a non-empty string")

        idmouse_data = data.get("idmouse")
        if idmouse_data is None:
            raise ValueError("persistence.idmouse must be configured")
        if not isinstance(idmouse_data, dict):
            raise ValueError("persistence.idmouse must be a table")

        return cls(
            backend=backend,
            uri=data["uri"],
            namespace=_string_or_default(data, "namespace", cls.namespace),
            database=_string_or_default(data, "database", cls.database),
            idmouse=IdmouseSettings.from_mapping(idmouse_data),
        )


@dataclass(frozen=True)
class IdcatSettings:
    endpoint: str
    github_app: str
    token_path: Path

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "IdcatSettings":
        endpoint = data.get("endpoint")
        github_app = data.get("github-app")
        token_path = data.get("token-path")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("idcat.endpoint must be a non-empty string")
        if not isinstance(github_app, str) or not github_app.strip():
            raise ValueError("idcat.github-app must be a non-empty string")
        if not isinstance(token_path, str) or not token_path.strip():
            raise ValueError("idcat.token-path must be a non-empty string")
        return cls(
            endpoint=endpoint,
            github_app=github_app,
            token_path=Path(token_path),
        )

    def bearer_token(self) -> str:
        return _read_secret_file(self.token_path)


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    manifests_repository: str | None = None
    detect_deployment: bool = False
    persistence: PersistenceSettings | None = None
    idcat: IdcatSettings | None = None
    roles: list[RoleConfig] = field(default_factory=list)

    @classmethod
    def from_toml(cls, path: str | Path) -> "Settings":
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(
                f"Invalid TOML in {path}: {exc}. "
                "Python tomllib parses TOML 1.0.0, where inline tables must be "
                "written on one line; for multiline idmouse settings, use a "
                "[persistence.idmouse] table."
            ) from exc
        persistence = data.get("persistence")
        if persistence is not None and not isinstance(persistence, dict):
            raise ValueError("persistence must be a table")
        idcat = data.get("idcat")
        if idcat is not None and not isinstance(idcat, dict):
            raise ValueError("idcat must be a table")
        raw_roles = data.get("role", [])
        if not isinstance(raw_roles, list):
            raise ValueError("role must be an array of tables")
        roles: list[RoleConfig] = []
        seen: set[str] = set()
        for entry in raw_roles:
            if not isinstance(entry, dict):
                raise ValueError("each role entry must be a table")
            role = RoleConfig.from_mapping(entry)
            if role.name in seen:
                raise ValueError(f"duplicate role '{role.name}'")
            seen.add(role.name)
            roles.append(role)
        return cls(
            host=data.get("host", cls.host),
            port=data.get("port", cls.port),
            log_level=_log_level_or_default(data, "log-level", cls.log_level),
            manifests_repository=_optional_string(data, "manifests-repository"),
            detect_deployment=_bool_or_default(
                data,
                "detect-deployment",
                cls.detect_deployment,
            ),
            persistence=(
                PersistenceSettings.from_mapping(persistence) if persistence else None
            ),
            idcat=IdcatSettings.from_mapping(idcat) if idcat else None,
            roles=roles,
        )


def _string_or_default(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"persistence.{key} must be a non-empty string")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _bool_or_default(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _log_level_or_default(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    normalized = value.upper()
    if normalized not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError(
            f"{key} must be one of DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    return normalized


def _optional_persistence_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"persistence.{key} must be a non-empty string")
    return value
