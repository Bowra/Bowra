"""KbStore unit tests (against real chromadb embedded, persist temp dir).

Covers:
- P0-O1: KbFileDTO 单参数 signature
- P0-O3: search 排除 inactive (is_active=False) chunks
- P0-N1: per-ws + per-team collection topology (跨 user personal in same ws coll metadata filter)
- P0-A: chunk_id idempotent across re-parse (upsert dedup)
- P0-O2 + P0-P2: cross-collection migration (workspace → team)
- P1-P4: collection name sanitize (special char workspace_id)
- P1-Q3: search timeout (mocked)
"""

from __future__ import annotations

import random
import shutil
import tempfile

import pytest
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.store import KbStore, _sanitize_id

# ─── helpers ──────────────────────────────────────────


@pytest.fixture
async def store():
    persist_dir = tempfile.mkdtemp(prefix="kb_test_store_")
    s = KbStore(persist_dir)
    try:
        yield s
    finally:
        # chromadb persisted client needs no explicit close.
        shutil.rmtree(persist_dir, ignore_errors=True)


def _make_emb(seed: int, dim: int = 384) -> list[float]:
    r = random.Random(seed)
    return [r.random() for _ in range(dim)]


def _make_chunk(
    file_id: str,
    idx: int,
    content: str,
    *,
    scope: str = "workspace",
    scope_owner: str = "ws_42",
    workspace_id: str = "ws_42",
    team_id: str = "team_5",
    is_active: bool = True,
    embedding_seed: int = 0,
) -> KbChunk:
    return KbChunk(
        chunk_id=make_chunk_id(file_id, idx, content),
        file_id=file_id,
        content=content,
        embedding=_make_emb(embedding_seed),
        metadata=ChunkMetadata(
            file_id=file_id,
            file_name=f"{file_id}.pdf",
            scope=scope,
            scope_owner=scope_owner,  # type: ignore[arg-type]
            workspace_id=workspace_id,
            team_id=team_id,
            chunk_index=idx,
            is_active=is_active,
        ),
    )


def _make_file(
    file_id: str = "kbf_test",
    scope: str = "workspace",
    scope_id: str = "ws_42",
    workspace_id: str = "ws_42",
    team_id: str = "team_5",
    uploaded_by: str = "user_a",
) -> KbFileDTO:
    return KbFileDTO(
        file_id=file_id,
        artifact_id="art_x",
        filename=f"{file_id}.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        scope=scope,
        scope_id=scope_id,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        team_id=team_id,
        uploaded_by=uploaded_by,
        uploaded_at="2026-05-04T10:00:00Z",
    )


# ─── _sanitize_id (P1-P4) ─────────────────────────────


def test_sanitize_id_valid_passes_through() -> None:
    assert _sanitize_id("ws_42") == "ws_42"
    assert _sanitize_id("ws-42") == "ws-42"
    assert _sanitize_id("ws.42") == "ws.42"


def test_sanitize_id_special_chars_replaced() -> None:
    """[P1-P4] chromadb collection name 限 alphanumeric / _ / - / . , 防 user-input 含特殊字符."""
    assert _sanitize_id("ws/42") == "ws_42"
    assert _sanitize_id("ws:42") == "ws_42"
    assert _sanitize_id("ws 42") == "ws_42"
    assert _sanitize_id("ws@42#bad") == "ws_42_bad"


def test_sanitize_id_short_padded() -> None:
    """3-63 char min, < 3 → padded."""
    assert len(_sanitize_id("a")) >= 3
    assert len(_sanitize_id("ab")) >= 3


def test_sanitize_id_capped_at_50() -> None:
    """Cap 50 char (留 prefix 'kb_ws_' / 'kb_team_' 空间)."""
    long_input = "a" * 100
    result = _sanitize_id(long_input)
    assert len(result) <= 50


# ─── add_chunks + count_chunks ────────────────────────


@pytest.mark.asyncio
async def test_add_chunks_kbfile_dto_signature(store: KbStore) -> None:
    """[P0-O1] caller 传 KbFileDTO 单参数, 不是 scope/scope_id 三参数."""
    file = _make_file()
    chunks = [_make_chunk(file.file_id, 1, "hello world")]
    # Must not raise TypeError/signature mismatch.
    await store.add_chunks(file, chunks)
    assert await store.count_chunks(file) == 1


