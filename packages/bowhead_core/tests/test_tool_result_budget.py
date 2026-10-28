"""ψ-B per-tool result token cap UT."""

import pytest
from bowhead_core.tool_result_budget import DEFAULT_CAPS_TOKEN, cap_result


@pytest.fixture(autouse=True)
def _bi_query_budget_of_1500(monkeypatch: pytest.MonkeyPatch) -> None:
    """These cases exercise the trimming order, so they pin a small budget; the default is covered separately."""
    monkeypatch.setitem(DEFAULT_CAPS_TOKEN, "bi_query", 1500)


def _big_bi_query(rows_count: int = 30) -> dict:
    return {
        "columns": ["品牌", "销量", "同比销售量", "同比变化率"],
        "rows": [{"品牌": f"brand-{i}", "销量": 1000 + i, "同比销售量": 950 + i, "同比变化率": -0.02} for i in range(rows_count)],
        "row_count": rows_count,
        "total_rows": rows_count,
        "_rows_full_count": rows_count,
        "artifact_id": "art_abc123",
        "_preflight_hint": "⚠ rows 已截断..." * 20,
        "_how_to_use_in_sandbox": "把数据传给 python_sandbox..." * 20,
    }


@pytest.mark.unit
def test_bi_query_caps_at_10_rows():
    """bi_query with 30 rows → cap to 10."""
    r = _big_bi_query(rows_count=30)
    out, saved = cap_result("bi_query", r)
    assert len(out["rows"]) == 10
    assert saved > 0


@pytest.mark.unit
def test_bi_query_preserves_artifact_id():
    """artifact_id 永不丢."""
    r = _big_bi_query(rows_count=30)
    out, _ = cap_result("bi_query", r)
    assert out.get("artifact_id") == "art_abc123"


@pytest.mark.unit
def test_bi_query_preserves_columns():
    """columns list 永不丢."""
    r = _big_bi_query(rows_count=30)
    out, _ = cap_result("bi_query", r)
    assert out.get("columns") == ["品牌", "销量", "同比销售量", "同比变化率"]


@pytest.mark.unit
def test_bi_query_preserves_total_rows():
    """total_rows / _rows_full_count 永不丢."""
    r = _big_bi_query(rows_count=1081)
    out, _ = cap_result("bi_query", r)
    assert out.get("_rows_full_count") == 1081 or out.get("total_rows") == 1081


@pytest.mark.unit
def test_bi_query_cap_keeps_the_truncation_sentence():
    """截断提醒不能在数据最大时整条被删（bi-answer-correctness C）。

    旧实现第一步就 pop 整个 `_preflight_hint`——「只给你看了一部分」恰好在里面。现在先裁行，
    仍超预算才把提示换成只剩截断那一句（带全量行数）；`row_count`（全量）不动、`rows_shown` 随裁剪同步。
    """
    r = _big_bi_query(rows_count=1081)
    r["_preflight_hint"] = "其他说明" * 3000  # Force still-over-budget-after-trimming branch
    out, _ = cap_result("bi_query", r)
    assert out["rows_shown"] == 10
    assert out["row_count"] == 1081
    assert "1081" in out["_preflight_hint"]
    assert "其他说明" not in out["_preflight_hint"]


@pytest.mark.unit
def test_bi_query_cap_keeps_the_unknown_words_sentence():
    """「datarc 没认出」那句同样不能在裁剪时丢（bi-answer-correctness A）——数据越大越需要它。"""
    r = _big_bi_query(rows_count=1081)
    r["_preflight_hint"] = "其他说明" * 3000
    r["_parse"] = {"tokens": [], "unknown_words": ["华东地"], "half_match_values": []}
    out, _ = cap_result("bi_query", r)
    assert "1081" in out["_preflight_hint"]
    assert "「华东地」" in out["_preflight_hint"]
    assert "其他说明" not in out["_preflight_hint"]


