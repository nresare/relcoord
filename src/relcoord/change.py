# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, cast

from dulwich import porcelain
from dulwich.repo import Repo
from manifest_builder import generate

from relcoord.config import IdcatSettings
from relcoord.git import github_https_credentials
from relcoord.kubernetes import KubernetesDeploymentDetector

logger = logging.getLogger(__name__)


class ChangeProcessingError(Exception):
    pass


class DeployConfigError(ChangeProcessingError):
    pass


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
class ChangeResult:
    repo: str
    commit: str
    deploy_config: Path
    manifests_checkout: Path
    generated_count: int
    deploy_id: str | None = None


@dataclass(frozen=True)
class ChangeProcessor:
    manifests_repository: str
    idcat: IdcatSettings | None = None
    detect_deployment: bool = False
    deployment_detector: DeploymentDetector | None = None

    def process(self, repo: str, commit: str, image: str | None) -> ChangeResult:
        workdir = Path(tempfile.mkdtemp(prefix="relcoord-change-"))
        try:
            source_checkout = workdir / "source"
            manifests_checkout = workdir / "manifests"
            logger.info(
                "change step 1/7: created temporary workspace %s for repo %s at commit %s",
                workdir,
                repo,
                commit,
            )
            logger.info(
                "change step 2/7: checking out source repo %s at commit %s",
                repo,
                commit,
            )
            _checkout_commit(repo, commit, source_checkout, self.idcat)
            deploy_config = source_checkout / ".deploy"
            if not deploy_config.is_dir():
                raise DeployConfigError(
                    f"commit {commit} in {repo} does not contain a top-level .deploy directory"
                )
            logger.info(
                "change step 3/7: found deploy config at %s",
                deploy_config,
            )

            logger.info(
                "change step 4/7: checking out manifests repo %s into %s",
                self.manifests_repository,
                manifests_checkout,
            )
            _clone_repository(
                self.manifests_repository,
                manifests_checkout,
                self.idcat,
                depth="1",
            )
            repo_root = Path("/")
            create_commit = True
            namespace = _namespace_from_repo(repo)
            logger.info(
                "change step 5/7: invoking manifest-builder generate("
                "deploy_config=%s, manifests_checkout=%s, repo_root=%s, "
                "create_commit=%s, image=%s, namespace=%s)",
                deploy_config,
                manifests_checkout,
                repo_root,
                create_commit,
                image,
                namespace,
            )
            generation_result = generate(
                deploy_config,
                manifests_checkout,
                repo_root=repo_root,
                create_commit=create_commit,
                image=image,
                namespace=namespace,
            )
            generated = _written_paths(generation_result)
            generated_paths = ", ".join(
                str(path.relative_to(manifests_checkout)) for path in sorted(generated)
            )
            logger.info(
                "change step 5/7: manifest-builder generated %d file(s)%s",
                len(generated),
                f": {generated_paths}" if generated_paths else "",
            )
            deploy_id = _deploy_id(generation_result)
            if self.detect_deployment and deploy_id is None:
                raise DeploymentDetectionError(
                    "manifest-builder did not return a deploy_id; "
                    "deployment detection requires git-backed generation"
                )
            manifest_commit = _head_commit(manifests_checkout)
            logger.info(
                "change step 6/7: manifest-builder created manifests commit %s",
                manifest_commit,
            )
            logger.info(
                "change step 7/7: pushing manifests commit %s to %s",
                manifest_commit,
                self.manifests_repository,
            )
            _push_repository(
                manifests_checkout,
                self.manifests_repository,
                self.idcat,
            )
            logger.info(
                "change complete: pushed manifests commit %s for source repo %s at commit %s",
                manifest_commit,
                repo,
                commit,
            )
            if self.detect_deployment:
                _start_deployment_detection(
                    generation_result,
                    deploy_id,
                    self.deployment_detector,
                )
            return ChangeResult(
                repo=repo,
                commit=commit,
                deploy_config=deploy_config,
                manifests_checkout=manifests_checkout,
                generated_count=len(generated),
                deploy_id=deploy_id,
            )
        except ChangeProcessingError:
            raise
        except Exception as exc:
            raise ChangeProcessingError(str(exc)) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _written_paths(generation_result: object) -> set[Path]:
    written_paths = getattr(generation_result, "written_paths", None)
    if written_paths is None:
        return set(cast(Iterable[Path], generation_result))
    return cast(set[Path], written_paths)


def _deploy_id(generation_result: object) -> str | None:
    deploy_id = getattr(generation_result, "deploy_id", None)
    return deploy_id if isinstance(deploy_id, str) else None


def _start_deployment_detection(
    generation_result: object,
    deploy_id: str | None,
    detector: DeploymentDetector | None,
) -> None:
    if deploy_id is None:
        raise DeploymentDetectionError(
            "manifest-builder did not return a deploy_id; "
            "deployment detection requires git-backed generation"
        )
    created_or_modified = set(getattr(generation_result, "created_or_modified"))
    removed = set(getattr(generation_result, "removed"))
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
    _clone_repository(source, target, idcat, no_checkout=True)
    _dulwich_checkout(target, commit)


def _namespace_from_repo(repo: str) -> str:
    namespace = repo.rsplit("/", maxsplit=1)[-1]
    return namespace.removesuffix(".git")


def _clone_repository(
    source: str,
    target: Path,
    idcat: IdcatSettings | None,
    *,
    depth: str | None = None,
    no_checkout: bool = False,
) -> None:
    credentials = github_https_credentials(source, idcat)
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
        raise ChangeProcessingError(
            _dulwich_error_message("clone", exc, clone_output)
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
        raise ChangeProcessingError(f"dulwich checkout {commit} failed: {exc}") from exc


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
    credentials = github_https_credentials(remote, idcat)
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
        raise ChangeProcessingError(
            _dulwich_error_message("push", exc, push_output)
        ) from exc
    else:
        _log_dulwich_output("push", push_output)


def _dulwich_error_message(operation: str, exc: Exception, errstream: BytesIO) -> str:
    message = errstream.getvalue().decode(errors="replace").strip() or str(exc)
    return f"dulwich {operation} failed: {message}"


def _log_dulwich_output(operation: str, errstream: BytesIO) -> None:
    stderr = errstream.getvalue().decode(errors="replace").strip()
    if stderr:
        logger.debug("dulwich %s stderr: %s", operation, stderr)
