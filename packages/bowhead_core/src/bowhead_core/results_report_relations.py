"""Typed Report artifact relations for Results and report read grants.

The extractor reads only persisted, server-confirmed section payloads.  It is
deliberately field- and section-aware: arbitrary strings named like IDs never
expand the report authorization closure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

ReportRelationRole = Literal[
    "report_chart_data",
    "report_detail_table",
    "report_computation",
    "report_embedded_media",
    "code_output",
    "code_input",
    "ordinary_source",
]

_ROLE_ORDER: tuple[ReportRelationRole, ...] = (
    "report_chart_data",
    "report_detail_table",
    "report_computation",
    "report_embedded_media",
    "code_output",
    "code_input",
    "ordinary_source",
)
_DIRECT_ROLES = frozenset(
    {
        "report_chart_data",
        "report_detail_table",
        "report_computation",
        "report_embedded_media",
    },
)


@dataclass(frozen=True, slots=True)
class ReportArtifactRelation:
    artifact_id: str
    roles: tuple[ReportRelationRole, ...]


@dataclass(frozen=True, slots=True)
class ReportRelations:
    relations: tuple[ReportArtifactRelation, ...]
    projection_revision: str

    @property
    def closure_ids(self) -> tuple[str, ...]:
        return tuple(relation.artifact_id for relation in self.relations)

    @property
    def direct_ids(self) -> tuple[str, ...]:
        return tuple(relation.artifact_id for relation in self.relations if any(role in _DIRECT_ROLES for role in relation.roles))

    def roles_for(self, artifact_id: str) -> tuple[ReportRelationRole, ...]:
        for relation in self.relations:
            if relation.artifact_id == artifact_id:
                return relation.roles
        return ()


class _RelationBuilder:
    def __init__(self) -> None:
        self._roles: dict[str, set[ReportRelationRole]] = {}

    def add(
        self,
        value: Any,
        *roles: ReportRelationRole,
    ) -> None:
        if not isinstance(value, str):
            return
        artifact_id = value.strip()
        if not artifact_id:
            return
        bucket = self._roles.setdefault(artifact_id, set())
        bucket.update(roles)

    def add_list(
        self,
        value: Any,
        *roles: ReportRelationRole,
    ) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if isinstance(item, dict):
                self.add(item.get("artifact_id"), *roles)
            else:
                self.add(item, *roles)

    def build(self) -> ReportRelations:
        relations = tuple(
            ReportArtifactRelation(
                artifact_id=artifact_id,
                roles=tuple(role for role in _ROLE_ORDER if role in roles),
            )
            for artifact_id, roles in self._roles.items()
        )
        canonical = sorted(
            [[relation.artifact_id, list(relation.roles)] for relation in relations],
            key=lambda item: item[0],
        )
        revision = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        return ReportRelations(
            relations=relations,
            projection_revision=revision,
        )


def _field(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _add_source_refs(builder: _RelationBuilder, value: Any) -> None:
    builder.add_list(value, "ordinary_source")


def _project_section_payload(
    builder: _RelationBuilder,
    *,
    kind: str,
    payload: dict[str, Any],
    direct: bool,
) -> None:
    # Persisted section-level source references are closure-only.  Their value
    # may be a SourceRef identity rather than an artifact identity; unmatched
    # values naturally disappear at the artifact-store authorization join.
    _add_source_refs(builder, payload.get("source_refs"))

    if kind == "chart":
        value = _field(payload.get("spec"), "data_artifact_id")
        value = value or payload.get("data_artifact_id")
        builder.add(
            value,
            *(("report_chart_data",) if direct else ()),
        )
        plan = _field(payload.get("spec"), "presentation_plan")
        notes = _field(plan, "evidence_notes")
        if isinstance(notes, list):
            for note in notes[:3]:
                builder.add(_field(_field(note, "source_ref"), "artifact_id"), "ordinary_source")
    elif kind == "data_table":
        value = _field(payload.get("meta"), "artifact_id") or payload.get("data_artifact_id") or payload.get("artifact_id")
        builder.add(
            value,
            *(("report_embedded_media",) if direct else ()),
        )
    elif kind == "insight_card":
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                table_roles: tuple[ReportRelationRole, ...] = ("report_detail_table",) if direct else ()
                computation_roles: tuple[ReportRelationRole, ...] = ("report_computation",) if direct else ()
                chart_roles: tuple[ReportRelationRole, ...] = ("report_chart_data",) if direct else ()
                # Older persisted InsightCard payloads could carry the chart
                # backing artifact directly.  This is a frozen typed field,
                # not a generic recursive artifact-ID fallback.
                builder.add(item.get("data_artifact_id"), *chart_roles)
                builder.add(item.get("table_artifact_id"), *table_roles)
                builder.add(
                    item.get("computation_artifact_id"),
                    *computation_roles,
                )
    elif kind in {
        "image",
        "html_artifact",
        "artifact",
        "datarc_iframe",
        "external_iframe",
    }:
        roles: tuple[ReportRelationRole, ...] = ("report_embedded_media",) if direct else ()
        builder.add(payload.get("artifact_id"), *roles)
        builder.add_list(payload.get("artifact_ids"), *roles)
    elif kind == "code_block":
        builder.add_list(payload.get("input_artifact_ids"), "code_input")
        builder.add_list(payload.get("input_artifacts"), "code_input")
        output_roles: tuple[ReportRelationRole, ...] = ("report_computation", "code_output") if direct else ("code_output",)
        builder.add_list(payload.get("output_artifact_ids"), *output_roles)
        builder.add_list(payload.get("output_artifacts"), *output_roles)
    elif kind == "design":
        # Design backing/flattened artifacts are required to render the report,
        # but are not user-facing entries in the direct-relation lens.
        builder.add(_field(payload.get("background"), "artifact_id"))
        builder.add(payload.get("flattened_artifact_id"))
    elif kind in {"source_list", "sources"}:
        entries = payload.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                builder.add(entry.get("artifact_id"), "ordinary_source")
                sub_sources = entry.get("sub_sources")
                if isinstance(sub_sources, list):
                    for sub_source in sub_sources:
                        if isinstance(sub_source, dict):
                            builder.add(
                                sub_source.get("artifact_id"),
                                "ordinary_source",
                            )

    # Frozen protected fields used by existing report renderers.  They extend
    # the closure only for the explicitly listed section kinds; this is not a
    # recursive "find anything named artifact_id" scanner.
    if kind in {
        "artifact",
        "datarc_iframe",
        "external_iframe",
    }:
        builder.add(payload.get("manifest_artifact_id"))
        builder.add_list(payload.get("input_artifact_ids"), "code_input")
        builder.add_list(payload.get("output_artifact_ids"), "code_output")

    body = payload.get("body")
    if isinstance(body, list):
        for child in body:
            if not isinstance(child, dict):
                continue
            child_kind = child.get("kind")
            if not isinstance(child_kind, str):
                continue
            _project_section_payload(
                builder,
                kind=child_kind,
                payload=child,
                direct=direct,
            )


def project_report_relations(
    sections: list[Any],
    *,
    source_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> ReportRelations:
    """Project closure and direct roles from persisted Canvas state.

    Superseded sections remain in the readable closure because the report UI
    exposes their collapsed history, but they cannot populate the "current
    report" lens. Chat-hidden sections are excluded from both sets.
    """

    builder = _RelationBuilder()
    for section in sections:
        if bool(getattr(section, "hidden_for_chat_mode", False)):
            continue
        payload = getattr(section, "payload", None)
        kind = getattr(section, "kind", None)
        if not isinstance(payload, dict) or not isinstance(kind, str):
            continue
        _project_section_payload(
            builder,
            kind=kind,
            payload=payload,
            direct=not bool(getattr(section, "superseded", False)),
        )

    for ref in source_refs:
        if not isinstance(ref, dict):
            continue
        builder.add(ref.get("artifact_id"), "ordinary_source")
        sub_sources = ref.get("sub_sources")
        if isinstance(sub_sources, list):
            for sub_source in sub_sources:
                if isinstance(sub_source, dict):
                    builder.add(
                        sub_source.get("artifact_id"),
                        "ordinary_source",
                    )

    return builder.build()


__all__ = [
    "ReportArtifactRelation",
    "ReportRelationRole",
    "ReportRelations",
    "project_report_relations",
]
