# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from relcoord.config import IdcatSettings
from relcoord.git import GitCredentialError, installation_token_cache
from relcoord.github import (
    GITHUB_API_VERSION,
    GithubCommentError,
    GithubIssueCommenter,
)

COMMENTS_URL = "https://api.github.com/repos/acme/config/issues/7/comments"
REPO = "https://github.com/acme/config.git"


@pytest.fixture(autouse=True)
def clear_installation_token_cache() -> Iterator[None]:
    installation_token_cache.clear()
    yield
    installation_token_cache.clear()


def _idcat(tmp_path: Path) -> IdcatSettings:
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    return IdcatSettings(
        endpoint="https://idcat.example.test",
        github_app="deployments",
        token_path=token_file,
    )


def test_post_comment_uses_an_idcat_installation_token(tmp_path: Path) -> None:
    with (
        patch(
            "relcoord.git.fetch_installation_token",
            return_value="github-installation-token",
        ) as fetch,
        patch("relcoord.github.httpx.post") as post,
    ):
        post.return_value = httpx.Response(
            201,
            json={"html_url": "https://github.com/acme/config/pull/7#issuecomment-1"},
            request=httpx.Request("POST", COMMENTS_URL),
        )

        url = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff"
        )

    assert url == "https://github.com/acme/config/pull/7#issuecomment-1"
    assert fetch.call_args.args[1].name == "config"
    post.assert_called_once_with(
        COMMENTS_URL,
        json={"body": "the diff"},
        headers={
            "Authorization": "Bearer github-installation-token",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        timeout=30.0,
    )


def test_post_comment_uses_a_configured_api_base_url(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.post") as post,
    ):
        post.return_value = httpx.Response(
            201, json={}, request=httpx.Request("POST", COMMENTS_URL)
        )

        GithubIssueCommenter(
            idcat=_idcat(tmp_path), api_base_url="https://github.example.test/api/v3/"
        ).post_comment(REPO, 7, "the diff")

    assert post.call_args.args[0] == (
        "https://github.example.test/api/v3/repos/acme/config/issues/7/comments"
    )


def test_post_comment_reports_a_rejected_comment(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.post") as post,
        pytest.raises(GithubCommentError) as excinfo,
    ):
        post.return_value = httpx.Response(
            403,
            text="Resource not accessible by integration",
            request=httpx.Request("POST", COMMENTS_URL),
        )

        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(REPO, 7, "the diff")

    assert "GitHub returned HTTP 403" in str(excinfo.value)
    assert "Resource not accessible by integration" in str(excinfo.value)


def test_post_comment_reports_an_unreachable_api(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch(
            "relcoord.github.httpx.post",
            side_effect=httpx.ConnectError("Name or service not known"),
        ),
        pytest.raises(GithubCommentError) as excinfo,
    ):
        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(REPO, 7, "the diff")

    assert "failed to post a comment to" in str(excinfo.value)
    assert "Name or service not known" in str(excinfo.value)


def test_post_comment_propagates_an_idcat_failure(tmp_path: Path) -> None:
    with (
        patch(
            "relcoord.git.fetch_installation_token",
            side_effect=GitCredentialError("idcat returned HTTP 404"),
        ),
        pytest.raises(GitCredentialError),
    ):
        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(REPO, 7, "the diff")


def test_post_comment_requires_idcat_configuration() -> None:
    with pytest.raises(GithubCommentError) as excinfo:
        GithubIssueCommenter().post_comment(REPO, 7, "the diff")

    assert "no GitHub installation token available" in str(excinfo.value)


def test_post_comment_rejects_a_non_github_repository(tmp_path: Path) -> None:
    with pytest.raises(GithubCommentError) as excinfo:
        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            "https://git.example.com/acme/config.git", 7, "the diff"
        )

    assert "only https github.com repository URLs are supported" in str(excinfo.value)


def test_post_comment_tolerates_a_response_without_a_url(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.post") as post,
    ):
        post.return_value = httpx.Response(
            201, text="not json", request=httpx.Request("POST", COMMENTS_URL)
        )

        url = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff"
        )

    assert url is None
