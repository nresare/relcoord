# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Create a kubeconfig entry for an EKS cluster from the current AWS credentials.

An EKS bearer token is a presigned STS GetCallerIdentity URL carrying the cluster
name in the ``x-k8s-aws-id`` header. It is signed with whatever credentials the
AWS SDK resolves, so an already-assumed cross-account role works without any
extra plumbing.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import boto3
import click
import yaml
from botocore.signers import RequestSigner

TOKEN_PREFIX = "k8s-aws-v1."
TOKEN_LIFETIME_MINUTES = 15
DEFAULT_KUBECONFIG = Path.home() / ".kube" / "config"


class EksError(Exception):
    pass


def cluster_token(session: boto3.Session, cluster_name: str) -> str:
    """Return a bearer token the given cluster's API server will accept."""
    region = session.region_name
    if region is None:
        raise EksError("no AWS region configured")
    credentials = session.get_credentials()
    if credentials is None:
        raise EksError("no AWS credentials found")
    sts = session.client("sts")
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        credentials,
        session.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": (
                f"https://sts.{region}.amazonaws.com/"
                "?Action=GetCallerIdentity&Version=2011-06-15"
            ),
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        region_name=region,
        # The signature stays valid for 15 minutes regardless; a short expiry
        # keeps the presigned URL itself from outliving the token.
        expires_in=60,
        operation_name="",
    )
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return TOKEN_PREFIX + encoded


def kubeconfig_path() -> Path:
    """Return the kubeconfig to write, honouring KUBECONFIG if it is set."""
    configured = os.environ.get("KUBECONFIG")
    if not configured:
        return DEFAULT_KUBECONFIG
    return Path(configured.split(os.pathsep)[0]).expanduser()


def update_kubeconfig(
    path: Path,
    *,
    name: str,
    endpoint: str,
    ca_data: str,
    token: str,
    set_current: bool,
) -> None:
    """Merge cluster, user and context entries named ``name`` into ``path``."""
    doc = _load(path)
    _upsert(
        doc,
        "clusters",
        {
            "name": name,
            "cluster": {
                "server": endpoint,
                "certificate-authority-data": ca_data,
            },
        },
    )
    _upsert(doc, "users", {"name": name, "user": {"token": token}})
    _upsert(
        doc,
        "contexts",
        {"name": name, "context": {"cluster": name, "user": name}},
    )
    if set_current or not doc.get("current-context"):
        doc["current-context"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    path.chmod(0o600)


def _load(path: Path) -> dict[str, Any]:
    doc: Any = None
    if path.exists():
        doc = yaml.safe_load(path.read_text())
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise EksError(f"{path} does not contain a kubeconfig mapping")
    doc.setdefault("apiVersion", "v1")
    doc.setdefault("kind", "Config")
    return doc


def _upsert(doc: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    entries = doc.setdefault(key, [])
    if not isinstance(entries, list):
        raise EksError(f"kubeconfig {key} is not a list")
    for index, existing in enumerate(entries):
        if isinstance(existing, dict) and existing.get("name") == entry["name"]:
            entries[index] = entry
            return
    entries.append(entry)


@click.command()
@click.argument("cluster_name")
@click.option(
    "--region",
    default=None,
    help="AWS region of the cluster. Defaults to the region of the AWS session.",
)
@click.option(
    "--profile",
    default=None,
    help="AWS profile to use. Defaults to the ambient credentials.",
)
@click.option(
    "--endpoint",
    required=True,
    help="API server URL of the cluster.",
)
@click.option(
    "--ca-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="PEM file holding the cluster CA certificate.",
)
@click.option(
    "--kubeconfig",
    "kubeconfig",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Kubeconfig file to update. Defaults to $KUBECONFIG or ~/.kube/config.",
)
@click.option(
    "--set-current/--no-set-current",
    default=True,
    show_default=True,
    help="Make the new context the current one.",
)
def main(
    cluster_name: str,
    region: str | None,
    profile: str | None,
    endpoint: str,
    ca_file: Path,
    kubeconfig: Path | None,
    set_current: bool,
) -> None:
    """Write a kubeconfig context named CLUSTER_NAME with a fresh EKS token."""
    session = boto3.Session(profile_name=profile, region_name=region)
    path = kubeconfig or kubeconfig_path()
    try:
        update_kubeconfig(
            path,
            name=cluster_name,
            endpoint=endpoint,
            ca_data=base64.b64encode(ca_file.read_bytes()).decode(),
            token=cluster_token(session, cluster_name),
            set_current=set_current,
        )
    except EksError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote context {cluster_name} to {path} "
        f"(token valid for {TOKEN_LIFETIME_MINUTES} minutes)"
    )


if __name__ == "__main__":
    main()
