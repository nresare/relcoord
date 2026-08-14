# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, cast

from dulwich import porcelain
from dulwich.objects import Commit, ObjectID
from dulwich.repo import Repo
from manifest_builder import ExternalPlugins, GenerationResult, generate
from manifest_builder.config import (
    TARGETS_VERSION,
    config_version,
    find_config_file,
    load_toml_file,
)

from relcoord.config import IdcatSettings, OutputSettings, RolloutSettings
from relcoord.git import GitCredentialError, GitCredentials, github_https_credentials
from relcoord.github import GithubCommentError, GithubIssueCommenter, IssueCommenter
from relcoord.kubernetes import KubernetesDeploymentDetector, KubernetesObjectRef
from relcoord.manifest_diff import (
    DiffSection,
    ManifestDiff,
    build_comment_body,
    comment_marker,
    manifest_diff,
)

logger = logging.getLogger(__name__)

# relcoord has nowhere to upload an artifact to, so the full diff travels back
# with the response that asked for the comment.
FULL_DIFF_REFERENCE = "returned in the relcoord response for this request"

# Subdirectory of the configured plugins repository holding the plugin modules,
# matching the layout manifest-builder expects of a config directory.
PLUGINS_DIRECTORY = "plugins"

# How much of a commit hash a progress message shows, matching what git prints.
SHORT_COMMIT_LENGTH = 7

# How many Kubernetes objects a progress message names before summarising the
# rest as a count. A change to a shared label rewrites everything that carries
# it, and a line listing hundreds of objects is one nobody reads.
MAX_REPORTED_OBJECTS = 3


class ChangeProcessingError(Exception):
    pass


class DeployConfigError(ChangeProcessingError):
    pass


class PluginsRepositoryError(ChangeProcessingError):
    """Raised when the configured plugins repository has no plugins directory.

    That is a relcoord configuration mistake rather than anything the change
    request did, so it is worth naming the repository the operator configured;
    manifest-builder would otherwise fail naming only a temporary path.
    """


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


class RolloutStageError(DeploymentDetectionError):
    """Raised when a rollout stage's deployment was not observed.

    What the stage pushed stays pushed and the stages after it are not started,
    so this reports on the deployment rather than on a fault in relcoord, and is
    reported without a stack trace.
    """


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
    rollout: str | None = None
    """Rollout that deployed this output, absent without [[rollout]] entries."""
    stage: int | None = None
    """One-based position of this output's stage within its rollout."""


@dataclass(frozen=True)
class ChangeStage:
    """One step of the plan a change is deployed in.

    ``rollout`` is None for the single stage of a deployment that configures no
    rollouts, where every output is deployed together and nothing gates anything.
    """

    outputs: tuple[OutputSettings, ...]
    rollout: str | None = None
    index: int = 1
    count: int = 1

    @property
    def label(self) -> str:
        """How a stage is referred to once its rollout has been named."""
        if self.rollout is None:
            return "all outputs"
        return f"stage {self.index} of {self.count}"


