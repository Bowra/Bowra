"""A 轴能力标签 + `route_step_agent` 去名字化（agent-capability-routing 第 1 期）。

判据分两类，缺一不可：

- **不许回归**：今天这套 system manifest 下，路由结果必须与去名字化之前**逐个相同**
  （needs_web→compute、needs_corpus→kb-analyst）。这不是"顺便测测"——
  「domain 专家不许去读上传文档」是 WP-2/WP-7 硬仗打下来的防线，拆过头同样会"通过"。
- **必须改进**：团队自建 agent 只要 manifest 里挂了对应工具就选得上，不必改代码。
"""

from __future__ import annotations

import json
import pathlib

import pytest
from bowhead_core.agents.capabilities import (
    QUERIES_BI,
    READS_UPLOADS,
    SEARCHES_WEB,
    capabilities_by_agent,
    derive_capabilities,
    render_capabilities,
    required_capabilities,
    select_capable_agent,
)
from bowhead_core.plan_authoring import route_step_agent

# Actual current system-manifest allowlist (copied verbatim from `manifests/system/*.yaml`,
# checked 2026-08-13: after delivery-surface M-C pen grants, 6 report agents each gained write_report/
# read_report). ★ consultant still lacks a pen (deck specialist uses render), no longer identical to financial-analyst;
# "who can produce PPT" remains phase 2 `produces:` work.
# 2026-09-05 (tool-inventory): removed `bi_fetch_rows` from this list and its registration;
# `_fetch_rows` is now internal to `bi_query`; removed from allowlists for 5 profiles.
_BI_ONLY = [
    "bi_query",
    "bi_schema",
    "bi_concept_resolve",
    "skill_invoke",
    "skill_read",
    "skill_list",
    "add_chart",
    "subagent_return",
]
# delivery-surface M-C: guarded pen (M-B bake+owner+CAS).
_PEN = ["write_report", "read_report"]
PROD_ALLOW = {
    "kb-analyst": [*_BI_ONLY, *_PEN, "kb_search", "kb_list_tables", "kb_query_records", "kb_get_record"],
    "consultant": [*_BI_ONLY, "render_deck"],
    "financial-analyst": [*_BI_ONLY, *_PEN],
    "business-analyst": [*_BI_ONLY, *_PEN],
    "compliance-analyst": [*_BI_ONLY, *_PEN],
    # ⚠ Manually copied. Batch 5ⓑ adds flat `python_exec` to compute (top-level code/inputs/timeout); follow here.
    # (`_PEN` already includes write_report/read_report; do not duplicate.)
    # compute-agent-optimize batch 1 originally removed bi_fetch_rows here; 2026-09-05 removed it from
    # all of `_BI_ONLY`, so no separate filtering remains.
    "compute": [
        *_BI_ONLY,
        *_PEN,
        "web_search",
        "web_fetch",
        "web_extract",
        "web_map",
        "web_crawl",
        "add_image",
        "python_exec",
        # Editing existing skills through conversation requires both read + save (previously no role had both).
        "skill_save_generated",
        # canvas-update-turn batch B2-2: follow-up revisions hide replaced prior-turn charts.
        "remove_chart",
    ],
    "marketing-analyst": [
        "bi_concept_resolve",
        "bi_query",
        "bi_schema",
        "add_chart",
        "add_image",
        "canvas_create_design",
        "canvas_flatten_design",
        "canvas_update_design_text",
        "image_generate",
        "read_report",
        "skill_invoke",
        "skill_list",
        "skill_read",
        "subagent_return",
        "web_search",
        "web_extract",
        "web_map",
        "web_crawl",
        "web_deep_research",
        "write_report",
    ],
}
PROD_CAPS = {name: derive_capabilities(allow) for name, allow in PROD_ALLOW.items()}


def test_prod_allow_fixture_matches_real_manifests() -> None:
    """★ 上面那份 `PROD_ALLOW` 是**手抄**的 —— 抄错或 manifest 改了而这里没跟，
    整个文件的"不许回归"判据就全是虚构的（测的是我脑子里的 manifest，不是仓里的）。
    这条闸对着真 yaml 校验，让漂移变成红灯而不是无声的假绿。
    """
    import yaml

    root = pathlib.Path(__file__).resolve().parents[1] / "src/bowhead_core/agents/manifests/system"
    for name, expected in PROD_ALLOW.items():
        data = yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))
        assert sorted(data["tools"]["allow"]) == sorted(expected), f"{name} 的 allowlist 与本文件的 fixture 不一致"


