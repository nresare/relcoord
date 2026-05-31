# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import logging

import click
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from relcoord.app import (
    BearerTokenValidator,
    NoopTokenValidator,
    RequestTokenValidator,
    create_app,
)
from relcoord.auth import TokenValidator
from relcoord.change import ChangeProcessor as ManifestChangeProcessor
from relcoord.config import Settings
from relcoord.dynamodb_store import DynamoDBImageInfoStore
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.store import ImageInfoStore
from relcoord.surreal_store import SurrealImageInfoStore

DEFAULT_CONFIG_PATH = "/config/relcoord.toml"
LOG_FORMAT = "[%(asctime)s] [%(process)d] [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger(__name__)


async def run(settings: Settings, disable_auth: bool) -> None:
    config = HypercornConfig()
    config.bind = [f"{settings.host}:{settings.port}"]
    token_validator = _build_token_validator(settings, disable_auth)
    change_processor = make_change_processor(settings)
    store = await make_store(settings)
    try:
        # This has been raised upstream: https://github.com/pgjones/hypercorn/issues/353
        # noinspection PyTypeChecker
        app = create_app(
            store,
            token_validator=token_validator,
            change_processor=change_processor,
        )
        await serve(app, config)  # ty: ignore[invalid-argument-type]
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            await close()


def _build_token_validator(
    settings: Settings, disable_auth: bool
) -> RequestTokenValidator:
    if disable_auth:
        logger.warning("authentication disabled by --disable-auth")
        return NoopTokenValidator()
    if not settings.roles:
        raise RuntimeError(
            "at least one [[role]] entry is required (or pass --disable-auth)"
        )
    return BearerTokenValidator(TokenValidator(settings.roles))


async def make_store(settings: Settings) -> ImageInfoStore:
    if settings.persistence is None or settings.persistence.backend == "in-memory":
        return InMemoryImageInfoStore()
    if settings.persistence.backend == "dynamodb":
        return await DynamoDBImageInfoStore.connect(settings.persistence)
    return await SurrealImageInfoStore.connect(settings.persistence)


def make_change_processor(
    settings: Settings,
) -> ManifestChangeProcessor:
    if settings.manifests_repository is None:
        raise RuntimeError("manifests-repository must be configured at the top level")
    return ManifestChangeProcessor(
        manifests_repository=settings.manifests_repository,
        idcat=settings.idcat,
        detect_deployment=settings.detect_deployment,
    )


@click.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to the TOML configuration file.",
)
@click.option(
    "--disable-auth",
    is_flag=True,
    default=False,
    help="Disable bearer-token authentication on write endpoints.",
)
def main(config_path: str, disable_auth: bool) -> None:
    settings = Settings.from_toml(config_path)
    configure_logging(settings.log_level)
    asyncio.run(run(settings, disable_auth))


def configure_logging(log_level: str) -> None:
    level = logging.getLevelNamesMapping()[log_level]
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format=LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S %z",
        )


if __name__ == "__main__":
    main()