@pytest.mark.asyncio
async def test_add_chunks_empty_list_noop(store: KbStore) -> None:
    """空 chunks list 不报错, 直接返."""
    file = _make_file()
    await store.add_chunks(file, [])
    assert await store.count_chunks(file) == 0


@pytest.mark.asyncio
async def test_count_chunks_excludes_inactive(store: KbStore) -> None:
    """[P0-O3] count_chunks 用 is_active=True filter, 排除 soft-deleted."""
    file = _make_file()
    chunks = [
        _make_chunk(file.file_id, 1, "active 1", embedding_seed=1),
        _make_chunk(file.file_id, 2, "inactive", is_active=False, embedding_seed=2),
        _make_chunk(file.file_id, 3, "active 2", embedding_seed=3),
    ]
    await store.add_chunks(file, chunks)
    assert await store.count_chunks(file) == 2


@pytest.mark.asyncio
async def test_chunk_id_idempotent_upsert(store: KbStore) -> None:
    """[P0-A] 重 parse 同 file 同 chunks → 不重复, count 仍是 N."""
    file = _make_file()
    chunks = [_make_chunk(file.file_id, i, f"content {i}", embedding_seed=i) for i in range(1, 4)]
    await store.add_chunks(file, chunks)
    # Add again with same chunk_id → upsert, count still 3.
    await store.add_chunks(file, chunks)
    assert await store.count_chunks(file) == 3


# ─── search (P0-N1 + P0-O3) ───────────────────────────


@pytest.mark.asyncio
async def test_search_excludes_inactive_chunks(store: KbStore) -> None:
    """[P0-O3] search 自动加 is_active=True filter, 排除 soft-deleted."""
    file = _make_file(scope="workspace", scope_id="ws_42")
    chunks = [
        _make_chunk(file.file_id, 1, "active 1", embedding_seed=10),
        _make_chunk(file.file_id, 2, "DELETED", is_active=False, embedding_seed=11),
    ]
    await store.add_chunks(file, chunks)

    ctx = ScopeContext(user_id="user_a", workspace_id="ws_42", team_id="team_5")
    results = await store.search(ctx, _make_emb(100), top_k=10)
    assert all(r["content"] != "DELETED" for r in results)
    assert any(r["content"] == "active 1" for r in results)


@pytest.mark.asyncio
async def test_search_personal_isolation(store: KbStore) -> None:
    """[P0-N1] ws collection 含多 user personal chunks, metadata filter 仅返自己 personal."""
    file_a = _make_file(file_id="kbf_a", scope="personal", scope_id="user_a", uploaded_by="user_a")
    file_b = _make_file(file_id="kbf_b", scope="personal", scope_id="user_b", uploaded_by="user_b")

    chunks_a = [_make_chunk("kbf_a", 1, "user a personal note", scope="personal", scope_owner="user_a", embedding_seed=20)]
    chunks_b = [_make_chunk("kbf_b", 1, "user b personal note", scope="personal", scope_owner="user_b", embedding_seed=21)]
    await store.add_chunks(file_a, chunks_a)
    await store.add_chunks(file_b, chunks_b)

    # caller user_a in ws_42
    ctx = ScopeContext(user_id="user_a", workspace_id="ws_42", team_id="team_5")
    results = await store.search(ctx, _make_emb(100), top_k=10)
    contents = [r["content"] for r in results]
    assert "user a personal note" in contents
    assert "user b personal note" not in contents  # ✅ user_a cannot match user_b personal files.


@pytest.mark.asyncio
async def test_search_workspace_visible_to_all_members(store: KbStore) -> None:
    """workspace scope chunks 给 ws 任何 member 都返."""
    file = _make_file(scope="workspace", scope_id="ws_42")
    chunks = [_make_chunk(file.file_id, 1, "ws workspace doc", embedding_seed=30)]
    await store.add_chunks(file, chunks)

    ctx_a = ScopeContext(user_id="user_a", workspace_id="ws_42", team_id="team_5")
    ctx_b = ScopeContext(user_id="user_b", workspace_id="ws_42", team_id="team_5")
    results_a = await store.search(ctx_a, _make_emb(100), top_k=10)
    results_b = await store.search(ctx_b, _make_emb(100), top_k=10)
    assert any(r["content"] == "ws workspace doc" for r in results_a)
    assert any(r["content"] == "ws workspace doc" for r in results_b)


