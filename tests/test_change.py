# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.errors import NotGitRepository
from manifest_builder import ExternalPlugins

from relcoord import change
from relcoord.change import (
    ChangeProcessingError,
    ChangeProcessor,
    ChangeProgress,
    CredentialError,
    DeployConfigError,
    DeploymentDetectionError,
    GitTransportError,
    RolloutStageError,
    SystemRepositoryError,
)
from relcoord.config import OutputSettings, RolloutSettings, RolloutStage
from relcoord.git import GitCredentialError

SYSTEM_REPO = "https://github.com/acme/shared-system.git"


@dataclass(frozen=True)
class Ref:
    kind: str
    namespace: str | None
    name: str
    api_version: str = "v1"


@dataclass(frozen=True)
class GenerationResult:
    written_paths: set[Path]
    created_or_modified: set[Ref]
    removed: set[Ref]
    deploy_id: str | None


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
        vars: dict[str, object],
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        calls.append(
            (
                "generate",
                deploy_config.name,
                manifests_checkout.name,
                repo_root,
                create_commit,
                image,
                namespace,
                vars,
            )
        )
        return GenerationResult(
            written_paths={
                manifests_checkout / "api.yaml",
                manifests_checkout / "worker.yaml",
            },
            created_or_modified={
                Ref(kind="Deployment", namespace="config", name="api")
            },
            removed=set(),
            deploy_id="0123456789abcdef",
        )

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
            {
                "purpose": (
                    "cloning manifests repo https://github.com/acme/manifests.git"
                ),
                "depth": "1",
            },
        ),
        (
            "generate",
            ".deploy",
            "manifests",
            Path("/"),
            True,
            "registry.example.com/team/api:1.2.3",
            "config",
            {},
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


def test_change_processor_reports_progress_for_each_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        vars: dict[str, object],
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified={
                Ref(kind="Deployment", namespace="config", name="api")
            },
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(
        change, "_push_repository", lambda repo_path, remote, idcat: None
    )

    events: list[ChangeProgress] = []
    ChangeProcessor("https://github.com/acme/manifests.git").process(
        "https://github.com/acme/config.git",
        "deadbeef",
        "registry.example.com/team/api:1.2.3",
        progress=events.append,
    )

    # The temporary workspace and the manifests commit are logged rather than
    # streamed: one is a path nobody watching a deployment cares about, and the
    # other is named by the push lines either side of it.
    assert [event.phase for event in events] == [
        "source-checkout",
        "deploy-config",
        "manifests-checkout",
        "generate",
        "generated",
        "changed-objects",
        "push",
        "pushed",
    ]
    by_phase = {event.phase: event for event in events}
    assert by_phase["changed-objects"].detail == {
        "output": "manifests",
        "repository": "https://github.com/acme/manifests.git",
        "cluster": None,
        "deploy_id": "0123456789abcdef",
        "created_or_modified": [
            {"kind": "Deployment", "namespace": "config", "name": "api"}
        ],
        "removed": [],
    }
    assert by_phase["changed-objects"].message == (
        "manifests: updated Deployment config/api"
    )
    assert by_phase["source-checkout"].detail == {
        "repo": "https://github.com/acme/config.git",
        "commit": "deadbeef",
    }
    assert by_phase["source-checkout"].message == "checking out acme/config at deadbee"
    assert by_phase["deploy-config"].detail["namespace"] == "config"
    assert by_phase["deploy-config"].detail["system"] is False
    assert by_phase["deploy-config"].message == (
        "generating for namespace config from .deploy"
    )
    assert by_phase["generated"].detail["generated"] == 1
    assert "paths" not in by_phase["generated"].detail
    assert by_phase["generated"].message == "manifests: 1 of 1 manifests changed"
    assert by_phase["push"].detail == {
        "repository": "https://github.com/acme/manifests.git",
        "manifest_commit": "feedface",
    }
    assert by_phase["push"].message == "pushing feedfac to acme/manifests"


def test_change_processor_reports_no_changes_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        vars: dict[str, object],
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            written_paths=set(),
            created_or_modified=set(),
            removed=set(),
            deploy_id=None,
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)

    events: list[ChangeProgress] = []
    ChangeProcessor("https://github.com/acme/manifests.git").process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
        progress=events.append,
    )

    assert [event.phase for event in events][-1] == "no-changes"
    assert events[-1].message == "no changes for acme/manifests"


