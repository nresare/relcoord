# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from dulwich import porcelain
from manifest_builder import ExternalPlugins

from relcoord import change
from relcoord.change import (
    ChangeProgress,
    CommentPostError,
    CredentialError,
    DeployConfigError,
    DiffCommentProcessor,
)
from relcoord.config import OutputSettings
from relcoord.git import GitCredentialError
from relcoord.github import GithubCommentError, PostedComment
from relcoord.manifest_diff import ManifestDiff

CONFIG_REPO = "https://github.com/acme/config.git"
MANIFESTS_REPO = "https://github.com/acme/manifests.git"
PLUGINS_REPO = "https://github.com/acme/plugins.git"


@dataclass(frozen=True)
class GenerationResult:
    written_paths: set[Path]
    created_or_modified: set[object]
    removed: set[object]
    deploy_id: str | None = None


class Commenter:
    """Records the comments a processor asks it to post."""

    def __init__(
        self,
        url: str | None = "https://github.com/acme/config/pull/7#c1",
        updated: bool = False,
    ):
        self.calls: list[tuple[str, int, str]] = []
        self.markers: list[str | None] = []
        self._url = url
        self._updated = updated

    def post_comment(
        self, repo: str, pull_request: int, body: str, *, marker: str | None = None
    ) -> PostedComment:
        self.calls.append((repo, pull_request, body))
        self.markers.append(marker)
        return PostedComment(url=self._url, updated=self._updated)


class FailingCommenter:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def post_comment(
        self, repo: str, pull_request: int, body: str, *, marker: str | None = None
    ) -> PostedComment:
        raise self._error


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    calls: list[tuple[object, ...]] | None = None,
    captured: dict[str, object] | None = None,
    diff: ManifestDiff | None = None,
    diffs: dict[str, ManifestDiff] | None = None,
    written: int = 1,
    targets: bool = False,
) -> None:
    """Replace the git and manifest-builder work with recorded fakes.

    ``targets`` writes a ``version = 2`` top-level config into the checked out
    config directory, for which manifest-builder takes a target rather than
    template variables. ``diff`` is what every manifests checkout diffs to, and
    ``diffs`` says it per checkout directory name, for a change that reaches
    some of the configured repositories and not others.
    """
    recorded = calls if calls is not None else []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        recorded.append(("checkout", repo, commit, target.name, idcat))
        deploy_config = target / ".deploy"
        deploy_config.mkdir(parents=True)
        if targets:
            (deploy_config / "config.toml").write_text(
                'version = 2\n\n[[target]]\nname = "dev"\n'
            )

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        recorded.append(("clone", repo, target.name, kwargs))
        target.mkdir(parents=True)
        if repo == PLUGINS_REPO:
            (target / "plugins").mkdir()

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str | None,
        vars: dict[str, object] | None = None,
        target: str | None = None,
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        if captured is not None:
            captured["plugins"] = plugins
        recorded.append(
            (
                "generate",
                deploy_config.name,
                output_path.relative_to(tmp_path),
                create_commit,
                image,
                namespace,
                vars if target is None else target,
            )
        )
        return GenerationResult(
            written_paths={
                output_path / f"api{index}.yaml" for index in range(written)
            },
            created_or_modified=set(),
            removed=set(),
        )

    def fake_manifests_diff(manifests_checkout: Path, base_commit: str) -> ManifestDiff:
        recorded.append(("diff", manifests_checkout.name, base_commit))
        if diffs is not None:
            return diffs.get(manifests_checkout.name, ManifestDiff(stat="", diff=""))
        return diff if diff is not None else ManifestDiff(stat="", diff="")

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_manifests_diff", fake_manifests_diff)
    monkeypatch.setattr(
        change,
        "_push_repository",
        lambda *args, **kwargs: pytest.fail("a diff must not push"),
    )


