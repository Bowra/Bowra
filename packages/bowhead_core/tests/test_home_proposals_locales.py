"""Per-locale deterministic homepage proposal copy (zh-CN, en-US, de-DE, fr-FR)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bowhead_core.capability_resolver import CapabilityRef
from bowhead_core.home_proposals import (
    build_envelope,
    generate_deterministic_proposals,
    platform_capability_refs,
)

from bowhead_core import home_proposals

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "home_proposals_zh_en_golden.json"


def _cap(
    capability_id: str,
    label: str,
    *,
    kind: str,
    operations: frozenset[str] = frozenset({"read", "execute"}),
    formats: tuple[str, ...] = (),
) -> CapabilityRef:
    return CapabilityRef(  # type: ignore[arg-type]
        id=capability_id,
        kind=kind,
        label=label,
        description=f"Description for {capability_id}",
        operations=operations,
        output_formats=formats,
        version="v1",
    )


_KB_TOOL = _cap("tool:kb_search", "kb_search", kind="tool")
_SCHEDULER_TOOL = _cap("tool:scheduled_job_create", "scheduled_job_create", kind="tool", operations=frozenset({"execute", "update"}))
_EMAIL_TOOL = _cap("tool:send_email", "Send email", kind="tool", operations=frozenset({"execute", "update"}))
_CRM_TOOL = _cap("tool:crm.update", "CRM 更新", kind="tool", operations=frozenset({"execute", "update"}))
_ZH_DOCX_SKILL = _cap("skill:canvas-docx", "报告导出 Word", kind="skill", formats=("docx",))
_PPTX_SKILL = _cap("skill:pptx", "Presentation builder", kind="skill", formats=("pptx", "pdf"))
_READ_SKILL = _cap("skill:brief-reader", "简报阅读", kind="skill")
_LEAD_ONLY = _cap("platform:lead", "Bowhead", kind="platform")


def _with_platforms(*refs: CapabilityRef, read_only: bool = False) -> tuple[CapabilityRef, ...]:
    return (*refs, *platform_capability_refs(refs, read_only=read_only))


# Each case: (name, capabilities, limit). Shared by the zh/en golden capture and the per-locale tests.
_CASES: tuple[tuple[str, tuple[CapabilityRef, ...], int], ...] = (
    ("full", _with_platforms(_PPTX_SKILL, _KB_TOOL, _CRM_TOOL, _SCHEDULER_TOOL), 4),
    ("full_default_limit", _with_platforms(_PPTX_SKILL, _KB_TOOL, _CRM_TOOL, _SCHEDULER_TOOL), 3),
    ("chinese_skill_labels", _with_platforms(_ZH_DOCX_SKILL, _KB_TOOL), 3),
    ("read_skill_as_knowledge", _with_platforms(_READ_SKILL, _EMAIL_TOOL), 3),
    ("write_tool_with_display_name", _with_platforms(_EMAIL_TOOL, _SCHEDULER_TOOL), 4),
    ("read_only_viewer", _with_platforms(_KB_TOOL, _SCHEDULER_TOOL, read_only=True), 4),
    ("lead_only", (_LEAD_ONLY,), 3),
    ("lead_only_platforms", _with_platforms(), 3),
)
_CASE_IDS = [name for name, _refs, _limit in _CASES]


def _render(locale: str, refs: tuple[CapabilityRef, ...], limit: int) -> list[dict[str, object]]:
    envelope = build_envelope(
        workspace_id="ws_1",
        workspace_name="Operations",
        capabilities=refs,
        locale=locale,
    )
    return [proposal.to_cache_payload() for proposal in generate_deterministic_proposals(envelope, limit=limit)]


def _render_all(locale: str) -> dict[str, list[dict[str, object]]]:
    return {name: _render(locale, refs, limit) for name, refs, limit in _CASES}


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_zh_and_en_output_is_byte_identical_to_pre_table_golden(locale: str) -> None:
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    rendered = _render_all(locale)

    assert json.dumps(rendered, ensure_ascii=False, sort_keys=True) == json.dumps(golden[locale], ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("locale", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_every_locale_renders_every_proposal_kind_from_its_own_locale(locale: str) -> None:
    english = _render_all("en-US")
    rendered = _render_all(locale)

    for name, _refs, _limit in _CASES:
        assert rendered[name], name
        assert [item["kind"] for item in rendered[name]] == [item["kind"] for item in english[name]], name
    envelope = build_envelope(workspace_id="ws_1", workspace_name="Operations", capabilities=(_LEAD_ONLY,), locale=locale)
    assert envelope.locale == locale


def test_locale_tables_share_keys_and_format_placeholders() -> None:
    reference = home_proposals._PROPOSAL_COPY["en-US"]
    placeholder = re.compile(r"\{(\w+)\}")
    for locale, table in home_proposals._PROPOSAL_COPY.items():
        assert table.keys() == reference.keys(), locale
        for key, template in table.items():
            assert set(placeholder.findall(template)) <= {"format", "label"}, (locale, key)
        assert "{format}" in table["create_title"], locale
        assert "{format}" in table["create_prompt"], locale
    assert set(home_proposals.CODEX_TRANSLATE_PLACEHOLDER_LOCALES) <= {"de-DE", "fr-FR"}


def test_normalize_locale_keeps_supported_locales_and_defaults_to_english() -> None:
    for locale in ("zh-CN", "en-US", "de-DE", "fr-FR"):
        assert home_proposals.normalize_locale(locale) == locale
    for unsupported in ("ja-JP", "de", "", "zh"):
        assert home_proposals.normalize_locale(unsupported) == "en-US"


@pytest.mark.parametrize(
    ("locale", "other_locales"),
    [("de-DE", ("zh-CN", "en-US", "fr-FR")), ("fr-FR", ("zh-CN", "en-US", "de-DE"))],
)
def test_editing_one_placeholder_table_changes_only_that_locale(
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    other_locales: tuple[str, ...],
) -> None:
    before = {code: _render_all(code) for code in ("zh-CN", "en-US", "de-DE", "fr-FR")}
    table = home_proposals._PROPOSAL_COPY[locale]  # type: ignore[index]
    monkeypatch.setitem(table, "lead_title", f"Translated lead title {locale}")
    monkeypatch.setitem(table, "act_title", f"Translated action {{label}} {locale}")

    after = {code: _render_all(code) for code in ("zh-CN", "en-US", "de-DE", "fr-FR")}

    assert after[locale]["lead_only"][0]["title"] == f"Translated lead title {locale}"
    assert after[locale]["lead_only"][0]["id"] != before[locale]["lead_only"][0]["id"]
    act = next(item for item in after[locale]["full"] if item["kind"] == "act")
    assert act["title"] == f"Translated action CRM 更新 {locale}"
    for other in other_locales:
        assert after[other] == before[other], other


def test_no_op_table_edit_leaves_output_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control for the edit test above: rewriting a value with itself must not change output."""
    before = _render_all("de-DE")
    table = home_proposals._PROPOSAL_COPY["de-DE"]
    monkeypatch.setitem(table, "lead_title", table["lead_title"])

    assert _render_all("de-DE") == before