def test_change_processor_generates_configured_outputs_with_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    outputs = [
        OutputSettings(
            name="example-dev",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-dev"),
            vars={
                "cluster_name": "example-dev",
                "account_id": 111122223333,
            },
        ),
        OutputSettings(
            name="example-prod",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-prod"),
            vars={
                "cluster_name": "example-prod",
                "account_id": 444455556666,
            },
        ),
    ]

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        vars: dict[str, object],
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        calls.append(
            (
                "generate",
                deploy_config.name,
                output_path.relative_to(tmp_path / "manifests"),
                repo_root,
                create_commit,
                image,
                namespace,
                vars,
            )
        )
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified={
                Ref(kind="Deployment", namespace="config", name="api")
            },
            removed=set(),
            deploy_id="0123456789abcdef",
        )

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

    result = ChangeProcessor(outputs=outputs).process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
    )

    assert result.generated_count == 2
    assert [output.name for output in result.outputs] == [
        "example-dev",
        "example-prod",
    ]
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
            {
                "purpose": (
                    "cloning manifests repo https://github.com/acme/manifests.git"
                ),
                "depth": "1",
            },
        ),
        (
            "generate",
            ".deploy",
            Path("example-dev"),
            Path("/"),
            True,
            None,
            "config",
            {"cluster_name": "example-dev", "account_id": 111122223333},
        ),
        (
            "generate",
            ".deploy",
            Path("example-prod"),
            Path("/"),
            True,
            None,
            "config",
            {"cluster_name": "example-prod", "account_id": 444455556666},
        ),
        ("head", "manifests"),
        ("push", "manifests", "https://github.com/acme/manifests.git", None),
    ]


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        pytest.param(None, False, id="no-config-file"),
        pytest.param('[simple.api]\nimage = "api:1"\n', False, id="blocks"),
        pytest.param('version = 1\n[simple.api]\nimage = "api:1"\n', False, id="v1"),
        pytest.param('version = 2\n\n[[target]]\nname = "dev"\n', True, id="v2"),
    ],
)
def test_declares_targets_reads_the_top_level_config_version(
    tmp_path: Path, contents: str | None, expected: bool
) -> None:
    if contents is not None:
        (tmp_path / "config.toml").write_text(contents)

    assert change._declares_targets(tmp_path) is expected


def test_declares_targets_reads_a_manifest_builder_toml(tmp_path: Path) -> None:
    (tmp_path / "manifest-builder.toml").write_text(
        'version = 2\n\n[[target]]\nname = "dev"\n'
    )

    assert change._declares_targets(tmp_path) is True


def test_change_processor_generates_targets_for_a_version_2_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    outputs = [
        # The vars stay configured, since the same output also serves config
        # repositories that declare config blocks directly.
        OutputSettings(
            name="example-dev",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-dev"),
            vars={"cluster_name": "example-dev"},
        ),
        OutputSettings(
            name="example-prod",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-prod"),
            vars={"cluster_name": "example-prod"},
            target="prod",
        ),
    ]

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        deploy_config = target / ".deploy"
        deploy_config.mkdir(parents=True)
        (deploy_config / "config.toml").write_text(
            'version = 2\n\n[[target]]\nname = "example-dev"\n'
        )

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        target: str,
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        calls.append(("generate", output_path.name, target))
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified={
                Ref(kind="Deployment", namespace="config", name="api")
            },
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *args, **kwargs: None)

    events: list[ChangeProgress] = []
    result = ChangeProcessor(outputs=outputs).process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
        progress=events.append,
    )

    assert result.generated_count == 2
    # An output that names no target generates the one sharing its name.
    assert calls == [
        ("generate", "example-dev", "example-dev"),
        ("generate", "example-prod", "prod"),
    ]
    by_phase = {event.phase: event for event in events}
    assert by_phase["deploy-config"].detail["targets"] is True
    assert by_phase["generate"].detail["target"] == "prod"


