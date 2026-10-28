"""Request-scoped, viewer-safe Canvas source projection.

This module is the only browser-facing bridge between canonical persisted
provenance and the source presentation DTO.  It deliberately reuses the
existing report artifact closure as the authorization seam; source families
without an authoritative read check remain unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from bowhead_core.citation_marks import CITATION_PROSE_KEYS
from bowhead_core.kb_citations import KB_CHUNK_KIND, parse_kb_citation_identity
from bowhead_core.results_report_relations import project_report_relations
from bowhead_core.source_access_projection import (
    KB_SOURCE_SAFE_FIELDS,
    MAX_SOURCE_IDENTITIES,
    SourcePatchError,
    ViewerSourceAliasMap,
    bake_unavailable_prose_markers,
    canonical_source_identity,
    collect_source_identities,
    merge_viewer_patch_preserving_provenance,
    project_sources_for_viewer,
    unregistered_source_like_paths,
)
from bowhead_core.source_attribution import (
    AttributionStores,
    InFlightIndex,
    KbFileAccess,
    ReportScope,
    attribute_refs,
)
from bowhead_router.inline_reasoning import split_inline_reasoning

from bowhead_gateway.kb_citation_access import kb_file_access_for_request

logger = logging.getLogger(__name__)


_REFERENCE_DISPATCH_MESSAGES = {
    "reference_delegation_role_required": "派发参数不符合本次选择，系统正在调整。",
    "reference_primary_already_dispatched": "本轮已派发指定智能体，不能重复派发。",
}


def _project_legacy_chat_failures(
    events: Sequence[tuple[str, Mapping[str, Any]]],
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep older persisted private model text and raw dispatch errors off browser surfaces."""
    narrative_run: list[int] = []

    def flush_narrative() -> None:
        if not narrative_run:
            return
        original = "".join(str(payloads[index].get("delta") or "") for index in narrative_run)
        visible, _ = split_inline_reasoning(original)
        if visible != original:
            for index in narrative_run:
                payloads[index]["delta"] = ""
            payloads[narrative_run[-1]]["delta"] = visible
        narrative_run.clear()

    for index, (event_type, _) in enumerate(events):
        if event_type == "narrative.delta":
            narrative_run.append(index)
            continue
        flush_narrative()
        payload = payloads[index]
        if event_type not in {"step_done", "tool_result"} or payload.get("tool_name") != "subagent_spawn":
            continue
        raw = " ".join(str(payload.get(key) or "") for key in ("error", "output_preview", "result_preview"))
        error = payload.get("error")
        if isinstance(error, dict):
            raw += " " + str(error.get("message_zh") or "")
        code = next((item for item in _REFERENCE_DISPATCH_MESSAGES if item in raw), None)
        if code is None:
            continue
        message = _REFERENCE_DISPATCH_MESSAGES[code]
        for key in ("output_preview", "result_preview"):
            if isinstance(payload.get(key), str) and code in payload[key]:
                payload[key] = message
        if event_type == "tool_result":
            payload["error"] = message
        else:
            payload["error"] = {"type": "reference_dispatch_blocked", "message_zh": message, "is_transient": False}
    flush_narrative()
    return payloads


class ViewerSourceProjectionError(RuntimeError):
    """The server cannot establish a stable report-scoped projection context."""


def source_attribution_v2_enabled() -> bool:
    """批 2 事件面开关（与 c1 canvas sidecar 同一枚 flag，tech-design §7.1）。

    =0 时事件投影走旧 `_collect_artifact_ids` closure 路（bit-for-bit）；
    灰度一个发布周期后由批 3 删旧分支——不许留成第二套判据（§0.3）。
    """
    return os.environ.get("FEATURE_SOURCE_ATTRIBUTION_V2", "1") == "1"


def source_ref_artifact_ids(refs: Sequence[Mapping[str, Any]] | None) -> frozenset[str]:
    """会话级 source_refs 的 artifact id（含批量子表）。

    ★ 这是归属判定的 **source_refs 臂**（tech-design §3.3 data_ref 谓词第三项）：
    批 1 对拍实测有 6 条 legacy data_ref（created_by_turn=NULL、canvas_id=NULL）
    只靠这条臂闭合——事件面 scope 不喂它们就回归 restricted（§7.2）。
    c1 sidecar 与批 2 事件面共用本收集器（两面同形）。
    """
    ids: set[str] = set()
    for ref in refs or ():
        if not isinstance(ref, Mapping):
            continue
        aid = ref.get("artifact_id")
        if isinstance(aid, str) and aid:
            ids.add(aid)
        for sub in ref.get("sub_sources") or ():
            if isinstance(sub, Mapping):
                sub_aid = sub.get("artifact_id")
                if isinstance(sub_aid, str) and sub_aid:
                    ids.add(sub_aid)
    return frozenset(ids)


# ---------------------------------------------------------------------------
# source-accessibility §1.6 consumer c1: Verdict → viewer-safe citation row.
# Single mapping point: canvas-detail turnCitations sidecars and event projections (after batch 2's switch)
# must both use this; one set of criteria (tech-design §0.3) extends to one state mapping too.
# Six-state contract: tech-design §4.1. Non-OWNED releases no safe fields (same security boundary as batch 1 gate B).

_CITATION_STATE_BY_ATTRIBUTION: Mapping[Any, str] = {
    "foreign": "linked_restricted",
    "missing": "linked_missing",
    "unsupported": "unsupported",
}


_KB_REF_KEYS = ("kb_file_id", "kb_chunk_id", "kb_scope", "kb_space_id")