@pytest.mark.unit
def test_bi_query_cap_keeps_other_hints_and_adds_the_truncation_note():
    """裁完行已在预算内时，其他提醒（如「请求列不存在」）原样保留，**并补上截断那句**。

    ⚠ 这条用例原先断言「提示原样不动」——那钉住的是一个真缺陷：行被从 200 砍到 10，
    模型却一个字都没被告知，于是同参数重试（116 实测 16 次）。保留的是「别把其他提醒
    顺手删掉」这个本意，补的是「裁了就必须说」。
    """
    r = _big_bi_query(rows_count=200)  # Row count exceeds budget to trigger trim; after 10 rows it fits
    r["_preflight_hint"] = "请求列 '季度' 在结果里不存在"
    out, _ = cap_result("bi_query", r)
    assert "请求列 '季度' 在结果里不存在" in out["_preflight_hint"]
    assert "只给你看了 10 行" in out["_preflight_hint"]
    assert out["rows_shown"] == 10


@pytest.mark.unit
def test_bi_query_batch_cap_syncs_rows_shown():
    """合批裁剪砍 sample_rows 时，`rows_shown` 与截断标记同步，`row_count`（全量）不动。"""
    r = {
        "results": [
            {
                "artifact_id": "a1",
                # 10 rows exceed 1500-token budget (trim), then 5 fit per-query budget (no drop-entire-subresult branch).
                "sample_rows": [{"省": f"p{i}", "v": "x" * 1500} for i in range(10)],
                "row_count": 38,
                "rows_shown": 10,
                "total_rows": 38,
                "_rows_truncated": True,
            }
        ]
    }
    out, _ = cap_result("bi_query", r)
    sub = out["results"][0]
    assert len(sub["sample_rows"]) == 5
    assert sub["rows_shown"] == 5
    assert sub["row_count"] == 38
    assert sub["_rows_truncated"] is True


@pytest.mark.unit
def test_bi_query_keeps_a_short_sandbox_howto():
    """**沙箱 how-to 改成降级不删**。

    ⚠ 本条原来的判据是「_how_to_use 也删掉（prompt covers it）」。
    sandbox-input-contract 把那个理由推翻了：数据越大越需要进沙箱算，而裁剪恰恰
    在最需要它的时候把它删了，只剩一句没有形状的 "Use python_sandbox + INPUTS to
    load."（没说 inputs 写哪、也没说代码里怎么取）。116 三十天实测，输入寻址是
    沙箱失败的第一大类（近 16 天 25/50）。短版只要约 40 token，而丢掉它换来的是
    整轮计算失败。
    """
    r = _big_bi_query(rows_count=30)
    out, _ = cap_result("bi_query", r)
    howto = out.get("_how_to_use_in_sandbox")
    assert howto, "沙箱 how-to 不许再被整条丢掉"
    # Need both skeleton and access method; providing only half still cannot execute.
    assert "art_abc123" in howto
    assert "INPUTS['rows']['path']" in howto
    # Truly short version. Threshold is one-sentence scale, not an arbitrary ratio: design promises
    # ≈40 tokens (≈120–200 Chinese characters); the fixture repeats a sentence 20 times, totaling 460 characters.
    assert len(howto) < 200
    assert len(howto) < len(r["_how_to_use_in_sandbox"])


@pytest.mark.unit
def test_bi_query_huge_drops_rows():
    """If still > cap after 10-row trim, drop rows entirely; artifact still saves user."""
    r = _big_bi_query(rows_count=10)
    # Force giant single-row content to overflow even at 10 rows
    r["rows"] = [{"col": "x" * 10000}] * 10
    out, _ = cap_result("bi_query", r)
    assert out.get("rows") == []
    assert "_rows_dropped" in out
    assert out["artifact_id"] == "art_abc123"


