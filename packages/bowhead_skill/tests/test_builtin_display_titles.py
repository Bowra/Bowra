"""Builtin display defaults preserve package bytes and authored Skill names."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
import yaml
from bowhead_skill.builtin_display import listing_description_fields
from bowhead_skill.builtin_listings import BUILTIN_LISTING_DESCRIPTIONS

from bowhead_skill import SkillCatalog, builtin_catalog_dir, load_skill

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, directory: str, *, name: str, title_zh: str = "") -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    metadata = {"name": name, "description": "Synthetic document fixture.", "title": "测试文档"}
    if title_zh:
        metadata["title_zh"] = title_zh
    (skill_dir / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n# Synthetic instructions\n",
        encoding="utf-8",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('synthetic output')\n", encoding="utf-8")
    return skill_dir


def _file_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    ("name", "expected_title", "expected_chinese_title"),
    [
        ("docx", "Word editing guide", "Word 编辑参考"),
        ("pdf", "PDF processing guide", "PDF 处理参考"),
        ("pptx", "PowerPoint authoring guide", "PPT 制作与编辑参考"),
        ("xlsx", "Excel editing guide", "Excel 编辑参考"),
    ],
)
def test_installed_document_titles_preserve_source_metadata_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected_title: str, expected_chinese_title: str
) -> None:
    builtin_root = tmp_path / "builtin"
    skill_dir = _write_skill(builtin_root, name, name=name)
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)
    before = _file_hashes(skill_dir)

    spec = load_skill(skill_dir)

    assert spec.title == expected_title
    assert spec.title_zh == expected_chinese_title
    assert spec.frontmatter["title"] == "测试文档"
    assert "title_zh" not in spec.frontmatter
    assert spec.body_md == "# Synthetic instructions\n"
    assert spec.name == name
    assert _file_hashes(skill_dir) == before
    catalog_spec = SkillCatalog(roots=[builtin_root]).get(name)
    assert catalog_spec is not None
    assert catalog_spec.title == expected_title


def test_installed_document_preserves_explicit_chinese_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_root = tmp_path / "builtin"
    skill_dir = _write_skill(builtin_root, "pdf", name="pdf", title_zh="中文版显示名称")
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)

    spec = load_skill(skill_dir)

    assert spec.title == "PDF processing guide"
    assert spec.title_zh == "中文版显示名称"
    assert spec.frontmatter["title_zh"] == "中文版显示名称"


@pytest.mark.parametrize(("directory", "name"), [("copy", "pdf"), ("pdf", "custom"), ("custom", "custom")])
def test_nonmatching_builtin_path_or_name_retains_authored_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str, name: str) -> None:
    builtin_root = tmp_path / "builtin"
    skill_dir = _write_skill(builtin_root, directory, name=name)
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)

    spec = load_skill(skill_dir)

    assert spec.title == "测试文档"
    assert spec.title_zh == ""


def test_same_name_user_skill_keeps_its_title_when_shadowing_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_root = tmp_path / "builtin"
    _write_skill(builtin_root, "pdf", name="pdf")
    user_root = tmp_path / "user"
    user_skill = _write_skill(user_root, "pdf", name="pdf", title_zh="用户中文名称")
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)

    spec = SkillCatalog(roots=[builtin_root, user_root]).get("pdf")

    assert spec is not None
    assert spec.source_dir == user_skill
    assert spec.title == "测试文档"
    assert spec.title_zh == "用户中文名称"


def test_builtin_symlink_to_user_bundle_does_not_adapt_user_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    user_skill = _write_skill(tmp_path / "user", "pdf", name="pdf")
    (builtin_root / "pdf").symlink_to(user_skill, target_is_directory=True)
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)

    spec = load_skill(builtin_root / "pdf")

    assert spec.title == "测试文档"
    assert spec.title_zh == ""


_LOCALES = ("en-US", "zh-CN", "de-DE", "fr-FR")
# Model-facing vocabulary that must not leak into user-facing listing text.
_INTERNAL_TERMS = ("subagent", "payload", "skill_read", "bi_query", "datarc", "sop", "json", "reportsection", "python-docx", "openpyxl", "reportlab")


def test_every_builtin_skill_has_reviewed_listing_descriptions() -> None:
    specs = SkillCatalog(roots=[builtin_catalog_dir()]).list_specs()

    assert specs
    assert {spec.name for spec in specs} <= set(BUILTIN_LISTING_DESCRIPTIONS)
    for spec in specs:
        # Display copy never lives in the hashed bundle.
        assert "listing" not in spec.frontmatter, spec.name
        assert set(spec.listing_descriptions) == set(_LOCALES), spec.name
        for locale, text in spec.listing_descriptions.items():
            assert text != spec.description, (spec.name, locale)
            lowered = text.lower()
            assert not [term for term in _INTERNAL_TERMS if term in lowered], (spec.name, locale, text)


def test_listing_text_comes_only_from_display_metadata_for_installed_builtins(tmp_path: Path) -> None:
    # A same-name authored bundle, even one declaring its own `listing`, gets no built-in listing text.
    skill_dir = tmp_path / "analyst-sop-compute"
    skill_dir.mkdir()
    metadata = {"name": "analyst-sop-compute", "description": "Model routing text.", "listing": {"en-US": {"description": "Authored."}}}
    (skill_dir / "SKILL.md").write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n# Body\n", encoding="utf-8")

    spec = load_skill(skill_dir)

    assert spec.description == "Model routing text."
    assert spec.listing_descriptions == {}
    assert listing_description_fields(spec) == {"description_en": "", "description_zh": "", "description_de": "", "description_fr": ""}

    installed = SkillCatalog(roots=[builtin_catalog_dir()]).get("analyst-sop-compute")
    assert installed is not None
    assert installed.listing_descriptions == BUILTIN_LISTING_DESCRIPTIONS["analyst-sop-compute"]


def test_installed_document_listing_preserves_source_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_root = tmp_path / "builtin"
    skill_dir = _write_skill(builtin_root, "xlsx", name="xlsx")
    copy_dir = _write_skill(tmp_path / "user", "xlsx", name="xlsx")
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)
    before = _file_hashes(skill_dir)

    spec = load_skill(skill_dir)

    assert set(spec.listing_descriptions) == set(_LOCALES)
    assert "listing" not in spec.frontmatter
    assert _file_hashes(skill_dir) == before
    # A same-name bundle outside the installed catalog keeps its authored (empty) listing.
    assert load_skill(copy_dir).listing_descriptions == {}
