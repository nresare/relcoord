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
from dulwich.objects import Commit, ObjectID
from dulwich.repo import Repo
from manifest_builder import GenerationResult, generate
from manifest_builder.config import (
    TARGETS_VERSION,
    config_version,
    find_config_file,
    load_toml_file,
)

from relcoord.config import IdcatSettings, OutputSettings
from relcoord.git import GitCredentialError, GitCredentials, github_https_credentials
from relcoord.github import GithubCommentError, GithubIssueCommenter, IssueCommenter
from relcoord.kubernetes import KubernetesDeploymentDetector, KubernetesObjectRef
from relcoord.manifest_diff import (
    DiffSection,
    ManifestDiff,
    build_comment_body,
    manifest_diff,
)

logger = logging.getLogger(__name__)

# relcoord has nowhere to upload an artifact to, so the full diff travels back
# with the response that asked for the comment.
FULL_DIFF_REFERENCE = "returned in the relcoord response for this request"


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


class CommentPostError(ChangeProcessingError):
    """Raised when a manifest diff comment could not be posted to GitHub.

    The diff itself succeeded in this case, so the failure is about GitHub
    rejecting the comment rather than about the manifests, and is reported
    without a stack trace.
    """


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
    """What one configured output contributed to a change.

    ``created_or_modified`` and ``removed`` name the Kubernetes objects the
    commit touched, which manifest-builder reads back out of the manifests it
    wrote, and ``deploy_id`` is the value of the noa.re/deploy-id annotation it
    stamped on each of them. Together they are what deployment detection waits
    for, and what a caller needs to follow the change into ``cluster``.
    """

    name: str
    repository: str
    directory: Path
    manifests_checkout: Path
    generated_count: int
    deploy_id: str | None = None
    cluster: str | None = None
    created_or_modified: tuple[KubernetesObjectRef, ...] = ()
    removed: tuple[KubernetesObjectRef, ...] = ()