def test_change_processor_skips_commit_and_push_when_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        manifests_checkout = args[1]
        # manifest-builder regenerated identical output: files are written but
        # there is nothing to commit, so the change sets are empty.
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=set(),
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    def fake_head_commit(repo_path: Path) -> str:
        calls.append("head")
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append("push")

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
            None,
        )

    assert calls == []
    assert result.generated_count == 1
    assert "nothing to commit or push" in caplog.text


def test_change_processor_skips_detection_when_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Detector:
        def __init__(self) -> None:
            self.called = threading.Event()

        def wait_for_success(self, **kwargs) -> None:
            self.called.set()

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        manifests_checkout = args[1]
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=set(),
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *a, **k: None)

    detector = Detector()
    ChangeProcessor(
        "https://github.com/acme/manifests.git",
        detect_deployment=True,
        deployment_detector=detector,
    ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert not detector.called.wait(timeout=0.2)


def test_change_processor_detects_deployment_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    created = {Ref(kind="Deployment", namespace="config", name="api")}
    removed = {Ref(kind="ConfigMap", namespace="config", name="old-api")}

    class Detector:
        def __init__(self) -> None:
            self.called = threading.Event()
            self.kwargs: dict[str, object] | None = None

        def wait_for_success(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.called.set()

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        calls.append(("generate", args[0].name, args[1].name))
        manifests_checkout = args[1]
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=created,
            removed=removed,
            deploy_id="0123456789abcdef",
        )

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

    detector = Detector()
    result = ChangeProcessor(
        "https://github.com/acme/manifests.git",
        detect_deployment=True,
        deployment_detector=detector,
    ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert result.generated_count == 1
    assert result.deploy_id == "0123456789abcdef"
    assert calls[-1] == (
        "push",
        "manifests",
        "https://github.com/acme/manifests.git",
        None,
    )
    assert detector.called.wait(timeout=1)
    assert detector.kwargs == {
        "deploy_id": "0123456789abcdef",
        "created_or_modified": created,
        "removed": removed,
    }


def test_change_processor_requires_deploy_id_for_deployment_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        manifests_checkout = args[1]
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=set(),
            removed=set(),
            deploy_id=None,
        )

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append("push")

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    with pytest.raises(DeploymentDetectionError, match="did not return a deploy_id"):
        ChangeProcessor(
            "https://github.com/acme/manifests.git",
            detect_deployment=True,
        ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert calls == []


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
        assert "does not contain a .deploy directory" in str(exc)
    else:
        raise AssertionError("expected DeployConfigError")


def test_change_processor_uses_custom_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy_configs: list[Path] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / "deploy" / "system").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        **kwargs,
    ) -> GenerationResult:
        deploy_configs.append(deploy_config)
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified={
                Ref(kind="Deployment", namespace="system", name="api")
            },
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    def fake_head_commit(repo_path: Path) -> str:
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        pass

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    processor = ChangeProcessor("https://github.com/acme/manifests.git")
    processor.process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
        config_path="deploy/system",
    )

    assert deploy_configs == [tmp_path / "source" / "deploy" / "system"]


def test_change_processor_system_mode_uses_root_and_no_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        # System config lives at the repository root; no .deploy directory.
        target.mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str | None,
        vars: dict[str, object],
        plugins: ExternalPlugins | None = None,
    ) -> GenerationResult:
        captured["deploy_config"] = deploy_config
        captured["namespace"] = namespace
        captured["image"] = image
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified={Ref(kind="Namespace", namespace=None, name="argo")},
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    def fake_head_commit(repo_path: Path) -> str:
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        pass

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    ChangeProcessor("https://github.com/acme/manifests.git").process(
        "https://github.com/acme/system.git",
        "deadbeef",
        None,
        system=True,
    )

    assert captured["deploy_config"] == tmp_path / "source"
    assert captured["namespace"] is None
    assert captured["image"] is None


def _system_repository_change_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    *,
    clones: list[tuple[str, Path, dict[str, object]]] | None = None,
    plugins_dir: bool = True,
) -> None:
    """Fake out the git and generate calls, keeping the system plugins.

    The system repository is the one clone whose contents matter here, so its
    checkout gets a plugins directory unless a test is about that directory
    being missing.
    """

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        if clones is not None:
            clones.append((repo, target, kwargs))
        target.mkdir(parents=True)
        if repo == SYSTEM_REPO and plugins_dir:
            (target / "plugins").mkdir()

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        **kwargs,
    ) -> GenerationResult:
        captured["plugins"] = kwargs.get("plugins")
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified={Ref(kind="Deployment", namespace="c", name="api")},
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *args: None)


