"""SourceRef aggregator (W11-5 / B-1).

从 session 内所有 turn 的 turn_log_store events 提取 SourceRef metadata,
canvas /by-session 路由用之填 CanvasDetailDTO.sourceRefs.

每个 tool_call event 配对其 tool_result 生成 1 条 SourceRef:
    id           = call_id (短 hash, frontend [num:V:U:src:S] 引用此 id)
    tool         = tool_name (datarc_nl2sql / compute_profile / ...)
    query        = arguments.query / .q / .question (常见 NL2SQL key)
    sql          = result.sql (NL2SQL 出来的 SQL,有则填)
    artifact_id  = result.artifact_id (有产出 artifact 时)
    turn_id      = parent turn id
    tool_call_id = call_id (同 id)

遇到 不可解析的 event 跳过(不阻断主流)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# Common query field names (used by NL2SQL / BI tools).
_QUERY_KEYS = ("query", "q", "question", "user_query", "natural_language_query")


def _extract_query(args: dict[str, Any] | None) -> str | None:
    if not isinstance(args, dict):
        return None
    for k in _QUERY_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:500]
    return None


def _extract_sql(result: Any) -> str | None:
    """从 tool_result.payload 提 sql 字段."""
    if not isinstance(result, dict):
        return None
    # Some NL2SQL shapes: {sql: "..."}, {result_json: {sql}}, {plan: {sql}}.
    direct = result.get("sql") or result.get("query_sql")
    if isinstance(direct, str):
        return direct.strip()[:5000]
    inner = result.get("result_json")
    if isinstance(inner, dict):
        s = inner.get("sql") or inner.get("query_sql")
        if isinstance(s, str):
            return s.strip()[:5000]
    return None


def _extract_artifact_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for k in ("artifact_id", "artifactId"):
        v = result.get(k)
        if isinstance(v, str) and v:
            return v
    inner = result.get("result_json")
    if isinstance(inner, dict):
        for k in ("artifact_id", "artifactId"):
            v = inner.get(k)
            if isinstance(v, str) and v:
                return v
    return None


_SUB_SOURCE_CAP = 12


def _extract_sub_sources(result: Any) -> list[dict[str, Any]] | None:
    """Per-sub-query sources for a batch bi_query (one tool call → N sub-queries →
    N data artifacts). Prefer the lightweight result_meta.sub_sources; else derive
    from the batch result_inline.results[] so legacy events (whose result_meta
    predates this) also enrich. Mirrors subagent._sub_sources_from_results
    (this is a leaf module — importing subagent would create a cycle)."""
    if not isinstance(result, dict):
        return None
    ss = result.get("sub_sources")
    if isinstance(ss, list):
        clean = [s for s in ss if isinstance(s, dict) and s.get("artifact_id")]
        if clean:
            return clean[:_SUB_SOURCE_CAP]
    results = result.get("results")
    if not isinstance(results, list):
        inner = result.get("result_json")
        results = inner.get("results") if isinstance(inner, dict) else None
    if isinstance(results, list):
        subs: list[dict[str, Any]] = []
        for s in results:
            if not isinstance(s, dict):
                continue
            aid = s.get("artifact_id") or s.get("_ref_artifact_id")
            if not isinstance(aid, str) or not aid:
                continue
            subs.append({"artifact_id": aid, "query": str(s.get("query_text") or "").strip()[:120]})
            if len(subs) >= _SUB_SOURCE_CAP:
                break
        return subs or None
    return None


def aggregate_source_refs_from_events(
    turn_id: str,
    events: Iterable[Any],  # iterable of TurnEventRow-like (with .event_type / .payload)
) -> list[dict[str, Any]]:
    """单 turn 的 events 列 → SourceRef list."""
    # First collect tool_call args by call_id; then pair tool_result events.
    calls: dict[str, dict[str, Any]] = {}
    for e in events:
        et = getattr(e, "event_type", None)
        payload = getattr(e, "payload", None) or {}
        if et == "tool_call":
            call_id = payload.get("call_id") or payload.get("tool_call_id")
            if not isinstance(call_id, str):
                continue
            calls[call_id] = {
                "tool": payload.get("tool_name") or "",
                "query": _extract_query(payload.get("arguments_preview") or payload.get("arguments")),
                "result": None,
            }
        elif et == "tool_result":
            call_id = payload.get("call_id") or payload.get("tool_call_id")
            if not isinstance(call_id, str):
                continue
            entry = calls.setdefault(call_id, {"tool": payload.get("tool_name") or "", "query": None, "result": None})
            # Extraction needs a DICT. MERGE both structured sources: result_inline
            # (full result, persisted only when ≤4KB) as base, result_meta
            # (lightweight citeable subset: artifact_id / sql / kb chunk refs —
            # built from the full result, so it survives the inline cap) overlaid
            # on top. Merging (not picking one) matters because an event may carry
            # a result_meta that lacks a field result_inline has, or vice-versa —
            # e.g. legacy events whose result_meta is only {type,row_count} while
            # artifact_id lives in result_inline. result_preview — a 500-char
            # STRING — is intentionally ignored: it fails every isinstance(dict)
            # extractor and silently dropped all enriched SourceRef fields in prod.
            merged: dict[str, Any] = {}
            inline = payload.get("result_inline")
            if isinstance(inline, dict):
                merged.update(inline)
            meta = payload.get("result_meta")
            if isinstance(meta, dict):
                merged.update(meta)
            entry["result"] = merged
            if not entry.get("tool"):
                entry["tool"] = payload.get("tool_name") or ""

    out: list[dict[str, Any]] = []
    for call_id, entry in calls.items():
        tool = str(entry.get("tool") or "")
        if not tool:
            continue  # No tool name: skip (no point).

        # V7 KB sprint Day 21 P1-21-2 fix: kb_search → 1 source_ref per chunk
        # (cite popover Day 14 already wired KB branch, just need backend aggregator emit kb_chunk_id)
        if tool == "kb_search":
            kb_chunks = _extract_kb_chunks(entry.get("result"))
            for chunk in kb_chunks:
                cid = chunk.get("chunk_id")
                if not cid:
                    continue
                page = chunk.get("page")
                kb_sref: dict[str, Any] = {
                    "id": cid,  # Use chunk_id directly as source_ref ID (V7 unified ID space P1-F).
                    "tool": "kb_search",
                    "query": entry.get("query") or "",
                    "turn_id": turn_id,
                    "tool_call_id": call_id,
                    "kb_chunk_id": cid,
                    "kb_file_id": chunk.get("file_id") or "",
                    "kb_filename": chunk.get("file_name") or "",
                    "kb_page": page if isinstance(page, int) else None,
                    "kb_section_title": chunk.get("section_title") or "",
                }
                out.append(kb_sref)
            continue  # KB skips default SQL/artifact extraction.

        sref: dict[str, Any] = {
            "id": call_id,
            "tool": tool,
            "query": entry.get("query") or "",
            "turn_id": turn_id,
            "tool_call_id": call_id,
        }
        sql = _extract_sql(entry.get("result"))
        if sql:
            sref["sql"] = sql
        art = _extract_artifact_id(entry.get("result"))
        if art:
            sref["artifact_id"] = art
        subs = _extract_sub_sources(entry.get("result"))
        if subs:
            sref["sub_sources"] = subs
            # Feishu 07.06 #11: batches (≥2 subresults) no longer collapse to the subs[0] single link. N tables under one
            # call_id pinned to the first table make gen2 [num:…:src:call_id]
            # popovers point at the wrong table (prod s_8a063ae8cd8e: YoY→tier-1 branch). Single subresults
            # still get an exact single link; multiple subresults expose only sub_sources for consumers to traverse
            # (frontend citation-popover sublists already support it; update gateway closure too).
            if "artifact_id" not in sref and len(subs) == 1:
                sref["artifact_id"] = subs[0]["artifact_id"]
            # Bypass found in real-stack dogfood: batch result_meta/inline already carries
            # a top-level artifact_id (leftover collapse when upstream generated meta); _extract_artifact_id
            # picks it up, bypassing the guard above. For batches, if that top-level ID is a subtable ID,
            # remove it too: it cannot identify which table supplied a number. IDs outside the subtable set are
            # genuine batch-level artifacts (e.g. merged CSVs); retain them.
            elif len(subs) >= 2 and sref.get("artifact_id") in {s["artifact_id"] for s in subs}:
                sref.pop("artifact_id", None)
        out.append(sref)
    return out


def _extract_kb_chunks(result: Any) -> list[dict[str, Any]]:
    """Extract chunks list from kb_search tool_result payload.

    Result shape (per tool_registry.py kb_search handler):
        {"chunks": [{"chunk_id", "content", "file_id", "file_name", "page", "section_title", "role", ...}, ...]}
    """
    if not isinstance(result, dict):
        return []
    chunks = result.get("chunks")
    if isinstance(chunks, list):
        return [c for c in chunks if isinstance(c, dict)]
    inner = result.get("result_json")
    if isinstance(inner, dict):
        chunks = inner.get("chunks")
        if isinstance(chunks, list):
            return [c for c in chunks if isinstance(c, dict)]
    return []


async def aggregate_source_refs_for_session(
    session_id: str,
    *,
    session_store: Any,
    turn_log_store: Any,
) -> list[dict[str, Any]]:
    """整 session 的 SourceRef 聚合(走每 turn → events)."""
    if session_store is None or turn_log_store is None:
        return []
    try:
        turns = await session_store.list_turns(session_id=session_id)
    except Exception as e:
        logger.warning("source_refs.list_turns_failed sid=%s err=%s", session_id, e)
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for t in turns:
        try:
            events = await turn_log_store.list_events(t.id)
        except Exception:  # noqa: S112
            continue
        for sref in aggregate_source_refs_from_events(t.id, events):
            sid = sref.get("id")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            out.append(sref)
    return out
