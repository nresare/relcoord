# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from relcoord import version as version_module
from relcoord.main import main
from relcoord.version import (
    UNKNOWN_VERSION,
    image_tag,
    relcoord_version,
    version_summary,
)


def test_image_tag_reads_the_file_the_image_build_wrote(tmp_path: Path) -> None:
    path = tmp_path / "image-tag"
    path.write_text("0.1.0-38225f79\n")

    assert image_tag(path) == "0.1.0-38225f79"


def test_image_tag_is_absent_outside_a_container_image(tmp_path: Path) -> None:
    assert image_tag(tmp_path / "image-tag") is None


def test_image_tag_is_absent_when_the_build_was_told_no_tag(tmp_path: Path) -> None:
    """An unset build arg writes an empty file, which is no tag at all."""
    path = tmp_path / "image-tag"
    path.write_text("")

    assert image_tag(path) is None


def test_image_tag_is_absent_when_the_path_is_a_directory(tmp_path: Path) -> None:
    assert image_tag(tmp_path) is None


def test_relcoord_version_prefers_the_image_tag(tmp_path: Path) -> None:
    path = tmp_path / "image-tag"
    path.write_text("0.1.0-38225f79")

    assert relcoord_version(path) == "0.1.0-38225f79"


def test_relcoord_version_falls_back_to_the_package_version(tmp_path: Path) -> None:
    """A checkout has no image tag, so the built package's version stands in."""
    version = relcoord_version(tmp_path / "image-tag")

    assert version != UNKNOWN_VERSION
    assert version[0].isdigit()


def test_relcoord_version_reports_an_uninstalled_package_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> str:
        raise version_module.PackageNotFoundError(name)

    monkeypatch.setattr(version_module, "version", missing)

    assert relcoord_version(tmp_path / "image-tag") == UNKNOWN_VERSION


def test_version_summary_names_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image-tag"
    path.write_text("0.1.0-38225f79")
    monkeypatch.setattr(version_module, "manifest_builder_version", lambda: "0.7.4")

    assert version_summary(path) == "relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)"


def test_startup_logs_the_running_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The log of a running relcoord says which release is serving it."""
    config = tmp_path / "relcoord.toml"
    config.write_text('manifests-repository = "https://github.com/acme/manifests"\n')
    monkeypatch.setattr(
        "relcoord.main.version_summary",
        lambda: "relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)",
    )

    async def fake_run(settings: object, disable_auth: bool) -> None:
        return None

    monkeypatch.setattr("relcoord.main.run", fake_run)

    with caplog.at_level(logging.INFO, logger="relcoord.main"):
        result = CliRunner().invoke(main, ["-c", str(config)])

    assert result.exit_code == 0
    assert "Starting relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)" in caplog.text


def test_version_option_prints_both_versions_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--version answers without a config file, which only the container has."""
    monkeypatch.setattr(
        "relcoord.main.version_summary",
        lambda: "relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)",
    )

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert result.output == "relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)\n"
