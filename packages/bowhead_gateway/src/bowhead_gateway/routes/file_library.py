"""Purpose-based, authorized owner library reads."""

import base64
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Annotated, Literal

from bowhead_core._db import open_pooled_read_ctx, run_sqlite_authority_write
from bowhead_core.artifact_store import mime_to_family
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from bowhead_core import file_library as library
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes import files

View = Literal["deliverables", "uploads", "run", "all", "trash"]
READ = SpaceOperation.READ_HISTORY


def require_library():
    if not library.enabled():
        raise HTTPException(404, "File library unavailable")


def private_library_response(response: Response):
    require_library()
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/api/v1/files/library", dependencies=[Depends(private_library_response)])


class Filters(BaseModel):
    view: View = "deliverables"
    q: str = Field("", max_length=512)
    workspace_id: str = ""
    session_id: str = ""
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    mime_family: str = ""
    category: str = ""
    in_kb: bool | None = None
    retention: bool = False
    sort: Literal["date", "size"] = "date"
    cursor: str | None = Field(None, max_length=1024)
    limit: int = Field(50, ge=1, le=200)

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.since and self.until and self.until <= self.since:
            raise ValueError("The end of the date range must follow its start")
        return self


@dataclass
class Entry:
    row: library.LibraryVersion
    members: list[library.LibraryVersion]
    delivery: library.Delivery | None
    views: set[str]
    source: dict
    stamp: str
    space: str
    sent: bool


def matches_source(query, session_id, title):
    return not query or query == session_id or query.casefold() in (title or "").casefold()


async def inventory(request, *, view=None, exact=None, history=False, session="", byte_totals=None, roots=None, proofs=None):
    store = files._store(request)
    owner = files._user_id(request)
    await files._require_request_workspace_access(request, operation=READ, user_id=owner)
    async with open_pooled_read_ctx(store.db_path) as db:
        await db.execute("BEGIN")
        if proofs is None:
            proofs = await library.delivery_roots(db, owner, await library.deliveries(db, owner))
        if roots is None and view == "deliverables":
            roots = set(proofs)
        if exact:
            async with db.execute("SELECT COALESCE(lineage_id,id) FROM artifacts WHERE id=? AND created_by=?", (exact, owner)) as cur:
                match = await cur.fetchone()
            if not match:
                raise files._file_not_found()
            roots = {match[0]}
        raw = await library.versions(db, owner, roots=roots)
    scopes = await files._artifact_scopes(request, store, [r.art for r in raw])
    access = await files._access_request(request, owner)
    verdicts = {}

    async def allowed(scope):
        if scope not in verdicts:
            try:
                await files._require_artifact_scope_access(access, scope, user_id=owner, operation=READ)
            except HTTPException as exc:
                if exc.status_code not in (403, 404):
                    raise
                verdicts[scope] = False
            else:
                verdicts[scope] = True
        return verdicts[scope]

    groups = defaultdict(list)
    for r in raw:
        if not await allowed(scopes[r.art.id]):
            continue
        if r.art.producer_kind == "template_document_v1":
            try:
                await files._require_artifact_visible(request, store, r.art, user_id=owner, operation=READ)
            except HTTPException as exc:
                if exc.status_code in (403, 404):
                    continue
                raise
        if byte_totals is not None:
            bucket = "trash" if r.art.deleted_at else "run" if r.art.kind in library.RUNTIME else "assets"
            byte_totals[bucket] += r.art.size
        groups[r.art.lineage_root].append(r)
    out = []
    for root, group in groups.items():
        live = [r for r in group if not r.art.deleted_at]
        deleted = defaultdict(list)
        for r in group:
            if r.art.deleted_at:
                deleted[r.art.deleted_at].append(r)
        batches = ([live] if live else []) + list(deleted.values())
        target = next((r for r in group if r.art.id == exact), None) if exact else None
        if exact:
            if not target:
                raise files._file_not_found()
            batches = [deleted[target.art.deleted_at] if target.art.deleted_at else live]
        for members in batches:
            newest = target or max(members, key=lambda r: (r.art.version_no or 1, r.art.id))
            candidates = members if history else [newest]
            eligible = []
            for proof in proofs.get(root, ()):
                if not matches_source(session, proof.session_id, proof.title):
                    continue
                if proof.workspace_id and await allowed(files._ArtifactScope("space", proof.workspace_id, False)):
                    # An existing, hidden version cannot supply evidence about its visible sibling.
                    if not proof.pruned and not any(r.art.id == proof.artifact_id for r in group):
                        continue
                    eligible.append(proof)
            delivery = max(eligible, key=lambda d: (d.delivered_at, d.session_id), default=None)
            sent = False
            for ws in {ws for member in members for ws in member.sent_spaces if ws}:
                if await allowed(files._ArtifactScope("space", ws, False)):
                    sent = True
                    break
            for r in candidates:
                a = r.art
                views = {"all"}
                if delivery:
                    views.add("deliverables")
                if a.kind == "user_upload" or sent or a.source in {"kb_upload", "kb_bundle_upload"}:
                    views.add("uploads")
                if a.kind in library.RUNTIME:
                    views.add("run")
                if a.deleted_at:
                    views = {"trash"}
                if view and view not in views:
                    continue
                origin = {}
                if r.origin_space and await allowed(files._ArtifactScope("space", r.origin_space, False)):
                    origin = {"session_id": r.session_id, "session_title": r.session_title}
                if delivery and (view == "deliverables" or exact):
                    origin = {"session_id": delivery.session_id, "session_title": delivery.title}
                stamp = a.deleted_at or (delivery.delivered_at if view == "deliverables" and delivery else a.created_at)
                out.append(Entry(r, members, delivery, views, origin, stamp, scopes[a.id].workspace_id, sent))
    if exact and not out:
        raise files._file_not_found()
    return out


