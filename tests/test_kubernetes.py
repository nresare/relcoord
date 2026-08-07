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
            }
        ]
    },
}


@dataclass(frozen=True)
class Ref:
    kind: str
    namespace: str | None
    name: str


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
            Ref("Deployment", "default", "api"),
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
            created_or_modified={Ref("Deployment", "default", "api")},
            removed=set(),
        )

    assert watches == 1
    assert "has materialised in cluster example-dev" in caplog.text


def deployments_handler(listed: dict[str, Any], *watched: dict[str, Any]):
    """Serve discovery, one listing of ``listed``, then a watch of ``watched``."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in DISCOVERY:
            return httpx.Response(200, json=DISCOVERY[path])
        if path != "/apis/apps/v1/namespaces/default/deployments":
            return httpx.Response(500, json={"unexpected": path})
        if "watch" not in request.url.params:
            return httpx.Response(200, json=listing(listed))
        return httpx.Response(
            200,
            content=watch_stream(*(("MODIFIED", obj) for obj in watched)),
        )

    return handler


def wait_for_deployment(handler, *, timeout_seconds: float = 5) -> None:
    detector(handler, timeout_seconds=timeout_seconds).wait_for_success(
        deploy_id=DEPLOY_ID,
        created_or_modified={Ref("Deployment", "default", "api")},
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
        created_or_modified={Ref("Deployment", "default", "api")},
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
            created_or_modified={Ref("Deployment", "default", "api")},
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


def test_detector_reports_a_failing_api_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(DeploymentDetectionError, match="status 403"):
        detector(handler).wait_for_success(
            deploy_id=DEPLOY_ID,
            created_or_modified={Ref("Deployment", "default", "api")},
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
            created_or_modified={Ref("Deployment", "default", "api")},
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
