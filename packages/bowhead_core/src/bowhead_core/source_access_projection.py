"""Viewer-safe source projection and provenance-preserving patch merge.

Persisted Canvas provenance remains canonical.  Every viewer response receives a
deep-copy projection whose source identities are report-scoped HMAC aliases.  A
sanitized response can therefore be edited without becoming the provenance write
authority: the merge helper restores protected fields from the current revision.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from bowhead_core.citation_marks import CITATION_PROSE_KEYS, strip_legacy
from bowhead_core.embedded_data_provenance import project_embedded_data_provenance

SOURCE_ALIAS_PREFIX = "source-ref-v1:"
MAX_SOURCE_IDENTITIES = 4_096
MAX_SOURCE_IDENTITY_BYTES = 512

SourceGenericType = Literal[
    "query_result",
    "document",
    "web_source",
    "generated_artifact",
    "other_source",
]


@dataclass(frozen=True, slots=True)
class SourcePresentationPath:
    section_kind: str
    path: tuple[str, ...]
    category: Literal["identity", "source_object", "ordinal"]


# One registry drives view projection, PATCH protection, and later static export.
SOURCE_PRESENTATION_PATHS: tuple[SourcePresentationPath, ...] = (
    SourcePresentationPath("*", ("source_refs", "*"), "identity"),
    SourcePresentationPath("sources", ("entries", "*"), "source_object"),
    SourcePresentationPath("chart", ("spec", "source_meta"), "source_object"),
    SourcePresentationPath("chart", ("spec", "presentation_plan", "evidence_notes", "*", "source_ref"), "source_object"),
    SourcePresentationPath("chart", ("spec", "presentation_plan", "dataset", "artifact_id"), "identity"),
    SourcePresentationPath("chart", ("data_source",), "source_object"),
    SourcePresentationPath("html_artifact", ("sources", "*"), "ordinal"),
)

# These fields contain the word ``source`` but identify a rendered visual's
# revision, not report provenance.  Keeping this exception list beside the
# presentation registry makes additions deliberate and testable.
SOURCE_NON_PRESENTATION_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (("design", ("flattened_source_rev",)),)

_SOURCE_IDENTITY_KEYS = (
    "id",
    "source_id",
    "artifact_id",
    "resolved_from_artifact_id",
    "context_id",
    "kb_chunk_id",
    "kb_file_id",
)
_REF_RE = re.compile(r"\[ref:([^\[\]\s]{1,1024})\]")
_NUM_SRC_RE = re.compile(r"(\[num:[^\[\]]*?:src:)([^:\[\]\s]+)(\])")


class SourcePatchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def derive_source_alias_key(root_secret: str | bytes) -> bytes:
    secret = root_secret.encode("utf-8") if isinstance(root_secret, str) else root_secret
    if not secret:
        raise ValueError("source alias root secret must not be empty")
    return hmac.new(secret, b"bowhead/source-ref-alias-key/v1", hashlib.sha256).digest()


def _valid_identity(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(raw) <= MAX_SOURCE_IDENTITY_BYTES else None


@dataclass(slots=True)
class ViewerSourceAliasMap:
    source_alias_key: bytes
    workspace_id: str
    report_scope_id: str
    _raw_to_alias: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    _alias_to_raw: dict[str, str] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.source_alias_key or not self.workspace_id or not self.report_scope_id:
            raise ValueError("source alias context must be complete")

    @classmethod
    def from_root_secret(
        cls,
        root_secret: str | bytes,
        *,
        workspace_id: str,
        report_scope_id: str,
    ) -> ViewerSourceAliasMap:
        return cls(
            derive_source_alias_key(root_secret),
            workspace_id=workspace_id,
            report_scope_id=report_scope_id,
        )

    def _sign(self, value: object) -> str:
        digest = hmac.new(self.source_alias_key, _canonical_json(value), hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return SOURCE_ALIAS_PREFIX + token

    def alias_for(self, identity: object, *, slot: tuple[str, str, int]) -> tuple[str, str | None]:
        valid = _valid_identity(identity)
        if valid is None:
            alias = self._sign(["invalid-source-slot-v1", self.workspace_id, self.report_scope_id, *slot])
            # Invalid identities never enter the HMAC input. A structured marker
            # can still round-trip through a no-op viewer PATCH because this
            # request-scoped reverse map is primed from the canonical payload.
            if isinstance(identity, str) and identity:
                self._alias_to_raw[alias] = identity
            return alias, None
        cached = self._raw_to_alias.get(valid)
        if cached is not None:
            return cached, valid
        alias = self._sign(["source-ref-v1", self.workspace_id, self.report_scope_id, valid])
        self._raw_to_alias[valid] = alias
        self._alias_to_raw[alias] = valid
        return alias, valid

    def raw_for_alias(self, alias: object) -> str | None:
        return self._alias_to_raw.get(alias) if isinstance(alias, str) else None


@dataclass(slots=True)
class StaticSafeSourceMap:
    """Ephemeral raw identity -> export ordinal mapping.

    Only the irreversible ``source-ordinal:N`` token is allowed to enter the
    frozen export snapshot/manifest. The mapping itself exists for one export
    snapshot construction and is never persisted.
    """

    _raw_to_token: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    _next_ordinal: int = field(init=False, default=1, repr=False)

    def token_for(self, identity: object, *, slot: tuple[str, str, int]) -> str:
        valid = _valid_identity(identity)
        if valid is not None:
            existing = self._raw_to_token.get(valid)
            if existing is not None:
                return existing
        token = f"source-ordinal:{self._next_ordinal}"
        self._next_ordinal += 1
        if valid is not None:
            self._raw_to_token[valid] = token
        return token


def generic_source_type(identity: object, source: Mapping[str, Any] | None = None) -> SourceGenericType:
    raw = identity if isinstance(identity, str) else ""
    if raw.startswith("art_"):
        return "generated_artifact"
    if raw.startswith(("kb_", "kbf_", "chunk_")):
        return "document"
    if raw.startswith(("tl_", "call_", "src_")):
        return "query_result"
    if raw.startswith(("http://", "https://")):
        return "web_source"
    safe_kind = str((source or {}).get("generic_type") or "")
    if safe_kind in {"query_result", "document", "web_source", "generated_artifact", "other_source"}:
        return safe_kind  # type: ignore[return-value]
    return "other_source"


def canonical_source_identity(source: Mapping[str, Any]) -> object:
    for key in _SOURCE_IDENTITY_KEYS:
        if key in source:
            return source[key]
    return None


def _path_matches_prefix(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if len(pattern) > len(path):
        return False
    return all(expected == "*" or expected == actual for expected, actual in zip(pattern, path, strict=False))


def _registered_path_relation(section_kind: str, path: tuple[str, ...]) -> Literal["covered", "ancestor"] | None:
    for entry in SOURCE_PRESENTATION_PATHS:
        if entry.section_kind not in {"*", section_kind}:
            continue
        if _path_matches_prefix(entry.path, path):
            return "covered"
        if _path_matches_prefix(path, entry.path):
            return "ancestor"
    for kind, registered_path in SOURCE_NON_PRESENTATION_PATHS:
        if kind == section_kind and _path_matches_prefix(registered_path, path):
            return "covered"
    return None


def unregistered_source_like_paths(section_kind: str, payload: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Return structured fields that look source-bearing but lack a registry entry.

    This is both a runtime fail-closed seam and the schema/fixture guard used by
    tests.  It intentionally inspects field names, never report prose values.
    """

    found: list[tuple[str, ...]] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        relation = _registered_path_relation(section_kind, path) if path else None
        if relation == "covered":
            return
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key)
                child_path = (*path, key)
                child_relation = _registered_path_relation(section_kind, child_path)
                if "source" in key.lower() and child_relation is None:
                    found.append(child_path)
                    continue
                walk(child, child_path)
        elif isinstance(value, list):
            for child in value:
                walk(child, (*path, "*"))

    walk(payload, ())
    return tuple(found)


