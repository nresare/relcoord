# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest

from relcoord.config import OutputSettings
from relcoord.eks import TOKEN_PREFIX, EksTokenAuth
from relcoord.kubernetes import (
    DEPLOY_ID_ANNOTATION,
    DeploymentDetectionError,
    KubernetesDeploymentDetector,
    cluster_client,
)

DEPLOY_ID = "0123456789abcdef"

DISCOVERY = {
    "/api/v1": {
        "resources": [
            {
                "name": "namespaces",
                "kind": "Namespace",
                "namespaced": False,
                "verbs": ["get", "list", "watch"],
            },
            {
                "name": "configmaps",
                "kind": "ConfigMap",
                "namespaced": True,
                "verbs": ["get", "list", "watch"],
            },
        ]
    },
    "/apis": {
        "groups": [
            {
                "name": "apps",
                "preferredVersion": {"version": "v1"},
                "versions": [{"version": "v1"}, {"version": "v1beta1"}],
            }
        ]
    },
    "/apis/apps/v1": {
        "resources": [
            {
                "name": "deployments",
                "kind": "Deployment",
                "namespaced": True,
                "verbs": ["get", "list", "watch"],
            },
            {
                "name": "statefulsets",
                "kind": "StatefulSet",
                "namespaced": True,
                "verbs": ["get", "list", "watch"],
            },
        ]
    },
}


@dataclass(frozen=True)
class Ref:
    kind: str
    namespace: str | None
    name: str
    api_version: str = "v1"


def annotated(name: str, deploy_id: str | None) -> dict[str, object]:
    annotations = {} if deploy_id is None else {DEPLOY_ID_ANNOTATION: deploy_id}
    return {"metadata": {"name": name, "annotations": annotations}}


