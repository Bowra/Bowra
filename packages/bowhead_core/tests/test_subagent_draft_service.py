from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.subagent_authoring_models import (
    CapabilityIntent,
    DraftBlueprint,
    GeneratedSystemPrompt,
    PurposeAssessment,
)
from bowhead_core.subagent_draft_generator import (
    StructuredOutputMode,
    SubagentDraftGenerationError,
    SubagentDraftGenerationResult,
)
from bowhead_core.subagent_draft_service import (
    REQUIRED_CAPABILITY_UNMET,
    TEXT_ONLY_CONFIRMATION,
    DraftServiceError,
    DraftServiceOutcome,
    SubagentDraftService,
    carry_forward_unmet_requirements,
)
from bowhead_core.subagent_draft_store import (
    SubagentDraftStore,
    SubagentDraftVersionConflictError,
)
from bowhead_core.user_store import UserStore


class FakeCatalogService:
    async def build(self, **kwargs) -> SubagentAuthoringCatalogSnapshot:
        tools = (
            _tool("bi_schema", frozenset({"data_discovery"})),
            _tool("bi_query", frozenset({"data_query"})),
        )
        return SubagentAuthoringCatalogSnapshot(
            team_id=kwargs["team_id"],
            actor_user_id=kwargs["actor_user_id"],
            native_tools=tools,
            team_tools=(),
            fingerprint="sha256:catalog",
            dependency_guard=(),
            built_at=datetime.now(UTC),
        )


def _tool(ref: str, classes: frozenset[str]) -> AuthoringTool:
    return AuthoringTool(
        stable_ref=ref,
        display_name=ref,
        description=ref,
        source_kind="native",
        source_display_name="Bowhead",
        capability_classes=classes,
        risk="read",
        discoverable=True,
        selectable=True,
        locked_reason=None,
        risk_source="native_registry",
        approval_required=False,
        confirmation_summary=None,
        dependency_revision="v1",
    )


def _assessment(outcome: str) -> PurposeAssessment:
    return PurposeAssessment(
        outcome=outcome,
        evidence_source_or_object=True,
        evidence_action=True,
        evidence_expected_result=True,
        clarification_question=("Which sales source should be used?" if outcome == "ambiguous" else None),
        reason_code=f"purpose_{outcome}",
    )


def _blueprint() -> DraftBlueprint:
    assessment = _assessment("clear")
    return DraftBlueprint(
        purpose_assessment=assessment,
        goal_summary="Analyze weekly store sales anomalies",
        display_name="Sales anomaly analyst",
        slug_seed="sales-anomaly",
        description="Analyze weekly store sales anomalies",
        system_prompt=GeneratedSystemPrompt(
            role="You are a sales analyst.",
            goals=["Identify material anomalies."],
            boundaries=["Do not modify source systems."],
            evidence_rules=["Use available evidence."],
            failure_behavior=["Explain missing data."],
            output_expectations=["Return concise findings."],
        ),
        capability_intents=[
            CapabilityIntent(
                intent_id="ci_sales",
                capability_class="data_query",
                operation="read",
                object_terms=["sales"],
                source_preference="native",
                necessity="required",
                reason="Read weekly sales",
                confidence=0.95,
            ),
        ],
        max_iterations_hint=10,
        assumptions=[],
        warnings=[],
    )


class FakeGenerator:
    def __init__(self, outcome: str = "clear") -> None:
        self.outcome = outcome
        self.calls = 0

    async def generate(self, **kwargs) -> SubagentDraftGenerationResult:
        self.calls += 1
        assessment = _assessment(self.outcome)
        return SubagentDraftGenerationResult(
            assessment=assessment,
            blueprint=_blueprint() if self.outcome == "clear" else None,
            mode=StructuredOutputMode.JSON_OBJECT_WITH_VALIDATION,
            attempts=1,
            input_tokens=100,
            output_tokens=200,
        )


class FakeAccountant:
    def __init__(self) -> None:
        self.preflights = 0
        self.settlements = 0
        self.releases = 0
        self.last_settlement = None

    async def preflight(self, **kwargs) -> None:
        self.preflights += 1

    async def settle(self, **kwargs) -> None:
        self.settlements += 1
        self.last_settlement = kwargs

    async def release(self, **kwargs) -> None:
        self.releases += 1