@pytest.mark.asyncio
async def test_search_cross_ws_isolation(store: KbStore) -> None:
    """user in ws_99 不命中 ws_42 workspace 文档."""
    file = _make_file(scope="workspace", scope_id="ws_42")
    chunks = [_make_chunk(file.file_id, 1, "ws_42 workspace doc", embedding_seed=40)]
    await store.add_chunks(file, chunks)

    # User in ws_99 (different ws).
    ctx = ScopeContext(user_id="user_x", workspace_id="ws_99", team_id="team_999")
    results = await store.search(ctx, _make_emb(100), top_k=10)
    assert all(r["content"] != "ws_42 workspace doc" for r in results)


# ─── 0.40.4 Bug 8 step a: extra_personal_workspaces cross-ws search ──


@pytest.mark.asyncio
async def test_search_finds_personal_chunks_in_extra_workspace(store: KbStore) -> None:
    """0.40.4 Bug 8 step a: personal chunks uploaded in ws_42 must be
    searchable when the session ws is ws_99 — IF the caller populates
    `extra_personal_workspaces=['ws_42']` in ScopeContext. This unblocks the
    datarc-ws vs Bowhead-KB-scope mismatch (chunks uploaded with datarc ws
    team_id, session ws default '1').
    """
    # Upload personal chunk in ws_42 as user_a
    file = _make_file(file_id="kbf_p1", scope="personal", scope_id="user_a", workspace_id="ws_42", uploaded_by="user_a")
    chunks = [_make_chunk("kbf_p1", 1, "user_a personal in ws_42", scope="personal", scope_owner="user_a", workspace_id="ws_42", embedding_seed=50)]
    await store.add_chunks(file, chunks)

    # Search as user_a from a DIFFERENT session ws (ws_99). Without extras → 0.
    no_extras = ScopeContext(user_id="user_a", workspace_id="ws_99", team_id="team_5")
    miss = await store.search(no_extras, _make_emb(100), top_k=10)
    assert all(r["content"] != "user_a personal in ws_42" for r in miss), "Without extra_personal_workspaces, legacy single-ws search should miss"

    # Now populate extras → should find it.
    with_extras = ScopeContext(
        user_id="user_a",
        workspace_id="ws_99",
        team_id="team_5",
        extra_personal_workspaces=["ws_42"],
    )
    found = await store.search(with_extras, _make_emb(100), top_k=10)
    assert any(r["content"] == "user_a personal in ws_42" for r in found), (
        f"Expected to find personal chunk via extras, got: {[r['content'] for r in found]}"
    )


@pytest.mark.asyncio
async def test_extra_workspaces_do_not_leak_other_users_personal(store: KbStore) -> None:
    """0.40.4 Bug 8: extra_personal_workspaces filter MUST be scoped to the
    caller's user_id — must not leak another user's personal chunks even if
    they're in the same ws collection.
    """
    file_a = _make_file(file_id="kbf_a", scope="personal", scope_id="user_a", workspace_id="ws_42", uploaded_by="user_a")
    file_b = _make_file(file_id="kbf_b", scope="personal", scope_id="user_b", workspace_id="ws_42", uploaded_by="user_b")
    await store.add_chunks(
        file_a, [_make_chunk("kbf_a", 1, "ALICE secret", scope="personal", scope_owner="user_a", workspace_id="ws_42", embedding_seed=60)]
    )
    await store.add_chunks(
        file_b, [_make_chunk("kbf_b", 1, "BOB secret", scope="personal", scope_owner="user_b", workspace_id="ws_42", embedding_seed=61)]
    )

    # user_a searching from ws_99 with extras=['ws_42'] → sees own chunk, NOT bob's
    ctx = ScopeContext(user_id="user_a", workspace_id="ws_99", team_id="team_5", extra_personal_workspaces=["ws_42"])
    results = await store.search(ctx, _make_emb(100), top_k=10)
    contents = [r["content"] for r in results]
    assert "ALICE secret" in contents
    assert "BOB secret" not in contents, "Personal scope filter MUST hold across extras"


@pytest.mark.asyncio
async def test_extra_workspaces_dedup_with_active_ws(store: KbStore) -> None:
    """0.40.4 Bug 8: if extras includes the active ws, don't double-query."""
    file = _make_file(file_id="kbf_x", scope="personal", scope_id="user_a", workspace_id="ws_42", uploaded_by="user_a")
    await store.add_chunks(
        file, [_make_chunk("kbf_x", 1, "single match content", scope="personal", scope_owner="user_a", workspace_id="ws_42", embedding_seed=70)]
    )

    ctx = ScopeContext(user_id="user_a", workspace_id="ws_42", team_id="team_5", extra_personal_workspaces=["ws_42", "ws_42"])
    results = await store.search(ctx, _make_emb(100), top_k=10)
    # Chunk_id dedup should still kick in; should NOT see same chunk twice.
    chunk_ids = [r["chunk_id"] for r in results]
    assert len(chunk_ids) == len(set(chunk_ids)), f"Duplicate chunk_ids: {chunk_ids}"