@dataclass(frozen=True)
class ChangeProcessor:
    manifests_repository: str | None = None
    plugins_repository: str | None = None
    """Repository whose ``plugins`` directory parses non-system config."""
    outputs: Sequence[OutputSettings] = ()
    rollouts: Sequence[RolloutSettings] = ()
    """Pipelines the outputs are deployed in, one stage at a time."""
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
            # The workspace is a temporary directory nobody watching a deployment
            # cares about, so it is logged for whoever debugs a change and left
            # out of the stream.
            logger.info(
                "change step 1/7: created temporary workspace %s for repo %s "
                "at commit %s",
                workdir,
                repo,
                commit,
            )
            logger.info(
                "change step 2/7: checking out source repo %s at commit %s",
                repo,
                commit,
            )
            report(
                "source-checkout",
                f"checking out {short_repo(repo)} at {short_commit(commit)}",
                repo=repo,
                commit=commit,
            )
            _checkout_commit(repo, commit, source_checkout, self.idcat)
            deploy_config, namespace = _deploy_config_and_namespace(
                source_checkout, repo, commit, config_path, system
            )
            declares_targets = _declares_targets(deploy_config)
            logger.info(
                "change step 3/7: found deploy config at %s (system mode: %s, "
                "targets: %s)",
                deploy_config,
                system,
                declares_targets,
            )
            report(
                "deploy-config",
                _deploy_config_message(namespace, config_path, system),
                deploy_config=str(deploy_config),
                system=system,
                namespace=namespace,
                targets=declares_targets,
            )
            plugins = _external_plugins(
                self.plugins_repository,
                workdir,
                system,
                self.idcat,
                report,
                step="change step 3/7",
            )

            output_results: list[OutputResult] = []
            total_generated = 0
            cloned: set[str] = set()

            for stage in _change_stages(output_settings, self.rollouts):
                _report_stage(stage, report)
                stage_results: list[tuple[OutputSettings, GenerationResult]] = []

                for repository, outputs in _outputs_by_repository(stage.outputs):
                    manifests_checkout = checkout_by_repository[repository]
                    if repository not in cloned:
                        self._clone_manifests(repository, manifests_checkout, report)
                        cloned.add(repository)
                    generation_results: list[GenerationResult] = []

                    for output in outputs:
                        output_result, generation_result = self._generate_output(
                            output,
                            stage,
                            deploy_config=deploy_config,
                            manifests_checkout=manifests_checkout,
                            declares_targets=declares_targets,
                            image=image,
                            namespace=namespace,
                            plugins=plugins,
                            report=report,
                        )
                        output_results.append(output_result)
                        generation_results.append(generation_result)
                        stage_results.append((output, generation_result))
                        total_generated += output_result.generated_count

                    if not any(
                        result.created_or_modified or result.removed
                        for result in generation_results
                    ):
                        logger.info(
                            "change step 6/7: manifest-builder produced no changes "
                            "for repo %s; nothing to commit or push",
                            repository,
                        )
                        report(
                            "no-changes",
                            f"no changes for {short_repo(repository)}",
                            repository=repository,
                        )
                        continue

                    self._push_manifests(
                        repository, manifests_checkout, repo, commit, report
                    )

                if self.detect_deployment:
                    self._detect_stage_deployments(stage, stage_results, report)
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

    def _clone_manifests(
        self,
        repository: str,
        manifests_checkout: Path,
        report: Callable[..., None],
    ) -> None:
        """Check out a manifests repository, once per change.

        The stages of a rollout generate into the same checkout, each committing
        on top of what the stage before it committed, and push what they added.
        """
        logger.info(
            "change step 4/7: checking out manifests repo %s into %s",
            repository,
            manifests_checkout,
        )
        report(
            "manifests-checkout",
            f"checking out {short_repo(repository)}",
            repository=repository,
        )
        _clone_repository(
            repository,
            manifests_checkout,
            self.idcat,
            purpose=f"cloning manifests repo {repository}",
            depth="1",
        )

    def _generate_output(
        self,
        output: OutputSettings,
        stage: ChangeStage,
        *,
        deploy_config: Path,
        manifests_checkout: Path,
        declares_targets: bool,
        image: str | None,
        namespace: str | None,
        plugins: ExternalPlugins | None,
        report: Callable[..., None],
    ) -> tuple[OutputResult, GenerationResult]:
        """Let manifest-builder write one output into the manifests checkout."""
        repo_root = Path("/")
        create_commit = True
        output_path = manifests_checkout / output.directory
        output_path.mkdir(parents=True, exist_ok=True)
        selection = _generate_selection(output, declares_targets)
        logger.info(
            "change step 5/7: invoking manifest-builder generate("
            "output=%s, deploy_config=%s, manifests_checkout=%s, "
            "repo_root=%s, create_commit=%s, image=%s, namespace=%s, "
            "plugins=%s, %s)",
            output.name,
            deploy_config,
            output_path,
            repo_root,
            create_commit,
            image,
            namespace,
            _plugins_log_summary(plugins),
            _selection_log_summary(selection),
        )
        report(
            "generate",
            _generating_message(output, declares_targets),
            output=output.name,
            repository=output.repository,
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
            plugins=plugins,
            **selection,
        )
        generated = _written_paths(generation_result)
        deploy_id = _deploy_id(generation_result)
        if self.detect_deployment and deploy_id is None:
            raise DeploymentDetectionError(
                "manifest-builder did not return a deploy_id; "
                "deployment detection requires git-backed generation"
            )
        created_or_modified = _sorted_refs(generation_result.created_or_modified)
        removed_refs = _sorted_refs(generation_result.removed)
        logger.info(
            "change step 5/7: manifest-builder generated %d file(s) for output %s",
            len(generated),
            output.name,
        )
        report(
            "generated",
            _generated_message(
                output, len(generated), created_or_modified, removed_refs
            ),
            output=output.name,
            repository=output.repository,
            generated=len(generated),
            changed=len(created_or_modified),
            removed=len(removed_refs),
        )
        if created_or_modified or removed_refs:
            logger.info(
                "change step 5/7: output %s in cluster %s changed %s (deploy-id %s)",
                output.name,
                _cluster_name(output) or "<none>",
                ", ".join(
                    _format_ref(ref) for ref in (*created_or_modified, *removed_refs)
                ),
                deploy_id or "<none>",
            )
            report(
                "changed-objects",
                _changed_objects_message(output, created_or_modified, removed_refs),
                output=output.name,
                repository=output.repository,
                cluster=_cluster_name(output),
                deploy_id=deploy_id,
                created_or_modified=object_ref_payloads(created_or_modified),
                removed=object_ref_payloads(removed_refs),
            )
        return (
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
                rollout=stage.rollout,
                stage=stage.index if stage.rollout is not None else None,
            ),
            generation_result,
        )

    def _push_manifests(
        self,
        repository: str,
        manifests_checkout: Path,
        repo: str,
        commit: str,
        report: Callable[..., None],
    ) -> None:
        """Push what manifest-builder committed for one stage of one repository."""
        manifest_commit = _head_commit(manifests_checkout)
        # The commit is named by the push lines either side of it, so it is logged
        # rather than reported: one action is worth one line in the stream.
        logger.info(
            "change step 6/7: manifest-builder created manifests commit %s",
            manifest_commit,
        )
        short = short_commit(manifest_commit)
        target = short_repo(repository)
        logger.info(
            "change step 7/7: pushing manifests commit %s to %s",
            manifest_commit,
            repository,
        )
        report(
            "push",
            f"pushing {short} to {target}",
            repository=repository,
            manifest_commit=manifest_commit,
        )
        _push_repository(
            manifests_checkout,
            repository,
            self.idcat,
        )
        logger.info(
            "change complete: pushed manifests commit %s for source repo %s "
            "at commit %s",
            manifest_commit,
            repo,
            commit,
        )
        report(
            "pushed",
            f"pushed {short} to {target}",
            repository=repository,
            manifest_commit=manifest_commit,
        )

    def _detect_stage_deployments(
        self,
        stage: ChangeStage,
        results: Sequence[tuple[OutputSettings, GenerationResult]],
        report: Callable[..., None],
    ) -> None:
        """Follow a stage's pushes into the clusters they were pushed to.

        A rollout waits here, because the stages after this one are gated on what
        it observes: a deployment that never materialises has to stop the change
        rather than only reach the log. Without a rollout there is nothing to
        gate, so detection runs in the background as it always has and the change
        answers as soon as the manifests are pushed.

        An output the commit did not affect generated nothing, so there is no
        deployment of it to wait for and its cluster is left alone.
        """
        deployed = [
            (output, result)
            for output, result in results
            if result.created_or_modified or result.removed
        ]
        observed: list[tuple[str, float]] = []
        for output, generation_result in deployed:
            deploy_id = _deploy_id(generation_result)
            connection = self._connection_for(output)
            report(
                "deployment-detection",
                f"waiting for {output.name} to pick up the change",
                deploy_id=deploy_id,
                output=output.name,
                cluster=output.name,
            )
            if stage.rollout is None:
                _start_deployment_detection(
                    generation_result,
                    deploy_id,
                    connection,
                    self.deployment_detector,
                )
                continue
            started = time.monotonic()
            _await_deployment_detection(
                generation_result,
                deploy_id,
                connection,
                self.deployment_detector,
            )
            observed.append((output.name, time.monotonic() - started))
        if stage.rollout is None:
            return
        message = _stage_verified_message(stage, observed)
        logger.info("change step 7/7: %s", message)
        report(
            "rollout-stage-verified",
            message,
            rollout=stage.rollout,
            stage=stage.index,
            stages=stage.count,
            outputs=[name for name, _ in observed],
        )

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
    updated: bool = False


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

    Every configured output is generated, because which of them a commit affects
    is not something the commit says: it is what generating shows. The comment
    then reports the manifests repositories that changed, so a diff of a section
    only one cluster is built from reads as that cluster's diff rather than as a
    wall of unchanged clusters.
    """

    manifests_repository: str | None = None
    plugins_repository: str | None = None
    """Repository whose ``plugins`` directory parses non-system config."""
    outputs: Sequence[OutputSettings] = ()
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
            output_settings = _resolve_output_settings(
                self.outputs, self.manifests_repository
            )
            checkout_by_repository = _checkout_paths_by_repository(
                workdir, output_settings
            )
            logger.info(
                "diff step 1/6: created temporary workspace %s for repo %s "
                "at commit %s",
                workdir,
                repo,
                commit,
            )
            logger.info(
                "diff step 2/6: checking out source repo %s at commit %s", repo, commit
            )
            report(
                "source-checkout",
                f"checking out {short_repo(repo)} at {short_commit(commit)}",
                repo=repo,
                commit=commit,
            )
            _checkout_commit(repo, commit, source_checkout, self.idcat)
            deploy_config, namespace = _deploy_config_and_namespace(
                source_checkout, repo, commit, config_path, system
            )
            declares_targets = _declares_targets(deploy_config)
            logger.info(
                "diff step 3/6: found deploy config at %s (system mode: %s, "
                "targets: %s)",
                deploy_config,
                system,
                declares_targets,
            )
            report(
                "deploy-config",
                _deploy_config_message(namespace, config_path, system),
                deploy_config=str(deploy_config),
                system=system,
                namespace=namespace,
                targets=declares_targets,
            )
            plugins = _external_plugins(
                self.plugins_repository,
                workdir,
                system,
                self.idcat,
                report,
                step="diff step 3/6",
            )

            output_diffs: list[OutputDiff] = []
            repository_diffs: list[RepositoryDiff] = []
            total_generated = 0

            for repository, manifests_checkout in checkout_by_repository.items():
                logger.info(
                    "diff step 4/6: checking out manifests repo %s into %s",
                    repository,
                    manifests_checkout,
                )
                report(
                    "manifests-checkout",
                    f"checking out {short_repo(repository)}",
                    repository=repository,
                )
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
                        "namespace=%s, plugins=%s, %s)",
                        output.name,
                        deploy_config,
                        output_path,
                        namespace,
                        _plugins_log_summary(plugins),
                        _selection_log_summary(selection),
                    )
                    report(
                        "generate",
                        _generating_message(output, declares_targets),
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
                        plugins=plugins,
                        **selection,
                    )
                    generated = _written_paths(generation_result)
                    logger.info(
                        "diff step 5/6: manifest-builder generated %d file(s) "
                        "for output %s",
                        len(generated),
                        output.name,
                    )
                    report(
                        "generated",
                        _generated_message(
                            output,
                            len(generated),
                            _sorted_refs(generation_result.created_or_modified),
                            _sorted_refs(generation_result.removed),
                        ),
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
                    files = "file" if changed_files == 1 else "files"
                    logger.info(
                        "diff step 6/6: manifest-builder changed %d file(s) in repo %s",
                        changed_files,
                        repository,
                    )
                    report(
                        "diff",
                        f"{short_repo(repository)}: {changed_files} {files} changed",
                        repository=repository,
                        changed=changed_files,
                    )
                else:
                    logger.info(
                        "diff step 6/6: manifest-builder produced no changes for repo %s",
                        repository,
                    )
                    report(
                        "no-changes",
                        f"no changes for {short_repo(repository)}",
                        repository=repository,
                    )

            marker = comment_marker()
            comment_body = build_comment_body(
                _diff_sections(repository_diffs),
                full_diff_reference=FULL_DIFF_REFERENCE,
                marker=marker,
            )
            comment = self._comment(
                repo, pull_request, comment_body.body, marker, report
            )
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
        marker: str,
        report: Callable[..., None],
    ) -> DiffComment:
        if pull_request is None:
            logger.info(
                "diff complete: no pull request requested; not posting a manifest "
                "diff comment for repo %s",
                repo,
            )
            report(
                "no-comment",
                "no pull request to comment on; returning the diff instead",
                repo=repo,
            )
            return DiffComment(body=body, posted=False)

        commenter = (
            self.commenter
            if self.commenter is not None
            else GithubIssueCommenter(idcat=self.idcat)
        )
        logger.info(
            "diff step 6/6: posting manifest diff comment to %s pull request #%s",
            repo,
            pull_request,
        )
        report(
            "comment",
            f"commenting on {short_repo(repo)} pull request #{pull_request}",
            repo=repo,
            pull_request=pull_request,
        )
        try:
            posted = commenter.post_comment(repo, pull_request, body, marker=marker)
        except GitCredentialError as exc:
            raise CredentialError(
                "failed to obtain git credentials while posting a manifest diff "
                f"comment to {repo} pull request #{pull_request}: {exc}"
            ) from exc
        except GithubCommentError as exc:
            raise CommentPostError(str(exc)) from exc

        verb = "updated" if posted.updated else "posted"
        logger.info(
            "diff complete: %s manifest diff comment on %s pull request #%s",
            verb,
            repo,
            pull_request,
        )
        report(
            "commented",
            f"commented on {short_repo(repo)} pull request #{pull_request}",
            repo=repo,
            pull_request=pull_request,
            url=posted.url,
            updated=posted.updated,
        )
        return DiffComment(
            body=body, posted=True, url=posted.url, updated=posted.updated
        )


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
    """Pair each affected repository's diff with the heading to render it under.

    A repository the commit generates no change for has nothing to show, so it
    is left out rather than headed by a line saying so: what a reviewer wants is
    the sections the change actually reaches. A comment left with a single
    repository renders without a heading at all, which is what a deployment with
    one manifests repository — the usual configuration — always gets.
    """
    changed = [entry for entry in diffs if entry.manifest_diff.diff.strip()]
    if not changed:
        return ()
    if len(changed) == 1:
        return (DiffSection(heading=None, diff=changed[0].manifest_diff),)
    return tuple(
        DiffSection(heading=entry.repository, diff=entry.manifest_diff)
        for entry in changed
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


def _outputs_by_repository(
    outputs: Sequence[OutputSettings],
) -> tuple[tuple[str, tuple[OutputSettings, ...]], ...]:
    """Group outputs by the manifests repository they are generated into.

    One checkout serves every output of a repository, and one push sends what
    they generated, so a change works a repository at a time within each stage.
    """
    repositories = list(dict.fromkeys(output.repository for output in outputs))
    return tuple(
        (repository, _outputs_for_repository(outputs, repository))
        for repository in repositories
    )


def _report_stage(stage: ChangeStage, report: Callable[..., None]) -> None:
    """Announce a rollout stage, where there is a rollout to announce it for."""
    if stage.rollout is None:
        return
    names = join_names([output.name for output in stage.outputs])
    message = f"rollout {stage.rollout}, {stage.label}: {names}"
    logger.info("change step 4/7: %s", message)
    report(
        "rollout-stage",
        message,
        rollout=stage.rollout,
        stage=stage.index,
        stages=stage.count,
        outputs=[output.name for output in stage.outputs],
    )


def _change_stages(
    outputs: Sequence[OutputSettings], rollouts: Sequence[RolloutSettings]
) -> tuple[ChangeStage, ...]:
    """Order the outputs into the stages a change deploys them in.

    Rollouts are walked in the order they are configured, one at a time, so that
    a change deploys and verifies in an order the configuration states rather
    than one that depends on how the work happens to interleave.
    """
    if not rollouts:
        return (ChangeStage(outputs=tuple(outputs)),)
    by_name = {output.name: output for output in outputs}
    stages: list[ChangeStage] = []
    for rollout in rollouts:
        for index, stage in enumerate(rollout.stages, start=1):
            missing = [name for name in stage.outputs if name not in by_name]
            if missing:
                raise ChangeProcessingError(
                    f"rollout '{rollout.name}' stage {index} names unconfigured "
                    f"output(s) {', '.join(missing)}"
                )
            stages.append(
                ChangeStage(
                    outputs=tuple(by_name[name] for name in stage.outputs),
                    rollout=rollout.name,
                    index=index,
                    count=len(rollout.stages),
                )
            )
    return tuple(stages)


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


def _plugins_log_summary(plugins: ExternalPlugins | None) -> str:
    return "none" if plugins is None else plugins.source


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


def short_repo(repo: str) -> str:
    """Name a repository the way someone talking about it would: owner/name.

    The scheme and host are the same for every repository relcoord touches, so
    they are noise in a line meant to be read. The full URL stays in the detail
    of the event and in the log.
    """
    trimmed = repo.rstrip("/").removesuffix(".git")
    path = trimmed.rpartition("://")[2]
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else path


def short_commit(commit: str) -> str:
    """Abbreviate a commit to the length git itself shows."""
    return commit[:SHORT_COMMIT_LENGTH]


def join_names(names: Sequence[str]) -> str:
    """Join names as a sentence does, so a list of two reads as a pair."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _describe_ref(ref: KubernetesObjectRef) -> str:
    """Name a Kubernetes object as kubectl would talk about it."""
    if ref.namespace is None:
        return f"{ref.kind} {ref.name}"
    return f"{ref.kind} {ref.namespace}/{ref.name}"


