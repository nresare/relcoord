# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, cast

from dulwich import porcelain
from dulwich.repo import Repo
from manifest_builder import GenerationResult, generate

from relcoord.config import IdcatSettings, OutputSettings, TemplateValue
from relcoord.git import GitCredentialError, GitCredentials, github_https_credentials
from relcoord.kubernetes import KubernetesDeploymentDetector

logger = logging.getLogger(__name__)


class ChangeProcessingError(Exception):
    pass


class DeployConfigError(ChangeProcessingError):
    pass


class CredentialError(ChangeProcessingError):
    """Raised when git credentials for a repository cannot be obtained.

    This commonly happens when idcat does not grant the configured GitHub app
    access to the requested repository, which is an expected condition rather
    than a bug, so callers should report it without a stack trace.
    """


class GitTransportError(ChangeProcessingError):
    """Raised when a git clone or push fails at the transport level.

    dulwich does not always surface a descriptive message (for example a
    NotGitRepository error when a repository is missing, private, or otherwise
    inaccessible), so this carries whatever detail is available and is reported
    without a stack trace.
    """


class DeploymentDetectionError(ChangeProcessingError):
    pass


class DeploymentDetector(Protocol):
    def wait_for_success(
        self,
        *,
        deploy_id: str,
        created_or_modified: set[Any],
        removed: set[Any],
    ) -> None: ...


@dataclass(frozen=True)
class ChangeProgress:
    """A step reported by :meth:`ChangeProcessor.process` as it happens.

    ``phase`` names the kind of step and is the stable part of the contract;
    ``message`` is human readable and intended for display; ``detail`` carries
    JSON serialisable specifics of the step.
    """

    phase: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


ProgressSink = Callable[[ChangeProgress], None]


def ignore_progress(event: ChangeProgress) -> None:
    """Discard progress events, for callers that do not observe them."""


@dataclass(frozen=True)
class ChangeResult:
    repo: str
    commit: str
    deploy_config: Path
    manifests_checkout: Path
    generated_count: int
    deploy_id: str | None = None
    outputs: tuple[OutputResult, ...] = ()


@dataclass(frozen=True)
class OutputResult:
    name: str
    repository: str
    directory: Path
    manifests_checkout: Path
    generated_count: int
    deploy_id: str | None = None