@pytest.mark.parametrize(
    ("locale", "expected_tool_label"),
    [("de-DE", "E-Mail senden"), ("fr-FR", "Envoyer un e-mail"), ("en-US", "Send email"), ("zh-CN", "Send email")],
)
def test_act_proposal_uses_generated_tool_display_name_for_german_and_french(locale: str, expected_tool_label: str) -> None:
    rendered = _render(locale, _with_platforms(_EMAIL_TOOL), 3)

    act = next(item for item in rendered if item["kind"] == "act")
    assert expected_tool_label in str(act["title"])
    assert expected_tool_label in str(act["prompt"])


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_unknown_tool_and_skill_labels_fall_back_to_english_display(locale: str) -> None:
    rendered = _render(locale, _with_platforms(_CRM_TOOL, _READ_SKILL), 3)

    by_kind = {item["kind"]: item for item in rendered}
    assert "CRM 更新" in str(by_kind["act"]["title"])
    assert str(by_kind["analyze"]["reason"]).startswith("简报阅读 ")


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_platform_labels_come_from_the_locale_table(monkeypatch: pytest.MonkeyPatch, locale: str) -> None:
    table = home_proposals._PROPOSAL_COPY[locale]  # type: ignore[index]
    monkeypatch.setitem(table, "platform_label_knowledge", f"Knowledge {locale}")

    rendered = _render(locale, _with_platforms(_KB_TOOL), 3)
    english = _render("en-US", _with_platforms(_KB_TOOL), 3)

    analyze = next(item for item in rendered if item["kind"] == "analyze")
    assert str(analyze["reason"]).startswith(f"Knowledge {locale} ")
    assert str(next(item for item in english if item["kind"] == "analyze")["reason"]).startswith("Space knowledge ")


def test_copy_fingerprint_is_per_locale_and_tracks_table_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprints = {code: home_proposals.proposal_copy_fingerprint(code) for code in ("zh-CN", "en-US", "de-DE", "fr-FR")}
    assert len(set(fingerprints.values())) == 4
    assert home_proposals.proposal_copy_fingerprint("ja-JP") == fingerprints["en-US"]

    monkeypatch.setitem(home_proposals._PROPOSAL_COPY["de-DE"], "lead_title", "Ziel in einen Plan zerlegen")

    assert home_proposals.proposal_copy_fingerprint("de-DE") != fingerprints["de-DE"]
    for other in ("zh-CN", "en-US", "fr-FR"):
        assert home_proposals.proposal_copy_fingerprint(other) == fingerprints[other]


def test_skill_capability_ref_carries_the_chinese_and_english_titles() -> None:
    """Novice walkthrough: a zh-CN Home card read "当前可用的Word documents支持产出 DOCX"."""
    from types import SimpleNamespace

    from bowhead_core.home_proposals import skill_capability_ref

    spec = SimpleNamespace(name="bowra-docx", title="Word documents", title_zh="Word 文档", description="", category="documents", version="1")
    ref = skill_capability_ref(spec)
    assert ref.label == "Word 文档"
    assert ref.label_en == "Word documents"