def _describe_refs(refs: Sequence[KubernetesObjectRef]) -> str:
    """List objects, cut short where a change touched more than a reader wants.

    Every object is in the detail of the event, so the cut costs nothing but the
    line's length.
    """
    shown = ", ".join(_describe_ref(ref) for ref in refs[:MAX_REPORTED_OBJECTS])
    remaining = len(refs) - MAX_REPORTED_OBJECTS
    return shown if remaining <= 0 else f"{shown} and {remaining} more"


def _deploy_config_message(
    namespace: str | None, config_path: str, system: bool
) -> str:
    """Say what the config commit is being generated as, rather than from where.

    The directory it was found in is a path inside a temporary workspace, so what
    is worth reading is the owner the manifests are generated as: the whole
    repository as system, or one namespace from its own config directory.
    """
    if system:
        return "generating as system from the repository root"
    return f"generating for namespace {namespace} from {config_path}"


def _stage_verified_message(
    stage: ChangeStage, observed: Sequence[tuple[str, float]]
) -> str:
    """Report what a stage's gate observed, or that it had nothing to wait for."""
    if not observed:
        names = join_names([output.name for output in stage.outputs])
        return f"{stage.label} skipped: {names} unaffected"
    if len(observed) == 1:
        name, seconds = observed[0]
        return f"{stage.label} verified: {name} picked it up after {seconds:.1f}s"
    picked_up = join_names(
        [f"{name} after {seconds:.1f}s" for name, seconds in observed]
    )
    return f"{stage.label} verified: picked up by {picked_up}"