@pytest.mark.unit
def test_bi_fetch_rows_always_artifact_only():
    """bi_fetch_rows always drops rows[], regardless of size."""
    r = {
        "rows": [{"col": 1}, {"col": 2}, {"col": 3}],
        "total_rows": 3,
        "artifact_id": "art_xyz",
    }
    out, _ = cap_result("bi_fetch_rows", r)
    assert out["rows"] == []
    assert out["artifact_id"] == "art_xyz"
    assert "_rows_dropped" in out


@pytest.mark.unit
def test_unknown_tool_passthrough():
    """Unknown tool → no cap."""
    r = {"foo": "bar", "data": list(range(100))}
    out, saved = cap_result("add_chart", r)
    assert out == r
    assert saved == 0


@pytest.mark.unit
def test_small_result_passthrough():
    """Small bi_query result < cap → no change."""
    r = {
        "columns": ["a"],
        "rows": [{"a": 1}, {"a": 2}],
        "total_rows": 2,
        "artifact_id": "art_small",
    }
    out, saved = cap_result("bi_query", r)
    assert out == r
    assert saved == 0


@pytest.mark.unit
def test_cap_never_raises_on_bad_input():
    """Malformed result → returns original instead of raise."""
    # Not even a dict
    out, saved = cap_result("bi_query", "not a dict")
    assert out == "not a dict"
    assert saved == 0
    # None
    out2, _ = cap_result("bi_query", None)
    assert out2 is None


@pytest.mark.unit
def test_cap_is_idempotent():
    """Capping a capped result → no further change (within tolerance)."""
    r = _big_bi_query(rows_count=30)
    out1, _ = cap_result("bi_query", r)
    out2, saved2 = cap_result("bi_query", out1)
    # out2 should be effectively identical to out1
    assert len(out2.get("rows", [])) == len(out1.get("rows", []))
    assert out2.get("artifact_id") == out1.get("artifact_id")
    # second pass should save ~0 (already shrunk)
    assert saved2 <= 50  # tolerate noise in token estimate


def _wide_rows(n: int) -> list[dict]:
    """Generate wide synthetic localized rows that exceed the result budget."""
    names = [f"示例门店{i:02d}" for i in range(10)]
    return [
        {"每月": f"2026年{m}月", "门店": names[i % len(names)], "营业额 / 元": 93847561.12345679 + m + i} for m in range(1, 10) for i in range(40)
    ][:n]


def _result_with_metadata(handler_rows: int, *, with_note: bool) -> dict:
    """Build a synthetic handler result with realistic metadata overhead."""
    from bowhead_core.tool_result_budget import rows_truncation_note

    r: dict = {
        "columns": ["每月", "门店", "营业额 / 元"],
        "rows": _wide_rows(handler_rows),
        "row_count": 358,
        "rows_shown": handler_rows,
        "total_rows": 358,
        "artifact_id": "art_000000000001",
        "artifact_filename": "各门店月度营业额.csv",
        "_how_to_use_in_sandbox": '传给 python 沙箱：`inputs={"rows": {"artifact_id": "art_000000000001"}}`，代码里 `pd.read_csv(INPUTS[\'rows\'][\'path\'])`。',
        "query_explain": "按门店维度与月份维度拆分营业额指标，时间范围 2026 年 1 月至 2026 年 9 月",
        "data_source": {"tool": "datarc", "context_id": "ws_x", "query": "2026年1-12月各门店每月营业额"},
        "_debug_csv_filename": "result_0.csv",
        "_debug_download_ms": 1916,
        "_debug_task_uid": "uid_abc123",
        "_preflight_hint": "⚠ 请求区间含尚未结束的 2026年9月（今天 2026-09-24）：该月只有部分天数的数据。",
    }
    if with_note:
        r["_preflight_hint"] += "; " + rows_truncation_note(handler_rows, 358)
    return r