async def overlays(request, entries, *, strict=False):
    ids = list(dict.fromkeys(m.art.id for e in entries for m in e.members))
    members, status, unknown = set(), {}, set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        try:
            batch, states = await files._kb_overlays(request, chunk, require_membership=True)
        except HTTPException as exc:
            if strict or exc.status_code != 503:
                raise
            unknown.update(chunk)
        else:
            members.update(batch)
            status.update(states)
    return members, status, unknown


async def serialize(request, entries):
    kb, status, unknown = await overlays(request, entries)
    trust = await files._export_trust_projection(request, files._store(request), [e.row.art for e in entries])
    out = []
    for e in entries:
        a = e.row.art
        member_ids = [m.art.id for m in e.members]
        indexed = next((aid for aid in [a.id, *member_ids] if aid in status), None)
        in_kb = any(aid in kb for aid in member_ids)
        dto = files._art_to_dto(
            a,
            0,
            in_kb=in_kb,
            kb=status.get(indexed),
            origin_session=e.source,
            lineage_canvas_ref=any(m.art.canvas_id for m in e.members),
            trust_override=trust.get(a.id),
        ).model_dump()
        dto.update(
            lineage_id=a.lineage_root,
            version_count=len(member_ids),
            kb_outdated=in_kb and a.id not in kb,
            kb_artifact_id=indexed,
            uncertain_upload=a.kind == "user_upload"
            and not e.sent
            and not e.source.get("session_id")
            and a.source not in {"kb_upload", "kb_bundle_upload"},
            delivered_at=e.delivery.delivered_at if e.delivery else None,
            recover_until=library.recover_until(a.deleted_at),
            views=sorted(e.views),
            kb_known=in_kb or not any(aid in unknown for aid in member_ids),
            kept=e.row.kept,
            pinned=e.row.pinned,
            workspace_id=e.space,
        )
        out.append(dto)
    return out


@router.get("")
async def listing(request: Request, filters: Annotated[Filters, Query()]):
    owner = files._user_id(request)
    await files._require_request_workspace_access(request, operation=READ, user_id=owner)

    def matches(e):
        art = e.row.art
        stamp = library._parse_iso_utc(e.stamp)
        return (
            (not filters.q or filters.q.casefold() in art.filename.casefold() or filters.q == art.id)
            and (not filters.workspace_id or filters.workspace_id == e.space)
            and matches_source(filters.session_id, e.source.get("session_id"), e.source.get("session_title"))
            and (not filters.category or filters.category == art.category)
            and (not filters.mime_family or filters.mime_family == mime_to_family(art.mime))
            and (not filters.since or stamp and stamp.timestamp() >= filters.since.timestamp())
            and (not filters.until or stamp and stamp.timestamp() < filters.until.timestamp())
        )

    def key(e):
        return (e.row.art.size if filters.sort == "size" else e.stamp, e.row.art.lineage_root)

    boundary = None
    digest = hashlib.sha256(filters.model_dump_json(exclude={"cursor", "limit"}).encode()).hexdigest()[:16]
    if filters.cursor:
        try:
            marker, value, root = json.loads(base64.urlsafe_b64decode(filters.cursor))
            if marker != digest or not isinstance(root, str) or not isinstance(value, int if filters.sort == "size" else str):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "Invalid file cursor for the current filters") from exc
        boundary = (value, root)
    async with open_pooled_read_ctx(files._store(request).db_path) as db:
        await db.execute("BEGIN")
        proofs = await library.delivery_roots(db, owner, await library.deliveries(db, owner))
        # Sort lightweight lineage bounds first. A denied newer version can only lower the
        # actual representative's key. Stop as soon as the next upper bound cannot enter the page.
        if filters.view == "deliverables" and filters.sort == "date":
            bounds = sorted(((max(d.delivered_at for d in ds), root) for root, ds in proofs.items()), reverse=True)
        else:
            column = "size" if filters.sort == "size" else "deleted_at" if filters.view == "trash" else "created_at"
            trash = "IS NOT NULL" if filters.view == "trash" else "IS NULL"
            query = f"""SELECT MAX({column}),COALESCE(lineage_id,id) AS root FROM artifacts
                WHERE created_by=? AND status='ready' AND deleted_at {trash} GROUP BY root"""
            args = [owner]
            if boundary is not None:
                query += f" HAVING (MIN({column}),root)<(?,?)"
                args.extend(boundary)
            async with db.execute(query + " ORDER BY 1 DESC,2 DESC", args) as cur:
                bounds = await cur.fetchall()
    entries = []
    for offset in range(0, len(bounds), 200):
        if len(entries) > filters.limit and tuple(bounds[offset]) <= key(entries[-1]):
            break
        roots = {root for _, root in bounds[offset : offset + 200]}
        if filters.view == "deliverables":
            roots.intersection_update(proofs)
        chunk = await inventory(request, view=filters.view, session=filters.session_id, roots=roots, proofs=proofs)
        chunk = [e for e in chunk if matches(e) and (boundary is None or key(e) < boundary)]
        if filters.in_kb is not None:
            members, _, _ = await overlays(request, chunk, strict=True)
            chunk = [e for e in chunk if any(m.art.id in members for m in e.members) == filters.in_kb]
        entries = sorted([*entries, *chunk], key=key, reverse=True)[: filters.limit + 1]
    page = entries[: filters.limit]
    cursor = base64.urlsafe_b64encode(json.dumps([digest, *key(page[-1])]).encode()).decode() if len(entries) > len(page) else None
    items = await serialize(request, page)
    if items and (filters.retention or filters.view == "run"):
        from bowhead_core.file_retention import evaluate

        store = files._store(request)
        async with open_pooled_read_ctx(store.db_path) as db:
            await db.execute("BEGIN")
            facts = await evaluate(
                db,
                getattr(request.app.state, "kb_files_store", None),
                owner=files._user_id(request),
                ids=[i["file_id"] for i in items],
                include_trash=True,
            )
        by_id = {row.id: row for row in facts}
        for item in items:
            fact = by_id.get(item["file_id"])
            item["retention_reason"] = fact.reason if fact else "retained"
            item["retention_days"] = fact.days if fact else None
    return {"items": items, "next_cursor": cursor}


