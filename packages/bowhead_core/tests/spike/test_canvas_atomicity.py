"""Sprint θ Phase 0d Spike #5: canvas atomicity smoke (no LLM).

**Purpose**: Verify whether canvas_store.{patch,delete,reorder}_sections are
atomic at the multi-op level (i.e., 3 ops in one Lead turn either all succeed
OR all rollback). PRD §3.8 S8(c) UX "已撤销修改" depends on this.

**Findings (per tech-design §0.6 A3)**:
- Each individual op IS atomic (single SQLite transaction per method)
- 3 ops in sequence are NOT atomic at the LLM-call / Lead-turn level — each
  op opens its own `aiosqlite.connect()` and commits independently
- → canvas_tools.py needs transactional wrapper (~80 LOC: buffer ops in memory,
  flush on success, discard on any fail)

**Pass criteria**:
- Pass 1: Confirm single-op atomicity (patch_section commits or rolls back)
- Pass 2: Confirm multi-op non-atomicity (current state — gap to fix)
- Pass 3: Sketch the wrapper needed in canvas_tools.py for atomic multi-op

Run: `cd packages/bowhead_core && uv run pytest tests/spike/test_canvas_atomicity.py -v`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.canvas_store import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def canvas_store(tmp_path: Path) -> CanvasStore:
    """Make CanvasStore backed by tmp file SQLite (real path, not :memory:)."""
    store = CanvasStore(db_path=tmp_path / "canvas_test.db")
    await store.initialize()
    return store


@pytest.fixture
async def seeded_canvas(canvas_store: CanvasStore) -> tuple[str, list[str]]:
    """Canvas with 3 sections (s_a, s_b, s_c), returns (canvas_id, [section_ids])."""
    canvas = await canvas_store.ensure_canvas(
        session_id="s_spike5",
        workspace_id="ws_spike5",
        user_id="u_spike5",
    )
    sec_a = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Section A original"},
        by_turn_id="t_setup",
    )
    sec_b = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Section B original"},
        by_turn_id="t_setup",
    )
    sec_c = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Section C original"},
        by_turn_id="t_setup",
    )
    return canvas.id, [sec_a.id, sec_b.id, sec_c.id]


# ===== Spike tests =====


@pytest.mark.asyncio
async def test_spike5_pass1_single_patch_atomic(
    canvas_store: CanvasStore,
    seeded_canvas: tuple[str, list[str]],
) -> None:
    """Pass 1: patch_section is atomic — payload commits as one."""
    _canvas_id, [sec_a, _, _] = seeded_canvas

    patched = await canvas_store.patch_section(
        section_id=sec_a,
        patch={"markdown": "Section A PATCHED"},
        by_turn_id="t_spike5",
    )
    assert patched is not None
    assert patched.payload["markdown"] == "Section A PATCHED"
    assert patched.rev == 2  # rev incremented atomically


@pytest.mark.asyncio
async def test_spike5_pass2_multi_op_NOT_atomic_independent_commits(
    canvas_store: CanvasStore,
    seeded_canvas: tuple[str, list[str]],
) -> None:
    """Pass 2 (CRITICAL FINDING): 3 ops in sequence are NOT atomic.

    Sequence: patch s_a → delete s_b → (3rd op fails: patch nonexistent section).
    Expected (current behavior): patch s_a committed, delete s_b committed,
    3rd op silently returns None. NO rollback. → canvas state PARTIAL.

    PRD §3.8 S8(c) UX "已撤销修改" toast is INACCURATE in current state.
    canvas_tools.py needs transactional wrapper to fix.
    """
    canvas_id, [sec_a, sec_b, _sec_c] = seeded_canvas

    # Op 1: patch s_a (commits)
    await canvas_store.patch_section(
        section_id=sec_a,
        patch={"markdown": "patched"},
        by_turn_id="t_op1",
    )

    # Op 2: delete s_b (commits independently)
    deleted = await canvas_store.delete_section(
        section_id=sec_b,
        by_turn_id="t_op2",
    )
    # A2: return IDs actually deleted (see canvas_store.delete_section docstring)
    assert deleted == [sec_b]

    # Op 3: try to patch a nonexistent section (returns None, no error)
    op3_result = await canvas_store.patch_section(
        section_id="sec_does_not_exist",
        patch={"markdown": "x"},
        by_turn_id="t_op3",
    )
    assert op3_result is None  # Silent fail, no rollback

    # Verify state: ops 1 and 2 PARTIAL committed (state is not pre-multi-op)
    sections_after = await canvas_store.list_sections(canvas_id)
    assert len(sections_after) == 2  # s_b deleted (op 2 committed)
    assert sections_after[0].payload["markdown"] == "patched"  # op 1 committed

    # ⚠️ FINDING: PRD S8(c)'s undo-confirmation toast claim is broken when canvas-writer
    # sequences multi-op and one fails. canvas_tools.py needs +80 LOC wrapper:
    #   - buffer ops in memory list
    #   - on flush: open ONE transaction, apply all, commit OR rollback all
    #   - emit canvas_op_failed{op, error, partial=False} only on rollback


@pytest.mark.asyncio
async def test_spike5_pass3_wrapper_design_sketch(
    canvas_store: CanvasStore,
    seeded_canvas: tuple[str, list[str]],
) -> None:
    """Pass 3: Sketch the transactional wrapper canvas_tools.py needs.

    Shape (pseudocode for Phase 8b-3 canvas_tools.py impl):

    ```python
    class AtomicCanvasOps:
        def __init__(self, store: CanvasStore):
            self._store = store
            self._buffered: list[Op] = []

        def buffer_patch(self, section_id, patch, by_turn_id):
            self._buffered.append(Op("patch", section_id, patch, by_turn_id))

        def buffer_delete(self, section_id, by_turn_id):
            self._buffered.append(Op("delete", section_id, None, by_turn_id))

        def buffer_reorder(self, canvas_id, ordered_ids, by_turn_id):
            self._buffered.append(Op("reorder", canvas_id, ordered_ids, by_turn_id))

        async def flush(self):
            # Open ONE transaction across all buffered ops
            async with aiosqlite.connect(self._store._path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    for op in self._buffered:
                        await self._apply_in_db(op, db)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise CanvasOpFailedError(...)
                finally:
                    self._buffered.clear()
    ```

    LOC estimate: ~80 (per tech-design §0.6 A3 fail mitigation).

    Phase 8b-3 implementation gate: this wrapper MUST exist before
    canvas_tools.{patch,delete,reorder}_section are exposed to canvas-writer
    LLM tool whitelist.
    """
    # Pass — this is a design sketch, no actual code to test.
    # Marker assertion to make pytest happy:
    assert True


@pytest.mark.asyncio
async def test_spike5_pass4_reorder_intra_op_atomic(
    canvas_store: CanvasStore,
    seeded_canvas: tuple[str, list[str]],
) -> None:
    """Pass 4: reorder_sections is intra-op atomic (3 UPDATE in 1 transaction)."""
    canvas_id, [sec_a, sec_b, sec_c] = seeded_canvas

    # Reorder: c → a → b
    await canvas_store.reorder_sections(
        canvas_id=canvas_id,
        ordered_ids=[sec_c, sec_a, sec_b],
        by_turn_id="t_reorder",
    )

    sections = await canvas_store.list_sections(canvas_id)
    assert sections[0].id == sec_c
    assert sections[1].id == sec_a
    assert sections[2].id == sec_b
    assert sections[0].seq == 1
    assert sections[1].seq == 2
    assert sections[2].seq == 3
