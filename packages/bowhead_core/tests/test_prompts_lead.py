"""Sprint θ Phase 8b-1: prompts/lead.py + active_facts.py tests.

Coverage:
- build_lead_system_prompt token budget (worst-case 10 facts)
- δ wire #1: active_facts injected at top
- R1 fences: BOWHEAD_DELTA_INJECT_ENABLED + BOWHEAD_DELTA_EXCLUDE_WORKSPACES
- R5 read-side cap: top 10 facts only
- I3 invariant: prompt content does NOT mention canvas write tools
- Anti-injection: <session_reminders> section present
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts import active_facts
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.registered_fact_store import RegisteredFact


def _mk_fact(*, fact_key: str, value_json: str) -> RegisteredFact:
    """Construct the REAL RegisteredFact build_block consumes — guards against the
    phantom-interface drift that hid the .summary/.id crash (dogfood s_3e854)."""
    return RegisteredFact(
        fact_id=f"factid_{fact_key}",
        workspace_id="ws_1",
        fact_key=fact_key,
        fact_type="preference",
        value_json=value_json,
        source="auto_learner",
        created_at="2026-04-22",
    )


def _mk_facts(n: int) -> list[RegisteredFact]:
    return [_mk_fact(fact_key=f"fact_{i}", value_json=f"事实 {i} 内容简介") for i in range(n)]


# =====  active_facts.build_block tests =====


def test_active_facts_empty_list_returns_empty() -> None:
    assert active_facts.build_block([]) == ""


def test_active_facts_single_fact_renders_xml_block() -> None:
    facts = _mk_facts(1)
    block = active_facts.build_block(facts)
    assert "<active_facts>" in block
    assert "<fact id=" in block
    assert "事实 0 内容简介" in block
    assert "</active_facts>" in block


def test_active_facts_top_k_cap_10_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5 read-side cap: only first 10 facts rendered, rest dropped."""
    facts = _mk_facts(15)
    block = active_facts.build_block(facts)
    # Count <fact tags
    assert block.count("<fact id=") == 10
    # First 10 in
    assert "fact_0" in block
    assert "fact_9" in block
    # 11th not rendered
    assert "fact_10" not in block


def test_active_facts_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 partial-rollback: BOWHEAD_DELTA_INJECT_ENABLED=0 → empty."""
    monkeypatch.setenv("BOWHEAD_DELTA_INJECT_ENABLED", "0")
    assert active_facts.build_block(_mk_facts(3)) == ""


def test_active_facts_workspace_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 partial-rollback: per-workspace exclude list."""
    monkeypatch.setenv("BOWHEAD_DELTA_EXCLUDE_WORKSPACES", "ws_bad,ws_test")
    assert active_facts.build_block(_mk_facts(3), workspace_id="ws_bad") == ""
    # Other workspace not affected
    assert active_facts.build_block(_mk_facts(3), workspace_id="ws_other") != ""


def test_active_facts_xml_escaping_in_summary() -> None:
    """Summary with XML special chars must be escaped (anti-injection in prompt)."""
    fact = _mk_fact(fact_key="x", value_json='<inject>"恶意"</inject>')
    block = active_facts.build_block([fact])
    assert "&lt;inject&gt;" in block
    # Raw < not present (would break XML structure)
    assert '"<inject>"' not in block


# =====  build_lead_system_prompt tests =====


def test_lead_prompt_says_ask_before_switching_datarc_workspace() -> None:
    """bi-answer-correctness B2（116 q5）：问题与当前北极九章工作区对不上 → 查名单、问用户换不换，不自己切换。"""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    block = prompt[prompt.index("<datarc_workspace_scope>") : prompt.index("</datarc_workspace_scope>")]
    assert "tool_list_workspaces" in block
    assert "clarification_ask" in block
    assert "tool_switch_context" in block
    assert "Do not switch on your own" in block


