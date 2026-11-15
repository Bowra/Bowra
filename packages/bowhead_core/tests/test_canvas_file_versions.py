"""Canvas HTML sections follow a file's newest version (file-versions design R4, batch B2).

A producer writing a new version updates the owner's HTML sections that embed an older
version: the stored section names the new version, its rev is bumped, and the Turn's event
stream carries canvas.section_patched, so canvas reads, exports and replay agree.
"""

from importlib import import_module
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_file_versions import follow_new_versions_on_canvas
from bowhead_core.canvas_store import CanvasStore

pytestmark = pytest.mark.asyncio


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> bool:
        self.events.append(event)
        return True


@pytest.fixture
async def stores(tmp_path):
    db_path = tmp_path / "core.db"
    artifacts = ArtifactStore(db_path=str(db_path), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await artifacts.initialize()
    canvases = CanvasStore(str(db_path))
    await canvases.initialize()
    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT);"
            "CREATE TABLE IF NOT EXISTS artifact_purge_claims (artifact_id TEXT PRIMARY KEY);"
        )
        await db.executemany("INSERT OR IGNORE INTO turns(id, session_id) VALUES (?, ?)", [("t1", "s1"), ("t2", "s1"), ("t3", "s1")])
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.commit()
    return artifacts, canvases


async def _put(store: ArtifactStore, turn: str, target: str | None = None, **overrides):
    fields = {
        "content": f"<html>{turn}</html>".encode(),
        "filename": "report.html",
        "mime": "text/html",
        "created_by": "u",
        "source": "python_sandbox",
        "kind": "deliverable",
        "created_by_turn": turn,
        "session_id": "s1",
        "workspace_id": "w1",
        **overrides,
    }
    return await store.put_bytes(version_of_target=target, **fields)


async def _html_section(canvases: CanvasStore, artifact_id: str, *, session: str = "s1"):
    canvas = await canvases.ensure_canvas(session_id=session, workspace_id="w1", user_id="u")
    payload = {"artifact_id": artifact_id, "title": "Report", "height": 480, "sandbox": True, "unverified": False, "data": {"rows": 3}}
    return await canvases.append_section(canvas_id=canvas.id, kind="html_artifact", payload=payload, by_turn_id="t1")


def _ctx(artifacts, canvases, turn="t2", session="s1"):
    return SimpleNamespace(artifact_store=artifacts, canvas_store=canvases, user_id="u", turn_id=turn, session_id=session, parent_bus=_Bus())


async def test_an_embedded_html_file_follows_each_new_version(stores) -> None:
    artifacts, canvases = stores
    first = await _put(artifacts, "t1")
    section = await _html_section(canvases, first.id)
    second = await _put(artifacts, "t2", first.id)
    ctx = _ctx(artifacts, canvases)
    assert await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": second.id}]}, ctx) == [section.id]
    [stored] = [s for s in await canvases.list_sections(section.canvas_id) if s.id == section.id]
    assert (stored.payload["artifact_id"], stored.payload["version_no"], stored.payload["provenance_version_no"]) == (second.id, 2, 1)
    assert stored.payload["data"] == {"rows": 3}, "Generation provenance stays, labelled with its version"
    assert stored.rev == section.rev + 1, "The rev bump lets snapshot validation and exports see the change"
    [event] = ctx.parent_bus.events
    assert event.event_type == "canvas.section_patched" and event.payload["patch"]["artifact_id"] == second.id
    assert event.payload["rev"] == stored.rev
    assert await canvases.section_ids_touched_by_turn("t2") == set(), "Following is not the Turn editing the report"
    assert ctx.parent_bus._canvas_followed_file_version is True, "This session's Canvas shows the new version (§9)"

    third = await _put(artifacts, "t3", second.id)
    await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": third.id}]}, _ctx(artifacts, canvases, "t3"))
    [stored] = [s for s in await canvases.list_sections(section.canvas_id) if s.id == section.id]
    assert (stored.payload["artifact_id"], stored.payload["version_no"], stored.payload["provenance_version_no"]) == (third.id, 3, 1)


async def test_an_oversized_version_is_not_embedded_and_the_section_says_why(stores, monkeypatch) -> None:
    artifacts, canvases = stores
    first = await _put(artifacts, "t1")
    section = await _html_section(canvases, first.id)
    second = await _put(artifacts, "t2", first.id, content=b"<html>" + b"x" * 400 + b"</html>")
    monkeypatch.setattr("bowhead_core.html_preview_policy.html_preview_limit_bytes", lambda: 100)
    await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": second.id}]}, _ctx(artifacts, canvases))
    [stored] = [s for s in await canvases.list_sections(section.canvas_id) if s.id == section.id]
    assert stored.payload["artifact_id"] == first.id
    assert stored.payload["version_note"] == {"reason": "too_large", "version_no": 2, "maximum_bytes": 100}


async def test_only_live_html_sections_of_the_same_file_follow(stores) -> None:
    artifacts, canvases = stores
    first = await _put(artifacts, "t1")
    other = await _put(artifacts, "t1", filename="other.html", content=b"<html>other</html>")
    kept = await _html_section(canvases, first.id)
    unrelated = await _html_section(canvases, other.id)
    folded = await _html_section(canvases, first.id)
    await canvases.mark_sections_superseded(section_ids=[folded.id], superseded_by_turn="t2")
    table = await _put(artifacts, "t1", filename="rows.csv", mime="text/csv", content=b"a,b")
    table_v2 = await _put(artifacts, "t2", table.id, filename="rows.csv", mime="text/csv", content=b"a,b\n1,2")
    second = await _put(artifacts, "t2", first.id)
    ctx = _ctx(artifacts, canvases)
    patched = await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": table_v2.id}, {"artifact_id": second.id}]}, ctx)
    assert patched == [kept.id]
    sections = {s.id: s for s in await canvases.list_sections(kept.canvas_id, include_hidden=True)}
    assert sections[unrelated.id].payload["artifact_id"] == other.id
    assert sections[folded.id].payload["artifact_id"] == first.id


async def test_another_users_canvas_is_never_changed(stores) -> None:
    """Following is scoped to the file owner's canvases, even if a foreign section names the same ID."""
    artifacts, canvases = stores
    first = await _put(artifacts, "t1")
    foreign_canvas = await canvases.ensure_canvas(session_id="s_other", workspace_id="w1", user_id="v")
    foreign = await canvases.append_section(canvas_id=foreign_canvas.id, kind="html_artifact", payload={"artifact_id": first.id, "title": "x"})
    second = await _put(artifacts, "t2", first.id)
    ctx = _ctx(artifacts, canvases)
    assert await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": second.id}]}, ctx) == []
    assert not getattr(ctx.parent_bus, "_canvas_followed_file_version", False)
    [stored] = await canvases.list_sections(foreign_canvas.id)
    assert stored.id == foreign.id and stored.payload["artifact_id"] == first.id


async def test_following_on_another_sessions_canvas_does_not_mark_this_turn(stores) -> None:
    """Revision-target design §9: only this session's Canvas showing the new version keeps a pointer to it."""
    artifacts, canvases = stores
    first = await _put(artifacts, "t1")
    await _html_section(canvases, first.id, session="s_other")
    second = await _put(artifacts, "t2", first.id)
    ctx = _ctx(artifacts, canvases)
    assert len(await follow_new_versions_on_canvas({"output_artifacts": [{"artifact_id": second.id}]}, ctx)) == 1
    assert not getattr(ctx.parent_bus, "_canvas_followed_file_version", False)