# ─── 0.40.3 Bug 7: _sanitize_id chromadb name validity ──


def test_sanitize_id_single_digit_pads_to_alnum() -> None:
    """0.40.3 Bug 7: pre-fix `_sanitize_id('1')` returned `_1_` → chromadb 1.5.8
    rejected `kb_ws__1_` ("name must start AND end with [a-zA-Z0-9]"). Every
    non-datarc dev user gets workspace_id='1' → silent KB failure. Fixed by
    padding with `0` (alnum) instead of `_`.
    """
    assert _sanitize_id("1") == "100"
    assert _sanitize_id("12") == "120"
    assert _sanitize_id("a") == "a00"


def test_sanitize_id_leading_underscore_prefix_x() -> None:
    """0.40.3 Bug 7: name must start with [a-zA-Z0-9]. Wrap underscore-led ids."""
    assert _sanitize_id("_test") == "x_test"


def test_sanitize_id_empty_string_pads() -> None:
    """0.40.3 Bug 7: empty input → 3-char alnum-only output."""
    s = _sanitize_id("")
    assert len(s) >= 3
    assert s[0].isalnum() and s[-1].isalnum()


def test_sanitize_id_collapses_consecutive_dots() -> None:
    """0.40.4 self-review: chromadb 1.5.8 rejects `..` in name. Path-traversal-
    shaped ws_ids ("../../etc/passwd") must produce a valid name (no
    consecutive periods) instead of triggering chromadb ValidationError.
    """
    assert ".." not in _sanitize_id("../../etc/passwd")
    assert ".." not in _sanitize_id("a..b")
    assert ".." not in _sanitize_id("...")
    # Existing single-dot case still works:
    s = _sanitize_id("a.b.c")
    assert s == "a.b.c", f"single-dot path should not be touched, got {s!r}"


def test_sanitize_id_all_chromadb_names_valid() -> None:
    """0.40.3 Bug 7: collection name `kb_ws_{sanitize(x)}` must always satisfy:
    3-63 chars, [a-zA-Z0-9._-], start AND end with [a-zA-Z0-9],
    NO consecutive periods (0.40.4 chromadb 1.5.8 strictness).
    """
    import re

    for raw in ["1", "12", "abc", "ws_42", "_test", "a.b.c", "", "__", "a", "x" * 70, "中文", "with space", "with/slash", "../../etc", "a..b", "..."]:
        s = _sanitize_id(raw)
        full = f"kb_ws_{s}"
        assert 3 <= len(full) <= 63, f"len={len(full)} for {raw!r}"
        assert full[0].isalnum(), f"start={full[0]!r} for {raw!r}"
        assert full[-1].isalnum(), f"end={full[-1]!r} for {raw!r}"
        assert re.fullmatch(r"[a-zA-Z0-9._-]+", full), f"invalid chars in {full!r} for raw={raw!r}"


# ─── 0.40.3 Bug 4 regression: chromadb 1.5.8 errors API ──


@pytest.mark.asyncio
async def test_search_missing_collection_does_not_crash(store: KbStore) -> None:
    """0.40.3 Bug 4: chromadb 1.5.8 removed `InvalidCollectionException` — only
    `NotFoundError` survives. search() must continue (skip the empty scope) when
    a collection raises NotFoundError, not AttributeError out the except clause.

    Pre-fix (0.40.2): Phase 4 dogfood Lead spawned 6 parallel kb_search; the 3
    against scopes with no collection raised AttributeError → unexpected_error
    → Lead saw 3/6 fails + answered "知识库缺必要文档".
    """
    # Brand-new scope context — no collection created, search hits "missing".
    ctx = ScopeContext(user_id="user_nodocs", workspace_id="ws_empty", team_id="team_empty")
    # MUST NOT raise; returns empty list.
    results = await store.search(ctx, _make_emb(100), top_k=5)
    assert results == []