def test_diff_generates_commits_and_diffs_without_pushing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_git(
        monkeypatch,
        tmp_path,
        calls=calls,
        diff=ManifestDiff(stat="api.yaml | 1 +\n", diff="+api"),
    )
    commenter = Commenter()

    result = DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=commenter
    ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert calls == [
        ("checkout", CONFIG_REPO, "deadbeef", "source", None),
        (
            "clone",
            MANIFESTS_REPO,
            "manifests",
            {"purpose": f"cloning manifests repo {MANIFESTS_REPO}", "depth": "1"},
        ),
        ("generate", ".deploy", Path("manifests"), True, None, "config", {}),
        ("diff", "manifests", "feedface"),
    ]
    assert result.generated_count == 1
    assert [entry.repository for entry in result.diffs] == [MANIFESTS_REPO]
    assert result.diffs[0].manifest_diff.diff == "+api"
    assert result.comment.posted
    assert result.comment.url == "https://github.com/acme/config/pull/7#c1"
    assert commenter.calls[0][:2] == (CONFIG_REPO, 7)
    assert "api.yaml | 1 +" in commenter.calls[0][2]


def test_diff_reports_progress_for_each_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(
        monkeypatch,
        tmp_path,
        diff=ManifestDiff(
            stat="api.yaml | 1 +\n", diff="diff --git a/api.yaml b/api.yaml\n+api\n"
        ),
    )

    events: list[ChangeProgress] = []
    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=Commenter()
    ).diff(CONFIG_REPO, "deadbeef", pull_request=7, progress=events.append)

    assert [event.phase for event in events] == [
        "source-checkout",
        "deploy-config",
        "manifests-checkout",
        "generate",
        "generated",
        "diff",
        "comment",
        "commented",
    ]
    by_phase = {event.phase: event for event in events}
    assert by_phase["source-checkout"].detail == {
        "repo": CONFIG_REPO,
        "commit": "deadbeef",
    }
    assert by_phase["source-checkout"].message == "checking out acme/config at deadbee"
    assert by_phase["diff"].detail == {"repository": MANIFESTS_REPO, "changed": 1}
    assert by_phase["diff"].message == "acme/manifests: 1 file changed"
    assert by_phase["commented"].message == "commented on acme/config pull request #7"
    assert by_phase["commented"].detail["pull_request"] == 7
    assert by_phase["commented"].detail["url"] == (
        "https://github.com/acme/config/pull/7#c1"
    )


def test_diff_counts_changed_files_from_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(
        monkeypatch,
        tmp_path,
        diff=ManifestDiff(
            stat="a.yaml | 1 +\nb.yaml | 1 +\n",
            diff=(
                "diff --git a/a.yaml b/a.yaml\n+a\ndiff --git a/b.yaml b/b.yaml\n+b\n"
            ),
        ),
    )

    events: list[ChangeProgress] = []
    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=Commenter()
    ).diff(CONFIG_REPO, "deadbeef", progress=events.append)

    by_phase = {event.phase: event for event in events}
    assert by_phase["diff"].detail["changed"] == 2


def test_diff_without_a_pull_request_returns_the_body_without_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = Commenter()

    events: list[ChangeProgress] = []
    result = DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=commenter
    ).diff(CONFIG_REPO, "deadbeef", progress=events.append)

    assert commenter.calls == []
    assert not result.comment.posted
    assert result.comment.url is None
    assert (
        "The generated output is the same before and after this change"
        in result.comment.body
    )
    assert [event.phase for event in events][-1] == "no-comment"


def test_diff_reports_no_changes_when_manifest_builder_changed_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)

    events: list[ChangeProgress] = []
    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=Commenter()
    ).diff(CONFIG_REPO, "deadbeef", progress=events.append)

    phases = [event.phase for event in events]
    assert "no-changes" in phases
    assert "diff" not in phases


def test_diff_generates_every_configured_output_before_diffing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_git(monkeypatch, tmp_path, calls=calls)
    outputs = [
        OutputSettings(
            name="example-dev",
            repository=MANIFESTS_REPO,
            directory=Path("example-dev"),
            vars={"cluster_name": "example-dev"},
        ),
        OutputSettings(
            name="example-prod",
            repository=MANIFESTS_REPO,
            directory=Path("example-prod"),
            vars={"cluster_name": "example-prod"},
        ),
    ]

    result = DiffCommentProcessor(outputs=outputs, commenter=Commenter()).diff(
        CONFIG_REPO, "deadbeef"
    )

    assert [output.name for output in result.outputs] == [
        "example-dev",
        "example-prod",
    ]
    assert result.generated_count == 2
    # One clone and one diff for the repository both outputs generate into.
    assert [call[0] for call in calls] == [
        "checkout",
        "clone",
        "generate",
        "generate",
        "diff",
    ]