def test_change_checks_out_the_system_repository_and_passes_plugins_to_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    clones: list[tuple[str, Path, dict[str, object]]] = []
    _system_repository_change_fakes(tmp_path, monkeypatch, captured, clones=clones)
    events: list[ChangeProgress] = []

    ChangeProcessor(
        "https://github.com/acme/manifests.git",
        system_repository=SYSTEM_REPO,
    ).process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
        progress=events.append,
    )

    assert captured["plugins"] == ExternalPlugins(
        path=tmp_path / "system" / "plugins",
        source=f"{SYSTEM_REPO}@feedface",
    )
    system_clone = [clone for clone in clones if clone[0] == SYSTEM_REPO]
    assert system_clone == [
        (
            SYSTEM_REPO,
            tmp_path / "system",
            {
                "purpose": f"cloning system repo {SYSTEM_REPO}",
                "depth": "1",
                "branch": "main",
            },
        )
    ]
    checkout = [event for event in events if event.phase == "system-checkout"]
    assert [event.detail["source"] for event in checkout] == [f"{SYSTEM_REPO}@feedface"]


def test_change_leaves_the_system_repository_out_of_system_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    clones: list[tuple[str, Path, dict[str, object]]] = []

    # System config lives at the repository root, and carries its own plugins.
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        target.mkdir(parents=True)

    _system_repository_change_fakes(tmp_path, monkeypatch, captured, clones=clones)
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)

    ChangeProcessor(
        "https://github.com/acme/manifests.git",
        system_repository=SYSTEM_REPO,
    ).process(
        "https://github.com/acme/system.git",
        "deadbeef",
        None,
        system=True,
    )

    assert captured["plugins"] is None
    assert [clone[0] for clone in clones if clone[0] == SYSTEM_REPO] == []


def test_change_passes_no_plugins_without_a_configured_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    _system_repository_change_fakes(tmp_path, monkeypatch, captured)

    ChangeProcessor("https://github.com/acme/manifests.git").process(
        "https://github.com/acme/config.git", "deadbeef", None
    )

    assert captured["plugins"] is None


def test_change_rejects_a_system_repository_without_a_plugins_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    _system_repository_change_fakes(tmp_path, monkeypatch, captured, plugins_dir=False)

    processor = ChangeProcessor(
        "https://github.com/acme/manifests.git",
        system_repository=SYSTEM_REPO,
    )
    with pytest.raises(SystemRepositoryError) as excinfo:
        processor.process("https://github.com/acme/config.git", "deadbeef", None)

    assert str(excinfo.value) == (
        f"system repo {SYSTEM_REPO} at commit feedface has no plugins/ directory"
    )


def test_dulwich_error_message_includes_action_and_parameters() -> None:
    # NotGitRepository (raised for missing/private/inaccessible repos) has an
    # empty string representation, so the message must surface its type instead.
    message = change._dulwich_error_message(
        "clone",
        {"remote": "https://github.com/acme/system", "target": "/tmp/source"},
        NotGitRepository(),
        BytesIO(),
    )

    assert message == (
        "dulwich clone failed "
        "(remote=https://github.com/acme/system, target=/tmp/source): "
        "dulwich.errors.NotGitRepository"
    )


def test_dulwich_error_message_prefers_stderr() -> None:
    errstream = BytesIO(b"fatal: repository not found\n")

    message = change._dulwich_error_message(
        "clone",
        {"remote": "https://github.com/acme/system"},
        NotGitRepository(),
        errstream,
    )

    assert message == (
        "dulwich clone failed (remote=https://github.com/acme/system): "
        "fatal: repository not found"
    )


def test_dulwich_error_message_without_errstream_uses_exception() -> None:
    message = change._dulwich_error_message(
        "checkout",
        {"target": "/tmp/source", "commit": "main"},
        ValueError("ref main not found"),
    )

    assert message == (
        "dulwich checkout failed (target=/tmp/source, commit=main): ref main not found"
    )


