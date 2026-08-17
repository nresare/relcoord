# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dulwich import porcelain
from dulwich.objects import Commit, ObjectID
from dulwich.repo import Repo

from relcoord.manifest_diff import (
    MANIFEST_BUILDER_VERSION,
    MAX_COMMENT_CHARS,
    DiffSection,
    ManifestDiff,
    build_comment_body,
    comment_marker,
    filter_metadata_hunks,
    manifest_diff,
    markdown_fence,
)

DEPLOYMENT = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  name: app\n"
    "  labels:\n"
    "    app.kubernetes.io/version: {version}\n"
    "  annotations:\n"
    "    noa.re/deploy-id: {deploy_id}\n"
    "spec:\n"
    "  template:\n"
    "    spec:\n"
    "      containers:\n"
    "      - name: app\n"
    "        image: example/app:{version}\n"
)

CHECKSUM_DEPLOYMENT = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  name: app\n"
    "  annotations:\n"
    "    noa.re/deploy-id: generated\n"
    "spec:\n"
    "  replicas: {replicas}\n"
    "  template:\n"
    "    metadata:\n"
    "      annotations:\n"
    "        checksum/config: {checksum}\n"
    "      labels:\n"
    "        app: app\n"
    "    spec:\n"
    "      containers:\n"
    "      - name: app\n"
    "        image: example/app:v1\n"
)

SERVICE = (
    "apiVersion: v1\n"
    "kind: Service\n"
    "metadata:\n"
    "  name: app\n"
    "  labels:\n"
    "    app.kubernetes.io/version: {version}\n"
    "  annotations:\n"
    "    noa.re/deploy-id: {deploy_id}\n"
    "spec:\n"
    "  ports:\n"
    "  - port: 80\n"
)


def _commit(repo: Repo, files: Mapping[str, str | None], message: str) -> ObjectID:
    """Write files (``None`` removes one) and commit them, returning the commit."""
    checkout = Path(repo.path)
    for name, content in files.items():
        path = checkout / name
        if content is None:
            path.unlink()
            porcelain.remove(repo, [str(path)])
            continue
        path.write_text(content)
        porcelain.add(repo, [str(path)])
    return ObjectID(
        porcelain.commit(
            repo,
            message=message.encode(),
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
        )
    )


def _tree(repo: Repo, commit: ObjectID) -> ObjectID:
    obj = repo[commit]
    assert isinstance(obj, Commit)
    return ObjectID(obj.tree)


def _diff(repo: Repo, first: ObjectID, second: ObjectID | None = None) -> ManifestDiff:
    if second is None:
        return manifest_diff(repo, _tree(repo, first), _tree(repo, first))
    return manifest_diff(repo, _tree(repo, first), _tree(repo, second))


def test_manifest_diff_reports_stat_and_diff_between_commits(tmp_path: Path) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo, {"gone.yaml": "old: true\n", "app.yaml": "name: app\n"}, "one"
    )
    second = _commit(
        repo,
        {"gone.yaml": None, "app.yaml": "name: app\nreplicas: 2\n"},
        "two",
    )

    result = _diff(repo, first, second)

    assert "app.yaml" in result.stat
    assert "gone.yaml" in result.stat
    assert "+replicas: 2" in result.diff
    assert "-old: true" in result.diff


def test_manifest_diff_is_empty_when_nothing_changed(tmp_path: Path) -> None:
    repo = porcelain.init(str(tmp_path))
    commit = _commit(repo, {"app.yaml": "name: app\n"}, "one")

    result = _diff(repo, commit)

    assert result == ManifestDiff(stat="", diff="", summary="", filtered_diff=None)
    comment = build_comment_body(
        [DiffSection(heading=None, diff=result)], full_diff_reference="unused"
    )
    assert (
        "The generated output is the same before and after this change" in comment.body
    )
    assert not comment.omitted_context_diff


