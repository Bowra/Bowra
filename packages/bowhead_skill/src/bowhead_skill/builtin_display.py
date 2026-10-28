"""Presentation metadata for built-in Skills.

Titles normally come from SKILL.md frontmatter (``title``/``title_zh``). Reviewed
document-guide titles live here; listing descriptions live in
:mod:`bowhead_skill.builtin_listings`. Both stay outside the bundles so display
copy does not change package bytes or revisions pinned by released Agents.
Explicit Chinese titles remain authoritative. The model reads ``description``
and the body only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bowhead_skill.builtin_listings import BUILTIN_LISTING_DESCRIPTIONS

if TYPE_CHECKING:
    from pathlib import Path

LISTING_LOCALES = ("en-US", "zh-CN", "de-DE", "fr-FR")
# DTO suffixes follow the authoring catalog's Tool translations (`description_en`, ...).
_LOCALE_SUFFIX = {"en-US": "en", "zh-CN": "zh", "de-DE": "de", "fr-FR": "fr"}

_DOCUMENT_TITLES = {
    "docx": "Word editing guide",
    "pdf": "PDF processing guide",
    "pptx": "PowerPoint authoring guide",
    "xlsx": "Excel editing guide",
}
_DOCUMENT_TITLES_ZH = {
    "docx": "Word 编辑参考",
    "pdf": "PDF 处理参考",
    "pptx": "PPT 制作与编辑参考",
    "xlsx": "Excel 编辑参考",
}


def _is_installed_builtin(skill_dir: Path, name: str) -> bool:
    # Import lazily: catalog imports the loader that consumes this adapter.
    from bowhead_skill.catalog import builtin_catalog_dir

    # Do not resolve the final expected child: a symlink to a user bundle must
    # not make that bundle an installed builtin merely because its slug matches.
    return skill_dir.resolve() == builtin_catalog_dir().resolve() / name


def builtin_display_titles(
    skill_dir: Path,
    *,
    name: str,
    title: str,
    title_zh: str,
) -> tuple[str, str]:
    """Adapt only exact installed packages without changing their source metadata."""
    if name not in _DOCUMENT_TITLES or not _is_installed_builtin(skill_dir, name):
        return title, title_zh
    return _DOCUMENT_TITLES[name], title_zh or _DOCUMENT_TITLES_ZH[name]


def builtin_listing_descriptions(skill_dir: Path, *, name: str) -> dict[str, str]:
    """Listing text for an exact installed built-in; same-name user or team bundles get none."""
    descriptions = BUILTIN_LISTING_DESCRIPTIONS.get(name)
    if not descriptions or not _is_installed_builtin(skill_dir, name):
        return {}
    return {locale: descriptions[locale] for locale in LISTING_LOCALES if descriptions.get(locale)}


def listing_description_fields(spec: Any) -> dict[str, str]:
    """Project a Skill's listing descriptions onto ``description_<lang>`` DTO fields.

    Callers pass only shipped built-ins: authored Skill descriptions stay as written.
    """
    descriptions = getattr(spec, "listing_descriptions", None) or {}
    return {f"description_{_LOCALE_SUFFIX[locale]}": descriptions.get(locale, "") for locale in LISTING_LOCALES}
