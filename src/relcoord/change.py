# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from manifest_builder import generate

from relcoord.config import IdcatSettings
from relcoord.git import GITHUB_TOKEN_USERNAME, github_https_credentials

logger = logging.getLogger(__name__)


class ChangeProcessingError(Exception):
    pass


class DeployConfigError(ChangeProcessingError):
    pass


@dataclass(frozen=True)
class ChangeResult:
    repo: str
    commit: str
    deploy_config: Path
    manifests_checkout: Path
    generated_count: int


@dataclass(frozen=True)
class ChangeProcessor:
    manifests_repository: str
    idcat: IdcatSettings | None = None

    def process(self, repo: str, commit: str) -> ChangeResult:
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
            logger.info(
                "change step 5/7: invoking manifest-builder with deploy config %s",
                deploy_config,
            )
            generated = generate(
                deploy_config,
                manifests_checkout,
                repo_root=Path("/"),
                create_commit=True,
            )
            generated_paths = ", ".join(
                str(path.relative_to(manifests_checkout)) for path in sorted(generated)
            )
            logger.info(
                "change step 5/7: manifest-builder generated %d file(s)%s",
                len(generated),
                f": {generated_paths}" if generated_paths else "",
            )
            manifest_commit = _git_stdout(
                ["rev-parse", "HEAD"],
                cwd=manifests_checkout,
            )
            logger.info(
                "change step 6/7: manifest-builder created manifests commit %s",
                manifest_commit,
            )
            with _git_auth_environment(self.manifests_repository, self.idcat) as env:
                logger.info(
                    "change step 7/7: pushing manifests commit %s to %s",
                    manifest_commit,
                    self.manifests_repository,
                )
                _run_git(["push"], cwd=manifests_checkout, env=env)
            logger.info(
                "change complete: pushed manifests commit %s for source repo %s at commit %s",
                manifest_commit,
                repo,
                commit,
            )
            return ChangeResult(
                repo=repo,
                commit=commit,
                deploy_config=deploy_config,
                manifests_checkout=manifests_checkout,
                generated_count=len(generated),
            )
        except ChangeProcessingError:
            raise
        except Exception as exc:
            raise ChangeProcessingError(str(exc)) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _checkout_commit(
    source: str, commit: str, target: Path, idcat: IdcatSettings | None
) -> None:
    _clone_repository(source, target, idcat, no_checkout=True)
    _run_git(["fetch", "--depth", "1", "origin", commit], cwd=target)
    _run_git(["checkout", "--detach", commit], cwd=target)


def _clone_repository(
    source: str,
    target: Path,
    idcat: IdcatSettings | None,
    *,
    depth: str | None = None,
    no_checkout: bool = False,
) -> None:
    with _git_auth_environment(source, idcat) as env:
        args = ["clone"]
        if depth is not None:
            args.extend(["--depth", depth])
        if no_checkout:
            args.append("--no-checkout")
        args.extend([source, str(target)])
        _run_git(args, env=env)


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        _log_git_output(args, result)
        return result
    except subprocess.CalledProcessError as exc:
        _log_git_output(args, exc)
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ChangeProcessingError(f"git {' '.join(args)} failed: {message}") from exc


def _git_stdout(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    return _run_git(args, cwd=cwd, env=env).stdout.strip()


def _log_git_output(
    args: list[str],
    result: subprocess.CompletedProcess[str] | subprocess.CalledProcessError,
) -> None:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        logger.debug("git %s stdout: %s", " ".join(args), stdout)
    if stderr:
        logger.debug("git %s stderr: %s", " ".join(args), stderr)


class _git_auth_environment:
    def __init__(self, source: str, idcat: IdcatSettings | None) -> None:
        self._source = source
        self._idcat = idcat
        self._tmpdir: Path | None = None
        self._env: dict[str, str] | None = None

    def __enter__(self) -> dict[str, str] | None:
        credentials = github_https_credentials(self._source, self._idcat)
        if credentials.username is None:
            return None

        tmpdir = Path(tempfile.mkdtemp(prefix="relcoord-git-askpass-"))
        askpass = tmpdir / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "*Username*) printf '%s\\n' \"$RELCOORD_GIT_USERNAME\" ;;\n"
            "*Password*) printf '%s\\n' \"$RELCOORD_GIT_PASSWORD\" ;;\n"
            "*) printf '\\n' ;;\n"
            "esac\n"
        )
        askpass.chmod(askpass.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "RELCOORD_GIT_USERNAME": credentials.username or GITHUB_TOKEN_USERNAME,
                "RELCOORD_GIT_PASSWORD": credentials.password or "",
            }
        )
        self._tmpdir = tmpdir
        self._env = env
        return env

    def __exit__(self, *args: object) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
