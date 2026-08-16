# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from relcoord.change import ChangeProcessor
from relcoord.config import OutputSettings, PersistenceSettings, Settings
from relcoord.dynamodb_store import DynamoDBImageInfoStore
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.main import (
    HTTP_CLIENT_LOGGERS,
    configure_logging,
    make_change_processor,
    make_diff_processor,
    make_store,
)
from relcoord.retrying_store import RetryingImageInfoStore


def test_make_change_processor_requires_manifests_repository() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"manifests-repository or at least one \[\[output\]\]",
    ):
        make_change_processor(Settings())


def test_make_change_processor_uses_manifests_repository() -> None:
    processor = make_change_processor(
        Settings(
            manifests_repository="https://github.com/acme/manifests.git",
            detect_deployment=True,
        )
    )

    assert isinstance(processor, ChangeProcessor)
    assert processor.manifests_repository == "https://github.com/acme/manifests.git"
    assert processor.detect_deployment is True


def test_processors_use_system_repository() -> None:
    settings = Settings(
        manifests_repository="https://github.com/acme/manifests.git",
        system_repository="https://github.com/acme/system.git",
    )

    change_processor = make_change_processor(settings)
    diff_processor = make_diff_processor(settings)

    assert change_processor.system_repository == "https://github.com/acme/system.git"
    assert diff_processor.system_repository == "https://github.com/acme/system.git"


def test_make_change_processor_uses_outputs() -> None:
    processor = make_change_processor(
        Settings(
            outputs=[
                OutputSettings(
                    name="example-dev",
                    repository="https://github.com/acme/manifests.git",
                    directory=Path("example-dev"),
                    vars={"cluster_name": "example-dev"},
                )
            ]
        )
    )

    assert isinstance(processor, ChangeProcessor)
    assert processor.manifests_repository is None
    assert processor.outputs[0].name == "example-dev"


def test_make_store_uses_in_memory_backend() -> None:
    store = asyncio.run(
        make_store(Settings(persistence=PersistenceSettings(backend="in-memory")))
    )

    assert isinstance(store, RetryingImageInfoStore)
    assert isinstance(store.wrapped_store, InMemoryImageInfoStore)


def test_make_store_uses_dynamodb_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = InMemoryImageInfoStore()

    async def connect(config: PersistenceSettings) -> InMemoryImageInfoStore:
        assert config.table_name == "relcoord-image-versions"
        return expected

    monkeypatch.setattr(DynamoDBImageInfoStore, "connect", connect)

    store = asyncio.run(
        make_store(
            Settings(
                persistence=PersistenceSettings(
                    backend="dynamodb",
                    table_name="relcoord-image-versions",
                )
            )
        )
    )

    assert isinstance(store, RetryingImageInfoStore)
    assert store.wrapped_store is expected


def test_configure_logging_uses_configured_log_level() -> None:
    root_logger = logging.getLogger()
    original_level = root_logger.level

    try:
        configure_logging("WARNING")

        assert root_logger.level == logging.WARNING
    finally:
        root_logger.setLevel(original_level)


@contextmanager
def restored_log_levels() -> Iterator[None]:
    loggers = [
        logging.getLogger(),
        *(logging.getLogger(n) for n in HTTP_CLIENT_LOGGERS),
    ]
    original_levels = [logger.level for logger in loggers]
    try:
        yield
    finally:
        for logger, level in zip(loggers, original_levels, strict=True):
            logger.setLevel(level)


def test_configure_logging_quietens_the_http_clients() -> None:
    with restored_log_levels():
        configure_logging("INFO")

        # A line per request to every API relcoord talks to, otherwise.
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_leaves_the_http_clients_alone_at_debug() -> None:
    with restored_log_levels():
        logging.getLogger("httpx").setLevel(logging.NOTSET)

        configure_logging("DEBUG")

        # Asking for debug is asking to see the traffic.
        assert logging.getLogger("httpx").level == logging.NOTSET


def test_configure_logging_does_not_make_the_http_clients_louder() -> None:
    with restored_log_levels():
        configure_logging("ERROR")

        # WARNING here would out-shout a root logger set to ERROR, since a
        # logger with a level of its own is judged by that rather than the root.
        assert logging.getLogger("httpx").level == logging.ERROR
