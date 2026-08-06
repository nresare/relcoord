# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import base64
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
import yaml

from relcoord.eks import TOKEN_PREFIX, EksError, cluster_token, update_kubeconfig


def make_session(region: str | None = "eu-west-1") -> boto3.Session:
    return boto3.Session(
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
        region_name=region,
    )


def decode(token: str) -> str:
    encoded = token.removeprefix(TOKEN_PREFIX)
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode()


def test_cluster_token_is_a_presigned_sts_url() -> None:
    token = cluster_token(make_session(), "platform-dev")

    assert token.startswith(TOKEN_PREFIX)
    assert "=" not in token
    url = urlparse(decode(token))
    query = parse_qs(url.query)
    assert url.netloc == "sts.eu-west-1.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert "X-Amz-Signature" in query


def test_cluster_token_requires_a_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-such-config"))

    with pytest.raises(EksError, match="region"):
        cluster_token(make_session(region=None), "platform-dev")


def test_update_kubeconfig_creates_named_entries(tmp_path: Path) -> None:
    path = tmp_path / "config"

    update_kubeconfig(
        path,
        name="platform-dev",
        endpoint="https://example.eks.amazonaws.com",
        ca_data="Y2E=",
        token="k8s-aws-v1.abc",
        set_current=True,
    )

    doc = yaml.safe_load(path.read_text())
    assert doc["clusters"] == [
        {
            "name": "platform-dev",
            "cluster": {
                "server": "https://example.eks.amazonaws.com",
                "certificate-authority-data": "Y2E=",
            },
        }
    ]
    assert doc["users"] == [
        {"name": "platform-dev", "user": {"token": "k8s-aws-v1.abc"}}
    ]
    assert doc["contexts"] == [
        {
            "name": "platform-dev",
            "context": {"cluster": "platform-dev", "user": "platform-dev"},
        }
    ]
    assert doc["current-context"] == "platform-dev"
    assert path.stat().st_mode & 0o777 == 0o600


def test_update_kubeconfig_replaces_matching_entries_and_keeps_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{"name": "other", "cluster": {"server": "https://other"}}],
                "users": [
                    {"name": "other", "user": {}},
                    {"name": "platform-dev", "user": {"token": "stale"}},
                ],
                "contexts": [{"name": "other", "context": {}}],
                "current-context": "other",
            }
        )
    )

    update_kubeconfig(
        path,
        name="platform-dev",
        endpoint="https://example.eks.amazonaws.com",
        ca_data="Y2E=",
        token="k8s-aws-v1.fresh",
        set_current=False,
    )

    doc = yaml.safe_load(path.read_text())
    assert [entry["name"] for entry in doc["users"]] == ["other", "platform-dev"]
    assert doc["users"][1]["user"]["token"] == "k8s-aws-v1.fresh"
    assert [entry["name"] for entry in doc["clusters"]] == ["other", "platform-dev"]
    assert doc["current-context"] == "other"
