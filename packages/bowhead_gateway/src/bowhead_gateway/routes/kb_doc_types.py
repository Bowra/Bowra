"""KB doc_type CRUD + LLM extract_schema draft routes.

kb-knowledge-tables sprint v1.0 §5 (tech design).

API:
- GET /api/v1/kb/doc_types?workspace_id=ws — list ws doc_types
- POST /api/v1/kb/doc_types — create
- PUT /api/v1/kb/doc_types/{id} — update
- DELETE /api/v1/kb/doc_types/{id} — soft_delete
- POST /api/v1/kb/doc_types/draft — LLM 生 extract_schema 草稿 (用户 NL 描述 → JSON fields)

V1 改语义: 用户 NL "我想从合同抽合同金额、起止日、付款条件" → fields
(replace V3 columns / chunk_strategy 装饰).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal

import aiosqlite
from bowhead_core.kb.doc_type_store import (
    FIELD_TYPES,
    MAX_FIELDS_PER_SCHEMA,
    ExtractField,
    KbDocTypeStore,
    derive_doc_type_kind,
    is_frozen_snapshot,
    validate_options,
)
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kb/doc_types", tags=["knowledge-base"])

_DOC_TYPE_VALIDATION_CODES = frozenset(
    {
        "duplicate_field_names",
        "options_required",
        "option_empty",
        "option_too_long",
        "option_duplicate",
        "options_too_many",
        "options_not_applicable",
    }
)


def _get_store(request: Request) -> KbDocTypeStore:
    s = getattr(request.app.state, "kb_doc_type_store", None)
    if s is None:
        raise _doc_type_error(503, "kb_doc_type_service_unavailable")
    return s


async def _require_workspace_member(
    request: Request,
    user: User,
    workspace_id: str,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> None:
    if not has_workspace_access_source(request):
        return
    await build_access_context_from_request(
        request,
        user=user,
        workspace_id=workspace_id,
        operation=operation,
    )


async def _require_admin_or_creator(request: Request, user: User, *, workspace_id: str, creator_id: str) -> None:
    """Destructive doc_type ops (PUT/DELETE) require ws_admin OR the doc_type's
    own creator — mirrors ``KbPermission.can_delete`` for files.

    S2 (kb-security-quickfix): previously any workspace member could PUT/DELETE a
    doc_type, and DELETE cascades into bound files' extraction. A plain member who
    neither created it nor is an admin is now refused. The creator keeps managing
    their own table (membership still re-verified when RBAC hooks are present);
    the legacy no-user_store test harness stays unenforced, matching
    ``_require_workspace_member``.
    """
    if user.id == creator_id:
        if has_workspace_access_source(request):
            await build_access_context_from_request(request, user=user, workspace_id=workspace_id)
        return
    if not has_workspace_access_source(request):
        return
    ctx = await build_access_context_from_request(request, user=user, workspace_id=workspace_id)
    if ctx.is_team_admin or ctx.workspace_role == "ws_admin":
        return
    raise _doc_type_error(403, "workspace_admin_or_creator_required")


# ─── Pydantic DTOs ───────────────────────────


class _FieldIn(BaseModel):
    name: str
    # M4 FR-42: exact equality with core doc_type_store.FIELD_TYPES (Literal requires static literals,
    # cannot derive directly; consistency UT test_kb_doc_types_field_types_ssot asserts get_args
    # == FIELD_TYPES, failing on drift).
    type: Literal["string", "number", "currency", "date", "select", "multi_select", "boolean"] = "string"
    description: str = ""
    required: bool = False
    cross_verify: bool = False
    extraction_hint: str = ""
    # M4: select/multi_select options (server ExtractField.validate normalizes/rejects invalid values);
    # dimension reserves M5 numeric dimensions (unused in M4, round-tripped).
    options: list[str] | None = None
    dimension: dict | None = None


class _DocTypeCreateIn(BaseModel):
    workspace_id: str
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    fields: list[_FieldIn] = []
    extractor_model: str = "qwen-plus"
    cross_verify_enabled: bool = True


class _DocTypeUpdateIn(BaseModel):
    # M4 wave-2 security review P2: rename is core template-manager work; update previously lacked length bounds
    # (create caps at 200), broadcasting enormous persisted names to all space members through list (render/response amplification).
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    fields: list[_FieldIn] | None = None
    extractor_model: str | None = None
    cross_verify_enabled: bool | None = None


class _DraftIn(BaseModel):
    workspace_id: str
    user_query: str = Field(..., min_length=1)
    sample_text: str = ""  # optional: PDF preview to ground LLM


class _SeedIn(BaseModel):
    workspace_id: str


# ─── helpers ────────────────────────────────


def _fields_in_to_domain(items: list[_FieldIn]) -> list[ExtractField]:
    return [
        ExtractField(
            name=c.name,
            type=c.type,
            description=c.description,
            required=c.required,
            cross_verify=c.cross_verify,
            extraction_hint=c.extraction_hint,
            options=c.options,
            dimension=c.dimension,
        )
        for c in items
    ]


async def _templates_seeded(request: Request, workspace_id: str) -> bool:
    """该空间是否已播种内置模板。缺标记存储或查询失败时按已播种处理——绝不诱导前端去写。"""
    try:
        marker_store = getattr(request.app.state, "kb_marker_store", None)
        if marker_store is None:
            return True
        from bowhead_core.kb.template_seed import template_marker_key

        return bool(await marker_store.has_marker(template_marker_key(workspace_id)))
    except Exception:
        logger.warning("template seed marker read failed ws=%r", workspace_id, exc_info=True)
        return True


async def _maybe_seed_templates(
    request: Request,
    store: KbDocTypeStore,
    workspace_id: str,
) -> dict[str, int | bool]:
    """Best-effort template seed behind an explicit lifecycle-gated write."""

    try:
        # Security review P3: _require_workspace_member legacy fail-opens without RBAC hooks
        # for old harness compatibility. Keep reads, but **writes** (INSERT seed) follow kb_matrix's
        # strict rule: no trusted membership decision, no seeding.
        if not has_workspace_access_source(request):
            return {"seeded": False, "inserted": 0}
        marker_store = getattr(request.app.state, "kb_marker_store", None)
        if marker_store is None:
            return {"seeded": False, "inserted": 0}
        from bowhead_core.kb.template_seed import seed_templates_for_ws

        result = await seed_templates_for_ws(workspace_id, store, marker_store)
        return {
            "seeded": True,
            "inserted": result.inserted,
        }
    except Exception:
        logger.warning("template lazy seed failed ws=%r (degraded)", workspace_id, exc_info=True)
        return {"seeded": False, "inserted": 0}


# ─── routes ─────────────────────────────────


@router.get("")
async def list_doc_types(
    workspace_id: str,
    request: Request,
    user: User = Depends(_current_user),
    include_stats: int = 0,
) -> dict:
    """List ws doc_types.

    Sprint 2 Phase C: ``?include_stats=1`` augments response with per-doc_type
    aggregate stats (total / needs_review_count / last_extracted_at) sourced
    from kb_extracted_records (cross-DB query via ``list_with_stats``).
    """
    store = _get_store(request)
    await _require_workspace_member(
        request,
        user,
        workspace_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    # Read-only: tell FE whether this space's built-in templates were seeded (marker read, no write). If not, FE calls
    # POST /seed once then refetches; once per space, ordinary page opens issue no writes (KB experience cleanup FR8.1).
    seeded = await _templates_seeded(request, workspace_id)
    if include_stats:
        from dataclasses import asdict

        doc_types, stats_map = await store.list_with_stats(workspace_id)
        return {
            "items": [d.to_dict() for d in doc_types],
            "stats": {dt_id: asdict(s) for dt_id, s in stats_map.items()},
            "templates_seeded": seeded,
        }
    items = await store.list_for_ws(workspace_id)
    return {"items": [d.to_dict() for d in items], "templates_seeded": seeded}


@router.post("/seed", dependencies=[Depends(require_not_readonly)])
async def seed_doc_type_templates(
    body: _SeedIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, int | bool]:
    """Seed built-in templates without making the list GET stateful."""

    store = _get_store(request)
    await _require_workspace_member(request, user, body.workspace_id)
    return await _maybe_seed_templates(request, store, body.workspace_id)


@router.post("", dependencies=[Depends(require_not_readonly)])
async def create_doc_type(
    body: _DocTypeCreateIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_store(request)
    await _require_workspace_member(request, user, body.workspace_id)
    if len(body.fields) > MAX_FIELDS_PER_SCHEMA:
        raise _doc_type_error(422, "too_many_doc_type_fields")
    try:
        dt_id = await store.create(
            workspace_id=body.workspace_id,
            name=body.name,
            description=body.description,
            fields=_fields_in_to_domain(body.fields),
            extractor_model=body.extractor_model,
            cross_verify_enabled=body.cross_verify_enabled,
            created_by=user.id,
        )
    except ValueError as e:
        raise _doc_type_validation_error(e) from e
    except aiosqlite.IntegrityError as e:
        # M4 incidental fix (review P3-4b, preexisting): raw f"create failed: {e}" leaked SQL
        # to FE; use structured 409 like PUT rename.
        raise _doc_type_error(409, "doc_type_name_taken") from e
    except Exception as e:
        logger.exception("doc_type create failed workspace=%s", body.workspace_id)
        raise _doc_type_error(503, "kb_doc_type_write_unavailable") from e
    dto = await store.get(dt_id)
    return dto.to_dict() if dto else {"doc_type_id": dt_id}


@router.put("/{doc_type_id}", dependencies=[Depends(require_not_readonly)])
async def update_doc_type(
    doc_type_id: str,
    body: _DocTypeUpdateIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_store(request)
    current = await store.get(doc_type_id)
    if current is None:
        raise _doc_type_error(404, "doc_type_not_found")
    await _require_admin_or_creator(request, user, workspace_id=current.workspace_id, creator_id=current.created_by)
    # kb-matrix M4 gate⑤ (decision 12): graduated snapshot fields are read-only; fields_json keys rows by field name,
    # so rename/delete orphans the whole column (all NULL in chat). Only name/description may change;
    # change columns in matrix, then graduate again.
    if body.fields is not None and is_frozen_snapshot(current):
        raise _doc_type_error(422, "graduated_fields_frozen")
    if body.fields is not None and len(body.fields) > MAX_FIELDS_PER_SCHEMA:
        raise _doc_type_error(422, "too_many_doc_type_fields")
    fields = _fields_in_to_domain(body.fields) if body.fields is not None else None
    try:
        ok = await store.update(
            doc_type_id,
            name=body.name,
            description=body.description,
            fields=fields,
            extractor_model=body.extractor_model,
            cross_verify_enabled=body.cross_verify_enabled,
        )
    except ValueError as e:
        raise _doc_type_validation_error(e) from e
    except aiosqlite.IntegrityError as e:
        # M4 incidental fix (journey F5①, preexisting): rename colliding with UNIQUE(ws,name) previously
        # raised unhandled 500; core template-manager rename action gets structured 409.
        raise _doc_type_error(409, "doc_type_name_taken") from e
    if not ok:
        raise _doc_type_error(404, "doc_type_not_found")
    dto = await store.get(doc_type_id)
    return dto.to_dict() if dto else {"doc_type_id": doc_type_id}


@router.delete("/{doc_type_id}", dependencies=[Depends(require_not_readonly)])
async def delete_doc_type(
    doc_type_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_store(request)
    current = await store.get(doc_type_id)
    if current is None:
        raise _doc_type_error(404, "doc_type_not_found")
    await _require_admin_or_creator(request, user, workspace_id=current.workspace_id, creator_id=current.created_by)
    # kb-matrix M4 (decision 12): deleting graduated snapshots cascades soft deletion to rows, otherwise original
    # excerpts remain forever (storage/compliance debt; inaccessible to chat but erasure compliance needs actual deletion). Best effort:
    # failed rows cascade never blocks doc_type deletion (still inaccessible to chat; retained rows tracked as compliance debt).
    cascaded_rows = 0
    if is_frozen_snapshot(current):
        rows_store = getattr(request.app.state, "kb_extracted_rows_store", None)
        if rows_store is not None:
            try:
                cascaded_rows = await rows_store.soft_delete_for_doc_type(doc_type_id, workspace_id=current.workspace_id)
            except Exception:
                logger.warning("doc_type delete rows cascade failed dt=%s", doc_type_id, exc_info=True)
    ok = await store.soft_delete(doc_type_id)
    if not ok:
        raise _doc_type_error(404, "doc_type_not_found")
    # kb-matrix-sharing §5.3: deleted source template cascades revocation of its organizational-template entry (best effort,
    # independent of flag: store is permanent). Early return by kind (>99% deletes are nontemplates; save DB opens).
    if derive_doc_type_kind(current) == "template":
        team_tpl_store = getattr(request.app.state, "kb_team_template_store", None)
        if team_tpl_store is not None:
            try:
                await team_tpl_store.revoke_by_source(doc_type_id)
            except Exception:
                logger.warning("doc_type delete team-template cascade failed dt=%s", doc_type_id, exc_info=True)
    return {"deleted": True, "doc_type_id": doc_type_id, "cascaded_rows": cascaded_rows}


@router.post("/draft", dependencies=[Depends(require_not_readonly)])
async def draft_extract_schema(
    body: _DraftIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    """LLM 生 extract_schema 草稿. 用户 NL 描述 → JSON fields list (用户预览编辑后 POST 创建).

    用户体验:
    1. user 自然语言: "我想从合同抽合同金额、起止日、付款条件、对方"
    2. → POST /draft → LLM 草稿 [{"name":"合同金额","type":"currency",...}]
    3. → 用户预览编辑 → POST / 创建真 doc_type
    4. → 后续 PDF 选该 doc_type 上传 → worker 走 StructuredExtractor 走 fields
    """
    await _require_workspace_member(
        request,
        user,
        body.workspace_id,
        operation=SpaceOperation.EXECUTE,
    )
    # credential-readiness D2.2: draft uses team router (process router is an assembly placeholder, RefusalAdapter in prod;
    # formerly silently consumed echo here). Unconfigured -> same 409 as chat.
    from bowhead_gateway.routes._deps import team_router_or_409

    router_obj = await team_router_or_409(request.app.state, user.team_id)

    from bowhead_router import LLMRequest, Message

    # M4 FR-42: derive type-enum string from core FIELD_TYPES (SSOT: prompt and allowlist cannot drift).
    type_enum = "|".join(FIELD_TYPES)
    sys_prompt = (
        "你是 KB 文档表 schema 设计助手. 用户描述他想从一类文档 (合同/回单/简历/...)"
        "抽哪些字段, 你输出 JSON fields 草稿 (用户会预览编辑).\n\n"
        "Schema:\n"
        f'{{"fields": [{{"name": str, "type": "{type_enum}", '
        '"description": str, "required": bool, "cross_verify": bool, '
        '"extraction_hint": str, "options": [str] (仅 select/multi_select)}]}\n\n'
        "规则:\n"
        "1. name 用用户描述的字段名 (中文 OK), 简洁\n"
        "2. type: 金额→currency, 日期→date, 数量/序号→number; 固定候选里选一个→select,"
        " 可选多个→multi_select, 是/否判断→boolean; 其他 string\n"
        "3. select/multi_select 必须带 options (2-20 个候选, 单项 ≤64 字, 从用户描述/文档"
        " preview 推断); 其余类型不带 options\n"
        "4. description 简短说明该字段含义\n"
        "5. required = true 表示必填 (空值会标记缺失)\n"
        "6. cross_verify = true 用于高敏字段 (金额/日期 等), 触发 N=2 self-consistency\n"
        "7. extraction_hint 提示 LLM 该字段从文档哪找 (e.g. '通常在合同首页 第一/二条')\n"
        f"8. fields 数 ≤ {MAX_FIELDS_PER_SCHEMA}\n"
        "9. 仅输出 JSON, 不解释\n"
    )
    user_prompt = f"用户描述: {body.user_query}\n"
    if body.sample_text:
        user_prompt += f"\n文档 preview (前 2000 chars):\n{body.sample_text[:2000]}\n"

    try:
        resp = await router_obj.invoke(
            "lead",
            LLMRequest(
                model="",
                messages=[Message(role="user", content=user_prompt)],
                system=sys_prompt,
                max_output_tokens=1500,
                temperature=0.2,
                response_format={"type": "json_object"},
            ),
        )
    except Exception as e:
        logger.warning("doc_types.draft_llm_failed", exc_info=True)
        raise _doc_type_error(502, "doc_type_draft_failed") from e

    raw = (resp.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try extract JSON from fenced block
        import re

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise _doc_type_error(502, "doc_type_draft_invalid_response") from None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e2:
            raise _doc_type_error(502, "doc_type_draft_invalid_response") from e2

    fields_raw = data.get("fields", []) if isinstance(data, dict) else []
    if not isinstance(fields_raw, list):
        raise _doc_type_error(502, "doc_type_draft_invalid_response")

    # Sanitize + validate (≤MAX_FIELDS_PER_SCHEMA; type allowlist = core FIELD_TYPES SSOT).
    out_fields: list[dict] = []
    for c in fields_raw[:MAX_FIELDS_PER_SCHEMA]:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        type_ = str(c.get("type", "string")).lower().strip()
        if type_ not in FIELD_TYPES:
            type_ = "string"
        # M4: sanitize options; draft is suggestion, not submission (M3 precedent: wholly invalid options fall back
        # to bare string field, not 422). Valid options travel with field for preview editing.
        options: list[str] | None = None
        raw_options = c.get("options")
        if type_ in ("select", "multi_select"):
            try:
                candidate = [str(x) for x in raw_options] if isinstance(raw_options, list) else None
                options = validate_options(type_, candidate)
            except ValueError:
                type_ = "string"
                options = None
        out_field = {
            "name": name[:100],
            "type": type_,
            "description": str(c.get("description", ""))[:500],
            "required": bool(c.get("required", False)),
            "cross_verify": bool(c.get("cross_verify", False)),
            "extraction_hint": str(c.get("extraction_hint", ""))[:300],
        }
        if options is not None:
            out_field["options"] = options
        out_fields.append(out_field)

    return {"fields": out_fields, "raw_llm_output": raw[:1000]}


def _doc_type_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code, {"error": {"code": code}})


def _doc_type_validation_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    if code in _DOC_TYPE_VALIDATION_CODES:
        return _doc_type_error(422, code)
    return _doc_type_error(422, "invalid_doc_type_schema")
