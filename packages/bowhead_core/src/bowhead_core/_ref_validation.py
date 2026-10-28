"""Sprint ν1 — Reference validation single entry.

Validates that all refs (chart_section_id / table_artifact_id /
computation_artifact_id / [ref:S] / [num:V:U:src:S] src) point to ID真实存在.

Produces errors[] for retry; if retry > 2, _strip_invalid_refs removes the
fakes + whitespace normalize.

per-turn scope: real_refs built from canvas_store + artifact_store + this
session's tool_invocations only. Cross-turn refs naturally rejected (KB
chunks via kb_search results scoped to current invocations).

drop allowlist injection (推 ν1.5) — peer 通过 errors[] retry 自纠.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# Marker regexes (mirror frontend citation-plugin.ts NUM_NEW_RE / REF_RE)
_NUM_RE = re.compile(
    r"\[num:(-?\d+(?:\.\d+)?(?:[^:#\]]*)?)"  # value (loose)
    r"(?::([^:#\]]*))?"  # optional unit
    r":src:([^#\]]+)"  # src (required for bound markers)
    r"(?:#highlight=([^\]]+))?\]"  # optional highlight
)
_REF_RE = re.compile(r"\[ref:([^\]]+)\]")


def _flatten_tool_invocations(invs: Iterable[Any]) -> list[Any]:
    """Walk ToolInvocation list incl. nested (subagent_spawn returns 也含
    sub-tool invocations on .nested attribute, types.py:173)."""
    flat: list[Any] = []
    for inv in invs:
        flat.append(inv)
        nested = getattr(inv, "nested", None)
        if isinstance(nested, list) and nested:
            flat.extend(_flatten_tool_invocations(nested))
    return flat


def _extract_turn_artifact_ids(invs: Iterable[Any]) -> set[str]:
    """本轮 tool_invocations 产出的全部 artifact id (含批量子结果 + nested)。

    review F5: `[num:…:src:S]` 的 artifact 白名单必须是**本轮**产物 — 用
    artifact_store.list_for_session 会把历史轮 artifact 全放进来, 丢掉本模块
    "cross-turn refs naturally rejected" 的守卫 (peer 从上下文拷老 id
    本该被剥除, 却变成指向陈旧数据的可点引用)。"""
    ids: set[str] = set()

    def _add(v: Any) -> None:
        if isinstance(v, str) and v:
            ids.add(v)

    def _collect(result: Any) -> None:
        if not isinstance(result, dict):
            return
        for k in ("artifact_id", "artifactId", "_ref_artifact_id"):
            _add(result.get(k))
        inner = result.get("result_json")
        if isinstance(inner, dict):
            _collect(inner)
        # review[1]-fix: batch subresults results[] + sandbox outputs[] + inputs
        # all count as current-turn artifacts. Traversing only results[] missed python_sandbox outputs,
        # causing valid [num:…:src:art_x] references to be stripped as illegal (the provenance #11
        # must preserve). Recognize camelCase artifactId too.
        for list_key in ("results", "outputs"):
            lst = result.get(list_key)
            if isinstance(lst, list):
                for r in lst:
                    if isinstance(r, dict):
                        for k in ("artifact_id", "artifactId", "_ref_artifact_id"):
                            _add(r.get(k))
        inputs = result.get("inputs")
        if isinstance(inputs, dict):
            for spec in inputs.values():
                if isinstance(spec, dict):
                    for k in ("artifact_id", "artifactId", "_ref_artifact_id"):
                        _add(spec.get(k))

    for inv in _flatten_tool_invocations(invs):
        _collect(getattr(inv, "result", None))
    return ids


def _extract_kb_chunk_ids(invs: Iterable[Any]) -> set[str]:
    """Extract chunk_ids from kb_search ToolInvocations.

    Pattern matches source_ref_aggregator.py:108-127 KB result shape.
    """
    ids: set[str] = set()
    for inv in _flatten_tool_invocations(invs):
        if getattr(inv, "tool_name", "") != "kb_search":
            continue
        result = getattr(inv, "result", None)
        if not isinstance(result, dict):
            continue
        chunks = result.get("chunks") or []
        for c in chunks:
            if isinstance(c, dict):
                cid = c.get("chunk_id")
                if isinstance(cid, str) and cid:
                    ids.add(cid)
    return ids


async def _build_real_refs(
    *,
    canvas_id: str,
    session_id: str,
    user_id: str,
    canvas_store: Any,
    artifact_store: Any,
    tool_invocations: Iterable[Any],
) -> dict[str, set[str]]:
    """Build allowlist of real existing reference IDs by kind.

    Returns dict with keys: chart, data, code, doc, tl, turn_artifacts.

    Raises nothing — store errors return empty sets (validate falls open
    not closed when stores miss; tested via UT mock).
    """
    chart_ids: set[str] = set()
    code_ids: set[str] = set()
    try:
        chart_ids = await canvas_store.list_section_ids_by_kind(canvas_id, kind="chart")
        # feedback-0713 #3: chart_section_id is a **visual reference**; compute peers'
        # matplotlib PNGs become kind="image" sections through add_image (0.72.16 F9).
        # Evidence schema has no image-specific field, so peers can and should use chart_section_id
        # for them. Previously allowlisting only kind="chart" rejected 100% of image references as
        # hallucinated; peers passed only after dropping references (permanently losing clickable image provenance,
        # reproduced in all three turns of prod s_f7f79d3d2ceb). Pool image sections with charts.
        image_ids = await canvas_store.list_section_ids_by_kind(canvas_id, kind="image")
        chart_ids = set(chart_ids) | set(image_ids)
        code_ids = await canvas_store.list_section_ids_by_kind(canvas_id, kind="code_block")
    except Exception:  # noqa: S110
        pass

    data_ids: set[str] = set()
    try:
        data_arts = await artifact_store.list_for_session(
            session_id=session_id,
            user_id=user_id,
        )
        data_ids = {a.id for a in (data_arts or []) if hasattr(a, "id")}
    except Exception:  # noqa: S110
        pass

    doc_ids = _extract_kb_chunk_ids(tool_invocations)
    turn_artifact_ids = _extract_turn_artifact_ids(tool_invocations)

    tl_ids = {getattr(inv, "call_id", "") for inv in _flatten_tool_invocations(tool_invocations) if getattr(inv, "call_id", "")}

    return {
        "chart": chart_ids,
        "data": data_ids,
        "code": code_ids,
        "doc": doc_ids,
        "tl": tl_ids,
        "turn_artifacts": turn_artifact_ids,
    }


def _scan_markers(
    text: str,
    field_path: str,
    real_refs: dict[str, set[str]],
) -> list[dict]:
    """Scan inline [num:...:src:S] and [ref:S] markers; return errors for
    refs not in allowlist."""
    errors: list[dict] = []
    if not isinstance(text, str) or not text:
        return errors

    # Feishu 07.06 #11: `:src:` is a content-addressed ID, either call_id (tl)
    # or artifact_id. TurnSourcesRegistry already promises "specialists emit
    # content-addressed markers" (artifact granularity); after the value-map root fix, batch
    # subresults bind by artifact ID. Accepting only tl rejects all valid artifact markers.
    # Review F5: restrict artifact set to **this turn's** invocation outputs (turn_artifacts),
    # not all session data, preserving "cross-turn refs naturally rejected".
    _valid_src = real_refs.get("tl", set()) | real_refs.get("turn_artifacts", set())
    for m in _NUM_RE.finditer(text):
        src = m.group(3)
        if src and src not in _valid_src:
            errors.append(
                {
                    "kind": "num_src_not_in_allowlist",
                    "field": field_path,
                    "value": m.group(0),
                    "ref_id": src,
                    "allowlist_sample": sorted(_valid_src)[:5],
                }
            )

    for m in _REF_RE.finditer(text):
        ref = m.group(1)
        if ref and ref not in real_refs.get("doc", set()):
            errors.append(
                {
                    "kind": "kb_ref_not_in_allowlist",
                    "field": field_path,
                    "value": m.group(0),
                    "ref_id": ref,
                    "allowlist_sample": sorted(real_refs.get("doc", set()))[:5],
                }
            )
    return errors


def _scan_evidence(
    ev: dict,
    field_path: str,
    real_refs: dict[str, set[str]],
) -> list[dict]:
    """Scan evidence kind-specific ref fields (chart_section_id /
    table_artifact_id / computation_artifact_id)."""
    errors: list[dict] = []
    if not isinstance(ev, dict):
        return errors
    kind = ev.get("kind")
    field_map = {
        "chart": ("chart_section_id", "chart"),
        "table": ("table_artifact_id", "data"),
        "computation": ("computation_artifact_id", "data"),
    }
    if kind in field_map:
        ref_field, ref_kind = field_map[kind]
        ref_id = ev.get(ref_field)
        if ref_id and ref_id not in real_refs.get(ref_kind, set()):
            errors.append(
                {
                    "kind": f"{ref_kind}_ref_not_in_allowlist",
                    "field": f"{field_path}.{ref_field}",
                    "value": ref_id,
                    "ref_id": ref_id,
                    "allowlist_sample": sorted(real_refs.get(ref_kind, set()))[:5],
                }
            )
    return errors


def _validate_all_refs(
    payload: dict[str, Any],
    real_refs: dict[str, set[str]],
) -> list[dict]:
    """Walk all narrative-bearing fields + evidence, return errors[].

    Closes Risk r2-P0-2 + Eng r2 P0-2: full field coverage including
    eyebrow / kpi.delta / callout_text / insight.title / etc.
    """
    errors: list[dict] = []

    # 1. Top-level narrative
    for field in ("eyebrow", "exec_summary"):
        val = payload.get(field)
        if isinstance(val, str):
            errors.extend(_scan_markers(val, f"payload.{field}", real_refs))

    # caveats can be list[str] or str
    caveats = payload.get("caveats")
    if isinstance(caveats, str):
        errors.extend(_scan_markers(caveats, "payload.caveats", real_refs))
    elif isinstance(caveats, list):
        for i, item in enumerate(caveats):
            if isinstance(item, str):
                errors.extend(_scan_markers(item, f"payload.caveats[{i}]", real_refs))

    # 2. insights[]
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        for sub in ("title", "narrative", "thesis"):
            text = ins.get(sub)
            if isinstance(text, str) and text:
                errors.extend(_scan_markers(text, f"insights[{i}].{sub}", real_refs))
        for j, ev in enumerate(ins.get("evidence") or []):
            errors.extend(_scan_evidence(ev, f"insights[{i}].evidence[{j}]", real_refs))
            if isinstance(ev, dict):
                ct = ev.get("callout_text")
                if isinstance(ct, str) and ct:
                    errors.extend(
                        _scan_markers(
                            ct,
                            f"insights[{i}].evidence[{j}].callout_text",
                            real_refs,
                        )
                    )

    # 3. recommendations[]
    for i, rec in enumerate(payload.get("recommendations") or []):
        if not isinstance(rec, dict):
            continue
        for sub in ("rationale", "action", "expected_impact"):
            text = rec.get(sub)
            if isinstance(text, str) and text:
                errors.extend(_scan_markers(text, f"recommendations[{i}].{sub}", real_refs))

    # 4. kpis[]
    for i, kpi in enumerate(payload.get("kpis") or []):
        if not isinstance(kpi, dict):
            continue
        for sub in ("value", "delta"):
            text = kpi.get(sub)
            if isinstance(text, str) and text:
                errors.extend(_scan_markers(text, f"kpis[{i}].{sub}", real_refs))

    return errors


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    """Collapse 2+ whitespace → 1; strip leading/trailing."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def build_ref_remap_index(*, tool_invocations: Iterable[Any], sections: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """ν1 第一遍映射用的索引（2026-09-06，116 90 天 417 个被拒引用逐个对库的结论）。

    被拒的引用多数不是编的，是**拿错了命名空间**：
    - `chart_section_id` 填了 add_chart / add_image 的 **call id**（30）或图的 **artifact id**（11 + image 段 70）
    - `computation_artifact_id` 填了 python_sandbox / bi_query 的 **call id**（10）或 **code_block 段 id**（43）
    这些都有唯一的真实目标，让 peer 重试两圈只会逼它从 allowlist_sample 里挑一个「合法但未必对」的 id。
    索引全部来自本轮 tool_invocations 与本画布 sections，不猜。
    """
    call_to_section: dict[str, str] = {}
    artifact_to_section: dict[str, str] = {}
    call_to_artifacts: dict[str, list[str]] = {}
    section_to_artifacts: dict[str, list[str]] = {}
    # Review fix (P1): first-output-wins can select PNG as computation evidence. Track each artifact's data_role and mime; `_pick_evidence_artifact` chooses.
    # Production shape (bowhead_skill/tools.py `_output_artifacts_from_result_json`): only stdout / structured data_role values; file artifacts have only mime.
    artifact_roles: dict[str, str] = {}
    artifact_mimes: dict[str, str] = {}

    def _artifact_ids_of(result: Any, *, default_role: str | None = None) -> list[str]:
        ids: list[str] = []

        def _add(v: Any, role: Any = None, mime: Any = None) -> None:
            if isinstance(v, str) and v:
                if v not in ids:
                    ids.append(v)
                role = role if isinstance(role, str) and role else default_role
                if role and v not in artifact_roles:
                    artifact_roles[v] = role
                if isinstance(mime, str) and mime and v not in artifact_mimes:
                    artifact_mimes[v] = mime

        if not isinstance(result, dict):
            return ids
        for k in ("_ref_artifact_id", "artifact_id", "artifactId"):
            _add(result.get(k), result.get("data_role"), result.get("mime"))
        inner = result.get("result_json")
        if isinstance(inner, dict):
            for k in ("_ref_artifact_id", "artifact_id", "artifactId"):
                _add(inner.get(k), inner.get("data_role"), inner.get("mime"))
        # Production python_sandbox top-level `outputs` lists **filename strings**; ID-bearing dicts are under `output_artifacts` / `result_json.outputs`.
        # Inspect all three without `or` short-circuiting (nonempty top-level outputs would mask result_json.outputs).
        for container in (result, inner if isinstance(inner, dict) else None):
            if container is None:
                continue
            for list_key in ("results", "outputs", "output_artifacts"):
                lst = container.get(list_key)
                if isinstance(lst, list):
                    for r in lst:
                        if isinstance(r, dict):
                            for k in ("_ref_artifact_id", "artifact_id", "artifactId"):
                                _add(r.get(k), r.get("data_role"), r.get("mime"))
        return ids

    for inv in _flatten_tool_invocations(tool_invocations):
        call_id = getattr(inv, "call_id", "") or ""
        result = getattr(inv, "result", None)
        if not isinstance(result, dict):
            continue
        section_id = result.get("section_id") or result.get("id")
        tool_name = str(getattr(inv, "tool_name", "") or "")
        # bi_query outputs tables (subresults have artifact_id, no data_role); record structured so table evidence recognizes them.
        arts = _artifact_ids_of(result, default_role="structured" if tool_name.startswith("bi_query") else None)
        if call_id and isinstance(section_id, str) and section_id.startswith("sec_"):
            call_to_section[call_id] = section_id
            for a in arts:
                artifact_to_section.setdefault(a, section_id)
        if call_id and arts:
            call_to_artifacts[call_id] = arts

    chart_data_arts: dict[str, list[str]] = {}  # data_artifact_id → chart section IDs (map only unique; review P2 fix)
    chart_source_calls: dict[str, list[str]] = {}  # Chart spec.source_meta.tool_call_id (data-producing sandbox call) → chart section IDs
    for sec in sections or []:
        sid = getattr(sec, "id", None)
        payload = getattr(sec, "payload", None)
        if not sid or not isinstance(payload, dict):
            continue
        kind = getattr(sec, "kind", None)
        art = payload.get("artifact_id")
        if isinstance(art, str) and art:
            artifact_to_section.setdefault(art, sid)
        spec = payload.get("spec")
        if kind == "chart" and isinstance(spec, dict):
            dart = spec.get("data_artifact_id")
            if isinstance(dart, str) and dart:
                chart_data_arts.setdefault(dart, []).append(sid)
            meta = spec.get("source_meta")
            scid = meta.get("tool_call_id") if isinstance(meta, dict) else None
            if isinstance(scid, str) and scid:
                chart_source_calls.setdefault(scid, []).append(sid)
        outs = payload.get("output_artifact_ids")
        if isinstance(outs, list) and outs:
            section_to_artifacts[sid] = [o for o in outs if isinstance(o, str) and o]
        # Review fix (round 2 P2): only chart/image sections may be chart_section_id targets; code_block also has top-level call_id,
        # and registering it prematurely maps sandbox calls to code (revalidation rejects it, also masking artifact-based image routing).
        cid = payload.get("call_id")
        if kind in ("chart", "image") and isinstance(cid, str) and cid and sid.startswith("sec_"):
            call_to_section.setdefault(cid, sid)

    for dart, sids in chart_data_arts.items():
        if len(sids) == 1:
            artifact_to_section.setdefault(dart, sids[0])
    for scid, sids in chart_source_calls.items():
        if len(sids) == 1:
            call_to_section.setdefault(scid, sids[0])

    return {
        "call_to_section": call_to_section,
        "artifact_to_section": artifact_to_section,
        "call_to_artifacts": call_to_artifacts,
        "section_to_artifacts": section_to_artifacts,
        "artifact_roles": artifact_roles,
        "artifact_mimes": artifact_mimes,
        "chart_data_artifacts": sorted(chart_data_arts),
    }


_TABULAR_MIMES = (
    "text/csv",
    "application/json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/tab-separated-values",
)


def _pick_evidence_artifact(arts: list[str], index: dict[str, dict[str, Any]], *, ref_field: str) -> str | None:
    """证据该指向哪个产物。**不绑 src 比绑错安全**：每一档只在恰好一个候选时才选，多于一个就不映射。

    review 修第二轮（P1）：原「唯一 / 多个取 stdout」会把 `table_artifact_id` 指到 stdout 文本（前端「打开表格」打开一个 .txt），
    也会把 `computation_artifact_id` 从图的数据 CSV 挪到 stdout（前端只在 computation == 某图 data_artifact_id 时内嵌图）。
    - `table_artifact_id`：只认表——data_role=structured（含 bi_query 产物）或表类 mime；排除 stdout 与图片
    - `computation_artifact_id` / `[num:…:src:]`：先认「是某张图的数据 artifact」，再认 stdout / structured，最后认唯一的非图片产物
    """
    if not arts:
        return None
    roles = index.get("artifact_roles") or {}
    mimes = index.get("artifact_mimes") or {}
    chart_data = set(index.get("chart_data_artifacts") or ())

    def _is_image(a: str) -> bool:
        return str(mimes.get(a, "")).startswith("image/")

    def _is_table(a: str) -> bool:
        return roles.get(a) == "structured" or str(mimes.get(a, "")).split(";")[0] in _TABULAR_MIMES

    if ref_field == "table_artifact_id":
        tiers = [[a for a in arts if _is_table(a)]]
    else:
        tiers = [
            [a for a in arts if a in chart_data],
            [a for a in arts if roles.get(a) in ("stdout", "structured")],
            [a for a in arts if not _is_image(a) and roles.get(a) != "stdout"],
        ]
    for tier in tiers:
        if len(tier) == 1:
            return tier[0]
        if len(tier) > 1:
            return None
    return None


def remap_invalid_refs(payload: dict[str, Any], errors: list[dict], index: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict]]:
    """把「拿错命名空间」的证据引用改回真实 id；返回 (payload, remapped[])。改不了的原样留给 strip。

    只动 evidence 字段（chart_section_id / computation_artifact_id / table_artifact_id）；
    正文里的 `[ref:S]` / `[num:…:src:S]` 没有可映射的目标（kb 引用只能是 chunk id），交给 strip。
    """
    remapped: list[dict] = []
    c2s = index.get("call_to_section") or {}
    a2s = index.get("artifact_to_section") or {}
    c2a = index.get("call_to_artifacts") or {}
    s2a = index.get("section_to_artifacts") or {}
    for err in errors:
        kind = err.get("kind", "")
        field = err.get("field", "")
        ref = err.get("ref_id")
        if not isinstance(ref, str):
            continue
        if kind == "num_src_not_in_allowlist":
            # Review fix (P1): remap `[num:V:U:src:S]` src too (call ID / code section → artifact); strip bakes unremappable markers into plain text.
            target = _pick_evidence_artifact(c2a.get(ref) or s2a.get(ref) or [], index, ref_field="computation_artifact_id")
            marker = err.get("value")
            if target and isinstance(marker, str) and marker:
                try:
                    text = _get_by_path(payload, field)
                except Exception:  # noqa: S112 — Skip this item if the path is inaccessible
                    continue
                head = marker.rfind(":src:") + len(":src:")
                if isinstance(text, str) and marker in text and marker[head : head + len(ref)] == ref:
                    # Replace src only; preserve `#highlight=…` tail verbatim (review round 2 P3).
                    new_marker = marker[:head] + target + marker[head + len(ref) :]
                    _set_by_path(payload, field, text.replace(marker, new_marker))
                    remapped.append({"field": field, "from": ref, "to": target, "kind": kind})
            continue
        if "." not in field:
            continue
        base, ref_field = field.rsplit(".", 1)
        target = None
        if kind == "chart_ref_not_in_allowlist":
            target = c2s.get(ref) or a2s.get(ref)
            if not target:
                # Sandbox call ID used as chart_section_id: follow call → unique artifact → chart/image section for that artifact (review round 2 P2).
                via = [a2s[a] for a in (c2a.get(ref) or []) if a in a2s]
                target = via[0] if len(set(via)) == 1 else None
        elif kind == "data_ref_not_in_allowlist":
            target = _pick_evidence_artifact(c2a.get(ref) or s2a.get(ref) or [], index, ref_field=ref_field)
        if not target or target == ref:
            continue
        try:
            ev = _get_by_path(payload, base)
        except Exception:  # noqa: S112 — Skip inaccessible paths; continue remapping the rest
            continue
        if isinstance(ev, dict):
            ev[ref_field] = target
            remapped.append({"field": field, "from": ref, "to": target, "kind": kind})
    return payload, remapped


def _strip_invalid_refs(
    payload: dict[str, Any],
    errors: list[dict],
) -> dict[str, Any]:
    """Strip invalid markers/refs from payload + whitespace normalize.

    Errors are processed: marker errors strip the marker substring + repair
    dangling text; evidence ref errors set field to None (frontend renders
    caption-only fallback per µ-N1b pattern).
    """
    # Group by field for efficient scan
    text_strips: dict[str, list[str]] = {}  # field_path → [markers to strip]
    ev_nullify: list[tuple[str, str]] = []  # (field_path, ref_field_name)

    for err in errors:
        kind = err.get("kind", "")
        field = err.get("field", "")
        if kind in ("num_src_not_in_allowlist", "kb_ref_not_in_allowlist"):
            text_strips.setdefault(field, []).append(err["value"])
        elif kind in ("chart_ref_not_in_allowlist", "data_ref_not_in_allowlist"):
            # field is e.g. "insights[2].evidence[0].chart_section_id"
            # Split into evidence path + ref field name
            if "." in field:
                base, ref_field = field.rsplit(".", 1)
                ev_nullify.append((base, ref_field))

    # Apply text strips (walk + replace + normalize)
    for field_path, markers in text_strips.items():
        try:
            text = _get_by_path(payload, field_path)
            if isinstance(text, str):
                for marker in markers:
                    # Review fix (P1): deleting an entire `[num:V:U:src:S]` also loses the **number** (low compliance rate with [num:11.39:%:src:x] becomes low compliance rate alone).
                    # Bake plain text (keep V+U, remove src), honoring the peer prompt's promise that markers become plain text. Still remove `[ref:S]` entirely.
                    replacement = _bake_num_marker(marker) if marker.startswith("[num:") else ""
                    text = text.replace(marker, replacement)
                text = _normalize_whitespace(text)
                _set_by_path(payload, field_path, text)
        except Exception:  # noqa: S110
            pass

    # Apply evidence nullify
    for base_path, ref_field in ev_nullify:
        try:
            ev = _get_by_path(payload, base_path)
            if isinstance(ev, dict):
                ev[ref_field] = None
        except Exception:  # noqa: S110
            pass

    return payload


def _bake_num_marker(marker: str) -> str:
    """`[num:V:U:src:S]` / `[num:V:U]` → 纯文本 V+U（`citation_marks.strip_legacy` 的同一口径）。"""
    try:
        from bowhead_core.citation_marks import strip_legacy

        baked = strip_legacy(marker)
        return baked if isinstance(baked, str) else ""
    except Exception:  # noqa: BLE001 — Fall back to deletion if baking fails (previous behavior)
        return ""


_PATH_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _get_by_path(obj: Any, path: str) -> Any:
    """Walk dotted+bracket path: 'insights[0].evidence[1].callout_text'."""
    cur = obj
    # Strip 'payload.' prefix if present
    if path.startswith("payload."):
        path = path[len("payload.") :]
    for tok in _PATH_RE.findall(path):
        key, idx = tok
        if key:
            cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
        elif idx:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        if cur is None:
            return None
    return cur


def _set_by_path(obj: Any, path: str, value: Any) -> None:
    """Set value at dotted+bracket path."""
    if path.startswith("payload."):
        path = path[len("payload.") :]
    parts = _PATH_RE.findall(path)
    if not parts:
        return
    cur = obj
    for tok in parts[:-1]:
        key, idx = tok
        if key:
            cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
        elif idx:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        if cur is None:
            return
    last_key, last_idx = parts[-1]
    if last_key and isinstance(cur, dict):
        cur[last_key] = value
    elif last_idx and isinstance(cur, list):
        i = int(last_idx)
        if i < len(cur):
            cur[i] = value