def citation_presentation(
    *,
    seq: int,
    kind: str,
    label: str | None,
    verdict: Any,
    rendered_artifact_ids: Container[str],
) -> dict[str, Any]:
    """One attribution Verdict → one viewer-safe citation row.

    - ``linked_accessible``：OWNED 且字节出口候选真的在 rendered closure 里
      （出口面语义与 ``/by-session/{sid}/artifacts/{aid}`` 的 closure 同源）；
    - ``linked_labeled``：OWNED 但没有出口（区块引用 / kb_row / 不在 closure 的产物）
      —— 有名字、可锚定，但取不到字节；
    - 其余按 Attribution 三态映射，label / anchor 一律不下发。
    """
    row: dict[str, Any] = {"seq": seq, "kind": kind}
    attribution = getattr(getattr(verdict, "attribution", None), "value", None)
    if attribution == "owned":
        exit_candidate = getattr(verdict, "exit_candidate", None)
        exit_ok = isinstance(exit_candidate, str) and exit_candidate in rendered_artifact_ids
        row["access_state"] = "linked_accessible" if exit_ok else "linked_labeled"
        if isinstance(label, str) and label:
            row["safe_label"] = label
        display = getattr(verdict, "display", None)
        anchor = getattr(display, "anchor_id", None) if display is not None else None
        if isinstance(anchor, str) and anchor:
            row["safe_anchor_id"] = anchor
        kb_ref = getattr(display, "kb_ref", None) if display is not None else None
        if isinstance(kb_ref, Mapping) and kb_ref.get("kb_file_id"):
            # KB snippets: viewable iff viewers can read the file (resolver already checked); opening uses KB's own gate.
            # Use current filenames as labels, avoiding old names after renames.
            row["access_state"] = "linked_accessible"
            if isinstance(display.label, str) and display.label:
                row["safe_label"] = display.label
            for key in _KB_REF_KEYS:
                val = kb_ref.get(key)
                if isinstance(val, str) and val:
                    row[key] = val
    else:
        # Unknown/anomalous attribution also fails closed to missing, never to any OWNED state.
        row["access_state"] = _CITATION_STATE_BY_ATTRIBUTION.get(attribution, "linked_missing")
    return row


_EVENT_SECTION_CARRIERS = {
    "canvas.section_added": "section",
    "canvas.section_patched": "patch",
    "canvas.section_done": "payload",
    "canvas.section_streaming": "partial_payload",
}
_ARTIFACT_SCALAR_KEYS = frozenset(
    {"artifact_id", "data_artifact_id", "flattened_artifact_id", "manifest_artifact_id"},
)
_ARTIFACT_ARRAY_KEYS = frozenset({"artifact_ids", "input_artifact_ids", "output_artifact_ids"})


