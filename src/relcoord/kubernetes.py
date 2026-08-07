# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Observing a change materialise in a Kubernetes cluster.

manifest-builder reports which objects a change touched and stamps each of them
with a deploy-id annotation. This module connects to the cluster those manifests
are deployed to and waits, using watches rather than polling, until every
changed object carries that deploy-id and every removed object is gone.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
import httpx

from relcoord.auth import KUBERNETES_TOKEN_PATH
from relcoord.config import OutputSettings
from relcoord.eks import EksTokenAuth

logger = logging.getLogger(__name__)

DEPLOY_ID_ANNOTATION = "noa.re/deploy-id"
DEFAULT_TIMEOUT_SECONDS = 300.0
# How long a single watch is allowed to stay open. The API server closes the
# stream when it expires, and a fresh list re-establishes a resourceVersion to
# watch from. Long enough that the usual wait needs only one stream.
WATCH_TIMEOUT_SECONDS = 300.0
# How long to wait before opening a watch that the API server just closed
# straight away, so a cluster refusing watches does not spin.
WATCH_RETRY_SECONDS = 1.0
CONNECT_TIMEOUT_SECONDS = 10.0


class KubernetesObjectRef(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def namespace(self) -> str | None: ...
    @property
    def name(self) -> str: ...


class DeploymentDetectionError(Exception):
    pass


@dataclass(frozen=True)
class KubernetesResource:
    """A REST resource the API server serves, as reported by discovery."""

    path_prefix: str
    name: str
    namespaced: bool

    def collection_path(self, namespace: str | None) -> str:
        if namespace is None:
            return f"{self.path_prefix}/{self.name}"
        return f"{self.path_prefix}/namespaces/{namespace}/{self.name}"


def cluster_client(output: OutputSettings) -> httpx.Client:
    """Return a client for the cluster represented by ``output``."""
    if output.connection_type is None:
        raise DeploymentDetectionError(f"output {output.name} has no connection-type")
    if output.api_endpoint is None:
        raise DeploymentDetectionError(f"output {output.name} has no API endpoint")
    if output.ca_path is None:
        raise DeploymentDetectionError(f"output {output.name} has no CA certificate")
    if not output.ca_path.exists():
        raise DeploymentDetectionError(
            f"CA certificate {output.ca_path} for cluster {output.name} does not exist"
        )
    try:
        ssl_context = ssl.create_default_context(cafile=str(output.ca_path))
    except ssl.SSLError as exc:
        raise DeploymentDetectionError(
            f"CA certificate {output.ca_path} for cluster {output.name} is not "
            f"a readable PEM certificate: {exc}"
        ) from exc
    authentication: dict[str, Any]
    if output.connection_type == "local":
        authentication = {
            "headers": {"Authorization": f"Bearer {_service_account_token(output)}"}
        }
    else:
        session = boto3.Session(region_name=output.region)
        authentication = {"auth": EksTokenAuth(session, output.eks_name)}
    return httpx.Client(
        base_url=output.api_endpoint.rstrip("/"),
        verify=ssl_context,
        # Reads have to outlast a watch that is idle but healthy, which is the
        # normal state of a watch waiting for a deployment to roll out.
        timeout=httpx.Timeout(
            WATCH_TIMEOUT_SECONDS + 60.0, connect=CONNECT_TIMEOUT_SECONDS
        ),
        **authentication,
    )


def _service_account_token(output: OutputSettings) -> str:
    try:
        token = KUBERNETES_TOKEN_PATH.read_text().strip()
    except OSError as exc:
        raise DeploymentDetectionError(
            f"service account token {KUBERNETES_TOKEN_PATH} for local cluster "
            f"{output.name} could not be read: {exc}"
        ) from exc
    if not token:
        raise DeploymentDetectionError(
            f"service account token {KUBERNETES_TOKEN_PATH} for local cluster "
            f"{output.name} is empty"
        )
    return token


class KubernetesDeploymentDetector:
    """Waits for a change's objects to materialise in one cluster.

    Objects are waited for one at a time. A list narrowed to a single name
    settles whether the object is already in the state the change asked for, and
    when it is not, a watch from that list's resourceVersion reports the moment
    it gets there. Waiting in sequence is enough because a change has only
    materialised once every one of its objects has, and it keeps one stream open
    at a time rather than one per object.
    """

    def __init__(
        self,
        *,
        client: httpx.Client,
        cluster_name: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        watch_timeout_seconds: float = WATCH_TIMEOUT_SECONDS,
        retry_delay_seconds: float = WATCH_RETRY_SECONDS,
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self._cluster_name = cluster_name
        self._timeout_seconds = timeout_seconds
        self._watch_timeout_seconds = watch_timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._owns_client = owns_client
        self._resources_by_kind: dict[str, list[KubernetesResource]] | None = None

    @classmethod
    def for_output(
        cls,
        output: OutputSettings,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> KubernetesDeploymentDetector:
        return cls(
            client=cluster_client(output),
            cluster_name=output.name,
            timeout_seconds=timeout_seconds,
            owns_client=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def wait_for_success(
        self,
        *,
        deploy_id: str,
        created_or_modified: set[KubernetesObjectRef],
        removed: set[KubernetesObjectRef],
    ) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        waited_for = 0
        for ref in sorted(created_or_modified, key=_object_ref_sort_key):
            self._wait_for_object(
                ref,
                deploy_id=deploy_id,
                satisfied=lambda obj: _deploy_id_of(obj) == deploy_id,
                describe=lambda obj: (
                    "it has not appeared"
                    if obj is None
                    else f"it has deploy-id {_deploy_id_of(obj) or '<missing>'!r}"
                ),
                goal=f"deploy-id {deploy_id}",
                deadline=deadline,
            )
            waited_for += 1
        for ref in sorted(removed, key=_object_ref_sort_key):
            self._wait_for_object(
                ref,
                deploy_id=deploy_id,
                satisfied=lambda obj: obj is None,
                describe=lambda obj: "it still exists",
                goal="removal",
                deadline=deadline,
            )
            waited_for += 1
        logger.info(
            "change with deploy-id %s has materialised in cluster %s: "
            "%d object(s) observed",
            deploy_id,
            self._cluster_name or "<unnamed>",
            waited_for,
        )

    def _wait_for_object(
        self,
        ref: KubernetesObjectRef,
        *,
        deploy_id: str,
        satisfied: Callable[[dict[str, Any] | None], bool],
        describe: Callable[[dict[str, Any] | None], str],
        goal: str,
        deadline: float,
    ) -> None:
        resource = self._resource_for(ref)
        while True:
            obj = self._list_object(resource, ref)
            if satisfied(obj):
                self._log_observed(ref, goal)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentDetectionError(
                    f"timed out after {self._timeout_seconds:g}s waiting for "
                    f"{_format_ref(ref)} to reach {goal} in cluster "
                    f"{self._cluster_name or '<unnamed>'}: {describe(obj)}"
                )
            logger.info(
                "watching %s in cluster %s for %s (deploy-id %s): %s",
                _format_ref(ref),
                self._cluster_name or "<unnamed>",
                goal,
                deploy_id,
                describe(obj),
            )
            started = time.monotonic()
            for event_type, event_object in self._watch_object(
                resource, ref, min(self._watch_timeout_seconds, remaining)
            ):
                observed = None if event_type == "DELETED" else event_object
                if satisfied(observed):
                    self._log_observed(ref, goal)
                    return
            # The watch ended without the object reaching the state the change
            # asked for, which is what an expired watch looks like, and the loop
            # opens another one. A watch that ends immediately instead means the
            # API server is dropping them, so this waits a little rather than
            # spinning through the whole timeout in a tight loop.
            if time.monotonic() - started < self._retry_delay_seconds:
                time.sleep(self._retry_delay_seconds)

    def _log_observed(self, ref: KubernetesObjectRef, goal: str) -> None:
        logger.info(
            "observed %s in cluster %s reaching %s",
            _format_ref(ref),
            self._cluster_name or "<unnamed>",
            goal,
        )

    def _list_object(
        self, resource: KubernetesResource, ref: KubernetesObjectRef
    ) -> dict[str, Any] | None:
        """Return the named object, or None when the cluster does not have it."""
        listing = self._get(
            resource.collection_path(ref.namespace),
            params={"fieldSelector": f"metadata.name={ref.name}"},
        )
        items = listing.get("items")
        if not isinstance(items, list):
            raise DeploymentDetectionError(
                f"listing {_format_ref(ref)} did not return an item list"
            )
        for item in items:
            if isinstance(item, dict):
                return item
        return None

    def _watch_object(
        self,
        resource: KubernetesResource,
        ref: KubernetesObjectRef,
        timeout_seconds: float,
    ) -> Iterator[tuple[str, dict[str, Any] | None]]:
        """Yield (type, object) watch events for one object until the stream ends.

        The stream is opened without a resourceVersion, which asks the API server
        for the object's current state first and then updates: relcoord cares
        about the state an object is in rather than the sequence of changes it
        went through, so starting from the present loses nothing and cannot miss
        an event between the list and the watch.
        """
        path = resource.collection_path(ref.namespace)
        params = {
            "watch": "1",
            "fieldSelector": f"metadata.name={ref.name}",
            "timeoutSeconds": str(max(1, int(timeout_seconds))),
        }
        try:
            with self._client.stream("GET", path, params=params) as response:
                if response.status_code >= 400:
                    if response.status_code == 410:
                        # Too old to resume from; the caller lists again.
                        return
                    # raise_for_status() reports the body, which a streamed
                    # response only has once it has been read.
                    response.read()
                    response.raise_for_status()
                for line in response.iter_lines():
                    event = _watch_event(line)
                    if event is None:
                        continue
                    event_type, event_object = event
                    if event_type == "ERROR":
                        logger.info(
                            "watch of %s returned an error event; listing again",
                            _format_ref(ref),
                        )
                        return
                    if event_type == "BOOKMARK":
                        continue
                    yield event_type, event_object
        except httpx.HTTPStatusError as exc:
            raise DeploymentDetectionError(
                f"watch of {_format_ref(ref)} failed with status "
                f"{exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.ReadTimeout:
            # The API server should have closed the stream itself; treat a
            # client-side timeout the same way and list again.
            logger.debug("watch of %s timed out client side", _format_ref(ref))
            return
        except httpx.RequestError as exc:
            raise DeploymentDetectionError(
                f"watch of {_format_ref(ref)} failed: {exc}"
            ) from exc

    def _resource_for(self, ref: KubernetesObjectRef) -> KubernetesResource:
        resources = self._matching_resources(ref)
        if not resources:
            self._resources_by_kind = self._discover_resources()
            resources = self._matching_resources(ref)
        if not resources:
            scope = "namespaced" if ref.namespace is not None else "cluster-scoped"
            raise DeploymentDetectionError(
                f"cluster {self._cluster_name or '<unnamed>'} serves no "
                f"{scope} resource of kind {ref.kind}"
            )
        if len(resources) > 1:
            served = ", ".join(
                f"{resource.path_prefix}/{resource.name}" for resource in resources
            )
            raise DeploymentDetectionError(
                f"kind {ref.kind} is ambiguous in cluster "
                f"{self._cluster_name or '<unnamed>'}: {served}"
            )
        return resources[0]

    def _matching_resources(self, ref: KubernetesObjectRef) -> list[KubernetesResource]:
        return [
            resource
            for resource in self._resources_by_kind_cached().get(ref.kind, [])
            if resource.namespaced == (ref.namespace is not None)
        ]

    def _resources_by_kind_cached(self) -> dict[str, list[KubernetesResource]]:
        if self._resources_by_kind is None:
            self._resources_by_kind = self._discover_resources()
        return self._resources_by_kind

    def _discover_resources(self) -> dict[str, list[KubernetesResource]]:
        resources: dict[str, list[KubernetesResource]] = {}
        core = self._get("/api/v1")
        _add_resources(resources, "/api/v1", core)

        apis = self._get("/apis")
        for group in apis.get("groups", []):
            group_name = group.get("name")
            for version in _group_versions(group):
                version_name = version.get("version")
                if not isinstance(group_name, str) or not isinstance(version_name, str):
                    continue
                path_prefix = f"/apis/{group_name}/{version_name}"
                _add_resources(resources, path_prefix, self._get(path_prefix))
        return resources

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} failed with status "
                f"{exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} failed: {exc}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} did not return a JSON object"
            )
        return data


def _group_versions(group: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the versions of an API group to discover resources from.

    Only the preferred version, where the group names one: a kind served by
    several versions of the same group is one kind, and discovering all of them
    would make it look ambiguous.
    """
    preferred = group.get("preferredVersion")
    if isinstance(preferred, dict) and preferred.get("version"):
        return [preferred]
    versions = group.get("versions", [])
    return [version for version in versions if isinstance(version, dict)]


def _watch_event(line: str) -> tuple[str, dict[str, Any] | None] | None:
    """Parse one line of a watch stream into an (type, object) pair."""
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except ValueError:
        logger.debug("ignoring unparseable watch event: %s", line[:200])
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    event_object = event.get("object")
    return event_type, event_object if isinstance(event_object, dict) else None


def _deploy_id_of(obj: dict[str, Any] | None) -> str | None:
    if obj is None:
        return None
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return None
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        return None
    value = annotations.get(DEPLOY_ID_ANNOTATION)
    return value if isinstance(value, str) else None


def _add_resources(
    resources: dict[str, list[KubernetesResource]],
    path_prefix: str,
    resource_list: dict[str, Any],
) -> None:
    for resource in resource_list.get("resources", []):
        name = resource.get("name")
        kind = resource.get("kind")
        namespaced = resource.get("namespaced")
        verbs = resource.get("verbs", [])
        if (
            not isinstance(name, str)
            or "/" in name
            or not isinstance(kind, str)
            or not isinstance(namespaced, bool)
            or "watch" not in verbs
        ):
            continue
        resources.setdefault(kind, []).append(
            KubernetesResource(
                path_prefix=path_prefix,
                name=name,
                namespaced=namespaced,
            )
        )


def _format_ref(ref: KubernetesObjectRef) -> str:
    if ref.namespace is None:
        return f"{ref.kind}/{ref.name}"
    return f"{ref.kind}/{ref.namespace}/{ref.name}"


def _object_ref_sort_key(ref: KubernetesObjectRef) -> tuple[str, str, str]:
    return (ref.kind, ref.namespace or "", ref.name)