def test_consultant_and_financial_analyst_are_indistinguishable_by_tools() -> None:
    """★ 本 sprint 最容易被忽略的一条，钉成测试免得后人再去"从 tools 推导交付物能力"。

    delivery-surface M-C 后 allowlist 不再一字不差（financial 多了笔），但
    **capability 推导**仍分不出「谁能出 PPT」——原命题（靠 tools 推导交付物
    能力不成立）依旧成立，断言收窄到 caps 层。
    """
    assert PROD_CAPS["consultant"] == PROD_CAPS["financial-analyst"]


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("kb-analyst", {READS_UPLOADS, QUERIES_BI}),
        ("consultant", {QUERIES_BI}),
        ("compute", {SEARCHES_WEB, QUERIES_BI}),
        ("marketing-analyst", {SEARCHES_WEB, QUERIES_BI}),
    ],
)
def test_capabilities_derived_from_real_manifests(agent: str, expected: set[str]) -> None:
    assert PROD_CAPS[agent] == frozenset(expected)


def test_kb_prefix_covers_the_whole_family() -> None:
    """判据是 `kb_` 前缀而不是逐个列名 —— 将来加 kb_* 工具不必回来改这里。"""
    assert derive_capabilities(["kb_whatever_new_tool"]) == frozenset({READS_UPLOADS})
    assert derive_capabilities(["bi_schema"]) == frozenset()  # bi_schema does not imply BI querying.


def test_power_bi_query_tools_share_the_data_query_capability() -> None:
    """Planner and Lead must classify Power BI-only Agents the same way."""

    assert derive_capabilities(["teammcp.ibt_powerbi.pbi_execute_dax"]) == frozenset({QUERIES_BI})


# ── No regression: current manifest routing must match each hardcoded-era result ──


@pytest.mark.parametrize(
    ("preferred", "needs_web", "needs_corpus", "expected"),
    [
        # ① needs_web → compute (old implementation hardcoded `return "compute"`).
        ("kb-analyst", True, False, "compute"),
        ("financial-analyst", True, False, "compute"),
        ("compute", True, False, "compute"),
        # ★ With both needs_web and needs_corpus true, web wins (WP-7: industry news is outside uploaded docs).
        ("kb-analyst", True, True, "compute"),
        # ② needs_corpus → kb-analyst (old implementation hardcoded `return KB_AGENT`).
        ("financial-analyst", False, True, "kb-analyst"),
        ("consultant", False, True, "kb-analyst"),
        ("kb-analyst", False, True, "kb-analyst"),
        # ③ Neither needed → respect planner choice unchanged.
        ("consultant", False, False, "consultant"),
        ("financial-analyst", False, False, "financial-analyst"),
    ],
)
def test_routing_is_byte_identical_to_hardcoded_era(preferred: str, needs_web: bool, needs_corpus: bool, expected: str) -> None:
    assert route_step_agent(preferred, needs_web=needs_web, needs_corpus=needs_corpus, agent_capabilities=PROD_CAPS) == expected


def test_no_agent_name_literal_left_in_router() -> None:
    """反向守卫：函数体里不许再出现 agent 名字。

    ★ 这条是"改完之后别被悄悄改回去"的闸 —— 本期的价值全在于**代码不认名字**，
    有人图省事加一句 `if ...: return "compute"` 就白改了，而行为测试抓不到（结果一样）。
    """
    import inspect

    src = inspect.getsource(route_step_agent)
    body = src.split('"""')[2]  # Implementation only; docstring illustrates the old implementation.
    for name in ("compute", "kb-analyst", "consultant", "financial-analyst", "KB_AGENT"):
        assert name not in body, f"route_step_agent 实现里又出现了 agent 名字: {name}"


# ── Required improvement: custom agents selectable through manifest, without code changes ──