@dataclass(frozen=True)
class ChangeProcessor:
    manifests_repository: str | None = None
    outputs: Sequence[OutputSettings] = ()
    idcat: IdcatSettings | None = None
    detect_deployment: bool = False
    deployment_detector: DeploymentDetector | None = None
    """Detector to use for every output, in place of connecting to its cluster."""

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
            deploy_config, namespace = _deploy_config_and_namespace(
                source_checkout, repo, commit, config_path, system
            )
            declares_targets = _declares_targets(deploy_config)
            message = (
                f"found deploy config at {deploy_config} (system mode: {system}, "
                f"targets: {declares_targets})"
            )
            logger.info("change step 3/7: %s", message)
            report(
                "deploy-config",
                message,
                deploy_config=str(deploy_config),
                system=system,
                namespace=namespace,
                targets=declares_targets,
            )

            repo_root = Path("/")
            create_commit = True
            output_results: list[OutputResult] = []
            total_generated = 0

            for repository, manifests_checkout in checkout_by_repository.items():
                detection_results: list[tuple[OutputSettings, GenerationResult]] = []
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
                    selection = _generate_selection(output, declares_targets)
                    logger.info(
                        "change step 5/7: invoking manifest-builder generate("
                        "output=%s, deploy_config=%s, manifests_checkout=%s, "
                        "repo_root=%s, create_commit=%s, image=%s, namespace=%s, "
                        "%s)",
                        output.name,
                        deploy_config,
                        output_path,
                        repo_root,
                        create_commit,
                        image,
                        namespace,
                        _selection_log_summary(selection),
                    )
                    report(
                        "generate",
                        f"invoking manifest-builder for output {output.name}",
                        output=output.name,
                        repository=repository,
                        directory=str(output.directory),
                        **_selection_detail(selection),
                    )
                    generation_result = generate(
                        deploy_config,
                        output_path,
                        repo_root=repo_root,
                        create_commit=create_commit,
                        image=image,
                        namespace=namespace,
                        **selection,
                    )
                    generated = _written_paths(generation_result)
                    message = (
                        f"manifest-builder generated {len(generated)} file(s) "
                        f"for output {output.name}"
                    )
                    logger.info("change step 5/7: %s", message)
                    report(
                        "generated",
                        message,
                        output=output.name,
                        repository=repository,
                        generated=len(generated),
                    )
                    deploy_id = _deploy_id(generation_result)
                    if self.detect_deployment and deploy_id is None:
                        raise DeploymentDetectionError(
                            "manifest-builder did not return a deploy_id; "
                            "deployment detection requires git-backed generation"
                        )
                    created_or_modified = _sorted_refs(
                        generation_result.created_or_modified
                    )
                    removed_refs = _sorted_refs(generation_result.removed)
                    if created_or_modified or removed_refs:
                        message = _changed_objects_message(
                            output, created_or_modified, removed_refs, deploy_id
                        )
                        logger.info("change step 5/7: %s", message)
                        report(
                            "changed-objects",
                            message,
                            output=output.name,
                            repository=repository,
                            cluster=_cluster_name(output),
                            deploy_id=deploy_id,
                            created_or_modified=object_ref_payloads(
                                created_or_modified
                            ),
                            removed=object_ref_payloads(removed_refs),
                        )
                    output_results.append(
                        OutputResult(
                            name=output.name,
                            repository=output.repository,
                            directory=output.directory,
                            manifests_checkout=manifests_checkout,
                            generated_count=len(generated),
                            deploy_id=deploy_id,
                            cluster=_cluster_name(output),
                            created_or_modified=created_or_modified,
                            removed=removed_refs,
                        )
                    )
                    detection_results.append((output, generation_result))
                    total_generated += len(generated)

                if not any(
                    result.created_or_modified or result.removed
                    for _, result in detection_results
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
                    for output, generation_result in detection_results:
                        deploy_id = _deploy_id(generation_result)
                        connection = self._connection_for(output)
                        report(
                            "deployment-detection",
                            "waiting for deployment of manifest-builder deploy-id "
                            f"{deploy_id} in cluster {output.name}",
                            deploy_id=deploy_id,
                            output=output.name,
                            cluster=output.name,
                        )
                        _start_deployment_detection(
                            generation_result,
                            deploy_id,
                            connection,
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
        return _resolve_output_settings(self.outputs, self.manifests_repository)

    def _connection_for(self, output: OutputSettings) -> OutputSettings | None:
        """Return the connection for an output's deployment destination.

        None where an injected detector stands in for a real cluster, which is
        how tests and callers that supply their own connection work; a
        configuration reaching detection without either is a bug rather than
        something a change request can cause, since config loading rejects it.
        """
        if output.connection_type is None:
            if self.deployment_detector is not None:
                return None
            raise DeploymentDetectionError(
                f"output {output.name} has no connection-type for deployment detection"
            )
        return output


@dataclass(frozen=True)
class OutputDiff:
    """What one configured output contributed to a manifest diff."""

    name: str
    repository: str
    directory: Path
    generated_count: int


@dataclass(frozen=True)
class RepositoryDiff:
    repository: str
    manifest_diff: ManifestDiff


@dataclass(frozen=True)
class DiffComment:
    body: str
    posted: bool
    url: str | None = None


@dataclass(frozen=True)
class DiffResult:
    repo: str
    commit: str
    pull_request: int | None
    generated_count: int
    outputs: tuple[OutputDiff, ...]
    diffs: tuple[RepositoryDiff, ...]
    comment: DiffComment


@dataclass(frozen=True)
class DiffCommentProcessor:
    """Reports what a config commit would do to the manifests repositories.

    The work is the same as :class:`ChangeProcessor` up to and including the
    manifest commit, and then stops: nothing is pushed. The commit is still made,
    in the throwaway checkout, because that is what makes the diff the one a
    change would produce, cleanups and deploy-id annotations included.

    ``diff_output`` names the single output to report on, for a deployment where
    a diff across every configured output would be more than a reviewer wants to
    read. Every configured output is reported when it is unset.
    """

    manifests_repository: str | None = None
    outputs: Sequence[OutputSettings] = ()
    diff_output: str | None = None
    idcat: IdcatSettings | None = None
    commenter: IssueCommenter | None = None

    def diff(
        self,
        repo: str,
        commit: str,
        config_path: str = ".deploy",
        system: bool = False,
        *,
        pull_request: int | None = None,
        progress: ProgressSink = ignore_progress,
    ) -> DiffResult:
        def report(phase: str, message: str, **detail: Any) -> None:
            progress(ChangeProgress(phase=phase, message=message, detail=detail))

        workdir = Path(tempfile.mkdtemp(prefix="relcoord-diff-"))
        try:
            source_checkout = workdir / "source"
            output_settings = _selected_outputs(
                _resolve_output_settings(self.outputs, self.manifests_repository),
                self.diff_output,
            )
            checkout_by_repository = _checkout_paths_by_repository(
                workdir, output_settings
            )
            message = (
                f"created temporary workspace {workdir} for repo {repo} "
                f"at commit {commit}"
            )
            logger.info("diff step 1/6: %s", message)
            report("workspace", message, workdir=str(workdir))
            message = f"checking out source repo {repo} at commit {commit}"
            logger.info("diff step 2/6: %s", message)
            report("source-checkout", message, repo=repo, commit=commit)
            _checkout_commit(repo, commit, source_checkout, self.idcat)
            deploy_config, namespace = _deploy_config_and_namespace(
                source_checkout, repo, commit, config_path, system
            )
            declares_targets = _declares_targets(deploy_config)
            message = (
                f"found deploy config at {deploy_config} (system mode: {system}, "
                f"targets: {declares_targets})"
            )
            logger.info("diff step 3/6: %s", message)
            report(
                "deploy-config",
                message,
                deploy_config=str(deploy_config),
                system=system,
                namespace=namespace,
                targets=declares_targets,
            )

            output_diffs: list[OutputDiff] = []
            repository_diffs: list[RepositoryDiff] = []
            total_generated = 0

            for repository, manifests_checkout in checkout_by_repository.items():
                message = f"checking out manifests repo {repository} into {manifests_checkout}"
                logger.info("diff step 4/6: %s", message)
                report("manifests-checkout", message, repository=repository)
                _clone_repository(
                    repository,
                    manifests_checkout,
                    self.idcat,
                    purpose=f"cloning manifests repo {repository}",
                    depth="1",
                )
                base_commit = _head_commit(manifests_checkout)

                for output in _outputs_for_repository(output_settings, repository):
                    output_path = manifests_checkout / output.directory
                    output_path.mkdir(parents=True, exist_ok=True)
                    selection = _generate_selection(output, declares_targets)
                    logger.info(
                        "diff step 5/6: invoking manifest-builder generate("
                        "output=%s, deploy_config=%s, manifests_checkout=%s, "
                        "namespace=%s, %s)",
                        output.name,
                        deploy_config,
                        output_path,
                        namespace,
                        _selection_log_summary(selection),
                    )
                    report(
                        "generate",
                        f"invoking manifest-builder for output {output.name}",
                        output=output.name,
                        repository=repository,
                        directory=str(output.directory),
                        **_selection_detail(selection),
                    )
                    generation_result = generate(
                        deploy_config,
                        output_path,
                        repo_root=Path("/"),
                        create_commit=True,
                        image=None,
                        namespace=namespace,
                        **selection,
                    )
                    generated = _written_paths(generation_result)
                    message = (
                        f"manifest-builder generated {len(generated)} file(s) "
                        f"for output {output.name}"
                    )
                    logger.info("diff step 5/6: %s", message)
                    report(
                        "generated",
                        message,
                        output=output.name,
                        repository=repository,
                        generated=len(generated),
                    )
                    output_diffs.append(
                        OutputDiff(
                            name=output.name,
                            repository=output.repository,
                            directory=output.directory,
                            generated_count=len(generated),
                        )
                    )
                    total_generated += len(generated)

                repository_diff = _manifests_diff(manifests_checkout, base_commit)
                repository_diffs.append(
                    RepositoryDiff(repository=repository, manifest_diff=repository_diff)
                )
                if repository_diff.diff.strip():
                    changed_files = _changed_file_count(repository_diff)
                    message = (
                        f"manifest-builder changed {changed_files} file(s) "
                        f"in repo {repository}"
                    )
                    logger.info("diff step 6/6: %s", message)
                    report(
                        "diff",
                        message,
                        repository=repository,
                        changed=changed_files,
                    )
                else:
                    message = (
                        f"manifest-builder produced no changes for repo {repository}"
                    )
                    logger.info("diff step 6/6: %s", message)
                    report("no-changes", message, repository=repository)

            comment_body = build_comment_body(
                _diff_sections(repository_diffs),
                full_diff_reference=FULL_DIFF_REFERENCE,
            )
            comment = self._comment(repo, pull_request, comment_body.body, report)
            return DiffResult(
                repo=repo,
                commit=commit,
                pull_request=pull_request,
                generated_count=total_generated,
                outputs=tuple(output_diffs),
                diffs=tuple(repository_diffs),
                comment=comment,
            )
        except ChangeProcessingError:
            raise
        except Exception as exc:
            raise ChangeProcessingError(str(exc)) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _comment(
        self,
        repo: str,
        pull_request: int | None,
        body: str,
        report: Callable[..., None],
    ) -> DiffComment:
        if pull_request is None:
            message = (
                "no pull request requested; not posting a manifest diff comment "
                f"for repo {repo}"
            )
            logger.info("diff complete: %s", message)
            report("no-comment", message, repo=repo)
            return DiffComment(body=body, posted=False)

        commenter = (
            self.commenter
            if self.commenter is not None
            else GithubIssueCommenter(idcat=self.idcat)
        )
        message = (
            f"posting manifest diff comment to {repo} pull request #{pull_request}"
        )
        logger.info("diff step 6/6: %s", message)
        report("comment", message, repo=repo, pull_request=pull_request)
        try:
            url = commenter.post_comment(repo, pull_request, body)
        except GitCredentialError as exc:
            raise CredentialError(
                "failed to obtain git credentials while posting a manifest diff "
                f"comment to {repo} pull request #{pull_request}: {exc}"
            ) from exc
        except GithubCommentError as exc:
            raise CommentPostError(str(exc)) from exc

        message = f"posted manifest diff comment to {repo} pull request #{pull_request}"
        logger.info("diff complete: %s", message)
        report(
            "commented",
            message,
            repo=repo,
            pull_request=pull_request,
            url=url,
        )
        return DiffComment(body=body, posted=True, url=url)


def _resolve_output_settings(
    outputs: Sequence[OutputSettings], manifests_repository: str | None
) -> tuple[OutputSettings, ...]:
    if outputs:
        return tuple(outputs)
    if manifests_repository is None:
        raise ChangeProcessingError("at least one output must be configured")
    return (
        OutputSettings(
            name="manifests",
            repository=manifests_repository,
            directory=Path("."),
        ),
    )


def _selected_outputs(
    outputs: tuple[OutputSettings, ...], name: str | None
) -> tuple[OutputSettings, ...]:
    """Narrow the configured outputs to the one a diff was configured to report."""
    if name is None:
        return outputs
    selected = tuple(output for output in outputs if output.name == name)
    if not selected:
        raise ChangeProcessingError(
            f"diff-output '{name}' does not name a configured output"
        )
    logger.info("reporting the manifest diff for output %s only", name)
    return selected


def _deploy_config_and_namespace(
    source_checkout: Path, repo: str, commit: str, config_path: str, system: bool
) -> tuple[Path, str | None]:
    if system:
        # System mode: config lives at the repository root and manifest-builder
        # runs as the 'system' owner (namespace=None), generating into every
        # namespace not claimed by another owner as well as cluster-scoped
        # directories.
        return source_checkout, None

    deploy_config = source_checkout / config_path
    if not deploy_config.is_dir():
        raise DeployConfigError(
            f"commit {commit} in {repo} does not contain a {config_path} directory"
        )
    return deploy_config, _namespace_from_repo(repo)


def _manifests_diff(manifests_checkout: Path, base_commit: str) -> ManifestDiff:
    """Diff what manifest-builder committed against the cloned commit.

    manifest-builder creates no commit when it changed nothing, in which case
    both sides of the diff are the commit the checkout was cloned at.
    """
    repo = Repo(manifests_checkout)
    try:
        base = _commit_tree(repo, ObjectID(base_commit.encode("ascii")))
        return manifest_diff(repo, base, _commit_tree(repo, repo.head()))
    finally:
        repo.close()


def _commit_tree(repo: Repo, commit: ObjectID) -> ObjectID:
    obj = repo.object_store[commit]
    if not isinstance(obj, Commit):
        raise ChangeProcessingError(f"{commit.decode()} is not a commit")
    return ObjectID(obj.tree)


def _changed_file_count(diff: ManifestDiff) -> int:
    return sum(1 for line in diff.diff.splitlines() if line.startswith("diff --git "))


def _diff_sections(diffs: Sequence[RepositoryDiff]) -> tuple[DiffSection, ...]:
    """Pair each repository's diff with the heading to render it under.

    A single manifests repository, which is the usual configuration, renders
    without a heading.
    """
    if len(diffs) == 1:
        return (DiffSection(heading=None, diff=diffs[0].manifest_diff),)
    return tuple(
        DiffSection(heading=entry.repository, diff=entry.manifest_diff)
        for entry in diffs
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


def _declares_targets(deploy_config: Path) -> bool:
    """Report whether a config directory declares targets.

    manifest-builder takes what to generate either as template variables or, for
    a ``version = 2`` config directory, as the name of a target, so relcoord has
    to know which layout a config commit uses before it calls generate(). A
    directory holding no top-level config file at all is left to manifest-builder
    to report on, since it says that better than a version check would.
    """
    try:
        config_file = find_config_file(deploy_config)
    except FileNotFoundError:
        return False
    return config_version(load_toml_file(config_file), config_file) == TARGETS_VERSION


def _generate_selection(
    output: OutputSettings, declares_targets: bool
) -> dict[str, Any]:
    """Return the generate() argument that picks what an output generates."""
    if declares_targets:
        return {"target": output.target_name}
    return {"vars": output.vars}


def _selection_log_summary(selection: dict[str, Any]) -> str:
    target = selection.get("target")
    if target is not None:
        return f"target={target}"
    vars = selection["vars"]
    return f"vars={', '.join(sorted(vars)) if vars else 'none'}"


def _selection_detail(selection: dict[str, Any]) -> dict[str, Any]:
    """Report the chosen target in progress, where vars would be too much."""
    target = selection.get("target")
    return {} if target is None else {"target": target}


def _sorted_refs(
    refs: Iterable[KubernetesObjectRef],
) -> tuple[KubernetesObjectRef, ...]:
    return tuple(sorted(refs, key=_object_ref_sort_key))


def _object_ref_sort_key(ref: KubernetesObjectRef) -> tuple[str, str, str]:
    return (ref.kind, ref.namespace or "", ref.name)


def _format_ref(ref: KubernetesObjectRef) -> str:
    if ref.namespace is None:
        return f"{ref.kind}/{ref.name}"
    return f"{ref.kind}/{ref.namespace}/{ref.name}"


def object_ref_payloads(
    refs: Iterable[KubernetesObjectRef],
) -> list[dict[str, str | None]]:
    """Describe Kubernetes objects for a progress event or an API response.

    Namespace is null for cluster-scoped objects, which is how manifest-builder
    reports them and what tells the two kinds of object apart.
    """
    return [
        {"kind": ref.kind, "namespace": ref.namespace, "name": ref.name} for ref in refs
    ]


def _changed_objects_message(
    output: OutputSettings,
    created_or_modified: Sequence[KubernetesObjectRef],
    removed: Sequence[KubernetesObjectRef],
    deploy_id: str | None,
) -> str:
    parts = []
    if created_or_modified:
        parts.append(
            "created or modified "
            + ", ".join(_format_ref(ref) for ref in created_or_modified)
        )
    if removed:
        parts.append("removed " + ", ".join(_format_ref(ref) for ref in removed))
    cluster_name = _cluster_name(output)
    cluster = f" in cluster {cluster_name}" if cluster_name else ""
    return (
        f"output {output.name}{cluster} {'; '.join(parts)} "
        f"(deploy-id {deploy_id or '<none>'})"
    )


def _cluster_name(output: OutputSettings) -> str | None:
    return output.name if output.connection_type is not None else None


def _start_deployment_detection(
    generation_result: GenerationResult,
    deploy_id: str | None,
    connection: OutputSettings | None,
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
        "starting deployment detection for manifest-builder deploy-id %s in cluster %s",
        deploy_id,
        connection.name if connection is not None else "<injected detector>",
    )
    thread = threading.Thread(
        target=_run_deployment_detection,
        name=f"relcoord-deployment-detection-{deploy_id}",
        kwargs={
            "deploy_id": deploy_id,
            "created_or_modified": created_or_modified,
            "removed": removed,
            "connection": connection,
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
    connection: OutputSettings | None,
    detector: DeploymentDetector | None,
) -> None:
    owned_detector: KubernetesDeploymentDetector | None = None
    if detector is None:
        if connection is None:
            logger.error(
                "deployment detection failed for manifest-builder deploy-id %s: "
                "no cluster and no detector to observe it with",
                deploy_id,
            )
            return
        try:
            owned_detector = KubernetesDeploymentDetector.for_output(connection)
        except Exception:
            logger.exception(
                "deployment detection failed for manifest-builder deploy-id %s: "
                "could not connect to cluster %s",
                deploy_id,
                connection.name,
            )
            return
    active_detector: DeploymentDetector | None = (
        owned_detector if owned_detector is not None else detector
    )
    if active_detector is None:
        logger.error(
            "deployment detection failed for manifest-builder deploy-id %s: "
            "no detector",
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
