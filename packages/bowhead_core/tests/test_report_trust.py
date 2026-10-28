"""Tests for the shared report-trust impl + the composer's compose-time trust payload (teams trust-fix).

`compute_report_trust` moved to bowhead_core so the canvas composer can compute trust ONCE, at
compose-time, over the raw (pre number-render-strip) section tree and ride it on canvas.done.
"""

from __future__ import annotations

from bowhead_core.canvas_composer import _canvas_trust_payload
from bowhead_core.report_trust import compute_report_trust


def test_compute_trust_gen2_not_counted_default() -> None:
    """citation-audit M1-4: 退役 gen2 标记不再计数——verified 只认有效 [N],
    unverified 只认结构化字段（未迁移存量 → 不显误导性 0%）。"""
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": "营收 [num:347100:CNY:src:tl_a] 增长", "unverified_numbers": ["毛估 X"]},
            {"kind": "sources", "entries": [{"label": "a"}, {"label": "b"}]},
        ]
    )
    assert (stats.verified, stats.unverified, stats.sources) == (0, 1, 2)
    # C-P2-2: verified==0 → pct=None (hide misleading 0%; still show unverified count)
    assert stats.verified_pct is None


def test_compute_trust_ignores_retired_markers(monkeypatch) -> None:
    """M3-2：gen2 计数分支与 kill-switch 一并删除后，退役标记**不再计任何数**。

    （原 `test_compute_trust_counts_src_markers_kill_switch` 锁的是 kill-switch
    复旧口径——开关随 M3 退役，锁改为断言新世界的行为：库里已无 gen2〔M2 洗净〕，
    万一混进来也只当普通文本，不给 verified 计数。）
    """
    monkeypatch.setenv("BOWHEAD_GEN2_MARKERS", "1")  # Switch no longer exists → no effect
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": "营收 [num:347100:CNY:src:tl_a] 增长", "unverified_numbers": ["毛估 X"]},
            {"kind": "sources", "entries": [{"label": "a"}, {"label": "b"}]},
        ]
    )
    assert (stats.verified, stats.unverified, stats.sources) == (0, 1, 2)
    assert stats.verified_pct is None  # verified==0 → hide % (C-P2-2)


def test_compute_trust_counts_numbered_refs_backed_by_sources() -> None:
    # current generation: plain numbers + [N] refs; [N] verified only when N ≤ sources entries.
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": "A 是 17.11% [1]，B 是 90 亿 [2]，越界 [9]"},
            {"kind": "sources", "entries": [{"label": "a"}, {"label": "b"}]},
        ]
    )
    assert stats.verified == 2 and stats.sources == 2  # [1],[2] count; [9] > sources ignored


def test_compute_trust_silent_when_nothing() -> None:
    assert compute_report_trust([{"kind": "narrative", "markdown": "no numbers"}]).silent


class _Sec:
    def __init__(self, d):
        self._d = d

    def to_report_section(self):
        return self._d


async def test_canvas_trust_payload_over_turn_sections() -> None:
    class _Store:
        def __init__(self, secs):
            self._secs = secs

        async def list_sections_appended_by_turn(self, _tid):
            return self._secs

    store = _Store(
        [
            _Sec({"kind": "narrative", "markdown": "营收 962.7亿元 [1] 增"}),
            _Sec({"kind": "sources", "entries": [{"label": "a"}]}),
        ]
    )
    payload = await _canvas_trust_payload(store, "t1")
    assert payload == {"verified": 1, "unverified": 0, "sources": 1, "verified_pct": 100}


async def test_canvas_trust_payload_none_when_no_turn_sections() -> None:
    class _Empty:
        async def list_sections_appended_by_turn(self, _tid):
            return []

    assert await _canvas_trust_payload(_Empty(), "t1") is None