def _redact_unregistered_source_like_fields(section_kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    blocked = set(unregistered_source_like_paths(section_kind, payload))

    def redact(value: Any, path: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            return {str(key): redact(child, (*path, str(key))) for key, child in value.items() if (*path, str(key)) not in blocked}
        if isinstance(value, list):
            return [redact(child, (*path, "*")) for child in value]
        return copy.deepcopy(value)

    return redact(payload, ())


def collect_source_identities(
    sections: Sequence[Mapping[str, Any]],
    source_refs: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        valid = _valid_identity(value)
        if valid is not None and valid not in seen:
            seen.add(valid)
            out.append(valid)

    for ref in source_refs:
        add(canonical_source_identity(ref))
    for section in sections:
        refs = section.get("source_refs")
        if isinstance(refs, list):
            for ref in refs:
                add(ref)
        kind = section.get("kind")
        if kind == "sources":
            entries = section.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, Mapping):
                        add(canonical_source_identity(entry))
        if kind == "chart":
            spec = section.get("spec")
            if isinstance(spec, Mapping) and isinstance(spec.get("source_meta"), Mapping):
                add(canonical_source_identity(spec["source_meta"]))
            if isinstance(spec, Mapping) and isinstance(spec.get("presentation_plan"), Mapping):
                plan = spec["presentation_plan"]
                if isinstance(plan.get("dataset"), Mapping):
                    add(plan["dataset"].get("artifact_id"))
                for note in plan["evidence_notes"][:3] if isinstance(plan.get("evidence_notes"), list) else []:
                    if isinstance(note, Mapping) and isinstance(note.get("source_ref"), Mapping):
                        add(canonical_source_identity(note["source_ref"]))
            if isinstance(section.get("data_source"), Mapping):
                add(canonical_source_identity(section["data_source"]))
    return out


def _safe_source_detail(
    source: Mapping[str, Any],
    *,
    alias: str,
    identity: str,
    ordinal: int | None,
    safe_links: Mapping[str, str] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": alias,
        "source_alias": alias,
        "access_state": "linked_accessible",
        "generic_type": generic_source_type(identity, source),
    }
    # Batch 0 0-D (chart-table-ux tech-design §1.4): ordinal is a document-level display number;
    # omit if unmatched to the source list. FE shows only source names, never a fabricated [1].
    if isinstance(ordinal, int) and ordinal > 0:
        out["ordinal"] = ordinal
    label = source.get("label") or source.get("kb_filename")
    query = source.get("query")
    preview = source.get("preview")
    if isinstance(label, str) and label:
        out["safe_label"] = label
    if isinstance(query, str) and query:
        out["safe_query"] = query
    if isinstance(preview, str) and preview:
        out["safe_preview"] = preview
    columns = source.get("preview_columns")
    rows = source.get("preview_rows")
    if isinstance(columns, list) and isinstance(rows, list):
        out["safe_preview_columns"] = copy.deepcopy(columns)
        out["safe_preview_rows"] = copy.deepcopy(rows)
        if isinstance(source.get("total_rows"), int):
            out["safe_total_rows"] = source["total_rows"]
    if safe_links:
        out.update({key: value for key, value in safe_links.items() if isinstance(value, str) and value})
    return out


# KB chunk sources (KB-answer citation technical design §3 reports): gateway precomputes state/fields using
# **viewer's** KB read permissions (``kb_presentation_by_identity``); only apply allowlists here. Only ``linked_accessible``
# includes filenames/locations; restricted/missing entries have only alias/state. KB entries never expose artifact bytes (safe_url, etc.);
# opening uses the KB's own permission gate.
KB_SOURCE_SAFE_FIELDS = ("safe_label", "kb_file_id", "kb_chunk_id", "kb_scope", "kb_space_id")
_KB_SOURCE_STATES = frozenset({"linked_accessible", "linked_restricted", "linked_missing"})


def _kb_source_object(presentation: Mapping[str, Any], *, alias: str, ordinal: int | None) -> dict[str, Any]:
    state = presentation.get("access_state")
    if state not in _KB_SOURCE_STATES:
        state = "linked_restricted"  # Unknown state fails closed.
    out: dict[str, Any] = {
        "id": alias,
        "source_alias": alias,
        "access_state": state,
        "generic_type": "document",
    }
    if isinstance(ordinal, int) and ordinal > 0:
        out["ordinal"] = ordinal
    if state == "linked_accessible":
        for key in KB_SOURCE_SAFE_FIELDS:
            value = presentation.get(key)
            if isinstance(value, str) and value:
                out[key] = value
    return out


def _unavailable_source(source: Mapping[str, Any], *, alias: str, identity: object, ordinal: int | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": alias,
        "source_alias": alias,
        "access_state": "linked_unavailable",
        "generic_type": generic_source_type(identity, source),
    }
    if isinstance(ordinal, int) and ordinal > 0:
        out["ordinal"] = ordinal
    return out


def _project_source_object(
    source: Mapping[str, Any],
    *,
    alias_map: ViewerSourceAliasMap,
    access_by_identity: Mapping[str, bool],
    section_id: str,
    registry_path: str,
    ordinal: int | None,
    slot_ordinal: int | None = None,
    safe_link_builder: Callable[[str], Mapping[str, str]] | None,
    kb_presentation_by_identity: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    # Batch 0 0-D: decouple slot ordinal (alias-stability key) from display ordinal; when charts' display numbers
    # point to real source-list ordinals, preserve alias slots or source ordering
    # changes aliases, invalidating all cache/authorization records.
    slot = slot_ordinal if slot_ordinal is not None else (ordinal if ordinal is not None else 1)
    identity = canonical_source_identity(source)
    alias, valid = alias_map.alias_for(identity, slot=(section_id, registry_path, slot))
    kb_presentation = kb_presentation_by_identity.get(valid) if valid is not None and kb_presentation_by_identity else None
    if kb_presentation is not None:
        return _kb_source_object(kb_presentation, alias=alias, ordinal=ordinal)
    accessible = valid is not None and access_by_identity.get(valid, False)
    if not accessible:
        out = _unavailable_source(source, alias=alias, identity=identity, ordinal=ordinal)
        # ★116 dogfood 1.0.95 P3 (t_7781d2970a27 batch-label shape): identity-less
        # descriptive sources (peer-reported entries with artifact_id=None) **have no object to authorize**.
        # Their label/query is report-author text, trusted like the body; stripping it leaves FE showing misleading
        # "Source is currently unavailable". Give them **distinct "descriptive" state** with text;
        # do not reuse linked_unavailable+safe_label: FE deliberately hides safe_label for that state
        # even if present (prevents stale projection leakage after permission revocation, pinned by paired tests).
        # Both semantics must be distinguishable via access_state.
        # **Identifiable entries without permission** remain fully stripped (B1a security boundary unchanged, paired tests pin it).
        if identity is None:
            label = source.get("label") or source.get("kb_filename")
            query = source.get("query")
            if (isinstance(label, str) and label) or (isinstance(query, str) and query):
                out["access_state"] = "descriptive"
                if isinstance(label, str) and label:
                    out["safe_label"] = label
                if isinstance(query, str) and query:
                    out["safe_query"] = query
        return out
    links = safe_link_builder(valid) if safe_link_builder is not None else None
    return _safe_source_detail(source, alias=alias, identity=valid, ordinal=ordinal, safe_links=links)


def _project_chart_evidence_plan(
    spec: dict[str, Any],
    raw_spec: Mapping[str, Any],
    *,
    alias_map: ViewerSourceAliasMap,
    access_by_identity: Mapping[str, bool],
    evidence_validity_by_digest: Mapping[tuple[str, str], bool],
    section_id: str,
    safe_link_builder: Callable[[str], Mapping[str, str]] | None,
) -> None:
    from bowhead_core.chart.presentation_plan import redact_note_text, validate_stored_notes_plan

    try:
        validate_stored_notes_plan(raw_spec)
    except (ValueError, TypeError, KeyError, AttributeError):
        # Unknown/corrupt material plans cannot expose protected text or descriptors.
        spec["presentation_plan"] = {
            "version": raw_spec.get("presentation_plan", {}).get("version") if isinstance(raw_spec.get("presentation_plan"), Mapping) else None,
            "availability": "unavailable",
        }
        for chart in [spec, *(view["spec"] for view in spec.get("views", []) if isinstance(view, Mapping) and isinstance(view.get("spec"), dict))]:
            chart["title"] = ""
            chart.pop("subtitle", None)
        return
    raw_plan = raw_spec["presentation_plan"]
    plan = copy.deepcopy(raw_plan)
    dataset = raw_plan["dataset"]
    aid = dataset["artifact_id"]
    alias = alias_map.alias_for(aid, slot=(section_id, "spec.presentation_plan.dataset", 1))[0]
    dataset_available = bool(access_by_identity.get(aid) and evidence_validity_by_digest.get((aid, dataset["content_digest"])))
    plan["dataset"] = {
        **(dataset if dataset_available else {}),
        "artifact_id": alias,
        "availability": "available" if dataset_available else "unavailable",
    }
    if "bindings" in plan:
        plan["bindings"] = {
            bid: ({**descriptor, "availability": "available"} if dataset_available else {"kind": descriptor["kind"], "availability": "unavailable"})
            for bid, descriptor in plan["bindings"].items()
        }
    unavailable: set[str] = set()
    for i, note in enumerate(plan["evidence_notes"]):
        source = note["source_ref"]
        identity = canonical_source_identity(source)
        projected_source = _project_source_object(
            source,
            alias_map=alias_map,
            access_by_identity=access_by_identity,
            section_id=section_id,
            registry_path=f"spec.presentation_plan.evidence_notes.{note['id']}.source_ref",
            ordinal=None,
            slot_ordinal=i + 1,
            safe_link_builder=safe_link_builder,
        )
        available = projected_source["access_state"] == "linked_accessible" and evidence_validity_by_digest.get(
            (identity, note["verified_content_digest"]), False
        )
        if not available:
            unavailable.add(note["id"])
            for key in ("excerpt", "locator", "verified_content_digest"):
                note.pop(key, None)
        note["source_ref"] = projected_source
        note["availability"] = "available" if available else "unavailable"
    spec["presentation_plan"] = plan
    redact_note_text(spec, raw_plan["text_dependencies"], unavailable=unavailable, dataset_available=dataset_available)
    if "annotations" in plan:
        plan["annotations"] = [
            {**item, "availability": "available"}
            if dataset_available and not unavailable
            else {"id": item["id"], "view_id": item["view_id"], "availability": "unavailable"}
            for item in raw_plan["annotations"]
        ]
    if "table_trends" in plan:
        plan["table_trends"] = [
            {**item, "availability": "available"} if dataset_available else {"view_id": item["view_id"], "availability": "unavailable"}
            for item in raw_plan["table_trends"]
        ]
    if "analytical_views" in plan:
        plan["analytical_views"] = [
            {**item, "availability": "available"}
            if dataset_available and not unavailable
            else {"view_id": item["view_id"], "availability": "unavailable"}
            for item in raw_plan["analytical_views"]
        ]
    if not dataset_available:
        spec.pop("data_artifact_id", None)
        for view in spec.get("views", []):
            view["spec"].pop("data_artifact_id", None)


def _replace_marker_identities(
    text: str,
    *,
    alias_map: ViewerSourceAliasMap,
    section_id: str,
    registry_path: str,
) -> str:
    counter = 0

    def alias(raw: str) -> str:
        nonlocal counter
        counter += 1
        return alias_map.alias_for(raw, slot=(section_id, registry_path, counter))[0]

    out = _REF_RE.sub(lambda match: f"[ref:{alias(match.group(1))}]", text)
    return _NUM_SRC_RE.sub(lambda match: f"{match.group(1)}{alias(match.group(2))}{match.group(3)}", out)


def _project_prose_markers(value: Any, *, alias_map: ViewerSourceAliasMap, section_id: str, path: str = "") -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in CITATION_PROSE_KEYS and isinstance(child, str):
                out[key] = _replace_marker_identities(
                    child,
                    alias_map=alias_map,
                    section_id=section_id,
                    registry_path=child_path,
                )
            else:
                out[key] = _project_prose_markers(child, alias_map=alias_map, section_id=section_id, path=child_path)
        return out
    if isinstance(value, list):
        return [_project_prose_markers(child, alias_map=alias_map, section_id=section_id, path=f"{path}[]") for child in value]
    return copy.deepcopy(value)


# Viewer-only derived field on code_block sections: the report-scoped aliases of the run's output
# artifacts. A numbered citation carries only its source alias, so this is how the browser links a
# "Run Code" source to the code that produced it. Aliases are a stateless HMAC of the identity, so the
# value depends only on the section itself: live events, replay and the canvas detail agree.
# The name deliberately avoids "source": it is derived, never persisted, and must not trip the
# unregistered-source-field guard when a viewer echoes the section back (the merge drops it).
CODE_BLOCK_OUTPUT_ALIASES_KEY = "output_aliases"


def code_block_output_aliases(
    raw_section: Mapping[str, Any],
    *,
    alias_map: ViewerSourceAliasMap,
    section_id: str,
) -> list[str]:
    raw_outputs = raw_section.get("output_artifact_ids")
    if not isinstance(raw_outputs, list):
        return []
    aliases: list[str] = []
    for ordinal, artifact_id in enumerate(raw_outputs, start=1):
        if _valid_identity(artifact_id) is None:
            continue
        alias = alias_map.alias_for(artifact_id, slot=(section_id, "output_artifact_ids", ordinal))[0]
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def project_sources_for_viewer(
    sections: Sequence[Mapping[str, Any]],
    source_refs: Sequence[Mapping[str, Any]],
    *,
    alias_map: ViewerSourceAliasMap,
    access_by_identity: Mapping[str, bool],
    safe_link_builder: Callable[[str], Mapping[str, str]] | None = None,
    kb_presentation_by_identity: Mapping[str, Mapping[str, Any]] | None = None,
    evidence_validity_by_digest: Mapping[tuple[str, str], bool] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Batch 0 0-D (chart-table-ux tech-design §1.4): chart source-line [N] formerly hardcoded ordinal=1
    # for spec.source_meta / data_source (intended as section-local slot), but frontend rendered
    # document-level citations, giving all 37 source lines in one production report [1]
    # (cv_55d680eda3f7; 27 charts actually used 23 distinct artifacts). Fix: prescan source list
    # for artifact_id → actual ordinal; emit matches' real numbers, omit unmatched numbers
    # (FE shows source name). Match raw ids on projection **input**, before aliases, with the same rule as FE
    # numberedSources: use the first non-superseded sources section.
    chart_source_ordinals: dict[str, int] = {}
    for raw in sections:
        if str(raw.get("kind") or "") != "sources" or raw.get("superseded"):
            continue
        raw_entries = raw.get("entries")
        # review: `break` formerly lay outside this isinstance; a first non-superseded sources section without
        # an entries array stopped BE with an empty mapping, making **all chart [N] disappear**;
        # FE `canvas-context.tsx` numberedSources continues to later sections (non-array entries also continue).
        # The comment claimed FE parity then, but behavior differed.
        if not isinstance(raw_entries, list):
            continue
        for entry_ordinal, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, Mapping):
                continue
            entry_artifact = entry.get("artifact_id")
            if isinstance(entry_artifact, str) and entry_artifact and entry_artifact not in chart_source_ordinals:
                chart_source_ordinals[entry_artifact] = entry_ordinal
        break

    def _chart_display_ordinal(source: Mapping[str, Any]) -> int | None:
        artifact_id = source.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            return chart_source_ordinals.get(artifact_id)
        return None

    projected_sections: list[dict[str, Any]] = []
    for section_index, raw_section in enumerate(sections):
        section_kind = str(raw_section.get("kind") or "")
        safe_section = _redact_unregistered_source_like_fields(section_kind, raw_section)
        section = _project_prose_markers(
            safe_section,
            alias_map=alias_map,
            section_id=str(raw_section.get("id") or f"section-{section_index}"),
        )
        section_id = str(section.get("id") or f"section-{section_index}")
        refs = raw_section.get("source_refs")
        if isinstance(refs, list):
            section["source_refs"] = [
                alias_map.alias_for(ref, slot=(section_id, "source_refs", ordinal))[0] for ordinal, ref in enumerate(refs, start=1)
            ]
        if section.get("kind") == "sources" and isinstance(raw_section.get("entries"), list):
            section["entries"] = [
                _project_source_object(
                    entry if isinstance(entry, Mapping) else {},
                    alias_map=alias_map,
                    access_by_identity=access_by_identity,
                    section_id=section_id,
                    registry_path="entries",
                    ordinal=ordinal,
                    safe_link_builder=safe_link_builder,
                    kb_presentation_by_identity=kb_presentation_by_identity,
                )
                for ordinal, entry in enumerate(raw_section["entries"], start=1)
            ]
        if section.get("kind") == "chart":
            raw_spec = raw_section.get("spec")
            if isinstance(raw_spec, Mapping):
                projected_spec = section.get("spec")
                spec = copy.deepcopy(dict(projected_spec)) if isinstance(projected_spec, Mapping) else copy.deepcopy(dict(raw_spec))
                source_meta = raw_spec.get("source_meta")
                if isinstance(source_meta, Mapping):
                    # Display number = actual source-list ordinal (unmatched = None; FE shows source name);
                    # slot_ordinal remains 1, an alias-stability key independent of display.
                    spec["source_meta"] = _project_source_object(
                        source_meta,
                        alias_map=alias_map,
                        access_by_identity=access_by_identity,
                        section_id=section_id,
                        registry_path="spec.source_meta",
                        ordinal=_chart_display_ordinal(source_meta),
                        slot_ordinal=1,
                        safe_link_builder=safe_link_builder,
                        kb_presentation_by_identity=kb_presentation_by_identity,
                    )
                if "presentation_plan" in raw_spec:
                    _project_chart_evidence_plan(
                        spec,
                        raw_spec,
                        alias_map=alias_map,
                        access_by_identity=access_by_identity,
                        evidence_validity_by_digest=evidence_validity_by_digest or {},
                        section_id=section_id,
                        safe_link_builder=safe_link_builder,
                    )
                section["spec"] = spec
            data_source = raw_section.get("data_source")
            if isinstance(data_source, Mapping):
                section["data_source"] = _project_source_object(
                    data_source,
                    alias_map=alias_map,
                    access_by_identity=access_by_identity,
                    section_id=section_id,
                    registry_path="data_source",
                    ordinal=_chart_display_ordinal(data_source),
                    slot_ordinal=1,
                    safe_link_builder=safe_link_builder,
                    kb_presentation_by_identity=kb_presentation_by_identity,
                )
        if section.get("kind") == "code_block":
            section.pop(CODE_BLOCK_OUTPUT_ALIASES_KEY, None)
            aliases = code_block_output_aliases(raw_section, alias_map=alias_map, section_id=section_id)
            if aliases:
                section[CODE_BLOCK_OUTPUT_ALIASES_KEY] = aliases
        if section.get("kind") == "html_artifact" and "sources" in raw_section:
            section["sources"] = list(project_embedded_data_provenance({"sources": raw_section["sources"]}).source_indexes)
        projected_sections.append(section)

    projected_refs: list[dict[str, Any]] = []
    for ordinal, ref in enumerate(source_refs, start=1):
        identity = canonical_source_identity(ref)
        valid = _valid_identity(identity)
        if valid is None or not access_by_identity.get(valid, False):
            # Unavailable top-level metadata is intentionally omitted; carriers
            # inside sections preserve the alias and linked-unavailable state.
            alias_map.alias_for(identity, slot=("sourceRefs", "sourceRefs", ordinal))
            continue
        projected_refs.append(
            _project_source_object(
                ref,
                alias_map=alias_map,
                access_by_identity=access_by_identity,
                section_id="sourceRefs",
                registry_path="sourceRefs",
                ordinal=ordinal,
                safe_link_builder=safe_link_builder,
                kb_presentation_by_identity=kb_presentation_by_identity,
            ),
        )
    return projected_sections, projected_refs


def _replace_markers_for_static_export(
    text: str,
    *,
    source_map: StaticSafeSourceMap,
    section_id: str,
    registry_path: str,
) -> str:
    counter = 0

    def token(raw: str) -> str:
        nonlocal counter
        counter += 1
        return source_map.token_for(raw, slot=(section_id, registry_path, counter))

    out = _REF_RE.sub(lambda match: f"[ref:{token(match.group(1))}]", text)
    return _NUM_SRC_RE.sub(lambda match: f"{match.group(1)}{token(match.group(2))}{match.group(3)}", out)


def _project_static_prose(
    value: Any,
    *,
    source_map: StaticSafeSourceMap,
    section_id: str,
    path: str = "",
) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in CITATION_PROSE_KEYS and isinstance(child, str):
                out[key] = _replace_markers_for_static_export(
                    child,
                    source_map=source_map,
                    section_id=section_id,
                    registry_path=child_path,
                )
            else:
                out[key] = _project_static_prose(
                    child,
                    source_map=source_map,
                    section_id=section_id,
                    path=child_path,
                )
        return out
    if isinstance(value, list):
        return [_project_static_prose(child, source_map=source_map, section_id=section_id, path=f"{path}[]") for child in value]
    return copy.deepcopy(value)


def _static_source_object(
    source: Mapping[str, Any],
    *,
    source_map: StaticSafeSourceMap,
    section_id: str,
    registry_path: str,
    ordinal: int,
) -> dict[str, Any]:
    identity = canonical_source_identity(source)
    return {
        "source_ref": source_map.token_for(identity, slot=(section_id, registry_path, ordinal)),
        "generic_type": generic_source_type(identity, source),
        "ordinal": ordinal,
    }


def project_section_for_static_export(
    *,
    section_id: str,
    section_kind: str,
    payload: Mapping[str, Any],
    source_refs: Sequence[Any],
    source_map: StaticSafeSourceMap,
) -> tuple[dict[str, Any], list[str]]:
    """Return a static-safe payload and ordinal-only source references."""

    safe_payload = _redact_unregistered_source_like_fields(section_kind, payload)
    projected = _project_static_prose(safe_payload, source_map=source_map, section_id=section_id)
    projected_refs = [
        source_map.token_for(
            canonical_source_identity(ref) if isinstance(ref, Mapping) else ref,
            slot=(section_id, "source_refs", ordinal),
        )
        for ordinal, ref in enumerate(source_refs, start=1)
    ]
    if section_kind == "sources" and isinstance(payload.get("entries"), list):
        projected["entries"] = [
            _static_source_object(
                entry if isinstance(entry, Mapping) else {},
                source_map=source_map,
                section_id=section_id,
                registry_path="entries",
                ordinal=ordinal,
            )
            for ordinal, entry in enumerate(payload["entries"], start=1)
        ]
    if section_kind == "chart":
        raw_spec = payload.get("spec")
        if isinstance(raw_spec, Mapping):
            projected_spec = projected.get("spec")
            spec = copy.deepcopy(dict(projected_spec)) if isinstance(projected_spec, Mapping) else copy.deepcopy(dict(raw_spec))
            source_meta = raw_spec.get("source_meta")
            if isinstance(source_meta, Mapping):
                spec["source_meta"] = _static_source_object(
                    source_meta,
                    source_map=source_map,
                    section_id=section_id,
                    registry_path="spec.source_meta",
                    ordinal=1,
                )
            if "presentation_plan" in raw_spec:
                from bowhead_core.chart.presentation_plan import validate_projected_notes_plan

                plan = copy.deepcopy(raw_spec["presentation_plan"])
                validate_projected_notes_plan(plan)
                plan["dataset"]["artifact_id"] = source_map.token_for(
                    plan["dataset"]["artifact_id"],
                    slot=(section_id, "spec.presentation_plan.dataset", 1),
                )
                for i, note in enumerate(plan["evidence_notes"], start=1):
                    note["source_ref"] = _static_source_object(
                        note["source_ref"],
                        source_map=source_map,
                        section_id=section_id,
                        registry_path=f"spec.presentation_plan.evidence_notes.{note['id']}.source_ref",
                        ordinal=i,
                    )
                validate_projected_notes_plan(plan, static=True)
                spec["presentation_plan"] = plan
                if plan["dataset"]["availability"] == "unavailable":
                    # Saved rows remain readable. Changed/restricted bytes must
                    # not become a required export asset or complete the chart.
                    spec.pop("data_artifact_id", None)
                    for view in spec.get("views", []):
                        view["spec"].pop("data_artifact_id", None)
            projected["spec"] = spec
        data_source = payload.get("data_source")
        if isinstance(data_source, Mapping):
            projected["data_source"] = _static_source_object(
                data_source,
                source_map=source_map,
                section_id=section_id,
                registry_path="data_source",
                ordinal=1,
            )
    if section_kind == "html_artifact" and "sources" in payload:
        projected["sources"] = list(project_embedded_data_provenance({"sources": payload["sources"]}).source_indexes)
    return projected, projected_refs


def _restore_marker_identities(text: str, alias_map: ViewerSourceAliasMap) -> str:
    def raw(alias: str) -> str:
        identity = alias_map.raw_for_alias(alias)
        if identity is None:
            code = "source_alias_stale" if alias.startswith(SOURCE_ALIAS_PREFIX) else "source_identity_injection"
            raise SourcePatchError(code, "source reference is not valid for the current report revision")
        return identity

    out = _REF_RE.sub(lambda match: f"[ref:{raw(match.group(1))}]", text)
    return _NUM_SRC_RE.sub(lambda match: f"{match.group(1)}{raw(match.group(2))}{match.group(3)}", out)


def bake_unavailable_prose_markers(text: str, access_by_identity: Mapping[str, bool]) -> str:
    """Bake retired syntax while retaining only provenance the viewer can access.

    A viewer-safe no-op PATCH normally restores canonical marker identities before
    persistence.  That exception must apply only to accessible sources; otherwise
    a historical/unresolvable marker would be reintroduced forever on every
    autosave.  Accessible markers are temporarily masked, the shared legacy baker
    handles every other retired syntax (including whitespace semantics), then the
    exact canonical markers are restored.
    """

    protected: dict[str, str] = {}
    counter = 0

    def reserve(match: re.Match[str], identity: str) -> str:
        nonlocal counter
        if not access_by_identity.get(identity, False):
            return match.group(0)
        while True:
            token = f"\ue000bowhead-accessible-source-{counter}\ue001"
            counter += 1
            if token not in text and token not in protected:
                protected[token] = match.group(0)
                return token

    masked = _REF_RE.sub(lambda match: reserve(match, match.group(1)), text)
    masked = _NUM_SRC_RE.sub(lambda match: reserve(match, match.group(2)), masked)
    baked = strip_legacy(masked)
    for token, marker in protected.items():
        baked = baked.replace(token, marker)
    return baked


def _restore_prose_markers(value: Any, alias_map: ViewerSourceAliasMap) -> Any:
    if isinstance(value, dict):
        return {
            key: _restore_marker_identities(child, alias_map)
            if key in CITATION_PROSE_KEYS and isinstance(child, str)
            else _restore_prose_markers(child, alias_map)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_restore_prose_markers(child, alias_map) for child in value]
    return copy.deepcopy(value)


def _validate_alias_value(value: object, alias_map: ViewerSourceAliasMap) -> None:
    if value is None:
        return
    if not isinstance(value, str) or alias_map.raw_for_alias(value) is None:
        code = "source_alias_stale" if isinstance(value, str) and value.startswith(SOURCE_ALIAS_PREFIX) else "source_identity_injection"
        raise SourcePatchError(code, "source identity cannot be written by a viewer")


def _validate_projected_source_object(
    incoming: object,
    current: object,
    *,
    alias_map: ViewerSourceAliasMap,
    section_id: str,
    registry_path: str,
    ordinal: int,
) -> None:
    if not isinstance(incoming, Mapping) or not isinstance(current, Mapping):
        raise SourcePatchError("source_shape_conflict", "source object shape changed")
    expected_alias = alias_map.alias_for(
        canonical_source_identity(current),
        slot=(section_id, registry_path, ordinal),
    )[0]
    supplied = False
    for key in ("id", "source_alias"):
        if key not in incoming:
            continue
        supplied = True
        value = incoming[key]
        if value != expected_alias:
            _validate_alias_value(value, alias_map)
            raise SourcePatchError("source_shape_conflict", "source object identity changed")
    for key in _SOURCE_IDENTITY_KEYS:
        if key in {"id"} or key not in incoming:
            continue
        # Viewer DTOs never expose canonical identity carriers.  Reject them
        # even when their value happens to be a known alias, so they cannot
        # become a second write path for persisted provenance.
        raise SourcePatchError("source_identity_injection", f"viewer patch contains protected source field: {key}")
    if not supplied:
        raise SourcePatchError("source_shape_conflict", "source alias is required")


def merge_viewer_patch_preserving_provenance(
    current_payload: Mapping[str, Any],
    incoming_patch: Mapping[str, Any],
    *,
    section_kind: str,
    section_id: str,
    alias_map: ViewerSourceAliasMap,
    access_by_identity: Mapping[str, bool],
    allow_label_edit: bool,
    evidence_validity_by_digest: Mapping[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Merge normal viewer edits while restoring canonical source provenance."""

    unknown_source_paths = unregistered_source_like_paths(section_kind, incoming_patch)
    if unknown_source_paths:
        path = ".".join(unknown_source_paths[0])
        raise SourcePatchError(
            "source_identity_injection",
            f"viewer patch contains an unregistered source field: {path}",
        )

    patch = _restore_prose_markers(incoming_patch, alias_map)
    merged = {**copy.deepcopy(current_payload), **patch}

    if "source_refs" in incoming_patch:
        incoming_refs = incoming_patch.get("source_refs")
        current_refs = current_payload.get("source_refs")
        if not isinstance(incoming_refs, list) or not isinstance(current_refs, list):
            raise SourcePatchError("source_shape_conflict", "source_refs shape changed")
        if len(incoming_refs) != len(current_refs):
            raise SourcePatchError("source_shape_conflict", "source_refs length changed")
        for ordinal, (incoming, current) in enumerate(zip(incoming_refs, current_refs, strict=True), start=1):
            expected = alias_map.alias_for(current, slot=(section_id, "source_refs", ordinal))[0]
            if incoming != expected:
                _validate_alias_value(incoming, alias_map)
                raise SourcePatchError("source_shape_conflict", "source_refs order changed")
    if "source_refs" in current_payload:
        merged["source_refs"] = copy.deepcopy(current_payload["source_refs"])
    else:
        merged.pop("source_refs", None)

    if section_kind == "sources" and "entries" in incoming_patch:
        current_entries = current_payload.get("entries")
        incoming_entries = incoming_patch.get("entries")
        if not isinstance(current_entries, list) or not isinstance(incoming_entries, list) or len(current_entries) != len(incoming_entries):
            raise SourcePatchError("source_shape_conflict", "source entries length changed")
        restored_entries: list[Any] = []
        for ordinal, (current, incoming) in enumerate(zip(current_entries, incoming_entries, strict=True), start=1):
            if not isinstance(current, Mapping) or not isinstance(incoming, Mapping):
                raise SourcePatchError("source_shape_conflict", "source entry shape changed")
            current_identity = canonical_source_identity(current)
            expected_alias = alias_map.alias_for(
                current_identity,
                slot=(section_id, "entries", ordinal),
            )[0]
            incoming_alias = incoming.get("source_alias", incoming.get("id"))
            if incoming_alias != expected_alias:
                _validate_alias_value(incoming_alias, alias_map)
                raise SourcePatchError("source_shape_conflict", "source entry order changed")
            _validate_projected_source_object(
                incoming,
                current,
                alias_map=alias_map,
                section_id=section_id,
                registry_path="entries",
                ordinal=ordinal,
            )
            restored = copy.deepcopy(dict(current))
            valid_identity = _valid_identity(current_identity)
            if allow_label_edit and valid_identity is not None and access_by_identity.get(valid_identity, False):
                label = incoming.get("safe_label", incoming.get("label"))
                if label is not None:
                    if not isinstance(label, str):
                        raise SourcePatchError("source_shape_conflict", "source label must be text")
                    restored["label"] = label
            restored_entries.append(restored)
        merged["entries"] = restored_entries

    if section_kind == "chart":
        current_spec = current_payload.get("spec")
        if isinstance(current_spec, Mapping):
            merged_spec = merged.get("spec")
            if not isinstance(merged_spec, Mapping):
                raise SourcePatchError("source_shape_conflict", "chart spec shape changed")
            incoming_spec = incoming_patch.get("spec")
            current_plan = current_spec.get("presentation_plan")
            if current_plan is None and isinstance(incoming_spec, Mapping) and "presentation_plan" in incoming_spec:
                raise SourcePatchError("source_identity_injection", "verified chart evidence must be authored by the native tool")
            if current_plan is not None and isinstance(incoming_spec, Mapping):
                _validate_chart_plan_echo(current_spec, incoming_spec, alias_map=alias_map, section_id=section_id)
            if isinstance(incoming_spec, Mapping) and "source_meta" in incoming_spec:
                _validate_projected_source_object(
                    incoming_spec["source_meta"],
                    current_spec.get("source_meta"),
                    alias_map=alias_map,
                    section_id=section_id,
                    registry_path="spec.source_meta",
                    ordinal=1,
                )
            restored_spec = copy.deepcopy(dict(merged_spec))
            if isinstance(current_spec.get("views"), list) and isinstance(incoming_spec, Mapping):
                # Native settings edit display fields inside one complete root.
                # Keep canonical observation/provenance metadata, including fields
                # deliberately absent from the viewer's source-safe projection.
                editable = {"kind", "title", "subtitle", "show_legend", "show_data_labels", "percent_stack"}
                root_editable = editable | {"layout"}
                if "layout" in incoming_spec:
                    from bowhead_core.chart.expression import validate_layout

                    try:
                        validate_layout(incoming_spec["layout"], [view["id"] for view in current_spec["views"]])
                    except ValueError as exc:
                        raise SourcePatchError("source_shape_conflict", "The layout must retain every canonical view exactly once") from exc
                canonical_safe = _redact_unregistered_source_like_fields("chart", {"spec": current_spec})["spec"]
                for key, value in incoming_spec.items():
                    if key in root_editable or key in {"views", "source_meta", "presentation_plan"}:
                        continue
                    if key not in canonical_safe or value != canonical_safe[key]:
                        raise SourcePatchError("source_shape_conflict", "composition data and layout require canonical revalidation")
                incoming_views = incoming_spec.get("views")
                current_views = current_spec["views"]
                safe_views = canonical_safe["views"]
                if not isinstance(incoming_views, list) or len(incoming_views) != len(current_views):
                    raise SourcePatchError("source_shape_conflict", "composition views changed")
                restored_spec = copy.deepcopy(dict(current_spec))
                restored_spec.update({key: value for key, value in incoming_spec.items() if key in root_editable})
                for key in editable - {"kind", "title"} - set(incoming_spec):
                    restored_spec.pop(key, None)
                restored_views: list[dict[str, Any]] = []
                for current_view, safe_view, incoming_view in zip(current_views, safe_views, incoming_views, strict=True):
                    if (
                        not isinstance(incoming_view, Mapping)
                        or set(incoming_view) != {"id", "kind", "spec"}
                        or incoming_view["id"] != current_view["id"]
                        or incoming_view["kind"] != "chart"
                        or not isinstance(incoming_view["spec"], Mapping)
                    ):
                        raise SourcePatchError("source_shape_conflict", "composition view identity changed")
                    for key, value in incoming_view["spec"].items():
                        if key not in editable and (key not in safe_view["spec"] or value != safe_view["spec"][key]):
                            raise SourcePatchError("source_identity_injection", "composition observation metadata cannot be edited by a viewer")
                    restored_child = copy.deepcopy(current_view["spec"])
                    restored_child.update({key: value for key, value in incoming_view["spec"].items() if key in editable})
                    for key in editable - {"kind", "title"} - set(incoming_view["spec"]):
                        restored_child.pop(key, None)
                    restored_views.append({**current_view, "spec": restored_child})
                restored_spec["views"] = restored_views
            if current_plan is not None and isinstance(incoming_spec, Mapping):
                from bowhead_core.chart.presentation_plan import note_text_dependencies

                if not isinstance(current_spec.get("views"), list) and isinstance(incoming_spec, Mapping):
                    editable = {"kind", "title", "subtitle", "show_legend", "show_data_labels", "percent_stack"}
                    safe = _redact_unregistered_source_like_fields("chart", {"spec": current_spec})["spec"]
                    if any(
                        key not in editable | {"source_meta", "presentation_plan"} and (key not in safe or value != safe[key])
                        for key, value in incoming_spec.items()
                    ):
                        raise SourcePatchError("source_identity_injection", "a viewer cannot replace the sealed chart dataset")
                    restored_spec = copy.deepcopy(dict(current_spec))
                    restored_spec.update({key: value for key, value in incoming_spec.items() if key in editable})
                    for key in editable - {"kind", "title"} - set(incoming_spec):
                        restored_spec.pop(key, None)
                restored_spec["presentation_plan"] = copy.deepcopy(current_plan)
                _restore_unavailable_chart_text(restored_spec, current_spec, access_by_identity, evidence_validity_by_digest or {})
                restored_spec["presentation_plan"]["text_dependencies"] = note_text_dependencies(
                    restored_spec, [note["id"] for note in current_plan["evidence_notes"]]
                )
            if "source_meta" in current_spec:
                restored_spec["source_meta"] = copy.deepcopy(current_spec["source_meta"])
            else:
                restored_spec.pop("source_meta", None)
            merged["spec"] = restored_spec
        if "data_source" in incoming_patch:
            _validate_projected_source_object(
                incoming_patch["data_source"],
                current_payload.get("data_source"),
                alias_map=alias_map,
                section_id=section_id,
                registry_path="data_source",
                ordinal=1,
            )
        if "data_source" in current_payload:
            merged["data_source"] = copy.deepcopy(current_payload["data_source"])
        else:
            merged.pop("data_source", None)

    if section_kind == "code_block":
        # Derived on every projection; an echoed copy must never be persisted.
        merged.pop(CODE_BLOCK_OUTPUT_ALIASES_KEY, None)

    if section_kind == "html_artifact":
        if "sources" in incoming_patch:
            expected = list(
                project_embedded_data_provenance({"sources": current_payload.get("sources")}).source_indexes,
            )
            if incoming_patch["sources"] != expected:
                raise SourcePatchError("source_shape_conflict", "embedded source ordinals changed")
        if "sources" in current_payload:
            merged["sources"] = copy.deepcopy(current_payload["sources"])
        else:
            merged.pop("sources", None)

    return merged


def _validate_chart_plan_echo(
    current_spec: Mapping[str, Any], incoming_spec: Mapping[str, Any], *, alias_map: ViewerSourceAliasMap, section_id: str
) -> None:
    from bowhead_core.chart.presentation_plan import validate_stored_notes_plan

    try:
        validate_stored_notes_plan(current_spec)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SourcePatchError("source_shape_conflict", "the saved chart expression requires a compatible editor") from exc
    if "presentation_plan" not in incoming_spec:
        return
    original = current_spec["presentation_plan"]
    incoming = incoming_spec["presentation_plan"]
    if not isinstance(incoming, Mapping) or set(incoming) - set(original) or incoming.get("version") != original["version"]:
        raise SourcePatchError("source_identity_injection", "chart evidence plans cannot be replaced by a viewer")
    if incoming.get("text_dependencies", original["text_dependencies"]) != original["text_dependencies"]:
        raise SourcePatchError("source_identity_injection", "chart text dependencies are server-owned")
    for key in ("view_bindings", "interactions", "scale_groups"):
        if incoming.get(key) != original.get(key):
            raise SourcePatchError("source_identity_injection", "chart linkage targets are server-owned")
    if "table_trends" in original:
        trends = incoming.get("table_trends")
        if not isinstance(trends, list) or len(trends) != len(original["table_trends"]):
            raise SourcePatchError("source_identity_injection", "saved Table trends cannot be replaced")
        for item, canonical in zip(trends, original["table_trends"], strict=True):
            if not isinstance(item, dict) or not (
                item == {"view_id": canonical["view_id"], "availability": "unavailable"}
                or (
                    item.get("availability") in (None, "available")
                    and {key: value for key, value in item.items() if key != "availability"} == canonical
                )
            ):
                raise SourcePatchError("source_identity_injection", "Table trend descriptors are server-owned")
    if "analytical_views" in original:
        trends = incoming.get("analytical_views")
        if not isinstance(trends, list) or len(trends) != len(original["analytical_views"]):
            raise SourcePatchError("source_identity_injection", "saved analytical views cannot be replaced")
        for item, canonical in zip(trends, original["analytical_views"], strict=True):
            if not isinstance(item, dict) or not (
                item == {"view_id": canonical["view_id"], "availability": "unavailable"}
                or (
                    item.get("availability") in (None, "available")
                    and {key: value for key, value in item.items() if key != "availability"} == canonical
                )
            ):
                raise SourcePatchError("source_identity_injection", "Analytical descriptors are server-owned")
    if "annotations" in original:
        annotations = incoming.get("annotations")
        if not isinstance(annotations, list) or len(annotations) != len(original["annotations"]):
            raise SourcePatchError("source_identity_injection", "saved annotations cannot be replaced")
        for item, canonical in zip(annotations, original["annotations"], strict=True):
            if not isinstance(item, dict) or not (
                item == {"id": canonical["id"], "view_id": canonical["view_id"], "availability": "unavailable"}
                or (
                    item.get("availability") in (None, "available")
                    and {key: value for key, value in item.items() if key != "availability"} == canonical
                )
            ):
                raise SourcePatchError("source_identity_injection", "annotation observations and captions are server-owned")
    if "guide_steps" in original:
        steps = incoming.get("guide_steps")
        if not isinstance(steps, list) or len(steps) != len(original["guide_steps"]):
            raise SourcePatchError("source_identity_injection", "saved reading steps cannot be replaced")
        for step, canonical in zip(steps, original["guide_steps"], strict=True):
            if (
                not isinstance(step, dict)
                or set(step) != set(canonical)
                or step["view_id"] != canonical["view_id"]
                or step.get("annotation_id") != canonical.get("annotation_id")
                or step["title"] not in (canonical["title"], "")
            ):
                raise SourcePatchError("source_identity_injection", "saved reading targets and titles cannot be edited by a viewer")
    if "bindings" in original:
        descriptors = incoming.get("bindings")
        if not isinstance(descriptors, Mapping) or set(descriptors) != set(original["bindings"]):
            raise SourcePatchError("source_identity_injection", "chart binding identity is server-owned")
        for bid, descriptor in descriptors.items():
            canonical = original["bindings"][bid]
            if not isinstance(descriptor, Mapping) or (
                dict(descriptor) != {"kind": canonical["kind"], "availability": "unavailable"}
                and {key: value for key, value in descriptor.items() if key != "availability"} != canonical
            ):
                raise SourcePatchError("source_identity_injection", "chart binding descriptors cannot be edited")
    dataset = incoming.get("dataset")
    expected = original["dataset"]
    if not isinstance(dataset, Mapping) or set(dataset) - {*expected, "availability"}:
        raise SourcePatchError("source_identity_injection", "chart dataset identity is server-owned")
    _validate_projected_source_object(
        {"id": dataset.get("artifact_id")},
        {"id": expected["artifact_id"]},
        alias_map=alias_map,
        section_id=section_id,
        registry_path="spec.presentation_plan.dataset",
        ordinal=1,
    )
    for key in ("content_digest", "schema_digest"):
        if key in dataset and dataset[key] != expected[key]:
            raise SourcePatchError("source_identity_injection", "chart dataset digests cannot be edited")
    notes = incoming.get("evidence_notes")
    if not isinstance(notes, list) or len(notes) != len(original["evidence_notes"]):
        raise SourcePatchError("source_shape_conflict", "chart evidence notes changed")
    for i, (note, canonical) in enumerate(zip(notes, original["evidence_notes"], strict=True)):
        if (
            not isinstance(note, Mapping)
            or set(note) - {*canonical, "availability"}
            or note.get("id") != canonical["id"]
            or note.get("measure_keys") != canonical["measure_keys"]
        ):
            raise SourcePatchError("source_identity_injection", "chart evidence identity cannot be edited")
        for key in ("excerpt", "locator", "verified_content_digest"):
            if key in note and note[key] != canonical.get(key):
                raise SourcePatchError("source_identity_injection", "verified chart evidence cannot be edited")
        _validate_projected_source_object(
            note.get("source_ref"),
            canonical["source_ref"],
            alias_map=alias_map,
            section_id=section_id,
            registry_path=f"spec.presentation_plan.evidence_notes.{canonical['id']}.source_ref",
            ordinal=i + 1,
        )


def _restore_unavailable_chart_text(
    spec: dict[str, Any], original: Mapping[str, Any], access: Mapping[str, bool], validity: Mapping[tuple[str, str], bool]
) -> None:
    plan = original["presentation_plan"]
    dataset = plan["dataset"]
    dataset_available = access.get(dataset["artifact_id"], False) and validity.get((dataset["artifact_id"], dataset["content_digest"]), False)
    unavailable = {
        note["id"]
        for note in plan["evidence_notes"]
        if not access.get(note["source_ref"]["artifact_id"], False)
        or not validity.get((note["source_ref"]["artifact_id"], note["verified_content_digest"]), False)
    }
    from bowhead_core.chart.presentation_plan import text_pointer_target

    for pointer, dependency in plan["text_dependencies"].items():
        if dataset_available and not set(dependency["note_ids"]) & unavailable:
            continue
        parts = pointer.lstrip("/").split("/")
        target = text_pointer_target(spec, parts)
        source = text_pointer_target(original, parts)
        target[parts[-1]] = copy.deepcopy(source[parts[-1]])


__all__ = [
    "KB_SOURCE_SAFE_FIELDS",
    "MAX_SOURCE_IDENTITIES",
    "SOURCE_ALIAS_PREFIX",
    "SOURCE_NON_PRESENTATION_PATHS",
    "SOURCE_PRESENTATION_PATHS",
    "SourcePatchError",
    "StaticSafeSourceMap",
    "ViewerSourceAliasMap",
    "canonical_source_identity",
    "collect_source_identities",
    "derive_source_alias_key",
    "generic_source_type",
    "merge_viewer_patch_preserving_provenance",
    "project_sources_for_viewer",
    "project_section_for_static_export",
    "unregistered_source_like_paths",
]
