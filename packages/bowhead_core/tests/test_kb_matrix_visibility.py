"""Only-me matrices in the store and the engine (document matrix step 1, S8).

Store: ``visibility`` needs revision 0240, the list filter has no role exception, the visibility change runs
its check under the structure lock and refuses navigation columns, and navigation roles are refused on
private matrices. Engine: personal-file cells never read or write the Team-wide cache, and under
``FEATURE_KB_USE_GUARD`` the source check accepts the initiator's own personal files only in private matrices.
A run by another member stops before its next call once the creator makes the matrix private; the creator's
own run goes on.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from bowhead_core.kb import matrix_engine
from bowhead_core.kb.eligibility import KbUseDecision
from bowhead_core.kb.matrix_store import KbMatrixStore, MatrixVisibilityError, private_matrices_enabled
from test_kb_matrix_engine import TEAM, USER, WS, Env, _mk_env, _wait_run

if TYPE_CHECKING:
    from pathlib import Path

OTHER = "u_other"
COLUMNS = [{"name": "Amount", "type": "question", "question": "How much?"}]


@pytest.fixture
async def env(tmp_path: Path, initialize_migrated_kb_files_database, initialize_migrated_kb_matrix_database, monkeypatch: pytest.MonkeyPatch) -> Env:
    """The engine harness on the matrix head schema. Runs bill with the v1 rate card: the harness's credit
    ledger has no model-usage table, which the head's v2 default (revision 0173 trigger) needs."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    initialize_migrated_kb_matrix_database(tmp_path / "kb_matrix.db")
    with closing(sqlite3.connect(tmp_path / "kb_matrix.db")) as db:
        db.execute("DROP TRIGGER matrix_credit_rate_at_insert")
        db.commit()
    return await _mk_env(tmp_path)


async def _file(env: Env, hint: str, *, scope: str = "workspace", owner: str = USER) -> str:
    file_id = await env.files_store.create(
        artifact_id=hint,
        filename=f"{hint}.md",
        mime_type="text/markdown",
        size_bytes=10,
        scope=scope,
        scope_id=owner if scope == "personal" else WS,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=owner,
    )
    env.kb.chunks_by_file[file_id] = [(f"chk_{hint}", f"{hint} text: the amount is 5.", 1)]
    return file_id


async def _matrix(env: Env, file_ids: list[str], *, visibility: str = "space", created_by: str = USER, columns: list[dict[str, Any]] | None = None):
    return await env.store.create_matrix(
        name=f"{visibility} matrix",
        workspace_id=WS,
        team_id=TEAM,
        created_by=created_by,
        files=[{"file_id": f, "char_count": 10} for f in file_ids],
        columns=columns or COLUMNS,
        visibility=visibility,
    )


def test_the_switch_is_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_KB_MATRIX_PRIVATE", raising=False)
    assert private_matrices_enabled() is False
    monkeypatch.setenv("FEATURE_KB_MATRIX_PRIVATE", "1")
    assert private_matrices_enabled() is True


# ─── store ─────────────────────────────────────────────────


async def test_a_private_matrix_needs_revision_0240_and_a_known_value(tmp_path: Path) -> None:
    baseline = KbMatrixStore(tmp_path / "baseline.db")
    await baseline.initialize()
    with pytest.raises(RuntimeError, match="0240"):
        await baseline.create_matrix(name="x", workspace_id=WS, team_id=TEAM, created_by=USER, files=[], columns=[], visibility="private")
    space = await baseline.create_matrix(name="x", workspace_id=WS, team_id=TEAM, created_by=USER, files=[], columns=[])
    assert space.visibility == "space"
    # Without the column every matrix is a Space matrix, listed for everyone.
    assert [m.matrix_id for m in await baseline.list_matrices(WS, viewer_id=OTHER)] == [space.matrix_id]
    with pytest.raises(ValueError, match="matrix_visibility_invalid"):
        await baseline.create_matrix(name="x", workspace_id=WS, team_id=TEAM, created_by=USER, files=[], columns=[], visibility="team")


async def test_the_list_shows_private_matrices_to_their_creator_only(env: Env) -> None:
    mine = await _matrix(env, [], visibility="private")
    theirs = await _matrix(env, [], visibility="private", created_by=OTHER)
    shared = await _matrix(env, [])
    assert {m.matrix_id for m in await env.store.list_matrices(WS, viewer_id=USER)} == {mine.matrix_id, shared.matrix_id}
    assert {m.matrix_id for m in await env.store.list_matrices(WS, viewer_id=OTHER)} == {theirs.matrix_id, shared.matrix_id}
    assert (await env.store.get_matrix(mine.matrix_id)).visibility == "private"