async def test_canvas_trust_payload_none_on_store_failure() -> None:
    class _Bad:
        async def list_sections_appended_by_turn(self, _tid):
            raise RuntimeError("boom")

    assert await _canvas_trust_payload(_Bad(), "t1") is None


# ─── _emit_canvas_done attaches authoritative trust (the REAL producer path) ──


class _CapBus:
    def __init__(self, store=None):
        self._d6_canvas_store = store
        self.emitted = []

    async def emit(self, ev):
        self.emitted.append(ev)


class _TurnStore:
    def __init__(self, secs):
        self._secs = secs

    async def list_sections_appended_by_turn(self, _tid):
        return self._secs


async def test_emit_canvas_done_attaches_trust_from_store() -> None:
    # the real producer: every composer canvas.done goes through _emit_canvas_done, which computes
    # authoritative trust over the turn's persisted sections (closes the "trust-less canvas.done" gap).
    from bowhead_core.canvas_composer import _emit_canvas_done

    bus = _CapBus(
        _TurnStore(
            [
                _Sec({"kind": "narrative", "markdown": "营收 962.7亿元 [1] 增，利润 8.4亿元 [1]"}),
                _Sec({"kind": "sources", "entries": [{"label": "a"}]}),
            ]
        )
    )
    await _emit_canvas_done(bus, "t1", {"canvas_id": "c", "section_count": 2, "errors": [], "source": "payload"})
    assert len(bus.emitted) == 1
    ev = bus.emitted[0]
    assert ev.event_type == "canvas.done"
    assert ev.payload["trust"] == {"verified": 2, "unverified": 0, "sources": 1, "verified_pct": 100}


async def test_emit_canvas_done_keeps_presupplied_trust() -> None:
    from bowhead_core.canvas_composer import _emit_canvas_done

    bus = _CapBus(_TurnStore([_Sec({"kind": "narrative", "markdown": "[num:9:U:src:z]"})]))
    pre = {"verified": 5, "unverified": 0, "sources": 3, "verified_pct": 100}
    await _emit_canvas_done(bus, "t1", {"canvas_id": "c", "source": "x", "trust": pre})
    assert bus.emitted[0].payload["trust"] == pre  # not recomputed


async def test_emit_canvas_done_no_store_no_trust() -> None:
    from bowhead_core.canvas_composer import _emit_canvas_done

    bus = _CapBus(store=None)
    await _emit_canvas_done(bus, "t1", {"canvas_id": "c", "source": "x"})
    assert "trust" not in bus.emitted[0].payload  # graceful: no store → no trust key


def test_trust_denominator_uses_the_real_unverified_count_not_the_capped_list() -> None:
    """★★ 可信度分母用真实条数：列表被截到 10/20 条只为少渲几条，拿它当分母会往乐观方向说谎。

    30 条未核验被记成 10 条时：10 条已核验的报告会显示 50%，而真实是 25%。
    **一个朝乐观方向说谎的可信度指标，比没有这个指标更糟**（护栏字段的数值同样要验）。
    """
    md = "、".join(f"数字{i} [1]" for i in range(10))  # Ten sourced [1] references
    capped = ["毛估 X"] * 10  # Writer capped the list at 10 entries
    sources = {"kind": "sources", "entries": [{"label": "a"}]}

    real = compute_report_trust([{"kind": "narrative", "markdown": md, "unverified_numbers": capped, "unverified_total": 30}, sources])
    assert (real.verified, real.unverified) == (10, 30), f"分母该用真实条数（实际 {real.unverified}）"
    assert real.verified_pct == 25

    # Negative control: remove the actual total from the same input (legacy section) ⇒ use list length, inflating percentage to 50%
    legacy = compute_report_trust([{"kind": "narrative", "markdown": md, "unverified_numbers": capped}, sources])
    assert (legacy.verified, legacy.unverified) == (10, 10)
    assert legacy.verified_pct == 50, "这组夹具必须能区分两种算法，否则是空测"


