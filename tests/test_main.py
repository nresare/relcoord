# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import logging

import pytest

from relcoord.change import ChangeProcessor
from relcoord.config import PersistenceSettings, Settings
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.main import configure_logging, make_change_processor, make_store


def test_configure_logging_sets_info_level() -> None:
    configure_logging()

    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_make_change_processor_requires_manifests_repository() -> None:
    with pytest.raises(
        RuntimeError,
        match="manifests-repository must be configured at the top level",
    ):
        make_change_processor(Settings())


def test_make_change_processor_uses_manifests_repository() -> None:
    processor = make_change_processor(
        Settings(manifests_repository="https://github.com/acme/manifests.git")
    )

    assert isinstance(processor, ChangeProcessor)
    assert processor.manifests_repository == "https://github.com/acme/manifests.git"


def test_make_store_uses_in_memory_backend() -> None:
    store = asyncio.run(
        make_store(Settings(persistence=PersistenceSettings(backend="in-memory")))
    )

    assert isinstance(store, InMemoryImageInfoStore)
