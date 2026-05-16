# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio

import click
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from relcoord.app import create_app
from relcoord.config import Settings
from relcoord.in_memory_repository import InMemoryImageVersionRepository

DEFAULT_CONFIG_PATH = "relcoord.toml"


async def run(config_path: str) -> None:
    settings = Settings.from_toml(config_path)
    config = HypercornConfig()
    config.bind = [f"{settings.host}:{settings.port}"]
    repository = InMemoryImageVersionRepository()
    # This has been raised upstream: https://github.com/pgjones/hypercorn/issues/353
    # noinspection PyTypeChecker
    await serve(create_app(repository), config)  # ty: ignore[invalid-argument-type]


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
def main(config_path: str) -> None:
    asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
