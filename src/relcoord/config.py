# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from relcoord.auth import (
    KUBERNETES_CA_CERT_PATH,
    KUBERNETES_SERVICE_HOST,
    RoleConfig,
)

logger = logging.getLogger(__name__)

TemplateValue = str | int | float | bool
ConnectionType = Literal["eks", "local"]


def _read_secret_file(path: Path) -> str:
    return path.read_text().rstrip("\r\n")


@dataclass(frozen=True)
class IdmouseSettings:
    url: str
    token_path: Path

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> IdmouseSettings:
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
    def from_mapping(cls, data: dict[str, Any]) -> PersistenceSettings:
        backend_value = _string_or_default(data, "backend", cls.backend)
        if backend_value not in ("in-memory", "surrealdb", "dynamodb"):
            raise ValueError(
                "persistence.backend must be one of "
                "'in-memory', 'surrealdb', or 'dynamodb'"
            )
        backend = backend_value

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
            raise TypeError("persistence.idmouse must be a table")

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
    def from_mapping(cls, data: dict[str, Any]) -> IdcatSettings:
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
class OutputSettings:
    """One manifests destination, and what manifest-builder generates into it.

    ``vars`` and ``target`` both say which of a config directory's manifests
    this output wants, for the two config directory layouts manifest-builder
    supports: a config directory that declares config blocks directly is
    rendered with ``vars``, and one that declares targets picks a ``target``.
    Which of the two applies is a property of the config commit being processed,
    not of this output, so an output serving both kinds of repository carries
    both.
    """

    name: str
    repository: str
    directory: Path
    vars: dict[str, TemplateValue] = field(default_factory=dict)
    target: str | None = None
    connection_type: ConnectionType | None = None
    api_endpoint: str | None = None
    ca_path: Path | None = None
    region: str | None = None
    eks_cluster_name: str | None = None

    @property
    def eks_name(self) -> str:
        """The EKS name used to bind an authentication token."""
        return self.name if self.eks_cluster_name is None else self.eks_cluster_name

    @property
    def target_name(self) -> str:
        """The target this output generates from a targets config directory.

        Targets are named in the config repository rather than here, so an
        output that does not say otherwise generates the target sharing its
        name.
        """
        return self.name if self.target is None else self.target

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> OutputSettings:
        name = _required_output_string(data, "name")
        repository = _required_output_string(data, "repository")
        directory = _required_output_directory(data)
        if "cluster" in data:
            raise ValueError(
                "output.cluster is not supported; put the connection settings "
                "directly in the output"
            )
        raw_vars = data.get("vars", {})
        if not isinstance(raw_vars, dict):
            raise TypeError("output.vars must be a table")
        connection_type = _optional_output_connection_type(data)
        api_endpoint: str | None = None
        ca_path: str | None = None
        if connection_type == "eks":
            api_endpoint = _optional_output_string(data, "api-endpoint")
            ca_path = _optional_output_string(data, "ca-path")
            api_endpoint = api_endpoint or _required_output_string(data, "api-endpoint")
            ca_path = ca_path or _required_output_string(data, "ca-path")
        elif connection_type == "local":
            api_endpoint = _optional_output_string(data, "api-endpoint")
            ca_path = _optional_output_string(data, "ca-path")
            api_endpoint = api_endpoint or KUBERNETES_SERVICE_HOST
            ca_path = ca_path or str(KUBERNETES_CA_CERT_PATH)
        return cls(
            name=name,
            repository=repository,
            directory=directory,
            vars=_output_vars(raw_vars),
            target=_optional_output_string(data, "target"),
            connection_type=connection_type,
            api_endpoint=api_endpoint,
            ca_path=Path(ca_path) if ca_path is not None else None,
            region=_optional_output_string(data, "region"),
            eks_cluster_name=_optional_output_string(data, "eks-cluster-name"),
        )


@dataclass(frozen=True)
class RolloutStage:
    """One step of a rollout: the outputs deployed before the next step starts."""

    outputs: tuple[str, ...]


@dataclass(frozen=True)
class RolloutSettings:
    """An ordered pipeline of stages over the outputs it names.

    A change deploys the outputs of the first stage, waits until each of those
    deployments has been observed in its cluster, and only then moves on to the
    next stage, so a deployment that fails to materialise in one stage stops the
    stages after it.

    Which outputs a change affects is not configured here: an output whose
    manifests the config commit leaves alone generates no changes, so it is
    skipped and its stage has nothing to wait for. A change to a section only
    the last stage's outputs are built from therefore reaches them immediately,
    while a change to a section every output shares walks the whole pipeline.
    """

    name: str
    stages: tuple[RolloutStage, ...]

    @property
    def output_names(self) -> tuple[str, ...]:
        return tuple(name for stage in self.stages for name in stage.outputs)