@router.get("/summary")
async def summary(request: Request):
    counts = Counter()
    sizes = Counter()
    for e in await inventory(request, byte_totals=sizes):
        counts.update(e.views)
    return {"counts": counts, "bytes": sizes}


@router.get("/items/{artifact_id}")
async def item(artifact_id: str, request: Request):
    return (await serialize(request, await inventory(request, exact=artifact_id)))[0]


@router.get("/items/{artifact_id}/versions")
async def versions(artifact_id: str, request: Request):
    entries = await inventory(request, exact=artifact_id, history=True)
    return await serialize(request, sorted(entries, key=lambda e: e.row.art.version_no or 1, reverse=True))


@router.get("/items/{artifact_id}/usages", response_model=files.FileUsagesDTO)
async def usages(artifact_id: str, request: Request):
    owner = files._user_id(request)
    await inventory(request, exact=artifact_id)
    access = await files._access_request(request, owner)
    async with (
        open_pooled_read_ctx(files._store(request).db_path) as db,
        db.execute(
            """SELECT s.workspace_id,s.id,s.title,t.id,t.seq,t.user_text,t.started_at
            FROM turn_attachments a JOIN turns t ON t.id=a.turn_id JOIN sessions s ON s.id=t.session_id
            WHERE a.artifact_id=? AND s.user_id=? AND s.deleted_at IS NULL ORDER BY t.started_at DESC""",
            (artifact_id, owner),
        ) as cur,
    ):
        rows = await cur.fetchall()
    visible = {}
    result = []
    for ws, sid, title, turn, seq, prompt, stamp in rows:
        if not ws:
            continue
        if ws not in visible:
            try:
                await files._require_artifact_scope_access(access, files._ArtifactScope("space", ws, False), user_id=owner, operation=READ)
            except HTTPException as exc:
                if exc.status_code not in (403, 404):
                    raise
                visible[ws] = False
            else:
                visible[ws] = True
        if visible[ws]:
            result.append(
                files.FileUsageEntryDTO(session_id=sid, session_title=title, turn_id=turn, turn_seq=seq or 0, user_text=prompt or "", used_at=stamp)
            )
    return files.FileUsagesDTO(usages=result)


class Keep(BaseModel):
    kept: bool


@router.put("/items/{artifact_id}/keep", dependencies=[Depends(require_not_readonly)])
async def keep(artifact_id: str, body: Keep, request: Request):
    store = files._store(request)
    owner = files._user_id(request)
    art = await store.get(artifact_id, check_created_by=owner, reject_null_owner=True)
    if not art:
        raise files._file_not_found()
    await files._require_artifact_visible(request, store, art, operation=SpaceOperation.WRITE, user_id=owner)

    async def write(db):
        cur = await db.execute(
            """INSERT INTO file_retention_state(artifact_id,kept)
           SELECT id,? FROM artifacts WHERE id=? AND created_by=? AND deleted_at IS NULL AND status='ready'
           ON CONFLICT(artifact_id) DO UPDATE SET kept=excluded.kept""",
            (body.kept, artifact_id, owner),
        )
        if cur.rowcount != 1:
            raise files._file_not_found()

    await run_sqlite_authority_write(store.db_path, write)
    return {"kept": body.kept}
