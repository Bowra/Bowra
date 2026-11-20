"""Hot file, Results, report, document and event-replay reads take no cross-file authority lease.

A write-capable connection that reads another domain file takes that file's shared
lease. On 116 such reads waited behind, and kept out, core permission and settings
writes (1.7.49: two Space saves answered 500). These reads now use the read-only pool.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import DocumentStore
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from test_sqlite_domain_stores import domain_directory as domain_directory

from bowhead_core import sqlite_domain_runtime

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _stores(domain_directory: Path) -> tuple[ArtifactStore, CanvasStore, str]:
    core = str(domain_directory / "bowhead.db")
    sessions = SessionStore(core)
    await sessions.ensure_session(session_id="s_hot", workspace_id="ws_1", user_id="u_1")
    await sessions.record_turn(turn_id="t_hot", session_id="s_hot", user_text="hot reads")
    artifacts = ArtifactStore(core, LocalBackend(domain_directory / "hot-blobs"))
    canvases = CanvasStore(core)
    await artifacts.initialize()
    await canvases.initialize()
    return artifacts, canvases, core


async def _hot_reads(artifacts: ArtifactStore, canvases: CanvasStore, artifact_id: str, canvas_id: str) -> None:
    assert await artifacts.get(artifact_id) is not None
    assert [item.id for item in await artifacts.get_many([artifact_id])] == [artifact_id]
    snapshot = await artifacts.results_high_water(session_id="s_hot", user_id="u_1")
    await artifacts.results_snapshot_membership_digest(session_id="s_hot", user_id="u_1", snapshot=snapshot)
    page = await artifacts.query_results_for_session(session_id="s_hot", user_id="u_1", snapshot=snapshot, normalized_query="")
    assert page is not None
    await artifacts.list_result_versions(session_id="s_hot", user_id="u_1", artifact_id=artifact_id)
    assert (await canvases.get_canvas_by_session("s_hot")).id == canvas_id
    assert await canvases.get_canvas(canvas_id) is not None
    assert await canvases.read_canvas_projection_state(canvas_id) is not None
    assert [row["id"] for row in await canvases.list_for_user(user_id="u_1", workspace_id="ws_1")] == [canvas_id]


@contextmanager
def _core_lease_held(core: str):
    """Hold core's exclusive authority lease with a fenced core update (a Space ``status`` change).

    0230 unfenced ``workspaces.revision``/name/description, so a metadata save no longer takes the lease; the
    holder asserts it really holds core exclusively, so this helper cannot silently stop contending.
    """
    held, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def holder() -> None:
        try:
            with connect_sync(core) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE workspaces SET status=CASE status WHEN 'active' THEN 'archived' ELSE 'active' END WHERE id='ws_1'").close()
                assert conn._leases.get("core") == 2, conn._leases
                held.set()
                release.wait(30)
                conn.rollback()
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test below
            failure.append(error)
            held.set()

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(10)
    try:
        assert not failure, failure
        yield
    finally:
        release.set()
        thread.join(10)


async def test_hot_reads_take_no_authority_lease(domain_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, canvases, _core = await _stores(domain_directory)
    artifact = await artifacts.put_bytes(
        content=b"a,b\n1,2\n", filename="t.csv", mime="text/csv", created_by="u_1", session_id="s_hot", created_by_turn="t_hot"
    )
    canvas = await canvases.ensure_canvas(session_id="s_hot", workspace_id="ws_1", user_id="u_1")
    acquired: list[tuple[str | None, str, int]] = []
    original = sqlite_domain_runtime.DomainConnection._acquire

    def recording(self, domain: str, mode: int) -> None:
        if mode > self._leases.get(domain, 0):
            acquired.append((self._domain, domain, mode))
        original(self, domain, mode)

    monkeypatch.setattr(sqlite_domain_runtime.DomainConnection, "_acquire", recording)
    await _hot_reads(artifacts, canvases, artifact.id, canvas.id)
    assert acquired == []


async def test_hot_reads_proceed_while_a_core_lease_is_held(domain_directory: Path) -> None:
    artifacts, canvases, core = await _stores(domain_directory)
    artifact = await artifacts.put_bytes(
        content=b"x", filename="held.txt", mime="text/plain", created_by="u_1", session_id="s_hot", created_by_turn="t_hot"
    )
    canvas = await canvases.ensure_canvas(session_id="s_hot", workspace_id="ws_1", user_id="u_1")
    with _core_lease_held(core):
        # A write-capable connection would wait up to its 60 s busy timeout here, or fail inside a transaction.
        with anyio.fail_after(5):
            await _hot_reads(artifacts, canvases, artifact.id, canvas.id)


async def test_a_committed_write_is_visible_to_the_next_read(domain_directory: Path) -> None:
    artifacts, canvases, _core = await _stores(domain_directory)
    artifact = await artifacts.put_bytes(
        content=b"fresh", filename="fresh.txt", mime="text/plain", created_by="u_1", session_id="s_hot", created_by_turn="t_hot"
    )
    assert (await artifacts.get(artifact.id)).filename == "fresh.txt"
    snapshot = await artifacts.results_high_water(session_id="s_hot", user_id="u_1")
    assert snapshot.high_water_artifact_id is not None
    canvas = await canvases.ensure_canvas(session_id="s_hot", workspace_id="ws_1", user_id="u_1")
    assert (await canvases.get_canvas_by_session("s_hot")).id == canvas.id
    with pytest.raises(sqlite3.DatabaseError):
        # The read-only pool still rejects writes: these blocks must stay pure reads.
        from bowhead_core._db import open_pooled_read_ctx

        async with open_pooled_read_ctx(artifacts.db_path) as db:
            await db.execute("UPDATE artifacts SET filename='nope' WHERE id=?", (artifact.id,))


async def _document_and_replay_reads(artifacts: ArtifactStore, core: str) -> None:
    scope = DocumentScope(team_id="team_1", workspace_id="ws_1", user_id="u_1")
    assert await DocumentStore(core).list_for_session(scope=scope, session_id="s_hot") == []
    assert await DocumentStore(core).get(scope=scope, job_id="doc_missing") is None
    with pytest.raises(DocumentRequestError):
        await DocumentDeliveryStore(artifacts).get(scope=scope, job_id="doc_missing")
    team_id, session_id, _version = await TerminalProtocolStore(core).read_persisted_turn_scope(turn_id="t_hot")
    assert (team_id, session_id) == ("team_1", "s_hot")


async def test_document_and_replay_reads_take_no_lease_and_proceed_while_core_is_held(
    domain_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts, _canvases, core = await _stores(domain_directory)
    acquired: list[tuple[str | None, str, int]] = []
    original = sqlite_domain_runtime.DomainConnection._acquire

    def recording(self, domain: str, mode: int) -> None:
        if mode > self._leases.get(domain, 0):
            acquired.append((self._domain, domain, mode))
        original(self, domain, mode)

    monkeypatch.setattr(sqlite_domain_runtime.DomainConnection, "_acquire", recording)
    await _document_and_replay_reads(artifacts, core)
    assert acquired == []
    monkeypatch.undo()
    with _core_lease_held(core), anyio.fail_after(5):
        # Inside their read transactions these reads failed at once on a held core lease before.
        await _document_and_replay_reads(artifacts, core)