async def test_the_visibility_change_checks_under_the_lock_and_is_structural(env: Env) -> None:
    matrix = await _matrix(env, [])
    seen: list[str] = []

    async def refuse(current: Any) -> None:
        seen.append(current.visibility)
        raise MatrixVisibilityError("blocked", count=1)

    with pytest.raises(MatrixVisibilityError) as caught:
        await env.store.set_visibility(matrix.matrix_id, "private", workspace_id=WS, check=refuse)
    assert caught.value.payload == {"count": 1} and seen == ["space"]
    assert (await env.store.get_matrix(matrix.matrix_id)).visibility == "space"

    changed = await env.store.set_visibility(matrix.matrix_id, "private", workspace_id=WS, check=AsyncMock())
    assert changed is not None
    updated, did_change = changed
    assert did_change and updated.visibility == "private" and updated.structure_seq == updated.last_seq > matrix.last_seq
    # The same value is not a change: no check, no write.
    check = AsyncMock()
    assert await env.store.set_visibility(matrix.matrix_id, "private", workspace_id=WS, check=check) == (updated, False)
    check.assert_not_awaited()
    assert await env.store.set_visibility(matrix.matrix_id, "private", workspace_id="ws_other") is None


async def test_navigation_roles_and_private_matrices_exclude_each_other(env: Env) -> None:
    private = await _matrix(env, [], visibility="private")
    col = private.columns[0]["column_id"]
    with pytest.raises(MatrixVisibilityError) as caught:
        await env.store.update_column(private.matrix_id, col, workspace_id=WS, navigation_role="description")
    assert caught.value.code == "matrix_navigation_role_private"
    assert await env.store.update_column(private.matrix_id, col, workspace_id=WS, navigation_role="none") is not None

    shared = await _matrix(env, [])
    await env.store.update_column(shared.matrix_id, shared.columns[0]["column_id"], workspace_id=WS, navigation_role="topics")
    with pytest.raises(MatrixVisibilityError) as caught:
        await env.store.set_visibility(shared.matrix_id, "private", workspace_id=WS)
    assert caught.value.code == "matrix_navigation_columns_block_private" and caught.value.payload == {"columns": ["Amount"]}


async def test_contributors_are_the_people_behind_cell_changes_and_runs(env: Env) -> None:
    space = await _file(env, "space")
    matrix = await _matrix(env, [space])
    col = matrix.columns[0]["column_id"]
    await env.store.edit_cell_value(
        matrix.matrix_id, space, col, value="1", column=matrix.columns[0], workspace_id=WS, team_id=TEAM, actor_user_id=OTHER
    )
    await env.store.create_run(
        matrix_id=matrix.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by="u_runner_2",
        cells_total=1,
        skipped_edited=0,
        credits_estimated=1,
    )
    assert await env.store.list_contributors(matrix.matrix_id) == sorted([OTHER, "u_runner_2"])


# ─── engine ────────────────────────────────────────────────


