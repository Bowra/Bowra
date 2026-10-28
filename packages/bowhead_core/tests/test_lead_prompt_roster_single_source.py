"""WP M3-12 判据：Lead 提示词里不许有第二份花名册。

权威设计 `docs/sprints/agent-prompt-configurability-2026-09-04/tech-design.md` §5。

★ **判据打在渲染后的整份提示词上，不扫源码字符串。** 扫描会稳定地绿着掩护一次零送达的
改动 —— 这正是本 WP 要修的病的成因：`_render_disabled_builtins_notice` 的
`n in preamble` 判据只看一段，于是 `compute` 从那一段被参数化掉之后，覆盖其余段落的
兜底静默失效，而没有任何测试发现。

批 1 做了 `data` 岗位，**批 2 把其余岗位补齐**：判据 1 现在对全部可停用内置成立
（逐个停用后名字出现 0 次）。三个领域专家不设岗位，那条规则改写成不点名的结构性表述。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.capability_roles import ROLE_DATA, manifest_primary_capability
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import PeerEntry, build_prompt_roster
from bowhead_core.agents.registry import AgentRegistry

# Roster order = lead.yaml callable_peers order (caller guarantees system > team > user).
_BUILTINS = (
    "kb-analyst",
    "compute",
    "financial-analyst",
    "business-analyst",
    "compliance-analyst",
    "consultant",
    "marketing-analyst",
)

# None may appear in disabled notices: that would call the team's configuration choice a system failure.
_OUTAGE_WORDS = ("不可用", "未恢复", "稍后重试", "服务故障", "unavailable", "not restored", "try again later")


def _all_indexes(haystack: str, needle: str) -> list[int]:
    out, start = [], 0
    while (i := haystack.find(needle, start)) != -1:
        out.append(i)
        start = i + 1
    return out


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.bootstrap()


def _entry(reg: AgentRegistry, name: str) -> PeerEntry:
    """按**生产形态**造 PeerEntry —— `_gather_lead_peers` 就是这么填的。"""
    m = reg.get(name)
    return PeerEntry(
        name=name,
        description=m.description,
        source="system",
        tool_names=frozenset(m.tools.allow),
        primary_capability=manifest_primary_capability(m),
        output_formats=tuple(m.produces or ()),
    )


@pytest.mark.unit
def test_responsibility_cannot_qualify_a_peer_without_its_execution_contract(registry: AgentRegistry) -> None:
    from dataclasses import replace

    knowledge_peer = _entry(registry, "kb-analyst")
    falsely_claimed = replace(knowledge_peer, primary_capability="cap_visualize")
    roster = build_prompt_roster([falsely_claimed, _entry(registry, "marketing-analyst")])
    assert roster.holders["image_gen"] == "marketing-analyst"
    deck_claim = replace(knowledge_peer, primary_capability="cap_presentation")
    assert build_prompt_roster([deck_claim]).holders["deck"] is None


def _prompt(reg: AgentRegistry, *, drop: tuple[str, ...] = (), extra: tuple[PeerEntry, ...] = ()) -> str:
    peers = [_entry(reg, n) for n in _BUILTINS if n not in drop] + list(extra)
    return build_lead_system_prompt(
        "ws_x",
        facts=[],
        peers=peers,
        disabled_builtins=frozenset(drop),
        # Skill catalog block is process-cached and independent of roster (destination #13, batch 3); this checks
        # routing prose only, so empty it to prevent named entries contaminating counts.
        skill_catalog_block_override="",
    )


_TEAM_DATA_AGENT = PeerEntry(
    "regional-data-analyst",
    "团队自建数据分析",
    "team",
    tool_names=frozenset({"bi_query", "python_exec"}),
)


# ── Criterion 1: Agent names absent from roster occur zero times in the entire prompt ──


def test_criterion_1_disabled_data_peer_is_never_named(registry: AgentRegistry) -> None:
    """★ 本 WP 的核心判据（data 岗位）。

    2026-09-04 实测基线：停用 `compute` 后整份提示词仍有 **14 处**点名它，其中 3 处是
    决策树里的 `MUST spawn`、4 处是 `subagent_spawn(profile="compute")` 的 few-shot ——
    而该名此时已被 `_widen_spawn_enum` 从 enum 里剪掉，提示词在教模型 emit 一个
    schema 不接受的值。
    """
    for extra in ((_TEAM_DATA_AGENT,), ()):
        prompt = _prompt(registry, drop=("compute",), extra=extra)
        assert "compute" not in prompt, f"停用的 compute 仍被点名（extra={[p.name for p in extra]}）"


def test_criterion_1_team_agent_takes_over_the_data_role(registry: AgentRegistry) -> None:
    """Route the data role to a capable custom Agent in the current roster."""
    prompt = _prompt(registry, drop=("compute",), extra=(_TEAM_DATA_AGENT,))
    assert "MUST spawn regional-data-analyst" in prompt
    assert 'subagent_spawn(profile="regional-data-analyst"' in prompt


def test_criterion_1_power_bi_agent_can_hold_the_data_role(registry: AgentRegistry) -> None:
    """★ 旧判据 `BI_FETCH_TOOL_NAMES` 不含 `teammcp.ibt_powerbi.pbi_execute_dax`：配了 Power BI 的团队
    自建取数 Agent 会被静默判成"不能取数"。能力目录里 `cap_data_query` 认这个工具。"""
    pbi = PeerEntry("pbi-agent", "Power BI 分析", "team", tool_names=frozenset({"teammcp.ibt_powerbi.pbi_execute_dax"}))
    prompt = _prompt(registry, drop=("compute",), extra=(pbi,))
    assert "MUST spawn pbi-agent" in prompt
    assert "compute" not in prompt


# ── Criterion 2: no assignee → disclose truthfully without naming anyone ──


def test_criterion_2_absent_data_role_names_nobody(registry: AgentRegistry) -> None:
    prompt = _prompt(registry, drop=("compute",))
    for name in _BUILTINS:
        if name == "compute":
            assert name not in prompt
    assert "has not enabled data querying yet" in prompt or "no Agent that can query data" in prompt


def test_criterion_2_absent_data_role_is_config_not_outage(registry: AgentRegistry) -> None:
    """Missing team configuration must not be described as a transient outage."""
    prompt = _prompt(registry, drop=("compute",))
    # The prohibition itself must remain; this is the model's sole constraint.
    assert "not a system fault" in prompt or "configuration state" in prompt
    # Failure wording may appear only in prohibitions; check each word's preceding text for "禁"/"绝不能".
    for word in _OUTAGE_WORDS:
        for idx in _all_indexes(prompt, word):
            context = prompt[max(0, idx - 120) : idx]
            assert any(marker in context for marker in ("禁", "绝不能", "不要", "Never", "never")), (
                f"{word!r} 出现在非禁令语境里: ...{context}【{word}】..."
            )


def test_criterion_2_no_template_placeholder_leaks(registry: AgentRegistry) -> None:
    """模板化引入的新失败面：`{data}` 忘了 format 会原样进提示词。"""
    for drop in ((), ("compute",)):
        assert "{data}" not in _prompt(registry, drop=drop)


# ── Criterion 3: full roster remains byte-for-byte unchanged (rollback safeguard) ──


def test_criterion_3_full_roster_renders_the_historical_names(registry: AgentRegistry) -> None:
    """全员在场 ⇒ 每个岗位解析回历史承担者 ⇒ 渲染出的字节与改动前相同。

    2026-09-04 与 origin/main 对拍实测：整份提示词只差**两处刻意改动**
    （consultant description 去掉「走 compute, 不是我」、peer bail 去掉幻影 `modeler`），
    其余逐字节一致。这里钉住解析结果本身。
    """
    roster = build_prompt_roster([_entry(registry, n) for n in _BUILTINS])
    assert roster.holders == {
        "data": "compute",
        "compute": "compute",
        "deck": "consultant",
        "knowledge": "kb-analyst",
        "image_gen": "marketing-analyst",
    }
    prompt = _prompt(registry)
    assert "MUST spawn compute" in prompt
    assert 'subagent_spawn(profile="compute", task="query <metric> for <month>")' in prompt


def test_criterion_3_phantom_modeler_is_gone(registry: AgentRegistry) -> None:
    """`modeler` 从来不在 boot 后的花名册里（实测 PROFILES 只有 8 内置 + lead），
    而 `_PEER_BAIL_HANDLING` 一直点着它。同 `search_dimension_value` 幻影族。"""
    assert "modeler" not in _prompt(registry)


# ── Criterion 4: mutation validation (do these criteria detect failures?) ──


def test_criterion_4_mutation_hardcoding_the_name_back_turns_red(registry: AgentRegistry) -> None:
    """把决策树的取数行改回字面名 ⇒ 判据 1 必须红。

    没有这条，上面那些 `assert "compute" not in prompt` 可能只是因为渲染路径根本没跑到。
    """
    from bowhead_core.agents.prompts import lead as lead_mod

    original = lead_mod._STEP2_DATA_LINE_TMPL
    try:
        lead_mod._STEP2_DATA_LINE_TMPL = original.replace("{data}", "compute")
        mutated = _prompt(registry, drop=("compute",), extra=(_TEAM_DATA_AGENT,))
        assert "compute" in mutated, "突变没生效——判据 1 是空转的"
    finally:
        lead_mod._STEP2_DATA_LINE_TMPL = original
    assert "compute" not in _prompt(registry, drop=("compute",), extra=(_TEAM_DATA_AGENT,))


# ── Criterion 5: planner has an independent view, easily missed ──


def test_criterion_5_planner_block_follows_the_roster() -> None:
    """`render_planner_team_tools_block` 进的是 **planner** 的提示词，不是 Lead 的。
    两个取景框共用同一条 sanitize/预算管线，但取数点名要各自跟着花名册走。"""
    from bowhead_core.agents.prompts.peer_block import TeamToolEntry, render_planner_team_tools_block

    entries = [TeamToolEntry(name="teammcp.src.op", description="查仓库信息", source_label="GitHub")]
    assert "该步派 regional-data-analyst" in render_planner_team_tools_block(entries, "regional-data-analyst")
    assert "compute" not in render_planner_team_tools_block(entries, "regional-data-analyst")
    # No assignee ⇒ generic wording referring to no peer
    assert "compute" not in render_planner_team_tools_block(entries, None)


# ── Complete builtin declarations (fail immediately on missing config, no silent degradation) ──


def test_every_dispatchable_builtin_declares_its_contract(registry: AgentRegistry) -> None:
    """★ 少写一条 `primary_capability`，那个内置就既不认领岗位、又不可被征用。

    没有这条闸，新加一个忘了声明的内置会**静默**让某个岗位判成"无人承担"，
    而提示词会如实说「本团队没有能做这件事的 Agent」—— 用户看到的是能力凭空消失。
    `lead` 不在此列：它是编排器，不是可派的 peer。
    """
    missing = [n for n in registry.list_all() if n != "lead" and not manifest_primary_capability(registry.get(n))]
    assert not missing, f"这些内置没声明 primary_capability: {missing}"


def test_a_declared_builtin_is_never_drafted_for_another_role(registry: AgentRegistry) -> None:
    """生产形态下 `kb-analyst` / `consultant` / 三个 specialist 的白名单里**都有
    `bi_query`**。它们各自声明了别的契约，就不该被抓去当取数兜底 —— 否则停用
    `compute` 的团队会把 BI 请求派给一个做文档研究的 agent。"""
    roster = build_prompt_roster([_entry(registry, n) for n in _BUILTINS if n != "compute"])
    assert roster.holder(ROLE_DATA) is None


# ── Full criterion 1: check each disableable builtin ──


@pytest.mark.parametrize("agent", _BUILTINS)
def test_criterion_1_holds_for_every_disableable_builtin(registry: AgentRegistry, agent: str) -> None:
    """★ 批 2 的收口判据：**任何一个内置被停用后，它的名字在整份提示词里出现 0 次。**

    批 1 时这条只对 `compute` 成立，其余七个各有 2-8 处残留（靠一句后置撤销声明兜底，
    而那句话是弱指令、且对 `compute` 根本不触发）。批 2 把 deck / knowledge
    三岗也岗位化、领域专家那条改成不点名，残留全部归零。

    ⚠ 计数打在**渲染后的整份提示词**上，且刻意包含 peer 描述行 —— 事故里有一半残留就藏在
    manifest description 里，只看路由散文会漏掉它们。
    """
    prompt = _prompt(registry, drop=(agent,))
    assert prompt.count(agent) == 0, f"停用的 {agent} 仍被点名"


def test_criterion_1_holds_when_several_builtins_are_disabled(registry: AgentRegistry) -> None:
    """多个同时停用 —— 岗位之间会互相引用（deck 那句提到取数承担者），单个停用测不到。"""
    drop = ("consultant", "kb-analyst")
    prompt = _prompt(registry, drop=drop)
    for agent in drop:
        assert prompt.count(agent) == 0, f"{agent} 仍被点名"


def test_specialists_are_referred_to_structurally_not_by_name(registry: AgentRegistry) -> None:
    """三个领域专家**不设岗位**：工具面几乎相同，按能力解析不出来也不该解析。
    规则改写成结构性表述后，判据（可独立成章）与并行纪律（同一次响应全部 emit）必须都还在。"""
    prompt = _prompt(registry)
    assert "one domain specialist per domain from the roster below" in prompt
    assert "in the SAME response" in prompt
    # Names belong in the following roster, not the rule prose
    body = prompt.split("**Peer relationship rules**", 1)[1].split("Available callable_peers", 1)[0]
    for name in ("financial-analyst", "business-analyst", "compliance-analyst"):
        assert name not in body, f"规则散文里仍点名 {name}"


def test_direct_builder_derives_manifest_roster_and_honors_disable(registry: AgentRegistry) -> None:
    """非 turn 的直接 builder 从 manifest 派生花名册；不会复活已停用 Agent。

    Live 调用方必须显式传 actor-scoped peers（失败时传空列表）；`None` 只服务 lint、
    预览等无 actor 的直接调用，因此可以安全使用 system manifests，但仍要应用停用集合。
    """
    prompt = build_lead_system_prompt(
        "ws_x",
        facts=[],
        peers=None,
        disabled_builtins=frozenset({"consultant"}),
        skill_catalog_block_override="",
    )
    assert "consultant" not in prompt
    assert "team configuration state" in prompt
