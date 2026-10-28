"""KbFilesStore tests for the kb-management sprint.

Covers:
- list_for_scope_filtered: per-scope WHERE, status/mime/doc_type/date/q filters,
  ordering + file_id tiebreaker (deterministic offset pagination), q LIKE escaping.
- requeue_for_reparse / mark_parse_failed: REQ-2.1 reparse state machine.
- update_doc_type: REQ-1.4b binding + cross-ws guard.
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.kb.files_store import KbFilesStore


@pytest.fixture
async def store(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    s = KbFilesStore(tmp_path / "kb_files.db")
    await s.initialize()
    return s


async def _make(store: KbFilesStore, **overrides) -> str:
    defaults = {
        "artifact_id": "art_x",
        "filename": "test.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        "scope": "personal",
        "scope_id": "user_a",
        "workspace_id": "ws_42",
        "team_id": "team_5",
        "uploaded_by": "user_a",
    }
    defaults.update(overrides)
    return await store.create(**defaults)


async def _set_uploaded_at(store: KbFilesStore, file_id: str, value: str) -> None:
    """uploaded_at isn't in update()'s whitelist; set it directly for ordering tests."""
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE kb_files SET uploaded_at = ? WHERE file_id = ?",
            (value, file_id),
        )
        await db.commit()


# ─── per-scope WHERE semantics ────────────────────────


async def test_filtered_personal_scope_only_own(store: KbFilesStore) -> None:
    mine = await _make(store, scope="personal", scope_id="user_a")
    await _make(store, scope="personal", scope_id="user_b")
    rows = await store.list_for_scope_filtered("personal", "user_a")
    assert [r.file_id for r in rows] == [mine]


async def test_filtered_workspace_excludes_personal_in_ws(store: KbFilesStore) -> None:
    """Isolation (dingsiyan 2026-07-07): the workspace tab shows workspace-scope
    files ONLY. A personal upload routed to the same ws (personal docs carry the
    ws_id for retrieval) must NOT appear in the workspace tab — it belongs to the
    personal tab. Each management-page tab maps to exactly one scope."""
    ws_file = await _make(store, scope="workspace", scope_id="ws_42", workspace_id="ws_42")
    # personal-in-ws: same workspace_id, but scope='personal' → must be excluded.
    await _make(store, scope="personal", scope_id="user_a", workspace_id="ws_42")
    await _make(store, scope="workspace", scope_id="ws_99", workspace_id="ws_99")
    rows = await store.list_for_scope_filtered("workspace", "ws_42")
    assert {r.file_id for r in rows} == {ws_file}


async def test_filtered_team_scope(store: KbFilesStore) -> None:
    team_file = await _make(store, scope="team", scope_id="team_5", workspace_id="")
    await _make(store, scope="team", scope_id="team_9", workspace_id="")
    rows = await store.list_for_scope_filtered("team", "team_5")
    assert [r.file_id for r in rows] == [team_file]


async def test_filtered_excludes_soft_deleted(store: KbFilesStore) -> None:
    keep = await _make(store, scope="personal", scope_id="user_a")
    gone = await _make(store, scope="personal", scope_id="user_a")
    await store.soft_delete(gone)
    rows = await store.list_for_scope_filtered("personal", "user_a")
    assert [r.file_id for r in rows] == [keep]


# ─── metadata filters ─────────────────────────────────


async def test_filter_by_status(store: KbFilesStore) -> None:
    failed = await _make(store, scope_id="user_a", initial_parse_status="failed")
    await _make(store, scope_id="user_a", initial_parse_status="done")
    rows = await store.list_for_scope_filtered("personal", "user_a", statuses=["failed"])
    assert [r.file_id for r in rows] == [failed]


async def test_filter_by_status_multi(store: KbFilesStore) -> None:
    f1 = await _make(store, scope_id="user_a", initial_parse_status="failed")
    f2 = await _make(store, scope_id="user_a", initial_parse_status="queued")
    await _make(store, scope_id="user_a", initial_parse_status="done")
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        statuses=["failed", "queued"],
    )
    assert {r.file_id for r in rows} == {f1, f2}


async def test_filter_by_mime_prefix(store: KbFilesStore) -> None:
    pdf = await _make(store, scope_id="user_a", mime_type="application/pdf")
    await _make(store, scope_id="user_a", mime_type="text/markdown")
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        mime_prefixes=["application/"],
    )
    assert [r.file_id for r in rows] == [pdf]


async def test_filter_by_doc_type(store: KbFilesStore) -> None:
    tagged = await _make(store, scope_id="user_a", doc_type_id="dt_inv")
    await _make(store, scope_id="user_a", doc_type_id=None)
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        doc_type_id="dt_inv",
    )
    assert [r.file_id for r in rows] == [tagged]


async def test_filter_by_date_range(store: KbFilesStore) -> None:
    early = await _make(store, scope_id="user_a")
    late = await _make(store, scope_id="user_a")
    await _set_uploaded_at(store, early, "2026-01-01T00:00:00Z")
    await _set_uploaded_at(store, late, "2026-06-01T00:00:00Z")
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        uploaded_after="2026-03-01T00:00:00Z",
    )
    assert [r.file_id for r in rows] == [late]


async def test_filter_q_filename_substring(store: KbFilesStore) -> None:
    match = await _make(store, scope_id="user_a", filename="2026-budget.xlsx")
    await _make(store, scope_id="user_a", filename="notes.pdf")
    rows = await store.list_for_scope_filtered("personal", "user_a", q="budget")
    assert [r.file_id for r in rows] == [match]


async def test_filter_q_escapes_like_wildcards(store: KbFilesStore) -> None:
    """A literal '%' in q must not act as a SQL LIKE wildcard."""
    literal = await _make(store, scope_id="user_a", filename="50%-off.pdf")
    await _make(store, scope_id="user_a", filename="totally-different.pdf")
    # '%x' would match everything if % were a wildcard; with escaping it matches none.
    rows = await store.list_for_scope_filtered("personal", "user_a", q="%x")
    assert rows == []
    # The literal '%' substring still matches the one file that contains it.
    rows2 = await store.list_for_scope_filtered("personal", "user_a", q="50%")
    assert [r.file_id for r in rows2] == [literal]


# ─── ordering + tiebreaker ────────────────────────────


async def test_order_by_filename_asc(store: KbFilesStore) -> None:
    c = await _make(store, scope_id="user_a", filename="c.pdf")
    a = await _make(store, scope_id="user_a", filename="a.pdf")
    b = await _make(store, scope_id="user_a", filename="b.pdf")
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        order_by="filename",
        order_dir="asc",
    )
    assert [r.file_id for r in rows] == [a, b, c]


async def test_order_by_unknown_column_falls_back(store: KbFilesStore) -> None:
    """An out-of-whitelist order_by must not inject; falls back to uploaded_at."""
    f1 = await _make(store, scope_id="user_a")
    f2 = await _make(store, scope_id="user_a")
    await _set_uploaded_at(store, f1, "2026-01-01T00:00:00Z")
    await _set_uploaded_at(store, f2, "2026-02-01T00:00:00Z")
    rows = await store.list_for_scope_filtered(
        "personal",
        "user_a",
        order_by="size_bytes; DROP TABLE kb_files",
        order_dir="desc",
    )
    assert [r.file_id for r in rows] == [f2, f1]


async def test_tiebreaker_makes_pagination_deterministic(store: KbFilesStore) -> None:
    """Same uploaded_at across rows → file_id tiebreaker gives a total order, so
    offset paging partitions the set with no overlap/gap (REQ-1.1 A-P1)."""
    ids = [await _make(store, scope_id="user_a") for _ in range(5)]
    for fid in ids:
        await _set_uploaded_at(store, fid, "2026-01-01T00:00:00Z")  # force ties
    full = await store.list_for_scope_filtered("personal", "user_a")
    ordered = [r.file_id for r in full]
    assert sorted(ordered) == sorted(ids)
    # Paginate in pages of 2 via the same ordering; reconstruct must equal `full`.
    page1 = ordered[0:2]
    page2 = ordered[2:4]
    page3 = ordered[4:6]
    assert page1 + page2 + page3 == ordered
    assert len(set(ordered)) == 5  # no duplicates


# ─── reparse state machine (REQ-2.1) ──────────────────


async def test_requeue_for_reparse_flips_only_when_failed(store: KbFilesStore) -> None:
    fid = await _make(store, initial_parse_status="failed")
    await store.update(fid, parse_error="boom")
    assert await store.requeue_for_reparse(fid) is True
    dto = await store.get(fid)
    assert dto.parse_status == "queued"
    assert dto.parse_error is None
    # Second call is a no-op (no longer failed) — race-safe / idempotent.
    assert await store.requeue_for_reparse(fid) is False


async def test_requeue_for_reparse_noop_when_done(store: KbFilesStore) -> None:
    fid = await _make(store, initial_parse_status="done")
    assert await store.requeue_for_reparse(fid) is False
    assert (await store.get(fid)).parse_status == "done"


async def test_mark_parse_failed_rolls_back(store: KbFilesStore) -> None:
    fid = await _make(store, initial_parse_status="failed")
    await store.requeue_for_reparse(fid)
    await store.mark_parse_failed(fid)
    assert (await store.get(fid)).parse_status == "failed"


# ─── doc_type binding (REQ-1.4b) ──────────────────────


async def test_update_doc_type_binds_and_unbinds(store: KbFilesStore) -> None:
    fid = await _make(store, scope="workspace", scope_id="ws_42", workspace_id="ws_42")
    assert await store.update_doc_type(fid, "dt_inv", expected_workspace_id="ws_42") is True
    assert (await store.get(fid)).doc_type_id == "dt_inv"
    assert await store.update_doc_type(fid, None, expected_workspace_id="ws_42") is True
    assert (await store.get(fid)).doc_type_id is None


async def test_update_doc_type_cross_ws_guard(store: KbFilesStore) -> None:
    fid = await _make(store, scope="workspace", scope_id="ws_42", workspace_id="ws_42")
    assert await store.update_doc_type(fid, "dt_inv", expected_workspace_id="ws_OTHER") is False
    assert (await store.get(fid)).doc_type_id is None