async def test_personal_file_cells_never_read_or_write_the_cache(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    space = await _file(env, "space")
    mine = await _file(env, "mine", scope="personal")
    lookups: list[str] = []
    original = env.store.find_cached_cell

    async def recorded(team_id: str, cache_key: str) -> Any:
        lookups.append(cache_key)
        return await original(team_id, cache_key)

    monkeypatch.setattr(env.store, "find_cached_cell", recorded)
    matrix = await _matrix(env, [space, mine], visibility="private")
    col = matrix.columns[0]["column_id"]
    run = await env.engine.start_run(matrix, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)

    personal_cell = await env.store.get_cell(matrix.matrix_id, mine, col)
    space_cell = await env.store.get_cell(matrix.matrix_id, space, col)
    assert personal_cell is not None and personal_cell.status == "done" and personal_cell.cache_key is None
    assert space_cell is not None and space_cell.status == "done" and space_cell.cache_key
    # One lookup, for the Space file only.
    assert lookups == [space_cell.cache_key]


class _Eligibility:
    """Stands in for the use guard's storage checks: the decision is the matrix's own authorization."""

    def __init__(self, files: Any, artifacts: Any, index: Any) -> None:
        self.files = files

    async def check(self, file_id: str, scope: Any, authorize: Any, **_kwargs: Any) -> KbUseDecision:
        file = await self.files.get(file_id)
        allowed = file is not None and await authorize(file)
        return KbUseDecision("eligible" if allowed else "paused", "verified_binding" if allowed else "permission_or_file_unavailable", None, "now")


async def test_the_use_guard_accepts_the_initiators_personal_files_only_in_private_matrices(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix_engine, "KbUseEligibility", _Eligibility)
    authorizer = AsyncMock(return_value=True)
    env.engine._file_authorizer = authorizer
    space = await _file(env, "space")
    mine = await _file(env, "mine", scope="personal")
    theirs = await _file(env, "theirs", scope="personal", owner=OTHER)

    async def allowed(file_id: str, *, personal_owner: bool) -> bool:
        return (await env.engine._source_check(file_id, USER, TEAM, WS, None, personal_owner=personal_owner)).allowed

    assert await allowed(space, personal_owner=False)
    assert await allowed(mine, personal_owner=True)
    assert not await allowed(mine, personal_owner=False)
    assert not await allowed(theirs, personal_owner=True)
    # The initiator's own personal file still passes the file grants like any other file.
    authorizer.return_value = False
    assert not await allowed(mine, personal_owner=True)


async def test_runs_of_private_matrices_ask_the_use_guard_with_personal_files_allowed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix_engine, "use_guard_enabled", lambda: True)
    asked: list[tuple[str, bool]] = []

    async def source_check(file_id: str, user_id: str, *_args: Any, personal_owner: bool = False, **_kwargs: Any) -> KbUseDecision:
        assert user_id == USER
        asked.append((file_id, personal_owner))
        return KbUseDecision("eligible", "verified_binding", None, "now")

    monkeypatch.setattr(env.engine, "_source_check", source_check)
    mine = await _file(env, "mine", scope="personal")
    private = await _matrix(env, [mine], visibility="private")
    run = await env.engine.start_run(private, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    # Start, before the call and before the value lands: each time with personal files allowed.
    assert asked == [(mine, True)] * 3
    assert (await env.store.get_cell(private.matrix_id, mine, private.columns[0]["column_id"])).status == "done"

    asked.clear()
    space = await _file(env, "space")
    shared = await _matrix(env, [space])
    run = await env.engine.start_run(shared, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    assert asked == [(space, False)] * 3


# ─── a matrix made private during a run ───────────────────


async def _run_while_made_private(env: Env, monkeypatch: pytest.MonkeyPatch, *, created_by: str) -> tuple[Any, Any, list[str], int]:
    """Three files, one call each, one at a time; the matrix becomes private during the first model call."""
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_CELL_CONCURRENCY", "1")
    file_ids = [await _file(env, f"f{i}") for i in range(3)]
    matrix = await _matrix(env, file_ids, created_by=created_by)
    invoke = env.router.invoke

    async def invoke_then_make_private(*args: Any, **kwargs: Any) -> Any:
        if len(env.router.calls) == 0:
            assert await env.store.set_visibility(matrix.matrix_id, "private", workspace_id=WS)
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(env.router, "invoke", invoke_then_make_private)
    before = (await env.user_store.get_user(USER)).credit_balance
    run = await env.engine.start_run(matrix, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    after = (await env.user_store.get_user(USER)).credit_balance
    return await env.store.get_run(run.run_id), matrix, file_ids, before - after


async def test_another_members_run_stops_before_its_next_call_when_the_matrix_becomes_private(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    run, matrix, file_ids, spent = await _run_while_made_private(env, monkeypatch, created_by=OTHER)
    assert run.status == "cancelled" and run.error_code == matrix_engine.MATRIX_BECAME_PRIVATE_CODE
    # The call in flight when the matrix changed lands and is charged once; nothing is dispatched after it.
    assert len(env.router.calls) == 1
    col = matrix.columns[0]["column_id"]
    cells = [await env.store.get_cell(matrix.matrix_id, file_id, col) for file_id in file_ids]
    assert cells[0] is not None and cells[0].status == "done"
    assert all(c is None or c.status not in ("queued", "running") for c in cells[1:])
    assert run.cells_done == 1 and run.credits_charged == spent > 0


async def test_the_creators_own_run_goes_on_when_they_make_the_matrix_private(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _matrix, _file_ids, spent = await _run_while_made_private(env, monkeypatch, created_by=USER)
    assert run.status == "succeeded" and run.error_code is None
    assert len(env.router.calls) == 3 and run.cells_done == 3 and run.credits_charged == spent