def deployment(
    name: str,
    deploy_id: str | None,
    *,
    generation: int = 1,
    observed_generation: int | None = None,
    replicas: int = 2,
    updated_replicas: int | None = None,
    status_replicas: int | None = None,
    available_replicas: int | None = None,
    paused: bool = False,
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A Deployment that has finished rolling out, unless told otherwise."""
    annotations = {} if deploy_id is None else {DEPLOY_ID_ANNOTATION: deploy_id}
    return {
        "metadata": {
            "name": name,
            "annotations": annotations,
            "generation": generation,
        },
        "spec": {"replicas": replicas, "paused": paused},
        "status": {
            "observedGeneration": (
                generation if observed_generation is None else observed_generation
            ),
            "replicas": replicas if status_replicas is None else status_replicas,
            "updatedReplicas": (
                replicas if updated_replicas is None else updated_replicas
            ),
            "availableReplicas": (
                replicas if available_replicas is None else available_replicas
            ),
            "conditions": conditions or [],
        },
    }


def statefulset(
    name: str,
    deploy_id: str | None,
    *,
    generation: int = 1,
    observed_generation: int | None = None,
    replicas: int = 2,
    ready_replicas: int | None = None,
    updated_replicas: int | None = None,
    update_strategy: str = "RollingUpdate",
    partition: int | None = None,
    current_revision: str = "db-7f4",
    update_revision: str | None = None,
) -> dict[str, Any]:
    """A StatefulSet that has finished rolling out, unless told otherwise."""
    annotations = {} if deploy_id is None else {DEPLOY_ID_ANNOTATION: deploy_id}
    strategy: dict[str, Any] = {"type": update_strategy}
    if partition is not None:
        strategy["rollingUpdate"] = {"partition": partition}
    return {
        "metadata": {
            "name": name,
            "annotations": annotations,
            "generation": generation,
        },
        "spec": {"replicas": replicas, "updateStrategy": strategy},
        "status": {
            "observedGeneration": (
                generation if observed_generation is None else observed_generation
            ),
            "readyReplicas": replicas if ready_replicas is None else ready_replicas,
            "updatedReplicas": (
                replicas if updated_replicas is None else updated_replicas
            ),
            "currentRevision": current_revision,
            "updateRevision": (
                current_revision if update_revision is None else update_revision
            ),
        },
    }


def listing(*items: dict[str, object]) -> dict[str, object]:
    return {"metadata": {"resourceVersion": "42"}, "items": list(items)}


def watch_stream(*events: tuple[str, dict[str, object] | None]) -> bytes:
    return "".join(
        json.dumps({"type": event_type, "object": obj}) + "\n"
        for event_type, obj in events
    ).encode()


def detector(
    handler,
    *,
    timeout_seconds: float = 5,
    watch_timeout_seconds: float = 5,
) -> KubernetesDeploymentDetector:
    return KubernetesDeploymentDetector(
        client=httpx.Client(
            base_url="https://kubernetes.example.test",
            transport=httpx.MockTransport(handler),
        ),
        cluster_name="example-dev",
        timeout_seconds=timeout_seconds,
        watch_timeout_seconds=watch_timeout_seconds,
        retry_delay_seconds=0,
    )


def test_detector_returns_when_the_objects_are_already_in_place() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path == "/apis/apps/v1/namespaces/default/deployments":
            return httpx.Response(200, json=listing(deployment("api", DEPLOY_ID)))
        if path == "/api/v1/namespaces":
            return httpx.Response(200, json=listing(annotated("production", DEPLOY_ID)))
        if path == "/api/v1/namespaces/default/configmaps":
            return httpx.Response(200, json=listing())
        return httpx.Response(500, json={"unexpected": path})

    detector(handler).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={
            Ref("Deployment", "default", "api", "apps/v1"),
            Ref("Namespace", None, "production"),
        },
        removed={Ref("ConfigMap", "default", "old-api")},
    )

    listed = [url for url in requests if "fieldSelector" in url.params]
    assert [url.path for url in listed] == [
        "/apis/apps/v1/namespaces/default/deployments",
        "/api/v1/namespaces",
        "/api/v1/namespaces/default/configmaps",
    ]
    assert listed[0].params["fieldSelector"] == "metadata.name=api"
    # Nothing had to be waited for, so no watch was opened.
    assert all("watch" not in url.params for url in requests)


def test_detector_watches_until_the_deploy_id_annotation_appears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal watches
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != "/apis/apps/v1/namespaces/default/deployments":
            return httpx.Response(500, json={"unexpected": path})
        if "watch" not in request.url.params:
            return httpx.Response(200, json=listing(deployment("api", "stale")))
        watches += 1
        return httpx.Response(
            200,
            content=watch_stream(
                ("MODIFIED", deployment("api", "stale")),
                ("MODIFIED", deployment("api", DEPLOY_ID)),
            ),
        )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
            removed=set(),
        )

    assert watches == 1
    assert "has materialised in cluster example-dev" in caplog.text


def apps_handler(resource: str, listed: dict[str, Any], *watched: dict[str, Any]):
    """Serve discovery, one listing of ``listed``, then a watch of ``watched``."""
    collection = f"/apis/apps/v1/namespaces/default/{resource}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != collection:
            return httpx.Response(500, json={"unexpected": path})
        if "watch" not in request.url.params:
            return httpx.Response(200, json=listing(listed))
        return httpx.Response(
            200,
            content=watch_stream(*(("MODIFIED", obj) for obj in watched)),
        )

    return handler


def deployments_handler(listed: dict[str, Any], *watched: dict[str, Any]):
    return apps_handler("deployments", listed, *watched)


def statefulsets_handler(listed: dict[str, Any], *watched: dict[str, Any]):
    return apps_handler("statefulsets", listed, *watched)


def wait_for_deployment(handler, *, timeout_seconds: float = 5) -> None:
    detector(handler, timeout_seconds=timeout_seconds).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
        removed=set(),
    )


def wait_for_statefulset(handler, *, timeout_seconds: float = 5) -> None:
    detector(handler, timeout_seconds=timeout_seconds).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={Ref("StatefulSet", "default", "db", "apps/v1")},
        removed=set(),
    )


def test_detector_waits_for_a_new_replica_set_to_replace_the_old_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = deployments_handler(
        # The new ReplicaSet is one pod in, and the old one still has both.
        deployment(
            "api",
            DEPLOY_ID,
            updated_replicas=1,
            status_replicas=3,
            available_replicas=2,
        ),
        # Scaled up, but the old ReplicaSet has a pod left.
        deployment("api", DEPLOY_ID, status_replicas=3, available_replicas=2),
        # Nothing but the new ReplicaSet, whose second pod is not ready yet.
        deployment("api", DEPLOY_ID, available_replicas=1),
        deployment("api", DEPLOY_ID),
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_deployment(handler)

    assert "1 of 2 replicas have been updated" in caplog.text
    assert "has materialised in cluster example-dev" in caplog.text


def test_detector_waits_for_the_controller_to_observe_a_change_it_need_not_roll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A change that leaves the pod template alone: every replica is already
    # updated and available, so the wait is only for the controller to catch up.
    handler = deployments_handler(
        deployment("api", DEPLOY_ID, generation=4, observed_generation=3),
        deployment("api", DEPLOY_ID, generation=4),
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_deployment(handler)

    assert "has observed generation 3, not 4" in caplog.text
    assert "has materialised in cluster example-dev" in caplog.text


def test_detector_logs_what_it_observed_of_a_rollout_it_never_waited_for(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A Deployment already in the state the change asked for is waited for by
    # doing nothing, so this line is the only evidence the rollout was checked.
    handler = deployments_handler(
        deployment("api", DEPLOY_ID, generation=7, replicas=3)
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_deployment(handler)

    assert (
        "reaching deploy-id 0123456789abcdef with a complete rollout after 0.0s: "
        "generation 7 observed, 3 of 3 replicas updated and available"
    ) in caplog.text


def test_detector_times_out_reporting_where_a_rollout_got_to() -> None:
    handler = deployments_handler(
        deployment("api", DEPLOY_ID, replicas=3, updated_replicas=1)
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_deployment(handler, timeout_seconds=0)

    message = str(excinfo.value)
    assert "complete rollout" in message
    assert "1 of 3 replicas have been updated" in message


def test_detector_reports_a_rollout_that_exceeded_its_progress_deadline() -> None:
    handler = deployments_handler(
        deployment(
            "api",
            DEPLOY_ID,
            updated_replicas=1,
            conditions=[
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": 'ReplicaSet "api-7d9" has timed out progressing.',
                }
            ],
        )
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_deployment(handler)

    message = str(excinfo.value)
    assert "exceeded the progress deadline" in message
    assert "has timed out progressing" in message
    # A deployment the cluster has given up on is not worth waiting out.
    assert "timed out after" not in message


def test_detector_reports_a_paused_deployment_rather_than_waiting_for_it() -> None:
    handler = deployments_handler(
        deployment("api", DEPLOY_ID, updated_replicas=0, paused=True)
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_deployment(handler)

    message = str(excinfo.value)
    assert "it is paused" in message
    assert "timed out after" not in message


def test_detector_reports_why_a_replica_set_cannot_create_pods() -> None:
    handler = deployments_handler(
        deployment(
            "api",
            DEPLOY_ID,
            updated_replicas=0,
            conditions=[
                {
                    "type": "ReplicaFailure",
                    "status": "True",
                    "reason": "FailedCreate",
                    "message": "exceeded quota: pods=10, used: pods=10",
                }
            ],
        )
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_deployment(handler, timeout_seconds=0)

    message = str(excinfo.value)
    assert "0 of 2 replicas have been updated" in message
    # Transient on its own, so it explains the wait rather than ending it.
    assert "exceeded quota" in message
    assert "timed out after" in message


def test_detector_waits_for_a_statefulset_to_replace_its_pods(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = statefulsets_handler(
        # The first pod has been taken down for its replacement.
        statefulset(
            "db",
            DEPLOY_ID,
            ready_replicas=1,
            updated_replicas=0,
            update_revision="db-9c1",
        ),
        # Back up at the new revision, and the second pod not yet updated.
        statefulset("db", DEPLOY_ID, updated_replicas=1, update_revision="db-9c1"),
        # Both updated, and the controller has yet to make the revision current.
        statefulset("db", DEPLOY_ID, update_revision="db-9c1"),
        statefulset("db", DEPLOY_ID, current_revision="db-9c1"),
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert "1 of 2 replicas are ready" in caplog.text
    assert "has materialised in cluster example-dev" in caplog.text


def test_detector_waits_for_a_statefulset_change_that_rolls_no_pods(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A change that leaves the pod template alone: every replica is already
    # updated and ready, so the wait is only for the controller to catch up.
    handler = statefulsets_handler(
        statefulset("db", DEPLOY_ID, generation=4, observed_generation=3),
        statefulset("db", DEPLOY_ID, generation=4),
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert "statefulset controller has observed generation 3, not 4" in caplog.text
    assert "has materialised in cluster example-dev" in caplog.text


def test_detector_logs_what_it_observed_of_a_statefulset_rollout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = statefulsets_handler(
        statefulset("db", DEPLOY_ID, generation=7, replicas=3)
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert (
        "reaching deploy-id 0123456789abcdef with a complete rollout after 0.0s: "
        "generation 7 observed, 3 of 3 replicas updated and ready"
    ) in caplog.text


def test_detector_times_out_reporting_where_a_statefulset_rollout_got_to() -> None:
    handler = statefulsets_handler(
        statefulset(
            "db", DEPLOY_ID, replicas=3, updated_replicas=1, update_revision="db-9c1"
        )
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_statefulset(handler, timeout_seconds=0)

    message = str(excinfo.value)
    assert "StatefulSet/default/db" in message
    assert "complete rollout" in message
    assert "1 of 3 replicas have been updated" in message


def test_detector_waits_only_for_the_replicas_a_partition_covers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # partition = 2 of 3 replicas holds ordinals 0 and 1 back, so the rollout is
    # over once the one ordinal above the partition carries the new revision.
    handler = statefulsets_handler(
        statefulset(
            "db",
            DEPLOY_ID,
            replicas=3,
            updated_replicas=0,
            partition=2,
            update_revision="db-9c1",
        ),
        statefulset(
            "db",
            DEPLOY_ID,
            replicas=3,
            updated_replicas=1,
            partition=2,
            update_revision="db-9c1",
        ),
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert "0 of 1 replicas above partition 2 have been updated" in caplog.text
    assert (
        "generation 1 observed, 1 of 1 replicas above partition 2 updated and "
        "3 of 3 ready"
    ) in caplog.text


def test_detector_waits_for_a_statefulset_a_partition_holds_entirely_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A partition at or above the replica count updates no pod at all, so the
    # readiness of the pods already there is the whole of the rollout.
    handler = statefulsets_handler(
        statefulset(
            "db",
            DEPLOY_ID,
            replicas=2,
            updated_replicas=0,
            partition=2,
            update_revision="db-9c1",
        )
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert "0 of 0 replicas above partition 2 updated" in caplog.text


def test_detector_does_not_wait_for_pods_an_on_delete_statefulset_keeps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing replaces the pods of an OnDelete StatefulSet until someone deletes
    # them, so the write landing is as far as detection can wait.
    handler = statefulsets_handler(
        statefulset(
            "db",
            DEPLOY_ID,
            update_strategy="OnDelete",
            updated_replicas=0,
            update_revision="db-9c1",
        )
    )

    with caplog.at_level(logging.INFO, logger="relcoord.kubernetes"):
        wait_for_statefulset(handler)

    assert "it is updated OnDelete" in caplog.text
    assert "has materialised in cluster example-dev" in caplog.text


def test_detector_waits_for_an_on_delete_statefulset_to_be_observed() -> None:
    # The write still has to be one the controller has acted on.
    handler = statefulsets_handler(
        statefulset(
            "db",
            DEPLOY_ID,
            update_strategy="OnDelete",
            generation=4,
            observed_generation=3,
        )
    )

    with pytest.raises(DeploymentDetectionError) as excinfo:
        wait_for_statefulset(handler, timeout_seconds=0)

    assert "has observed generation 3, not 4" in str(excinfo.value)


def test_detector_watches_until_a_removed_object_is_deleted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != "/api/v1/namespaces/default/configmaps":
            return httpx.Response(500, json={"unexpected": path})
        if "watch" not in request.url.params:
            return httpx.Response(200, json=listing(annotated("old-api", DEPLOY_ID)))
        return httpx.Response(
            200, content=watch_stream(("DELETED", annotated("old-api", DEPLOY_ID)))
        )

    detector(handler).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified=set(),
        removed={Ref("ConfigMap", "default", "old-api")},
    )


def test_detector_lists_again_when_a_watch_ends_without_the_change() -> None:
    lists = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lists
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != "/apis/apps/v1/namespaces/default/deployments":
            return httpx.Response(500, json={"unexpected": path})
        if "watch" in request.url.params:
            # An expired watch closes without having reported the change.
            return httpx.Response(200, content=b"")
        lists += 1
        deploy_id = DEPLOY_ID if lists > 2 else "stale"
        return httpx.Response(200, json=listing(deployment("api", deploy_id)))

    detector(handler).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
        removed=set(),
    )

    assert lists == 3


def test_detector_times_out_reporting_the_observed_annotation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != "/apis/apps/v1/namespaces/default/deployments":
            return httpx.Response(500, json={"unexpected": path})
        return httpx.Response(200, json=listing(deployment("api", "stale")))

    with pytest.raises(DeploymentDetectionError) as excinfo:
        detector(handler, timeout_seconds=0).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
            removed=set(),
        )

    message = str(excinfo.value)
    assert "Deployment/default/api" in message
    assert "'stale'" in message
    assert "cluster example-dev" in message


def test_detector_reports_a_kind_the_cluster_does_not_serve() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        return httpx.Response(500, json={"unexpected": path})

    with pytest.raises(DeploymentDetectionError, match="no namespaced resource"):
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Widget", "default", "api")},
            removed=set(),
        )


def shared_role_discovery(*groups: str) -> dict[str, Any]:
    """DISCOVERY, with a namespaced Role served by each of ``groups`` at v1."""
    discovery: dict[str, Any] = dict(DISCOVERY)
    discovery["/apis"] = {
        "groups": [
            {"name": "apps", "preferredVersion": {"version": "v1"}},
            *(
                {"name": group, "preferredVersion": {"version": "v1"}}
                for group in groups
            ),
        ]
    }
    for group in groups:
        discovery[f"/apis/{group}/v1"] = {
            "resources": [
                {
                    "name": "roles",
                    "kind": "Role",
                    "namespaced": True,
                    "verbs": ["get", "list", "watch"],
                }
            ]
        }
    return discovery


SHARED_ROLE_GROUPS = ("rbac.authorization.k8s.io", "iam.aws.m.upbound.io")


@pytest.mark.parametrize("group", SHARED_ROLE_GROUPS)
def test_detector_resolves_a_shared_kind_through_the_refs_group(group: str) -> None:
    """A kind two groups serve resolves to the group the manifest named."""
    discovery = shared_role_discovery(*SHARED_ROLE_GROUPS)
    listed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in discovery:
            return httpx.Response(200, json=discovery[path])
        if path.startswith("/apis/") and path.endswith("/namespaces/default/roles"):
            listed.append(path)
            return httpx.Response(200, json=listing(annotated("api", DEPLOY_ID)))
        return httpx.Response(500, json={"unexpected": path})

    detector(handler).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={Ref("Role", "default", "api", f"{group}/v1")},
        removed=set(),
    )

    assert listed == [f"/apis/{group}/v1/namespaces/default/roles"]


def test_detector_resolves_a_shared_kind_whose_ref_names_another_version() -> None:
    """Only the group has to match: one kind is one kind across its versions."""
    discovery = shared_role_discovery("iam.aws.m.upbound.io")
    listed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in discovery:
            return httpx.Response(200, json=discovery[path])
        listed.append(path)
        return httpx.Response(200, json=listing(annotated("api", DEPLOY_ID)))

    detector(handler).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={
            Ref("Role", "default", "api", "iam.aws.m.upbound.io/v1beta1")
        },
        removed=set(),
    )

    assert listed == ["/apis/iam.aws.m.upbound.io/v1/namespaces/default/roles"]


def test_detector_reports_a_kind_no_group_the_cluster_serves_defines() -> None:
    discovery = shared_role_discovery("rbac.authorization.k8s.io")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in discovery:
            return httpx.Response(200, json=discovery[path])
        return httpx.Response(500, json={"unexpected": path})

    with pytest.raises(DeploymentDetectionError) as excinfo:
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={
                Ref("Role", "default", "api", "iam.aws.m.upbound.io/v1beta1")
            },
            removed=set(),
        )

    message = str(excinfo.value)
    assert "serves no namespaced resource of kind Role" in message
    assert "in iam.aws.m.upbound.io/v1beta1" in message


def test_detector_reports_a_shared_kind_a_ref_carries_no_api_version_for() -> None:
    discovery = shared_role_discovery(*SHARED_ROLE_GROUPS)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in discovery:
            return httpx.Response(200, json=discovery[path])
        return httpx.Response(500, json={"unexpected": path})

    with pytest.raises(DeploymentDetectionError) as excinfo:
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Role", "default", "api", "")},
            removed=set(),
        )

    message = str(excinfo.value)
    assert "kind Role (the manifest carried no apiVersion) is ambiguous" in message
    for group in SHARED_ROLE_GROUPS:
        assert f"/apis/{group}/v1/roles" in message


def test_detector_reports_a_failing_api_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(DeploymentDetectionError, match="status 403"):
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
            removed=set(),
        )


def test_detector_reports_a_watch_that_the_api_server_rejects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if "watch" in request.url.params:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=listing(deployment("api", "stale")))

    with pytest.raises(DeploymentDetectionError, match="watch of .* status 500"):
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Deployment", "default", "api", "apps/v1")},
            removed=set(),
        )


def test_cluster_client_authenticates_with_a_token_for_the_eks_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Any real PEM bundle will do; the connection is never made.
    ca_path = Path(certifi.where())
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    client = cluster_client(
        OutputSettings(
            name="example-dev",
            repository="https://github.com/acme/manifests",
            directory=Path("example-dev"),
            connection_type="eks",
            api_endpoint="https://kubernetes.example.test/",
            ca_path=ca_path,
            region="eu-west-1",
            eks_cluster_name="example-dev-eks",
        )
    )

    auth = client.auth
    assert isinstance(auth, EksTokenAuth)
    assert auth.token().startswith(TOKEN_PREFIX)
    assert str(client.base_url) == "https://kubernetes.example.test"
    client.close()


def test_cluster_client_authenticates_with_the_local_service_account_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token\n")
    monkeypatch.setattr("relcoord.kubernetes.KUBERNETES_TOKEN_PATH", token_path)

    client = cluster_client(
        OutputSettings(
            name="local",
            repository="https://github.com/acme/manifests",
            directory=Path("local"),
            api_endpoint="https://kubernetes.default.svc/",
            ca_path=Path(certifi.where()),
            connection_type="local",
        )
    )

    assert client.headers["authorization"] == "Bearer service-account-token"
    assert client.auth is None
    assert str(client.base_url) == "https://kubernetes.default.svc"
    client.close()


def test_cluster_client_rejects_a_missing_local_service_account_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_path = tmp_path / "absent-token"
    monkeypatch.setattr("relcoord.kubernetes.KUBERNETES_TOKEN_PATH", token_path)

    with pytest.raises(DeploymentDetectionError, match="could not be read"):
        cluster_client(
            OutputSettings(
                name="local",
                repository="https://github.com/acme/manifests",
                directory=Path("local"),
                api_endpoint="https://kubernetes.default.svc",
                ca_path=Path(certifi.where()),
                connection_type="local",
            )
        )


def test_cluster_client_rejects_a_missing_ca_certificate(tmp_path: Path) -> None:
    with pytest.raises(DeploymentDetectionError, match="does not exist"):
        cluster_client(
            OutputSettings(
                name="example-dev",
                repository="https://github.com/acme/manifests",
                directory=Path("example-dev"),
                connection_type="eks",
                api_endpoint="https://kubernetes.example.test",
                ca_path=tmp_path / "absent.pem",
            )
        )