async def _service(tmp_path, generator: FakeGenerator):
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    users = UserStore(str(db_path))
    await users.initialize()
    drafts = SubagentDraftStore(str(db_path), hmac_key=b"s" * 32)
    await drafts.initialize()
    accountant = FakeAccountant()
    service = SubagentDraftService(
        draft_store=drafts,
        definition_store=users,
        catalog_service=FakeCatalogService(),
        generator=generator,
        accountant=accountant,
    )
    return service, drafts, accountant


@pytest.mark.asyncio
async def test_canvas_generation_uses_existing_draft_and_replays_one_suggestion(tmp_path) -> None:
    generator = FakeGenerator()
    service, drafts, accountant = await _service(tmp_path, generator)
    draft = await drafts.create_manual_draft(
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        locale="zh-CN",
        purpose="分析每周门店销售异常并给出趋势",
        idempotency_key="manual-canvas-task",
    )
    result = await service.generate_canvas_suggestion(
        draft=draft,
        workspace_id="ws_a",
        expected_version=1,
        purpose=draft.purpose_text or "",
        locale="zh-CN",
        idempotency_key="canvas-generation-request",
        mcp_entitled=True,
    )
    assert result.suggestion is not None
    assert result.suggestion.draft_id == draft.id
    assert result.suggestion.payload["definition"]["name"]
    assert (await drafts.get_draft(draft.id, team_id="team_a", owner_user_id="user_a")).row_version == 1
    replay = await service.generate_canvas_suggestion(
        draft=draft,
        workspace_id="ws_a",
        expected_version=1,
        purpose=draft.purpose_text or "",
        locale="zh-CN",
        idempotency_key="canvas-generation-request",
        mcp_entitled=True,
    )
    assert replay.replayed and replay.suggestion == result.suggestion
    assert generator.calls == 1
    assert accountant.settlements == 1


@pytest.mark.asyncio
async def test_absent_purpose_does_not_claim_or_call_model(tmp_path) -> None:
    generator = FakeGenerator()
    service, drafts, accountant = await _service(tmp_path, generator)
    result = await service.create_intent_draft(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="我想创建一个 Agent",
        locale="zh-CN",
        idempotency_key="purpose-required-1",
        actor_active=True,
        writable=True,
        authoring_enabled=True,
        mcp_entitled=True,
    )
    assert result.outcome is DraftServiceOutcome.PURPOSE_REQUIRED
    assert result.draft is None
    assert generator.calls == 0
    assert accountant.preflights == 0
    with sqlite3.connect(drafts.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM team_subagent_draft_request",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_failed_generation_settles_usage_already_consumed(tmp_path) -> None:
    class FailingGenerator(FakeGenerator):
        async def generate(self, **kwargs) -> SubagentDraftGenerationResult:
            self.calls += 1
            raise SubagentDraftGenerationError(
                "subagent_draft_schema_invalid",
                attempts=2,
                input_tokens=20,
                output_tokens=40,
            )

    generator = FailingGenerator()
    service, _drafts, accountant = await _service(tmp_path, generator)

    with pytest.raises(DraftServiceError) as raised:
        await service.create_intent_draft(
            team_id="team_a",
            owner_user_id="user_a",
            purpose="每周分析门店销售异常并给出趋势",
            locale="zh-CN",
            idempotency_key="failed-generation-usage-1",
            actor_active=True,
            writable=True,
            authoring_enabled=True,
            mcp_entitled=True,
        )

    assert raised.value.code == "subagent_draft_schema_invalid"
    assert accountant.settlements == 1
    assert accountant.releases == 0
    assert accountant.last_settlement is not None
    assert accountant.last_settlement["input_tokens"] == 20
    assert accountant.last_settlement["output_tokens"] == 40


@pytest.mark.asyncio
async def test_materialization_rejects_high_entropy_secret_in_model_output(
    tmp_path,
) -> None:
    generator = FakeGenerator()
    service, _drafts, _accountant = await _service(tmp_path, generator)
    blueprint = _blueprint().model_copy(
        update={
            "assumptions": [
                "Use token sk_live_Q7w9pL2nV8xR4cM6bK1zT5sH3dF0aJ9u",
            ],
        },
    )

    with pytest.raises(DraftServiceError) as raised:
        await service.materialize_blueprint(
            team_id="team_a",
            owner_user_id="user_a",
            purpose="每周分析门店销售异常并给出趋势",
            blueprint=blueprint,
            mcp_entitled=True,
        )

    assert raised.value.code == "subagent_draft_secret_pattern_rejected"


@pytest.mark.asyncio
async def test_materialization_allows_security_boundary_without_secret_value(
    tmp_path,
) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _blueprint()
    blueprint.system_prompt.boundaries.append("Never ask users for passwords, API keys, access tokens, or MCP headers.")
    blueprint.system_prompt.boundaries.append("Authorization: none is required for public pages.")

    review = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="每周分析门店销售异常并给出趋势",
        blueprint=blueprint,
        mcp_entitled=True,
    )

    assert "Never ask users for passwords" in review.definition["system_prompt"]


