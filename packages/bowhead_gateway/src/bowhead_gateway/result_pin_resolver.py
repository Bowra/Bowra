"""Resolve a person's sidebar result pins into what they can open now.

docs/sprints/result-pins-2026-11-24/tech-design.md §4.2. Every step is one batched read: checking pins
one by one took 353–394 ms for 100 pins in the spike, the batched form 7–8 ms. Read access is decided by
the rules the result's own pages apply today, so a pin never shows what its page would refuse:

- an own file: ``_history_visible_ids`` (Space ``READ_HISTORY``), as the Files list;
- a file shared with the person: ``_read_shared_html``, as the shared-file viewer;
- a report: ``_can_read_canvas`` (owner or report share), as opening the report.

A pin whose target is gone, unreadable, empty or in another Space is left out and counted in ``hidden``;
its row stays, so the pin returns when the cause is undone (trash restored, access regained).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import HTTPException, Request

ResultPinKind = Literal["file", "report"]
OpensIn = Literal["session", "files", "shared_view"]


@dataclass(frozen=True, slots=True)
class ResolvedResultPin:
    kind: ResultPinKind
    pin_id: str
    title: str
    session_id: str | None
    opens: OpensIn
    changed_at: str
    pinned_at: str
    artifact_id: str | None = None
    mime: str | None = None
    version_no: int | None = None
    first_section_title: str | None = None
    created_at: str | None = None

    def dto(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "pin_id": self.pin_id,
            "title": self.title,
            "session_id": self.session_id,
            "opens": self.opens,
            "changed_at": self.changed_at,
            "pinned_at": self.pinned_at,
        }
        if self.kind == "file":
            out.update(artifact_id=self.artifact_id, mime=self.mime, version_no=self.version_no)
        else:
            out.update(first_section_title=self.first_section_title, created_at=self.created_at)
        return out


@dataclass(slots=True)
class ResultPinListing:
    items: list[ResolvedResultPin] = field(default_factory=list)
    hidden: Counter[str] = field(default_factory=Counter)


async def resolve_result_pins(
    request: Request,
    *,
    user_id: str,
    rows: list[tuple[str, str, str]],
    workspace_id: str | None,
) -> ResultPinListing:
    """Resolve ``(kind, target_id, pinned_at)`` rows; items come newest change first."""
    listing = ResultPinListing()
    file_rows = [(target, pinned) for kind, target, pinned in rows if kind == "file"]
    report_rows = [(target, pinned) for kind, target, pinned in rows if kind == "report"]
    if file_rows:
        await _resolve_files(request, user_id=user_id, rows=file_rows, workspace_id=workspace_id, listing=listing)
    if report_rows:
        await _resolve_reports(request, user_id=user_id, rows=report_rows, workspace_id=workspace_id, listing=listing)
    listing.items.sort(key=lambda item: (item.changed_at, item.pinned_at), reverse=True)
    return listing


async def _resolve_files(
    request: Request,
    *,
    user_id: str,
    rows: list[tuple[str, str]],
    workspace_id: str | None,
    listing: ResultPinListing,
) -> None:
    from bowhead_gateway.routes import files as files_routes

    store = files_routes._store(request)
    latest = await store.latest_live_versions([root for root, _ in rows])
    artifacts = {art.id: art for art in await store.get_many(list(latest.values()))}
    own = [art for art in artifacts.values() if art.created_by == user_id]
    visible_own = await files_routes._history_visible_ids(request, store, own, user_id=user_id) if own else set()
    conversations = await store.conversation_ids(list(artifacts))
    owners = await request.app.state.session_store.live_session_owners(list(set(conversations.values())))

    for root, pinned_at in rows:
        art = artifacts.get(latest.get(root, ""))
        if art is None:
            listing.hidden["gone"] += 1
            continue
        if art.created_by == user_id:
            if art.id not in visible_own:
                listing.hidden["unreadable"] += 1
                continue
            if workspace_id and art.workspace_id and art.workspace_id != workspace_id:
                listing.hidden["other_space"] += 1
                continue
        else:
            # Shared with the person: the shared-file viewer's own rule, keyed by the lineage root.
            # Shared results show in every Space (PRD D6).
            try:
                art = await files_routes._read_shared_html(request, root, viewer_id=user_id)
            except HTTPException:
                listing.hidden["unreadable"] += 1
                continue
        session_id = conversations.get(art.id)
        openable = session_id is not None and owners.get(session_id) == user_id
        opens: OpensIn = "session" if openable else ("files" if art.created_by == user_id else "shared_view")
        listing.items.append(
            ResolvedResultPin(
                kind="file",
                pin_id=root,
                artifact_id=art.id,
                title=art.filename,
                mime=art.mime,
                version_no=art.version_no or 1,
                session_id=session_id if openable else None,
                opens=opens,
                changed_at=art.created_at,
                pinned_at=pinned_at,
            )
        )


async def _resolve_reports(
    request: Request,
    *,
    user_id: str,
    rows: list[tuple[str, str]],
    workspace_id: str | None,
    listing: ResultPinListing,
) -> None:
    from bowhead_gateway.routes import canvas as canvas_routes

    facts = await request.app.state.canvas_store.report_pin_rows([target for target, _ in rows])
    live = await request.app.state.session_store.live_session_owners([str(row["session_id"]) for row in facts.values()])
    shared_sessions: frozenset[str] | None = None
    verdicts: dict[tuple[str, bool], bool] = {}

    for target, pinned_at in rows:
        row = facts.get(target)
        if row is None:
            listing.hidden["gone"] += 1
            continue
        session_id = str(row["session_id"])
        if session_id not in live or not int(row["report_sections"] or 0):
            # Conversation deleted, or the canvas holds no report (cleared, code only).
            listing.hidden["gone"] += 1
            continue
        is_owner = row["user_id"] == user_id
        if not is_owner:
            if shared_sessions is None:
                shared_sessions = await canvas_routes._shared_report_session_ids(request, user_id)
            if session_id not in shared_sessions:
                listing.hidden["unreadable"] += 1
                continue
        # The same verdict holds for every report of one Space and one access path.
        key = (str(row["workspace_id"] or ""), is_owner)
        if key not in verdicts:
            canvas = SimpleNamespace(user_id=row["user_id"], workspace_id=row["workspace_id"], session_id=session_id)
            verdicts[key] = await canvas_routes._can_read_canvas(request, canvas)
        if not verdicts[key]:
            listing.hidden["unreadable"] += 1
            continue
        if is_owner and workspace_id and row["workspace_id"] and row["workspace_id"] != workspace_id:
            listing.hidden["other_space"] += 1
            continue
        listing.items.append(
            ResolvedResultPin(
                kind="report",
                pin_id=target,
                title=str(row["title"] or ""),
                session_id=session_id,
                opens="session",
                changed_at=str(row["updated_at"]),
                pinned_at=pinned_at,
                first_section_title=row["first_section_title"],
                created_at=str(row["created_at"]),
            )
        )


async def remove_gone_result_pins(state: Any) -> dict[str, int]:
    """Remove every person's pins whose file was purged or whose report was deleted.

    Run by the nightly artifact GC after its hard deletion. Each side reads its own domain file and the
    execution side deletes, so no write crosses domains. A file in the trash still has rows and keeps its
    pin; it only hides until restored.
    """
    sessions = state.session_store
    removed = {"files": 0, "reports": 0}
    roots = await sessions.result_pin_targets(target_kind="file")
    if roots and getattr(state, "artifact_store", None) is not None:
        gone = await state.artifact_store.lineage_roots_without_rows(roots)
        removed["files"] = await sessions.delete_result_pins(target_kind="file", target_ids=gone)
    canvas_ids = await sessions.result_pin_targets(target_kind="report")
    if canvas_ids and getattr(state, "canvas_store", None) is not None:
        present = await state.canvas_store.report_pin_rows(canvas_ids)
        removed["reports"] = await sessions.delete_result_pins(
            target_kind="report", target_ids=[canvas_id for canvas_id in canvas_ids if canvas_id not in present]
        )
    return removed
