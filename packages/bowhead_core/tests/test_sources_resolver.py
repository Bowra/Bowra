"""Phase F — sources_resolver.py UT.

Sprint agent-redesign-1 (2026-05-15). Tests:
  - resolve_in_text: [num:V:U:src:S] → [N], [ref:CHUNK] → [N]
  - build_sources_list: peer-emitted first, augment from tool_invocations
  - resolve_payload_inline_refs: walks all text fields
  - back-compat: unresolvable markers preserved as-is (no crash)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.sources_resolver import (
    build_sources_list,
    resolve_in_text,
    resolve_payload_inline_refs,
)


@dataclass
class _Inv:
    """Minimal ToolInvocation stub for tests."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


# ─────────────────────────────────────────────────────────────
# resolve_in_text
# ─────────────────────────────────────────────────────────────


def test_resolve_num_marker_to_numbered() -> None:
    text = "营收 [num:1234567:元:src:call_aaa] 同比"
    inv = _Inv("call_aaa", "bi_query", {"text": "Q3 sales"}, {"artifact_id": "art_aaa"})
    new, _srcs, _ = resolve_in_text(
        text,
        sources=[{"label": "Q3 sales", "tool": "bi_query", "artifact_id": "art_aaa", "query": None}],
        id_to_index={"art_aaa": 1, "call_aaa": 1},
        tool_invocations=[inv],
    )
    assert "[1]" in new
    assert "1234567 元 [1]" in new
    assert "[num:" not in new


def test_resolve_ref_marker_to_numbered() -> None:
    text = "见 [ref:chunk_abc123]"
    new, _, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={"chunk_abc123": 5},
        tool_invocations=[],
    )
    assert new == "见 [5]"


def test_resolve_lazy_alloc_new_id() -> None:
    """Marker references an id not yet in id_to_index → lazy add."""
    sources: list = []
    id2idx: dict = {}
    text = "数据 [num:100:%:src:art_xyz]"
    inv = _Inv("call_z", "bi_query", {"text": "y"}, {"artifact_id": "art_xyz"})
    new, sources, id2idx = resolve_in_text(
        text,
        sources=sources,
        id_to_index=id2idx,
        tool_invocations=[inv],
    )
    assert "[1]" in new
    assert len(sources) == 1
    assert sources[0]["artifact_id"] == "art_xyz"
    assert id2idx["art_xyz"] == 1


def test_lazy_entry_surfaces_flat_sandbox_as_python_sandbox() -> None:
    """批 5ⓑ：扁平入口 `python_exec` 的懒注册来源必须以 `python_sandbox` 出面——
    FE 按该字符串硬匹配来源徽章/文件分组，下发内部入口名等于没徽章 + 裸工具名标签。
    2026-08-29 真机 t_1831b22ed3c1 实测（e2-r2 让懒注册条目首次真产出后暴露）。"""
    sources: list = []
    id2idx: dict = {}
    inv = _Inv(
        "call_flat",
        "python_exec",
        {"code": "# 量价拆解\ndf = pd.read_csv('bridge.csv')\nprint(df)"},
        {"ok": True},
    )
    new, sources, _ = resolve_in_text(
        "量贡献 [num:-3505.52:万元:src:call_flat]",
        sources=sources,
        id_to_index=id2idx,
        tool_invocations=[inv],
    )
    assert "[1]" in new
    assert sources[0]["tool"] == "python_sandbox"
    # Label uses the first non-comment code statement; prefix is the display name (Run code), neither internal entrance python_exec
    # nor pseudo tool python_sandbox. Sources is a credibility surface; do not display tool code names.
    assert "python_exec" not in sources[0]["label"]
    # Label uses the leading intent comment (`# 量价拆解`), not `df = pd.read_csv(...)`:
    # see `test_lazy_sandbox_label_prefers_intent_comment_over_import`
    assert "量价拆解" in sources[0]["label"]


