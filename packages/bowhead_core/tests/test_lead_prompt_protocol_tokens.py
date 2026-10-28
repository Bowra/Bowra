"""Lead 提示词改英文后的协议记号闸：对**启动后渲染出的** Lead 提示词断言。

Lead 的回答由下游按 ASCII 协议记号解析（response_kind 扫描器、引用登记、provenance 校验、
bail 分支、spawn 参数）。提示词里这些记号一旦被翻译或改写，解析会静默失效，
所以这里逐个钉住记号本身，并确认示例里没有会被模型当数据照抄的具体数值。
"""

from __future__ import annotations

import re

import pytest
from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
from bowhead_core.agents.prompts import lead as lead_prompt
from bowhead_core.agents.prompts.peer_block import SPAWN_LANGUAGE_RULE, default_prompt_peers
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.analysis_payload import _VALID_BAIL_KINDS
from bowhead_core.provenance import _VALID_KINDS


@pytest.fixture(scope="module")
def rendered() -> str:
    register_all_system_manifests(AgentRegistry.bootstrap())
    return lead_prompt.build_lead_system_prompt(
        "ws_protocol",
        facts=[],
        peers=list(default_prompt_peers()),
        planning_auto=True,
        skill_catalog_block_override="",
        memory_pack_block="- prefers concise answers",
    )


@pytest.mark.parametrize(
    "token",
    [
        "<response_kind>chat</response_kind>",
        "<response_kind>canvas</response_kind>",
        "<headline>",
        "</headline>",
        "[cite:N]",
        "[cite:1]",
        'delivery="chat"',
        'delivery="report"',
        "subagent_spawn(profile, task, rationale)",
        "clarification_ask",
        "plan_propose",
        "type: 'multi_select'",
        "config_propose",
        "form_id=user_language",
        "remember(kind, text)",
        "recall(query)",
        "feedback_escalate",
        "tool_list_workspaces",
        "tool_switch_context(name=",
        "remember=true",
        "<remembered_about_user>",
        "data_source.context_name",
        "`[数据]`",
        "`[data]`",
        "<lead_orchestrator>",
        "</lead_orchestrator>",
    ],
)
def test_protocol_token_survives_render(rendered: str, token: str) -> None:
    assert token in rendered


def test_every_provenance_kind_is_taught(rendered: str) -> None:
    for kind in _VALID_KINDS:
        assert f"[{kind}:" in rendered, kind


def test_every_bail_kind_is_taught(rendered: str) -> None:
    block = lead_prompt._PEER_BAIL_HANDLING
    assert block in rendered
    for kind in _VALID_BAIL_KINDS:
        assert re.search(rf"kind=(?:\w+ / )?{kind}\b", block), kind


def test_spawn_language_rule_lists_every_supported_output_language(rendered: str) -> None:
    assert SPAWN_LANGUAGE_RULE in rendered
    for lang in ("'en-US'", "'zh-CN'", "'de-DE'", "'fr-FR'"):
        assert lang in SPAWN_LANGUAGE_RULE


def test_output_language_rule_preserves_choices_and_defaults_to_english(rendered: str) -> None:
    # Explicit named/configured choices win; unconfigured paths use English.
    block = lead_prompt._OUTPUT_LANGUAGE
    assert block in rendered
    assert rendered.index(block) > rendered.index("</query_classification_decision_tree>")
    rules = [line for line in block.splitlines() if re.match(r"\d\. ", line)]
    assert len(rules) == 3, rules
    # Named language choices take precedence with or without a language note.
    assert rules[0] == (
        '1. A language the user\'s message explicitly names ("answer in English" / 「用中文回答」) wins over everything else, with or without a language note.'
    )
    assert rules[1].startswith("2. Otherwise, the configured language stated in the language note")
    assert "does not change it" in rules[1]
    assert rules[2].startswith("3. Otherwise (no named language and no language note) → English (en-US)")
    assert "[语言：…] / [Language: …] / [Sprache: …] / [Langue : …]" in block
    assert "<output_language>" in lead_prompt._TONE


def test_citation_marker_is_taught_verbatim_and_alone(rendered: str) -> None:
    # Citation registration accepts only `[cite:N]`; teaching another [xxx:N] form makes model output unresolvable superscripts that are dropped.
    assert "- the citation marker is `[cite:N]` (N is a 1-based consecutive integer: 1, 2, 3...)." in rendered
    # Another valid form is `[ref:N]` in kb-analyst reports (recognized by backend lead_orchestrator), appearing in roster descriptions.
    assert set(re.findall(r"\[([a-z-]+):N\]", rendered)) <= {"cite", "ref"}


def test_few_shot_names_no_real_people(rendered: str) -> None:
    # Models copy example names as data (original "张三"); allow placeholders only.
    few_shot = lead_prompt.build_few_shot_examples("compute")
    for text in (few_shot, rendered):
        assert not re.search(r"[张王李赵刘陈杨黄周吴徐孙马朱胡郭何林罗高][一-鿿]{1,2}的", text)
        assert not re.search(r"\b(?:John|Jane|Mary|Tom|David|Michael|Sarah|Alice|Bob)\s+[A-Z][a-z]+", text)
    assert "<person>" in few_shot


def test_examples_carry_no_concrete_figures(rendered: str) -> None:
    # Data-shaped examples get copied as answers when the model lacks data.
    assert not re.findall(r"\d[\d,\.]*\s*(?:万|亿|元|%|K\b|M\b|B\b)", rendered)
    assert not re.findall(r"20\d\d\s*年", rendered)
    few_shot = lead_prompt.build_few_shot_examples("compute")
    assert "<month>" in few_shot and "<person>" in few_shot
    assert not re.search(r"\d+\s*月", few_shot)