def test_team_custom_agent_with_web_search_is_selected() -> None:
    """旧实现无条件 `return "compute"`：团队自建的 market-researcher 挂着 web_search 也永远轮不到。"""
    caps = {**PROD_CAPS, "market-researcher": derive_capabilities(["web_search", "bi_query"])}
    assert route_step_agent("market-researcher", needs_web=True, needs_corpus=False, agent_capabilities=caps) == "market-researcher"


def test_team_custom_agent_with_kb_tools_is_selected() -> None:
    caps = {**PROD_CAPS, "dd-reader": derive_capabilities(["kb_search"])}
    assert route_step_agent("dd-reader", needs_web=False, needs_corpus=True, agent_capabilities=caps) == "dd-reader"


def test_incapable_pick_is_replaced_not_kept() -> None:
    """planner 选了个够不着数据的 → 换成具备能力的（这正是触发本 sprint 的形状）。"""
    assert route_step_agent("consultant", needs_web=False, needs_corpus=True, agent_capabilities=PROD_CAPS) == "kb-analyst"


def test_no_capable_agent_returns_none_not_a_guess() -> None:
    """★ 一个都没有时必须返回 None —— 塞一个够不到数据的上去空跑，正是「答应了不会做的事」。"""
    caps = {"consultant": PROD_CAPS["consultant"]}  # No kb_* tools.
    assert route_step_agent("consultant", needs_web=False, needs_corpus=True, agent_capabilities=caps) is None


def test_missing_capability_map_does_not_force_anything() -> None:
    """取不到能力表时不做强制路由 —— 不能退回按名字硬路由（那正是本期拆掉的东西）。"""
    assert route_step_agent("consultant", needs_web=False, needs_corpus=True, agent_capabilities=None) == "consultant"


def test_tie_between_capable_agents_is_deterministic() -> None:
    """多个候选时按名字排序取第一个：任意但**可复现**，不许出现随机/字典序不稳。"""
    caps = {n: PROD_CAPS[n] for n in ("compute", "marketing-analyst")}
    picks = {route_step_agent("kb-analyst", needs_web=True, needs_corpus=False, agent_capabilities=caps) for _ in range(20)}
    assert picks == {"compute"}


# ── Helpers ────────────────────────────────────────────────────


def test_required_capabilities_precedence() -> None:
    assert required_capabilities(needs_web=True, needs_corpus=True) == frozenset({SEARCHES_WEB})
    assert required_capabilities(needs_web=False, needs_corpus=True) == frozenset({READS_UPLOADS})
    assert required_capabilities(needs_web=False, needs_corpus=False) == frozenset()


def test_select_capable_agent_keeps_preferred_when_already_capable() -> None:
    assert select_capable_agent("compute", frozenset({SEARCHES_WEB}), PROD_CAPS) == "compute"


def test_render_capabilities_is_stable_and_empty_stays_empty() -> None:
    assert render_capabilities(PROD_CAPS["kb-analyst"]) == "读上传文档/查本空间BI"
    assert render_capabilities(PROD_CAPS["compute"]) == "联网检索/查本空间BI"
    assert render_capabilities(frozenset()) == ""  # Empty → caller omits whole section; do not write "能力: (无)".


def test_capabilities_by_agent_isolates_a_broken_manifest() -> None:
    """一个 manifest 坏掉只丢它自己 —— 全表塌空会把"注册表抖了一下"错报成"这活没人能干"。"""

    class _Reg:
        def get(self, name: str, workspace_id: str | None = None) -> object:
            if name == "broken":
                raise RuntimeError("manifest unreadable")
            return type("M", (), {"tools": type("T", (), {"allow": ["kb_search"]})()})()

    out = capabilities_by_agent(_Reg(), "ws1", ["good", "broken"])
    assert out == {"good": frozenset({READS_UPLOADS})}


# ── Axis B: declared deliverables (phase 2a) ────────────────────


