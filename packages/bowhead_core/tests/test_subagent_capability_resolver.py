from __future__ import annotations

from datetime import UTC, datetime

from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.subagent_authoring_models import CapabilityIntent
from bowhead_core.subagent_capability_resolver import (
    SubagentCapabilityResolver,
)


def _tool(
    ref: str,
    *,
    risk: str = "read",
    classes: frozenset[str] = frozenset({"data_query"}),
    selectable: bool = True,
) -> AuthoringTool:
    return AuthoringTool(
        stable_ref=ref,
        display_name=ref,
        description=f"Description for {ref}",
        source_kind="team_mcp" if ref.startswith("teammcp.") else "native",
        source_display_name="CRM" if ref.startswith("teammcp.") else "Bowhead",
        capability_classes=classes,
        risk=risk,  # type: ignore[arg-type]
        discoverable=True,
        selectable=selectable,
        locked_reason=None if selectable else "mcp_feature_unlicensed",
        risk_source=("openapi_method" if ref.startswith("teammcp.") else "native_registry"),
        approval_required=risk in {"external_write", "destructive"},
        confirmation_summary=None,
        dependency_revision="v1",
    )


def _catalog(*tools: AuthoringTool) -> SubagentAuthoringCatalogSnapshot:
    return SubagentAuthoringCatalogSnapshot(
        team_id="team_a",
        actor_user_id="user_a",
        native_tools=tuple(tool for tool in tools if tool.source_kind == "native"),
        team_tools=tuple(tool for tool in tools if tool.source_kind == "team_mcp"),
        fingerprint="sha256:test",
        dependency_guard=(),
        built_at=datetime.now(UTC),
    )


def test_data_query_expands_required_schema_pair() -> None:
    resolver = SubagentCapabilityResolver()
    recommendations = resolver.resolve(
        [
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
        _catalog(
            _tool(
                "bi_schema",
                classes=frozenset({"data_discovery"}),
            ),
            _tool("bi_query"),
        ),
    )
    by_ref = {item.tool_ref: item for item in recommendations}
    assert by_ref["bi_query"].selected is True
    assert by_ref["bi_query"].resolution_confidence == "high"
    assert by_ref["bi_schema"].selected is True
    assert "catalog_required_pair" in by_ref["bi_schema"].evidence


def test_write_unknown_and_locked_tools_are_never_default_selected() -> None:
    resolver = SubagentCapabilityResolver()
    recommendations = resolver.resolve(
        [
            CapabilityIntent(
                intent_id="ci_crm",
                capability_class="team_domain_write",
                operation="external_write",
                object_terms=["customer"],
                source_preference="team",
                necessity="required",
                reason="Update customer status",
                confidence=0.99,
            ),
        ],
        _catalog(
            _tool(
                "teammcp.crm.update-customer",
                risk="external_write",
                classes=frozenset({"team_domain_write"}),
            ),
            _tool(
                "teammcp.crm.unknown",
                risk="unknown",
                classes=frozenset({"team_domain_write"}),
                selectable=False,
            ),
        ),
    )
    assert recommendations
    assert all(item.selected is False for item in recommendations)
    assert next(item for item in recommendations if item.risk == "external_write").to_dict()["locked_reason"] == "approval_executor_unavailable"


def test_known_url_only_recommends_page_reading() -> None:
    resolver = SubagentCapabilityResolver()
    catalog = _catalog(
        _tool("web_fetch", classes=frozenset({"public_web_fetch"})),
        _tool("web_search", classes=frozenset({"public_web_research"})),
    )
    intent = CapabilityIntent(
        intent_id="ci_web",
        capability_class="public_web_fetch",
        operation="read",
        object_terms=["user supplied URL"],
        source_preference="native",
        necessity="required",
        reason="Read the given page",
        confidence=0.95,
    )

    recommendations = resolver.resolve([intent], catalog)

    assert [(item.tool_ref, item.selected) for item in recommendations] == [("web_fetch", True)]


def test_web_research_recommends_search_and_page_reading() -> None:
    resolver = SubagentCapabilityResolver()
    catalog = _catalog(
        _tool("web_fetch", classes=frozenset({"public_web_fetch"})),
        _tool("web_search", classes=frozenset({"public_web_research"})),
    )
    intent = CapabilityIntent(
        intent_id="ci_web",
        capability_class="public_web_research",
        operation="read",
        object_terms=["product announcements"],
        source_preference="native",
        necessity="required",
        reason="Find and inspect public announcements",
        confidence=0.95,
    )

    recommendations = resolver.resolve([intent], catalog)

    assert {item.tool_ref for item in recommendations if item.selected} == {"web_search", "web_fetch"}


def _team_data_intent(**overrides: object) -> CapabilityIntent:
    values: dict[str, object] = {
        "intent_id": "ci_sales",
        "capability_class": "data_query",
        "operation": "read",
        "object_terms": ["sales"],
        "source_preference": "team",
        "necessity": "required",
        "reason": "Read weekly store sales",
        "confidence": 0.9,
    }
    values.update(overrides)
    return CapabilityIntent.model_validate(values)


def test_team_preference_keeps_matching_team_tool() -> None:
    resolution = SubagentCapabilityResolver().resolve_with_coverage(
        [_team_data_intent()],
        _catalog(
            _tool("bi_schema", classes=frozenset({"data_discovery"})),
            _tool("bi_query"),
            _tool("teammcp.sales.query"),
        ),
    )

    by_ref = {item.tool_ref: item for item in resolution.recommendations}
    assert set(by_ref) == {"teammcp.sales.query"}
    assert by_ref["teammcp.sales.query"].selected is True
    assert resolution.unmet_required == ()


def test_team_preference_without_team_match_falls_back_to_native_data_tools() -> None:
    resolution = SubagentCapabilityResolver().resolve_with_coverage(
        [_team_data_intent()],
        _catalog(
            _tool("bi_schema", classes=frozenset({"data_discovery"})),
            _tool("bi_query"),
            _tool("teammcp.docs.search", classes=frozenset({"team_domain_read"})),
        ),
    )

    by_ref = {item.tool_ref: item for item in resolution.recommendations}
    assert {ref for ref, item in by_ref.items() if item.selected} == {"bi_query", "bi_schema"}
    assert by_ref["bi_query"].evidence == ("native_fallback_no_team_match",)
    assert by_ref["bi_query"].tier == "required"
    assert resolution.unmet_required == ()


def test_team_fallback_never_selects_a_locked_native_tool() -> None:
    resolution = SubagentCapabilityResolver().resolve_with_coverage(
        [_team_data_intent()],
        _catalog(_tool("bi_query", selectable=False)),
    )

    assert [(item.tool_ref, item.selected) for item in resolution.recommendations] == [("bi_query", False)]
    [unmet] = resolution.unmet_required
    assert unmet.intent_id == "ci_sales"
    assert unmet.capability_class == "data_query"
    assert unmet.cause == "tool_locked"
    assert unmet.candidate_tool_refs == ("bi_query",)


def test_required_intent_with_no_tool_in_catalog_is_reported_unmet() -> None:
    resolution = SubagentCapabilityResolver().resolve_with_coverage(
        [
            _team_data_intent(),
            _team_data_intent(intent_id="ci_chart", capability_class="artifact_chart", necessity="recommended"),
        ],
        _catalog(_tool("teammcp.docs.search", classes=frozenset({"team_domain_read"}))),
    )

    assert resolution.recommendations == []
    assert [(item.intent_id, item.cause) for item in resolution.unmet_required] == [("ci_sales", "no_matching_tool")]