@pytest.mark.asyncio
async def test_materialization_allows_long_descriptive_slug(tmp_path) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _blueprint()
    blueprint.slug_seed = "public-product-announcement-checker-2026"

    review = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="核对公开产品公告",
        blueprint=blueprint,
        mcp_entitled=True,
    )

    assert review.definition["name"].startswith("public-product-announcement")


@pytest.mark.asyncio
async def test_known_url_draft_selects_fetch_without_search(tmp_path) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _blueprint()
    blueprint.capability_intents[0] = CapabilityIntent(
        intent_id="ci_url",
        capability_class="public_web_fetch",
        operation="read",
        object_terms=["user URL"],
        source_preference="native",
        necessity="required",
        reason="Read the public page supplied by the user",
        confidence=0.95,
    )
    catalog = SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_a",
        native_tools=(
            _tool("web_fetch", frozenset({"public_web_fetch"})),
            _tool("web_search", frozenset({"public_web_research"})),
        ),
        team_tools=(),
        fingerprint="sha256:web-catalog",
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )

    review = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="只读取用户提供的公告 URL 并核对内容",
        blueprint=blueprint,
        mcp_entitled=True,
        catalog=catalog,
    )

    assert review.definition["tool_refs"] == ["web_fetch"]


def _team_data_catalog(*, bi_query_locked: bool) -> SubagentAuthoringCatalogSnapshot:
    bi_query = _tool("bi_query", frozenset({"data_query"}))
    if bi_query_locked:
        bi_query = replace(bi_query, selectable=False, locked_reason="mcp_feature_unlicensed")
    return SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_a",
        native_tools=(_tool("bi_schema", frozenset({"data_discovery"})), bi_query),
        team_tools=(
            replace(
                _tool("teammcp.docs.search", frozenset({"team_domain_read"})),
                source_kind="team_mcp",
                source_display_name="Docs",
            ),
        ),
        fingerprint="sha256:team-data",
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )


def _team_data_blueprint() -> DraftBlueprint:
    blueprint = _blueprint()
    blueprint.capability_intents[0] = blueprint.capability_intents[0].model_copy(
        update={"source_preference": "team", "reason": "读取上周各门店营业额"},
    )
    return blueprint


@pytest.mark.asyncio
async def test_team_labelled_data_need_materializes_with_native_query(tmp_path) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())

    review = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="每周读取团队各门店营业额并找出环比下降的门店",
        blueprint=_team_data_blueprint(),
        mcp_entitled=True,
        catalog=_team_data_catalog(bi_query_locked=False),
    )

    assert review.definition["tool_refs"] == ["bi_query", "bi_schema"]
    assert review.validation_status == "valid"
    assert review.draft_status == "ready"
    assert review.validation_issues == []


@pytest.mark.asyncio
async def test_unmet_required_need_blocks_draft_with_actionable_issue(tmp_path) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _team_data_blueprint()
    blueprint.capability_intents.append(
        CapabilityIntent(
            intent_id="ci_2",
            capability_class="artifact_chart",
            operation="artifact_write",
            object_terms=["trend"],
            source_preference="native",
            necessity="required",
            reason="Draw the trend",
            confidence=0.9,
        ),
    )
    catalog = _team_data_catalog(bi_query_locked=True)
    catalog = replace(
        catalog, native_tools=(*catalog.native_tools, replace(_tool("add_chart", frozenset({"artifact_chart"})), risk="artifact_write"))
    )

    review = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="每周读取团队各门店营业额并画趋势图",
        blueprint=blueprint,
        mcp_entitled=True,
        catalog=catalog,
    )

    assert review.definition["tool_refs"] == ["add_chart"]
    assert review.validation_status == "invalid"
    assert review.draft_status == "blocked"
    [issue] = review.validation_issues
    assert issue["code"] == REQUIRED_CAPABILITY_UNMET
    assert issue["details"]["intent_id"] == "ci_sales"
    assert issue["details"]["cause"] == "tool_locked"
    assert issue["details"]["candidate_tool_refs"] == ["bi_query"]
    assert "query data" in issue["message"]
    assert "读取上周各门店营业额" in issue["message"]
    assert "bi_query" in issue["message"]
    assert "team admin" in issue["message"]


