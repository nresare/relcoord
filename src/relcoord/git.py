# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from dulwich import porcelain

from relcoord.config import IdcatSettings

GITHUB_TOKEN_USERNAME = "x-access-token"
_SCP_STYLE_GIT_URI = re.compile(r"(?:[^@/:]+@)?([^/:]+):(.+)")
_SSH_STYLE_SCHEMES = {"ssh", "git+ssh"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloneResult:
    source: str
    path: str
    head: str


@dataclass(frozen=True)
class GithubRepo:
    owner: str
    name: str


@dataclass(frozen=True)
class SshStyleGitUri:
    hostname: str
    owner: str
    name: str


class GitCredentialError(Exception):
    pass


@dataclass(frozen=True)
class GitCredentials:
    username: str | None = None
    password: str | None = None


def clone_repository(
    source: str,
    *,
    branch: str | None = None,
    idcat: IdcatSettings | None = None,
) -> CloneResult:
    credentials = github_https_credentials(source, idcat)
    target = Path(tempfile.mkdtemp(prefix="relcoord-clone-"))
    clone_output = BytesIO()
    try:
        if credentials.username is None:
            repo = porcelain.clone(
                source,
                target,
                checkout=True,
                branch=branch,
                depth=1,
                errstream=clone_output,
            )
        else:
            repo = porcelain.clone(
                source,
                target,
                checkout=True,
                branch=branch,
                depth=1,
                errstream=clone_output,
                username=credentials.username,
                password=credentials.password or "",
            )
        try:
            head = repo.head().decode("ascii")
        finally:
            repo.close()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    return CloneResult(source=source, path=str(target), head=head)


def github_https_credentials(
    source: str, idcat: IdcatSettings | None
) -> GitCredentials:
    repo = github_repo_from_url(source)
    if repo is None or idcat is None:
        return GitCredentials()

    try:
        bearer_token = idcat.bearer_token()
    except OSError as exc:
        raise GitCredentialError(
            f"failed to read idcat token-path {idcat.token_path}: {exc}"
        ) from exc

    installation_token = fetch_installation_token(idcat, repo, bearer_token)
    return GitCredentials(
        username=GITHUB_TOKEN_USERNAME,
        password=installation_token,
    )


def fetch_installation_token(
    idcat: IdcatSettings, repo: GithubRepo, bearer_token: str
) -> str:
    url = installation_token_url(idcat, repo)
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        logger.warning(
            "Failed to request installation token from idcat at %s: %s",
            url,
            exc,
        )
        raise GitCredentialError(
            f"failed to request installation token from idcat at {url}: {exc}"
        ) from exc
    if not response.is_success:
        raise GitCredentialError(
            f"idcat returned HTTP {response.status_code}: {response.text}"
        )

    installation_token = response.text.strip()
    if not installation_token:
        raise GitCredentialError("idcat returned an empty installation token")
    return installation_token


def installation_token_url(idcat: IdcatSettings, repo: GithubRepo) -> str:
    endpoint = idcat.endpoint.rstrip("/")
    segments = [
        "installation-token",
        idcat.github_app,
        repo.owner,
        repo.name,
    ]
    encoded_segments = "/".join(quote(segment, safe="") for segment in segments)
    return f"{endpoint}/{encoded_segments}"


def github_repo_from_url(source: str) -> GithubRepo | None:
    url = urlparse(source)
    if url.scheme != "https" or url.hostname is None:
        return None
    if url.hostname.lower() != "github.com":
        return None

    components = [
        component for component in url.path.lstrip("/").split("/") if component
    ]
    if len(components) < 2:
        return None

    name = components[1]
    name = name.removesuffix(".git")
    if not components[0] or not name:
        return None
    return GithubRepo(owner=components[0], name=name)


def ssh_style_git_uri_from_url(source: str) -> SshStyleGitUri | None:
    parsed = urlparse(source)
    if parsed.scheme in _SSH_STYLE_SCHEMES:
        hostname = parsed.hostname
        path = parsed.path.lstrip("/")
    else:
        match = _SCP_STYLE_GIT_URI.fullmatch(source)
        if match is None:
            return None
        hostname = match.group(1)
        path = match.group(2)

    if hostname is None:
        return None

    components = [component for component in path.split("/") if component]
    if len(components) != 2:
        return None

    name = components[1]
    name = name.removesuffix(".git")
    if not components[0] or not name:
        return None
    return SshStyleGitUri(hostname=hostname, owner=components[0], name=name)


def is_ssh_style_git_uri(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in _SSH_STYLE_SCHEMES or (
        parsed.scheme == "" and _SCP_STYLE_GIT_URI.fullmatch(source) is not None
    )


def github_https_url_from_ssh_style_uri(source: str) -> str | None:
    repo = ssh_style_git_uri_from_url(source)
    if repo is None:
        return None
    if repo.hostname.lower() != "github.com":
        return None
    return f"https://github.com/{repo.owner}/{repo.name}.git"
