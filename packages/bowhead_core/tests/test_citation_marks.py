"""citation_marks — M1 地基模块测试（citation-audit sprint）。

锁四个契约：
1. strip_legacy：所有退役语法 bake 成纯文本且**单位必须保留**（禁 salvage 丢单位）。
2. bind_to_numbered：值→[N]，[N] 落在单位之后（禁 "9627万 [1]元"）；已 cite 的
   数字跳过；标题/引用/锚行/代码围栏不绑（**表格行绑**，2026-08-12 起）；ambiguous-drop 保留。
3. convert_gen2_src：显式 :src: 归因 content-addressed 直连编号空间。
4. strip_untrusted_numbered：新鲜 writer 输出里的裸 [N] 剥除（A9 残余面）。
"""

from __future__ import annotations

from typing import Any

from bowhead_core.citation_marks import (
    bind_to_numbered,
    convert_gen2_src,
    format_marker_value,
    has_legacy_markers,
    strip_legacy,
    strip_untrusted_numbered,
)
from bowhead_core.sources_resolver import TurnSourcesRegistry


class _Inv:
    """Minimal tool-invocation stand-in (duck-typed like ToolInvocation)."""

    def __init__(self, call_id: str, tool_name: str = "bi_query", **kw: Any) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.arguments = kw.get("arguments") or {"query": kw.get("query", "q")}
        self.result = kw.get("result")


# ── strip_legacy / format_marker_value ──────────────────────────────────────


def test_strip_standard_src_keeps_unit() -> None:
    # r3 migration P0: salvaging lost units is this function's purpose; never reproduce that loss.
    assert strip_legacy("营收[num:5.2:亿元:src:tl_x]领先") == "营收5.2亿元领先"


def test_strip_standard_src_percent_enum_unit() -> None:
    assert strip_legacy("占比 [num:30:percent:src:tl_7eaa]。") == "占比 30%。"


def test_strip_standard_src_with_highlight() -> None:
    assert strip_legacy("[num:4.18:%:src:call_e5a0#highlight=同比增长]") == "4.18%"


def test_strip_smushed_src_value_asis() -> None:
    assert strip_legacy("规模[num:11.91亿:src:tl_a]") == "规模11.91亿"


def test_strip_plain_two_and_three_part() -> None:
    assert strip_legacy("约 [num:150:亿元] 规模") == "约 150亿元 规模"
    assert strip_legacy("增长 [num:17.8:%]") == "增长 17.8%"
    # Trailing colon, empty unit
    assert strip_legacy("[num:42:]") == "42"
    # Value only
    assert strip_legacy("[num:0.72]") == "0.72"


def test_strip_enum_units_map_like_fe() -> None:
    # Align with FE citation-marks.ts stripLegacy (same source on both sides): ratio→倍.
    assert strip_legacy("[num:1.618:ratio]") == "1.618倍"
    # M3-1: currency enum=raw value+currency code; scale to readable text (old concatenation produced
    # `347100元`; the production deposit card sec_553d83cd99ce became an eleven-digit bare number).
    assert strip_legacy("[num:347100:CNY]") == "34.7万元"


def test_dense_bake_fixture_is_current() -> None:
    """密集对拍语料必须与当前 BE bake 输出一致（impl review P1-1 的机器闸的 BE 半边）。

    人挑的 26 行 golden 是**抽样不是证明**：同一套格式化逻辑写两遍（Python/TS）必然
    在某处分家——review 实锤 BE f-string `.Nf`=round-half-even vs JS toFixed=half-away，
    `[num:12500:CNY]` → `1.2万元` vs `1.3万元`（万档每 1 万撞一次，日常值域），而 6 条
    currency golden 无一命中 tie。

    改了 bake 语义 → 本测试红 → **重生成语料**（FE 侧同 fixture 的逐行对拍随之更新）：

        PYTHONPATH="$(printf '%s:' packages/*/src | sed 's/:$//')" .venv/bin/python - <<'EOF'
        import json, pathlib
        from bowhead_core.citation_marks import strip_legacy
        p = pathlib.Path("packages/bowhead_frontend/lib/canvas-pm-adapter/extensions/__fixtures__/citation-bake-dense.json")
        d = json.loads(p.read_text())
        for row in d["rows"]:
            row["text"] = strip_legacy(row["input"])
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\\n")
        EOF
    """
    import json
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "bowhead_frontend"
        / "lib"
        / "canvas-pm-adapter"
        / "extensions"
        / "__fixtures__"
        / "citation-bake-dense.json"
    )
    rows = json.loads(fixture.read_text(encoding="utf-8"))["rows"]
    assert len(rows) > 150, "密集语料被削薄了——它的价值就在密度"
    stale = [(r["input"], r["text"], strip_legacy(r["input"])) for r in rows if strip_legacy(r["input"]) != r["text"]]
    assert not stale, f"语料过期 {len(stale)}/{len(rows)} 行（bake 语义变了？按 docstring 重生成）: {stale[:5]}"


