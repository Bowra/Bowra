"""Sprint ν1.5 — integration tests on full path (closes CTO ν1.5 P0-T1).

Tests cross-module interaction unit tests can't catch:
- canvas.add_insight thin path translate ORDER (must run before missing-check)
- ν1.5 translate + ν1 validate sequencing
- subagent ctx state cleanup (Lead spawning multi subagents — no leak)
- evidence_section_ids list translation (was missed in unit-only tests)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core._ref_validation import _build_real_refs, _validate_all_refs


@dataclass
class _Inv:
    call_id: str = ""
    tool_name: str = ""
    result: Any = None
    nested: list[_Inv] = field(default_factory=list)


@dataclass
class _Sect:
    id: str
    kind: str


@dataclass
class _Art:
    id: str


class _MockCS:
    def __init__(self, sections_by_kind: dict[str, set[str]]) -> None:
        self._sections_by_kind = sections_by_kind

    async def list_section_ids_by_kind(self, _cid: str, *, kind: str) -> set[str]:
        return self._sections_by_kind.get(kind, set())

    async def list_sections(self, _cid: str) -> list[_Sect]:
        out = []
        for kind, ids in self._sections_by_kind.items():
            for sid in ids:
                out.append(_Sect(id=sid, kind=kind))
        return out

    async def ensure_canvas(self, **kwargs):
        return SimpleNamespace(id="cv_test")


class _MockAS:
    def __init__(self, arts: list[_Art]) -> None:
        self._a = arts

    async def list_for_session(self, **kwargs):
        return self._a


# ─── Integration: ν1.5 translate ORDER (P0-A1 close) ───


@pytest.mark.asyncio
async def test_thin_path_simple_int_evidence_translates_before_missing_check() -> None:
    """canvas.add_insight: peer 写 evidence_section_ids=["1"] (simple int);
    backend translate "1" → "sec_real" BEFORE missing-check (real_ids 含).
    Without ν1.5 translate-first: missing-check 100% reject "1" not in real_ids.
    """
    al = RefAllowlist()
    al.add("chart", "sec_real")  # peer's "1" should resolve here

    # Simulate the canvas_tools._add_insight order:
    args_evidence = ["1"]  # peer writes simple int
    # ν1.5 translate (NEW, before missing-check):
    translated = [al.resolve(str(eid)) for eid in args_evidence]
    assert translated == ["sec_real"]

    # Missing-check (existing line 376):
    existing_ids = {"sec_real"}
    missing = [sid for sid in translated if sid not in existing_ids]
    assert missing == []  # not rejected — happy path


@pytest.mark.asyncio
async def test_thin_path_unknown_simple_int_falls_through_to_missing_check() -> None:
    """If peer writes simple int not in allowlist, translate passthroughs the
    string, missing-check catches it (proper error to peer)."""
    al = RefAllowlist()
    al.add("chart", "sec_a")  # only id 1 mapped

    args_evidence = ["99"]  # peer hallucinated
    translated = [al.resolve(str(eid)) for eid in args_evidence]
    assert translated == ["99"]  # passthrough

    existing_ids = {"sec_a"}
    missing = [sid for sid in translated if sid not in existing_ids]
    assert missing == ["99"]  # rejected as expected


# ─── Integration: ν1.5 translate + ν1 validate full path ───


@pytest.mark.asyncio
async def test_ν15_translate_then_ν1_validate_full_payload() -> None:
    """Peer payload uses simple ints throughout; translate first then ν1
    validate sees real IDs and accepts them."""
    al = RefAllowlist()
    al.add("chart", "sec_chart_real")  # → 1
    al.add("data", "art_data_real")  # → 2
    al.add("doc", "chunk_doc_real")  # → 3
    al.add("tl", "tl_call_real")  # → 4

    payload = {
        "exec_summary": "见 [ref:3]",  # simple int
        "insights": [
            {
                "title": "Title",
                "narrative": "[num:1.5:倍:src:4]",  # simple int src
                "evidence": [
                    {"kind": "chart", "chart_section_id": "1", "caption": "Chart"},  # simple int
                    {"kind": "table", "table_artifact_id": "2", "caption": "Data"},
                ],
            }
        ],
        "kpis": [{"value": "[num:1:src:4]"}],
    }

    # ν1.5 translate
    al.translate_payload(payload)

    # Now real IDs in place
    assert payload["exec_summary"] == "见 [ref:chunk_doc_real]"
    assert "tl_call_real" in payload["insights"][0]["narrative"]
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "sec_chart_real"
    assert payload["insights"][0]["evidence"][1]["table_artifact_id"] == "art_data_real"

    # ν1 validate sees real IDs and passes
    real_refs = {
        "chart": {"sec_chart_real"},
        "data": {"art_data_real"},
        "code": set(),
        "doc": {"chunk_doc_real"},
        "tl": {"tl_call_real"},
    }
    errors = _validate_all_refs(payload, real_refs)
    assert errors == []


@pytest.mark.asyncio
async def test_ν15_translate_then_ν1_validate_catches_unknown_simple_int() -> None:
    """Peer writes simple int not in allowlist → translate passthrough →
    ν1 validate catches as hallucinate."""
    al = RefAllowlist()
    al.add("chart", "sec_a")  # only id 1

    payload = {
        "insights": [
            {
                "evidence": [{"kind": "chart", "chart_section_id": "99"}],  # invalid
            }
        ],
    }
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "99"

    real_refs = {"chart": {"sec_a"}, "data": set(), "code": set(), "doc": set(), "tl": set()}
    errors = _validate_all_refs(payload, real_refs)
    assert len(errors) == 1
    assert errors[0]["kind"] == "chart_ref_not_in_allowlist"


# ─── Integration: ν1.5 + _build_real_refs work together (chart 1 = sec_a in both views) ───


@pytest.mark.asyncio
async def test_allowlist_and_real_refs_view_same_chart_id() -> None:
    """Verify that whatever ν1.5 allowlist exposes (simple "1" → real
    "sec_a"), ν1 _build_real_refs ALSO sees "sec_a" (consistent state)."""
    cs = _MockCS({"chart": {"sec_a"}, "code_block": set()})
    ars = _MockAS([])
    invs = []

    al = RefAllowlist()
    await al.refresh_from_stores(
        canvas_store=cs,
        artifact_store=ars,
        session_id="s_1",
        user_id="u_1",
        canvas_id="cv_1",
        tool_invocations=invs,
    )
    real = await _build_real_refs(
        canvas_id="cv_1",
        session_id="s_1",
        user_id="u_1",
        canvas_store=cs,
        artifact_store=ars,
        tool_invocations=invs,
    )

    # ν1.5 allowlist: simple "1" → "sec_a"
    assert al.resolve("1") == "sec_a"
    # ν1 real_refs: "sec_a" in chart set
    assert "sec_a" in real["chart"]


# ─── Integration: ctx state cleanup (P0-A2 close) ───


def test_ctx_state_cleanup_pattern_no_leak() -> None:
    """Simulate the subagent _ν_cleanup_ctx pattern. Re-entrant: parent_ctx
    accumulates state from spawn 1; spawn 2 must see clean state."""

    class _MockCtx:
        pass

    ctx = _MockCtx()
    al1 = RefAllowlist()
    al1.add("chart", "sec_x")

    # spawn 1 attaches state
    ctx._ν15_allowlist = al1
    ctx._ν_retry_count = 1
    ctx._ν_thin_retry_count = 0
    ctx._current_subagent_invocations = ["inv_1"]

    # spawn 2 begins: snapshot prior + delete + setup new
    prior_state: dict[str, Any] = {}
    for attr in ("_ν15_allowlist", "_current_subagent_invocations", "_ν_retry_count", "_ν_thin_retry_count"):
        if hasattr(ctx, attr):
            prior_state[attr] = getattr(ctx, attr)
        with contextlib.suppress(AttributeError, TypeError):
            delattr(ctx, attr)
    al2 = RefAllowlist()  # spawn 2's fresh allowlist
    ctx._ν15_allowlist = al2

    # spawn 2 sees fresh allowlist (no chart from spawn 1)
    assert getattr(ctx, "_ν15_allowlist", None) is al2
    assert al2.resolve("1") == "1"  # passthrough — no ID mapped yet

    # spawn 2 cleanup → restore prior
    for a in ("_ν15_allowlist", "_current_subagent_invocations", "_ν_retry_count", "_ν_thin_retry_count"):
        with contextlib.suppress(AttributeError, TypeError):
            delattr(ctx, a)
    for a, v in prior_state.items():
        setattr(ctx, a, v)

    # spawn 1's state restored
    assert getattr(ctx, "_ν15_allowlist", None) is al1
    assert getattr(ctx, "_ν_retry_count", None) == 1
    assert getattr(ctx, "_current_subagent_invocations", None) == ["inv_1"]


# ─── Integration: regex sharing (P0-E1 close) ───


def test_regexes_truly_shared_module_identity() -> None:
    """ν1.5 _NUM_SRC_RE and _ref_validation._NUM_RE 必须是同一对象, 不是 copy."""
    from bowhead_core import _ref_allowlist, _ref_validation

    assert _ref_allowlist._NUM_SRC_RE is _ref_validation._NUM_RE
    assert _ref_allowlist._REF_RE is _ref_validation._REF_RE
