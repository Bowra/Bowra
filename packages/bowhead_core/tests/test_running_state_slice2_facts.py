"""Chat running state slice 2: the facts the interface and Lead read about a Turn's files and report.

- ``step_done`` output artifacts carry the stored kind and version (R2.1), so a delivered file shows as a card when
  it is saved and a later version updates it.
- Helper results tell Lead which saved files were not delivered (R2.8), as facts.
- The final payload names the report sections the Turn added and revised (R2.2) and counts its work files (R2.3).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _art(art_id: str, kind: str = "deliverable", *, filename: str = "", lineage: str | None = None, version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=art_id, kind=kind, filename=filename or f"{art_id}.bin", lineage_id=lineage, version_no=version)


# ─── R2.1 ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_saved_files_carry_their_stored_kind_and_version() -> None:
    from bowhead_core.subagent import _with_artifact_identity

    async def get_many(ids):
        assert ids == ["art_deck_v2", "art_scratch"]
        return [_art("art_deck_v2", lineage="art_deck_v1", version=2), _art("art_scratch", "sandbox_output")]

    ctx = SimpleNamespace(artifact_store=SimpleNamespace(get_many=get_many))
    refs = [{"artifact_id": "art_deck_v2", "filename": "deck.pptx"}, {"artifact_id": "art_scratch", "filename": "tmp.csv"}]
    assert await _with_artifact_identity(refs, ctx) == [
        {"artifact_id": "art_deck_v2", "filename": "deck.pptx", "kind": "deliverable", "lineage_id": "art_deck_v1", "version_no": 2},
        {"artifact_id": "art_scratch", "filename": "tmp.csv", "kind": "sandbox_output", "lineage_id": "art_scratch", "version_no": 1},
    ]


@pytest.mark.unit
async def test_a_failed_lookup_leaves_the_saved_files_as_they_were() -> None:
    from bowhead_core.subagent import _with_artifact_identity

    async def get_many(ids):
        raise RuntimeError("database is locked")

    refs = [{"artifact_id": "art_page", "filename": "page.html"}]
    assert await _with_artifact_identity(refs, SimpleNamespace(artifact_store=SimpleNamespace(get_many=get_many))) == refs
    assert await _with_artifact_identity(refs, SimpleNamespace()) == refs


# ─── R2.8 ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_lead_sees_the_saved_files_nobody_delivered_with_their_ids() -> None:
    from bowhead_core.deliverable_ledger import build_deliverable_ledger, produced_facts

    async def list_result_artifacts_for_turn(turn_id):
        return [
            _art("art_deck", filename="report.pptx"),
            _art("art_slide_1", "deck_preview", filename="slide-1.png"),
            _art("art_csv", "sandbox_output", filename="store_sales.csv"),
        ]

    store = SimpleNamespace(list_result_artifacts_for_turn=list_result_artifacts_for_turn)
    facts = produced_facts(await build_deliverable_ledger(turn_id="t_1", artifact_store=store))
    # Slide previews belong to the deck; only the work file is undelivered.
    assert {"artifact_id": "art_deck", "name": "report.pptx"} in facts["delivered_files"]
    assert facts["undelivered_files"] == [{"artifact_id": "art_csv", "name": "store_sales.csv"}]


# ─── R2.2 / R2.3 ─────────────────────────────────────────────────────────────────────────────────────────


def _section(section_id: str, kind: str = "narrative", title: str = "", **extra) -> SimpleNamespace:
    payload = {"markdown": f"## {title}\n\n正文"} if kind == "narrative" else {"title": title}
    return SimpleNamespace(id=section_id, kind=kind, payload=payload, superseded=False, **extra)


def _turn_stores(*, appended, touched, saved):
    async def list_sections_appended_by_turn(turn_id, *, kinds=None, include_patches=False):
        return touched if include_patches else appended

    async def list_result_artifacts_for_turn(turn_id):
        return saved

    return SimpleNamespace(
        canvas_store=SimpleNamespace(list_sections_appended_by_turn=list_sections_appended_by_turn),
        artifact_store=SimpleNamespace(list_result_artifacts_for_turn=list_result_artifacts_for_turn),
    )


@pytest.mark.unit
async def test_the_answer_names_the_report_sections_it_added_and_revised_and_counts_work_files() -> None:
    from bowhead_core.agents.lead_orchestrator import _inject_turn_changes

    new = _section("sec_new", title="各门店营业额对比")
    revised = _section("sec_old", title="季度趋势")
    code = _section("sec_code", "code_block", title="计算")
    ctx = _turn_stores(
        appended=[new, code],
        touched=[revised, new, code],
        saved=[_art("art_page", filename="page.html"), _art("art_draft", filename="page-draft.html"), _art("art_csv", "sandbox_output")],
    )
    payload = {"outputs": [{"kind": "report", "count": 1}], "inline_artifacts": [{"kind": "file", "id": "art_page"}]}
    await _inject_turn_changes(payload, ctx, "t_1")
    assert payload["report_changes"] == {
        "added": [{"section_id": "sec_new", "kind": "narrative", "title": "各门店营业额对比"}],
        "updated": [{"section_id": "sec_old", "kind": "narrative", "title": "季度趋势"}],
        "added_kinds": {"narrative": 1},
        "updated_kinds": {"narrative": 1},
    }
    # The undelivered draft and the work file; the delivered page is a card, not a work file.
    assert payload["work_files"] == {"count": 2}


@pytest.mark.unit
async def test_no_report_card_when_the_turn_keeps_no_report() -> None:
    from bowhead_core.agents.lead_orchestrator import _inject_turn_changes

    ctx = _turn_stores(appended=[_section("sec_new", title="草稿")], touched=[], saved=[])
    payload = {"outputs": [{"kind": "chart", "count": 1}]}
    await _inject_turn_changes(payload, ctx, "t_1")
    assert "report_changes" not in payload
    assert "work_files" not in payload


@pytest.mark.unit
async def test_report_changes_count_every_kind_past_the_entry_cap() -> None:
    """116 report Turns write a title, a sources block, chapters and charts; one wrote 26 chapters and 8 charts.
    The entries stop at 20, the per-kind counts do not, so the card can say "26 节 · 8 张图"."""
    from bowhead_core.agents.lead_orchestrator import _inject_turn_changes

    sections = [_section("sec_h1", "h1", title="年度汇报"), _section("sec_src", "sources")]
    sections += [_section(f"sec_n{i}", title=f"第{i}章") for i in range(26)]
    sections += [_section(f"sec_c{i}", "chart", title=f"图{i}") for i in range(8)]
    ctx = _turn_stores(appended=sections, touched=sections, saved=[])
    payload = {"outputs": [{"kind": "report", "count": 1}]}
    await _inject_turn_changes(payload, ctx, "t_1")
    changes = payload["report_changes"]
    assert len(changes["added"]) == 20
    assert changes["added_omitted"] == 16
    assert changes["added_kinds"] == {"h1": 1, "sources": 1, "narrative": 26, "chart": 8}
    assert changes["updated"] == [] and changes["updated_kinds"] == {}