def test_budget_trim_always_restates_the_truncation_note() -> None:
    """Any row trimming must update its disclosure to the final visible row count.

    Cover a handler that supplied every row, one with an earlier truncation note, and
    a small result that needs no further trimming.
    """
    from bowhead_core.tool_result_budget import cap_result

    # ① Handler sees no truncation (all 358 rows supplied), so writes no note; trimming to 10 must add one.
    after, _ = cap_result("bi_query", _result_with_metadata(358, with_note=False))
    hint = str(after.get("_preflight_hint") or "")
    assert len(after["rows"]) == 10 and after["rows_shown"] == 10
    assert "只给你看了 10 行" in hint, f"裁到 10 行却没有提醒：{hint!r}"
    assert "不会给到更多行" in hint, "必须明说再查一次也一样，否则模型会重试"

    # ② Handler says 30 rows; trimming to 10 must rewrite it, never leave stale count.
    after2, _ = cap_result("bi_query", _result_with_metadata(30, with_note=True))
    hint2 = str(after2.get("_preflight_hint") or "")
    assert len(after2["rows"]) == 10 and after2["rows_shown"] == 10
    assert "只给你看了 10 行" in hint2
    assert "只给你看了 30 行" not in hint2, f"陈旧行数还在：{hint2!r}"
    assert "尚未结束" in hint2, "其余提醒不能被顺带丢掉"

    # ③ Negative control: no trimming means no wording changes.
    small = _result_with_metadata(8, with_note=True)
    before_hint = small["_preflight_hint"]
    after3, _ = cap_result("bi_query", dict(small))
    assert len(after3["rows"]) == 8
    assert after3["_preflight_hint"] == before_hint, "没裁就不该改提示"


# Inline-window ordering affects whether a ranking claim is supported.


def _bi_result(rows: list[dict], full: int) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "row_count": full,
        "rows_shown": len(rows),
        "artifact_id": "art_111111111111",
        "_ref_artifact_id": "art_111111111111",
        "_preflight_hint": "",
    }


# Synthetic unsorted values with enough precision to exercise token trimming.
_UNSORTED = [
    {"每月": "2025年1月", "种类大类": "示例类别甲", "营业额 / 元": 32000.125},
    {"每月": "2025年1月", "种类大类": "示例类别乙", "营业额 / 元": 840000.375},
    {"每月": "2025年1月", "种类大类": "示例类别丙", "营业额 / 元": 42000000.625},
    {"每月": "2025年1月", "种类大类": "示例类别丁", "营业额 / 元": 43.0},
    {"每月": "2025年1月", "种类大类": "示例类别戊", "营业额 / 元": 160000.875},
]
# Synthetic descending values for comparison.
_SORTED = [
    {"种类大类": "示例类别己", "营业额 / 元": 5800000000.125},
    {"种类大类": "示例类别庚", "营业额 / 元": 2600000000.375},
    {"种类大类": "示例类别辛", "营业额 / 元": 1800000000.625},
    {"种类大类": "示例类别壬", "营业额 / 元": 1400000000.875},
    {"种类大类": "示例类别癸", "营业额 / 元": 760000000.125},
]


def test_an_unsorted_window_says_so() -> None:
    """An unsorted partial window must not be presented as a top-N ranking."""
    # Trimming retains first 10 rows, so provide enough rows; concatenation remains unsorted (the actual shape was unsorted).
    out, _ = cap_result("bi_query", _bi_result(_UNSORTED * 40, 390))

    hint = out["_preflight_hint"]
    assert "不是按" in hint and "营业额 / 元" in hint, f"没说窗口是乱序：{hint!r}"
    assert "Top N" in hint


def test_a_sorted_window_does_not_get_the_warning() -> None:
    """A genuinely descending window retains only the applicable truncation warning."""
    # Repeating _SORTED would jump back to the first value and break descending order;
    # the initial fixture did this and failed the negative control immediately (fixture error, not code error).
    desc = [{"种类大类": f"类{i:02d}", "营业额 / 元": (200 - i) * 1e8} for i in range(200)]
    out, _ = cap_result("bi_query", _bi_result(desc, 380))

    assert "不是按" not in out["_preflight_hint"]
    # Positive control: fixture really reached trimming (otherwise absence of warning proves nothing).
    assert "只给你看了" in out["_preflight_hint"]