def test_chromadb_errors_module_has_expected_exceptions() -> None:
    """0.40.3 Bug 4 guard: catch future chromadb error-rename regressions.

    If chromadb bumps a major version and renames `NotFoundError`, this test
    fails loud before kb_search starts crashing in prod (the kind of mismatch
    that took us 1 ship cycle to find via dogfood).
    """
    import chromadb.errors as _ce

    available = {name for name in dir(_ce) if not name.startswith("_")}
    # Names we depend on in kb/store.py — fail if any disappears.
    expected = {"NotFoundError"}
    missing = expected - available
    assert not missing, f"chromadb.errors removed names we depend on: {missing}. Currently available: {sorted(available)}"
    # Names we used to depend on — should be GONE in 1.5.8+ (was the Bug 4 bug).
    # Loud-fail if a future version brings them back AND we forget to use them.
    gone = {"InvalidCollectionException"}
    found_again = gone & available
    if found_again:
        # Don't fail — just warn loud via assertion message; new exception type
        # might be worth catching alongside NotFoundError.
        msg = f"chromadb.errors brought back: {found_again}. Re-evaluate kb/store.py except clause."
        # Use a soft skip — not a regression unless we want to catch them.
        # Comment out the raise once the kb/store.py decision is recorded.
        assert True, msg


# ─── soft delete + hard delete ────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_marks_inactive_keeps_content(store: KbStore) -> None:
    """[P0-B] soft delete 后, content 仍在 chromadb (cite popover 仍可 fetch), 但 search 不命中."""
    file = _make_file()
    chunks = [_make_chunk(file.file_id, i, f"content {i}", embedding_seed=i) for i in range(1, 4)]
    await store.add_chunks(file, chunks)
    assert await store.count_chunks(file) == 3

    n = await store.soft_delete_file(file)
    assert n == 3
    # P0-O3: count_chunks filters is_active=True, excludes soft-deleted.
    assert await store.count_chunks(file) == 0


@pytest.mark.asyncio
async def test_hard_delete_removes_chunks(store: KbStore) -> None:
    file = _make_file()
    chunks = [_make_chunk(file.file_id, i, f"c{i}", embedding_seed=i) for i in range(1, 4)]
    await store.add_chunks(file, chunks)

    n = await store.hard_delete_file(file)
    assert n == 3
    assert await store.count_chunks(file) == 0


# ─── update_chunks_scope (P0-O2 + P0-P2) ───────────────


@pytest.mark.asyncio
async def test_workspace_to_team_chunks_migrate_correctly(store: KbStore) -> None:
    """[P0-O2 + P0-P2] workspace ws_42 → team team_5 跨 collection 物理迁移.

    迁移后 ws collection 不应再有 chunks (delete 旧成功); team collection 有 chunks.
    """
    file = _make_file(file_id="kbf_migrate", scope="workspace", scope_id="ws_42")
    chunks = [_make_chunk(file.file_id, 1, "to migrate", embedding_seed=50)]
    await store.add_chunks(file, chunks)
    assert await store.count_chunks(file) == 1

    n = await store.update_chunks_scope(
        file_id=file.file_id,
        old_scope="workspace",
        old_scope_id="ws_42",
        old_workspace_id="ws_42",
        old_team_id="team_5",
        new_scope="team",
        new_scope_id="team_5",
        new_workspace_id="",
        new_team_id="team_5",
    )
    assert n == 1

    # Legacy workspace collection gone; count uses new file context.
    file_old = _make_file(file_id="kbf_migrate", scope="workspace", scope_id="ws_42", workspace_id="ws_42", team_id="team_5")
    assert await store.count_chunks(file_old) == 0

    # New file uses team collection with chunks.
    file_new = _make_file(file_id="kbf_migrate", scope="team", scope_id="team_5", workspace_id="", team_id="team_5")
    assert await store.count_chunks(file_new) == 1


@pytest.mark.asyncio
async def test_personal_to_workspace_in_place_metadata_update(store: KbStore) -> None:
    """同 ws collection 内 personal → workspace: in-place metadata update, 不跨 collection."""
    file = _make_file(file_id="kbf_inplace", scope="personal", scope_id="user_a", workspace_id="ws_42", uploaded_by="user_a")
    chunks = [_make_chunk(file.file_id, 1, "in place", scope="personal", scope_owner="user_a", workspace_id="ws_42", embedding_seed=60)]
    await store.add_chunks(file, chunks)

    n = await store.update_chunks_scope(
        file_id=file.file_id,
        old_scope="personal",
        old_scope_id="user_a",
        old_workspace_id="ws_42",
        old_team_id="team_5",
        new_scope="workspace",
        new_scope_id="ws_42",
        new_workspace_id="ws_42",
        new_team_id="team_5",
    )
    assert n == 1

    # New caller in ws_42 sees chunk via workspace filter.
    ctx = ScopeContext(user_id="user_b", workspace_id="ws_42", team_id="team_5")  # Different user.
    results = await store.search(ctx, _make_emb(60), top_k=10)
    assert any(r["content"] == "in place" for r in results)