def test_lead_prompt_without_bi_omits_datarc_workspace_scope() -> None:
    assert "<datarc_workspace_scope>" not in build_lead_system_prompt("ws_test", facts=[], has_datarc=False)


def test_lead_system_prompt_has_all_11_sections_in_order() -> None:
    """All XML-tagged sections present in expected order. Plan A iter 2 renamed tool_use_guidance → query_classification_decision_tree."""
    prompt = build_lead_system_prompt("ws_test", facts=[])

    # ⚠ Context engineering sprint batch 2-b divided blocks into three volatility tiers
    # (static → per-team → per-user/per-turn), changing their order.
    # `<query_classification_decision_tree>` renders via `build_tool_use_guidance(planning_auto=...)`
    # and varies per turn (planning_auto is evaluated each turn), so it moved from the middle tier to the last.
    # Same guard intent: all these blocks exist in deterministic order, without random drift.
    expected_sections = [
        "<role>",
        "<provenance_block>",
        "<workspace_context_lazy>",
        "<refusal_and_safety>",
        "<tone>",
        "<error_handling>",
        "<session_reminders>",
        "<knowledge_boundary>",
        "<few_shot_examples>",
        "<query_classification_decision_tree>",
    ]
    last_idx = -1
    for tag in expected_sections:
        idx = prompt.find(tag)
        assert idx > last_idx, f"Section {tag} missing or out of order"
        last_idx = idx


def test_lead_system_prompt_includes_active_facts_at_top() -> None:
    """G3 wire #1: active_facts block injected after <role>, before <provenance>."""
    facts = _mk_facts(3)
    prompt = build_lead_system_prompt("ws_test", facts=facts)

    role_idx = prompt.find("<role>")
    facts_idx = prompt.find("<active_facts>")
    prov_idx = prompt.find("<provenance_block>")

    assert role_idx >= 0
    assert facts_idx >= 0
    assert prov_idx >= 0
    assert role_idx < facts_idx < prov_idx, "Order must be: <role> → <active_facts> → <provenance_block>"


def test_lead_system_prompt_no_canvas_write_tools_mentioned() -> None:
    """I3 invariant: prompt content must NOT instruct LLM to use canvas write tools.

    Lead manifest excludes them from tool whitelist; prompt should not promote them.
    """
    prompt = build_lead_system_prompt("ws_test", facts=[])

    # canvas.read OK; canvas.write/patch/delete/reorder/export NOT for Lead
    forbidden_tools = [
        "canvas.write_section",
        "canvas_patch_section",
        "canvas_delete_section",
        "canvas_reorder_sections",
        "canvas.export(",
    ]
    for t in forbidden_tools:
        assert t not in prompt, f"Lead prompt MUST NOT mention {t} (I3 violation)"


def test_lead_prompt_deck_routing_keyword_present() -> None:
    """pptx-from-template M3w.2 (RD-8) + subagent-unification (consultant capstone):
    the LIVE Lead prompt must route deck/PPT requests (uploaded .pptx → "出/做 a PPT
    in this format") to a spawn — else the Lead chat-deflects (the prod
    s_a9b693829c3d tool_count=0 root cause). As of the consultant capstone, deck
    requests route to **consultant** (its contract is the deck), while reports still
    route to compute. Content-level assertion only — LLM routing behavior is
    validated by dogfood, not a single-point prompt test (feedback_llm_emit_test_ill_posed)."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "PPT" in prompt, "Lead prompt lost its deck-routing keyword"
    assert "compute" in prompt, "Lead prompt lost the compute report-routing peer"
    assert "consultant" in prompt, "Lead prompt lost the consultant deck-routing peer"


def test_lead_system_prompt_session_reminders_anti_injection_present() -> None:
    """Anti-injection text in <session_reminders> per Opus 4.7 borrowing."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "<session_reminders>" in prompt
    assert "ignore previous instructions" in prompt
    assert "role hijack" in prompt