def test_produces_vocabulary_is_the_export_registry_not_a_new_one() -> None:
    """★ `produces` 的词表**必须**就是导出格式注册表，不许自成一套。

    上个 sprint 因为「同一件事在多处各自枚举」连吃两次亏（invalid Office output /
    unsupported export copy producer），已经用 `test_export_format_registry_drift.py`
    把 6 个枚举点钉在一起。`produces` 是第 7 个成员 —— 自己发明词表就是当场破自己立的规矩。
    """
    from bowhead_core.agents.manifest import AgentManifest
    from bowhead_core.canvas_export import _FMT_SKILL

    base = {
        "name": "test-agent",
        "model": "analyst",
        "trust_class": "read-only",
        "max_iterations": 5,
        "max_output_tokens": 1000,
        "description": "d",
        "tools": {"allow": ["bi_query"]},
    }
    # Accept every registered format.
    for fmt in _FMT_SKILL:
        AgentManifest(**base, produces=[fmt])
    # Reject all unregistered formats (including canvas_report mentioned casually in discovery §3.3).
    for bad in ("canvas_report", "ppt", "keynote", ""):
        with pytest.raises(Exception, match="unknown produces|String should"):
            AgentManifest(**base, produces=[bad])


def test_produces_defaults_empty_and_rejects_duplicates() -> None:
    from bowhead_core.agents.manifest import AgentManifest

    base = {
        "name": "test-agent",
        "model": "analyst",
        "trust_class": "read-only",
        "max_iterations": 5,
        "max_output_tokens": 1000,
        "description": "d",
        "tools": {"allow": ["bi_query"]},
    }
    assert AgentManifest(**base).produces == []  # Old manifests lack field → empty, current behavior unchanged.
    with pytest.raises(Exception, match="duplicate produces"):
        AgentManifest(**base, produces=["pptx", "pptx"])


def test_deck_inputs_derived_from_produces_is_byte_identical_to_the_name_set() -> None:
    """★ 第 ④ 处名字硬编码退休，但**行为必须一个字节都不变**。

    `deck_inputs` 不是展示位：它开着的时候，收尾会去把 render_deck.py 产出的
    output.pptx 登记成 artifact（`subagent.py` 那段唯一从 skill 收产物的路径）。
    多给一个 agent = 去翻它根本没有的产物；少给一个 = **用户拿不到文件**
    （23 次「要文件」只出成 4 次那个 sprint 的老伤）。
    所以这条钉的是集合相等，而不是"consultant 有就行"。
    """
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()
    with_deck = {n for n in registry.list_all() if manifest_to_profile(registry.get(n)).deck_inputs}
    assert with_deck == {"compute", "consultant"}, "deck 钩子的归属变了——这是行为回归，不是重构"


def test_produces_by_agent_matches_manifests_and_isolates_breakage() -> None:
    from bowhead_core.agents.capabilities import produces_by_agent
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()
    prod = produces_by_agent(registry, None, registry.list_all())
    assert {n for n, v in prod.items() if v} == {"compute", "consultant"}
    assert prod["consultant"] == ("pptx",)

    class _Reg:
        def get(self, name: str, workspace_id: str | None = None) -> object:
            if name == "broken":
                raise RuntimeError("boom")
            return type("M", (), {"produces": ["pptx"]})()

    assert produces_by_agent(_Reg(), "ws", ["ok", "broken"]) == {"ok": ("pptx",)}


def test_render_produces_empty_means_no_downloadable_file_not_broken_agent() -> None:
    from bowhead_core.agents.capabilities import render_produces

    assert render_produces(["pptx"]) == "PPT"
    assert render_produces(["pptx", "docx"]) == "PPT/Word"
    assert render_produces([]) == ""  # Caller omits whole section; do not render "可交付: (无)".


def test_roster_renders_both_axes_separately() -> None:
    """两轴各渲各的段：A 轴回答够不够得着数据，B 轴回答产不产得出文件。"""
    from bowhead_core.plan_authoring import _render_roster

    line = _render_roster(
        [("consultant", "顾问")],
        {"consultant": frozenset({QUERIES_BI})},
        agent_produces={"consultant": ("pptx",)},
    )
    entry = json.loads(line.splitlines()[1])
    assert entry == {
        "name": "consultant",
        "description": "顾问",
        "capabilities": "查本空间BI",
        "produces": "PPT",
    }
    # Agents without deliverable declarations must have no "可交付" section.
    no_produces = json.loads(
        _render_roster([("x", "d")], {"x": frozenset({QUERIES_BI})}, agent_produces={"x": ()}).splitlines()[1],
    )
    assert "produces" not in no_produces


