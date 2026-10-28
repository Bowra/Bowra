from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.team_subagent_validation import (
    MAX_SYSTEM_PROMPT,
    TeamSubagentDefinitionCandidate,
    TeamSubagentValidationError,
    validate_candidate,
    validate_output_schema,
    validate_tool_refs,
)


def _catalog(*, selectable: bool = True, locked_reason: str = "risk_metadata_unknown") -> SubagentAuthoringCatalogSnapshot:
    tool = AuthoringTool(
        stable_ref="bi_query",
        display_name="bi_query",
        description="Query data",
        source_kind="native",
        source_display_name="Bowhead",
        capability_classes=frozenset({"data_query"}),
        risk="read",
        discoverable=True,
        selectable=selectable,
        locked_reason=None if selectable else locked_reason,
        risk_source="native_registry",
        approval_required=False,
        confirmation_summary=None,
        dependency_revision="v1",
    )
    return SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_a",
        native_tools=(tool,),
        team_tools=(),
        fingerprint="sha256:test",
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )


def _candidate(**overrides: object) -> TeamSubagentDefinitionCandidate:
    values: dict[str, object] = {
        "name": "sales-analyst",
        "display_name": "Sales analyst",
        "description": "Analyze sales trends",
        "system_prompt": "Analyze evidence and explain uncertainty.",
        "tool_refs": ("bi_query",),
        "max_iterations": 10,
        "skills": (),
        "model": "analyst",
        "output_schema": None,
        "audience_mode": "private",
    }
    values.update(overrides)
    return TeamSubagentDefinitionCandidate(**values)  # type: ignore[arg-type]


def test_candidate_accepts_private_ready_definition() -> None:
    validate_candidate(
        _candidate(),
        catalog=_catalog(),
        reserved_names={"lead"},
        operation="create",
    )


def test_create_rejects_non_private_audience() -> None:
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(
            _candidate(audience_mode="teamwide"),
            catalog=_catalog(),
            reserved_names=set(),
            operation="create",
        )
    assert exc.value.code == "subagent_create_must_be_private"


def test_candidate_rejects_locked_or_unknown_tool() -> None:
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(
            _candidate(),
            catalog=_catalog(selectable=False),
            reserved_names=set(),
            operation="create",
        )
    assert exc.value.code == "subagent_tool_not_selectable"

    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(
            _candidate(tool_refs=("teammcp.other.secret",)),
            catalog=_catalog(),
            reserved_names=set(),
            operation="create",
        )
    assert exc.value.code == "subagent_tool_no_longer_available"


@pytest.mark.parametrize("locked_reason", ["risk_metadata_unknown", "mcp_feature_unlicensed"])
def test_existing_binding_does_not_bypass_risk_or_license_lock(locked_reason: str) -> None:
    with pytest.raises(TeamSubagentValidationError, match="not selectable"):
        validate_tool_refs(
            ["bi_query"], catalog=_catalog(selectable=False, locked_reason=locked_reason), require_selectable=True, existing_tool_refs=["bi_query"]
        )


def test_output_schema_keeps_security_bounds() -> None:
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_output_schema(
            {
                "type": "object",
                "properties": {"name": {"type": "string", "pattern": "(a+)+$"}},
                "additionalProperties": False,
            },
        )
    assert exc.value.code == "subagent_output_schema_keyword_forbidden"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "invalid-type"},
        {"type": "object", "additionalProperties": False, "properties": {"exec_summary": {}}, "required": "exec_summary"},
        {"type": "string"},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"revenue": {"type": "string"}, "profit": {"type": "string"}, "profit_margin": {"type": "string"}},
            "required": ["revenue", "profit", "profit_margin"],
        },
        {"type": "object", "additionalProperties": False, "properties": {"exec_summary": False}},
        {"type": "object", "additionalProperties": False, "properties": {"exec_summary": {}}, "required": ["missing_field"]},
    ],
    ids=["invalid-type", "invalid-required", "scalar-root", "business-object-only", "forbidden-summary", "forbidden-required"],
)
def test_output_schema_rejects_invalid_or_clearly_incompatible_payload_contracts(schema) -> None:
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_output_schema(schema)
    assert exc.value.code == "subagent_output_schema_invalid"
    assert exc.value.field == "output_schema"