def test_clone_repository_failure_reports_remote_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_clone(*args: object, **kwargs: object) -> None:
        raise NotGitRepository()

    monkeypatch.setattr(change.porcelain, "clone", fake_clone)
    target = tmp_path / "source"

    with pytest.raises(GitTransportError) as excinfo:
        change._clone_repository(
            "https://github.com/acme/system",
            target,
            None,
            purpose="checking out source repo",
        )

    message = str(excinfo.value)
    assert message == (
        "dulwich clone failed "
        f"(remote=https://github.com/acme/system, target={target}): "
        "dulwich.errors.NotGitRepository"
    )


def test_dulwich_checkout_failure_raises_git_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_reset(*args: object, **kwargs: object) -> None:
        raise ValueError("ref main not found")

    monkeypatch.setattr(change.porcelain, "reset", fake_reset)
    target = tmp_path / "source"

    with pytest.raises(GitTransportError) as excinfo:
        change._dulwich_checkout(target, "main")

    message = str(excinfo.value)
    assert message == (
        f"dulwich checkout failed (target={target}, commit=main): ref main not found"
    )


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


def test_credentials_for_wraps_git_credential_error_with_operation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_github_https_credentials(source: str, idcat) -> None:
        raise GitCredentialError("idcat returned HTTP 401: not allowed")

    monkeypatch.setattr(
        change, "github_https_credentials", fake_github_https_credentials
    )

    with pytest.raises(CredentialError) as excinfo:
        change._credentials_for(
            "https://github.com/acme/system.git",
            None,
            "checking out source repo https://github.com/acme/system.git",
        )

    message = str(excinfo.value)
    assert "checking out source repo https://github.com/acme/system.git" in message
    assert "idcat returned HTTP 401: not allowed" in message
    assert isinstance(excinfo.value.__cause__, GitCredentialError)


def test_change_processor_reports_the_objects_a_change_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = {
        Ref(kind="Deployment", namespace="config", name="api"),
        Ref(kind="Namespace", namespace=None, name="config"),
    }
    removed = {Ref(kind="ConfigMap", namespace="config", name="old-api")}

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        return GenerationResult(
            written_paths={args[1] / "api.yaml"},
            created_or_modified=created,
            removed=removed,
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *a, **k: None)

    result = ChangeProcessor(
        outputs=[
            OutputSettings(
                name="example-dev",
                repository="https://github.com/acme/manifests.git",
                directory=Path("example-dev"),
                connection_type="eks",
                api_endpoint="https://example.eks.amazonaws.com",
                ca_path=Path("/ca.pem"),
            )
        ],
    ).process("https://github.com/acme/config.git", "deadbeef", None)

    output = result.outputs[0]
    assert output.cluster == "example-dev"
    assert output.deploy_id == "0123456789abcdef"
    # Sorted by kind, then namespace, then name, so the report is stable.
    assert output.created_or_modified == (
        Ref(kind="Deployment", namespace="config", name="api"),
        Ref(kind="Namespace", namespace=None, name="config"),
    )
    assert output.removed == (
        Ref(kind="ConfigMap", namespace="config", name="old-api"),
    )


