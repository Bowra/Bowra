"""SkillLoader + SkillCatalog 单元测试."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bowhead_skill import SkillCatalog, load_skill

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, desc: str, body: str = "# body") -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nversion: 1.0.0\n---\n\n{body}\n",
        encoding="utf-8",
    )
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return d


def test_load_valid_skill(tmp_path: Path) -> None:
    d = _write_skill(tmp_path, "pdf", "make PDFs")
    s = load_skill(d)
    assert s.name == "pdf"
    assert s.description == "make PDFs"
    assert s.version == "1.0.0"
    assert len(s.scripts) == 1
    assert s.scripts[0].name == "hello.py"


def test_load_skill_dependencies_as_deduplicated_metadata(tmp_path: Path) -> None:
    d = _write_skill(tmp_path, "method", "method")
    path = d / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "version: 1.0.0",
            "version: 1.0.0\nrequired_skills: [analyst-framework, analyst-framework, method]",
        ),
        encoding="utf-8",
    )

    assert load_skill(d).required_skills == ["analyst-framework"]


def test_load_missing_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        load_skill(d)


def test_load_missing_name(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name"):
        load_skill(d)


def test_catalog_refresh(tmp_path: Path) -> None:
    _write_skill(tmp_path, "pdf", "pdf skill")
    _write_skill(tmp_path, "pptx", "pptx skill")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    assert sorted(cat.list_names()) == ["pdf", "pptx"]
    assert cat.get("pdf") is not None
    assert cat.get("nope") is None


def test_catalog_skips_broken(tmp_path: Path) -> None:
    _write_skill(tmp_path, "good", "ok")
    broken = tmp_path / "bad"
    broken.mkdir()
    (broken / "SKILL.md").write_text("garbage\n", encoding="utf-8")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    assert "good" in cat.list_names()
    assert "bad" not in cat.list_names()