def _artifact_ids_for_source(source: Mapping[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.startswith("art_") and value not in ids:
            ids.append(value)

    identity = canonical_source_identity(source)
    add(identity)
    add(source.get("artifact_id"))
    for child in source.get("sub_sources") or ():
        if isinstance(child, Mapping):
            add(child.get("artifact_id"))
    return tuple(ids)


def _source_objects(
    sections: Sequence[Mapping[str, Any]],
    source_refs: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    out = list(source_refs)
    for section in sections:
        if section.get("kind") == "sources":
            for entry in section.get("entries") or ():
                if isinstance(entry, Mapping):
                    out.append(entry)
        if section.get("kind") == "chart":
            spec = section.get("spec")
            if isinstance(spec, Mapping) and isinstance(spec.get("source_meta"), Mapping):
                out.append(spec["source_meta"])
            plan = spec.get("presentation_plan") if isinstance(spec, Mapping) else None
            notes = plan.get("evidence_notes") if isinstance(plan, Mapping) else None
            if isinstance(notes, list):
                out.extend(note["source_ref"] for note in notes[:3] if isinstance(note, Mapping) and isinstance(note.get("source_ref"), Mapping))
            if isinstance(section.get("data_source"), Mapping):
                out.append(section["data_source"])
    return out


def _collect_artifact_ids(value: Any, out: list[str], seen: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _ARTIFACT_SCALAR_KEYS and isinstance(child, str) and child and child not in seen:
                seen.add(child)
                out.append(child)
            elif key in _ARTIFACT_ARRAY_KEYS and isinstance(child, list):
                for item in child:
                    if isinstance(item, str) and item and item not in seen:
                        seen.add(item)
                        out.append(item)
            _collect_artifact_ids(child, out, seen)
    elif isinstance(value, list):
        for child in value:
            _collect_artifact_ids(child, out, seen)


def _infer_section_kind(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("entries"), list):
        return "sources"
    spec = payload.get("spec")
    if (isinstance(spec, Mapping) and ("source_meta" in spec or "presentation_plan" in spec)) or "data_source" in payload:
        return "chart"
    return "narrative"


def _contains_source_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in CITATION_PROSE_KEYS and isinstance(child, str):
                if "[ref:" in child or ":src:" in child:
                    return True
            elif _contains_source_marker(child):
                return True
    elif isinstance(value, list):
        return any(_contains_source_marker(child) for child in value)
    return False


def _section_has_source_presentation(section: Mapping[str, Any]) -> bool:
    """Cheaply identify the exact event payloads that require source I/O."""

    kind = str(section.get("kind") or _infer_section_kind(section))
    if isinstance(section.get("source_refs"), list):
        return True
    if kind == "sources" and isinstance(section.get("entries"), list):
        return True
    if kind == "chart":
        spec = section.get("spec")
        if (isinstance(spec, Mapping) and ("source_meta" in spec or "presentation_plan" in spec)) or "data_source" in section:
            return True
    if kind == "html_artifact" and "sources" in section:
        return True
    # Code blocks gain viewer output aliases in the projection; live events must take the same path
    # as the canvas detail, or the live section would lack them until a refresh.
    if kind == "code_block" and isinstance(section.get("output_artifact_ids"), list):
        return True
    return bool(unregistered_source_like_paths(kind, section)) or _contains_source_marker(section)


def _event_section(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    turn_id: str,
    ordinal: int,
) -> tuple[dict[str, Any] | None, str | None, set[str]]:
    absent: set[str] = set()
    carrier = _EVENT_SECTION_CARRIERS.get(event_type)
    if carrier is not None:
        raw = payload.get(carrier)
        if not isinstance(raw, Mapping):
            return None, None, absent
        section = copy.deepcopy(dict(raw))
        if "id" not in section:
            absent.add("id")
            section["id"] = str(payload.get("section_id") or f"event-section-{ordinal}")
        if "kind" not in section:
            absent.add("kind")
            section["kind"] = str(payload.get("kind") or _infer_section_kind(section))
        if not _section_has_source_presentation(section):
            return None, None, set()
        return section, carrier, absent
    if event_type == "canvas.section_chunk" and isinstance(payload.get("delta"), str):
        if "[ref:" not in payload["delta"] and ":src:" not in payload["delta"]:
            return None, None, absent
        return (
            {
                "id": str(payload.get("section_id") or f"event-section-{ordinal}"),
                "kind": "narrative",
                "markdown": payload["delta"],
            },
            "delta",
            {"id", "kind", "markdown"},
        )
    if event_type == "turn.citation_registered" and isinstance(payload.get("artifact_id"), str):
        citation_seq = payload.get("citation_seq")
        citation_slot = citation_seq if isinstance(citation_seq, int) and citation_seq > 0 else ordinal
        return (
            {
                "id": f"event-citation-{turn_id}-{citation_slot}",
                "kind": "sources",
                "entries": [
                    {
                        "artifact_id": payload["artifact_id"],
                        "label": payload.get("label"),
                    },
                ],
            },
            "citation",
            {"id", "kind"},
        )
    if event_type == "lead.final.payload" and isinstance(payload.get("sources"), list) and payload["sources"]:
        return (
            {
                # The event is emit-once per turn. Including the turn ID keeps
                # invalid-source-slot aliases stable across live, paged replay,
                # and F5 instead of depending on a page-local ordinal.
                "id": f"event-lead-final-sources-{turn_id}",
                "kind": "sources",
                "entries": copy.deepcopy(payload["sources"]),
            },
            "lead_sources",
            {"id", "kind"},
        )
    return None, None, absent


def _event_sections(
    turn_id: str,
    events: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    event_ordinals: Sequence[int] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any] | None, str | None, set[str]]]]:
    if event_ordinals is not None and len(event_ordinals) != len(events):
        raise ValueError("event projection ordinal count mismatch")
    sections: list[dict[str, Any]] = []
    descriptors: list[tuple[dict[str, Any] | None, str | None, set[str]]] = []
    for position, (event_type, payload) in enumerate(events, start=1):
        ordinal = event_ordinals[position - 1] if event_ordinals is not None else position
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise ValueError("event projection ordinal must be positive")
        descriptor = _event_section(event_type, payload, turn_id=turn_id, ordinal=ordinal)
        descriptors.append(descriptor)
        if descriptor[0] is not None:
            sections.append(descriptor[0])
    return sections, descriptors


async def _turn_owned_artifact_ids(store: Any, artifact_ids: Sequence[str], turn_id: str) -> set[str]:
    """Keep only event artifacts that were created by the requested turn.

    The artifact closure is a tenant-isolation boundary: bulk artifact lookup does
    not independently filter by session or space. Event payloads can contain
    model-authored references, so their artifact IDs require the server-written
    created_by_turn field to match. The caller has already authorized the turn.
    """
    if not artifact_ids or store is None or not turn_id:
        return set()
    get_many = getattr(store, "get_many", None)
    if not callable(get_many):
        # Older stores/test doubles without batch interfaces **do not permit access** (fail-closed); prefer stripping
        # labels to widening closure without attribution evidence.
        return set()
    try:
        artifacts = await get_many(list(dict.fromkeys(artifact_ids)))
    except Exception:
        return set()
    return {art.id for art in artifacts if getattr(art, "id", None) and getattr(art, "created_by_turn", None) == turn_id}


async def _existing_artifact_ids(store: Any, artifact_ids: Sequence[str]) -> set[str]:
    if not artifact_ids or store is None:
        return set()
    get_many = getattr(store, "get_many", None)
    if callable(get_many):
        try:
            artifacts = await get_many(list(artifact_ids))
        except Exception:
            return set()
        return {art.id for art in artifacts if getattr(art, "id", None) and getattr(art, "status", None) != "missing"}

    # Test doubles and older store implementations may only expose get().  Keep
    # this compatibility path bounded rather than issuing an unbounded gather.
    get_one = getattr(store, "get", None)
    if not callable(get_one):
        return set()
    semaphore = asyncio.Semaphore(16)

    async def resolve(artifact_id: str) -> Any:
        async with semaphore:
            return await get_one(artifact_id)

    results = await asyncio.gather(*(resolve(artifact_id) for artifact_id in artifact_ids), return_exceptions=True)
    return {
        art.id for art in results if not isinstance(art, BaseException) and getattr(art, "id", None) and getattr(art, "status", None) != "missing"
    }


_KB_CHUNK_PREFIX = "chunk_"
KbFileAccessCheck = Callable[[str], Awaitable[KbFileAccess]]


def _kb_targets(identities: Sequence[str], source_refs: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str] | None]:
    """报告里的知识库来源身份 → (file_id, chunk_id)；认不出文件的片段 → None（判失效）。

    两种身份：对话登记的 ``kb:<file_id>:<chunk_id>``；画布来源段 / 会话 source_refs 里的裸
    ``chunk_*``。裸片段的所属文件只认**服务端**聚合的会话 source_refs（本会话 ``kb_search``
    真实返回的片段，来自 turn log），不采信条目自身写的 ``kb_file_id``——来源段条目可能出自
    peer 自报。
    """
    chunk_files: dict[str, str] = {}
    for ref in source_refs:
        if not isinstance(ref, Mapping):
            continue
        chunk_id, file_id = ref.get("kb_chunk_id"), ref.get("kb_file_id")
        if isinstance(chunk_id, str) and chunk_id and isinstance(file_id, str) and file_id:
            chunk_files.setdefault(chunk_id, file_id)
    out: dict[str, tuple[str, str] | None] = {}
    for identity in identities:
        parsed = parse_kb_citation_identity(identity)
        if parsed is not None:
            out[identity] = parsed
        elif identity.startswith(_KB_CHUNK_PREFIX):
            file_id = chunk_files.get(identity)
            out[identity] = (file_id, identity) if file_id else None
    return out