@dataclass(frozen=True)
class Settings:
    listen: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    manifests_repository: str | None = None
    plugins_repository: str | None = None
    outputs: list[OutputSettings] = field(default_factory=list)
    rollouts: list[RolloutSettings] = field(default_factory=list)
    diff_output: str | None = None
    detect_deployment: bool = False
    persistence: PersistenceSettings | None = None
    idcat: IdcatSettings | None = None
    roles: list[RoleConfig] = field(default_factory=list)

    @classmethod
    def from_toml(cls, path: str | Path) -> Settings:
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
            raise TypeError("role must be an array of tables")
        raw_outputs = data.get("output", [])
        if not isinstance(raw_outputs, list):
            raise TypeError("output must be an array of tables")
        outputs = _outputs_from_entries(raw_outputs)
        raw_rollouts = data.get("rollout", [])
        if not isinstance(raw_rollouts, list):
            raise TypeError("rollout must be an array of tables")
        rollouts = _rollouts_from_entries(raw_rollouts)
        _check_rollout_outputs(rollouts, outputs)
        if "cluster" in data:
            raise ValueError(
                "[[cluster]] entries are not supported; put connection settings "
                "directly in each [[output]]"
            )
        manifests_repository = _optional_string(data, "manifests-repository")
        if manifests_repository is not None and outputs:
            raise ValueError(
                "configure either manifests-repository or [[output]], not both"
            )
        diff_output = _diff_output(data, outputs)
        detect_deployment = _bool_or_default(
            data, "detect-deployment", cls.detect_deployment
        )
        _check_deployment_outputs(outputs, detect_deployment)
        _check_rollout_detection(rollouts, detect_deployment)
        roles: list[RoleConfig] = []
        seen: set[str] = set()
        for entry in raw_roles:
            if not isinstance(entry, dict):
                raise TypeError("each role entry must be a table")
            role = RoleConfig.from_mapping(entry)
            if role.name in seen:
                raise ValueError(f"duplicate role '{role.name}'")
            seen.add(role.name)
            roles.append(role)
        if "host" in data:
            logger.warning(
                "The 'host' config option is deprecated; use 'listen' instead"
            )
        listen = data.get("listen", data.get("host", cls.listen))
        return cls(
            listen=listen,
            port=data.get("port", cls.port),
            log_level=_log_level_or_default(data, "log-level", cls.log_level),
            manifests_repository=manifests_repository,
            plugins_repository=_optional_string(data, "plugins-repository"),
            outputs=outputs,
            rollouts=rollouts,
            diff_output=diff_output,
            detect_deployment=detect_deployment,
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
        raise TypeError(f"{key} must be a boolean")
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


def _diff_output(data: dict[str, Any], outputs: list[OutputSettings]) -> str | None:
    """Read the name of the output that /v1/diffcomment reports on.

    A diff covering several clusters is not what a reviewer wants to read, so a
    deployment with more than one output picks the one worth commenting on.
    """
    name = _optional_string(data, "diff-output")
    if name is None:
        return None
    if not outputs:
        raise ValueError("diff-output requires [[output]] entries")
    configured = {output.name for output in outputs}
    if name not in configured:
        raise ValueError(
            f"diff-output '{name}' does not name a configured output; "
            f"expected one of {', '.join(sorted(configured))}"
        )
    return name


def _outputs_from_entries(entries: list[Any]) -> list[OutputSettings]:
    outputs: list[OutputSettings] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("each output entry must be a table")
        output = OutputSettings.from_mapping(entry)
        if output.name in seen:
            raise ValueError(f"duplicate output '{output.name}'")
        seen.add(output.name)
        outputs.append(output)
    return outputs


def _rollouts_from_entries(entries: list[Any]) -> list[RolloutSettings]:
    rollouts: list[RolloutSettings] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("each rollout entry must be a table")
        rollout = _rollout_from_mapping(entry)
        if rollout.name in seen:
            raise ValueError(f"duplicate rollout '{rollout.name}'")
        seen.add(rollout.name)
        rollouts.append(rollout)
    return rollouts


def _rollout_from_mapping(data: dict[str, Any]) -> RolloutSettings:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("rollout.name must be a non-empty string")
    raw_stages = data.get("stage")
    if raw_stages is None:
        raise ValueError(
            f"rollout '{name}' must declare at least one [[rollout.stage]]"
        )
    if not isinstance(raw_stages, list):
        raise TypeError("rollout.stage must be an array of tables")
    if not raw_stages:
        raise ValueError(
            f"rollout '{name}' must declare at least one [[rollout.stage]]"
        )
    stages: list[RolloutStage] = []
    for index, entry in enumerate(raw_stages, start=1):
        if not isinstance(entry, dict):
            raise TypeError("each rollout.stage entry must be a table")
        stages.append(RolloutStage(outputs=_stage_outputs(entry, name, index)))
    return RolloutSettings(name=name, stages=tuple(stages))


def _stage_outputs(data: dict[str, Any], rollout: str, index: int) -> tuple[str, ...]:
    value = data.get("outputs")
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"rollout '{rollout}' stage {index}: outputs must be a non-empty "
            "array of output names"
        )
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"rollout '{rollout}' stage {index}: outputs must be a non-empty "
                "array of output names"
            )
        names.append(name)
    return tuple(names)