def test_manifest_diff_summarizes_repeated_metadata_and_filters_noise(
    tmp_path: Path,
) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {
            "deployment.yaml": DEPLOYMENT.format(
                version="v1.8.0", deploy_id="old-deploy"
            ),
            "service.yaml": SERVICE.format(version="v1.8.0", deploy_id="old-deploy"),
        },
        "one",
    )
    second = _commit(
        repo,
        {
            "deployment.yaml": DEPLOYMENT.format(
                version="v1.8.1", deploy_id="new-deploy"
            ),
            "service.yaml": SERVICE.format(version="v1.8.1", deploy_id="new-deploy"),
        },
        "two",
    )

    result = _diff(repo, first, second)

    assert (
        "- Label `app.kubernetes.io/version` changed from `v1.8.0` to `v1.8.1` "
        "on 2 manifests."
    ) in result.summary
    assert "noa.re/deploy-id" in result.diff
    assert "noa.re/deploy-id" not in result.summary
    assert result.filtered_diff is not None
    assert "noa.re/deploy-id" not in result.filtered_diff
    assert "app.kubernetes.io/version" not in result.filtered_diff
    assert "image: example/app:v1.8.1" in result.filtered_diff


def test_manifest_diff_keeps_a_one_off_metadata_change_in_the_diff(
    tmp_path: Path,
) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.0", deploy_id="deploy")},
        "one",
    )
    second = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.1", deploy_id="deploy")},
        "two",
    )

    result = _diff(repo, first, second)

    assert result.summary == ""
    assert "app.kubernetes.io/version: v1.8.1" in result.diff


def test_manifest_diff_drops_a_deploy_id_only_change(tmp_path: Path) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.0", deploy_id="old")},
        "one",
    )
    second = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.0", deploy_id="new")},
        "two",
    )

    result = _diff(repo, first, second)

    assert "noa.re/deploy-id: new" in result.diff
    assert result.filtered_diff == ""
    comment = build_comment_body(
        [DiffSection(heading=None, diff=result)],
        full_diff_reference="returned in the response",
    )
    assert "noa.re/deploy-id" not in comment.body
    assert "returned in the response" in comment.body
    assert comment.omitted_context_diff


def test_manifest_diff_drops_a_checksum_only_change(tmp_path: Path) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {"deployment.yaml": CHECKSUM_DEPLOYMENT.format(replicas=1, checksum="aaa")},
        "one",
    )
    second = _commit(
        repo,
        {"deployment.yaml": CHECKSUM_DEPLOYMENT.format(replicas=1, checksum="bbb")},
        "two",
    )

    result = _diff(repo, first, second)

    assert "checksum/config: bbb" in result.diff
    assert result.filtered_diff == ""


def test_manifest_diff_keeps_a_real_change_next_to_a_checksum(tmp_path: Path) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {"deployment.yaml": CHECKSUM_DEPLOYMENT.format(replicas=1, checksum="aaa")},
        "one",
    )
    second = _commit(
        repo,
        {"deployment.yaml": CHECKSUM_DEPLOYMENT.format(replicas=2, checksum="bbb")},
        "two",
    )

    result = _diff(repo, first, second)

    assert result.filtered_diff is not None
    assert "+  replicas: 2" in result.filtered_diff
    assert "checksum/config" not in result.filtered_diff


def test_filter_metadata_hunks_drops_an_added_checksum_annotation() -> None:
    raw_diff = (
        "diff --git a/deployment.yaml b/deployment.yaml\n"
        "index 1234567..89abcde 100644\n"
        "--- a/deployment.yaml\n"
        "+++ b/deployment.yaml\n"
        "@@ -12,6 +12,9 @@ spec:\n"
        "   template:\n"
        "     metadata:\n"
        "+      annotations:\n"
        "+        checksum/config: 89abcde\n"
        "+        checksum/secret: 1234567\n"
        "       labels:\n"
        "         app: app\n"
    )

    filtered_diff = filter_metadata_hunks(
        raw_diff, {("metadata", "annotations", "noa.re/deploy-id")}
    )

    assert filtered_diff == ""