@dataclass(frozen=True)
class ChangeProcessor:
    manifests_repository: str | None = None
    outputs: Sequence[OutputSettings] = ()
    idcat: IdcatSettings | None = None
    detect_deployment: bool = False
    deployment_detector: DeploymentDetector | None = None

    def process(
        self,
        repo: str,
        commit: str,
        image: str | None,
        config_path: str = ".deploy",
        system: bool = False,
        *,
        progress: ProgressSink = ignore_progress,
    ) -> ChangeResult:
        def report(phase: str, message: str, **detail: Any) -> None:
            progress(ChangeProgress(phase=phase, message=message, detail=detail))

        workdir = Path(tempfile.mkdtemp(prefix="relcoord-change-"))
        try:
            source_checkout = workdir / "source"
            output_settings = self._configured_outputs()
            checkout_by_repository = _checkout_paths_by_repository(
                workdir, output_settings
            )
            message = (
                f"created temporary workspace {workdir} for repo {repo} "
                f"at commit {commit}"
            )
            logger.info("change step 1/7: %s", message)
            report("workspace", message, workdir=str(workdir))
            message = f"checking out source repo {repo} at commit {commit}"
            logger.info("change step 2/7: %s", message)
            report("source-checkout", message, repo=repo, commit=commit)
            _checkout_commit(repo, commit, source_checkout, self.idcat)
            if system:
                # System mode: config lives at the repository root and
                # manifest-builder runs as the 'system' owner (namespace=None),
                # generating into every namespace not claimed by another owner
                # as well as cluster-scoped directories.
                deploy_config = source_checkout
                namespace = None
            else:
                deploy_config = source_checkout / config_path
                namespace = _namespace_from_repo(repo)
                if not deploy_config.is_dir():
                    raise DeployConfigError(
                        f"commit {commit} in {repo} does not contain "
                        f"a {config_path} directory"
                    )
            message = f"found deploy config at {deploy_config} (system mode: {system})"
            logger.info("change step 3/7: %s", message)
            report(
                "deploy-config",
                message,
                deploy_config=str(deploy_config),
                system=system,
                namespace=namespace,
            )

            repo_root = Path("/")
            create_commit = True
            output_results: list[OutputResult] = []
            total_generated = 0

            for repository, manifests_checkout in checkout_by_repository.items():
                detection_results: list[tuple[GenerationResult, str | None]] = []
                message = f"checking out manifests repo {repository} into {manifests_checkout}"
                logger.info("change step 4/7: %s", message)
                report("manifests-checkout", message, repository=repository)
                _clone_repository(
                    repository,
                    manifests_checkout,
                    self.idcat,
                    purpose=f"cloning manifests repo {repository}",
                    depth="1",
                )

                for output in _outputs_for_repository(output_settings, repository):
                    output_path = manifests_checkout / output.directory
                    output_path.mkdir(parents=True, exist_ok=True)
                    logger.info(
                        "change step 5/7: invoking manifest-builder generate("
                        "output=%s, deploy_config=%s, manifests_checkout=%s, "
                        "repo_root=%s, create_commit=%s, image=%s, namespace=%s, "
                        "vars=%s)",
                        output.name,
                        deploy_config,
                        output_path,
                        repo_root,
                        create_commit,
                        image,
                        namespace,
                        _vars_log_summary(output.vars),
                    )
                    report(
                        "generate",
                        f"invoking manifest-builder for output {output.name}",
                        output=output.name,
                        repository=repository,
                        directory=str(output.directory),
                    )
                    generation_result = generate(
                        deploy_config,
                        output_path,
                        repo_root=repo_root,
                        create_commit=create_commit,
                        image=image,
                        namespace=namespace,
                        vars=output.vars,
                    )
                    generated = _written_paths(generation_result)
                    relative_paths = [
                        str(path.relative_to(output_path)) for path in sorted(generated)
                    ]
                    generated_paths = ", ".join(relative_paths)
                    message = (
                        f"manifest-builder generated {len(generated)} file(s) "
                        f"for output {output.name}"
                        f"{f': {generated_paths}' if generated_paths else ''}"
                    )
                    logger.info("change step 5/7: %s", message)
                    report(
                        "generated",
                        message,
                        output=output.name,
                        repository=repository,
                        generated=len(generated),
                        paths=relative_paths,
                    )
                    deploy_id = _deploy_id(generation_result)
                    if self.detect_deployment and deploy_id is None:
                        raise DeploymentDetectionError(
                            "manifest-builder did not return a deploy_id; "
                            "deployment detection requires git-backed generation"
                        )
                    output_results.append(
                        OutputResult(
                            name=output.name,
                            repository=output.repository,
                            directory=output.directory,
                            manifests_checkout=manifests_checkout,
                            generated_count=len(generated),
                            deploy_id=deploy_id,
                        )
                    )
                    detection_results.append((generation_result, deploy_id))
                    total_generated += len(generated)

                if not any(
                    result.created_or_modified or result.removed
                    for result, _ in detection_results
                ):
                    message = (
                        f"manifest-builder produced no changes for repo {repository}; "
                        "nothing to commit or push"
                    )
                    logger.info("change step 6/7: %s", message)
                    report("no-changes", message, repository=repository)
                    continue

                manifest_commit = _head_commit(manifests_checkout)
                message = f"manifest-builder created manifests commit {manifest_commit}"
                logger.info("change step 6/7: %s", message)
                report(
                    "commit",
                    message,
                    repository=repository,
                    manifest_commit=manifest_commit,
                )
                message = f"pushing manifests commit {manifest_commit} to {repository}"
                logger.info("change step 7/7: %s", message)
                report(
                    "push",
                    message,
                    repository=repository,
                    manifest_commit=manifest_commit,
                )
                _push_repository(
                    manifests_checkout,
                    repository,
                    self.idcat,
                )
                message = (
                    f"pushed manifests commit {manifest_commit} for source repo "
                    f"{repo} at commit {commit}"
                )
                logger.info("change complete: %s", message)
                report(
                    "pushed",
                    message,
                    repository=repository,
                    manifest_commit=manifest_commit,
                )

                if self.detect_deployment:
                    for generation_result, deploy_id in detection_results:
                        report(
                            "deployment-detection",
                            "waiting for deployment of manifest-builder deploy-id "
                            f"{deploy_id}",
                            deploy_id=deploy_id,
                        )
                        _start_deployment_detection(
                            generation_result,
                            deploy_id,
                            self.deployment_detector,
                        )
            return ChangeResult(
                repo=repo,
                commit=commit,
                deploy_config=deploy_config,
                manifests_checkout=output_results[0].manifests_checkout,
                generated_count=total_generated,
                deploy_id=output_results[0].deploy_id,
                outputs=tuple(output_results),
            )
        except ChangeProcessingError:
            raise
        except Exception as exc:
            raise ChangeProcessingError(str(exc)) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _configured_outputs(self) -> tuple[OutputSettings, ...]:
        if self.outputs:
            return tuple(self.outputs)
        if self.manifests_repository is None:
            raise ChangeProcessingError("at least one output must be configured")
        return (
            OutputSettings(
                name="manifests",
                repository=self.manifests_repository,
                directory=Path("."),
            ),
        )


