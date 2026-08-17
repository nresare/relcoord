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

        posted = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff"
        )

    assert posted.url == "https://github.com/acme/config/pull/7#issuecomment-1"
    assert not posted.updated
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

        posted = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff"
        )

    assert posted.url is None


def _listing(comments: list[dict[str, object]], page: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json=comments,
        request=httpx.Request("GET", f"{COMMENTS_URL}?page={page}"),
    )


def test_post_comment_edits_an_earlier_comment_carrying_the_marker(
    tmp_path: Path,
) -> None:
    marker = "<!-- relcoord:manifest-diff -->"
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.get") as get,
        patch("relcoord.github.httpx.patch") as patch_request,
        patch("relcoord.github.httpx.post") as post,
    ):
        get.return_value = _listing(
            [
                {"id": 1, "body": "a human's review"},
                {"id": 2, "body": f"{marker}\n\nan older diff"},
            ]
        )
        patch_request.return_value = httpx.Response(
            200,
            json={"html_url": "https://github.com/acme/config/pull/7#issuecomment-2"},
            request=httpx.Request("PATCH", COMMENTS_URL),
        )

        posted = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, f"{marker}\n\nthe new diff", marker=marker
        )

    assert posted.updated
    assert posted.url == "https://github.com/acme/config/pull/7#issuecomment-2"
    post.assert_not_called()
    assert patch_request.call_args.args[0] == (
        "https://api.github.com/repos/acme/config/issues/comments/2"
    )
    assert patch_request.call_args.kwargs["json"] == {
        "body": f"{marker}\n\nthe new diff"
    }


def test_post_comment_posts_when_no_comment_carries_the_marker(tmp_path: Path) -> None:
    marker = "<!-- relcoord:manifest-diff -->"
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.get") as get,
        patch("relcoord.github.httpx.patch") as patch_request,
        patch("relcoord.github.httpx.post") as post,
    ):
        get.return_value = _listing(
            [
                {"id": 1, "body": "a human's review"},
                {
                    "id": 2,
                    "body": "<!-- relcoord:manifest-diff prod -->\nanother scope",
                },
            ]
        )
        post.return_value = httpx.Response(
            201, json={}, request=httpx.Request("POST", COMMENTS_URL)
        )

        posted = GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff", marker=marker
        )

    assert not posted.updated
    patch_request.assert_not_called()
    assert post.call_args.args[0] == COMMENTS_URL


def test_post_comment_edits_the_newest_marked_comment_across_pages(
    tmp_path: Path,
) -> None:
    marker = "<!-- relcoord:manifest-diff -->"
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.get") as get,
        patch("relcoord.github.httpx.patch") as patch_request,
    ):
        get.side_effect = [
            _listing([{"id": index, "body": marker} for index in range(100)]),
            _listing([{"id": 200, "body": f"{marker} newest"}], page=2),
        ]
        patch_request.return_value = httpx.Response(
            200, json={}, request=httpx.Request("PATCH", COMMENTS_URL)
        )

        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff", marker=marker
        )

    assert [call.kwargs["params"]["page"] for call in get.call_args_list] == [1, 2]
    assert patch_request.call_args.args[0].endswith("/issues/comments/200")


def test_post_comment_reports_a_rejected_comment_listing(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.get") as get,
        pytest.raises(GithubCommentError) as excinfo,
    ):
        get.return_value = httpx.Response(
            403, text="no", request=httpx.Request("GET", COMMENTS_URL)
        )

        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(
            REPO, 7, "the diff", marker="<!-- relcoord:manifest-diff -->"
        )

    assert "GitHub returned HTTP 403" in str(excinfo.value)
    assert "for the comments at" in str(excinfo.value)


def test_post_comment_without_a_marker_does_not_list_comments(tmp_path: Path) -> None:
    with (
        patch("relcoord.git.fetch_installation_token", return_value="token"),
        patch("relcoord.github.httpx.get") as get,
        patch("relcoord.github.httpx.post") as post,
    ):
        post.return_value = httpx.Response(
            201, json={}, request=httpx.Request("POST", COMMENTS_URL)
        )

        GithubIssueCommenter(idcat=_idcat(tmp_path)).post_comment(REPO, 7, "the diff")

    get.assert_not_called()
