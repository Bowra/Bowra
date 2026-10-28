"""report-engine Phase-2 WP-A — per-section ownership enforcement in CanvasStore.

Verifies the store-level owner stamp + caller check that the multi-writer model
relies on (re-review 安全P0: owner must be a real, enforced, unforgeable column,
not a self-stamped payload string)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from bowhead_core.canvas_store import CanvasNotOwnerError, CanvasRevConflictError

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "t.db")
    await s.initialize()
    return s


async def _canvas(store: CanvasStore) -> str:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return c.id


async def test_owner_stamped_and_same_owner_can_patch(store: CanvasStore) -> None:
    cid = await _canvas(store)
    s = await store.append_section(
        canvas_id=cid,
        kind="narrative",
        payload={"markdown": "a"},
        owner="sub_aaa",
    )
    # same owner updates fine
    patched = await store.patch_section(
        section_id=s.id,
        patch={"markdown": "b"},
        expected_rev=1,
        caller="sub_aaa",
    )
    assert patched is not None
    assert patched.rev == 2
    assert patched.payload["markdown"] == "b"


async def test_different_caller_is_rejected(store: CanvasStore) -> None:
    cid = await _canvas(store)
    s = await store.append_section(
        canvas_id=cid,
        kind="narrative",
        payload={"markdown": "a"},
        owner="sub_aaa",
    )
    with pytest.raises(CanvasNotOwnerError) as ei:
        await store.patch_section(
            section_id=s.id,
            patch={"markdown": "hijack"},
            expected_rev=1,
            caller="sub_bbb",
        )
    assert ei.value.owner == "sub_aaa"
    # content untouched
    rows = await store.list_sections(cid)
    assert rows[0].payload["markdown"] == "a"


async def test_null_owner_is_claimable_by_anyone(store: CanvasStore) -> None:
    cid = await _canvas(store)
    # legacy / non-report append: no owner
    s = await store.append_section(canvas_id=cid, kind="narrative", payload={"markdown": "a"})
    patched = await store.patch_section(
        section_id=s.id,
        patch={"markdown": "b"},
        expected_rev=1,
        caller="sub_anyone",
    )
    assert patched is not None and patched.payload["markdown"] == "b"


async def test_caller_none_skips_owner_check_backward_compat(store: CanvasStore) -> None:
    cid = await _canvas(store)
    s = await store.append_section(
        canvas_id=cid,
        kind="narrative",
        payload={"markdown": "a"},
        owner="sub_aaa",
    )
    # HTTP route / legacy ops pass no caller → ownership not enforced at store layer
    patched = await store.patch_section(section_id=s.id, patch={"markdown": "b"})
    assert patched is not None and patched.payload["markdown"] == "b"


async def test_owner_field_not_forgeable_via_patch(store: CanvasStore) -> None:
    cid = await _canvas(store)
    s = await store.append_section(
        canvas_id=cid,
        kind="narrative",
        payload={"markdown": "a"},
        owner="sub_aaa",
    )
    # try to overwrite owner + added_by through the payload patch
    await store.patch_section(
        section_id=s.id,
        patch={"markdown": "b", "owner": "sub_evil", "added_by": "sub_evil"},
        expected_rev=1,
        caller="sub_aaa",
    )
    # owner column unchanged → a different caller is still rejected
    with pytest.raises(CanvasNotOwnerError):
        await store.patch_section(
            section_id=s.id,
            patch={"markdown": "c"},
            caller="sub_evil",
        )
    rows = await store.list_sections(cid)
    assert "owner" not in rows[0].payload  # protected field stripped from payload
    assert "added_by" not in rows[0].payload


async def test_rev_conflict_still_enforced_alongside_owner(store: CanvasStore) -> None:
    cid = await _canvas(store)
    s = await store.append_section(
        canvas_id=cid,
        kind="narrative",
        payload={"markdown": "a"},
        owner="sub_aaa",
    )
    await store.patch_section(section_id=s.id, patch={"markdown": "b"}, expected_rev=1, caller="sub_aaa")
    # stale rev by the rightful owner → rev_conflict (not silently overwritten)
    with pytest.raises(CanvasRevConflictError) as ei:
        await store.patch_section(section_id=s.id, patch={"markdown": "c"}, expected_rev=1, caller="sub_aaa")
    assert ei.value.current_rev == 2


async def test_concurrent_appends_get_distinct_contiguous_seqs(store: CanvasStore) -> None:
    """WP-A.2: BEGIN IMMEDIATE serializes the MAX(seq)+1 → INSERT so concurrent
    appends to the same canvas never collide on seq (the TOCTOU re-review P0)."""
    cid = await _canvas(store)
    n = 20
    results = await asyncio.gather(
        *[
            store.append_section(
                canvas_id=cid,
                kind="narrative",
                payload={"markdown": f"s{i}"},
                owner=f"sub_{i}",
            )
            for i in range(n)
        ]
    )
    seqs = sorted(s.seq for s in results)
    assert seqs == list(range(1, n + 1)), f"seq collision/gap: {seqs}"
    rows = await store.list_sections(cid)
    assert len(rows) == n
    assert len({r.seq for r in rows}) == n  # all distinct in the DB too


async def test_initialize_idempotent_owner_migration(tmp_path: Path) -> None:
    p = tmp_path / "t.db"
    s1 = CanvasStore(p)
    await s1.initialize()
    cid = await _canvas(s1)
    await s1.append_section(canvas_id=cid, kind="narrative", payload={"markdown": "a"}, owner="sub_aaa")
    # re-open + re-initialize (simulates reboot / migration re-run) — must not error
    s2 = CanvasStore(p)
    await s2.initialize()
    rows = await s2.list_sections(cid)
    assert len(rows) == 1