def test_strip_currency_scale_matches_fe_formatnumvalue() -> None:
    """货币族 bake 缩放（r3「bake = FE formatNumValue 语义」的缩放半边，M1 漏继承）。"""
    assert strip_legacy("[num:7016893124:CNY]") == "70.17亿元"  # Production regression example
    assert strip_legacy("[num:139000000:CNY]") == "1.39亿元"
    assert strip_legacy("[num:78300:CNY]") == "7.8万元"
    assert strip_legacy("[num:9999:CNY]") == "9,999元"  # Below ten thousand: no scaling, with thousands separators.
    assert strip_legacy("[num:1234.5:CNY]") == "1,234.50元"
    assert strip_legacy("[num:5200000:USD]") == "520.0万美元"  # Suffix uses the ASCII map.
    # Never rescale display-shaped units (72 亿元 / 7 万元 corpus occurrences).
    assert strip_legacy("[num:165.35:亿元]") == "165.35亿元"
    assert strip_legacy("[num:13.78:万元]") == "13.78万元"


def test_strip_percent_symbol_is_display_form_never_scaled() -> None:
    """`%` 符号形=已放大的显示形；只有 `percent` **枚举**（i18n-v1 契约=raw 分数）×100。

    prod 15 处 |v|≤1 的 `%` 标记逐条核过上下文，全是真·小百分比（「同比增长仅
    [num:0.66:%] 基本持平」「相对误差降至 [num:0.1:%] 以下」）——放大就是 100 倍
    错数。老 FE md.ts 对 `%` 做 `|v|<=1 → ×100`，chat 里把 0.66% 显示成 66%，
    canvas 侧同期显示 0.66%（债5 的 100× 真实伤害，M3-1 三份合一后消失）。
    """
    assert strip_legacy("同比增长仅 [num:0.66:%] 基本持平") == "同比增长仅 0.66% 基本持平"
    assert strip_legacy("[num:0.1:%]") == "0.1%"
    assert strip_legacy("[num:-0.034:%]") == "-0.034%"
    assert strip_legacy("[num:0.0039:percent]") == "0.39%"  # Fractional enum values ×100
    assert strip_legacy("[num:30:percent]") == "30%"  # Already-scaled enum values are not scaled again.


def test_strip_e_notation_value() -> None:
    assert strip_legacy("[num:1.2e-4:%:src:tl_z]") == "1.2e-4%"


def test_strip_catchall_malformed() -> None:
    # Malformed remnants: salvage value+unit where possible, leaving no bare marker.
    out = strip_legacy("[num:5.2:亿元:src:]")  # Empty src: standard does not match.
    assert "[num:" not in out
    assert "5.2" in out and "亿元" in out


def test_strip_ref_chart_fact_families() -> None:
    assert strip_legacy("结论 [ref:chunk_abc123] 成立") == "结论 成立"
    assert strip_legacy("见图 [chart:sec_1]") == "见图"
    assert strip_legacy("依据 [fact:q3_rev] 与 [bi:s_9] 及 [memory:m_1]") == "依据 与 及"


def test_strip_untouched_content_passthrough() -> None:
    s = "纯文本 739 元与 [5] 编号引用原样保留（数组 [1,2] 也不动）"
    assert strip_legacy(s) == s


def test_has_legacy_markers() -> None:
    assert has_legacy_markers("x [num:1:%] y")
    assert has_legacy_markers("x [ref:c] y")
    assert not has_legacy_markers("x 5.2亿元 [3] y")


