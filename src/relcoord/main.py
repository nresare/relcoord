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
from relcoord.change import DiffCommentProcessor
from relcoord.config import Settings
from relcoord.dynamodb_store import DynamoDBImageInfoStore
from relcoord.github import GithubIssueCommenter
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.retrying_store import RetryingImageInfoStore
from relcoord.store import ImageInfoStore
from relcoord.surreal_store import SurrealImageInfoStore
from relcoord.version import version_summary

DEFAULT_CONFIG_PATH = "/config/relcoord.toml"
LOG_FORMAT = "[%(asctime)s] [%(process)d] [%(levelname)s] %(name)s: %(message)s"
# The HTTP client libraries, which log a line for every request relcoord makes.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger(__name__)


async def run(settings: Settings, disable_auth: bool) -> None:
    config = HypercornConfig()
    config.bind = [f"{settings.listen}:{settings.port}"]
    token_validator = _build_token_validator(settings, disable_auth)
    change_processor = make_change_processor(settings)
    diff_processor = make_diff_processor(settings)
    store = await make_store(settings)
    try:
        # This has been raised upstream: https://github.com/pgjones/hypercorn/issues/353
        # noinspection PyTypeChecker
        app = create_app(
            store,
            token_validator=token_validator,
            change_processor=change_processor,
            diff_processor=diff_processor,
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
        return RetryingImageInfoStore(InMemoryImageInfoStore())
    if settings.persistence.backend == "dynamodb":
        return RetryingImageInfoStore(
            await DynamoDBImageInfoStore.connect(settings.persistence)
        )
    return RetryingImageInfoStore(
        await SurrealImageInfoStore.connect(settings.persistence)
    )


def make_change_processor(
    settings: Settings,
) -> ManifestChangeProcessor:
    if settings.manifests_repository is None and not settings.outputs:
        raise RuntimeError(
            "manifests-repository or at least one [[output]] entry must be configured"
        )
    return ManifestChangeProcessor(
        manifests_repository=settings.manifests_repository,
        system_repository=settings.system_repository,
        outputs=settings.outputs,
        rollouts=settings.rollouts,
        idcat=settings.idcat,
        detect_deployment=settings.detect_deployment,
    )


def make_diff_processor(settings: Settings) -> DiffCommentProcessor:
    return DiffCommentProcessor(
        manifests_repository=settings.manifests_repository,
        system_repository=settings.system_repository,
        outputs=settings.outputs,
        idcat=settings.idcat,
        commenter=GithubIssueCommenter(idcat=settings.idcat),
    )


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Report both versions and exit, before --config is looked for.

    The option is eager so that ``--version`` answers wherever it is run: the
    default config path only exists in the container, and click would otherwise
    reject the invocation for the missing file before printing anything.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(version_summary())
    ctx.exit()


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
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_version,
    help="Print the running release and the manifest-builder it uses, and exit.",
)
def main(config_path: str, disable_auth: bool) -> None:
    settings = Settings.from_toml(config_path)
    configure_logging(settings.log_level)
    logger.info("Starting %s", version_summary())
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
    _quieten_http_client_logging(level)


def _quieten_http_client_logging(level: int) -> None:
    """Keep the HTTP clients from logging a line per request.

    relcoord talks to several APIs to serve one change, and httpx logs every
    request it makes at INFO, which buries the lines about the change itself.
    Asking for DEBUG is asking to see the traffic, so leave them alone then.
    """
    if level <= logging.DEBUG:
        return
    # Never below the configured level: a logger with a level of its own is
    # judged by that level rather than the root's, so pinning these to WARNING
    # under a quieter root would make them the loudest thing in the log.
    quiet_level = max(level, logging.WARNING)
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(quiet_level)


if __name__ == "__main__":
    main()