def test_change_processor_detects_deployment_in_the_output_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = {Ref(kind="Deployment", namespace="config", name="api")}
    detected = threading.Event()
    observed: dict[str, object] = {}

    class Detector:
        def __init__(self, output: OutputSettings) -> None:
            self.output = output

        def wait_for_success(self, **kwargs) -> None:
            observed.update(kwargs)
            observed["cluster"] = self.output.name
            detected.set()

        def close(self) -> None:
            observed["closed"] = True

    def fake_for_output(output: OutputSettings, **kwargs) -> Detector:
        return Detector(output)

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        return GenerationResult(
            written_paths={args[1] / "api.yaml"},
            created_or_modified=created,
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *a, **k: None)
    monkeypatch.setattr(
        change.KubernetesDeploymentDetector, "for_output", fake_for_output
    )

    ChangeProcessor(
        outputs=[
            OutputSettings(
                name="example-dev",
                repository="https://github.com/acme/manifests.git",
                directory=Path("example-dev"),
                connection_type="eks",
                api_endpoint="https://example.eks.amazonaws.com",
                ca_path=Path("/ca.pem"),
            )
        ],
        detect_deployment=True,
    ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert detected.wait(timeout=1)
    assert observed["cluster"] == "example-dev"
    assert observed["deploy_id"] == "0123456789abcdef"
    assert observed["created_or_modified"] == created


def test_change_processor_rejects_detection_without_a_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        return GenerationResult(
            written_paths={args[1] / "api.yaml"},
            created_or_modified={Ref(kind="Deployment", namespace="c", name="api")},
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", lambda *a, **k: None)

    with pytest.raises(DeploymentDetectionError, match="has no connection-type"):
        ChangeProcessor(
            "https://github.com/acme/manifests.git",
            detect_deployment=True,
        ).process("https://github.com/acme/config.git", "deadbeef", None)


ROLLOUT_REPOSITORY = "https://github.com/acme/manifests.git"

ROLLOUT_OUTPUTS = [
    OutputSettings(
        name="platform-dev",
        repository=ROLLOUT_REPOSITORY,
        directory=Path("platform-dev"),
    ),
    OutputSettings(
        name="platform-prod",
        repository=ROLLOUT_REPOSITORY,
        directory=Path("platform-prod"),
    ),
    OutputSettings(
        name="observability",
        repository=ROLLOUT_REPOSITORY,
        directory=Path("observability"),
    ),
]

# platform-dev first, then the two outputs that wait for it.
LINEAR_ROLLOUT = RolloutSettings(
    name="linear",
    stages=(
        RolloutStage(outputs=("platform-dev",)),
        RolloutStage(outputs=("platform-prod", "observability")),
    ),
)


def _staged_change_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: set[str],
    failing: frozenset[str] = frozenset(),
) -> tuple[ChangeProcessor, list[str]]:
    """A processor whose outputs generate changes only where ``changed`` says.

    The recorded calls are what a rollout is about: which outputs were generated
    and pushed, and in what order that interleaved with waiting for the
    deployments to be observed. Each output's deploy-id names it, so a wait says
    which output it was waiting for.
    """
    calls: list[str] = []

    class Detector:
        def wait_for_success(
            self, *, deploy_id: str, created_or_modified: set, removed: set
        ) -> None:
            calls.append(f"verify:{deploy_id}")
            if deploy_id in failing:
                raise RuntimeError(f"{deploy_id} never reached the cluster")

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append("clone")
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path, output_path: Path, **kwargs
    ) -> GenerationResult:
        name = output_path.name
        calls.append(f"generate:{name}")
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified=(
                {Ref(kind="Deployment", namespace="config", name=name)}
                if name in changed
                else set()
            ),
            removed=set(),
            deploy_id=f"deploy-{name}",
        )

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append("push")

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    processor = ChangeProcessor(
        outputs=ROLLOUT_OUTPUTS,
        rollouts=[LINEAR_ROLLOUT],
        detect_deployment=True,
        deployment_detector=Detector(),
    )
    return processor, calls


def test_rollout_waits_for_each_stage_before_deploying_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, calls = _staged_change_processor(
        tmp_path,
        monkeypatch,
        changed={"platform-dev", "platform-prod", "observability"},
    )

    result = processor.process("https://github.com/acme/config.git", "deadbeef", None)

    assert calls == [
        "clone",
        "generate:platform-dev",
        "push",
        "verify:deploy-platform-dev",
        "generate:platform-prod",
        "generate:observability",
        "push",
        "verify:deploy-platform-prod",
        "verify:deploy-observability",
    ]
    assert [
        (output.name, output.rollout, output.stage) for output in result.outputs
    ] == [
        ("platform-dev", "linear", 1),
        ("platform-prod", "linear", 2),
        ("observability", "linear", 2),
    ]


def test_rollout_skips_a_stage_whose_outputs_the_change_does_not_affect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, calls = _staged_change_processor(
        tmp_path, monkeypatch, changed={"observability"}
    )

    events: list[ChangeProgress] = []
    processor.process(
        "https://github.com/acme/config.git", "deadbeef", None, progress=events.append
    )

    verified = [event for event in events if event.phase == "rollout-stage-verified"]
    assert verified[0].message == "stage 1 of 2 skipped: platform-dev unaffected"
    assert calls == [
        "clone",
        "generate:platform-dev",
        "generate:platform-prod",
        "generate:observability",
        "push",
        "verify:deploy-observability",
    ]