def test_unmet_requirement_stays_open_until_a_covering_tool_is_selected() -> None:
    catalog = _team_data_catalog(bi_query_locked=False)
    issue = {
        "code": REQUIRED_CAPABILITY_UNMET,
        "details": {"capability_class": "data_query", "candidate_tool_refs": ["bi_query"]},
    }
    other = {"code": "subagent_name_conflict", "details": {}}
    by_ref = catalog.tools_by_ref

    assert carry_forward_unmet_requirements([issue, other], [by_ref["bi_schema"]]) == [issue]
    assert carry_forward_unmet_requirements([issue, other], [by_ref["bi_schema"], by_ref["bi_query"]]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [
        "password=letmein",
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
    ],
)
async def test_materialization_rejects_explicit_secret_value(tmp_path, boundary: str) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _blueprint()
    blueprint.system_prompt.boundaries.append(boundary)

    with pytest.raises(DraftServiceError) as raised:
        await service.materialize_blueprint(
            team_id="team_a",
            owner_user_id="user_a",
            purpose="每周分析门店销售异常并给出趋势",
            blueprint=blueprint,
            mcp_entitled=True,
        )

    assert raised.value.code == "subagent_draft_secret_pattern_rejected"


@pytest.mark.asyncio
async def test_clear_purpose_creates_private_ready_draft_once(tmp_path) -> None:
    generator = FakeGenerator("clear")
    service, _drafts, accountant = await _service(tmp_path, generator)
    kwargs = {
        "team_id": "team_a",
        "owner_user_id": "user_a",
        "purpose": "每周分析门店销售异常并给出趋势",
        "locale": "zh-CN",
        "idempotency_key": "clear-purpose-1",
        "actor_active": True,
        "writable": True,
        "authoring_enabled": True,
        "mcp_entitled": True,
    }
    result = await service.create_intent_draft(**kwargs)
    assert result.outcome is DraftServiceOutcome.DRAFT_READY
    assert result.draft is not None
    assert result.draft.status == "ready"
    assert result.draft.definition is not None
    assert result.draft.definition["visibility"] == "creator_only"
    assert result.draft.definition["tool_refs"] == [
        "bi_query",
        "bi_schema",
    ]

    replay = await service.create_intent_draft(**kwargs)
    assert replay.outcome is DraftServiceOutcome.REPLAY
    assert replay.draft is not None
    assert replay.draft.id == result.draft.id
    assert generator.calls == 1
    assert accountant.preflights == 1
    assert accountant.settlements == 1


@pytest.mark.asyncio
async def test_ambiguous_purpose_creates_one_clarification_draft(tmp_path) -> None:
    generator = FakeGenerator("ambiguous")
    service, _drafts, _accountant = await _service(tmp_path, generator)
    result = await service.create_intent_draft(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="分析销售并给出结果",
        locale="zh-CN",
        idempotency_key="ambiguous-purpose-1",
        actor_active=True,
        writable=True,
        authoring_enabled=True,
        mcp_entitled=True,
    )
    assert result.outcome is DraftServiceOutcome.NEEDS_CLARIFICATION
    assert result.draft is not None
    assert result.draft.status == "needs_clarification"
    assert result.draft.clarification is not None


@pytest.mark.asyncio
async def test_clarification_is_claimed_once_and_materializes_draft(
    tmp_path,
) -> None:
    generator = FakeGenerator("ambiguous")
    service, _drafts, accountant = await _service(tmp_path, generator)
    initial = await service.create_intent_draft(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="分析销售并给出结果",
        locale="zh-CN",
        idempotency_key="clarification-purpose-1",
        actor_active=True,
        writable=True,
        authoring_enabled=True,
        mcp_entitled=True,
    )
    assert initial.draft is not None
    initial_version = initial.draft.row_version

    generator.outcome = "clear"
    clarified = await service.clarify_draft(
        draft_id=initial.draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        expected_version=initial_version,
        answer="使用团队 BI 销售数据，返回每周异常趋势图。",
        mcp_entitled=True,
    )

    assert clarified.outcome is DraftServiceOutcome.DRAFT_READY
    assert clarified.draft is not None
    assert clarified.draft.status == "ready"
    assert clarified.draft.business_clarification_count == 1
    assert clarified.draft.clarification is not None
    assert clarified.draft.clarification["answered"] is True
    assert clarified.draft.definition is not None
    assert generator.calls == 2
    assert accountant.preflights == 2
    assert accountant.settlements == 2

    with pytest.raises(SubagentDraftVersionConflictError):
        await service.clarify_draft(
            draft_id=initial.draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            expected_version=initial_version,
            answer="再试一次",
            mcp_entitled=True,
        )
    assert generator.calls == 2