def test_lead_system_prompt_decision_tree_present() -> None:
    """Plan A iter 2: 3-step decision tree replaces nested guidance sections."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "<query_classification_decision_tree>" in prompt
    assert "Step 1" in prompt
    assert "Step 2" in prompt
    assert "Step 3" in prompt
    assert "metric" in prompt
    assert "clarification_ask" in prompt
    assert "candidates" in prompt


def test_lead_system_prompt_lazy_fetch_path_present() -> None:
    """Opus 4.7 capability_check borrowed via decision-tree Step 2 data path."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    # Decision tree Step 2 includes the live fetch sequence for data path.
    # bi_list_measures sunset (visible_to_llm=False); decision tree uses only bi_schema→bi_query.
    assert "bi_list_measures" not in prompt
    assert "bi_schema" in prompt
    assert "bi_query" in prompt


def test_lead_system_prompt_with_empty_facts_omits_active_facts_block() -> None:
    """When facts list empty, <active_facts> section is omitted (saves tokens)."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "<active_facts>" not in prompt
    # All other sections still there
    assert "<role>" in prompt
    assert "<knowledge_boundary>" in prompt


# ── ws-datarc-decouple P3: datarc-as-capability gate on the Lead prompt ──
def test_lead_prompt_default_has_datarc_omits_no_data_source_notice() -> None:
    """Behavior-preserving default (has_datarc=True): the no-BI notice is ABSENT and the BI
    tool guidance is present — exactly today's prompt."""
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "<no_data_source>" not in prompt
    assert "<query_classification_decision_tree>" in prompt  # BI guidance still there


def test_lead_prompt_no_datarc_injects_no_data_source_notice() -> None:
    """has_datarc=False (a P4 no-connection team): inject the no-BI notice so Lead won't promise
    or attempt BI and routes data questions to KB."""
    prompt = build_lead_system_prompt(
        "ws_test",
        facts=[],
        has_datarc=False,
    )
    assert "<no_data_source>" in prompt
    assert "no usable Datarc connection for this turn" in prompt
    assert "applies only to Datarc" in prompt
    assert "kb_search" in prompt  # routes to KB


def test_lead_prompt_dynamic_roster_is_complete_without_truncation() -> None:
    """actor-scoped 动态花名册必须保留能力路由和 prompt 尾部，不能按体积丢段。"""
    from bowhead_core.agents.prompts.peer_block import default_prompt_peers

    peers = list(default_prompt_peers())
    assert any(p.name == "marketing-analyst" for p in peers)  # Validate configuration-driven inclusion

    facts = _mk_facts(10)
    for f in facts:
        f.value_json = "A" * 200
    prompt = build_lead_system_prompt(
        "ws_test",
        facts=facts,
        peers=peers,
    )
    assert "marketing-analyst" in prompt
    assert "ordinary PDF / Excel (xlsx)" in prompt
    assert "</query_classification_decision_tree>" in prompt
    assert prompt.endswith("</lead_orchestrator>")


# ===== Memory rebuild (item 2): memory pack + protocol injection =====


def test_memory_pack_block_injected_when_present():
    pack = "📌 规则：head to head 默认加同环比+share gap"
    p = build_lead_system_prompt("ws", facts=[], memory_pack_block=pack)
    assert "<remembered_about_user>" in p
    assert "</remembered_about_user>" in p
    assert "head to head 默认加同环比+share gap" in p


def test_memory_pack_section_dropped_when_empty():
    p = build_lead_system_prompt("ws", facts=[], memory_pack_block="")
    # The real section (with closing tag) is gone; the bare token still appears in
    # the static <memory_protocol> prose, so assert on the CLOSING tag.
    assert "</remembered_about_user>" not in p


def test_memory_protocol_always_present():
    # The remember/recall contract is static — present even with no memory.
    p = build_lead_system_prompt("ws", facts=[], memory_pack_block="")
    assert "<memory_protocol>" in p
    assert "remember" in p and "recall" in p


