"""知识库回答引用（批 1 后端）：run 出口翻译、终答登记、归属判定。"""

from __future__ import annotations

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.agents.lead_orchestrator import _resolve_citations
from bowhead_core.citations import ensure_registry
from bowhead_core.kb_citations import (
    kb_citation_identity,
    parse_kb_citation_identity,
    register_kb_refs,
    translate_doc_refs,
)
from bowhead_core.source_attribution import (
    Attribution,
    AttributionStores,
    KbFileAccess,
    ReportScope,
    attribute_refs,
)
from bowhead_core.types import ToolInvocation

pytestmark = pytest.mark.unit

CHUNK_A = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"
CHUNK_B = "chunk_bbbbbbbbbbbbbbbbbbbbbbbb"


def _kb_search(*chunks: tuple[str, str, str]) -> ToolInvocation:
    return ToolInvocation(
        tool_name="kb_search",
        arguments={"query": "q"},
        result={"chunks": [{"chunk_id": c, "file_id": f, "file_name": n} for c, f, n in chunks]},
    )


class _Ctx:
    pass


# ─── Run exit: each agent translates with own table ─────────────


def test_translate_uses_this_runs_own_table() -> None:
    """同一个编号在两个 agent 里指向不同片段：各翻各的，不会绑错。"""
    lead, peer = RefAllowlist(), RefAllowlist()
    lead.add("data", "art_x")
    lead.add("doc", CHUNK_A)  # Lead 2.
    peer.add("doc", CHUNK_B)  # Peer 1.
    peer.add("doc", CHUNK_A)  # Peer 2.

    assert translate_doc_refs("见 [ref:2]", lead) == f"见 [ref:{CHUNK_A}]"
    assert translate_doc_refs("见 [ref:1]", peer) == f"见 [ref:{CHUNK_B}]"


def test_translate_leaves_non_doc_and_unknown_numbers() -> None:
    al = RefAllowlist()
    al.add("data", "art_x")  # 1
    al.add("doc", CHUNK_A)  # 2
    assert translate_doc_refs("[ref:1] [ref:9] [ref:2]", al) == f"[ref:1] [ref:9] [ref:{CHUNK_A}]"
    assert translate_doc_refs("[ref:2]", None) == "[ref:2]"


# ─── Final answer: register only chunks actually retrieved this turn ──


def test_register_numbers_each_chunk_once_and_leaves_unknown() -> None:
    ctx = _Ctx()
    reg = ensure_registry(ctx)
    inv = [_kb_search((CHUNK_A, "kbf_1", "门店日销售.csv"), (CHUNK_B, "kbf_2", "合同.pdf"))]
    text = f"甲 [ref:{CHUNK_A}] 乙 [ref:{CHUNK_B}] 丙 [ref:{CHUNK_A}] 丁 [ref:chunk_notreturned]"

    out = register_kb_refs(text, reg, inv)

    assert out == "甲 [cite:1] 乙 [cite:2] 丙 [cite:1] 丁 [ref:chunk_notreturned]"
    first = reg.resolve(1)
    assert first is not None
    assert (first.artifact_id, first.label, first.kind) == (kb_citation_identity("kbf_1", CHUNK_A), "门店日销售.csv", "kb_chunk")


def test_register_sees_chunks_retrieved_by_peers() -> None:
    reg = ensure_registry(_Ctx())
    spawn = ToolInvocation(tool_name="subagent_spawn", arguments={}, nested=[_kb_search((CHUNK_A, "kbf_1", "a.pdf"))])
    assert register_kb_refs(f"x [ref:{CHUNK_A}]", reg, [spawn]) == "x [cite:1]"


def test_resolve_citations_registers_after_data_refs() -> None:
    ctx = _Ctx()
    inv = [_kb_search((CHUNK_A, "kbf_1", "a.pdf"))]
    text = f"总盘 [data:art_abc123] 规则见 [ref:{CHUNK_A}]，另见 [ref:chunk_fabricated00]"

    out = _resolve_citations(text, ctx, invocations=inv)

    assert out == "总盘 [cite:1] 规则见 [cite:2]，另见"
    second = ensure_registry(ctx).resolve(2)
    assert second is not None and second.kind == "kb_chunk"


# ─── Attribution by viewer read permissions ─────────────────────


def _scope() -> ReportScope:
    return ReportScope(workspace_id="ws_1", session_id="s_1")


def _checker(results: dict[str, KbFileAccess | Exception]):
    calls: list[str] = []

    async def check(file_id: str) -> KbFileAccess:
        calls.append(file_id)
        res = results[file_id]
        if isinstance(res, Exception):
            raise res
        return res

    return check, calls


@pytest.mark.asyncio
async def test_kb_chunk_verdicts_by_viewer_access() -> None:
    check, calls = _checker(
        {
            "kbf_ok": KbFileAccess(state="readable", file_name="当前名.pdf", scope="space", space_id="ws_9"),
            "kbf_no": KbFileAccess(state="forbidden"),
            "kbf_gone": KbFileAccess(state="missing"),
            "kbf_err": RuntimeError("db down"),
        }
    )
    refs = [
        ("kb_chunk", kb_citation_identity("kbf_ok", CHUNK_A)),
        ("kb_chunk", kb_citation_identity("kbf_ok", CHUNK_B)),
        ("kb_chunk", kb_citation_identity("kbf_no", CHUNK_A)),
        ("kb_chunk", kb_citation_identity("kbf_gone", CHUNK_A)),
        ("kb_chunk", kb_citation_identity("kbf_err", CHUNK_A)),
        ("kb_chunk", "chunk_without_identity"),
    ]
    out = await attribute_refs(refs, scope=_scope(), stores=AttributionStores(kb_file_access=check))

    ok = out[refs[0]]
    assert ok.attribution is Attribution.OWNED
    assert ok.display is not None and ok.display.label == "当前名.pdf"
    assert dict(ok.display.kb_ref or {}) == {"kb_file_id": "kbf_ok", "kb_chunk_id": CHUNK_A, "kb_scope": "space", "kb_space_id": "ws_9"}
    assert out[refs[1]].attribution is Attribution.OWNED
    assert calls.count("kbf_ok") == 1  # Once per file.
    for ref, expected in (
        (refs[2], Attribution.FOREIGN),
        (refs[3], Attribution.MISSING),
        (refs[4], Attribution.FOREIGN),
        (refs[5], Attribution.MISSING),
    ):
        verdict = out[ref]
        assert verdict.attribution is expected
        assert verdict.display is None and verdict.exit_candidate is None


@pytest.mark.asyncio
async def test_kb_chunk_without_viewer_is_restricted() -> None:
    ref = ("kb_chunk", kb_citation_identity("kbf_1", CHUNK_A))
    out = await attribute_refs([ref], scope=_scope(), stores=AttributionStores())
    assert out[ref].attribution is Attribution.FOREIGN
    assert out[ref].display is None


def test_identity_roundtrip() -> None:
    assert parse_kb_citation_identity(kb_citation_identity("kbf_1", CHUNK_A)) == ("kbf_1", CHUNK_A)
    assert parse_kb_citation_identity("art_x") is None
    assert parse_kb_citation_identity("kb::chunk") is None