@pytest.mark.asyncio
async def test_second_ambiguous_assessment_blocks_without_reasking(
    tmp_path,
) -> None:
    generator = FakeGenerator("ambiguous")
    service, _drafts, _accountant = await _service(tmp_path, generator)
    initial = await service.create_intent_draft(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="分析销售并给出结果",
        locale="zh-CN",
        idempotency_key="clarification-purpose-2",
        actor_active=True,
        writable=True,
        authoring_enabled=True,
        mcp_entitled=True,
    )
    assert initial.draft is not None

    clarified = await service.clarify_draft(
        draft_id=initial.draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        expected_version=initial.draft.row_version,
        answer="按适合的方式分析。",
        mcp_entitled=True,
    )

    assert clarified.outcome is DraftServiceOutcome.DRAFT_BLOCKED
    assert clarified.draft is not None
    assert clarified.draft.status == "blocked"
    assert clarified.draft.business_clarification_count == 1
    assert clarified.draft.definition is None
    assert clarified.draft.validation_issues[0]["code"] == ("subagent_clarification_insufficient")


@pytest.mark.asyncio
async def test_failed_clarification_returns_latest_blocked_draft(tmp_path) -> None:
    class SecretAfterClarification(FakeGenerator):
        async def generate(self, **kwargs) -> SubagentDraftGenerationResult:
            result = await super().generate(**kwargs)
            if kwargs.get("clarification_answer") and result.blueprint is not None:
                blueprint = result.blueprint.model_copy(deep=True)
                blueprint.system_prompt.boundaries.append("password=letmein")
                return replace(result, blueprint=blueprint)
            return result

    generator = SecretAfterClarification("ambiguous")
    service, _drafts, _accountant = await _service(tmp_path, generator)
    initial = await service.create_intent_draft(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="核对公开产品公告并列出缺失项",
        locale="zh-CN",
        idempotency_key="clarification-failure-1",
        actor_active=True,
        writable=True,
        authoring_enabled=True,
        mcp_entitled=True,
    )
    assert initial.draft is not None
    generator.outcome = "clear"

    result = await service.clarify_draft(
        draft_id=initial.draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        expected_version=initial.draft.row_version,
        answer="只检查用户给出的公开网址",
        mcp_entitled=True,
    )

    assert result.outcome is DraftServiceOutcome.DRAFT_BLOCKED
    assert result.draft is not None
    assert result.draft.status == "blocked"
    assert result.draft.row_version == initial.draft.row_version + 2
    assert result.draft.clarification["answer"] == "只检查用户给出的公开网址"
    assert result.draft.validation_issues[0]["code"] == "subagent_draft_secret_pattern_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,has_intents,enabled,expected",
    [
        ("provided_text", False, True, "warning"),
        ("provided_text", False, False, "invalid"),
        ("tools", False, True, "invalid"),
        ("tools", True, True, "invalid"),
    ],
)
async def test_text_only_draft_needs_explicit_mode_and_rollout(tmp_path, monkeypatch, mode, has_intents, enabled, expected):
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "1" if enabled else "0")
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY_TEAM_IDS", "team_a")
    service, _, _ = await _service(tmp_path, FakeGenerator())
    payload = _blueprint().model_dump()
    payload["resource_mode"] = mode
    payload["capability_intents"] = payload["capability_intents"] if has_intents else []
    # A model-supplied warning must never substitute for the explicit mode.
    payload["warnings"] = [{"code": TEXT_ONLY_CONFIRMATION, "message": "Model claims this is approved."}]
    blueprint = DraftBlueprint.model_validate(payload)
    catalog = SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_a",
        native_tools=(),
        team_tools=(),
        fingerprint="sha256:empty",
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )
    result = await service.materialize_blueprint(
        team_id="team_a",
        owner_user_id="user_a",
        purpose="Rewrite the supplied text",
        blueprint=blueprint,
        mcp_entitled=True,
        catalog=catalog,
        locale="en-US",
    )
    assert result.validation_status == expected
    assert result.definition["tool_refs"] == []
    assert result.definition["skills"] == []
    if expected == "warning":
        assert result.draft_status == "ready"
        assert result.warnings[0]["code"] == TEXT_ONLY_CONFIRMATION
        assert result.warnings[0]["message"].startswith("I confirm")
    else:
        assert result.draft_status == "blocked"
        assert not any(w["code"] == TEXT_ONLY_CONFIRMATION for w in result.warnings)
        assert result.validation_issues[0]["code"] == "subagent_tool_required"


