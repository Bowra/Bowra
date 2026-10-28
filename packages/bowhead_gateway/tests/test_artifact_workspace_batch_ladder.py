"""产物空间归属梯子的批量版 `files._artifact_workspace_ids`（单条版 `_artifact_workspace_id` 是它的特例）。

为什么要批量：全局搜索要一次判几十个产物，逐个推导是每行约 3 次开库；116 实测团队管理员
首窗 20 行 ≈ 66 次开库 ≈ 0.9s，挤进 4s 来源超时后文件结果整组消失。

本文件钉三件事：
1. **每一级的结果**（期望值逐条写死，不拿批量去比单条——单条就是批量的特例，那样比是自证）；
2. **开库次数与行数无关**（同形的 30 行与 1 行一样多）；
3. **批量查询失败不放大**：某一级的批量查询出错时逐个重试，一个产物出错不连带同批其它产物变成「推不出」
   （「推不出」在闸关时是 fail-open，放大它就是放行一整批）。
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files as files_routes

_FAIL_CLOSE = "FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE"


@pytest.fixture
async def ladder(tmp_path, monkeypatch):
    monkeypatch.delenv(_FAIL_CLOSE, raising=False)
    db = tmp_path / "bowhead.db"
    sessions = SessionStore(db)
    await sessions.initialize()
    canvases = CanvasStore(db)
    await canvases.initialize()
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "blobs"), db_path=db)
    await artifacts.initialize()

    s1 = await sessions.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")
    s2 = await sessions.ensure_session(session_id=None, workspace_id="ws_two", user_id="u")
    s3 = await sessions.ensure_session(session_id=None, workspace_id="ws_three", user_id="u")
    s4 = await sessions.ensure_session(session_id=None, workspace_id="ws_four", user_id="u")
    s5 = await sessions.ensure_session(session_id=None, workspace_id="ws_five", user_id="u")
    await sessions.record_turn(turn_id="turn_two", session_id=s2.id, user_text="t")
    await sessions.record_turn(turn_id="turn_four", session_id=s4.id, user_text="t")
    await sessions.record_turn(turn_id="turn_five", session_id=s5.id, user_text="t")
    canvas = await canvases.ensure_canvas(session_id=s3.id, workspace_id="ws_three", user_id="u")
    canvas_b = await canvases.ensure_canvas(session_id=s5.id, workspace_id="ws_five", user_id="u")

    async def put(label: str, **kw):
        art = await artifacts.put_bytes(content=b"x", filename=f"{label}.csv", mime="text/csv", created_by="u", **kw)
        return art.id

    ids = {
        "binding": await put("binding"),
        "binding_empty": await put("binding_empty", session_id=s1.id),  # Empty binding ⇒ continue to pin source.
        "turn": await put("turn", created_by_turn="turn_two"),
        "turn_missing_then_canvas": await put("turn_missing_then_canvas", created_by_turn="turn_nope", canvas_id=canvas.id),
        "canvas": await put("canvas", canvas_id=canvas.id),
        "origin": await put("origin", session_id=s1.id),
        "session_row_gone": await put("session_row_gone", created_by_turn="turn_four"),  # Session resolved but row gone ⇒ empty, no further fallback.
        "nothing": await put("nothing"),
        "column_and_binding": await put("column_and_binding", workspace_id="ws_column"),
        # Second instance at each rung (other turn/report/pin session/binding): failure isolation proves one failure does not affect another.
        "turn_b": await put("turn_b", created_by_turn="turn_five"),
        "canvas_b": await put("canvas_b", canvas_id=canvas_b.id),
        "origin_b": await put("origin_b", session_id=s5.id),
        "binding_b": await put("binding_b"),
        "blank_column": await put("blank_column"),  # Whitespace-only column: FAIL_CLOSE strips to empty, falls back to binding.
    }
    async with aiosqlite.connect(db) as conn:
        for label, ws in (
            ("binding", "ws_bind"),
            ("binding_empty", ""),
            ("column_and_binding", "ws_bind"),
            ("binding_b", "ws_bind_b"),
            ("blank_column", "ws_bind_blank"),
        ):
            await conn.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?,?,?,?,?)",
                (ids[label], ws, "u", None, "2026-09-01T00:00:00+00:00"),
            )
        await conn.execute("DELETE FROM sessions WHERE id=?", (s4.id,))
        await conn.execute("UPDATE artifacts SET workspace_id='   ' WHERE id=?", (ids["blank_column"],))
        await conn.commit()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_store=sessions, canvas_store=canvases)))
    arts = {label: await artifacts.get(aid) for label, aid in ids.items()}
    return SimpleNamespace(request=request, store=artifacts, arts=arts, db=db, s1=s1)


_EXPECTED_FLAG_OFF = {
    "binding": "ws_bind",
    "binding_empty": "ws_one",
    "turn": "ws_two",
    "turn_missing_then_canvas": "ws_three",
    "canvas": "ws_three",
    "origin": "ws_one",
    "session_row_gone": "",
    "nothing": "",
    "column_and_binding": "ws_bind",  # Gate OFF: byte-for-byte current behavior, never read column.
    "turn_b": "ws_five",
    "canvas_b": "ws_five",
    "origin_b": "ws_five",
    "binding_b": "ws_bind_b",
    "blank_column": "ws_bind_blank",
}


async def _resolve(ctx, labels) -> dict[str, str]:
    by_id = await files_routes._artifact_workspace_ids(ctx.request, ctx.store, [ctx.arts[label] for label in labels])
    return {label: by_id[ctx.arts[label].id] for label in labels}


async def test_every_rung_resolves_as_the_ladder_defines(ladder) -> None:
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == _EXPECTED_FLAG_OFF


async def test_batch_session_lookup_equals_get_session(ladder) -> None:
    """``workspace_ids_for_sessions`` 是 ``get_session(sid).workspace_id`` 的批量版：存在的逐个相等，不存在的不在字典里。"""
    sessions = ladder.request.app.state.session_store
    async with aiosqlite.connect(ladder.db) as conn:
        ids = [row[0] for row in await (await conn.execute("SELECT id FROM sessions")).fetchall()]
    found = await sessions.workspace_ids_for_sessions([*ids, "s_missing", ""])
    assert set(found) == set(ids)
    for session_id in ids:
        assert found[session_id] == (await sessions.get_session(session_id)).workspace_id


async def test_single_lookup_gives_the_same_answer_as_being_part_of_a_batch(ladder) -> None:
    """单条版在路由里被十几处直接调用：换成批量实现后，每一级单独问也必须是同一个答案。"""
    for label, expected in _EXPECTED_FLAG_OFF.items():
        assert await files_routes._artifact_workspace_id(ladder.request, ladder.store, ladder.arts[label]) == expected, label


async def test_column_is_read_first_only_when_fail_close_is_on(ladder, monkeypatch) -> None:
    monkeypatch.setenv(_FAIL_CLOSE, "1")
    assert await _resolve(ladder, ["column_and_binding", "binding", "blank_column"]) == {
        "column_and_binding": "ws_column",
        "binding": "ws_bind",
        "blank_column": "ws_bind_blank",  # Whitespace-only column has no value: fall back to ladder.
    }


async def test_without_a_session_store_only_bindings_resolve(ladder) -> None:
    ladder.request.app.state.session_store = None
    assert await _resolve(ladder, ["binding", "origin", "turn"]) == {"binding": "ws_bind", "origin": "", "turn": ""}


async def test_connection_count_does_not_grow_with_the_number_of_rows(ladder, monkeypatch) -> None:
    """同形的产物（都 pin 在同一个会话上）：30 行与 1 行的开库次数相同。"""
    extra = [
        await ladder.store.get(
            (await ladder.store.put_bytes(content=b"x", filename=f"more-{i}.csv", mime="text/csv", created_by="u", session_id=ladder.s1.id)).id
        )
        for i in range(30)
    ]
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting)
    await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra[:1])
    one = opened["n"]
    opened["n"] = 0
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra)
    assert set(out.values()) == {"ws_one"}
    assert opened["n"] == one, f"1 行开库 {one} 次，30 行开库 {opened['n']} 次"


async def test_a_failing_batch_lookup_is_retried_one_by_one(ladder, monkeypatch) -> None:
    """``origin_sessions`` 批量查询失败 ⇒ 逐个重试；结果与正常时一致，而不是整批退成「推不出」。"""
    real = ladder.store.origin_sessions

    async def flaky(artifact_ids):
        if len(artifact_ids) > 1:
            raise aiosqlite.OperationalError("database is locked")
        return await real(artifact_ids)

    monkeypatch.setattr(ladder.store, "origin_sessions", flaky)
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == _EXPECTED_FLAG_OFF


async def test_connection_count_does_not_grow_with_the_number_of_turns(ladder, monkeypatch) -> None:
    """每个产物挂在**各自的** turn 上（agent 产物的常态）：按 turn 推会话那一级也必须整批查。"""
    sessions = ladder.request.app.state.session_store
    extra = []
    for i in range(30):
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")  # One ongoing turn per session.
        await sessions.record_turn(turn_id=f"turn_many_{i}", session_id=session.id, user_text="t")
        put = await ladder.store.put_bytes(content=b"x", filename=f"t{i}.csv", mime="text/csv", created_by="u", created_by_turn=f"turn_many_{i}")
        extra.append(await ladder.store.get(put.id))
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting)
    await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra[:2])
    two = opened["n"]
    opened["n"] = 0
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra)
    assert set(out.values()) == {"ws_one"}
    assert opened["n"] == two, f"2 个 turn 开库 {two} 次，30 个 turn 开库 {opened['n']} 次"


async def test_connection_count_does_not_grow_with_the_number_of_canvases(ladder, monkeypatch) -> None:
    """每个产物挂在**各自的**报告上、turn 推不出会话：按报告推会话那一级也必须整批查。"""
    canvases = ladder.request.app.state.canvas_store
    extra = []
    for i in range(30):
        session = await ladder.request.app.state.session_store.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")
        canvas = await canvases.ensure_canvas(session_id=session.id, workspace_id="ws_one", user_id="u")
        put = await ladder.store.put_bytes(content=b"x", filename=f"c{i}.csv", mime="text/csv", created_by="u", canvas_id=canvas.id)
        extra.append(await ladder.store.get(put.id))
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting)
    await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra[:2])
    two = opened["n"]
    opened["n"] = 0
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, extra)
    assert set(out.values()) == {"ws_one"}
    assert opened["n"] == two, f"2 个报告开库 {two} 次，30 个报告开库 {opened['n']} 次"


async def test_batch_canvas_lookup_equals_get_canvas(ladder) -> None:
    """``session_ids_for_canvases`` 是 ``get_canvas(cid).session_id`` 的批量版：存在的逐个相等，不存在的不在字典里；超过一块也全部查得到。"""
    canvases = ladder.request.app.state.canvas_store
    ids = []
    for _ in range(501):
        session = await ladder.request.app.state.session_store.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")
        ids.append((await canvases.ensure_canvas(session_id=session.id, workspace_id="ws_one", user_id="u")).id)
    found = await canvases.session_ids_for_canvases([*ids, "cv_missing", ""])
    assert set(found) == set(ids)
    for canvas_id in ids[:3] + ids[-3:]:
        assert found[canvas_id] == (await canvases.get_canvas(canvas_id)).session_id


async def test_turn_lookup_is_chunked(ladder) -> None:
    """``session_ids_for_turns`` 分块查询：超过一块的 turn 数也全部查得到。"""
    sessions = ladder.request.app.state.session_store
    expected = {}
    for i in range(501):
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")
        await sessions.record_turn(turn_id=f"turn_chunk_{i}", session_id=session.id, user_text="t")
        expected[f"turn_chunk_{i}"] = session.id
    found = await sessions.session_ids_for_turns([*expected, "turn_missing", ""])
    assert found == expected


async def test_binding_lookup_is_chunked(ladder, monkeypatch) -> None:
    """``IN (...)`` 分块查询（SQLite 绑定参数有上限）：块边界两侧的结果都对。"""
    monkeypatch.setattr(files_routes, "_LOOKUP_CHUNK", 2)
    # Binding-only artifacts occupy each chunk's **last** position (odd indexes): one-off boundaries miss them, yielding empty rather than same-value fallbacks.
    labels = ["nothing", "binding", "origin", "binding_b", "turn", "column_and_binding"]
    assert await _resolve(ladder, labels) == {label: _EXPECTED_FLAG_OFF[label] for label in labels}


async def test_a_failing_binding_batch_is_retried_one_by_one(ladder, monkeypatch) -> None:
    """绑定表的批量查询吃到瞬时 ``database is locked`` ⇒ 逐个重试，而不是整批当作「没有绑定」。

    只有绑定这一个来源的产物（``binding``：无会话）最能说明问题——整批吞掉的话它会变成 ""（闸关时＝fail-open）。
    """
    from contextlib import asynccontextmanager

    real_open = files_routes._open_db

    @asynccontextmanager
    async def locked_for_batches(db_path):
        async with real_open(db_path) as db:

            class Proxy:
                def __getattr__(self, name):
                    return getattr(db, name)

                def execute(self, sql, params=()):
                    if "artifact_upload_bindings" in sql and len(params) > 1:
                        raise aiosqlite.OperationalError("database is locked")
                    return db.execute(sql, params)

            yield Proxy()

    monkeypatch.setattr(files_routes, "_open_db", locked_for_batches)
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == _EXPECTED_FLAG_OFF


def _fail_for(obj, method: str, should_fail) -> None:
    real = getattr(obj, method)

    async def wrapper(arg, *rest, **kw):
        if should_fail(arg):
            raise aiosqlite.OperationalError("disk I/O error")
        return await real(arg, *rest, **kw)

    return wrapper


@pytest.mark.parametrize("rung", ["turn", "canvas", "origin_retry", "binding_retry"])
async def test_one_failing_lookup_never_spreads_to_other_artifacts_in_the_batch(ladder, monkeypatch, rung) -> None:
    """每一级都一样：一个产物的查询出错，只让**它自己**推不出；同批里同一级的其它产物照常解析。

    「推不出」在闸关时是 fail-open，所以一个故障扩散成一整批，就是一整批放行。
    （turn / canvas / origin / binding 四级先让批量查询整体失败，逐个重试时再让其中**较早**的一个失败——``break`` / ``return`` 式的漏写会连累排在后面的。）
    """
    sessions = ladder.request.app.state.session_store
    canvases = ladder.request.app.state.canvas_store
    arts = ladder.arts
    if rung == "turn":
        monkeypatch.setattr(sessions, "session_ids_for_turns", _fail_for(sessions, "session_ids_for_turns", lambda ids: True))
        monkeypatch.setattr(sessions, "get_turn", _fail_for(sessions, "get_turn", lambda tid: tid == "turn_two"))
        broken = {"turn": ""}
    elif rung == "canvas":
        bad = arts["canvas"].canvas_id
        monkeypatch.setattr(canvases, "session_ids_for_canvases", _fail_for(canvases, "session_ids_for_canvases", lambda ids: True))
        monkeypatch.setattr(canvases, "get_canvas", _fail_for(canvases, "get_canvas", lambda cid: cid == bad))
        broken = {"canvas": "", "turn_missing_then_canvas": ""}
    elif rung == "origin_retry":
        bad = arts["binding_empty"].id  # Before origin / origin_b.
        monkeypatch.setattr(ladder.store, "origin_sessions", _fail_for(ladder.store, "origin_sessions", lambda ids: len(ids) > 1 or ids == [bad]))
        broken = {"binding_empty": ""}
    else:
        from contextlib import asynccontextmanager

        real_open = files_routes._open_db
        bad = arts["binding"].id  # Before column_and_binding / binding_b / blank_column.

        @asynccontextmanager
        async def flaky(db_path):
            async with real_open(db_path) as db:

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def execute(self, sql, params=()):
                        if "artifact_upload_bindings" in sql and (len(params) > 1 or list(params) == [bad]):
                            raise aiosqlite.OperationalError("database is locked")
                        return db.execute(sql, params)

                yield Proxy()

        monkeypatch.setattr(files_routes, "_open_db", flaky)
        broken = {"binding": ""}
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == {**_EXPECTED_FLAG_OFF, **broken}


async def test_connection_count_grows_with_distinct_sessions_not_with_rows(ladder, monkeypatch) -> None:
    """形状二：每个产物各有自己的 turn 与会话。开库次数随**不同的 turn / 会话**增长，不随行数重复计：
    同一批里把每个产物重复一遍（行数翻倍），开库次数不变。"""
    sessions = ladder.request.app.state.session_store
    arts = []
    for i in range(6):
        s = await sessions.ensure_session(session_id=None, workspace_id=f"ws_many_{i}", user_id="u")
        await sessions.record_turn(turn_id=f"turn_many_{i}", session_id=s.id, user_text="t")
        art = await ladder.store.put_bytes(content=b"x", filename=f"many-{i}.csv", mime="text/csv", created_by="u", created_by_turn=f"turn_many_{i}")
        arts.append(await ladder.store.get(art.id))
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting)
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, arts)
    once = opened["n"]
    opened["n"] = 0
    await files_routes._artifact_workspace_ids(ladder.request, ladder.store, arts + arts)
    assert sorted(out.values()) == [f"ws_many_{i}" for i in range(6)]
    assert opened["n"] == once, f"同样 6 个产物各出现两次：开库 {opened['n']} 次，出现一次时是 {once} 次"


async def test_one_failing_session_lookup_only_affects_its_own_artifacts(ladder, monkeypatch) -> None:
    """会话那一级先批量查；批量失败 ⇒ 逐个重试，逐个时某一个出错只让挂在它上面的产物推不出。"""
    sessions = ladder.request.app.state.session_store
    real = sessions.get_session

    async def batch_fails(_session_ids):
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(sessions, "workspace_ids_for_sessions", batch_fails)

    async def get_session(session_id):
        turn = await sessions.get_turn("turn_two")
        if session_id == turn.session_id:
            raise aiosqlite.OperationalError("disk I/O error")
        return await real(session_id)

    monkeypatch.setattr(sessions, "get_session", get_session)
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == {**_EXPECTED_FLAG_OFF, "turn": ""}  # Only that session's artifact is unresolved.


async def _many_on_own_turns(ladder, n: int) -> list:
    sessions = ladder.request.app.state.session_store
    arts = []
    for i in range(n):
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_one", user_id="u")
        await sessions.record_turn(turn_id=f"turn_bulk_{i}", session_id=session.id, user_text="t")
        put = await ladder.store.put_bytes(content=b"x", filename=f"b{i}.csv", mime="text/csv", created_by="u", created_by_turn=f"turn_bulk_{i}")
        arts.append(await ladder.store.get(put.id))
    return arts


async def test_a_large_failing_batch_raises_instead_of_one_lookup_per_row(ladder, monkeypatch) -> None:
    """库本身出问题时逐个重试＝上千次开库（2026-09-24 116 事故的形状）⇒ 大块连失败两次直接报错，一次单条都不查。"""
    arts = await _many_on_own_turns(ladder, files_routes._PER_ITEM_RETRY_MAX + 1)
    sessions = ladder.request.app.state.session_store
    singles = {"n": 0}
    real_get_turn = sessions.get_turn

    async def broken(turn_ids):
        raise aiosqlite.OperationalError("unable to open database file")

    async def counting_get_turn(turn_id):
        singles["n"] += 1
        return await real_get_turn(turn_id)

    monkeypatch.setattr(sessions, "session_ids_for_turns", broken)
    monkeypatch.setattr(sessions, "get_turn", counting_get_turn)
    with pytest.raises(aiosqlite.OperationalError):
        await files_routes._artifact_workspace_ids(ladder.request, ladder.store, arts)
    assert singles["n"] == 0


async def test_a_transient_batch_failure_is_retried_as_a_batch(ladder, monkeypatch) -> None:
    """一次瞬时 ``database is locked`` ⇒ 整块再查一次就好，不退化成逐个查。"""
    arts = await _many_on_own_turns(ladder, files_routes._PER_ITEM_RETRY_MAX + 1)
    sessions = ladder.request.app.state.session_store
    real_batch = sessions.session_ids_for_turns
    calls = {"batch": 0, "single": 0}

    async def flaky_once(turn_ids):
        calls["batch"] += 1
        if calls["batch"] == 1:
            raise aiosqlite.OperationalError("database is locked")
        return await real_batch(turn_ids)

    async def no_single(turn_id):
        calls["single"] += 1
        raise AssertionError("should not fall back to per-row lookups")

    monkeypatch.setattr(sessions, "session_ids_for_turns", flaky_once)
    monkeypatch.setattr(sessions, "get_turn", no_single)
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, arts)
    assert set(out.values()) == {"ws_one"}
    assert calls == {"batch": 2, "single": 0}


async def test_every_rung_is_chunked(ladder, monkeypatch) -> None:
    """分块边界两侧都查得到：块大小压到 2，全梯子的结果与不分块时一致。"""
    monkeypatch.setattr(files_routes, "_LOOKUP_CHUNK", 2)
    assert await _resolve(ladder, list(_EXPECTED_FLAG_OFF)) == _EXPECTED_FLAG_OFF


async def test_lookups_are_really_split_into_chunks(ladder, monkeypatch) -> None:
    """块大小 2、5 个不同 turn ⇒ 批量查询至少 3 次，且每次不超过 2 个 id（结果对不代表真的分了块）。"""
    monkeypatch.setattr(files_routes, "_LOOKUP_CHUNK", 2)
    arts = await _many_on_own_turns(ladder, 5)
    sessions = ladder.request.app.state.session_store
    real_batch = sessions.session_ids_for_turns
    sizes: list[int] = []

    async def recording(turn_ids):
        sizes.append(len(turn_ids))
        return await real_batch(turn_ids)

    monkeypatch.setattr(sessions, "session_ids_for_turns", recording)
    out = await files_routes._artifact_workspace_ids(ladder.request, ladder.store, arts)
    assert set(out.values()) == {"ws_one"}
    assert sizes == [2, 2], sizes  # Third chunk has one id: use single-item path.
