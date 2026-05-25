# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
from pathlib import Path

from dulwich import porcelain
import pytest

from relcoord import change
from relcoord.change import ChangeProcessor, DeployConfigError


def test_change_processor_checks_out_deploy_config_generates_commit_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
    ) -> set[Path]:
        calls.append(
            (
                "generate",
                deploy_config.name,
                manifests_checkout.name,
                repo_root,
                create_commit,
                image,
                namespace,
            )
        )
        return {manifests_checkout / "api.yaml", manifests_checkout / "worker.yaml"}

    def fake_head_commit(repo_path: Path) -> str:
        calls.append(("head", repo_path.name))
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append(("push", repo_path.name, remote, idcat))

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    with caplog.at_level(logging.INFO, logger="relcoord.change"):
        result = ChangeProcessor("https://github.com/acme/manifests.git").process(
            "https://github.com/acme/config.git",
            "deadbeef",
            "registry.example.com/team/api:1.2.3",
        )

    assert result.repo == "https://github.com/acme/config.git"
    assert result.commit == "deadbeef"
    assert result.deploy_config == tmp_path / "source" / ".deploy"
    assert result.manifests_checkout == tmp_path / "manifests"
    assert result.generated_count == 2
    assert calls == [
        (
            "checkout",
            "https://github.com/acme/config.git",
            "deadbeef",
            "source",
            None,
        ),
        (
            "clone",
            "https://github.com/acme/manifests.git",
            "manifests",
            None,
            {"depth": "1"},
        ),
        (
            "generate",
            ".deploy",
            "manifests",
            Path("/"),
            True,
            "registry.example.com/team/api:1.2.3",
            "config",
        ),
        ("head", "manifests"),
        ("push", "manifests", "https://github.com/acme/manifests.git", None),
    ]
    assert (
        "change step 2/7: checking out source repo https://github.com/acme/config.git "
        "at commit deadbeef"
    ) in caplog.text
    assert "change step 5/7: invoking manifest-builder" in caplog.text
    assert (
        "change step 6/7: manifest-builder created manifests commit feedface"
        in caplog.text
    )
    assert (
        "change step 7/7: pushing manifests commit feedface to "
        "https://github.com/acme/manifests.git"
    ) in caplog.text


def test_change_processor_requires_top_level_deploy_directory(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        target.mkdir(parents=True)

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)

    processor = ChangeProcessor("https://github.com/acme/manifests.git")

    try:
        processor.process("https://github.com/acme/config.git", "deadbeef", None)
    except DeployConfigError as exc:
        assert "does not contain a top-level .deploy directory" in str(exc)
    else:
        raise AssertionError("expected DeployConfigError")


@pytest.mark.parametrize(
    ("repo", "namespace"),
    [
        ("https://github.com/acme/config.git", "config"),
        ("https://github.com/acme/config", "config"),
        ("acme/config.git", "config"),
    ],
)
def test_namespace_from_repo(repo: str, namespace: str) -> None:
    assert change._namespace_from_repo(repo) == namespace


def test_checkout_commit_materializes_requested_commit(tmp_path: Path) -> None:
    source = tmp_path / "source-repo"
    repo = porcelain.init(source)
    try:
        (source / "README.md").write_text("first\n")
        porcelain.add(repo, b"README.md")
        first_commit = porcelain.commit(
            repo,
            message=b"first",
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
        )

        (source / ".deploy").mkdir()
        (source / ".deploy" / "api.yaml").write_text("image: example\n")
        porcelain.add(repo, [b".deploy/api.yaml"])
        porcelain.commit(
            repo,
            message=b"second",
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
        )
    finally:
        repo.close()

    checkout = tmp_path / "checkout"
    change._checkout_commit(str(source), first_commit.decode("ascii"), checkout, None)

    assert (checkout / "README.md").read_text() == "first\n"
    assert not (checkout / ".deploy").exists()