def test_manifest_diff_leaves_a_checksum_out_of_the_metadata_summary(
    tmp_path: Path,
) -> None:
    repo = porcelain.init(str(tmp_path))
    manifest = (
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: {name}\n"
        "  annotations:\n"
        "    checksum/config: {checksum}\n"
        "data:\n"
        "  greeting: {greeting}\n"
    )
    files = {
        f"{name}.yaml": manifest.format(name=name, checksum=checksum, greeting=greeting)
        for name, checksum, greeting in (("one", "aaa", "hi"), ("two", "aaa", "hi"))
    }
    first = _commit(repo, files, "one")
    second = _commit(
        repo,
        {name: content.replace("aaa", "bbb") for name, content in files.items()},
        "two",
    )

    result = _diff(repo, first, second)

    assert result.summary == ""
    assert result.filtered_diff == ""


def test_manifest_diff_labels_hunk_headers_with_the_enclosing_key(
    tmp_path: Path,
) -> None:
    repo = porcelain.init(str(tmp_path))
    first = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.0", deploy_id="old")},
        "one",
    )
    second = _commit(
        repo,
        {"deployment.yaml": DEPLOYMENT.format(version="v1.8.0", deploy_id="new")},
        "two",
    )

    result = _diff(repo, first, second)

    # The same header `git diff` writes for this change, which is what lets the
    # metadata filtering see that the changed line lives under metadata.
    assert "@@ -5,7 +5,7 @@ metadata:" in result.diff


def test_filter_metadata_hunks_handles_metadata_context_headers() -> None:
    raw_diff = (
        "diff --git a/deployment.yaml b/deployment.yaml\n"
        "index 1234567..89abcde 100644\n"
        "--- a/deployment.yaml\n"
        "+++ b/deployment.yaml\n"
        "@@ -8,7 +8,9 @@ metadata:\n"
        "     control-plane: envoy-gateway\n"
        "     app.kubernetes.io/name: gateway-helm\n"
        "     app.kubernetes.io/instance: envoy-gateway\n"
        "-    app.kubernetes.io/version: v1.8.0\n"
        "+    app.kubernetes.io/version: v1.8.1\n"
        "+  annotations:\n"
        "+    noa.re/deploy-id: generated\n"
        " spec:\n"
        "   replicas: 1\n"
    )

    filtered_diff = filter_metadata_hunks(
        raw_diff,
        {
            ("metadata", "labels", "app.kubernetes.io/version"),
            ("metadata", "annotations", "noa.re/deploy-id"),
        },
    )

    assert filtered_diff == ""


def test_filter_metadata_hunks_treats_null_annotations_as_absent() -> None:
    raw_diff = (
        "diff --git a/apiservice.yaml b/apiservice.yaml\n"
        "index 1234567..89abcde 100644\n"
        "--- a/apiservice.yaml\n"
        "+++ b/apiservice.yaml\n"
        "@@ -6,8 +6,9 @@ metadata:\n"
        "   labels:\n"
        "     app.kubernetes.io/name: metrics-server\n"
        "     app.kubernetes.io/instance: metrics-server\n"
        "-    app.kubernetes.io/version: 0.8.0\n"
        "-  annotations: null\n"
        "+    app.kubernetes.io/version: 0.8.1\n"
        "+  annotations:\n"
        "+    noa.re/deploy-id: generated\n"
        " spec:\n"
        "   group: metrics.k8s.io\n"
    )

    filtered_diff = filter_metadata_hunks(
        raw_diff,
        {
            ("metadata", "labels", "app.kubernetes.io/version"),
            ("metadata", "annotations", "noa.re/deploy-id"),
        },
    )

    assert filtered_diff == ""