def test_format_marker_value_ascii_unknown_unit_spaced() -> None:
    assert format_marker_value("12", "GWh") == "12 GWh"
    assert format_marker_value("7", "") == "7"


# ── strip_untrusted_numbered ─────────────────────────────────────────────────


def test_strip_untrusted_numbered_removes_bare_n() -> None:
    assert strip_untrusted_numbered("旧稿说 5.2亿元 [3]，如上") == "旧稿说 5.2亿元 ，如上"


# ── convert_gen2_src (ingest converter) ────────────────────────────────────────


def test_convert_gen2_src_allocates_registry_n() -> None:
    reg = TurnSourcesRegistry()
    inv = _Inv("tl_abc", tool_name="bi_query", arguments={"query": "各省营业额"})
    out = convert_gen2_src(
        "同比 [num:4.18:%:src:tl_abc] 提升",
        sources=reg.sources,
        id_to_index=reg.id_to_index,
        tool_invocations=[inv],
    )
    # Space-joined value unit [N] is resolve_in_text's current live form (unchanged in this slice);
    # any polishing to attach % belongs in M1-2 closure, updating test_sources_resolver together.
    assert out == "同比 4.18 % [1] 提升"
    assert reg.size() == 1
    assert reg.id_to_index["tl_abc"] == 1


def test_convert_gen2_src_reuses_existing_n() -> None:
    reg = TurnSourcesRegistry()
    reg.register_source("tl_abc", tool="bi_query", query="q")
    out = convert_gen2_src(
        "[num:9.6:亿元:src:tl_abc]",
        sources=reg.sources,
        id_to_index=reg.id_to_index,
        tool_invocations=[],
    )
    assert out == "9.6 亿元 [1]"
    assert reg.size() == 1


# ── bind_to_numbered ─────────────────────────────────────────────────────────


def _reg_with(*ids: str) -> TurnSourcesRegistry:
    reg = TurnSourcesRegistry()
    for i in ids:
        reg.register_source(i, tool="bi_query", query=f"q-{i}")
    return reg