def test_diff_generates_a_target_for_a_version_2_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_git(monkeypatch, tmp_path, calls=calls, targets=True)
    outputs = [
        OutputSettings(
            name="example-dev",
            repository=MANIFESTS_REPO,
            directory=Path("example-dev"),
            vars={"cluster_name": "example-dev"},
            target="dev",
        )
    ]

    DiffCommentProcessor(outputs=outputs, commenter=Commenter()).diff(
        CONFIG_REPO, "deadbeef"
    )

    assert [call for call in calls if call[0] == "generate"] == [
        (
            "generate",
            ".deploy",
            Path("manifests/example-dev"),
            True,
            None,
            "config",
            "dev",
        )
    ]


def test_diff_comments_only_on_the_repositories_the_change_affects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_repo = "https://github.com/acme/other-manifests.git"
    _fake_git(
        monkeypatch,
        tmp_path,
        diffs={
            "manifests-2": ManifestDiff(
                stat="prod/api.yaml | 1 +\n",
                diff="diff --git a/prod/api.yaml b/prod/api.yaml\n+api\n",
            )
        },
    )
    commenter = Commenter()
    outputs = [
        OutputSettings(name="dev", repository=MANIFESTS_REPO, directory=Path("dev")),
        OutputSettings(name="prod", repository=other_repo, directory=Path("prod")),
    ]

    result = DiffCommentProcessor(outputs=outputs, commenter=commenter).diff(
        CONFIG_REPO, "deadbeef", pull_request=7
    )

    # Both repositories were generated into and diffed; only the one the change
    # reached is worth a comment, and a single section renders without a heading.
    assert [entry.repository for entry in result.diffs] == [MANIFESTS_REPO, other_repo]
    body = commenter.calls[0][2]
    assert "prod/api.yaml | 1 +" in body
    assert MANIFESTS_REPO not in body
    assert other_repo not in body


def test_diff_reports_no_changes_when_no_output_is_affected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = Commenter()
    outputs = [
        OutputSettings(name="dev", repository=MANIFESTS_REPO, directory=Path("dev")),
        OutputSettings(
            name="prod",
            repository="https://github.com/acme/other-manifests.git",
            directory=Path("prod"),
        ),
    ]

    result = DiffCommentProcessor(outputs=outputs, commenter=commenter).diff(
        CONFIG_REPO, "deadbeef", pull_request=7
    )

    assert result.comment.body.startswith(
        "<!-- relcoord:manifest-diff -->\n\n"
        "The generated output is the same before and after this change"
    )


def test_diff_comments_once_per_request_for_multiple_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(
        monkeypatch, tmp_path, diff=ManifestDiff(stat="api.yaml | 1 +\n", diff="+api")
    )
    commenter = Commenter()
    outputs = [
        OutputSettings(name="dev", repository=MANIFESTS_REPO, directory=Path("dev")),
        OutputSettings(
            name="prod",
            repository="https://github.com/acme/other-manifests.git",
            directory=Path("prod"),
        ),
    ]

    result = DiffCommentProcessor(outputs=outputs, commenter=commenter).diff(
        CONFIG_REPO, "deadbeef", pull_request=7
    )

    assert [entry.repository for entry in result.diffs] == [
        MANIFESTS_REPO,
        "https://github.com/acme/other-manifests.git",
    ]
    assert len(commenter.calls) == 1
    body = commenter.calls[0][2]
    assert f"### {MANIFESTS_REPO}" in body
    assert "### https://github.com/acme/other-manifests.git" in body


def test_diff_system_mode_uses_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    _fake_git(monkeypatch, tmp_path, calls=calls)

    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=Commenter()
    ).diff(CONFIG_REPO, "deadbeef", system=True)

    generate_call = next(call for call in calls if call[0] == "generate")
    assert generate_call[1] == "source"
    assert generate_call[5] is None


def test_diff_checks_out_the_plugins_repository_and_passes_it_to_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    _fake_git(monkeypatch, tmp_path, captured=captured)

    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO,
        plugins_repository=PLUGINS_REPO,
        commenter=Commenter(),
    ).diff(CONFIG_REPO, "deadbeef")

    assert captured["plugins"] == ExternalPlugins(
        path=tmp_path / "plugins" / "plugins",
        source=f"{PLUGINS_REPO}@feedface",
    )