def _generating_message(output: OutputSettings, declares_targets: bool) -> str:
    """Say what is being generated, in the terms the config repository uses."""
    if not declares_targets:
        return f"generating manifests for output {output.name}"
    target = output.target_name
    if target == output.name:
        return f"generating manifests for target {target}"
    return f"generating manifests for target {target} into {output.name}"


def _generated_message(
    output: OutputSettings,
    generated: int,
    created_or_modified: Sequence[KubernetesObjectRef],
    removed: Sequence[KubernetesObjectRef],
) -> str:
    """Report what a generation changed, rather than how much it wrote.

    An output is generated in full every time, so the count of files written says
    how large the target is and not what the commit did to it. The count that
    answers the second question leads, with the first one behind it as the
    reassurance that the whole target was rendered.
    """
    if not created_or_modified and not removed:
        return f"{output.name}: none of {generated} manifests changed"
    parts = [f"{len(created_or_modified)} of {generated} manifests changed"]
    if removed:
        parts.append(f"{len(removed)} removed")
    return f"{output.name}: {', '.join(parts)}"


def _changed_objects_message(
    output: OutputSettings,
    created_or_modified: Sequence[KubernetesObjectRef],
    removed: Sequence[KubernetesObjectRef],
) -> str:
    parts = []
    if created_or_modified:
        parts.append(f"updated {_describe_refs(created_or_modified)}")
    if removed:
        parts.append(f"removed {_describe_refs(removed)}")
    return f"{output.name}: {'; '.join(parts)}"


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