def test_the_truncation_note_is_still_there_either_way() -> None:
    """★ 第二个负对照：新加这句不能把原来那句「只给你看了 N 行」挤掉。"""
    desc = [{"种类大类": f"类{i:02d}", "营业额 / 元": (200 - i) * 1e8} for i in range(200)]
    for rows, full in ((_UNSORTED * 40, 390), (desc, 380)):
        out, _ = cap_result("bi_query", _bi_result(rows, full))
        assert "只给你看了" in out["_preflight_hint"]


def test_the_metric_is_the_last_numeric_column_not_the_first() -> None:
    """Use the final numeric metric rather than an earlier numeric sort-helper column."""
    rows = [
        {"每月": f"2025年{m}月", "月序": m, "种类大类": c, "营业额 / 元": v}
        for m, c, v in ((1, "示例类别甲", 32000.1), (1, "示例类别丙", 42000000.6), (2, "示例类别丁", 43.0)) * 20
    ]

    out, _ = cap_result("bi_query", _bi_result(rows, 456))

    hint = out["_preflight_hint"]
    assert "不是按 `营业额 / 元` 排序" in hint, f"指名了错的列：{hint!r}"
    assert "月序" not in hint


# For multiple queries, shrink metadata before dropping useful rows.

# A long synthetic incomplete-period hint exercises metadata budgeting.
_SEPT_HINT = (
    "⚠ 请求区间含**尚未结束**的 2026年9月（今天 2026-09-26）：该月只有部分天数的数据。"
    "与完整月份做环比/同比会系统性偏低，把它当成「下滑」是取数口径的产物、不是业务现象。"
    "做月度趋势/环比时请排除 9 月，或在结论里明确标注它是不完整月份。"
)
_HOWTO = (
    "python_sandbox 里这样取：inputs={'d': '<artifact_id>'}；代码里 import pandas as pd; "
    "df = pd.read_csv(INPUTS['d']['path'])；列名见 query_explain。更多用法见工具说明中的骨架示例。" * 2
)


def _sub(query: str, rows: list[dict], total: int) -> dict:
    return {
        "ok": True,
        "artifact_id": "art_111111111111",
        "_ref_artifact_id": "art_111111111111",
        "query_text": query,
        "sample_rows": rows,
        "total_rows": total,
        "row_count": total,
        "rows_shown": len(rows),
        "_preflight_hint": _SEPT_HINT,
        "_how_to_use_in_sandbox": _HOWTO,
        "query_explain": {
            "models": ["门店品牌品类表"],
            "measures": ["营业额"],
            "dimensions": ["省"],
            "date_filters": ["「日期」的「2026年」（2026-01-01 至 2026-09-25）"],
        },
    }


_FIVE = [
    {"省": "上海", "营业额 / 元": 3800000000.25},
    {"省": "北京", "营业额 / 元": 3400000000.50},
    {"省": "江苏", "营业额 / 元": 1800000000.75},
    {"省": "天津", "营业额 / 元": 1600000000.25},
    {"省": "河南", "营业额 / 元": 1200000000.50},
]


def test_a_five_row_result_keeps_its_rows_even_when_metadata_blows_the_cap() -> None:
    """Keep a small synthetic result when metadata, rather than rows, exceeds the budget."""
    out, _ = cap_result(
        "bi_query", {"results": [_sub("2026年各省营业额", _FIVE, 5), _sub("各省 种类 营业额", _FIVE, 451), _sub("各省营业额 客流量", _FIVE, 5)]}
    )

    first = out["results"][0]
    assert first["sample_rows"], f"5 行的小结果被清零了：{first.get('_rows_dropped')}"
    assert first["rows_shown"] == 5
    # True values must remain: the model's only basis for identifying the leader.
    assert any("上海" in str(r) for r in first["sample_rows"])


