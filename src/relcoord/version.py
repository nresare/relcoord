# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""What release of relcoord is running, and what it builds manifests with."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from manifest_builder import get_version as manifest_builder_version

logger = logging.getLogger(__name__)

# Written into the container image at build time, holding the tag the image was
# published under. Nothing mounts over /usr/share, unlike /etc/relcoord, where a
# mounted CA certificate would hide it.
IMAGE_TAG_PATH = Path("/usr/share/relcoord/image-tag")

# What manifest-builder reports for a version it could not determine, used here
# for the same, so that one unknown version reads like another.
UNKNOWN_VERSION = "0.0.0"


def image_tag(path: Path = IMAGE_TAG_PATH) -> str | None:
    """Return the tag the running container image was published under.

    None outside a container image, where there is no tag to report: the file is
    written by the image build, which is the only thing that knows the tag. It
    carries the whole tag, version and build hash both, so it says which build is
    running where the package version can only say which release it came from.
    """
    try:
        tag = path.read_text().strip()
    except OSError:
        return None
    return tag or None


def relcoord_version(path: Path = IMAGE_TAG_PATH) -> str:
    """Return the release of relcoord that is running.

    The image tag is preferred where there is one, since it names the published
    build. A checkout falls back to the version the package was built with, which
    hatch-vcs derives from the nearest reachable tag.
    """
    tag = image_tag(path)
    if tag is not None:
        return tag
    try:
        return version("relcoord")
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def version_summary(path: Path = IMAGE_TAG_PATH) -> str:
    """Describe both versions in one line, for --version and the startup log."""
    return (
        f"relcoord {relcoord_version(path)} "
        f"(manifest-builder {manifest_builder_version()})"
    )