def test_trust_denominator_ignores_a_smaller_total_than_the_list() -> None:
    """★ `unverified_total` 比列表还短 ⇒ 数据坏了，按列表长度算（只朝「更不可信」的方向兜底）。"""
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": "数字 [1]", "unverified_numbers": ["a", "b", "c"], "unverified_total": 1},
            {"kind": "sources", "entries": [{"label": "a"}]},
        ]
    )
    assert stats.unverified == 3


def test_empty_list_ignores_a_stale_total() -> None:
    """★★★ 列表被清空后残留的 `unverified_total` 不许被采信。

    `patch_section` 是**浅合并**，而「`unverified_numbers: []` 清除 stale 警示」是既有约定：
    只清列表、留下旧总数的段会让一份本该 100% 的报告显示 3%，而界面上一个警示都没有
    （`UnverifiedNumbersNote` 对空列表 return null，strip 却渲出「30 项待核」的按钮，
    点下去找不到任何元素）。这是**反向**说谎，比虚高更刺眼。
    """
    sections = [
        {"kind": "narrative", "markdown": "新正文 [1]", "unverified_numbers": [], "unverified_total": 30},
        {"kind": "sources", "entries": [{"label": "a"}]},
    ]
    stats = compute_report_trust(sections)
    assert (stats.verified, stats.unverified, stats.verified_pct) == (1, 0, 100)


def test_unverified_count_matches_the_frontend_predicate() -> None:
    """★★ 与前端 `unverifiedCount` 逐条同口径 —— 同一份 section 两边必须给同一个数。

    ⚠ 第一版两边不一致：Python 按 `isinstance(int)` 判（拒整数值的 float），
    JS 的 `Number.isInteger(30.0)` 为真；TS 侧还没有上界（构造得出 1e21）。
    下面这组夹具与 `report-trust.test.ts` 里的**逐字相同**，改一侧必须同步另一侧。
    """
    from bowhead_core.report_trust import unverified_count

    cases = [
        ({"unverified_numbers": ["a"] * 10, "unverified_total": 30}, 30),
        ({"unverified_numbers": ["a"] * 10}, 10),
        ({"unverified_numbers": [], "unverified_total": 30}, 0),
        ({"unverified_numbers": ["a", "b", "c"], "unverified_total": 1}, 3),
        ({"unverified_numbers": ["a"] * 3, "unverified_total": 30.0}, 30),  # Accept integral float
        ({"unverified_numbers": ["a"] * 3, "unverified_total": 30.5}, 3),  # Reject non-integral value
        ({"unverified_numbers": ["a"], "unverified_total": 10**21}, 1),  # Reject above upper bound
        ({"unverified_numbers": ["a"], "unverified_total": True}, 1),  # bool subclasses int
        ({"unverified_numbers": ["a"], "unverified_total": "30"}, 1),
        ({}, 0),
    ]
    for section, expected in cases:
        assert unverified_count(section) == expected, f"{section} → {unverified_count(section)}，应为 {expected}"


def test_apply_unverified_writes_and_clears_the_pair_together() -> None:
    """★★ 列表与总数**成对**写：分开写迟早漏（复审实测五个维护点我只覆盖了一个）。"""
    from bowhead_core.report_trust import apply_unverified

    payload: dict = {}
    apply_unverified(payload, [f"数字{i}" for i in range(30)], cap=10)
    assert len(payload["unverified_numbers"]) == 10 and payload["unverified_total"] == 30

    # Clear both keys together; clearing only the list leaves a stale denominator
    apply_unverified(payload, [])
    assert payload["unverified_numbers"] == [] and "unverified_total" not in payload

    # Whitespace / non-strings do not count
    payload2: dict = {}
    apply_unverified(payload2, ["  ", "", 5, None, "真的"])  # type: ignore[list-item]
    assert payload2["unverified_numbers"] == ["真的"] and payload2["unverified_total"] == 1