def _written_paths(generation_result: object) -> set[Path]:
    written_paths = getattr(generation_result, "written_paths", None)
    if written_paths is None:
        return set(cast(Iterable[Path], generation_result))
    return cast(set[Path], written_paths)


def _deploy_id(generation_result: object) -> str | None:
    deploy_id = getattr(generation_result, "deploy_id", None)
    return deploy_id if isinstance(deploy_id, str) else None


def _checkout_paths_by_repository(
    workdir: Path, outputs: Sequence[OutputSettings]
) -> dict[str, Path]:
    repositories = list(dict.fromkeys(output.repository for output in outputs))
    if len(repositories) == 1:
        return {repositories[0]: workdir / "manifests"}
    return {
        repository: workdir / f"manifests-{index}"
        for index, repository in enumerate(repositories, start=1)
    }


def _outputs_for_repository(
    outputs: Sequence[OutputSettings], repository: str
) -> tuple[OutputSettings, ...]:
    return tuple(output for output in outputs if output.repository == repository)


def _vars_log_summary(vars: dict[str, TemplateValue]) -> str:
    if not vars:
        return "none"
    return ", ".join(sorted(vars))


def _start_deployment_detection(
    generation_result: GenerationResult,
    deploy_id: str | None,
    detector: DeploymentDetector | None,
) -> None:
    if deploy_id is None:
        raise DeploymentDetectionError(
            "manifest-builder did not return a deploy_id; "
            "deployment detection requires git-backed generation"
        )
    created_or_modified = set(generation_result.created_or_modified)
    removed = set(generation_result.removed)
    logger.info(
        "starting deployment detection for manifest-builder deploy-id %s",
        deploy_id,
    )
    thread = threading.Thread(
        target=_run_deployment_detection,
        name=f"relcoord-deployment-detection-{deploy_id}",
        kwargs={
            "deploy_id": deploy_id,
            "created_or_modified": created_or_modified,
            "removed": removed,
            "detector": detector,
        },
        daemon=True,
    )
    thread.start()


def _run_deployment_detection(
    *,
    deploy_id: str,
    created_or_modified: set[Any],
    removed: set[Any],
    detector: DeploymentDetector | None,
) -> None:
    owned_detector = KubernetesDeploymentDetector() if detector is None else None
    active_detector = owned_detector if owned_detector is not None else detector
    if active_detector is None:
        logger.error(
            "deployment detection failed for manifest-builder deploy-id %s: "
            "deployment detector is not configured",
            deploy_id,
        )
        return
    try:
        active_detector.wait_for_success(
            deploy_id=deploy_id,
            created_or_modified=created_or_modified,
            removed=removed,
        )
    except Exception:
        logger.exception(
            "deployment detection failed for manifest-builder deploy-id %s",
            deploy_id,
        )
    else:
        logger.info(
            "deployment detected for manifest-builder deploy-id %s",
            deploy_id,
        )
    finally:
        if owned_detector is not None:
            owned_detector.close()