def test_memory_protocol_anti_hallucination_rule():
    """The load-bearing rule: only confirm「已记住」after the tool returns ok."""
    p = build_lead_system_prompt("ws", facts=[])
    assert "Only after the tool returns ok" in p


def test_lead_prompt_with_populated_memory_pack_under_runtime_budget():
    """review P3: the live recall content (pack, capped 6000 char) is real prompt bulk
    the static budget test never measures. A near-max pack + worst-case facts must still
    render the section; documents the live magnitude."""
    pack = "📌 规则：" + ("记住一条很长的业务规则。" * 200)  # ~2400 CJK chars
    facts = _mk_facts(10)
    p = build_lead_system_prompt(
        "ws",
        facts=facts,
        memory_pack_block=pack,
    )
    assert "<remembered_about_user>" in p
    assert "记住一条很长的业务规则" in p


def test_lead_prompt_custom_skills_block_and_empty_pin() -> None:
    """skill-audit A3: entries → <user_team_skills> 在场；None/[] → 与不传逐字节相同."""
    from bowhead_core.agents.prompts.peer_block import CustomSkillEntry

    base = build_lead_system_prompt("ws_test", facts=[])
    assert build_lead_system_prompt("ws_test", facts=[], custom_skills=None) == base
    assert build_lead_system_prompt("ws_test", facts=[], custom_skills=[]) == base
    p = build_lead_system_prompt(
        "ws_test",
        facts=[],
        custom_skills=[CustomSkillEntry("my-skill", "自建 skill 描述")],
    )
    assert "<user_team_skills" in p
    assert "`my-skill`" in p


def test_memory_protocol_carves_out_language_preference():
    """多语言批0：语言偏好必须**离开** remember 的触发词族，改点名 config_propose。

    116 实测 t_fe809270f6c7：「以后你回答都用英文吧」→ Lead 调 `remember`（deduped=true，
    早记过而设置从没变）。根因是本块把 "以后都" 占为 remember 的 MUST 触发词，赢过了只写在
    工具 description 里的同名 MUST。这条钉的就是"赢的那一侧已经让出这一族"。
    """
    p = build_lead_system_prompt("ws", facts=[])
    assert "does not go into long-term memory" in p
    # Previously the system prompt had zero config_propose occurrences; this is the actual regression surface
    assert "config_propose" in p
    assert "user_language" in p


def test_language_preference_tool_named_in_prompt_actually_exists():
    """反幻影引用：提示词点名的工具必须真在 Lead 白名单里。

    仓里犯过一次同款（compute prompt 教模型先调从未注册的 search_dimension_value）。
    这条是 test_memory_protocol_carves_out_language_preference 的正对照：它保证那条规则
    指向的是一个**真调得动**的工具，而不是一句好看的空话。
    """
    from bowhead_core.agents import AgentRegistry

    reg = AgentRegistry.bootstrap()
    lead_manifest = reg.get("lead", workspace_id="ws_test_lang_batch0")
    assert "config_propose" in tuple(lead_manifest.tools.allow)


def test_memory_protocol_carves_out_team_escalation_only_when_the_flag_is_on(monkeypatch):
    """agent-feedback-loop 批 3c：同 config_propose 的教训——remember 的 MUST 会吃掉「把这条提给管理员」。

    flag 开：提示词点名 `feedback_escalate`（而且工具那时才被注册，白名单条目才不是幻影）；
    flag 关：一个字都不提——flag 关的部署里工具没注册，点名＝教模型调一个不存在的工具。
    """
    p = build_lead_system_prompt("ws", facts=[])
    assert "feedback_escalate" in p and "what would count as fixed?" in p
    assert p.index("feedback_escalate") < p.index("</memory_protocol>")
    from bowhead_core.agents import AgentRegistry

    lead_manifest = AgentRegistry.bootstrap().get("lead", workspace_id="ws_test_fbl_3c")
    assert "feedback_escalate" in tuple(lead_manifest.tools.allow)