def test_when_rows_really_are_dropped_the_note_stops_claiming_they_are_there() -> None:
    """★★ 丢完必须改口。原先不改 ⇒ 提示写着「只给你看了 30 行」而 `rows_shown` 是 0：
    **告诉模型它有 30 行，它手里一行都没有** —— 编造的完美温床。
    同一个疏漏在 `_cap_bi_query_single` 里补过，这一支漏了。
    """
    huge = [{"省": f"省{i:03d}", "明细": "x" * 4000} for i in range(40)]
    sub = _sub("超大结果", huge, 451)
    sub["_preflight_hint"] = _SEPT_HINT + "\n⚠ 只给你看了 30 行，全量 451 行在 artifact_id 里。"
    sub["_rows_truncated"] = True

    out, _ = cap_result("bi_query", {"results": [sub, _sub("b", _FIVE, 5), _sub("c", _FIVE, 5)]})

    r0 = out["results"][0]
    assert r0["rows_shown"] == 0 and not r0["sample_rows"], "前提：这一条确实被清零了"
    hint = str(r0.get("_preflight_hint") or "")
    assert "30 行" not in hint, f"清零了还说「30 行」：{hint!r}"
    # ★ Positive assertion: never pass by deleting the entire hint; the truncation sentence itself **must remain**
    # (`_notes_to_keep`'s first item); only update its count truthfully.
    # Asserting only absence of 30 rows lets the delete-entire-hint mutation pass (observed survivor).
    assert "只给你看了 0 行" in hint, f"清零后没有照实改口：{hint!r}"


def test_the_metadata_is_what_gets_shrunk_first() -> None:
    """★ 负对照方向：缩的是元数据，不是数据。缩完 `_how_to_use_in_sandbox` 应该变短。"""
    out, _ = cap_result("bi_query", {"results": [_sub("q1", _FIVE, 5), _sub("q2", _FIVE, 5), _sub("q3", _FIVE, 5)]})

    first = out["results"][0]
    assert len(str(first.get("_how_to_use_in_sandbox") or "")) < len(_HOWTO), "沙箱说明没被缩短"
    assert first["sample_rows"], "元数据能缩的时候不该动数据"


def test_the_hint_shrink_alone_can_save_the_rows() -> None:
    """★ 只有「缩提示」这一步能救回来的那一档：沙箱说明本来就短，撑爆预算的全是提示。
    第一版夹具里缩沙箱说明就够了，于是「不缩提示」的突变**存活**——那一步根本没被测到。
    """
    sub = _sub("q", _FIVE, 5)
    sub["_how_to_use_in_sandbox"] = "短"  # Cannot shrink further
    sub["_preflight_hint"] = _SEPT_HINT * 3  # All budget overflow comes from this

    out, _ = cap_result("bi_query", {"results": [sub, _sub("b", _FIVE, 5), _sub("c", _FIVE, 5)]})

    r0 = out["results"][0]
    assert r0["sample_rows"], f"提示能缩却先丢了数据：{r0.get('_rows_dropped')}"
    assert len(str(r0.get("_preflight_hint") or "")) < len(_SEPT_HINT * 3), "提示没被缩"


def test_the_howto_shrink_alone_can_save_the_rows() -> None:
    """★ 只有「缩沙箱说明」能救的那一档：连 `_preflight_hint` 都没有。
    缺了它，「缩完不重新量 token」的突变会被后面那次重新量掩盖掉（实测存活过）。
    """
    sub = _sub("q", _FIVE, 5)
    sub.pop("_preflight_hint")
    sub["_how_to_use_in_sandbox"] = _HOWTO * 3

    out, _ = cap_result("bi_query", {"results": [sub, _sub("b", _FIVE, 5), _sub("c", _FIVE, 5)]})

    r0 = out["results"][0]
    assert r0["sample_rows"], f"沙箱说明能缩却先丢了数据：{r0.get('_rows_dropped')}"


