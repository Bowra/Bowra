"""Sprint multi-agent-v1 PR-#8 — 3 specialist agents 单测.

覆盖: financial / business / compliance 各自加载 + 配置正确.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.agents import AgentRegistry

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "bowhead_core" / "agents" / "prompts" / "system"


@pytest.fixture
def registry() -> AgentRegistry:
    return AgentRegistry.bootstrap()


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_loaded(registry: AgentRegistry, name: str) -> None:
    assert name in registry
    m = registry.get(name)
    assert m.name == name
    # delivery-surface M-C: specialists use report-writer trust to write domain report bodies themselves through the guarded tool.
    assert m.trust_class == "report-writer"
    assert m.model == "analyst"


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_compute_compatible_tools(registry: AgentRegistry, name: str) -> None:
    """3 specialist 共享 compute 白名单 (bi.* + skill.* + canvas.add_chart 等)."""
    m = registry.get(name)
    assert "bi_query" in m.tools.allow
    assert "bi_schema" in m.tools.allow
    # F5 (canvas-pipeline-fix): bi_attribute was removed from specialist allowlists; attribution uses
    # bi_fetch_rows + python_sandbox. Check only the remaining shared tools here.
    assert "bi_attribute" not in m.tools.allow
    assert "skill_read" in m.tools.allow
    assert "skill_invoke" in m.tools.allow
    assert "add_chart" in m.tools.allow
    assert "subagent_return" in m.tools.allow


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_no_canvas_section_tools(registry: AgentRegistry, name: str) -> None:
    """所有 specialist 都不可 add_section / compose_final (canvas-writer 唯一)."""
    m = registry.get(name)
    assert "canvas_add_section" not in m.tools.allow
    assert "canvas_compose_final" not in m.tools.allow


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_has_framework(registry: AgentRegistry, name: str) -> None:
    """skill 感知的权威载体 = prompt .md 文本（G3/G6 终态）.

    manifest `skills:` 块已弃用清空（防回潮断言）；framework 认知 + skill_list
    全库指引必须出现在各 specialist 的 prompt 文本里。
    """
    m = registry.get(name)
    assert m.skills.framework == []
    assert m.skills.sop_lazy == []
    text = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert "analyst-framework" in text
    assert "skill_list" in text


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_has_examples(registry: AgentRegistry, name: str) -> None:
    """每 specialist 至少 3 examples (router 用 few-shot)."""
    m = registry.get(name)
    assert len(m.examples) >= 3


@pytest.mark.parametrize(
    "name",
    ["financial-analyst", "business-analyst", "compliance-analyst"],
)
def test_specialist_has_output_schema(registry: AgentRegistry, name: str) -> None:
    """所有 specialist 都有 output_schema (M137 payload contract)."""
    m = registry.get(name)
    assert m.output_schema is not None
    assert m.output_schema.get("additionalProperties") is False
    assert "title" in m.output_schema.get("required", [])
    assert "insights" in m.output_schema.get("required", [])


def test_compliance_has_risk_flags_required(registry: AgentRegistry) -> None:
    """compliance-analyst 独有: risk_flags 是 required 字段."""
    m = registry.get("compliance-analyst")
    assert "risk_flags" in m.output_schema.get("required", [])


def test_compliance_has_risk_level_enum(registry: AgentRegistry) -> None:
    """compliance-analyst 含 risk_level enum."""
    m = registry.get("compliance-analyst")
    risk_level_schema = m.output_schema["properties"]["risk_level"]
    assert risk_level_schema["enum"] == ["low", "medium", "high", "critical"]


def test_financial_prompt_names_own_methods() -> None:
    """financial prompt 点名本域 4 个 method-skill（赋能 sprint 终态）."""
    text = (_PROMPTS_DIR / "financial-analyst.md").read_text(encoding="utf-8")
    for skill in (
        "analyst-method-earnings-quality",
        "analyst-method-roe-dupont",
        "analyst-method-solvency-cashflow",
        "analyst-method-valuation",
    ):
        assert skill in text


def test_business_prompt_names_own_methods() -> None:
    """business prompt 点名本域 4 个 method-skill."""
    text = (_PROMPTS_DIR / "business-analyst.md").read_text(encoding="utf-8")
    for skill in (
        "analyst-method-unit-economics",
        "analyst-method-cohort-retention",
        "analyst-method-funnel-growth",
        "analyst-method-customer-structure",
    ):
        assert skill in text


def test_all_specialists_in_registry_list(registry: AgentRegistry) -> None:
    all_names = registry.list_all()
    for name in ["financial-analyst", "business-analyst", "compliance-analyst"]:
        assert name in all_names


def test_specialist_count(registry: AgentRegistry) -> None:
    """10 system manifests（meta-agent 孤儿 manifest 已删——引用未注册工具且
    无人 spawn，docs/skills-tools-ssot.md §5-G14；canvas-writer 已在 c4d50b03b 移除）."""
    expected = {
        "compute",
        "financial-analyst",
        "business-analyst",
        "compliance-analyst",
        "lead",  # Sprint θ Phase 8b-4
        "kb-analyst",  # Sprint θ Phase 8b-4
        "consultant",  # subagent-unification capstone (comprehensive business consultant producing PPT decks).
        "marketing-analyst",  # multimodal-v1 rework (marketing: materials/campaign reviews/ad placement/research).
        "configuration-agent",  # bounded settings specialist (4aac905ba).
        "agent-coach",  # scoped Agent/Skill draft authoring (5801c95e8).
    }
    assert set(registry.list_all()) == expected
