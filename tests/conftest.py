# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True, scope="session")
def git_config_without_the_developers(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep the tests away from the git config of whoever is running them.

    dulwich reads the global config the way git does, so a developer with
    commit.gpgsign set has every commit a test makes go to gpg-agent for a
    passphrase and a hardware key, which either hangs the run or fails it.
    Pointing git's config environment variables at an empty file keeps the rest
    of a personal config out of the tests too; every test that commits passes
    its own author and committer.
    """
    empty_config = tmp_path_factory.mktemp("gitconfig") / "config"
    empty_config.touch()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty_config))
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
        yield


@pytest.fixture(autouse=True)
def clear_installation_token_cache() -> Iterator[None]:
    from relcoord.git import installation_token_cache

    installation_token_cache.clear()
    yield
    installation_token_cache.clear()