def test_rollout_stops_at_a_stage_whose_deployment_is_not_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, calls = _staged_change_processor(
        tmp_path,
        monkeypatch,
        changed={"platform-dev", "platform-prod", "observability"},
        failing=frozenset({"deploy-platform-dev"}),
    )

    with pytest.raises(
        RolloutStageError,
        match="deployment of manifest-builder deploy-id deploy-platform-dev "
        "was not observed: deploy-platform-dev never reached the cluster",
    ):
        processor.process("https://github.com/acme/config.git", "deadbeef", None)

    assert calls == [
        "clone",
        "generate:platform-dev",
        "push",
        "verify:deploy-platform-dev",
    ]


def test_rollout_reports_progress_for_each_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _ = _staged_change_processor(
        tmp_path, monkeypatch, changed={"platform-dev", "observability"}
    )

    events: list[ChangeProgress] = []
    processor.process(
        "https://github.com/acme/config.git", "deadbeef", None, progress=events.append
    )

    assert [event.phase for event in events] == [
        "source-checkout",
        "deploy-config",
        "rollout-stage",
        "manifests-checkout",
        "generate",
        "generated",
        "changed-objects",
        "push",
        "pushed",
        "deployment-detection",
        "rollout-stage-verified",
        "rollout-stage",
        "generate",
        "generated",
        "generate",
        "generated",
        "changed-objects",
        "push",
        "pushed",
        "deployment-detection",
        "rollout-stage-verified",
    ]
    stages = [event for event in events if event.phase == "rollout-stage"]
    assert [event.detail for event in stages] == [
        {
            "rollout": "linear",
            "stage": 1,
            "stages": 2,
            "outputs": ["platform-dev"],
        },
        {
            "rollout": "linear",
            "stage": 2,
            "stages": 2,
            "outputs": ["platform-prod", "observability"],
        },
    ]
    assert [event.message for event in stages] == [
        "rollout linear, stage 1 of 2: platform-dev",
        "rollout linear, stage 2 of 2: platform-prod and observability",
    ]
    by_phase = {event.phase: event for event in events}
    assert by_phase["deployment-detection"].message == (
        "waiting for observability to pick up the change"
    )
    # How long the deployment took is what a reader is waiting to hear, so the
    # message carries it; the value itself is whatever the clock said.
    verified = [event for event in events if event.phase == "rollout-stage-verified"]
    assert re.fullmatch(
        r"stage 1 of 2 verified: platform-dev picked it up after \d+\.\d+s",
        verified[0].message,
    )
    assert re.fullmatch(
        r"stage 2 of 2 verified: observability picked it up after \d+\.\d+s",
        verified[1].message,
    )
    assert verified[1].detail["outputs"] == ["observability"]


def test_change_stages_without_rollouts_deploy_every_output_at_once() -> None:
    stages = change._change_stages(ROLLOUT_OUTPUTS, ())

    assert len(stages) == 1
    assert stages[0].rollout is None
    assert [output.name for output in stages[0].outputs] == [
        "platform-dev",
        "platform-prod",
        "observability",
    ]


def test_change_stages_reject_an_unconfigured_output() -> None:
    rollout = RolloutSettings(
        name="linear", stages=(RolloutStage(outputs=("platform-staging",)),)
    )

    with pytest.raises(
        ChangeProcessingError,
        match="rollout 'linear' stage 1 names unconfigured output\\(s\\) "
        "platform-staging",
    ):
        change._change_stages(ROLLOUT_OUTPUTS, (rollout,))


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        pytest.param(
            "https://github.com/portswigger-cloud/system",
            "portswigger-cloud/system",
            id="https",
        ),
        pytest.param(
            "https://github.com/acme/manifests.git", "acme/manifests", id="dot-git"
        ),
        pytest.param(
            "https://github.com/acme/manifests/", "acme/manifests", id="slash"
        ),
        pytest.param("acme/config", "acme/config", id="already-short"),
        pytest.param("manifests", "manifests", id="one-part"),
    ],
)
def test_short_repo_names_a_repository_as_owner_and_name(
    repo: str, expected: str
) -> None:
    assert change.short_repo(repo) == expected