def _check_rollout_outputs(
    rollouts: list[RolloutSettings], outputs: list[OutputSettings]
) -> None:
    """Check that the rollouts and the outputs describe the same deployment.

    Each output is deployed by one rollout stage and no other, so that no two
    stages push to a cluster at once, and every output belongs to a stage, so
    that adding a rollout cannot leave an output deploying outside it.
    """
    if not rollouts:
        return
    if not outputs:
        raise ValueError(
            "[[rollout]] requires [[output]] entries; manifests-repository does "
            "not name the outputs a rollout stages"
        )
    configured = {output.name for output in outputs}
    staged: dict[str, str] = {}
    for rollout in rollouts:
        for name in rollout.output_names:
            if name not in configured:
                raise ValueError(
                    f"rollout '{rollout.name}' names output '{name}', which is not "
                    f"configured; expected one of {', '.join(sorted(configured))}"
                )
            if name in staged:
                where = (
                    f"stage of rollout '{rollout.name}'"
                    if staged[name] == rollout.name
                    else f"rollout stage; also in rollout '{staged[name]}'"
                )
                raise ValueError(f"output '{name}' appears in more than one {where}")
            staged[name] = rollout.name
    unstaged = sorted(configured - set(staged))
    if unstaged:
        raise ValueError(
            "every output must appear in a rollout stage once rollouts are "
            f"configured; {', '.join(unstaged)} does not"
        )


def _check_rollout_detection(
    rollouts: list[RolloutSettings], detect_deployment: bool
) -> None:
    if rollouts and not detect_deployment:
        raise ValueError(
            "[[rollout]] requires detect-deployment = true; a stage waits for the "
            "deployment it pushed to be observed before the next stage starts"
        )


def _check_deployment_outputs(
    outputs: list[OutputSettings], detect_deployment: bool
) -> None:
    if not detect_deployment:
        return
    if not outputs:
        raise ValueError(
            "detect-deployment requires [[output]] entries with connection settings; "
            "manifests-repository does not say which cluster its manifests are "
            "deployed to"
        )
    missing = [output.name for output in outputs if output.connection_type is None]
    if missing:
        raise ValueError(
            "detect-deployment requires every output to set connection-type; "
            f"{', '.join(sorted(missing))} does not"
        )


def _optional_output_connection_type(
    data: dict[str, Any],
) -> ConnectionType | None:
    value = data.get("connection-type")
    if value is None:
        connection_keys = {"api-endpoint", "ca-path", "region", "eks-cluster-name"}
        if connection_keys.intersection(data):
            raise ValueError(
                "output.connection-type must be set when configuring a cluster "
                "connection"
            )
        return None
    if value not in ("eks", "local"):
        raise ValueError("output.connection-type must be 'eks' or 'local'")
    return value


def _required_output_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"output.{key} must be a non-empty string")
    return value


def _optional_output_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"output.{key} must be a non-empty string")
    return value


def _required_output_directory(data: dict[str, Any]) -> Path:
    value = data.get("directory")
    if value is None:
        directory = Path(".")
    elif not isinstance(value, str) or not value.strip():
        raise ValueError("output.directory must be a non-empty string")
    else:
        directory = Path(value)
    if directory.is_absolute() or ".." in directory.parts:
        raise ValueError("output.directory must be a relative path without '..'")
    return directory


def _output_vars(data: dict[str, Any]) -> dict[str, TemplateValue]:
    vars: dict[str, TemplateValue] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("output.vars keys must be non-empty strings")
        if not isinstance(value, str | int | float | bool):
            raise TypeError(f"output.vars.{key} must be a string, number, or boolean")
        vars[key] = value
    return vars
