from __future__ import annotations

import json

import pytest
from bowhead_core.subagent_draft_generator import (
    StructuredOutputMode,
    SubagentDraftGenerationError,
    SubagentDraftGenerator,
)
from bowhead_router.types import LLMResponse, TokenUsage


def _valid_payload() -> dict[str, object]:
    assessment = {
        "outcome": "clear",
        "evidence_source_or_object": True,
        "evidence_action": True,
        "evidence_expected_result": True,
        "clarification_question": None,
        "reason_code": "purpose_clear",
    }
    return {
        "purpose_assessment": assessment,
        "blueprint": {
            "purpose_assessment": assessment,
            "goal_summary": "Analyze weekly store sales anomalies",
            "display_name": "Sales anomaly analyst",
            "slug_seed": "sales-anomaly",
            "description": "Analyze weekly store sales anomalies",
            "system_prompt": {
                "role": "You are a sales analysis specialist.",
                "goals": ["Identify material weekly anomalies."],
                "boundaries": ["Do not modify source systems."],
                "evidence_rules": ["Cite the data used."],
                "failure_behavior": ["Explain missing data."],
                "output_expectations": ["Return a concise finding."],
            },
            "capability_intents": [
                {
                    "intent_id": "ci_sales",
                    "capability_class": "data_query",
                    "operation": "read",
                    "object_terms": ["sales"],
                    "source_preference": "native",
                    "necessity": "required",
                    "reason": "Read weekly sales",
                    "confidence": 0.95,
                },
            ],
            "max_iterations_hint": 10,
            "assumptions": [],
            "warnings": [],
        },
    }