@pytest.mark.parametrize(
    "properties",
    [
        {"delivery_surface": {"const": "chat"}, "exec_summary": {"type": "string"}},
        {"error": {"type": "object"}},
        {"title": {"type": "string"}, "deliverables": {"type": "array"}},
        {"exec_summary": {"type": "string"}, "business_result": {"type": "object"}},
    ],
    ids=["chat", "clarification", "deliverable", "payload-extension"],
)
def test_output_schema_accepts_supported_payload_roots_and_free_text(properties) -> None:
    validate_output_schema(None)
    validate_output_schema({"type": "object", "additionalProperties": False, "properties": properties})


def test_prompt_length_gate_is_token_based_not_character_based() -> None:
    """闸的口径是 token，不是字符——同样长的中文和英文提示词必须判得一样。

    正对照（必须放行）：一份和我们最大的内置 agent（compute，8831 tok）同量级的中文
    提示词。它有 8000 个汉字、远超旧的 4000 字符线，是这次调整要解锁的形状。
    反对照（必须拒）：token 超线的中文提示词——即使它的**字符数**还在 MAX_SYSTEM_PROMPT
    以内。字符线放行、token 线拦住，正是"字符不是合适的单位"这件事本身。
    """
    ok_cn = "分析销售数据并解释不确定性。" * 600  # ~8400 characters ≈ within 12,000 tokens
    validate_candidate(
        _candidate(system_prompt=ok_cn),
        catalog=_catalog(),
        reserved_names=set(),
        operation="create",
    )

    over_cn = "分析销售数据并解释不确定性。" * 1400  # ~19600 characters, still < MAX_SYSTEM_PROMPT
    assert len(over_cn) <= MAX_SYSTEM_PROMPT, "反对照必须是字符线放行的那一档，否则测的是旧闸"
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(
            _candidate(system_prompt=over_cn),
            catalog=_catalog(),
            reserved_names=set(),
            operation="create",
        )
    assert exc.value.code == "subagent_system_prompt_too_many_tokens"


def test_prompt_character_gate_still_rejects_beyond_precheck_line() -> None:
    """字符线仍在（tokenize 前的廉价预检）：英文 token 很省，只有它拦得住超大文本。"""
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(
            _candidate(system_prompt="a" * (MAX_SYSTEM_PROMPT + 1)),
            catalog=_catalog(),
            reserved_names=set(),
            operation="create",
        )
    assert exc.value.code == "subagent_system_prompt_too_long"


def test_empty_system_prompt_is_rejected() -> None:
    """下限：没有指令的 agent 不许存在。

    这条来自 116 现场：6 个团队 agent 里 3 个 `system_prompt` 为空，其中一个已被 Lead
    派发跑完整轮（t_5d59b5d71fde）——它运行时的 system prompt 就是空串，没有角色、边界和
    输出契约。空之所以能进来，是因为此前**只查上限不查下限**，而字段全链路可选。
    """
    for blank in ("", "   ", "\n\t "):
        with pytest.raises(TeamSubagentValidationError) as exc:
            validate_candidate(
                _candidate(system_prompt=blank),
                catalog=_catalog(),
                reserved_names=set(),
                operation="create",
            )
        assert exc.value.code == "subagent_system_prompt_required"
        assert exc.value.field == "system_prompt"


def test_empty_draft_tools_still_require_intent_validation(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY_TEAM_IDS", "team_a")
    with pytest.raises(TeamSubagentValidationError) as exc:
        validate_candidate(_candidate(tool_refs=()), catalog=_catalog(), reserved_names=set(), operation="draft")
    assert exc.value.code == "subagent_tool_required"
