"""Owner-scoped durable document projection, preview and exact confirmation."""

from urllib.parse import quote

from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.sessions import _assert_session_access, _store, _user_id

router = APIRouter(dependencies=[Depends(_current_user)])


async def published_file_identity(request: Request, art, user_id: str):
    """Generic file routes cannot bypass parent-source or document-owner ACL."""
    service = _service(request)
    from bowhead_core.documents.file_access import published_file_identity as resolve_published_file

    row = await resolve_published_file(service.delivery.path, artifact_id=art.id, user_id=user_id)
    if row is None:
        raise HTTPException(404, detail={"error": {"code": "document_not_found"}})
    scope = await _scope(request, job_id=row[0])
    await service.delivery.get(scope=scope, job_id=row[0])
    return scope, row[0], row[1]


class ConfirmDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: str = Field(min_length=1, max_length=128)
    draft_digest: str = Field(pattern="^[a-f0-9]{64}$")


class CancelDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: str = Field(min_length=1, max_length=128)


def _service(request):
    service = getattr(request.app.state, "document_service", None)
    if service is None:
        raise HTTPException(503, detail={"error": {"code": "document_capability_unavailable"}})
    return service


async def _scope(request: Request, *, session_id: str | None = None, job_id: str | None = None, write: bool = False):
    service = _service(request)
    async with open_shared_ctx(service.delivery.path) as db:
        if job_id is not None:
            async with db.execute("SELECT session_id FROM document_jobs WHERE id=? AND requested_by=?", (job_id, _user_id(request))) as cursor:
                row = await cursor.fetchone()
            if row is None or (session_id is not None and session_id != row[0]):
                raise HTTPException(404, detail={"error": {"code": "document_not_found"}})
            session_id = row[0]
        session = await _assert_session_access(
            session_id, request, _store(request), operation=SpaceOperation.WRITE if write else SpaceOperation.READ_HISTORY, conceal=True
        )
        async with db.execute("SELECT team_id FROM workspaces WHERE id=?", (session.workspace_id,)) as cursor:
            team = await cursor.fetchone()
    if team is None:
        raise HTTPException(404, detail={"error": {"code": "document_not_found"}})
    return DocumentScope(team_id=team[0], workspace_id=session.workspace_id, user_id=_user_id(request))


def _error(exc: DocumentRequestError):
    inaccessible = {"document_not_found", "document_canvas_unavailable", "document_canvas_source_unavailable", "document_canvas_input_unavailable"}
    return HTTPException(404 if exc.code in inaccessible else 409, detail={"error": {"code": exc.code}})


@router.get("/sessions/{session_id}/documents")
async def list_documents(session_id: str, request: Request, response: Response, after_id: str = ""):
    response.headers["Cache-Control"] = "no-store, private"
    scope = await _scope(request, session_id=session_id)
    service = _service(request)
    try:
        jobs = await service.jobs.list_for_session(scope=scope, session_id=session_id, after_id=after_id, limit=100)
        visible = []
        for job in jobs:
            try:
                await service.authorize_canvas(scope, job.id)
            except DocumentRequestError as exc:
                if exc.code in {"document_canvas_unavailable", "document_canvas_source_unavailable", "document_canvas_input_unavailable"}:
                    continue
                raise
            visible.append(await service.delivery.get(scope=scope, job_id=job.id))
        return {
            "jobs": visible,
            "next_cursor": jobs[-1].id if len(jobs) == 100 else None,
        }
    except DocumentRequestError as exc:
        raise _error(exc) from None


@router.get("/sessions/{session_id}/documents/{job_id}")
async def get_document(session_id: str, job_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store, private"
    scope = await _scope(request, session_id=session_id, job_id=job_id)
    try:
        service = _service(request)
        await service.authorize_canvas(scope, job_id)
        return await service.delivery.get(scope=scope, job_id=job_id)
    except DocumentRequestError as exc:
        raise _error(exc) from None


@router.post("/documents/{job_id}/confirm")
async def confirm_document(job_id: str, body: ConfirmDraft, request: Request):
    scope = await _scope(request, job_id=job_id, write=True)
    try:
        service = _service(request)
        await service.authorize_publication(scope)
        await service.authorize_canvas(scope, job_id)
        return await service.delivery.confirm(scope=scope, job_id=job_id, command_id=body.command_id, draft_digest=body.draft_digest)
    except DocumentRequestError as exc:
        raise _error(exc) from None


@router.post("/documents/{job_id}/cancel")
async def cancel_document(job_id: str, body: CancelDraft, request: Request):
    scope = await _scope(request, job_id=job_id)
    service = _service(request)
    try:
        await service.authorize_canvas(scope, job_id)
        current = await service.delivery.get(scope=scope, job_id=job_id)
        if current["state"] == "published":
            raise DocumentRequestError("document_already_published")
        await service.jobs.cancel(scope=scope, job_id=job_id, command_id=body.command_id, expected_proposal_revision=1)
        return await service.delivery.get(scope=scope, job_id=job_id)
    except DocumentRequestError as exc:
        raise _error(exc) from None


@router.get("/documents/{job_id}/preview/{name}")
@router.get("/documents/{job_id}/download/{name}")
async def document_file(job_id: str, name: str, request: Request):
    scope = await _scope(request, job_id=job_id)
    preview = "/preview/" in request.url.path
    if not preview and name not in {"candidate.pptx", "candidate.pdf"}:
        raise HTTPException(404, detail={"error": {"code": "document_not_found"}})
    try:
        service = _service(request)
        await service.authorize_canvas(scope, job_id)
        content, mime = await service.delivery.read_file(scope=scope, job_id=job_id, name=name, preview=preview)
        await service.authorize_canvas(scope, job_id)
    except DocumentRequestError as exc:
        raise _error(exc) from None
    headers = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"}
    if not preview:
        job = await _service(request).delivery.get(scope=scope, job_id=job_id)
        filename = job["draft"]["output_stem"] + (".pdf" if name.endswith(".pdf") else ".pptx")
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(filename, safe="")
    return Response(content=content, media_type=mime, headers=headers)