def _await_deployment_detection(
    generation_result: GenerationResult,
    deploy_id: str | None,
    connection: OutputSettings | None,
    detector: DeploymentDetector | None,
) -> None:
    """Wait for a deployment, raising when it does not arrive.

    This is the gate between the stages of a rollout, so unlike the background
    detection of a change without one, a failure is reported to the caller: it
    is what stops the stages after this one from being pushed.
    """
    if deploy_id is None:
        raise DeploymentDetectionError(
            "manifest-builder did not return a deploy_id; "
            "deployment detection requires git-backed generation"
        )
    owned_detector: KubernetesDeploymentDetector | None = None
    active_detector: DeploymentDetector
    if detector is not None:
        active_detector = detector
    else:
        if connection is None:
            raise DeploymentDetectionError(
                f"deployment detection for manifest-builder deploy-id {deploy_id} "
                "has no cluster and no detector to observe it with"
            )
        try:
            owned_detector = KubernetesDeploymentDetector.for_output(connection)
        except Exception as exc:
            raise RolloutStageError(
                f"could not connect to cluster {connection.name} to detect the "
                f"deployment of manifest-builder deploy-id {deploy_id}: {exc}"
            ) from exc
        active_detector = owned_detector
    logger.info(
        "waiting for deployment of manifest-builder deploy-id %s in cluster %s",
        deploy_id,
        connection.name if connection is not None else "<injected detector>",
    )
    try:
        active_detector.wait_for_success(
            deploy_id=deploy_id,
            created_or_modified=set(generation_result.created_or_modified),
            removed=set(generation_result.removed),
        )
    except Exception as exc:
        raise RolloutStageError(
            f"deployment of manifest-builder deploy-id {deploy_id} was not "
            f"observed: {exc}"
        ) from exc
    else:
        logger.info(
            "deployment detected for manifest-builder deploy-id %s",
            deploy_id,
        )
    finally:
        if owned_detector is not None:
            owned_detector.close()


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


