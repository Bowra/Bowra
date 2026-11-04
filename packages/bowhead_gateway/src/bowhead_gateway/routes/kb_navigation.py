"""Source navigation on the original KB authorization and management surface."""

from __future__ import annotations

from typing import Annotated, Literal

import aiosqlite
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.kb.source_navigation import _project, list_sources, visible_categories
from bowhead_core.kb.source_navigation_store import NavigationConflict, SourceNavigationStore
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.kb_source_navigation import current_description_assignment, navigation_context
from bowhead_gateway.routes.auth import _current_user

router = APIRouter(tags=["knowledge-base"])


def _category_public(row: dict) -> dict:
    return {key: row[key] for key in ("id", "name", "scope", "aliases", "parent_id", "revision")}


class NavigationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_version: int = Field(ge=1)
    expected_generation_id: str | None
    expected_revision: int = Field(ge=0)
    manual_description: str | None = Field(default=None, max_length=500)
    category_ids: list[str] | None = Field(default=None, max_length=8)


class GenerateDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_version: int = Field(ge=1)
    expected_generation_id: str
    expected_revision: int = Field(ge=0)
    action_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class CategoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["team", "space", "personal"]
    name: str = Field(min_length=1, max_length=80)
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=8)
    parent_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


async def _context(request: Request, user, *, write: bool = False):
    from bowhead_gateway.routes.kb import _get_files_store, _kb_error, _make_scope_context_checked

    if getattr(request.app.state, "user_store", None) is None:
        raise _kb_error(503, "kb_service_unavailable")
    scope = await _make_scope_context_checked(
        user, request, allow_missing_workspace=True, operation=SpaceOperation.WRITE if write else SpaceOperation.READ_HISTORY
    )
    return scope, navigation_context(request.app, scope), SourceNavigationStore(_get_files_store(request)._db_path)


@router.get("/sources")
async def sources(
    request: Request,
    query: str = Query(default="", max_length=200),
    category_id: str | None = None,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    user=Depends(_current_user),
):
    from bowhead_gateway.routes.kb import _kb_error

    _, ctx, _ = await _context(request, user)
    try:
        return (await list_sources(ctx, query=query, category_id=category_id, cursor=cursor, limit=limit)).payload()
    except KnowledgeAccessError as exc:
        if str(exc) == "knowledge_catalog_continuation_unavailable":
            raise _kb_error(
                503,
                "knowledge_catalog_continuation_unavailable",
                hint="Source directory continuation is unavailable. Ask the instance administrator to check the existing encryption configuration.",
            ) from exc
        raise _kb_error(
            503,
            "knowledge_source_authorization_unavailable",
            hint="Source permissions could not be checked. Try again when the permission service is available.",
        ) from exc
    except ValueError as exc:
        raise _kb_error(400, "knowledge_catalog_cursor_invalid", hint="Refresh the source directory and try again.") from exc


@router.get("/files/{file_id}/navigation")
async def get_navigation(file_id: str, request: Request, user=Depends(_current_user)):
    from bowhead_gateway.routes.kb import _kb_file_not_found, _kb_scope_check

    scope, ctx, store = await _context(request, user)
    file = await _kb_scope_check(file_id, scope, ctx.kb_files_store, request)
    try:
        definitions = await visible_categories(ctx, store)
        page = await _project(ctx, [file], definitions, store)
    except KnowledgeAccessError as exc:
        from bowhead_gateway.routes.kb import _kb_error

        raise _kb_error(
            503,
            "knowledge_source_authorization_unavailable",
            hint="Source permissions could not be checked. Try again when the permission service is available.",
        ) from exc
    if not page.entries:
        # Processing files have no readable content navigation yet.
        if file.parse_status == "done":
            raise _kb_file_not_found()
        entry = {
            "file_id": file.file_id,
            "generation_id": file.active_generation_id,
            "source_version": file.version,
            "navigation_revision": await store.revision(file_id),
            "description": "",
            "description_kind": "name",
            "categories": [],
            "topics": [],
            "coverage": "not_ready",
        }
    else:
        entry = page.entries[0]
    selected_ids = {row["id"] for row in entry["categories"]} - {row["id"] for row in definitions}
    if selected_ids:
        definitions += await visible_categories(
            ctx, store, await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id, identities=list(selected_ids))
        )
    try:
        assignment = await current_description_assignment(request.app, ctx.team_id)
        generation_status = "enabled" if assignment else "disabled"
    except Exception:
        assignment = None
        generation_status = "unavailable"
    job = await store.latest_job(file_id)
    writable = await ctx.kb_file_authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id, write=True)
    action_prefix = f"manual:{ctx.user_id}:"
    own_action = (
        job["admission_key"][len(action_prefix) :]
        if job and job["actor_user_id"] == ctx.user_id and job["admission_key"].startswith(action_prefix)
        else None
    )
    return {
        **entry,
        "can_edit": writable,
        "generation_enabled": assignment is not None,
        "generation_status": generation_status,
        "generation_model": assignment.model if assignment and writable else None,
        "manual_needs_review": await store.manual_needs_review(file) if writable else False,
        "description_job": {"id": job["id"], "state": job["state"], "error_code": job["error_code"], "action_id": own_action}
        if writable
        and job
        and job["generation_id"] == file.active_generation_id
        and job["membership_revision"] == file.membership_revision
        and job["source_version"] == file.version
        else None,
        "category_options": [_category_public(row) for row in definitions],
    }