@pytest.mark.asyncio
async def test_chat_canvas_task_retains_origin_and_one_generation_after_edits(tmp_path, monkeypatch) -> None:
    generator = FakeGenerator()
    service, drafts, accountant = await _service(tmp_path, generator)
    creation = {
        "team_id": "team_a",
        "owner_user_id": "user_a",
        "workspace_id": "ws_a",
        "source_turn_id": "t_origin",
        "purpose": "Analyze weekly sales anomalies",
        "locale": "en-US",
        "idempotency_key": "chat-task",
    }
    draft = await service.create_chat_canvas_draft(**creation)
    assert draft.entry_mode == "canvas" and draft.source_mode == "turn"
    assert draft.workspace_id == "ws_a" and draft.source_turn_id == "t_origin"
    assert draft.row_version == 1 and generator.calls == 0
    generation = {
        "workspace_id": "ws_a",
        "expected_version": 1,
        "purpose": creation["purpose"],
        "locale": "en-US",
        "idempotency_key": "chat-task-suggestion",
        "mcp_entitled": False,
    }
    result = await service.generate_canvas_suggestion(draft=draft, **generation)
    saved = await drafts.save_manual_draft(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        expected_version=1,
        purpose=creation["purpose"],
        definition={"name": "my-edited-agent"},
        text_only_confirmed=False,
    )
    replayed_draft = await service.create_chat_canvas_draft(**creation)
    assert replayed_draft.id == draft.id and replayed_draft.definition == saved.definition
    replay = await service.generate_canvas_suggestion(draft=replayed_draft, **generation)
    assert replay.replayed and replay.suggestion == result.suggestion
    assert generator.calls == 1 and accountant.settlements == 1
    assert await drafts.get_draft(draft.id, team_id="team_a", owner_user_id="other_user") is None
    with pytest.raises(DraftServiceError, match="subagent_draft_not_found"):
        await service.generate_canvas_suggestion(draft=replayed_draft, **{**generation, "workspace_id": "ws_other"})


@pytest.mark.asyncio
async def test_materialization_writes_prompt_headings_in_the_review_language(tmp_path) -> None:
    """Novice walkthrough: a zh-CN draft showed English "Role:/Goals:" headings."""
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    review = await service.materialize_blueprint(
        team_id="team_a", owner_user_id="user_a", purpose="每周分析门店销售异常", blueprint=_blueprint(), mcp_entitled=True, locale="zh-CN"
    )
    prompt = review.definition["system_prompt"]
    assert prompt.startswith("角色：") and "\n目标：\n" in prompt and "\n证据要求：\n" in prompt
    assert "Role:" not in prompt and "Goals:" not in prompt


@pytest.mark.asyncio
async def test_materialization_recommends_the_email_plugin_for_an_email_need(tmp_path) -> None:
    service, _drafts, _accountant = await _service(tmp_path, FakeGenerator())
    blueprint = _blueprint()
    blueprint.capability_intents.append(
        CapabilityIntent(
            intent_id="ci_mail",
            capability_class="email_send",
            operation="external_write",
            object_terms=["weekly report"],
            source_preference="native",
            necessity="required",
            reason="Email the weekly report",
            confidence=0.9,
        )
    )
    review = await service.materialize_blueprint(
        team_id="team_a", owner_user_id="user_a", purpose="每周汇总销售并发邮件给团队", blueprint=blueprint, mcp_entitled=True
    )
    assert review.definition["builtin_plugin_ids"] == ["builtin:email"]
    # The Email Plugin meets the need, so it is not reported as a missing Tool.
    assert not any("ci_mail" in str(issue) for issue in review.validation_issues)
