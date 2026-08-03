# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Posting pull request comments to the GitHub REST API.

The token is an idcat-issued installation token for the repository the comment
goes to, taken from the same cache the git operations use, so a service that
already talks to a repository does not need a second credential to comment on
it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from relcoord.config import IdcatSettings
from relcoord.git import github_https_credentials, github_repo_from_url

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
COMMENT_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


class GithubCommentError(Exception):
    """Raised when a pull request comment cannot be posted."""


class IssueCommenter(Protocol):
    def post_comment(self, repo: str, pull_request: int, body: str) -> str | None: ...


@dataclass(frozen=True)
class GithubIssueCommenter:
    idcat: IdcatSettings | None = None
    api_base_url: str = GITHUB_API_BASE_URL

    def post_comment(self, repo: str, pull_request: int, body: str) -> str | None:
        """Comment on a pull request, returning the comment's URL if given.

        Raises:
            GithubCommentError: if the repository is not a GitHub HTTPS URL, no
                installation token is available, or GitHub rejects the comment.
            GitCredentialError: if idcat does not issue an installation token.
        """
        github_repo = github_repo_from_url(repo)
        if github_repo is None:
            raise GithubCommentError(
                f"cannot comment on a pull request of {repo}: "
                "only https github.com repository URLs are supported"
            )

        credentials = github_https_credentials(repo, self.idcat)
        if credentials.password is None:
            raise GithubCommentError(
                f"no GitHub installation token available for {repo}; "
                "commenting on a pull request requires [idcat] configuration"
            )

        url = (
            f"{self.api_base_url.rstrip('/')}/repos/"
            f"{github_repo.owner}/{github_repo.name}/issues/{pull_request}/comments"
        )
        try:
            response = httpx.post(
                url,
                json={"body": body},
                headers={
                    "Authorization": f"Bearer {credentials.password}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                timeout=COMMENT_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise GithubCommentError(
                f"failed to post a comment to {url}: {exc}"
            ) from exc

        if not response.is_success:
            raise GithubCommentError(
                f"GitHub returned HTTP {response.status_code} "
                f"for a comment to {url}: {response.text}"
            )
        return _comment_url(response)


def _comment_url(response: httpx.Response) -> str | None:
    try:
        payload: Any = response.json()
    except ValueError:
        logger.warning("GitHub answered a posted comment with a non-JSON body")
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("html_url")
    return url if isinstance(url, str) else None