def test_diff_leaves_the_plugins_repository_out_of_system_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    calls: list[tuple[object, ...]] = []
    _fake_git(monkeypatch, tmp_path, calls=calls, captured=captured)

    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO,
        plugins_repository=PLUGINS_REPO,
        commenter=Commenter(),
    ).diff(CONFIG_REPO, "deadbeef", system=True)

    assert captured["plugins"] is None
    assert [
        call for call in calls if call[0] == "clone" and call[1] == PLUGINS_REPO
    ] == []


def test_diff_requires_the_deploy_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    monkeypatch.setattr(
        change,
        "_checkout_commit",
        lambda repo, commit, target, idcat: target.mkdir(parents=True),
    )

    with pytest.raises(DeployConfigError) as excinfo:
        DiffCommentProcessor(
            manifests_repository=MANIFESTS_REPO, commenter=Commenter()
        ).diff(CONFIG_REPO, "deadbeef")

    assert "does not contain a .deploy directory" in str(excinfo.value)


def test_diff_reports_a_rejected_comment_as_a_comment_post_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = FailingCommenter(GithubCommentError("GitHub returned HTTP 403"))

    with pytest.raises(CommentPostError) as excinfo:
        DiffCommentProcessor(
            manifests_repository=MANIFESTS_REPO, commenter=commenter
        ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert "GitHub returned HTTP 403" in str(excinfo.value)


def test_diff_reports_a_missing_installation_token_as_a_credential_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = FailingCommenter(GitCredentialError("idcat returned HTTP 404"))

    with pytest.raises(CredentialError) as excinfo:
        DiffCommentProcessor(
            manifests_repository=MANIFESTS_REPO, commenter=commenter
        ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert "idcat returned HTTP 404" in str(excinfo.value)
    assert "pull request #7" in str(excinfo.value)


def test_diff_removes_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    _fake_git(monkeypatch, workdir)

    DiffCommentProcessor(
        manifests_repository=MANIFESTS_REPO, commenter=Commenter()
    ).diff(CONFIG_REPO, "deadbeef")

    assert not workdir.exists()


def test_manifests_diff_compares_the_cloned_commit_with_what_was_generated(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "manifests"
    checkout.mkdir()
    repo = porcelain.init(str(checkout))
    (checkout / "api.yaml").write_text("name: api\n")
    porcelain.add(repo, [str(checkout / "api.yaml")])
    base_commit = porcelain.commit(
        repo,
        message=b"cloned state",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    ).decode("ascii")
    (checkout / "api.yaml").write_text("name: api\nreplicas: 2\n")
    porcelain.add(repo, [str(checkout / "api.yaml")])
    porcelain.commit(
        repo,
        message=b"generated",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    repo.close()

    result = change._manifests_diff(checkout, base_commit)

    assert "+replicas: 2" in result.diff
    assert "api.yaml" in result.stat


def test_manifests_diff_is_empty_when_manifest_builder_made_no_commit(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "manifests"
    checkout.mkdir()
    repo = porcelain.init(str(checkout))
    (checkout / "api.yaml").write_text("name: api\n")
    porcelain.add(repo, [str(checkout / "api.yaml")])
    base_commit = porcelain.commit(
        repo,
        message=b"cloned state",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    ).decode("ascii")
    repo.close()

    result = change._manifests_diff(checkout, base_commit)

    assert result == ManifestDiff(stat="", diff="", summary="", filtered_diff=None)


def test_diff_marks_its_comment_so_a_later_diff_can_update_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(
        monkeypatch, tmp_path, diff=ManifestDiff(stat="api.yaml | 1 +\n", diff="+api")
    )
    commenter = Commenter(updated=True)
    outputs = [
        OutputSettings(name="dev", repository=MANIFESTS_REPO, directory=Path("dev"))
    ]

    result = DiffCommentProcessor(outputs=outputs, commenter=commenter).diff(
        CONFIG_REPO, "deadbeef", pull_request=7
    )

    marker = "<!-- relcoord:manifest-diff -->"
    assert commenter.markers == [marker]
    assert marker in commenter.calls[0][2]
    assert result.comment.updated