def _checkout_commit(
    source: str, commit: str, target: Path, idcat: IdcatSettings | None
) -> None:
    _clone_repository(
        source,
        target,
        idcat,
        purpose=f"checking out source repo {source}",
        no_checkout=True,
    )
    _dulwich_checkout(target, commit)


def _namespace_from_repo(repo: str) -> str:
    namespace = repo.rsplit("/", maxsplit=1)[-1]
    return namespace.removesuffix(".git")


def _credentials_for(
    source: str, idcat: IdcatSettings | None, purpose: str
) -> GitCredentials:
    try:
        return github_https_credentials(source, idcat)
    except GitCredentialError as exc:
        raise CredentialError(
            f"failed to obtain git credentials while {purpose}: {exc}"
        ) from exc


def _clone_repository(
    source: str,
    target: Path,
    idcat: IdcatSettings | None,
    *,
    purpose: str,
    depth: str | None = None,
    no_checkout: bool = False,
) -> None:
    credentials = _credentials_for(source, idcat, purpose)
    clone_output = BytesIO()
    repo: Repo | None = None
    try:
        if credentials.username is None:
            repo = porcelain.clone(
                source,
                target,
                checkout=not no_checkout,
                depth=int(depth) if depth is not None else None,
                errstream=clone_output,
            )
        else:
            repo = porcelain.clone(
                source,
                target,
                checkout=not no_checkout,
                depth=int(depth) if depth is not None else None,
                errstream=clone_output,
                username=credentials.username,
                password=credentials.password or "",
            )
    except Exception as exc:
        _log_dulwich_output("clone", clone_output)
        raise GitTransportError(
            _dulwich_error_message(
                "clone",
                {"remote": source, "target": str(target)},
                exc,
                clone_output,
            )
        ) from exc
    else:
        _log_dulwich_output("clone", clone_output)
    finally:
        if repo is not None:
            repo.close()


def _dulwich_checkout(target: Path, commit: str) -> None:
    try:
        porcelain.reset(target, "hard", commit)
    except Exception as exc:
        raise GitTransportError(
            _dulwich_error_message(
                "checkout",
                {"target": str(target), "commit": commit},
                exc,
            )
        ) from exc


def _head_commit(repo_path: Path) -> str:
    repo = Repo(repo_path)
    try:
        return repo.head().decode("ascii")
    finally:
        repo.close()


def _push_repository(
    repo_path: Path,
    remote: str,
    idcat: IdcatSettings | None,
) -> None:
    credentials = _credentials_for(remote, idcat, f"pushing to manifests repo {remote}")
    push_output = BytesIO()
    try:
        if credentials.username is None:
            porcelain.push(
                repo_path,
                remote,
                errstream=push_output,
            )
        else:
            porcelain.push(
                repo_path,
                remote,
                errstream=push_output,
                username=credentials.username,
                password=credentials.password or "",
            )
    except Exception as exc:
        _log_dulwich_output("push", push_output)
        raise GitTransportError(
            _dulwich_error_message(
                "push",
                {"source": str(repo_path), "remote": remote},
                exc,
                push_output,
            )
        ) from exc
    else:
        _log_dulwich_output("push", push_output)


def _dulwich_error_message(
    action: str,
    params: dict[str, str],
    exc: Exception,
    errstream: BytesIO | None = None,
) -> str:
    stderr = ""
    if errstream is not None:
        stderr = errstream.getvalue().decode(errors="replace").strip()
    detail = stderr or str(exc).strip() or _exception_label(exc)
    described = ", ".join(f"{key}={value}" for key, value in params.items())
    return f"dulwich {action} failed ({described}): {detail}"


def _exception_label(exc: Exception) -> str:
    """Return a readable name for an exception that carries no message.

    Some dulwich errors (for example NotGitRepository, raised when a repository
    is missing, private, or otherwise inaccessible) have an empty string
    representation, which would otherwise produce a blank error detail.
    """
    module = type(exc).__module__
    qualname = type(exc).__qualname__
    if module and module != "builtins":
        return f"{module}.{qualname}"
    return qualname


def _log_dulwich_output(operation: str, errstream: BytesIO) -> None:
    stderr = errstream.getvalue().decode(errors="replace").strip()
    if stderr:
        logger.debug("dulwich %s stderr: %s", operation, stderr)