def test_short_commit_matches_what_git_prints() -> None:
    assert change.short_commit("b8d4f34d8fd265311b7a379072441675ff524f6e") == "b8d4f34"
    assert change.short_commit("deadbee") == "deadbee"
    assert change.short_commit("dead") == "dead"


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param([], "", id="none"),
        pytest.param(["platform-dev"], "platform-dev", id="one"),
        pytest.param(
            ["platform-prod", "observability"],
            "platform-prod and observability",
            id="two",
        ),
        pytest.param(["a", "b", "c"], "a, b and c", id="three"),
    ],
)
def test_join_names_reads_as_a_sentence(names: list[str], expected: str) -> None:
    assert change.join_names(names) == expected


def test_describe_refs_names_objects_the_way_kubectl_talks_about_them() -> None:
    refs = (
        Ref(kind="Deployment", namespace="relcoord", name="relcoord"),
        Ref(kind="Namespace", namespace=None, name="relcoord"),
    )

    assert change._describe_refs(refs) == (
        "Deployment relcoord/relcoord, Namespace relcoord"
    )


def test_describe_refs_cuts_a_long_list_short() -> None:
    """A shared label change rewrites everything, and the detail keeps the rest."""
    refs = tuple(
        Ref(kind="ConfigMap", namespace="relcoord", name=f"c{index}")
        for index in range(14)
    )

    assert change._describe_refs(refs) == (
        "ConfigMap relcoord/c0, ConfigMap relcoord/c1, ConfigMap relcoord/c2 "
        "and 11 more"
    )


CHANGED = (Ref(kind="Deployment", namespace="relcoord", name="relcoord"),)
GONE = (Ref(kind="ConfigMap", namespace="relcoord", name="old"),)


@pytest.mark.parametrize(
    ("changed", "removed", "expected"),
    [
        pytest.param((), (), "platform-dev: none of 95 manifests changed", id="none"),
        pytest.param(
            CHANGED, (), "platform-dev: 1 of 95 manifests changed", id="changed"
        ),
        pytest.param(
            CHANGED,
            GONE,
            "platform-dev: 1 of 95 manifests changed, 1 removed",
            id="changed-and-removed",
        ),
        pytest.param(
            (),
            GONE,
            "platform-dev: 0 of 95 manifests changed, 1 removed",
            id="removed-only",
        ),
    ],
)
def test_generated_message_leads_with_what_the_change_did(
    changed: tuple[Ref, ...], removed: tuple[Ref, ...], expected: str
) -> None:
    output = OutputSettings(
        name="platform-dev",
        repository=ROLLOUT_REPOSITORY,
        directory=Path("platform-dev"),
    )

    assert change._generated_message(output, 95, changed, removed) == expected


@pytest.mark.parametrize(
    ("target", "declares_targets", "expected"),
    [
        pytest.param(
            None, True, "generating manifests for target platform-dev", id="target"
        ),
        pytest.param(
            "prod",
            True,
            "generating manifests for target prod into platform-dev",
            id="renamed-target",
        ),
        pytest.param(
            None, False, "generating manifests for output platform-dev", id="vars"
        ),
    ],
)
def test_generating_message_uses_the_config_repositorys_own_terms(
    target: str | None, declares_targets: bool, expected: str
) -> None:
    output = OutputSettings(
        name="platform-dev",
        repository=ROLLOUT_REPOSITORY,
        directory=Path("platform-dev"),
        target=target,
    )

    assert change._generating_message(output, declares_targets) == expected


def test_stage_verified_message_reports_a_skipped_stage() -> None:
    stage = change._change_stages(ROLLOUT_OUTPUTS, [LINEAR_ROLLOUT])[1]

    assert change._stage_verified_message(stage, []) == (
        "stage 2 of 2 skipped: platform-prod and observability unaffected"
    )


def test_stage_verified_message_reports_every_cluster_that_picked_it_up() -> None:
    stage = change._change_stages(ROLLOUT_OUTPUTS, [LINEAR_ROLLOUT])[1]

    assert change._stage_verified_message(
        stage, [("platform-prod", 27.94), ("observability", 3.1)]
    ) == (
        "stage 2 of 2 verified: picked up by platform-prod after 27.9s "
        "and observability after 3.1s"
    )