async def _writable_file(file_id: str, request: Request, user, expected_generation: str | None, expected_source_version: int):
    from bowhead_gateway.routes.kb import _kb_error, _kb_scope_check

    scope, ctx, store = await _context(request, user, write=True)
    file = await _kb_scope_check(file_id, scope, ctx.kb_files_store, request)
    if not await ctx.kb_file_authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id, write=True):
        raise _kb_error(403, "kb_scope_access_denied")
    if file.parse_status != "done":
        raise _kb_error(409, "knowledge_source_not_ready", hint="Wait for knowledge processing to finish before editing source navigation.")
    if file.active_generation_id != expected_generation or file.version != expected_source_version:
        raise _kb_error(409, "knowledge_source_changed", hint="Refresh the file and review its current version.")
    return ctx, store, file


@router.patch("/files/{file_id}/navigation", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def patch_navigation(file_id: str, body: NavigationPatch, request: Request, user=Depends(_current_user)):
    from bowhead_gateway.routes.kb import _kb_error

    ctx, store, file = await _writable_file(file_id, request, user, body.expected_generation_id, body.expected_source_version)
    labels = body.category_ids if "category_ids" in body.model_fields_set else None
    if "category_ids" in body.model_fields_set:
        labels = labels or []
        visible = {
            category["id"]
            for category in await visible_categories(
                ctx, store, await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id, identities=labels)
            )
        }
        if not set(labels).issubset(visible):
            raise _kb_error(404, "knowledge_category_unavailable", hint="Choose a currently visible category.")
    try:
        revision = await store.edit(
            file,
            revision=body.expected_revision,
            actor_user_id=ctx.user_id,
            manual_text=(body.manual_description or "").strip(),
            edit_text="manual_description" in body.model_fields_set,
            category_ids=labels,
        )
    except NavigationConflict as exc:
        raise _kb_error(409, "knowledge_navigation_changed", hint="Refresh the file before saving the description or labels.") from exc
    return {"file_id": file_id, "navigation_revision": revision}


@router.post("/files/{file_id}/description-jobs", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def generate(file_id: str, body: GenerateDescription, request: Request, user=Depends(_current_user)):
    from bowhead_gateway.routes.kb import _kb_error

    ctx, store, file = await _writable_file(file_id, request, user, body.expected_generation_id, body.expected_source_version)
    assignment = await current_description_assignment(request.app, ctx.team_id)
    if assignment is None:
        raise _kb_error(409, "knowledge_description_disabled", hint="Ask a Team administrator to select a knowledge source description model.")
    try:
        identity = await store.enqueue(
            file,
            fingerprint=assignment.fingerprint,
            admission_key=f"manual:{ctx.user_id}:{body.action_id}",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            revision=body.expected_revision,
        )
    except NavigationConflict as exc:
        if str(exc) == "knowledge_generation_unverified":
            raise _kb_error(
                409,
                "knowledge_generation_unverified",
                hint="Reindex this source to establish a verified current publication before generating its description.",
            ) from exc
        raise _kb_error(409, "knowledge_navigation_changed", hint="Refresh the file and review the current source before generating.") from exc
    return {"job_id": identity, "generation_model": assignment.model}


@router.get("/source-categories")
async def categories(request: Request, query: str = Query(default="", max_length=80), user=Depends(_current_user)):
    _, ctx, store = await _context(request, user)
    return {
        "categories": [
            _category_public(row)
            for row in await visible_categories(ctx, store, await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id, query=query))
        ],
        "partial": True,
    }


async def _save_category(body: CategoryInput, request: Request, user, identity: str | None):
    from bowhead_gateway.routes.kb import _kb_error, authorize_kb_scope_write

    scope, ctx, store = await _context(request, user, write=True)
    target = ctx.team_id if body.scope == "team" else ctx.user_id if body.scope == "personal" else ctx.workspace_id
    await authorize_kb_scope_write(request, user, scope=body.scope, scope_id=target)
    if not body.name.strip() or any(not value.strip() or len(value) > 80 for value in body.aliases):
        raise _kb_error(422, "knowledge_category_invalid", hint="Use a nonempty category name and short aliases.")
    try:
        return await store.save_category(
            team_id=ctx.team_id,
            scope=body.scope,
            scope_id=target,
            name=body.name.strip(),
            aliases=list(dict.fromkeys(value.strip() for value in body.aliases)),
            parent_id=body.parent_id,
            actor_user_id=ctx.user_id,
            category_id=identity,
            revision=body.expected_revision,
        )
    except (NavigationConflict, aiosqlite.IntegrityError) as exc:
        raise _kb_error(409, "knowledge_category_changed", hint="Refresh categories; use a unique name and a parent in the same scope.") from exc


@router.post("/source-categories", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def create_category(body: CategoryInput, request: Request, user=Depends(_current_user)):
    return await _save_category(body, request, user, None)


@router.patch("/source-categories/{category_id}", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def edit_category(category_id: str, body: CategoryInput, request: Request, user=Depends(_current_user)):
    from bowhead_gateway.routes.kb import _kb_error

    if body.expected_revision is None:
        raise _kb_error(422, "knowledge_category_revision_required", hint="Refresh the category before editing it.")
    return await _save_category(body, request, user, category_id)