# The budget is for the model's copy; a small result keeps real rows.


def _nine_rows_with_backend_debug() -> dict:
    """Synthetic nine-row, five-column result whose raw size exceeded the cap mostly through
    `_debug_cut_props`, a key `model_view` strips before the model sees the result."""
    return {
        "columns": ["区域", "产品", "月份", "销量", "金额"],
        "rows": [{"区域": f"区域{i}", "产品": "P", "月份": "2026-09", "销量": 100 + i, "金额": 1000.5 + i} for i in range(9)],
        "row_count": 9,
        "rows_shown": 9,
        "artifact_id": "art_rows",
        "_debug_cut_props": [{"word": f"词{i}", "prop": f"m{i}|d{i}|v{i}" * 4} for i in range(120)],
    }


@pytest.mark.unit
def test_backend_only_keys_do_not_spend_the_model_budget() -> None:
    r = _nine_rows_with_backend_debug()
    from bowhead_core.token_estimator import count_tokens_of_obj

    assert count_tokens_of_obj(r) > 1500, "precondition: the raw result is over the cap"
    out, _ = cap_result("bi_query", r)
    assert len(out["rows"]) == 9, f"rows were cut for keys the model never sees: {out.get('_rows_dropped')}"
    assert "_rows_dropped" not in out


@pytest.mark.unit
def test_an_over_budget_small_result_keeps_some_rows_and_says_how_many() -> None:
    r = _nine_rows_with_backend_debug()
    r.pop("_debug_cut_props")
    r["query_explain"] = {"filters": [f"条件{i}：某个很长的筛选说明" * 3 for i in range(25)]}
    from bowhead_core.tool_result_budget import _model_tokens

    assert _model_tokens(r) > 1500, "precondition: the model-visible result is over the cap"
    out, _ = cap_result("bi_query", r)
    shown = len(out["rows"])
    assert 0 < shown < 9, f"expected a smaller window, not all or nothing: {shown}"
    assert out["rows_shown"] == shown and out["row_count"] == 9
    assert f"只给你看了 {shown} 行" in out["_preflight_hint"]
    assert "_rows_dropped" not in out
    assert _model_tokens(out) <= 1500


def test_a_batch_sub_result_over_its_budget_keeps_a_smaller_window() -> None:
    wide = [{"省": f"省{i}", "说明": "较长的单元格内容" * 12} for i in range(5)]
    out, _ = cap_result("bi_query", {"results": [_sub("宽表", wide, 5), _sub("b", _FIVE, 5), _sub("c", _FIVE, 5)]})

    r0 = out["results"][0]
    assert 0 < len(r0["sample_rows"]) < 5, f"expected a smaller window, not all or nothing: {r0.get('_rows_dropped')}"
    assert r0["rows_shown"] == len(r0["sample_rows"])
    assert f"只给你看了 {len(r0['sample_rows'])} 行" in r0["_preflight_hint"]


def test_the_default_budget_leaves_a_typical_thirty_row_result_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default window follows dsh's 12,500 tokens: the handler's 30-row window with ordinary metadata
    reaches the model untouched instead of being cut to 10 rows."""
    monkeypatch.undo()
    from bowhead_core.tool_result_budget import _model_tokens

    r = _big_bi_query(rows_count=30)
    r["query_explain"] = {"measures": ["销量"], "dimensions": ["品牌"], "filters": ["年份 = 2026"]}
    assert DEFAULT_CAPS_TOKEN["bi_query"] == 12500
    assert _model_tokens(r) > 1500, "precondition: the old budget would have trimmed this result"
    out, saved = cap_result("bi_query", r)
    assert out is r and saved == 0