class FakeRouter:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = payloads
        self.requests = []
        self.resolve_profiles: list[str | None] = []
        self.invoke_profiles: list[str | None] = []

    def resolve(self, agent_type: str, profile: str | None = None):
        assert agent_type == "analyst"
        self.resolve_profiles.append(profile)
        return object()

    async def invoke(
        self,
        agent_type: str,
        request,
        profile: str | None = None,
    ):
        assert agent_type == "analyst"
        self.invoke_profiles.append(profile)
        self.requests.append(request)
        return LLMResponse(
            content=self.payloads.pop(0),
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


@pytest.mark.asyncio
async def test_generator_validates_json_object_without_catalog_or_tool_refs() -> None:
    router = FakeRouter([json.dumps(_valid_payload())])
    result = await SubagentDraftGenerator(router).generate(
        purpose="每周分析门店销售异常并画图",
        locale="zh-CN",
        trace_id="trace_1",
    )
    assert result.mode == StructuredOutputMode.JSON_OBJECT_WITH_VALIDATION
    assert result.blueprint is not None
    assert result.blueprint.capability_intents[0].capability_class == "data_query"
    assert result.attempts == 1
    assert result.input_tokens == 10
    rendered = router.requests[0].messages[0].content
    assert "teammcp." not in rendered
    assert "tool_ref" not in rendered
    assert router.resolve_profiles == ["structured"]
    assert router.invoke_profiles == ["structured"]
    assert len(result.model_usage) == 1
    assert result.model_usage[0].model is None
    assert result.model_usage[0].usage.input == 10
    assert result.model_usage[0].usage.output == 20


@pytest.mark.asyncio
async def test_generator_allows_one_bounded_repair() -> None:
    router = FakeRouter(
        [
            '{"purpose_assessment":{"outcome":"clear"}}',
            json.dumps(_valid_payload()),
        ],
    )
    result = await SubagentDraftGenerator(router).generate(
        purpose="Analyze weekly sales anomalies",
        locale="en-US",
        trace_id="trace_2",
    )
    assert result.attempts == 2
    assert result.input_tokens == 20
    assert len(router.requests) == 2


@pytest.mark.asyncio
async def test_generator_keeps_unidentified_usage_alongside_identified_model_usage() -> None:
    class MixedRouter(FakeRouter):
        async def invoke(self, agent_type, request, profile=None):
            response = await super().invoke(agent_type, request, profile)
            if len(self.requests) == 1:
                response.provider = "provider"
                response.model = "model"
            return response

    router = MixedRouter(['{"purpose_assessment":{"outcome":"clear"}}', json.dumps(_valid_payload())])
    result = await SubagentDraftGenerator(router).generate(purpose="Analyze weekly sales anomalies", locale="en-US", trace_id="trace_mixed_usage")
    assert [(line.provider, line.model, line.usage.input, line.usage.output) for line in result.model_usage] == [
        ("provider", "model", 10, 20),
        (None, None, 10, 20),
    ]


@pytest.mark.asyncio
async def test_generator_preserves_usage_when_bounded_repair_fails() -> None:
    router = FakeRouter(["{}", "{}"])

    with pytest.raises(SubagentDraftGenerationError) as raised:
        await SubagentDraftGenerator(router).generate(
            purpose="Analyze weekly sales anomalies",
            locale="en-US",
            trace_id="trace_usage_error",
        )

    assert raised.value.code == "subagent_draft_schema_invalid"
    assert raised.value.attempts == 2
    assert raised.value.input_tokens == 20
    assert raised.value.output_tokens == 40


@pytest.mark.parametrize("mode,intents,valid", [("provided_text", False, True), ("provided_text", True, False), ("tools", False, True)])
def test_generated_resource_mode_requires_consistent_capability_intents(mode, intents, valid):
    from bowhead_core.subagent_draft_generator import _parse_generation

    payload = _valid_payload()
    payload["blueprint"]["resource_mode"] = mode
    if not intents:
        payload["blueprint"]["capability_intents"] = []
    if valid:
        _, blueprint = _parse_generation(json.dumps(payload))
        assert blueprint.resource_mode == mode
    else:
        with pytest.raises(SubagentDraftGenerationError):
            _parse_generation(json.dumps(payload))


def test_system_prompt_carries_the_validated_field_names() -> None:
    # Observed on 116: prompts saying only three evidence booleans led to invented has_clear_object-like keys in 4/4 cases,
    # all rejected. Prompt field names must come from the validation model.
    from bowhead_core.subagent_authoring_models import (
        DraftBlueprint,
        GeneratedSystemPrompt,
        PurposeAssessment,
    )

    from bowhead_core import subagent_draft_generator as generator

    for model in (PurposeAssessment, DraftBlueprint, GeneratedSystemPrompt):
        for field in model.model_fields:
            assert f'"{field}"' in generator._SYSTEM_PROMPT, (model.__name__, field)
    assert '"additionalProperties":false' in generator._SYSTEM_PROMPT
    assert "_parse_generation" not in generator._SYSTEM_PROMPT


def test_generation_budget_fits_inside_request_lease_and_stale_window() -> None:
    # Lease expiry mid-generation duplicates same-key replay; leases exceeding the reclamation window mark still-generating drafts interrupted.
    from bowhead_core import subagent_draft_generator as generator
    from bowhead_core import subagent_draft_store as store

    assert generator.MODEL_HARD_TIMEOUT_SECONDS < store.REQUEST_LEASE_SECONDS
    assert store.REQUEST_LEASE_SECONDS < store.STALE_GENERATING_SECONDS
    # Observed on 116: full blueprints ~52s; primary timeout then backup 123s. Budget must at least cover the latter.
    assert generator.MODEL_HARD_TIMEOUT_SECONDS >= 125


@pytest.mark.asyncio
async def test_model_authored_intent_ids_are_replaced_by_server_ids() -> None:
    # Observed on 116: qwen / deepseek both wrote underscore IDs like ci_sales_query; unchanged validation rejected 4/4.
    payload = _valid_payload()
    blueprint = payload["blueprint"]
    assert isinstance(blueprint, dict)
    second = dict(blueprint["capability_intents"][0], intent_id="ci_chart_artifact")
    blueprint["capability_intents"] = [
        dict(blueprint["capability_intents"][0], intent_id="ci_sales_query"),
        second,
    ]
    router = FakeRouter([json.dumps(payload)])

    result = await SubagentDraftGenerator(router).generate(
        purpose="Analyze weekly store sales anomalies",
        locale="en-US",
        trace_id="draft_intent_ids",
    )

    assert result.attempts == 1
    assert result.blueprint is not None
    assert [intent.intent_id for intent in result.blueprint.capability_intents] == ["ci_1", "ci_2"]


@pytest.mark.asyncio
async def test_given_url_clarification_corrects_model_search_intent() -> None:
    payload = _valid_payload()
    blueprint = payload["blueprint"]
    assert isinstance(blueprint, dict)
    blueprint["capability_intents"] = [
        {
            "intent_id": "ci_web",
            "capability_class": "public_web_research",
            "operation": "read",
            "object_terms": ["product announcements"],
            "source_preference": "native",
            "necessity": "required",
            "reason": "Find public announcements",
            "confidence": 0.95,
        }
    ]
    router = FakeRouter([json.dumps(payload)])

    result = await SubagentDraftGenerator(router).generate(
        purpose="帮我创建一个用于核对公开产品公告的智能体",
        clarification_answer="只核对用户提供的公开官网或新闻稿网址，不自行扩展搜索",
        locale="zh-CN",
        trace_id="given_url_scope",
    )

    assert result.blueprint is not None
    assert [intent.capability_class for intent in result.blueprint.capability_intents] == ["public_web_fetch"]
    assert "Read the public page" in result.blueprint.capability_intents[0].reason


@pytest.mark.asyncio
async def test_runtime_url_wording_selects_page_reading_without_concrete_url() -> None:
    payload = _valid_payload()
    blueprint = payload["blueprint"]
    assert isinstance(blueprint, dict)
    blueprint["resource_mode"] = "tools"
    blueprint["capability_intents"][0]["capability_class"] = "public_web_research"
    router = FakeRouter([json.dumps(payload)])

    result = await SubagentDraftGenerator(router).generate(
        purpose="帮我总结用户给定的公开网页，并给出可核对的来源链接。",
        clarification_answer="网页 URL 由用户在每次使用时提供；输出摘要和关键要点。",
        locale="zh-CN",
        trace_id="runtime_url_scope",
    )

    assert result.blueprint is not None
    assert [intent.capability_class for intent in result.blueprint.capability_intents] == ["public_web_fetch"]
    assert "Never ask the creator for a concrete URL" in router.requests[0].system


@pytest.mark.asyncio
async def test_explicit_search_and_given_page_keep_both_web_needs() -> None:
    payload = _valid_payload()
    blueprint = payload["blueprint"]
    assert isinstance(blueprint, dict)
    blueprint["resource_mode"] = "tools"
    blueprint["capability_intents"][0]["capability_class"] = "public_web_research"
    router = FakeRouter([json.dumps(payload)])

    result = await SubagentDraftGenerator(router).generate(
        purpose="先搜索相关公开网页，再读取用户给定的网址并对比",
        locale="zh-CN",
        trace_id="search_and_read",
    )

    assert result.blueprint is not None
    assert [intent.capability_class for intent in result.blueprint.capability_intents] == [
        "public_web_research",
        "public_web_fetch",
    ]


@pytest.mark.asyncio
async def test_supplied_text_with_a_url_stays_text_only() -> None:
    payload = _valid_payload()
    blueprint = payload["blueprint"]
    assert isinstance(blueprint, dict)
    blueprint["resource_mode"] = "provided_text"
    blueprint["capability_intents"] = []
    router = FakeRouter([json.dumps(payload)])

    result = await SubagentDraftGenerator(router).generate(
        purpose="只整理用户提供的文字，其中包含 https://example.com 这个地址",
        locale="zh-CN",
        trace_id="provided_url_text",
    )

    assert result.blueprint is not None
    assert result.blueprint.resource_mode == "provided_text"
    assert result.blueprint.capability_intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("expectation", ["Follow the runtime user's language.", "Answer in French as explicitly requested."])
async def test_review_locale_preserves_exact_names_and_runtime_language_intent(expectation: str) -> None:
    payload = _valid_payload()
    payload["blueprint"]["display_name"] = "Dogfood 单页核对 1003"
    payload["blueprint"]["system_prompt"]["output_expectations"] = [expectation]
    purpose = f"Create an Agent named Dogfood 单页核对 1003 to analyze sales. {expectation}"
    router = FakeRouter([json.dumps(payload, ensure_ascii=False)])
    result = await SubagentDraftGenerator(router).generate(purpose=purpose, locale="en-US", trace_id="trace_language")
    assert result.blueprint.display_name == "Dogfood 单页核对 1003"
    assert result.blueprint.system_prompt.output_expectations == [expectation]
    user_input = router.requests[0].messages[0].content
    assert purpose in user_input
    assert "not an Agent answer-language setting" in user_input


@pytest.mark.asyncio
async def test_a_provider_timeout_is_reported_as_a_timeout() -> None:
    """Novice walkthrough B12: a slow model must surface as a timeout the UI can explain, not a generic failure."""
    from bowhead_router.errors import ProviderFailureKind

    class TimedOut(Exception):
        kind = ProviderFailureKind.TIMEOUT

    class SlowRouter(FakeRouter):
        async def invoke(self, agent_type, request, profile=None):
            raise TimedOut("read timeout")

    with pytest.raises(SubagentDraftGenerationError) as raised:
        await SubagentDraftGenerator(SlowRouter([])).generate(purpose="Analyze weekly sales", locale="en-US", trace_id="trace_timeout")
    assert raised.value.code == "subagent_draft_model_timeout"


@pytest.mark.asyncio
async def test_router_retries_start_only_when_they_fit_the_generation_budget() -> None:
    from bowhead_core.subagent_draft_generator import MODEL_HARD_TIMEOUT_SECONDS
    from bowhead_router.errors import ProviderFailureKind
    from bowhead_router.retry import RetryNotice, admit_retry, attempt_fits

    decisions: list[tuple[bool, bool]] = []

    class ObservingRouter(FakeRouter):
        async def invoke(self, agent_type, request, profile=None):
            short = RetryNotice(
                retry=1, max_retries=3, kind=ProviderFailureKind.TIMEOUT, delay_s=1.0, mode="invoke", provider="p", model="m", attempt_timeout_s=60.0
            )
            too_long = RetryNotice(
                retry=2,
                max_retries=3,
                kind=ProviderFailureKind.TIMEOUT,
                delay_s=1.0,
                mode="invoke",
                provider="p",
                model="m",
                attempt_timeout_s=MODEL_HARD_TIMEOUT_SECONDS + 1,
            )
            decisions.append((await admit_retry(short), await admit_retry(too_long)))
            assert attempt_fits(attempt_timeout_s=60.0)
            return await super().invoke(agent_type, request, profile)

    await SubagentDraftGenerator(ObservingRouter([json.dumps(_valid_payload())])).generate(
        purpose="Analyze weekly sales", locale="en-US", trace_id="trace_budget"
    )
    # A retry that can finish in the budget starts; one that would be cut by the hard cap does not.
    assert decisions == [(True, False)]


@pytest.mark.asyncio
async def test_exhausted_router_targets_after_timeouts_are_reported_as_a_timeout() -> None:
    """Seen live: qwen3.6-plus timed out twice at 60 s; the Router then raised its exhausted-targets error."""
    from bowhead_router.errors import InvocationTargetsExhaustedError, ProviderFailureKind

    class Failure:
        kind = ProviderFailureKind.TIMEOUT

    class SlowRouter(FakeRouter):
        async def invoke(self, agent_type, request, profile=None):
            raise InvocationTargetsExhaustedError([Failure(), Failure()])

    with pytest.raises(SubagentDraftGenerationError) as raised:
        await SubagentDraftGenerator(SlowRouter([])).generate(purpose="Analyze weekly sales", locale="en-US", trace_id="trace_exhausted")
    assert raised.value.code == "subagent_draft_model_timeout"