def test_build_comment_body_includes_metadata_summary_and_filtered_diff() -> None:
    comment = build_comment_body(
        [
            DiffSection(
                heading=None,
                diff=ManifestDiff(
                    stat="deployment.yaml | 4 ++--\n",
                    diff="diff --git a/deployment.yaml b/deployment.yaml\n-noise\n+noise",
                    summary=(
                        "- Label `app.kubernetes.io/version` changed from `v1` to "
                        "`v2` on 2 manifests."
                    ),
                    filtered_diff=(
                        "diff --git a/deployment.yaml b/deployment.yaml\n-old\n+new"
                    ),
                ),
            )
        ],
        full_diff_reference="returned in the response",
    )

    assert "Metadata changes:" in comment.body
    assert "changed from `v1` to `v2` on 2 manifests" in comment.body
    assert (
        "Repeated metadata-only changes have been summarized or omitted" in comment.body
    )
    assert "-noise" not in comment.body
    assert "+new" in comment.body
    assert comment.omitted_context_diff


def test_build_comment_body_reports_the_manifest_builder_version() -> None:
    comment = build_comment_body(
        [
            DiffSection(
                heading=None,
                diff=ManifestDiff(
                    stat="file.yaml | 1 +\n", diff="```diff\n+hello\n```"
                ),
            )
        ],
        full_diff_reference="returned in the response",
    )

    assert "file.yaml | 1 +" in comment.body
    assert "````diff\n```diff\n+hello\n```\n````" in comment.body
    assert f"manifest-builder version: `{MANIFEST_BUILDER_VERSION}`" in comment.body
    assert not comment.omitted_context_diff


def test_build_comment_body_preserves_stat_leading_alignment() -> None:
    comment = build_comment_body(
        [
            DiffSection(
                heading=None,
                diff=ManifestDiff(
                    stat=" file.yaml | 1 +\n 1 file changed, 1 insertion(+)\n",
                    diff="diff",
                ),
            )
        ],
        full_diff_reference="returned in the response",
    )

    assert comment.body.startswith(
        "```\n file.yaml | 1 +\n 1 file changed, 1 insertion(+)\n```"
    )


def test_build_comment_body_omits_context_diff_when_too_large() -> None:
    comment = build_comment_body(
        [
            DiffSection(
                heading=None,
                diff=ManifestDiff(
                    stat="file.yaml | 1 +\n", diff="x" * MAX_COMMENT_CHARS
                ),
            )
        ],
        full_diff_reference="returned in the relcoord response",
    )

    assert "file.yaml | 1 +" in comment.body
    assert "```diff" not in comment.body
    assert "too large for a GitHub comment" in comment.body
    assert "returned in the relcoord response" in comment.body
    assert comment.omitted_context_diff


def test_build_comment_body_renders_a_heading_per_repository() -> None:
    comment = build_comment_body(
        [
            DiffSection(
                heading="https://github.com/acme/manifests",
                diff=ManifestDiff(stat="dev.yaml | 1 +\n", diff="+dev"),
            ),
            DiffSection(
                heading="https://github.com/acme/other-manifests",
                diff=ManifestDiff(stat="", diff=""),
            ),
        ],
        full_diff_reference="returned in the response",
    )

    assert "### https://github.com/acme/manifests" in comment.body
    assert "### https://github.com/acme/other-manifests" in comment.body
    assert (
        comment.body.count(
            "The generated output is the same before and after this change"
        )
        == 1
    )
    assert "+dev" in comment.body


def test_markdown_fence_outgrows_the_backticks_it_has_to_hold() -> None:
    assert markdown_fence("plain") == "```"
    assert markdown_fence("a ``` fence") == "````"
    assert markdown_fence("a ````` fence") == "``````"


def test_build_comment_body_carries_the_marker_as_a_hidden_first_line() -> None:
    comment = build_comment_body(
        [DiffSection(heading=None, diff=ManifestDiff(stat="", diff=""))],
        full_diff_reference="unused",
        marker=comment_marker(),
    )

    assert comment.body.startswith("<!-- relcoord:manifest-diff -->\n")