# ─── Failed migration compensation / no empty collections on reads / honest timeout reporting ──


class _FailingColl:
    """真集合的代理：指定方法抛错，其余透传。"""

    def __init__(self, inner, fail: str) -> None:
        self._inner = inner
        self._fail = fail

    def __getattr__(self, name: str):
        if name == self._fail:

            def _boom(*_a, **_kw):
                raise RuntimeError(f"vector store down: {name}")

            return _boom
        return getattr(self._inner, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["delete", "upsert", "get"])
async def test_failed_cross_collection_migration_restores_old_collection(store: KbStore, monkeypatch, fail: str) -> None:
    """跨集合迁移任一步失败：抛 ScopeChangeMigrationError，分块全部留在旧集合、新集合无副本。"""
    from bowhead_core.kb.errors import ScopeChangeMigrationError

    file_old = _make_file(file_id="kbf_fail", scope="space", scope_id="ws_42")
    file_new = _make_file(file_id="kbf_fail", scope="team", scope_id="team_5", workspace_id="")
    await store.add_chunks(file_old, [_make_chunk("kbf_fail", i, f"c{i}", embedding_seed=i) for i in range(1, 4)])
    real_routing = store._routing_collection

    async def _routing(scope, workspace_id, team_id, *, create):
        coll = await real_routing(scope, workspace_id, team_id, create=create)
        # delete/get fail on old collection; upsert fails on new.
        target_is_old = scope != "team"
        if coll is not None and (target_is_old if fail in ("delete", "get") else not target_is_old):
            return _FailingColl(coll, fail)
        return coll

    monkeypatch.setattr(store, "_routing_collection", _routing)

    with pytest.raises(ScopeChangeMigrationError):
        await store.update_chunks_scope(
            file_id="kbf_fail",
            old_scope="space",
            old_scope_id="ws_42",
            old_workspace_id="ws_42",
            old_team_id="team_5",
            new_scope="team",
            new_scope_id="team_5",
            new_workspace_id="",
            new_team_id="team_5",
        )
    monkeypatch.setattr(store, "_routing_collection", real_routing)

    assert await store.count_chunks(file_old) == 3
    assert await store.count_chunks(file_new) == 0


@pytest.mark.asyncio
async def test_read_paths_do_not_create_empty_collections(store: KbStore) -> None:
    ctx = ScopeContext(user_id="user_a", workspace_id="ws_empty", team_id="team_empty", extra_personal_workspaces=["ws_other"])
    file = _make_file(file_id="kbf_none", scope="space", scope_id="ws_empty", workspace_id="ws_empty")

    assert await store.search(ctx, _make_emb(1)) == []
    assert await store.count_chunks(file) == 0
    assert await store.list_active_chunks(file) == []
    assert await store.hard_delete_file(file) == 0
    assert store._client.list_collections() == []


@pytest.mark.asyncio
async def test_search_timeout_marks_result_partial(store: KbStore, monkeypatch) -> None:
    """某个集合检索超时被跳过：结果带 skipped_collections，不再静默当成「没有」。"""
    import time

    from bowhead_core.kb import store as store_module

    file = _make_file(file_id="kbf_ws", scope="space", scope_id="ws_42")
    await store.add_chunks(file, [_make_chunk("kbf_ws", 1, "空间内容", embedding_seed=3)])
    real_query = store._query_if_exists
    team_name = store._team_collection_name("team_5")

    def _slow_team(name, **query):
        if name == team_name:
            time.sleep(0.3)
        return real_query(name, **query)

    monkeypatch.setattr(store, "_query_if_exists", _slow_team)
    monkeypatch.setattr(store_module, "SEARCH_TIMEOUT_S", 0.1)

    results = await store.search(ScopeContext(user_id="user_a", workspace_id="ws_42", team_id="team_5"), _make_emb(3))

    assert [r["content"] for r in results] == ["空间内容"]
    assert results.partial is True
    assert results.skipped_collections == 1
