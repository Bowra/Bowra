"""Each conversion copies the image's pre-built LibreOffice profile (docx inline edit design §5.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.documents import render


def test_a_conversion_gets_its_own_copy_of_the_pre_built_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = tmp_path / "template"
    (template / "user" / "config").mkdir(parents=True)
    (template / "user" / "registrymodifications.xcu").write_text("<items/>")
    monkeypatch.setattr(render, "PROFILE_TEMPLATE", template)
    first, second = tmp_path / "run-1", tmp_path / "run-2"
    first.mkdir()
    second.mkdir()
    profile = render.libreoffice_profile(first)
    assert profile == first / "profile"
    assert (profile / "user" / "registrymodifications.xcu").read_text() == "<items/>"
    (profile / "user" / "registrymodifications.xcu").write_text("changed by one run")
    assert (render.libreoffice_profile(second) / "user" / "registrymodifications.xcu").read_text() == "<items/>"
    assert (template / "user" / "registrymodifications.xcu").read_text() == "<items/>", "The image copy is never written"


def test_without_the_pre_built_profile_libreoffice_builds_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "PROFILE_TEMPLATE", tmp_path / "absent")
    assert render.libreoffice_profile(tmp_path) == tmp_path


def test_a_failed_copy_falls_back_to_a_new_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = tmp_path / "template"
    template.mkdir()
    monkeypatch.setattr(render, "PROFILE_TEMPLATE", template)

    def broken(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(render.shutil, "copytree", broken)
    run = tmp_path / "run"
    run.mkdir()
    assert render.libreoffice_profile(run) == run