async def _kb_presentations(
    targets: Mapping[str, tuple[str, str] | None],
    kb_file_access: KbFileAccessCheck | None,
) -> dict[str, dict[str, str]]:
    """按看的人的知识库读权限给每个知识库身份出态与可下发字段（与对话 `_KbChunkResolver` 同一判定）。

    没有看的人 / 判定异常 ⇒ 受限；文件不存在或已删、片段认不出文件 ⇒ 失效。同一文件只判一次，
    每次投影实查不缓存（撤权即时生效）。
    """
    if not targets:
        return {}
    file_ids = sorted({target[0] for target in targets.values() if target is not None})
    access_by_file: dict[str, KbFileAccess | None] = {}
    if kb_file_access is not None and file_ids:
        semaphore = asyncio.Semaphore(8)

        async def judge(file_id: str) -> KbFileAccess:
            async with semaphore:
                return await kb_file_access(file_id)

        results = await asyncio.gather(*(judge(file_id) for file_id in file_ids), return_exceptions=True)
        for file_id, result in zip(file_ids, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("canvas_kb_source_access_failed file=%s", file_id)
                access_by_file[file_id] = None
            else:
                access_by_file[file_id] = result
    out: dict[str, dict[str, str]] = {}
    for identity, target in targets.items():
        if target is None:
            out[identity] = {"access_state": "linked_missing"}
            continue
        file_id, chunk_id = target
        access = access_by_file.get(file_id)
        state = getattr(access, "state", None)
        if state == "missing":
            out[identity] = {"access_state": "linked_missing"}
        elif state == "readable" and access is not None:
            row = {"access_state": "linked_accessible", "kb_file_id": file_id, "kb_chunk_id": chunk_id}
            if access.file_name:
                row["safe_label"] = access.file_name
            if access.scope:
                row["kb_scope"] = access.scope
            if access.space_id:
                row["kb_space_id"] = access.space_id
            out[identity] = row
        else:
            out[identity] = {"access_state": "linked_restricted"}
    return out


async def _source_refs_for_session(request: Any, session_id: str) -> list[dict[str, Any]]:
    session_store = getattr(request.app.state, "session_store", None)
    turn_log_store = getattr(request.app.state, "turn_log_store", None)
    if session_store is None or turn_log_store is None:
        return []
    try:
        from bowhead_core.source_ref_aggregator import aggregate_source_refs_for_session

        return await aggregate_source_refs_for_session(
            session_id,
            session_store=session_store,
            turn_log_store=turn_log_store,
        )
    except Exception:
        # Source detail is optional presentation. Resolver failure is fail-closed
        # and must not prevent the report/event stream itself from loading.
        return []


@dataclass(slots=True)
class ViewerSafeCanvasProjector:
    """A single request/revision projection and provenance merge context."""

    alias_map: ViewerSourceAliasMap
    access_by_identity: dict[str, bool]
    artifact_by_identity: dict[str, str]
    session_id: str
    kb_by_identity: dict[str, dict[str, str]] = field(default_factory=dict)
    evidence_validity_by_digest: dict[tuple[str, str], bool] = field(default_factory=dict)

    @classmethod
    async def build(
        cls,
        *,
        root_secret: str | bytes | None,
        workspace_id: str,
        session_id: str,
        sections: Sequence[Mapping[str, Any]],
        source_refs: Sequence[Mapping[str, Any]],
        report_artifact_closure: Sequence[str],
        artifact_store: Any,
        kb_file_access: KbFileAccessCheck | None = None,
    ) -> ViewerSafeCanvasProjector:
        """``kb_file_access``：已绑定看的人的知识库读权限判定（`kb_file_access_for_request`）；
        None ⇒ 知识库来源一律受限（fail-closed）。非知识库来源不受影响。"""
        if not workspace_id or not session_id:
            raise ViewerSourceProjectionError("source projection context unavailable")
        identities = collect_source_identities(sections, source_refs)
        if not root_secret and identities:
            raise ViewerSourceProjectionError("source projection context unavailable")
        # A source-free report has no alias to sign. This keeps unrelated Canvas
        # reads available during partial test/bootstrap setup without weakening
        # the fail-closed rule for any source-bearing response.
        aliases = ViewerSourceAliasMap.from_root_secret(
            root_secret or b"source-free-report",
            workspace_id=workspace_id,
            report_scope_id=session_id,
        )
        resolvable = set(identities[:MAX_SOURCE_IDENTITIES])
        closure = set(report_artifact_closure)
        backing_by_identity: dict[str, tuple[str, ...]] = {}
        for source in _source_objects(sections, source_refs):
            identity = canonical_source_identity(source)
            if not isinstance(identity, str) or identity not in resolvable:
                continue
            backing = _artifact_ids_for_source(source)
            if backing:
                backing_by_identity.setdefault(identity, backing)
        for identity in resolvable:
            if identity.startswith("art_"):
                backing_by_identity.setdefault(identity, (identity,))

        candidates = sorted(
            {artifact_id for backing in backing_by_identity.values() for artifact_id in backing if artifact_id in closure},
        )
        existing = await _existing_artifact_ids(artifact_store, candidates)
        access_by_identity = dict.fromkeys(identities, False)
        artifact_by_identity: dict[str, str] = {}
        for identity, backing in backing_by_identity.items():
            if backing and all(artifact_id in closure and artifact_id in existing for artifact_id in backing):
                access_by_identity[identity] = True
                if len(backing) == 1:
                    artifact_by_identity[identity] = backing[0]

        kb_by_identity = await _kb_presentations(
            _kb_targets([identity for identity in identities if identity in resolvable], source_refs),
            kb_file_access,
        )
        for identity, presentation in kb_by_identity.items():
            # KB sources bypass artifact closure: viewing depends only on viewers' KB read access, with no byte exit.
            access_by_identity[identity] = presentation["access_state"] == "linked_accessible"
            artifact_by_identity.pop(identity, None)

        from bowhead_core.chart.presentation_plan import verify_saved_plan_evidence

        evidence_validity = await verify_saved_plan_evidence(sections, artifact_store, access_by_identity)

        return cls(
            alias_map=aliases,
            access_by_identity=access_by_identity,
            artifact_by_identity=artifact_by_identity,
            session_id=session_id,
            kb_by_identity=kb_by_identity,
            evidence_validity_by_digest=evidence_validity,
        )

    def _safe_links(self, identity: str) -> Mapping[str, str]:
        artifact_id = self.artifact_by_identity.get(identity)
        if artifact_id is None:
            return {}
        session = quote(self.session_id, safe="")
        artifact = quote(artifact_id, safe="")
        base = f"/api/v1/canvas/by-session/{session}/artifacts/{artifact}"
        return {"safe_url": base, "safe_preview_url": f"{base}/preview"}

    def project(
        self,
        sections: Sequence[Mapping[str, Any]],
        source_refs: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return project_sources_for_viewer(
            sections,
            source_refs,
            alias_map=self.alias_map,
            access_by_identity=self.access_by_identity,
            safe_link_builder=self._safe_links,
            kb_presentation_by_identity=self.kb_by_identity,
            evidence_validity_by_digest=self.evidence_validity_by_digest,
        )

    def merge_patch(
        self,
        current_payload: Mapping[str, Any],
        incoming_patch: Mapping[str, Any],
        *,
        section_kind: str,
        section_id: str,
        allow_label_edit: bool,
    ) -> dict[str, Any]:
        return merge_viewer_patch_preserving_provenance(
            current_payload,
            self._without_kb_display_fields(incoming_patch, section_kind),
            section_kind=section_kind,
            section_id=section_id,
            alias_map=self.alias_map,
            access_by_identity=self.access_by_identity,
            allow_label_edit=allow_label_edit,
            evidence_validity_by_digest=self.evidence_validity_by_digest,
        )

    def _without_kb_display_fields(self, incoming_patch: Mapping[str, Any], section_kind: str) -> Mapping[str, Any]:
        """viewer 回传的知识库条目带着本投影下发的 kb_file_id / kb_chunk_id 等展示字段；它们与
        canonical 身份同名，原样进合并会被当成身份注入拒掉（一次无改动保存就 409）。这些字段
        本来就不写回（合并从 current 还原整条），只对**本投影判为知识库的别名**剥掉。
        `safe_label` 一并剥：它是文件**当前**名，不是条目自己的 label，回写会把文件名存进报告。"""
        entries = incoming_patch.get("entries") if section_kind == "sources" else None
        if not self.kb_by_identity or not isinstance(entries, list):
            return incoming_patch
        cleaned: list[Any] = []
        for entry in entries:
            if isinstance(entry, Mapping):
                alias = entry.get("source_alias", entry.get("id"))
                if self.alias_map.raw_for_alias(alias) in self.kb_by_identity:
                    entry = {key: value for key, value in entry.items() if key not in KB_SOURCE_SAFE_FIELDS}
            cleaned.append(entry)
        return {**incoming_patch, "entries": cleaned}

    def normalize_unchanged_prose(self, text: str) -> str:
        return bake_unavailable_prose_markers(text, self.access_by_identity)


@dataclass(slots=True)
class EventAttributionContext:
    """批 2：一次事件投影的归属判定上下文（scope + 权威库句柄 + rendered）。

    rendered = `project_report_relations(persisted).closure_ids` ∪ source_refs
    artifact id —— 与字节出口端点同源（`linked_accessible` 的判定不许有第二套
    closure，tech-design §3.1）。
    """

    scope: ReportScope
    stores: AttributionStores
    rendered: frozenset[str]


@dataclass(slots=True)
class LiveEventProjectionCache:
    """chat.py live SSE 每流一个：turn-scoped 复用昂贵投影状态（tech-design §3.4-3）。

    live 路径逐事件调 `project_live_event_payload_for_viewer`，每次全量重建
    projector（canvas 状态读 + source_refs 聚合）。报告轮 pre-done 一次 flush
    几十上百条 citation 事件，逐条全量重建会把首响吃掉。缓存规则：
    · `canvas.section_*` 事件 ⇒ 画布状态变了 ⇒ 失效缓存（并把服务端铸的
      section id 记进 in-flight 索引，§3.4-1）；
    · citation 事件且缓存可用 ⇒ 快路：复用缓存 projector（alias 是按 identity
      的无状态 HMAC，可安全复用）+ 缓存 scope 跑单条 attribute_refs；
    · 其余源承载事件走慢路，慢路完成后回填缓存。
    """

    turn_id: str | None = None
    projector: ViewerSafeCanvasProjector | None = None
    attribution: EventAttributionContext | None = None
    in_flight_section_ids: set[str] = field(default_factory=set)

    def reset_for_turn(self, turn_id: str) -> None:
        if self.turn_id != turn_id:
            self.turn_id = turn_id
            self.projector = None
            self.attribution = None
            self.in_flight_section_ids = set()

    def invalidate_projection(self) -> None:
        self.projector = None
        self.attribution = None


def _inflight_section_id(event_type: str, payload: Mapping[str, Any]) -> str | None:
    """事件载荷里**服务端铸的**画布区块 id（sec_*）。只认服务端字段，不收模型正文。"""
    if event_type not in _EVENT_SECTION_CARRIERS:
        return None
    sid = payload.get("section_id")
    if not isinstance(sid, str):
        raw = payload.get(_EVENT_SECTION_CARRIERS[event_type])
        sid = raw.get("id") if isinstance(raw, Mapping) else None
    return sid if isinstance(sid, str) and sid.startswith("sec_") else None


async def _event_projector(
    request: Any,
    turn_id: str,
    event_sections: list[dict[str, Any]],
    *,
    want_attribution: bool = False,
) -> tuple[ViewerSafeCanvasProjector, EventAttributionContext | None]:
    session_store = getattr(request.app.state, "session_store", None)
    canvas_store = getattr(request.app.state, "canvas_store", None)
    if session_store is None or canvas_store is None:
        raise ViewerSourceProjectionError("event source projection stores unavailable")
    turn = await session_store.get_turn(turn_id)
    if turn is None:
        raise ViewerSourceProjectionError("event source projection turn unavailable")
    session = await session_store.get_session(turn.session_id)
    if session is None:
        raise ViewerSourceProjectionError("event source projection session unavailable")
    canvas = await canvas_store.get_canvas_by_session(session.id)
    persisted_sections: list[Any] = []
    canonical_sections: list[dict[str, Any]] = []
    if canvas is not None:
        state = await canvas_store.read_canvas_projection_state(canvas.id)
        if state is not None:
            _, persisted_sections = state
            canonical_sections = [section.to_report_section() for section in persisted_sections]
    all_sections = [*canonical_sections, *event_sections]
    source_refs = await _source_refs_for_session(request, session.id)
    closure: list[str] = []
    _collect_artifact_ids(canonical_sections, closure, set())
    _collect_artifact_ids(source_refs, closure, set(closure))
    # Chat citations may exist only in turn events. Include their artifacts only
    # after checking ownership: the closure is a tenant-isolation boundary, and
    # model-injected references to artifacts from another turn must stay stripped.
    event_artifact_ids: list[str] = []
    _collect_artifact_ids(event_sections, event_artifact_ids, set())
    owned_event_ids: set[str] = set()
    if event_artifact_ids:
        owned_event_ids = await _turn_owned_artifact_ids(
            getattr(request.app.state, "artifact_store", None),
            event_artifact_ids,
            turn_id,
        )
        seen = set(closure)
        closure.extend(artifact_id for artifact_id in event_artifact_ids if artifact_id in owned_event_ids and artifact_id not in seen)
    projector = await ViewerSafeCanvasProjector.build(
        root_secret=getattr(request.app.state, "artifact_fetch_secret", None),
        workspace_id=session.workspace_id or "",
        session_id=session.id,
        sections=all_sections,
        source_refs=source_refs,
        report_artifact_closure=closure,
        artifact_store=getattr(request.app.state, "artifact_store", None),
        kb_file_access=kb_file_access_for_request(request),
    )
    attribution_ctx: EventAttributionContext | None = None
    if want_attribution:
        # rendered = exit-surface closure (typed extractor + source_refs), shared with
        # /by-session/{sid}/artifacts authorization. Old _collect_artifact_ids closure
        # feeds only the legacy access map above, no longer this exit decision.
        rendered_ids: list[str] = list(project_report_relations(persisted_sections).closure_ids)
        ref_ids = source_ref_artifact_ids(source_refs)
        seen_rendered = set(rendered_ids)
        rendered_ids.extend(aid for aid in sorted(ref_ids) if aid not in seen_rendered)
        # Preserve byte access for event artifacts owned by this turn. Ownership
        # is server-written; omitting this set would incorrectly downgrade
        # accessible sandbox outputs to labels without accessible content.
        seen_rendered.update(rendered_ids)
        rendered_ids.extend(aid for aid in sorted(owned_event_ids) if aid not in seen_rendered)
        session_turns: frozenset[str] = frozenset()
        with contextlib.suppress(Exception):  # Older stores/test doubles lack list_turns: degrade this branch.
            session_turns = frozenset(t.id for t in await session_store.list_turns(session_id=session.id) if getattr(t, "id", None))
        scope = ReportScope(
            workspace_id=session.workspace_id or "",
            session_id=session.id,
            canvas_id=getattr(canvas, "id", None),
            turn_id=turn_id,
            session_turn_ids=session_turns,
            source_ref_artifact_ids=ref_ids,
            rendered_artifact_ids=frozenset(rendered_ids),
        )
        stores = AttributionStores(
            artifact_store=getattr(request.app.state, "artifact_store", None),
            canvas_store=canvas_store,
            kb_rows_store=getattr(request.app.state, "kb_extracted_rows_store", None),
            kb_file_access=kb_file_access_for_request(request),
        )
        attribution_ctx = EventAttributionContext(
            scope=scope,
            stores=stores,
            rendered=frozenset(rendered_ids),
        )
    return projector, attribution_ctx


def _apply_v2_citation_row(
    projected_payload: dict[str, Any],
    *,
    payload: Mapping[str, Any],
    entry: Mapping[str, Any],
    kind: str,
    verdict: Any,
    rendered: Container[str],
) -> None:
    """批 2 citation 载荷六态铺展的**唯一落笔点**（review F10）。

    慢路（分页/重放/live 慢路）与 live 快路都必须走这里——两份手写已经在
    review F1 里漂移过一次（快路冻结 rendered）。任何新 safe_* 字段 / label
    规则改动只改这一处，live 与 replay 天然同形。
    """
    raw_seq = payload.get("citation_seq")
    raw_label = payload.get("label") if isinstance(payload.get("label"), str) else None
    row = citation_presentation(
        seq=raw_seq if isinstance(raw_seq, int) and raw_seq > 0 else 0,
        kind=kind,
        label=raw_label,
        verdict=verdict,
        rendered_artifact_ids=rendered,
    )
    projected_payload["access_state"] = row["access_state"]
    if "safe_label" in row:
        projected_payload["label"] = row["safe_label"]
        projected_payload["safe_label"] = row["safe_label"]
    else:
        projected_payload.pop("label", None)
    if "safe_anchor_id" in row:
        projected_payload["safe_anchor_id"] = row["safe_anchor_id"]
    for key in _KB_REF_KEYS:
        if key in row:
            projected_payload[key] = row[key]
    if not row.get("kb_file_id"):
        projected_payload.pop("kb_generation_id", None)
        projected_payload.pop("kb_source_check_state", None)
    projected_payload["generic_type"] = entry.get("generic_type", "other_source")
    with contextlib.suppress(Exception):  # Gate C: metric failures never affect projection.
        from bowhead_core.metrics import inc as _metric_inc

        _metric_inc(
            "source_attribution.decision",
            labels={
                "kind": kind,
                "state": row["access_state"],
                "reason": str(getattr(verdict, "reason_code", "") or "none"),
            },
        )


async def project_event_payloads_for_viewer(
    request: Any,
    turn_id: str,
    events: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    event_ordinals: Sequence[int] | None = None,
    _live_cache: LiveEventProjectionCache | None = None,
    _extra_in_flight: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Project a persisted event page without changing event ordering or shape."""
    if any(event_type == "document.draft_open" for event_type, _ in events):
        from bowhead_gateway.document_canvas_projection import project_document_draft_open

        events = [
            (event_type, await project_document_draft_open(request, turn_id, payload) if event_type == "document.draft_open" else payload)
            for event_type, payload in events
        ]
    event_sections, descriptors = _event_sections(
        turn_id,
        events,
        event_ordinals=event_ordinals,
    )
    inline_charts: list[tuple[int, int, dict[str, Any]]] = []
    for event_index, (event_type, payload) in enumerate(events):
        artifacts = payload.get("inline_artifacts")
        if event_type != "lead.final.payload" or not isinstance(artifacts, list):
            continue
        for artifact_index, artifact in enumerate(artifacts):
            if not isinstance(artifact, Mapping) or artifact.get("kind") != "chart" or not isinstance(artifact.get("spec"), Mapping):
                continue
            section = {
                "id": artifact.get("id") or f"event-inline-chart-{turn_id}-{artifact_index}",
                "kind": "chart",
                "spec": copy.deepcopy(artifact["spec"]),
            }
            if _section_has_source_presentation(section):
                inline_charts.append((event_index, artifact_index, section))
    event_sections.extend(section for _, _, section in inline_charts)
    if not event_sections:
        return _project_legacy_chat_failures(events, [copy.deepcopy(dict(payload)) for _, payload in events])
    # ── Batch 2: citation payloads use batch 1 attribution (flag-gated, tech-design §9-2) ──
    flag_on = source_attribution_v2_enabled()
    citation_refs: list[tuple[str, str]] = []
    inflight_ids: set[str] = set(_extra_in_flight or ())
    for (event_type, payload), (_section, carrier, _absent) in zip(events, descriptors, strict=True):
        if carrier == "citation" and isinstance(payload.get("artifact_id"), str):
            kind = str(payload.get("kind") or "data_ref")
            # KB snippets ignore V2: their criterion (viewer KB read access) is unrelated to legacy closure, which always
            # marks them inaccessible (dependency removed when KB-answer citations shipped). Other kinds still branch on V2.
            if flag_on or kind == KB_CHUNK_KIND:
                citation_refs.append((kind, payload["artifact_id"]))
        if flag_on:
            sid = _inflight_section_id(event_type, payload)
            if sid:
                inflight_ids.add(sid)
    want_attribution = bool(citation_refs) or (flag_on and _live_cache is not None)
    projector, attribution_ctx = await _event_projector(
        request,
        turn_id,
        event_sections,
        want_attribution=want_attribution,
    )
    if _live_cache is not None:
        # Align turn ownership first: this page function (reconnect replay batch) is often the cache's **first writer**,
        # with cache.turn_id still None. Without setting turn_id, the next live event's wrapper calls reset_for_turn,
        # erasing the just-warmed cache (caught immediately by F6 tests).
        _live_cache.reset_for_turn(turn_id)
        _live_cache.projector = projector
        _live_cache.attribution = attribution_ctx
        # review F6: in-flight section ids appearing in reconnect replay batches must enter the turn-scoped
        # index too, or later live citations on reconnected clients mark unpersisted sections missing
        # (original client labeled / persisted replay labeled, violating three-state consistency).
        _live_cache.in_flight_section_ids |= inflight_ids
    verdicts: dict[tuple[str, str], Any] = {}
    if attribution_ctx is not None and citation_refs:
        verdicts = await attribute_refs(
            citation_refs,
            scope=attribution_ctx.scope,
            stores=attribution_ctx.stores,
            in_flight=InFlightIndex(frozenset(inflight_ids)),
        )
    projected_sections, _ = projector.project(event_sections)
    projected_iter = iter(projected_sections)
    out: list[dict[str, Any]] = []
    for (_, payload), (section, carrier, absent) in zip(events, descriptors, strict=True):
        projected_payload = copy.deepcopy(dict(payload))
        if section is None or carrier is None:
            out.append(projected_payload)
            continue
        safe_section = next(projected_iter)
        if carrier == "delta":
            projected_payload["delta"] = safe_section.get("markdown", "")
        elif carrier == "lead_sources":
            projected_payload["sources"] = safe_section.get("entries", [])
        elif carrier == "citation":
            entry = (safe_section.get("entries") or [{}])[0]
            raw_artifact_id = payload.get("artifact_id")
            projected_payload["artifact_id"] = entry.get("source_alias") or entry.get("id")
            kind = str(payload.get("kind") or "data_ref")
            if attribution_ctx is not None and isinstance(raw_artifact_id, str) and (flag_on or kind == KB_CHUNK_KIND):
                # Batch 2 new path: batch 1 attribute_refs resolves attribution + citation_presentation yields six states
                # (same mapping point as c1 canvas sidecars; same shape on both surfaces, tech-design §0.3).
                _apply_v2_citation_row(
                    projected_payload,
                    payload=payload,
                    entry=entry,
                    kind=kind,
                    verdict=verdicts.get((kind, raw_artifact_id)),
                    rendered=attribution_ctx.rendered,
                )
            else:
                # Legacy path (flag=0, retained one release cycle then removed in batch 3).
                if entry.get("access_state") == "linked_accessible" and isinstance(entry.get("safe_label"), str):
                    projected_payload["label"] = entry["safe_label"]
                else:
                    projected_payload.pop("label", None)
                projected_payload["access_state"] = entry.get("access_state", "linked_unavailable")
                projected_payload.pop("kb_generation_id", None)
                projected_payload.pop("kb_source_check_state", None)
                projected_payload["generic_type"] = entry.get("generic_type", "other_source")
        else:
            for key in absent:
                safe_section.pop(key, None)
            projected_payload[carrier] = safe_section
        out.append(projected_payload)
    for event_index, artifact_index, _ in inline_charts:
        out[event_index]["inline_artifacts"][artifact_index]["spec"] = next(projected_iter)["spec"]
    return _project_legacy_chat_failures(events, out)


async def project_live_event_payload_for_viewer(
    request: Any,
    turn_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    event_ordinal: int | None = None,
    cache: LiveEventProjectionCache | None = None,
) -> dict[str, Any]:
    if cache is not None:
        cache.reset_for_turn(turn_id)
        sid = _inflight_section_id(event_type, payload)
        if sid:
            cache.in_flight_section_ids.add(sid)
        if event_type in _EVENT_SECTION_CARRIERS:
            # Canvas state changed: invalidate cached projector/attribution context (this event's slow path repopulates them).
            cache.invalidate_projection()
        elif (
            event_type == "turn.citation_registered"
            and cache.projector is not None
            and cache.attribution is not None
            and source_attribution_v2_enabled()
            and isinstance(payload.get("artifact_id"), str)
        ):
            fast = await _project_live_citation_fast(
                cache,
                turn_id,
                payload,
                event_ordinal=event_ordinal,
            )
            if fast is not None:
                return fast
    return (
        await project_event_payloads_for_viewer(
            request,
            turn_id,
            [(event_type, payload)],
            event_ordinals=([event_ordinal] if event_ordinal is not None else None),
            _live_cache=cache,
            _extra_in_flight=frozenset(cache.in_flight_section_ids) if cache is not None else None,
        )
    )[0]


async def _project_live_citation_fast(
    cache: LiveEventProjectionCache,
    turn_id: str,
    payload: Mapping[str, Any],
    *,
    event_ordinal: int | None,
) -> dict[str, Any] | None:
    """live citation 快路（tech-design §3.4-3）：复用缓存 projector（alias 是按
    identity 的无状态 HMAC）+ 缓存 scope 跑单条 attribute_refs。报告轮 pre-done
    的引用 flush 是几十上百条连发，逐条全量重建 canvas 状态会吃掉首响。

    返回 None = 快路条件不满足，调用方落回慢路（慢路与分页/重放共用同一段代码，
    正确性以慢路为准；对拍测试钉两路 byte-equivalent）。
    """
    fake_section, carrier, _absent = _event_section(
        "turn.citation_registered",
        payload,
        turn_id=turn_id,
        ordinal=event_ordinal if isinstance(event_ordinal, int) and event_ordinal >= 1 else 1,
    )
    if fake_section is None or carrier != "citation":
        return None
    assert cache.attribution is not None and cache.projector is not None  # Caller already checked.
    kind = str(payload.get("kind") or "data_ref")
    raw_artifact_id = str(payload.get("artifact_id"))
    verdicts = await attribute_refs(
        [(kind, raw_artifact_id)],
        scope=cache.attribution.scope,
        stores=cache.attribution.stores,
        in_flight=InFlightIndex(frozenset(cache.in_flight_section_ids)),
    )
    verdict = verdicts.get((kind, raw_artifact_id))
    rendered: Container[str] = cache.attribution.rendered
    exit_candidate = getattr(verdict, "exit_candidate", None)
    if isinstance(exit_candidate, str) and exit_candidate and exit_candidate not in cache.attribution.rendered:
        # The slow path includes turn-owned artifacts in this page's events,
        # while the cached fast path contains only artifacts known at warm time;
        # new turn artifacts narrow to labeled (live ≠ replay). Recheck ownership once using the slow path's
        # same criterion (created_by_turn == current turn, server-written) and update the cache:
        # rendered grows monotonically within a turn; subsequent citations hit directly.
        owned_now = await _turn_owned_artifact_ids(
            cache.attribution.stores.artifact_store,
            [exit_candidate],
            turn_id,
        )
        if owned_now:
            cache.attribution.rendered = frozenset(cache.attribution.rendered | owned_now)
            rendered = cache.attribution.rendered
    safe_sections, _ = cache.projector.project([fake_section])
    entry = (safe_sections[0].get("entries") or [{}])[0] if safe_sections else {}
    projected_payload = copy.deepcopy(dict(payload))
    projected_payload["artifact_id"] = entry.get("source_alias") or entry.get("id")
    _apply_v2_citation_row(
        projected_payload,
        payload=payload,
        entry=entry,
        kind=kind,
        verdict=verdict,
        rendered=rendered,
    )
    return projected_payload


__all__ = [
    "EventAttributionContext",
    "LiveEventProjectionCache",
    "SourcePatchError",
    "ViewerSafeCanvasProjector",
    "ViewerSourceProjectionError",
    "citation_presentation",
    "project_event_payloads_for_viewer",
    "project_live_event_payload_for_viewer",
    "source_attribution_v2_enabled",
    "source_ref_artifact_ids",
]
