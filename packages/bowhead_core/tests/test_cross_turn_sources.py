"""来源跨轮合并（canvas-elements，`FEATURE_CANVAS_SOURCES_MERGE` 默认开）。

权威=`docs/sprints/canvas-elements-2026-09-04/tech-design-cross-turn-sources.md`。

判据的取向：**每条都要在「漏接线」时转红**。设计 v1 的判据有一半是空真断言
（「前缀不变」在不接线时更成立），所以这里的正断言一律是「新来源真的接在后面」
「历史条目真的还在」这种会被漏接线打红的形状。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from bowhead_core.sources_resolver import (
    TurnSourcesRegistry,
    find_canvas_sources_section,
    fold_source_entries,
    qualifying_sources_sections,
    upsert_turn_sources_section,
)

pytestmark = pytest.mark.unit


# ── Minimal store/bus stub (only the four methods used by upsert) ──────────


class _Sec:
    def __init__(self, sid: str, kind: str, payload: dict[str, Any], turn_id: str | None, seq: int, superseded: bool = False) -> None:
        self.id = sid
        self.kind = kind
        self.payload = payload
        self.turn_id = turn_id
        self.seq = seq
        self.superseded = superseded
        self.rev = 1

    def to_report_section(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, **self.payload}


class _Store:
    def __init__(self, sections: list[_Sec] | None = None) -> None:
        self.sections = sections or []
        self.appends: list[_Sec] = []
        self.patch_turns: list[str | None] = []

    async def list_sections(self, canvas_id: str) -> list[_Sec]:
        return list(self.sections)

    async def patch_section(
        self, *, section_id: str, patch: dict[str, Any], expected_rev: int | None = None, by_turn_id: str | None = None, **_: Any
    ) -> _Sec:
        sec = next(s for s in self.sections if s.id == section_id)
        sec.payload = {**sec.payload, **patch}
        sec.rev += 1
        self.patch_turns.append(by_turn_id)
        return sec

    async def append_section(self, *, canvas_id: str, kind: str, payload: dict[str, Any], by_turn_id: str | None = None) -> _Sec:
        sec = _Sec(f"sec_{len(self.sections) + 1}", kind, payload, by_turn_id, len(self.sections) + 1)
        self.sections.append(sec)
        self.appends.append(sec)
        return sec


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _entry(aid: str, label: str | None = None, tool: str = "bi_query", query: str | None = None) -> dict[str, Any]:
    return {"label": label or f"{tool}: {query or aid}", "tool": tool, "artifact_id": aid, "query": query}


def _sources_sec(entries: list[dict[str, Any]], *, turn_id: str, sid: str = "sec_src", seq: int = 1, superseded: bool = False) -> _Sec:
    return _Sec(sid, "sources", {"label": "数据来源", "entries": entries}, turn_id, seq, superseded)


async def _upsert(store: _Store, *, turn_id: str, entries: list[dict[str, Any]]) -> str | None:
    return await upsert_turn_sources_section(canvas_store=store, bus=_Bus(), canvas_id="cv_1", turn_id=turn_id, entries=entries)


# ── 1. Fold rather than replace (no backfill and no loss of history) ─────────


@pytest.mark.anyio
async def test_upsert_folds_instead_of_replacing_when_caller_only_has_this_turn() -> None:
    """★ 设计 v1 的致命缺口：6 个建表点只有 3 个回灌，而 patch 是全量替换。

    这里模拟没回灌的调用方（registry 里只有本轮那一条）——落库必须仍是 A,B,C+D。
    漏接线（照收 entries 全量替换）⇒ 第一条断言就红。
    """
    store = _Store([_sources_sec([_entry("art_a"), _entry("art_b"), _entry("art_c")], turn_id="t1")])
    await _upsert(store, turn_id="t5", entries=[_entry("art_d")])

    got = store.sections[0].payload["entries"]
    assert [e["artifact_id"] for e in got] == ["art_a", "art_b", "art_c", "art_d"]
    assert len(store.appends) == 0, "画布作用域下不许再 append 第二份来源段"


@pytest.mark.anyio
async def test_upsert_patch_carries_turn_id_into_the_ledger() -> None:
    """patch 不传 by_turn_id ⇒ 修订账本不认这次来源更新是哪一轮干的。"""
    store = _Store([_sources_sec([_entry("art_a")], turn_id="t1")])
    await _upsert(store, turn_id="t5", entries=[_entry("art_d")])
    assert store.patch_turns == ["t5"]


# ── 2. Continuous numbering across turns ──────────────────────────────────


@pytest.mark.anyio
async def test_numbering_continues_across_turns() -> None:
    """turn5 引用 turn1 用过的 artifact → 复用旧编号；新 artifact → 接在后面。"""
    prior = [_entry("art_a"), _entry("art_b")]
    store = _Store([_sources_sec(list(prior), turn_id="t1")])

    reg = TurnSourcesRegistry()
    reg.seed(store.sections[0].payload["entries"])
    assert reg.register_source("art_b", tool="bi_query") == 2, "同一 artifact 必须复用旧编号"
    assert reg.register_source("art_c", tool="bi_query") == 3, "新来源接在后面"

    await _upsert(store, turn_id="t5", entries=reg.sources)
    assert [e["artifact_id"] for e in store.sections[0].payload["entries"]] == ["art_a", "art_b", "art_c"]


# ── 3. INV-2: historical entry positions and numbers never change ──────────


def test_seed_preserves_position_even_for_rows_that_would_dedup() -> None:
    """★ 为什么 seed 不能复用 `_add_entry`：已落库的编号是文本里的死数字。

    构造两条会被去重规则折掉的行（都无 aid、label 相同）——`seed` 必须原样保两位，
    否则它后面所有 `[N]` 集体前移。
    """
    stored = [
        {"label": "同名来源", "tool": "bi_query", "artifact_id": None, "query": None},
        {"label": "同名来源", "tool": "bi_query", "artifact_id": None, "query": None},
        {"label": "第三条", "tool": "bi_query", "artifact_id": "art_c", "query": None},
    ]
    reg = TurnSourcesRegistry()
    reg.seed(stored)
    assert reg.size() == 3
    assert reg.sources[2]["artifact_id"] == "art_c", "第三条必须还在第 3 位"


def test_fold_never_moves_or_drops_prior_rows() -> None:
    prior = [_entry("art_a"), _entry("art_b"), _entry("art_c")]
    folded = fold_source_entries(prior, [_entry("art_c"), _entry("art_d")])
    assert [e["artifact_id"] for e in folded] == ["art_a", "art_b", "art_c", "art_d"]
    assert folded[:3] == prior, "前缀逐条同一（位置与编号都不动）"


def test_fold_grafts_preview_onto_existing_row_without_moving_it() -> None:
    """INV-2 只保「位置与编号」——行内补充（preview 嫁接）是合法的，判据不能断言逐字节。"""
    prior = [_entry("art_a"), _entry("art_b")]
    incoming = [{**_entry("art_a"), "preview_rows": [["1"]], "preview_columns": ["c"]}]
    folded = fold_source_entries(prior, incoming)
    assert len(folded) == 2
    assert folded[0]["preview_rows"] == [["1"]]


# ── 4. Existing-state gate: do not merge already-diverged canvases ─────────


@pytest.mark.anyio
async def test_forked_canvas_falls_back_to_turn_scope() -> None:
    """★ 合并会让 numberedLen 变长 ⇒ 历史上超范围而哑掉的 chip 复活成「会点、指错」。

    116 实测 25 个（5 个画布）。所以已经有 ≥2 段的画布退回 turn 作用域：不变好也不变坏。
    """
    store = _Store(
        [
            _sources_sec([_entry("art_a")], turn_id="t1", sid="sec_1", seq=1),
            _sources_sec([_entry("art_b")], turn_id="t2", sid="sec_2", seq=2),
        ]
    )
    await _upsert(store, turn_id="t3", entries=[_entry("art_c")])

    assert len(store.appends) == 1, "已分叉 ⇒ 照旧为本轮 append 第三段"
    assert store.sections[0].payload["entries"] == [_entry("art_a")], "第一段一条都不许动"
    assert store.sections[1].payload["entries"] == [_entry("art_b")]


# ── 5. Kill switch: disabling restores current multi-turn canvas behavior byte for byte ──


# ── 6. INV-3: one criterion for reads and writes ────────────────────────────


def test_entries_not_a_list_is_skipped_by_both_sides() -> None:
    """entries 缺失/非数组的段前端会跳过；后端选中它就等于往一个没人读的段里写。"""
    bad = _Sec("sec_bad", "sources", {"label": "数据来源"}, "t1", 1)
    good = _sources_sec([_entry("art_a")], turn_id="t2", sid="sec_good", seq=2)
    assert qualifying_sources_sections([bad, good]) == [good]
    picked, canvas_scope = find_canvas_sources_section([bad, good], turn_id="t9")
    assert picked is good and canvas_scope is True


def test_superseded_section_is_skipped() -> None:
    folded_away = _sources_sec([_entry("art_a")], turn_id="t1", sid="sec_old", seq=1, superseded=True)
    live = _sources_sec([_entry("art_b")], turn_id="t2", sid="sec_live", seq=2)
    assert qualifying_sources_sections([folded_away, live]) == [live]


_FE_CONTEXT = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "lib" / "canvas-pm-adapter" / "canvas-context.tsx"


def _fe_selector_body(src: str) -> str:
    """抠出 `numberedSources` 那个 memo 的函数体。

    ★分隔符不能用「依赖数组的字面量」——它一改（本批就加了 `citationSources`）
    `str.split` 找不到分隔符会**整段返回**，判据的作用域静默放大到整份文件而不报错。
    这里用「memo 结束 + 下一个 const」定界，并断言真的截到了东西。
    """
    assert "const numberedSources = useMemo" in src, "FE 选段器改名/搬家了——对拍已失效，先修对拍"
    body = src.split("const numberedSources = useMemo", 1)[1].split("const sourceLookup", 1)[0]
    assert 0 < len(body) < len(src) / 2, "没截到 memo 体（分隔符失效），判据的作用域被放大了"
    return body


def test_fe_selector_is_parseable() -> None:
    """★ 正对照：FE 换写法后对拍不许静默空跑（打法照 test_canvas_rank_parity）。"""
    src = _FE_CONTEXT.read_text(encoding="utf-8")
    body = _fe_selector_body(src)
    assert re.search(r'kind\s*!==\s*"sources"', body)
    assert re.search(r"\.superseded\)\s*continue", body), "superseded 判据要断言**代码形状**——裸子串会被同区域的注释喂饱"
    assert "Array.isArray" in body


def test_backend_selector_matches_fe_three_conditions() -> None:
    """三个条件逐条对齐：kind / superseded / entries 是数组。"""
    src = _FE_CONTEXT.read_text(encoding="utf-8")
    body = _fe_selector_body(src)
    fe_conditions = {
        "kind": bool(re.search(r'kind\s*!==\s*"sources"', body)),
        "superseded": bool(re.search(r"\.superseded\)\s*continue", body)),
        "entries_is_array": "Array.isArray" in body,
    }
    wrong_kind = _Sec("s1", "narrative", {"entries": []}, "t1", 1)
    superseded = _sources_sec([], turn_id="t1", sid="s2", seq=2, superseded=True)
    no_entries = _Sec("s3", "sources", {}, "t1", 3)
    ok = _sources_sec([_entry("art_a")], turn_id="t1", sid="s4", seq=4)
    assert fe_conditions == {"kind": True, "superseded": True, "entries_is_array": True}
    assert qualifying_sources_sections([wrong_kind, superseded, no_entries, ok]) == [ok]


# ── 7. Scan guard for the single entry point ───────────────────────────────


_SRC_ROOTS = [
    Path(__file__).resolve().parents[2] / "bowhead_core" / "src",
    Path(__file__).resolve().parents[2] / "bowhead_gateway" / "src",
]


def test_ensure_registry_has_a_single_entry_point() -> None:
    """★ 设计 v1 就栽在「6 个建表点只有 3 个回灌」。

    收成单一入口后加这条闸：`ensure_turn_sources_registry(` 只许出现在
    `get_turn_sources_registry` 内部。正对照 = 先断言真的扫到了那个入口
    （防它改名后判据恒真——守卫要断言行为，不能被自己的注释喂饱）。
    """
    hits: list[str] = []
    entry_found = False
    for root in _SRC_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name == "sources_resolver.py":
                entry_found = "async def get_turn_sources_registry(" in text
                continue
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", "*")):
                    continue
                if "ensure_turn_sources_registry(" in line:
                    hits.append(f"{path.name}:{i}")
    assert entry_found, "单一入口 get_turn_sources_registry 不见了——判据先失效了"
    assert hits == [], f"这些地方绕过了单一入口（漏回灌 = 新来源从 [1] 起铸撞号）：{hits}"


# ── 8. Fixes from the second review ────────────────────────────────────────


@pytest.mark.anyio
async def test_rehydrate_reports_failure_so_the_flag_does_not_latch() -> None:
    """★ code-review E：`rehydrate_turn_registry` 自己吞异常后正常返回，
    调用方「没抛就算灌成了」⇒ 一次瞬时 store 故障就把防双灌位latch 住，
    本轮后面所有取表都短路，`[N]` 从 1 起铸、折叠后指到历史条目上。
    """
    from bowhead_core.sources_resolver import rehydrate_turn_registry

    class _Boom:
        async def list_sections(self, canvas_id: str) -> list[Any]:
            raise RuntimeError("transient")

    reg = TurnSourcesRegistry()
    assert await rehydrate_turn_registry(reg, canvas_store=_Boom(), canvas_id="cv_1", turn_id="t1") is False

    # Positive control: return True when readable (otherwise False could just be a constant).
    ok_store = _Store([_sources_sec([_entry("art_a")], turn_id="t1")])
    reg2 = TurnSourcesRegistry()
    assert await rehydrate_turn_registry(reg2, canvas_store=ok_store, canvas_id="cv_1", turn_id="t9") is True
    assert reg2.size() == 1


def test_sources_sections_never_fold_into_history() -> None:
    """★ code-review G：画布级来源段今天只是因为「本轮 patch 过」才躲开 α 折叠——
    那是副作用不是保证。被折走 ⇒ 下一轮从 [1] 重建 ⇒ 历史 [N] 集体指错。"""
    from bowhead_core.canvas_revise import prior_block_ids

    sections = [
        _Sec("sec_old_n", "narrative", {"markdown": "x"}, "t1", 1),
        _sources_sec([_entry("art_a")], turn_id="t1", sid="sec_src", seq=2),
    ]
    ids = prior_block_ids(sections, this_turn_id="t2")
    assert "sec_old_n" in ids, "上一轮正文照旧算旧块（正对照）"
    assert "sec_src" not in ids, "来源段永不折叠"
