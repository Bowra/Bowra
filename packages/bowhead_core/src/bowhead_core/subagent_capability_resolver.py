"""Resolve bounded capability intents against a live authoring catalog."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.subagent_authoring_models import CapabilityIntent

ResolutionConfidence = Literal["high", "medium", "low"]

_NATIVE_MAPPING: dict[str, tuple[str, ...]] = {
    "data_discovery": ("bi_schema",),
    "data_query": ("bi_query",),
    "isolated_compute": ("python_sandbox",),
    "artifact_chart": ("add_chart",),
    "artifact_report": ("canvas_add_html_artifact",),
    # Discovery may need a second step to read the found page. A known URL
    # needs only web_fetch and must not grant open-ended search by default.
    "public_web_research": ("web_search", "web_fetch"),
    "public_web_fetch": ("web_fetch",),
    "knowledge_search": ("kb_search",),
    "file_read": ("skill_read",),
}
_TOKEN_RE = re.compile(r"[a-z0-9\u3400-\u9fff]+")


@dataclass(frozen=True, slots=True)
class ToolRecommendation:
    tool_ref: str
    display_name: str
    tier: Literal["required", "recommended"]
    selected: bool
    risk: str
    source_kind: str
    source_display_name: str | None
    reason: str
    evidence: tuple[str, ...]
    resolution_confidence: ResolutionConfidence
    locked_reason: str | None
    approval_required: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_ref": self.tool_ref,
            "display_name": self.display_name,
            "tier": self.tier,
            "selected": self.selected,
            "risk": self.risk,
            "source": {
                "kind": self.source_kind,
                "display_name": self.source_display_name,
            },
            "reason": self.reason,
            "evidence": list(self.evidence),
            "confidence": self.resolution_confidence,
            "locked_reason": self.locked_reason,
            "approval_required": self.approval_required,
        }


def _tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return frozenset(_TOKEN_RE.findall(normalized))


def _select_by_default(
    tool: AuthoringTool,
    confidence: ResolutionConfidence,
) -> bool:
    return tool.selectable and confidence == "high" and tool.risk in {"read", "compute", "artifact_write"}


UnmetCause = Literal["no_matching_tool", "tool_locked", "needs_confirmation"]


@dataclass(frozen=True, slots=True)
class UnmetRequirement:
    """A required capability intent that ended with no selected tool."""

    intent_id: str
    capability_class: str
    object_terms: tuple[str, ...]
    reason: str
    cause: UnmetCause
    candidate_tool_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    recommendations: list[ToolRecommendation]
    unmet_required: tuple[UnmetRequirement, ...]


class SubagentCapabilityResolver:
    def resolve(
        self,
        intents: list[CapabilityIntent],
        catalog: SubagentAuthoringCatalogSnapshot,
    ) -> list[ToolRecommendation]:
        return self.resolve_with_coverage(intents, catalog).recommendations

    def resolve_with_coverage(
        self,
        intents: list[CapabilityIntent],
        catalog: SubagentAuthoringCatalogSnapshot,
    ) -> CapabilityResolution:
        by_ref = catalog.tools_by_ref
        recommendations: dict[str, ToolRecommendation] = {}
        selected_refs: set[str] = set()
        candidates_by_intent: list[tuple[CapabilityIntent, tuple[str, ...]]] = []

        for intent in intents:
            candidate_refs: list[str] = []
            team_candidates = self._team_candidates(intent, catalog.team_tools) if intent.source_preference != "native" else []
            # Models usually mean Datarc data by team data, which built-in system tools such as bi_query read.
            # With team preference but no comparable team tools, fall back to the system mapping;
            # otherwise data retrieval disappears (observed on 116: team connections were only external document tools,
            # leaving no retrieval tools in the draft). Locked system tools remain listed but unselected (_select_by_default
            # enforces this); system tools absent from the catalog never appear.
            native_evidence = "deterministic_native_mapping"
            use_native = intent.source_preference != "team"
            if not use_native and not team_candidates:
                use_native = True
                native_evidence = "native_fallback_no_team_match"
            if use_native:
                for tool_ref in _NATIVE_MAPPING.get(intent.capability_class, ()):
                    tool = by_ref.get(tool_ref)
                    if tool is None:
                        continue
                    recommendation = self._recommend(
                        tool,
                        intent,
                        confidence="high",
                        evidence=(native_evidence,),
                    )
                    recommendations.setdefault(tool_ref, recommendation)
                    candidate_refs.append(tool_ref)
                    if recommendation.selected:
                        selected_refs.add(tool_ref)

            for tool, confidence in team_candidates[:5]:
                recommendation = self._recommend(
                    tool,
                    intent,
                    confidence=confidence,
                    evidence=("catalog_capability_match",),
                )
                recommendations.setdefault(
                    tool.stable_ref,
                    recommendation,
                )
                candidate_refs.append(tool.stable_ref)
                if recommendation.selected:
                    selected_refs.add(tool.stable_ref)
            candidates_by_intent.append((intent, tuple(dict.fromkeys(candidate_refs))))

        if "bi_query" in selected_refs and "bi_schema" in by_ref:
            tool = by_ref["bi_schema"]
            recommendations["bi_schema"] = ToolRecommendation(
                tool_ref=tool.stable_ref,
                display_name=tool.display_name,
                tier="required",
                selected=_select_by_default(tool, "high"),
                risk=tool.risk,
                source_kind=tool.source_kind,
                source_display_name=tool.source_display_name,
                reason="Inspect available data before running a query.",
                evidence=("catalog_required_pair",),
                resolution_confidence="high",
                locked_reason=tool.locked_reason,
                approval_required=tool.approval_required,
            )

        return CapabilityResolution(
            recommendations=[recommendations[key] for key in sorted(recommendations)],
            unmet_required=tuple(_unmet_required(candidates_by_intent, recommendations)),
        )

    def _recommend(
        self,
        tool: AuthoringTool,
        intent: CapabilityIntent,
        *,
        confidence: ResolutionConfidence,
        evidence: tuple[str, ...],
    ) -> ToolRecommendation:
        tool = tool.for_selection()
        return ToolRecommendation(
            tool_ref=tool.stable_ref,
            display_name=tool.display_name,
            tier=intent.necessity,
            selected=_select_by_default(tool, confidence),
            risk=tool.risk,
            source_kind=tool.source_kind,
            source_display_name=tool.source_display_name,
            reason=intent.reason,
            evidence=evidence,
            resolution_confidence=confidence,
            locked_reason=tool.locked_reason,
            approval_required=tool.approval_required,
        )

    def _team_candidates(
        self,
        intent: CapabilityIntent,
        team_tools: tuple[AuthoringTool, ...],
    ) -> list[tuple[AuthoringTool, ResolutionConfidence]]:
        terms = {token for term in intent.object_terms for token in _tokens(term)}
        matches: list[tuple[int, str, AuthoringTool]] = []
        for tool in team_tools:
            if intent.capability_class not in tool.capability_classes:
                continue
            haystack = _tokens(
                " ".join(
                    (
                        tool.stable_ref,
                        tool.display_name,
                        tool.source_display_name or "",
                        tool.description,
                    ),
                ),
            )
            overlap = len(terms & haystack)
            matches.append((overlap, tool.stable_ref, tool))
        matches.sort(key=lambda item: (-item[0], item[1]))
        unique_top = len(matches) == 1 or (bool(matches) and matches[0][0] > matches[1][0])
        results: list[tuple[AuthoringTool, ResolutionConfidence]] = []
        for index, (overlap, _ref, tool) in enumerate(matches[:50]):
            exact = bool(terms) and overlap == len(terms)
            confidence: ResolutionConfidence = "low"
            if index == 0 and unique_top and exact and intent.necessity == "required":
                confidence = "high"
            elif overlap > 0:
                confidence = "medium"
            results.append((tool, confidence))
        return results


def _unmet_required(
    candidates_by_intent: list[tuple[CapabilityIntent, tuple[str, ...]]],
    recommendations: dict[str, ToolRecommendation],
) -> list[UnmetRequirement]:
    """Required intents none of whose candidate tools ended up selected."""
    unmet: list[UnmetRequirement] = []
    for intent, candidate_refs in candidates_by_intent:
        if intent.necessity != "required":
            continue
        if any(recommendations[ref].selected for ref in candidate_refs):
            continue
        cause: UnmetCause = "needs_confirmation"
        if not candidate_refs:
            cause = "no_matching_tool"
        elif all(recommendations[ref].locked_reason for ref in candidate_refs):
            cause = "tool_locked"
        unmet.append(
            UnmetRequirement(
                intent_id=intent.intent_id,
                capability_class=intent.capability_class,
                object_terms=tuple(intent.object_terms),
                reason=intent.reason,
                cause=cause,
                candidate_tool_refs=candidate_refs,
            ),
        )
    return unmet


__all__ = [
    "CapabilityResolution",
    "SubagentCapabilityResolver",
    "ToolRecommendation",
    "UnmetRequirement",
]