# ── R0: who cannot execute plan steps ───────────────────────────


def test_non_plannable_covers_orchestrator_and_writer() -> None:
    """调度员与组稿工都不该被派活。

    `lead` 是 2026-08-10 加的：它此前只是漏了，而能力标签把它**放大**成花名册里
    "能力最全"的那个，于是真机 plan_39f5147554fd 把「用一句话总结」派给了它。

    The internal helpers stay out of plans too: `configuration-agent` (4aac905ba, bounded
    settings specialist) and `agent-coach` (5801c95e8, scoped draft authoring) are reached
    only through their own Lead/Canvas delegation paths, never as plan workers.
    """
    from bowhead_core.plan_authoring import NON_PLANNABLE_AGENTS

    assert frozenset({"canvas-writer", "lead", "configuration-agent", "agent-coach"}) == NON_PLANNABLE_AGENTS


def test_non_plannable_agents_are_dropped_at_the_parse_chokepoint() -> None:
    """★ 名单里不给**不等于**模型不会点名 —— LLM 路由不可靠，解析咽喉必须再拦一次。

    只摘名单不丢步，等于把"选错人"从提案期推迟到执行期（那时用户已经批过了）。
    """
    from bowhead_core.plan_authoring import _parse_authored

    raw = """{"steps": [
      {"id": "bi", "agent_name": "business-analyst", "objective": "查今年营业额前三品牌"},
      {"id": "sum", "agent_name": "lead", "objective": "基于上一步结果, 用一句话总结"},
      {"id": "wr", "agent_name": "canvas-writer", "objective": "汇总成报告"}
    ]}"""
    out = _parse_authored(raw)
    assert [s.agent_name for s in out] == ["business-analyst"], "调度员/组稿工步必须在解析层就被丢掉"


def test_planner_roster_excludes_non_plannable() -> None:
    """名单侧同源：两个 roster 构造点都读同一个常量。"""
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.plan_authoring import NON_PLANNABLE_AGENTS

    registry = AgentRegistry.bootstrap()
    roster_names = [n for n in registry.list_all() if n not in NON_PLANNABLE_AGENTS]
    assert "lead" not in roster_names
    assert "canvas-writer" not in roster_names
    # Positive control: actual workers remain (otherwise an empty roster could make this assertion pass).
    assert {"kb-analyst", "compute", "consultant"} <= set(roster_names)


def test_builtin_agents_win_the_tiebreak_over_team_agents() -> None:
    """★ P2（独立 review）：R3 之后自建 agent 与内置并进同一张能力表，
    纯字母序兜底可被**静默劫持** —— 建一个名字排序靠前、挂着 web_search 的
    `team_wide` agent，就能接管全团队计划里所有需要兜底的联网步。
    兜底先在内置里找，内置无人具备时才轮到自建。
    """
    from bowhead_core.agents.capabilities import SEARCHES_WEB, select_capable_agent

    caps = {"aaa-hijacker": frozenset({SEARCHES_WEB}), "compute": frozenset({SEARCHES_WEB})}
    builtin = ("compute", "kb-analyst", "consultant")
    assert select_capable_agent("kb-analyst", frozenset({SEARCHES_WEB}), caps) == "aaa-hijacker"  # Hijacked without prefer.
    assert select_capable_agent("kb-analyst", frozenset({SEARCHES_WEB}), caps, prefer=builtin) == "compute"


def test_team_agent_still_wins_when_no_builtin_has_the_capability() -> None:
    """内置无人具备时，自建 agent 仍然该被兜底选中 —— prefer 只是**排序**不是**过滤**。"""
    from bowhead_core.agents.capabilities import READS_UPLOADS, select_capable_agent

    caps = {"dd-reader": frozenset({READS_UPLOADS}), "compute": frozenset()}
    assert select_capable_agent("compute", frozenset({READS_UPLOADS}), caps, prefer=("compute",)) == "dd-reader"
