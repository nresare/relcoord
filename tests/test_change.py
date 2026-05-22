# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
import subprocess
from pathlib import Path

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
    ) -> set[Path]:
        calls.append(
            (
                "generate",
                deploy_config.name,
                manifests_checkout.name,
                repo_root,
                create_commit,
                image,
            )
        )
        return {manifests_checkout / "api.yaml", manifests_checkout / "worker.yaml"}

    def fake_run_git(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(("git", args, kwargs["cwd"].name, kwargs.get("env")))
        stdout = "feedface\n" if args == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout)

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_run_git", fake_run_git)

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
        ),
        ("git", ["rev-parse", "HEAD"], "manifests", None),
        ("git", ["push"], "manifests", None),
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