def test_bind_unit_swallow_n_after_unit() -> None:
    # r3 architecture P1-4: [N] must follow the unit.
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "全省合计 9627万元。",
        value_to_source={9627e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out == "全省合计 9627万元 [1]。"
    assert "万 [1]元" not in out


def test_bind_bare_yuan_number_binds_with_n_after_unit() -> None:
    """表达面 e2：裸元形（60,484元 / 351.4元）可绑，[N] 落在 元 后。"""
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "LMDI量贡献+60,484元，单价351.4元。",
        value_to_source={60484.0: "tl_a", 351.4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "+60,484元 [1]" in out
    assert "351.4元 [1]" in out
    assert "元 [1]元" not in out


def test_bind_spaced_yuan_swallows_unit_before_n() -> None:
    """空格元形（2,460 元）：swallow 连空白一起吞，[N] 落在 元 后。"""
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "6月营业额 2,460 元。",
        value_to_source={2460.0: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "2,460 元 [1]" in out
    assert "[1] 元" not in out


def test_bind_spaced_count_unit_does_not_split_word() -> None:
    """review 验证轮实测：隔空白只吞元族——「1,234,567 家长」的 家 是「家长」
    一词的首字，跟空白连吞会渲成「家 [1]长」劈词。"""
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "共有1,234,567 家长表示满意。",
        value_to_source={1234567.0: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "家 [1]长" not in out
    assert "1,234,567 [1] 家长" in out


def test_bind_fullwidth_space_before_yuan_swallows() -> None:
    """U+3000 全角空格也算单位前空白：[N] 落在 元 后。"""
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "客单价为2,460\u3000元。",
        value_to_source={2460.0: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "元 [1]" in out
    assert "[1]\u3000元" not in out


def test_bind_bare_number_without_yuan_still_unbound() -> None:
    """反例对照：无单位裸数（Top 5 / 108350）照旧不绑——保守性不降。"""
    reg = _reg_with("tl_a")
    src = "Top 5 门店贡献 108350"
    out = bind_to_numbered(
        src,
        value_to_source={5.0: "tl_a", 108350.0: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out == src


def test_bind_wan_yuan_still_uses_scale_arm() -> None:
    """「108,350 万元」仍走量级臂（值=万 缩放后），不被元臂按裸数错解。"""
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "投入 108,350 万元。",
        value_to_source={1083500000.0: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "万元 [1]" in out


def test_bind_percent_no_swallow() -> None:
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "增速 17.8%，超预期",
        value_to_source={0.178: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out == "增速 17.8% [1]，超预期"


def test_bind_skips_already_cited_number() -> None:
    reg = _reg_with("tl_a")
    text = "差距 38% [5]。"
    out = bind_to_numbered(
        text,
        value_to_source={0.38: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out == text  # Do not bind twice.


def test_bind_skips_cited_after_unit() -> None:
    reg = _reg_with("tl_a")
    text = "合计 9627万元 [2]。"
    out = bind_to_numbered(
        text,
        value_to_source={9627e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out == text


def test_bind_unbindable_left_plain() -> None:
    reg = _reg_with("tl_a")
    out = bind_to_numbered(
        "预计明年突破 150亿元 大关",
        value_to_source={9627e4: "tl_a"},  # 150亿 is absent from the map.
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "[" not in out


def test_bind_skips_headings_quotes_anchors_fences_but_not_tables() -> None:
    """⚠ **表格行改成绑了**（2026-08-12），其余四种行型照旧跳过。

    此前 `_collect_unverified` 已取消跳过 `|` 行（表格数字进核验），binder 却还跳着 ⇒
    表格数字**只能进 unverified 分母、永远进不了 verified 分子**，
    `pct = verified/(verified+unverified)` 对表格多的报告系统性偏低：内容全对的表里
    只要有一个 `_is_derivable` 覆盖不到的合计行（它没有 `a+b`），pct 就从 100 掉到 50。
    """
    reg = _reg_with("tl_a")
    text = "\n".join(
        [
            "# 标题 9627万元",
            "| 区域 | 9627万元 |",
            "> 引述 9627万元",
            "[figure:sec_1] 附 9627万元",
            "```",
            "code 9627万元",
            "```",
            "正文 9627万元。",
        ],
    )
    out = bind_to_numbered(
        text,
        value_to_source={9627e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    lines = out.split("\n")
    assert lines[0] == "# 标题 9627万元"
    assert lines[1] == "| 区域 | 9627万元 [1] |", "表格行必须能绑，否则表格数字永远进不了分子"
    assert lines[2] == "> 引述 9627万元"
    assert lines[3] == "[figure:sec_1] 附 9627万元"
    assert lines[5] == "code 9627万元"
    assert lines[7] == "正文 9627万元 [1]。"


def test_bind_same_source_reuses_n_two_numbers() -> None:
    reg = _reg_with("tl_a", "tl_b")
    out = bind_to_numbered(
        "上海 9.627亿，全省 9627万元。",
        value_to_source={9.627e8: "tl_b", 9627e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert "9.627亿 [2]" in out
    assert "9627万元 [1]" in out


def test_bind_registry_lazy_add_from_invocation() -> None:
    # value_to_source points to an ID absent from the registry; register lazily via tool_invocations.
    reg = TurnSourcesRegistry()
    inv = _Inv("tl_new", tool_name="bi_query", arguments={"query": "新查询"})
    out = bind_to_numbered(
        "合计 9627万元。",
        value_to_source={9627e4: "tl_new"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
        tool_invocations=[inv],
    )
    assert out == "合计 9627万元 [1]。"
    assert reg.size() == 1


# ── bake_payload_strings (core walker for M1-6's permanent ingress seam) ─────────────


def test_bake_payload_prose_keys_only() -> None:
    from bowhead_core.citation_marks import bake_payload_strings

    payload = {
        "markdown": "营收[num:5.2:亿元:src:tl_x]领先",
        "sql": "SELECT * FROM t WHERE id='art_0123456789ab'",  # Preserve structural fields.
        "code": "x = '[num:1:%]'",  # Preserve code fields.
        "label": "artifact: art_0123456789ab",  # Preserve label (bare art is normal data).
        "cells": [{"lbl": "营收", "val": "[num:96270000:CNY]", "delta": "[num:4.18:%]"}],
        "entries": [{"artifact_id": "art_0123456789ab", "label": "bi_query: q"}],
        "spec": {"title": "各省 [num:9627:万元]", "data_artifact_id": "art_ff"},
        # Production census 2026-07-17: writer-invented out-of-schema field observed to carry markers.
        "impact": {"magnitude": "[num:-60.71:%]"},
    }
    out = bake_payload_strings(payload)
    assert out["markdown"] == "营收5.2亿元领先"
    assert out["sql"] == payload["sql"]
    assert out["code"] == payload["code"]
    assert out["label"] == payload["label"]
    assert out["cells"][0]["val"] == "9627.0万元"  # M3-1: scale currency enums.
    assert out["cells"][0]["delta"] == "4.18%"
    assert out["impact"]["magnitude"] == "-60.71%"
    assert out["entries"][0]["artifact_id"] == "art_0123456789ab"
    assert out["spec"]["title"] == "各省 9627万元"
    assert out["spec"]["data_artifact_id"] == "art_ff"


def test_bake_payload_idempotent_and_marker_free_noop() -> None:
    from bowhead_core.citation_marks import bake_payload_strings

    clean = {"markdown": "纯文本 9627万元 [1]。", "cells": [{"val": "9627万元"}]}
    assert bake_payload_strings(clean) == clean
    once = bake_payload_strings({"markdown": "x [num:1:%] y"})
    assert bake_payload_strings(once) == once


def test_bake_changed_payload_preserves_unchanged_alias_but_bakes_edit() -> None:
    from bowhead_core.citation_marks import bake_changed_payload_strings

    canonical = {
        "markdown": "Revenue [ref:art_private]",
        "nested": {"caption": "Margin [num:13.2:percent]"},
    }
    projected = {
        "markdown": "Revenue [ref:source-ref-v1:alias]",
        "nested": {"caption": "Margin [num:13.2:percent]"},
    }
    assert bake_changed_payload_strings(canonical, projected, projected) == canonical

    edited = bake_changed_payload_strings(
        {
            "markdown": "Updated revenue [ref:art_private]",
            "nested": {"caption": "Updated margin [num:13.2:percent]"},
        },
        {
            "markdown": "Updated revenue [ref:source-ref-v1:alias]",
            "nested": {"caption": "Updated margin [num:13.2:percent]"},
        },
        projected,
    )
    assert edited == {
        "markdown": "Updated revenue",
        "nested": {"caption": "Updated margin 13.2%"},
    }


# ── Cross-end parity (tech-design §5.1; fixture shared with FE citation-plugin.test.ts) ──


def test_binder_output_parity_with_fe_fixture() -> None:
    """BE 产出形状 == parity fixture text——FE 侧对同一 fixture 断言
    findCitedNumberRanges 命中 token（verified-edit-guard 依赖同函数：BE 产出
    FE 认不出 = 被引数字失去防篡改守卫，impl review C-P1-4）。"""
    import json
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "bowhead_frontend"
        / "lib"
        / "canvas-pm-adapter"
        / "extensions"
        / "__fixtures__"
        / "citation-parity.json"
    )
    rows = json.loads(fixture.read_text(encoding="utf-8"))["rows"]
    assert rows, "parity fixture 空了"
    for row in rows:
        reg = TurnSourcesRegistry()
        reg.register_source("tl_par", tool="bi_query", query="parity")
        if row["mode"] == "bind":
            out = bind_to_numbered(
                row["input"],
                value_to_source={float(row["value"]): "tl_par"},
                sources=reg.sources,
                id_to_index=reg.id_to_index,
            )
        elif row["mode"] == "convert":
            out = convert_gen2_src(
                row["input"],
                sources=reg.sources,
                id_to_index=reg.id_to_index,
                tool_invocations=[],
            )
        elif row["mode"] == "bake":
            # M3-4: interlock permanent fallback grammar across both ends; FE citation-marks.ts stripLegacy asserts
            # identical text for identical input. Divergent baking yields different numbers in chat/canvas/persistence;
            # this caused the 100× % incident detected in M3-1.
            out = strip_legacy(row["input"])
        else:  # literal: FE-only row (handwritten writer [cite:K]→[N] form)
            continue
        assert out == row["text"], f"parity row {row!r} → {out!r}"


# ── Forged citations (M3 impl-review security P1-1) ────────────────────────────────────


def test_strip_untrusted_numbered_covers_table_heading_blockquote() -> None:
    """表格/标题/blockquote 里模型手写的裸 [N] 必须被剥掉。

    这是 M1 SEC-P0（peer 手写死号 [1] 被渲成可点「可信引用」并刷 verified 计数）
    从 skip-list 缝里的复活形态：`_SKIP_LINE_PREFIXES` 原本是 **binder 的排版清单**
    （「别往表格行里加 [N]」），被 strip 借来当安全清单——极性相反，配对最坏：
    **binder 从不在这些行铸真 [N]，于是这些行里的裸 [N] 只可能是伪造的，而 strip
    恰恰只放过它们**。BI 报告正文天然大量是表格 → 不是边角输入。
    """
    for name, text in {
        "表格行": "| 指标 | 值 |\n|---|---|\n| 营收 | 100亿 [1] |",
        "标题行": "# 营收 100亿 [1]",
        "blockquote": "> 营收 100亿 [1]",
        "prose（基线，本就覆盖）": "营收 100亿 [1]。",
    }.items():
        assert "[1]" not in strip_untrusted_numbered(text), f"{name}: 伪造 [N] 存活"


def test_strip_untrusted_numbered_keeps_code_fence_indices() -> None:
    """fence 内的 `a[1]` 是数组下标不是引用——唯一保留的跳过（A-P2-3）。"""
    src = "正文 [1] 应剥。\n```python\nx = a[1]\ny = b[2]\n```\n尾部 [3] 应剥。"
    out = strip_untrusted_numbered(src)
    assert "a[1]" in out and "b[2]" in out, "代码块下标被误剥"
    assert "正文  应剥" in out and "尾部  应剥" in out, "fence 外的裸 [N] 漏剥"


def test_forged_table_cannot_inflate_verified_pct() -> None:
    """端到端：零真实工具调用的表格造不出「100% 已验证」。

    这条锁的是**用户实际看到的信任信号**，不只是 strip 的返回值——
    `compute_report_trust` 只判 `1 ≤ N ≤ sources`，不套 strip 的行型规则，
    所以任何漏剥都会直接变成 verified 计数（review 实测 50 行表格 → pct=100%）。
    """
    from bowhead_core.report_trust import compute_report_trust

    forged = "| 指标 | 值 |\n|---|---|\n" + "\n".join(f"| 指标{i} | {i * 100}亿 [1] |" for i in range(50))
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": strip_untrusted_numbered(forged)},
            {"kind": "sources", "entries": [{"n": 1, "title": "某真实但无关的来源"}]},
        ]
    )
    assert stats.verified == 0, f"伪造表格刷出 verified={stats.verified}"
    assert stats.verified_pct != 100, f"伪造表格刷出 pct={stats.verified_pct}%"


def test_binder_skip_list_is_layout_only_not_a_security_boundary() -> None:
    """binder 的跳过清单只剩**排版**语义：标题不绑，表格和正文都绑。

    ⚠ 这条**推翻**了原来的「binder 也跳表格行」。当时那条反向锁是为了防止把
    strip/binder 两份清单又合回去；但它顺带把「表格数字能不能进 verified 分子」
    这件事焊死了。安全性现在**只**由顺序保证（strip 跑在所有铸造者之前），
    行型不再承担任何安全职责 —— 见 `test_citation_bind_table_lines.py` 的顺序守卫。
    """
    text = "| 指标 | 9627万元 |\n# 标题 9627万元\n正文 9627万元。"
    out = bind_to_numbered(
        text,
        value_to_source={9627_0000.0: "tl_a"},
        sources=[{"n": 1, "artifact_id": "tl_a", "title": "T"}],
        id_to_index={"tl_a": 1},
    )
    lines = out.split("\n")
    assert "[1]" in lines[0], f"binder 漏了表格行（表格数字又只能进分母了）: {lines[0]}"
    assert "[1]" not in lines[1], f"binder 往标题行铸了 [N]: {lines[1]}"
    assert "[1]" in lines[2], f"binder 漏了正文行（跳过清单拆错了）: {lines[2]}"