def test_lazy_entry_resolves_a_sandbox_output_artifact_id() -> None:
    """review F2：value map 现在拿 **stdout 产物 id** 当 `:src:`，而它住在
    `result_json.outputs[]` —— `_build_inv_lookup` 不索引那里的话反查不到 invocation，
    条目落进「Unknown id」兜底：label 变「数据源 art_xxx」、tool 空，把展示名收敛
    那半整个架空。两半必须一起成立。

    fixture 用 runner 真记录形状（`name`+`data_role`，无 `filename`）。"""
    sources: list = []
    inv = _Inv(
        "call_sbx",
        "python_exec",
        {"code": "bridge = compute_bridge(df)"},
        {
            "ok": True,
            "result_json": {
                "ok": True,
                "exit_code": 0,
                "stdout": "量贡献 -3,505.52 万元",
                "outputs": [
                    {"name": "sandbox-stdout.txt", "artifact_id": "art_stdout00001", "mime": "text/plain", "size": 42, "data_role": "stdout"},
                ],
            },
        },
    )
    new, sources, id2idx = resolve_in_text(
        "量贡献 [num:-3505.52:万元:src:art_stdout00001]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    assert "[1]" in new
    assert id2idx["art_stdout00001"] == 1
    # Successful reverse lookup: no bare data-source art_… fallback; tool normalized and label includes code
    assert sources[0]["tool"] == "python_sandbox"
    assert "数据源" not in sources[0]["label"]
    assert "bridge = compute_bridge" in sources[0]["label"]


def test_lazy_sandbox_label_prefers_intent_comment_over_import() -> None:
    """2026-09-02 真机 dogfood（`t_7ed4e2864200`）：初版「取首个非注释语句」把
    agent 写的意图注释跳过了，Sources 里显示 `运行代码: import pandas as pd`。

    ⚠ code 逐字取自那一轮的落库 `code_block`，不是自己编的
    （[[feedback_my_own_fixtures_are_the_happy_path]]）。"""
    real_code = (
        "import pandas as pd\n"
        "\n"
        "# 从 bi_query 直接读到的真实值（单值 query，1 行表）\n"
        "# 报告期（最近90天 约2026-06-04~2026-09-02）\n"
        "rev_report = 425983811.2433854   # 营业额 元\n"
        "vol_report = 201773              # 销售量\n"
    )
    sources: list = []
    inv = _Inv("call_real", "python_exec", {"code": real_code}, {"ok": True})
    _, sources, _ = resolve_in_text(
        "营业额 [num:42598.38:万元:src:call_real]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    assert "import pandas" not in sources[0]["label"]
    assert "从 bi_query 直接读到的真实值" in sources[0]["label"]


def test_lazy_sandbox_label_skips_noise_comments_then_falls_back() -> None:
    """编码声明/纯分隔线不算意图；没有实义注释时退首个非 import 语句。"""
    sources: list = []
    inv = _Inv(
        "call_noise",
        "python_exec",
        {"code": "# -*- coding: utf-8 -*-\n# ====================\nimport numpy as np\ntotal = np.sum(values)\n"},
        {"ok": True},
    )
    _, sources, _ = resolve_in_text(
        "合计 [num:1:元:src:call_noise]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    assert sources[0]["label"] == "Run Code: total = np.sum(values)"


def _label_for_code(code: str, call_id: str = "call_x") -> str:
    sources: list = []
    inv = _Inv(call_id, "python_exec", {"code": code}, {"ok": True})
    _, sources, _ = resolve_in_text(
        f"值 [num:1:元:src:{call_id}]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    return str(sources[0]["label"])


def test_lazy_sandbox_label_never_quotes_commented_out_code() -> None:
    """review R3-1：**注释掉的调试代码不是出处**。扫满前 N 行会把
    `# df = df.head(100)  # 调试` 当意图——那一行从没运行过，拿它当数字的来源
    是错误归属。序言区遇到第一条可执行语句就必须停。"""
    label = _label_for_code(
        "import pandas as pd\ndf = pd.read_csv('a.csv')\n# df = df.head(100)  # 调试\ntotal = df.x.sum()\n",
        "call_dbg",
    )
    assert "df.head(100)" not in label and "调试" not in label
    assert "df = pd.read_csv" in label


def test_lazy_sandbox_label_ignores_hash_inside_a_string_literal() -> None:
    """同上：多行字符串里的 markdown 标题不是意图注释。"""
    label = _label_for_code('tpl = """\n# 报告标题\n"""\nout = render(tpl)\n', "call_tpl")
    assert "报告标题" not in label
    assert "tpl =" in label


def test_lazy_sandbox_label_keeps_banner_style_intent_comments() -> None:
    """review R3-2：banner 是 agent 宣告意图最常见的写法之一，不能整条当噪声——
    先剥装饰再看剩不剩内容。"""
    assert _label_for_code("# === 计算营业额 ===\nrev = q1 + q2\n", "call_b1").endswith("计算营业额")
    assert _label_for_code("# -- 用 bi_query 的真实值\nrev = 1\n", "call_b2").endswith("用 bi_query 的真实值")
    assert _label_for_code("# ## 数据准备\ndf = load()\n", "call_b3").endswith("数据准备")
    # Pure decoration remains noise → fall back to a substantive statement
    assert _label_for_code("# ====\nrev = 1\n", "call_b4").endswith("rev = 1")


def test_lazy_sandbox_label_falls_back_when_only_imports() -> None:
    """review R3-3：只有 import（+ 无意图注释）时必须仍给字面，否则退回裸
    `python_sandbox` ⇒ 标签变 `运行代码: python_sandbox`，比改动前更差。"""
    label = _label_for_code("# ====\nimport numpy as np\n", "call_imp")
    assert label == "Run Code: import numpy as np"


def test_pass1_peer_reported_sandbox_tool_is_converged_for_bridging() -> None:
    """review R2-4：peer 自报的 sources 条目（Pass-1）也要收敛工具名——`_add_entry`
    按归一化 `(tool, query)` 把「无 artifact_id 的自报条目」桥接到真条目上；自报写
    `python_exec`、系统侧写 `python_sandbox` 就桥不上，于是并排出现两行数据来源、
    其中一行渲成「该来源不可用」，正是这道桥要防的形态。"""
    payload_srcs = [{"label": "量价拆解计算", "tool": "python_exec", "query": "bridge", "artifact_id": None}]
    sources, _ = build_sources_list(payload_srcs, None)
    assert sources[0]["tool"] == "python_sandbox"
    assert sources[0]["label"] == "量价拆解计算"  # Preserve the self-reported label; do not overwrite it with the tool name


def test_eager_ingest_does_not_register_a_sandbox_run() -> None:
    """review R2-2 订正：**eager 路对沙箱本就不可达**，别再拿编造的形状假装它生效。

    `ingest_tool_invocations` 只在 `result["results"]` 是 list 或 `_extract_artifact_id`
    命中（顶层 `artifact_id` / `_ref_artifact_id` / `results[0]`）时登记，而沙箱 envelope
    （`bowhead_skill/tools.py` `_invoke`）三者都没有——它的 artifact 住在
    `output_artifacts[]` / `result_json.outputs[]`。上一版这条测试喂了
    `{"artifact_id": "art_eager…"}`，一个 `python_exec` 从不返回的形状，于是「F4 修好了」
    是自证的（与 F1 同一失败模式，[[feedback_my_own_fixtures_are_the_happy_path]]）。

    保留 `surface_sandbox_tool_name` 在 eager 路上的接线＝前瞻保护（envelope 若哪天带上
    顶层 artifact_id 就自动正确），但**当下的真实行为是不登记**，这里如实钉住；沙箱来源
    实际由懒注册路产生（见上一条测试）。"""
    from bowhead_core.sources_resolver import TurnSourcesRegistry

    reg = TurnSourcesRegistry()
    # Real envelope shape: no top-level artifact_id; outputs are path strings, artifacts live in output_artifacts
    inv = _Inv(
        "call_eager",
        "python_exec",
        {"code": "df.to_csv('out.csv')"},
        {
            "ok": True,
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
            "outputs": ["out.csv", "sandbox-stdout.txt"],
            "output_artifacts": [
                {"artifact_id": "art_csv00000001", "name": "out.csv", "mime": "text/csv", "size": 12, "data_role": "structured"},
            ],
            "result_json": {"ok": True, "exit_code": 0, "outputs": []},
        },
    )
    reg.ingest_tool_invocations([inv])
    assert reg.sources == [], "沙箱 envelope 没有 eager 登记所需的顶层 artifact_id"


def test_lazy_entry_surfaces_skill_invoke_sandbox_as_python_sandbox() -> None:
    """老入口 skill_invoke(name=python_sandbox) 同款收敛（三入口对齐）。"""
    sources: list = []
    inv = _Inv(
        "call_sk",
        "skill_invoke",
        {"name": "python_sandbox", "input": {"code": "total = a + b"}},
        {"ok": True},
    )
    _, sources, _ = resolve_in_text(
        "合计 [num:12:元:src:call_sk]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    assert sources[0]["tool"] == "python_sandbox"
    assert "total = a + b" in sources[0]["label"]


def test_lazy_entry_keeps_non_sandbox_tool_name() -> None:
    """负对照：非沙箱工具的名字一个字都不许改。"""
    sources: list = []
    inv = _Inv("call_bi", "bi_query", {"text": "近90天营业额"}, {"artifact_id": "art_q"})
    _, sources, _ = resolve_in_text(
        "营业额 [num:1:元:src:art_q]",
        sources=sources,
        id_to_index={},
        tool_invocations=[inv],
    )
    assert sources[0]["tool"] == "bi_query"
    assert "近90天营业额" in sources[0]["label"]


def test_unresolvable_unknown_id_synthesizes_entry() -> None:
    """Unknown src id (no matching inv) → synth minimal entry; [N] still resolves."""
    sources: list = []
    id2idx: dict = {}
    text = "随便 [num:50:%:src:unknown_id]"
    new, sources, _ = resolve_in_text(
        text,
        sources=sources,
        id_to_index=id2idx,
        tool_invocations=[],
    )
    assert "[1]" in new
    assert sources[0]["artifact_id"] == "unknown_id"
    assert "数据源" in sources[0]["label"]


def test_multiple_markers_share_index() -> None:
    """Same src ID appearing twice → same [N]."""
    text = "增 [num:10:%:src:art_a] 同 [num:20:%:src:art_a]"
    new, _, _ = resolve_in_text(
        text,
        sources=[{"label": "x", "tool": "bi_query", "artifact_id": "art_a", "query": None}],
        id_to_index={"art_a": 1},
        tool_invocations=[],
    )
    assert new.count("[1]") == 2


def test_no_markers_text_unchanged() -> None:
    new, _, _ = resolve_in_text(
        "纯文本无引用 increase 5%",
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert new == "纯文本无引用 increase 5%"


# ─────────────────────────────────────────────────────────────
# build_sources_list
# ─────────────────────────────────────────────────────────────


def test_build_sources_from_payload_only() -> None:
    payload_srcs = [
        {"label": "Q3 sales", "tool": "bi_query", "artifact_id": "art_x", "query": "..."},
    ]
    sources, id2idx = build_sources_list(payload_srcs, None)
    assert len(sources) == 1
    assert id2idx["art_x"] == 1


def test_build_sources_augments_kb_chunks() -> None:
    inv = _Inv("call_kb", "kb_search", {"query": "..."}, {"chunks": [{"chunk_id": "chunk_111"}, {"chunk_id": "chunk_222"}]})
    sources, id2idx = build_sources_list([], [inv])
    assert len(sources) == 2
    assert id2idx["chunk_111"] == 1
    assert id2idx["chunk_222"] == 2
    assert sources[0]["tool"] == "kb_search"


def test_build_sources_payload_first_then_kb() -> None:
    payload_srcs = [{"label": "Q3", "tool": "bi_query", "artifact_id": "art_q3", "query": "x"}]
    inv = _Inv("call_kb", "kb_search", {"query": "y"}, {"chunks": [{"chunk_id": "chunk_z"}]})
    sources, id2idx = build_sources_list(payload_srcs, [inv])
    # Order: payload first
    assert sources[0]["artifact_id"] == "art_q3"
    assert sources[1]["artifact_id"] == "chunk_z"
    assert id2idx["art_q3"] == 1
    assert id2idx["chunk_z"] == 2


# ─────────────────────────────────────────────────────────────
# resolve_payload_inline_refs (walks whole payload)
# ─────────────────────────────────────────────────────────────


def test_payload_walk_rewrites_narrative_and_exec_summary() -> None:
    payload = {
        "exec_summary": "Q3 营收 [num:1234:元:src:art_a]",
        "insights": [
            {
                "title": "i1",
                "thesis": "thesis [num:50:%:src:art_a]",
                "narrative": "data [ref:chunk_xx]",
                "evidence": [],
            }
        ],
        "recommendations": [
            {
                "priority": "P1",
                "action": "act on [num:1:次:src:art_a]",
            }
        ],
        "caveats": ["caveat [num:99:个:src:art_a]"],
        "sources": [{"label": "Q3 sales", "tool": "bi_query", "artifact_id": "art_a", "query": "..."}],
    }
    inv_kb = _Inv("call_kb", "kb_search", {"query": "..."}, {"chunks": [{"chunk_id": "chunk_xx"}]})
    out = resolve_payload_inline_refs(payload, [inv_kb])
    # All markers → [N]
    assert "[num:" not in out["exec_summary"]
    assert "[num:" not in out["insights"][0]["thesis"]
    assert "[ref:" not in out["insights"][0]["narrative"]
    assert "[num:" not in out["recommendations"][0]["action"]
    assert "[num:" not in out["caveats"][0]
    # sources includes both art_a + chunk_xx
    assert len(out["sources"]) == 2
    # art_a is [1] (from payload, first); chunk_xx is [2] (augmented)
    assert "[1]" in out["exec_summary"]
    assert "[2]" in out["insights"][0]["narrative"]


def test_payload_no_markers_unchanged_text_but_sources_kept() -> None:
    payload = {
        "exec_summary": "纯散文报告",
        "insights": [{"thesis": "x", "narrative": "y", "evidence": []}],
        "sources": [{"label": "src1", "tool": "bi_query", "artifact_id": "art_1", "query": "q"}],
    }
    out = resolve_payload_inline_refs(payload, [])
    assert out["exec_summary"] == "纯散文报告"
    assert len(out["sources"]) == 1
    assert out["sources"][0]["artifact_id"] == "art_1"


def test_payload_empty_sources_no_invocations_no_crash() -> None:
    payload = {"exec_summary": "无任何引用", "insights": [], "recommendations": []}
    out = resolve_payload_inline_refs(payload, None)
    assert out["sources"] == []


def test_payload_with_string_source_id_only() -> None:
    """Peer emits inline marker but didn't populate sources[]; tool_invocations
    has the matching artifact → auto-augment + N resolves."""
    payload = {
        "exec_summary": "Q3 [num:100:%:src:art_q3]",
        "insights": [],
    }
    inv = _Inv("call_q3", "bi_query", {"text": "Q3 sales"}, {"artifact_id": "art_q3"})
    out = resolve_payload_inline_refs(payload, [inv])
    assert "[1]" in out["exec_summary"]
    assert len(out["sources"]) == 1
    assert out["sources"][0]["artifact_id"] == "art_q3"


def test_back_compat_legacy_num_unbound_preserved() -> None:
    """Legacy `[num:V:U]` (no src:) should NOT be rewritten — frontend handles
    that via NUM_OLD_RE fallback. Phase F resolver only touches bound markers."""
    payload = {
        "exec_summary": "old marker [num:42:元] preserved",
        "insights": [],
    }
    out = resolve_payload_inline_refs(payload, [])
    assert "[num:42:元]" in out["exec_summary"]


# ─────────────────────────────────────────────────────────────
# Prod regression test — t_136ece61 (2026-05-15)
# LLM emits smushed value-and-unit markers; current ν1 regex misses them.
# ─────────────────────────────────────────────────────────────


def test_prod_smushed_marker_136_8pct() -> None:
    """Prod trace t_136ece61 insight_card narrative."""
    text = "西藏以[num:136.8%:src:tl_79cf9d25c029]远超指标"
    new, _, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    # `[num:136.8%:src:...]` resolved to "136.8 % [N]"
    assert "136.8 % [1]" in new
    assert "[num:" not in new


def test_prod_smushed_signed_marker() -> None:
    """Prod trace t_136ece61 insight_card with `+`."""
    text = "Angio-Seal（[num:+60.2%:src:tl_eca592082f2b]）"
    new, _, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "+60.2 % [1]" in new
    assert "[num:" not in new


def test_prod_smushed_integer_marker() -> None:
    text = "达成率低于[num:85%:src:tl_79cf9d25c029]的省份"
    new, _, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "85 % [1]" in new


def test_prod_mixed_standard_and_smushed_in_same_text() -> None:
    """Real prod narrative has both shapes simultaneously."""
    text = "销售达成金额 [num:24.00:亿元:src:tl_a] ，西藏 [num:136.8%:src:tl_a] 远超指标"
    new, sources, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "[num:" not in new
    assert "24.00 亿元 [1]" in new
    assert "136.8 % [1]" in new
    # Same src → same N (only 1 source entry)
    assert len(sources) == 1


def test_prod_tl_locator_as_src_id() -> None:
    """Prod uses `tl_*` (tool-locator) prefix, not just `art_*` or `call_*`.
    Resolver must accept any non-bracket id as a valid src."""
    text = "数据 [num:50%:src:tl_79cf9d25c029]"
    new, sources, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "50 % [1]" in new
    assert sources[0]["artifact_id"] == "tl_79cf9d25c029"


def test_prod_replay_narrative_full_fragment() -> None:
    """Real fragment from t_136ece61 section 'kind=narrative' (seq 3)."""
    text = (
        "2025年全年销售金额达成率 [num:93.35:%:src:tl_79cf9d25c029] ，"
        "销售数量达成率 [num:96.33:%:src:tl_79cf9d25c029] ，"
        "销售达成金额 [num:24.00:亿元:src:tl_79cf9d25c029] "
        "（指标 [num:25.71:亿元:src:tl_79cf9d25c029] ）"
    )
    new, sources, _ = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    # 4 markers all resolved to same src → all `[1]`, 1 source entry
    assert new.count("[1]") == 4
    assert "[num:" not in new
    assert len(sources) == 1


def test_split_smushed_val_unit_variants() -> None:
    from bowhead_core.sources_resolver import _split_smushed_val_unit

    assert _split_smushed_val_unit("136.8%") == ("136.8", "%")
    assert _split_smushed_val_unit("+60.2%") == ("+60.2", "%")
    assert _split_smushed_val_unit("-19.0%") == ("-19.0", "%")
    assert _split_smushed_val_unit("85%") == ("85", "%")
    assert _split_smushed_val_unit("12.3万元") == ("12.3", "万元")
    assert _split_smushed_val_unit("500") == ("500", "")


def test_registry_bridges_aidless_payload_source_with_tool_result_same_query() -> None:
    """★116 dogfood 1.0.93 P3（t_c8959fde27ca）：peer payload 自报的来源
    （label 有、artifact_id 空）与工具真返的同一条 query 是同一个来源——
    原去重键（aid / label|tool|query）对不上 → 数据来源段成对重复，aidless
    条目渲成 "Source is currently unavailable"。(tool, query) 桥接后就地升级，
    编号不动。"""
    from types import SimpleNamespace

    from bowhead_core.sources_resolver import ensure_turn_sources_registry

    bus = SimpleNamespace()
    reg = ensure_turn_sources_registry(bus)
    # Peer payload reports first (without aid)
    reg.ingest_payload_sources([{"label": "近30天各省营业额排名", "tool": "bi_query", "artifact_id": None, "query": "近30天 各省 营业额 排名"}], None)
    assert len(reg.sources) == 1
    # Ingest the actual tool result afterward (same query, with aid + preview rows)
    n = reg.register_source(
        "art_d6e6d7f1841e",
        tool="bi_query",
        query="近30天 各省 营业额 排名",
        sample_rows=[{"省": "北京", "营业额 / 元": "289350304.8"}],
    )
    assert n == 1, "同 (tool,query) 必须桥接到既有条目，不另开新号"
    assert len(reg.sources) == 1
    e = reg.sources[0]
    assert e["artifact_id"] == "art_d6e6d7f1841e"  # Upgrade in place: [1] now resolves
    assert e["label"] == "近30天各省营业额排名"  # Preserve the human-readable label
    assert e.get("preview_rows"), "工具侧预览行要嫁接过来"


def test_registry_aidless_duplicate_of_existing_aid_entry_reuses_number() -> None:
    """反向到达序：工具真返先注册（带 aid），payload 自报后到（无 aid）——
    复用既有编号，不追加重复行。"""
    from types import SimpleNamespace

    from bowhead_core.sources_resolver import ensure_turn_sources_registry

    bus = SimpleNamespace()
    reg = ensure_turn_sources_registry(bus)
    reg.register_source("art_x1", tool="bi_query", query="近30天 营业额 各省 占比")
    reg.ingest_payload_sources([{"label": "近30天各省营业额占比", "tool": "bi_query", "artifact_id": None, "query": "近30天 营业额 各省 占比"}], None)
    assert len(reg.sources) == 1
    assert reg.sources[0]["artifact_id"] == "art_x1"


def test_registry_different_queries_stay_separate() -> None:
    """负对照：不同 query 的 aidless 来源不许被桥接吞并。"""
    from types import SimpleNamespace

    from bowhead_core.sources_resolver import ensure_turn_sources_registry

    bus = SimpleNamespace()
    reg = ensure_turn_sources_registry(bus)
    reg.ingest_payload_sources([{"label": "A", "tool": "bi_query", "artifact_id": None, "query": "各省 营业额"}], None)
    reg.register_source("art_y1", tool="bi_query", query="各品牌 退款率")
    assert len(reg.sources) == 2


def test_source_labels_never_show_a_tool_code() -> None:
    """来源引用是报告的**可信度面**，不许摆工具代号。

    116 dogfood 2026-09-09 实拍，报告底部「数据来源」：
        [1] · python_sandbox: df = pd.read_csv(INPUTS['rows']['path'])
        [2] · bi_query: 2026年1-12月 营业额 按 省
    `_infer_label_from_inv` 上方原有注释写着「标签也走展示名收敛」，但它只把
    `python_exec` 收敛成 `python_sandbox` —— **代号换代号**，没到人话这一步。

    突变：把 `_source_tool_label` 改成原样返回 tool → 本条红。
    """
    from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
    from bowhead_core.agents._lead_context import reset_current_output_language, set_current_output_language
    from bowhead_core.sources_resolver import _source_tool_label

    token = set_current_output_language("zh-CN")
    try:
        for ref in sorted(TOOL_DISPLAY):
            got = _source_tool_label(ref)
            assert got != ref, f"来源标签仍是工具代号：{ref}"
            assert got == TOOL_DISPLAY[ref]["display_zh"], ref
        set_current_output_language("en-US")
        assert _source_tool_label("bi_query") == TOOL_DISPLAY["bi_query"]["display_en"]
        # Return unrecognized names (team tools / historical names) unchanged; do not pretend to recognize them
        assert _source_tool_label("teammcp.ots_x.addPet") == "teammcp.ots_x.addPet"
        assert _source_tool_label("") == ""
    finally:
        reset_current_output_language(token)