def _external_plugins(
    plugins_repository: str | None,
    workdir: Path,
    system: bool,
    idcat: IdcatSettings | None,
    report: Callable[..., None],
    *,
    step: str,
) -> ExternalPlugins | None:
    """Check out the configured plugins repository, for a non-system request.

    System-mode config comes from a repository that carries its own plugins, so
    the configured repository is only consulted for the ordinary case where it
    does not.
    """
    if plugins_repository is None or system:
        return None
    plugins = _checkout_plugins(plugins_repository, workdir / "plugins", idcat)
    logger.info("%s: checked out plugins from %s", step, plugins.source)
    _, _, plugins_commit = plugins.source.rpartition("@")
    report(
        "plugins-checkout",
        f"using plugins from {short_repo(plugins_repository)} "
        f"at {short_commit(plugins_commit)}",
        repository=plugins_repository,
        source=plugins.source,
    )
    return plugins


def _checkout_plugins(
    repository: str, target: Path, idcat: IdcatSettings | None
) -> ExternalPlugins:
    """Check out the plugins repository and describe it for manifest-builder.

    The latest commit on the default branch is what gets used, and its hash goes
    into ``source`` so the ``Plugins from:`` line of a generated commit records
    exactly which plugins produced those manifests.
    """
    _clone_repository(
        repository,
        target,
        idcat,
        purpose=f"cloning plugins repo {repository}",
        depth="1",
    )
    commit = _head_commit(target)
    path = target / PLUGINS_DIRECTORY
    if not path.is_dir():
        raise PluginsRepositoryError(
            f"plugins repo {repository} at commit {commit} has no "
            f"{PLUGINS_DIRECTORY}/ directory"
        )
    return ExternalPlugins(path=path, source=f"{repository}@{commit}")


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
