from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.user_store import User
from bowhead_gateway.routes import unified_search as subject
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI

from bowhead_core import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


def _user() -> User:
    return User(
        id="usr_viewer",
        team_id="team_1",
        email="viewer@example.com",
        email_verified=True,
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(subject.router)
    app.dependency_overrides[_current_user] = _user
    return app


@pytest.mark.asyncio
async def test_unified_search_keeps_group_order_and_isolates_source_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sessions(*_args, **_kwargs):
        return [
            subject.UnifiedSearchItemDTO(
                kind="session",
                id="s_1",
                title="季度收入",
                session_id="s_1",
            ),
        ]

    async def reports(*_args, **_kwargs):
        raise RuntimeError("database password and query must never escape")

    async def files(*_args, **_kwargs):
        return []

    async def knowledge(*_args, **_kwargs):
        return []

    monkeypatch.setattr(subject, "_search_sessions", sessions)
    monkeypatch.setattr(subject, "_search_reports", reports)
    monkeypatch.setattr(subject, "_search_files", files)
    monkeypatch.setattr(subject, "_search_knowledge", knowledge)

    transport = httpx2.ASGITransport(app=_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/search",
            json={"query": "季度收入"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "季度收入"
    assert body["partial"] is True
    assert response.headers["cache-control"] == "private, no-store"
    assert [group["kind"] for group in body["groups"]] == [
        "session",
        "report",
        "file",
        "knowledge",
    ]
    report_group = body["groups"][1]
    assert report_group == {"kind": "report", "unavailable": True, "timed_out": False, "items": [], "total": None, "next_cursor": None}
    assert "password" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["a", "   "])
async def test_unified_search_rejects_queries_shorter_than_two_after_trim(
    query: str,
) -> None:
    transport = httpx2.ASGITransport(app=_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/search",
            json={"query": query},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unified_search_has_no_query_string_get_contract() -> None:
    transport = httpx2.ASGITransport(app=_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/search", params={"q": "sensitive"})

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_unified_search_marks_a_timed_out_source_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return []

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(subject, "_SOURCE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(subject, "_search_sessions", slow)
    monkeypatch.setattr(subject, "_search_reports", empty)
    monkeypatch.setattr(subject, "_search_files", empty)
    monkeypatch.setattr(subject, "_search_knowledge", empty)

    transport = httpx2.ASGITransport(app=_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/search",
            json={"query": "sensitive"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    # A timed-out source says so: its empty items mean "not searched in time", not "no matches".
    assert body["groups"][0] == {
        "kind": "session",
        "unavailable": True,
        "timed_out": True,
        "items": [],
        "total": None,
        "next_cursor": None,
    }
    assert all(group["timed_out"] is False for group in body["groups"][1:])


@pytest.mark.asyncio
async def test_unified_search_returns_503_when_every_source_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(subject, "_search_sessions", unavailable)
    monkeypatch.setattr(subject, "_search_reports", unavailable)
    monkeypatch.setattr(subject, "_search_files", unavailable)
    monkeypatch.setattr(subject, "_search_knowledge", unavailable)

    transport = httpx2.ASGITransport(app=_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/search",
            json={"query": "sensitive"},
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["partial"] is True


@pytest.mark.asyncio
async def test_session_and_file_sources_force_authenticated_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class SessionStore:
        async def list_session_workspace_ids(self, *, user_id: str):
            calls.append(("session_spaces", user_id))
            return ["ws_any"]

        async def first_matching_user_texts(self, **_kwargs):
            return {}

        async def count_sessions(self, **_kwargs):
            return 1

        async def list_sessions(self, *, user_id: str, **_kwargs):
            calls.append(("session", user_id))
            return [
                SimpleNamespace(
                    id="s_owned",
                    title="Owned session",
                    last_active_at="2026-07-22T00:00:00Z",
                    workspace_id="ws_any",
                ),
            ]

    class ArtifactStore:
        async def list_for_user(self, *, user_id: str, **_kwargs):
            calls.append(("file", user_id))
            return (
                [
                    SimpleNamespace(
                        id="art_owned",
                        filename="owned.csv",
                        last_used_at=None,
                        created_at="2026-07-22T00:00:00Z",
                    ),
                ],
                1,
                10,
            )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=SessionStore(),
                artifact_store=ArtifactStore(),
            ),
        ),
    )

    async def readable_scope(_request, _store, _art):
        return subject.files_routes._ArtifactScope("space", "ws_any", False)

    async def readable(*_args, **_kwargs):
        return None

    async def all_spaces_readable(_request, *, workspace_ids, **_kwargs):
        return set(workspace_ids)

    # Verify only fetching by logged-in user; dedicated cases test per-item checks. Allow all here to avoid auth-mode-dependent results.
    _patch_scope(monkeypatch, readable_scope)
    monkeypatch.setattr(subject.files_routes, "_require_artifact_scope_access", readable)
    monkeypatch.setattr(subject.sessions_routes, "readable_session_workspaces", all_spaces_readable)

    sessions = await subject._search_sessions(request, "usr_viewer", "owned", 5)
    files = await subject._search_files(request, "usr_viewer", "owned", 5)

    assert calls == [("session_spaces", "usr_viewer"), ("session", "usr_viewer"), ("file", "usr_viewer")]
    assert [item.id for item in sessions.items] == ["s_owned"]
    assert [item.id for item in files] == ["art_owned"]


@pytest.mark.asyncio
async def test_session_source_excludes_explicitly_archived_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    await store.initialize()
    session = await store.ensure_session(
        session_id="s_archived",
        workspace_id="w1",
        user_id="usr_viewer",
    )
    await store.set_title(session_id=session.id, title="Archived revenue")
    assert (
        await store.archive_session(
            user_id="usr_viewer",
            session_id=session.id,
        )
        == "ok"
    )

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_store=store)),
    )
    page = await subject._search_sessions(
        request,
        "usr_viewer",
        "revenue",
        5,
    )
    assert page.items == []
    assert page.total == 0


@pytest.mark.asyncio
async def test_report_source_only_queries_grant_resolved_shared_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_calls: list[list[str] | None] = []

    class CanvasStore:
        async def list_for_user(self, *, session_ids=None, **_kwargs):
            shared_calls.append(session_ids)
            if session_ids is None:
                return []
            assert session_ids == ["s_allowed"]
            return [
                {
                    "id": "cv_shared",
                    "session_id": "s_allowed",
                    "title": "Shared report",
                    "updated_at": "2026-07-22T00:00:00Z",
                },
            ]

    async def allowed(*_args, **_kwargs):
        return frozenset({"s_allowed"})

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", allowed)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())),
    )

    results = await subject._search_reports(
        request,
        "usr_viewer",
        "shared",
        5,
    )

    assert shared_calls == [None, ["s_allowed"]]
    assert [(item.id, item.session_id) for item in results] == [
        ("cv_shared", "s_allowed"),
    ]


@pytest.mark.asyncio
async def test_knowledge_source_falls_back_to_authorized_file_metadata_when_semantic_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable_semantic(*_args, **_kwargs):
        raise subject.HTTPException(503, {"error": {"code": "kb_service_unavailable"}})

    async def list_files(*_args, scope: str, **_kwargs):
        if scope != "personal":
            raise subject.HTTPException(403, {"error": {"code": "resource_grant_denied"}})
        return SimpleNamespace(
            files=[
                SimpleNamespace(
                    file_id="kbf_canvas",
                    filename="Bowhead Canvas guide.md",
                    uploaded_at="2026-08-08T00:00:00Z",
                ),
            ],
        )

    monkeypatch.setattr(subject.kb_routes, "search", unavailable_semantic)
    monkeypatch.setattr(subject.kb_routes, "list_files", list_files)

    results = await subject._search_knowledge(
        SimpleNamespace(),
        _user(),
        "Bowhead Canvas",
        5,
    )

    assert [(item.id, item.title) for item in results] == [
        ("kbf_canvas", "Bowhead Canvas guide.md"),
    ]


@pytest.mark.asyncio
async def test_knowledge_source_merges_semantic_snippets_with_metadata_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def semantic(*_args, **_kwargs):
        return SimpleNamespace(
            chunks=[
                SimpleNamespace(
                    file_id="kbf_canvas",
                    file_name="Bowhead Canvas guide.md",
                    content="Canvas evidence snippet",
                ),
            ],
        )

    async def list_files(*_args, scope: str, **_kwargs):
        files = []
        if scope == "personal":
            files = [
                SimpleNamespace(
                    file_id="kbf_canvas",
                    filename="Bowhead Canvas guide.md",
                    uploaded_at="2026-08-08T00:00:00Z",
                ),
                SimpleNamespace(
                    file_id="kbf_other",
                    filename="Bowhead operations.md",
                    uploaded_at="2026-08-07T00:00:00Z",
                ),
            ]
        return SimpleNamespace(files=files)

    monkeypatch.setattr(subject.kb_routes, "search", semantic)
    monkeypatch.setattr(subject.kb_routes, "list_files", list_files)

    results = await subject._search_knowledge(
        SimpleNamespace(),
        _user(),
        "Bowhead",
        5,
    )

    assert [item.id for item in results] == ["kbf_canvas", "kbf_other"]
    assert results[0].snippet == "Canvas evidence snippet"


@pytest.mark.asyncio
async def test_knowledge_source_does_not_report_partial_when_semantic_search_succeeds_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def semantic(*_args, **_kwargs):
        return SimpleNamespace(chunks=[])

    async def unavailable_metadata(*_args, **_kwargs):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(subject.kb_routes, "search", semantic)
    monkeypatch.setattr(subject, "_search_knowledge_metadata", unavailable_metadata)

    results = await subject._search_knowledge(
        SimpleNamespace(),
        _user(),
        "missing knowledge",
        5,
    )

    assert results == []


@pytest.mark.asyncio
async def test_knowledge_source_is_unavailable_only_when_both_paths_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(subject, "_search_knowledge_semantic", unavailable)
    monkeypatch.setattr(subject, "_search_knowledge_metadata", unavailable)

    with pytest.raises(RuntimeError, match="unavailable"):
        await subject._search_knowledge(
            SimpleNamespace(),
            _user(),
            "missing knowledge",
            5,
        )


def test_snippet_is_normalized_and_bounded() -> None:
    snippet = subject._snippet("first\n\n" + "x" * 300)

    assert snippet is not None
    assert "\n" not in snippet
    assert len(snippet) == 240
    assert snippet.endswith("…")


# ── ``_fetch_visible``: fetch limit first; insufficient readable rows → widen ×4; stop on exhaustion/cap ──


def _newest_first_store(total: int):
    """最新在前的假 store：共 ``total`` 行，行就是序号 0..total-1（0 最新）；``calls`` 记下每次被要求的行数。"""
    calls: list[int] = []

    async def fetch(n: int) -> list[int]:
        calls.append(n)
        assert len(calls) <= 10, f"加宽没有终止：{calls}"  # Fail infinite widening quickly, not hang.
        return list(range(min(n, total)))

    return fetch, calls


def _readable_from(first_readable: int):
    async def keep(rows: list[int]) -> list[int]:
        return [row for row in rows if row >= first_readable]

    return keep


@pytest.mark.asyncio
async def test_fetch_visible_takes_one_window_when_enough_rows_are_readable() -> None:
    fetch, calls = _newest_first_store(100)
    assert await subject._fetch_visible(fetch, _readable_from(0), 3) == [0, 1, 2]
    assert calls == [3]  # First window=limit; no hidden rows means one fetch, no extra row checked.


@pytest.mark.asyncio
async def test_fetch_visible_widens_until_enough_rows_are_readable() -> None:
    fetch, calls = _newest_first_store(100)
    assert await subject._fetch_visible(fetch, _readable_from(30), 5) == [30, 31, 32, 33, 34]  # Preserve order, trim to limit.
    assert calls == [5, 20, 80]  # First two windows hidden → ×4 each; third has 50 readable of 80, enough.


@pytest.mark.asyncio
async def test_fetch_visible_stops_when_the_store_has_no_more_rows() -> None:
    fetch, calls = _newest_first_store(6)
    assert await subject._fetch_visible(fetch, _readable_from(100), 5) == []
    assert calls == [5, 20]  # Second window returns 6 <20⇒exhausted; widening cannot add rows.


@pytest.mark.asyncio
async def test_fetch_visible_stops_at_the_cap_and_returns_fewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_VISIBLE_MAX_FETCH", 50)
    fetch, calls = _newest_first_store(1000)
    assert await subject._fetch_visible(fetch, _readable_from(500), 5) == []  # Readable rows beyond 500 unreachable; return fewer.
    assert calls == [5, 20, 50]  # 20→min(80, 50); stop at cap, never widen endlessly.


@pytest.mark.asyncio
async def test_fetch_visible_first_window_never_exceeds_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_VISIBLE_MAX_FETCH", 3)
    fetch, calls = _newest_first_store(1000)
    assert await subject._fetch_visible(fetch, _readable_from(0), 5) == [0, 1, 2]  # Cap <limit: stop there, return fewer.
    assert calls == [3]  # First-window limit=5 clamped to cap 3.


@pytest.mark.asyncio
async def test_report_source_widens_each_tier_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """「被分享」那一档最新的 10 条都在已不可读的空间里：``limit=1`` 时第一窗（4 行）全被滤掉，必须只对**这一档**加宽。"""
    calls: list[tuple[bool, int]] = []  # (shared tier?, requested row count).
    shared_rows = [
        {
            "id": f"cv_{i}",
            "session_id": f"s_{i}",
            "user_id": "usr_owner",
            "workspace_id": "ws_dead" if i < 10 else "ws_ok",
            "title": "Shared report",
            "updated_at": f"2026-07-{30 - i:02d}T00:00:00Z",
        }
        for i in range(12)
    ]  # Newest first.

    class CanvasStore:
        async def list_for_user(self, *, limit, session_ids=None, **_kwargs):
            calls.append((session_ids is not None, limit))
            return [] if session_ids is None else shared_rows[:limit]

    async def allowed(*_args, **_kwargs):
        return frozenset(f"s_{i}" for i in range(12))

    async def readable_only(_request, _user_id, items, *, enforce_space_membership=True):
        return [item for item in items if item["workspace_id"] != "ws_dead"]  # Filtering only; actual decisions have dedicated tests.

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", allowed)
    monkeypatch.setattr(subject.canvas_routes, "_filter_report_history_items", readable_only)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())))

    results = await subject._search_reports(request, "usr_viewer", "shared", 1)

    assert [item.id for item in results] == ["cv_10"]
    assert sorted(calls) == [(False, 1), (True, 1), (True, 4), (True, 16)]  # Own tier empty⇒one window exhausted; shared tier 1→4→16.


@pytest.mark.asyncio
async def test_fetch_visible_stops_when_exactly_limit_rows_are_readable() -> None:
    fetch, calls = _newest_first_store(100)
    assert await subject._fetch_visible(fetch, _readable_from(12), 4) == [12, 13, 14, 15]  # Second window: exactly 4 of 16 readable.
    assert calls == [4, 16]  # Exactly enough: stop, no widening.


@pytest.mark.asyncio
async def test_report_source_merges_both_tiers_newest_first_and_cuts_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """两档各自都有 ``limit`` 条：合并后按 ``updated_at`` 从新到旧，只给最新的 ``limit`` 条（两档穿插）。"""

    def row(prefix: str, i: int, day: int) -> dict[str, str]:
        return {
            "id": f"{prefix}{i}",
            "session_id": f"s_{prefix}{i}",
            "user_id": "usr_viewer" if prefix == "m" else "usr_owner",
            "workspace_id": "ws_ok",
            "title": "Report",
            "updated_at": f"2026-07-{day:02d}T00:00:00Z",
        }

    mine = [row("m", 0, 30), row("m", 1, 28), row("m", 2, 26)]
    shared = [row("s", 0, 29), row("s", 1, 27), row("s", 2, 25)]

    class CanvasStore:
        async def list_for_user(self, *, limit, session_ids=None, **_kwargs):
            return (mine if session_ids is None else shared)[:limit]

    async def allowed(*_args, **_kwargs):
        return frozenset({"s_s0", "s_s1", "s_s2"})

    async def pass_through(_request, _user_id, items, *, enforce_space_membership=True):
        return items

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", allowed)
    monkeypatch.setattr(subject.canvas_routes, "_filter_report_history_items", pass_through)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())))

    results = await subject._search_reports(request, "usr_viewer", "report", 3)

    assert [item.id for item in results] == ["m0", "s0", "m1"]


@pytest.mark.asyncio
async def test_fetch_visible_keeps_widening_while_fewer_than_limit_rows_are_readable() -> None:
    fetch, calls = _newest_first_store(100)
    # First window (0..19) has only 2 readable (18, 19), below limit; cannot stop yet.
    assert await subject._fetch_visible(fetch, _readable_from(18), 5) == [18, 19, 20, 21, 22]
    assert calls == [5, 20, 80]


def _file_rows(ids) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=fid, filename=f"{fid}.csv", last_used_at=None, created_at=f"2026-07-{20 - i:02d}") for i, fid in enumerate(ids)]


def _files_request(rows) -> SimpleNamespace:
    class ArtifactStore:
        async def list_for_user(self, *, limit, **_kwargs):
            return rows[:limit], len(rows), 0

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=ArtifactStore())))


def _patch_scope(monkeypatch: pytest.MonkeyPatch, per_row) -> None:
    """文件来源按批推导归属（``_artifact_scopes``）；用例只关心「每个产物的归属是什么」，按行给出即可。"""

    async def scopes(request, store, arts):
        return {art.id: await per_row(request, store, art) for art in arts}

    monkeypatch.setattr(subject.files_routes, "_artifact_scopes", scopes)


def _gone_is_denied(monkeypatch: pytest.MonkeyPatch, judged: list[str] | None = None) -> None:
    """``ws_gone`` 判不可读（403），其余可读；``judged`` 记下每次被判的空间。"""

    async def access(_request, scope, *, user_id, operation):
        if judged is not None:
            judged.append(scope.workspace_id)
        if scope.workspace_id == "ws_gone":
            raise subject.HTTPException(403, {"error": {"code": "workspace_access_denied"}})

    monkeypatch.setattr(subject.files_routes, "_require_artifact_scope_access", access)


@pytest.mark.asyncio
async def test_file_source_filters_every_row_and_judges_each_scope_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """可读行夹在两段不可读行中间（同一个不可读归属出现两次）：后一段不可读行不能因为「前面已经见过可读行」而漏出来。"""
    workspace_of = {"f_0": "ws_gone", "f_1": "ws_gone", "f_2": "ws_keep", "f_3": "ws_keep", "f_4": "ws_gone", "f_5": "ws_gone"}
    judged: list[str] = []

    async def scope_of(_request, _store, art):
        return subject.files_routes._ArtifactScope("space", workspace_of[art.id], False)

    _patch_scope(monkeypatch, scope_of)
    _gone_is_denied(monkeypatch, judged)

    results = await subject._search_files(_files_request(_file_rows(workspace_of)), "usr_viewer", "csv", 5)

    assert [item.id for item in results] == ["f_2", "f_3"]
    assert judged == ["ws_gone", "ws_keep"]  # Check each attribution once.


@pytest.mark.asyncio
async def test_file_source_judges_the_scope_it_keyed_on_and_never_asks_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """归属推导遇到暂态故障会被吞成「推不出」（fail-open）。键与判定若来自两次各自的推导，一次故障就会把「可读」缓存给同一归属的所有行。

    这里让每个产物**第二次**被问归属时给出 fail-open 的答案：只要每行只推导一次，4 条被移出空间的文件就一条都不该漏出来。
    """
    asked: dict[str, int] = {}

    async def flaky_scope(_request, _store, art):
        asked[art.id] = asked.get(art.id, 0) + 1
        return subject.files_routes._ArtifactScope("space", "ws_gone" if asked[art.id] == 1 else "", False)

    _patch_scope(monkeypatch, flaky_scope)
    _gone_is_denied(monkeypatch)

    results = await subject._search_files(_files_request(_file_rows(["f_0", "f_1", "f_2", "f_3"])), "usr_viewer", "csv", 5)

    assert results == []
    assert asked == {"f_0": 1, "f_1": 1, "f_2": 1, "f_3": 1}


@pytest.mark.asyncio
async def test_file_source_keeps_fail_close_in_the_verdict_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``unattributed`` 的两档（切点后 ``fail_close=True`` 拒绝 / 切点前 ``False`` 保持今天）只差 ``fail_close``。

    键里漏了它，先出现的那一行的判定就会盖住另一档。判定用真实的 ``_require_artifact_scope_access``（这一支不碰 request）。
    """
    fail_close_of = {"f_0": True, "f_1": False, "f_2": True, "f_3": False}

    async def scope_of(_request, _store, art):
        return subject.files_routes._ArtifactScope("unattributed", "", fail_close_of[art.id])

    _patch_scope(monkeypatch, scope_of)

    results = await subject._search_files(_files_request(_file_rows(fail_close_of)), "usr_viewer", "csv", 5)

    assert [item.id for item in results] == ["f_1", "f_3"]


@pytest.mark.asyncio
async def test_file_source_fails_closed_on_unexpected_access_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 403 / 404 表示「不可读」；判定本身出错（如 503）时整个来源报错（对外＝该来源 unavailable），

    既不能当成「可读」放行，也不能悄悄当成「没有」把用户蒙在鼓里。
    """

    async def scope_of(_request, _store, _art):
        return subject.files_routes._ArtifactScope("space", "ws_any", False)

    async def unavailable(_request, _scope, *, user_id, operation):
        raise subject.HTTPException(503, {"error": {"code": "authz_unavailable"}})

    _patch_scope(monkeypatch, scope_of)
    monkeypatch.setattr(subject.files_routes, "_require_artifact_scope_access", unavailable)

    with pytest.raises(subject.HTTPException) as caught:
        await subject._search_files(_files_request(_file_rows(["f_0"])), "usr_viewer", "csv", 5)

    assert caught.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_status", [403, 404], ids=["forbidden", "not_found"])
async def test_file_source_hides_a_row_when_the_verdict_says_forbidden_or_not_found(monkeypatch: pytest.MonkeyPatch, denied_status: int) -> None:
    """列表路由把 403 和 404 都当作「不可读」（404＝反枚举的拒绝口径）；搜索必须同口径，只认其中一个会把另一个放行或让整个来源报错。"""

    async def scope_of(_request, _store, _art):
        return subject.files_routes._ArtifactScope("space", "ws_any", False)

    async def denied(_request, _scope, *, user_id, operation):
        raise subject.HTTPException(denied_status, {"error": {"code": "denied"}})

    _patch_scope(monkeypatch, scope_of)
    monkeypatch.setattr(subject.files_routes, "_require_artifact_scope_access", denied)

    assert await subject._search_files(_files_request(_file_rows(["f_0", "f_1"])), "usr_viewer", "csv", 5) == []


# ── Review-added gaps: query passthrough / filter errors / attribution key / merge dedup / third window ──


@pytest.mark.asyncio
async def test_fetch_visible_reaches_the_third_and_cap_windows() -> None:
    fetch, calls = _newest_first_store(1000)
    assert await subject._fetch_visible(fetch, _readable_from(50), 1) == [50]
    assert calls == [1, 4, 16, 64]  # ×4 each: 1→4→16→64 (row 50 lies in 64-row window).

    fetch, calls = _newest_first_store(1000)
    assert await subject._fetch_visible(fetch, _readable_from(90), 1) == [90]
    assert calls == [1, 4, 16, 64, 100]  # 64×4=256 clamped to default cap 100.


@pytest.mark.asyncio
async def test_every_source_passes_the_query_to_its_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """丢掉 ``q`` 不会让任何既有用例变红——但搜任何词都会返回全部会话 / 文件 / 报告。"""
    seen: dict[str, object] = {"report": []}

    class SessionStore:
        async def list_session_workspace_ids(self, **_kwargs):
            return []

        async def first_matching_user_texts(self, **_kwargs):
            return {}

        async def list_sessions(self, *, q=None, **_kwargs):
            seen["session"] = q
            return []

    class ArtifactStore:
        async def list_for_user(self, *, q=None, **_kwargs):
            seen["file"] = q
            return [], 0, 0

    class CanvasStore:
        async def list_for_user(self, *, q=None, **_kwargs):
            seen["report"].append(q)
            return []

    async def no_grants(*_args, **_kwargs):
        return frozenset()

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", no_grants)
    state = SimpleNamespace(session_store=SessionStore(), artifact_store=ArtifactStore(), canvas_store=CanvasStore())
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    await subject._search_sessions(request, "usr_viewer", "needle", 5)
    await subject._search_files(request, "usr_viewer", "needle", 5)
    await subject._search_reports(request, "usr_viewer", "needle", 5)

    assert seen == {"session": "needle", "file": "needle", "report": ["needle", "needle"]}


@pytest.mark.asyncio
async def test_session_source_fails_closed_when_the_filter_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """过滤器出错时该来源必须报错（对外＝unavailable）：吞掉异常、退回未过滤的行就是 fail-open。"""

    class SessionStore:
        async def list_session_workspace_ids(self, **_kwargs):
            return ["ws_any"]

        async def list_sessions(self, **_kwargs):
            return [SimpleNamespace(id="s_1", title="t", last_active_at="2026-07-22T00:00:00Z", workspace_id="ws_any")]

    async def boom(*_args, **_kwargs):
        raise RuntimeError("filter failed")

    monkeypatch.setattr(subject.sessions_routes, "readable_session_workspaces", boom)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_store=SessionStore())))

    with pytest.raises(RuntimeError, match="filter failed"):
        await subject._search_sessions(request, "usr_viewer", "t", 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_tier", ["mine", "shared"])
async def test_report_source_fails_closed_when_either_tier_filter_errors(monkeypatch: pytest.MonkeyPatch, failing_tier: str) -> None:

    def row(prefix: str) -> dict[str, str]:
        return {
            "id": f"cv_{prefix}",
            "session_id": f"s_{prefix}",
            "user_id": "usr_viewer" if prefix == "m" else "usr_owner",
            "workspace_id": "ws_any",
            "updated_at": "2026-07-22T00:00:00Z",
        }

    class CanvasStore:
        async def list_for_user(self, *, session_ids=None, **_kwargs):
            return [row("m")] if session_ids is None else [row("s")]

    async def allowed(*_args, **_kwargs):
        return frozenset({"s_s"})

    async def boom_for_one_tier(_request, _user_id, items, *, enforce_space_membership=True):
        if (failing_tier == "mine") == enforce_space_membership:  # Own tier enforce=True; shared tier enforce=False.
            raise RuntimeError("filter failed")
        return items

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", allowed)
    monkeypatch.setattr(subject.canvas_routes, "_filter_report_history_items", boom_for_one_tier)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())))

    with pytest.raises(RuntimeError, match="filter failed"):
        await subject._search_reports(request, "usr_viewer", "r", 5)


@pytest.mark.asyncio
async def test_report_source_lists_a_report_once_even_when_both_tiers_return_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """属主把报告分享给全团队时，同一个 id 会同时出现在「自己的」和「被分享」两档里。"""
    same = {"id": "cv_1", "session_id": "s_1", "user_id": "usr_viewer", "workspace_id": "ws_any", "title": "R", "updated_at": "2026-07-22T00:00:00Z"}

    class CanvasStore:
        async def list_for_user(self, **_kwargs):
            return [same]

    async def allowed(*_args, **_kwargs):
        return frozenset({"s_1"})

    async def pass_through(_request, _user_id, items, *, enforce_space_membership=True):
        return items

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", allowed)
    monkeypatch.setattr(subject.canvas_routes, "_filter_report_history_items", pass_through)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())))

    results = await subject._search_reports(request, "usr_viewer", "r", 5)

    assert [item.id for item in results] == ["cv_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("team_first", [True, False], ids=["team_first", "unattributed_first"])
async def test_file_source_keys_verdicts_on_the_scope_kind_too(monkeypatch: pytest.MonkeyPatch, team_first: bool) -> None:
    """团队级（``team``）与切点前的 ``unattributed`` 的 ``workspace_id`` 都是 ``""``、``fail_close`` 都是 ``False``，只差 ``kind``——判定却可以相反。"""
    kind_of = {"f_team": "team", "f_open": "unattributed"}
    order = ["f_team", "f_open"] if team_first else ["f_open", "f_team"]

    async def scope_of(_request, _store, art):
        return subject.files_routes._ArtifactScope(kind_of[art.id], "", False)

    async def access(_request, scope, *, user_id, operation):
        if scope.kind == "team":
            raise subject.HTTPException(403, {"error": {"code": "workspace_access_denied"}})

    _patch_scope(monkeypatch, scope_of)
    monkeypatch.setattr(subject.files_routes, "_require_artifact_scope_access", access)

    results = await subject._search_files(_files_request(_file_rows(order)), "usr_viewer", "csv", 5)

    assert [item.id for item in results] == ["f_open"]


@pytest.mark.asyncio
async def test_file_source_resolves_the_scope_per_artifact_not_per_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同空间里可以有同名文件：归属必须按产物 id 记，不能按文件名。"""
    workspace_of = {"f_gone": "ws_gone", "f_keep": "ws_keep"}
    rows = [
        SimpleNamespace(id=fid, filename="report.csv", last_used_at=None, created_at=f"2026-07-{20 - i:02d}") for i, fid in enumerate(workspace_of)
    ]

    async def scope_of(_request, _store, art):
        return subject.files_routes._ArtifactScope("space", workspace_of[art.id], False)

    _patch_scope(monkeypatch, scope_of)
    _gone_is_denied(monkeypatch)

    results = await subject._search_files(_files_request(rows), "usr_viewer", "report", 5)

    assert [item.id for item in results] == ["f_keep"]


@pytest.mark.asyncio
async def test_report_source_never_uses_the_canvas_id_as_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """An untitled report carries an empty title plus ``created_at``; the client builds the list's fallback title."""
    rows = [
        {
            "id": "cv_796810b91267",
            "session_id": "s_untitled",
            "user_id": "usr_viewer",
            "title": None,
            "first_section_title": None,
            "created_at": "2026-10-06T07:38:00Z",
            "updated_at": "2026-10-06T08:00:00Z",
        },
        {
            "id": "cv_35847490c01d",
            "session_id": "s_section",
            "user_id": "usr_viewer",
            "title": "  ",
            "first_section_title": "Revenue by region",
            "created_at": "2026-10-05T07:38:00Z",
            "updated_at": "2026-10-05T08:00:00Z",
        },
    ]

    class CanvasStore:
        async def list_for_user(self, *, limit, session_ids=None, **_kwargs):
            return [] if session_ids is not None else rows[:limit]

    async def no_grants(*_args, **_kwargs):
        return frozenset()

    async def pass_through(_request, _user_id, items, *, enforce_space_membership=True):
        return items

    monkeypatch.setattr(subject.canvas_routes, "_shared_report_session_ids", no_grants)
    monkeypatch.setattr(subject.canvas_routes, "_filter_report_history_items", pass_through)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=CanvasStore())))

    results = await subject._search_reports(request, "usr_viewer", "report", 5)

    assert [(item.id, item.title, item.created_at) for item in results] == [
        ("cv_796810b91267", "", "2026-10-06T07:38:00Z"),
        ("cv_35847490c01d", "Revenue by region", "2026-10-05T07:38:00Z"),
    ]


def test_knowledge_snippet_is_readable_text_not_an_exported_table() -> None:
    # 116 dogfood 1.5.25: a report exported to Excel and uploaded to the knowledge base came back as a raw table.
    chunk = (
        "| 2026年1-9月销售额与环比增长率 | chart | sec_lead_t_8f2c1a_001 | 3 |\n"
        "| --- | --- | --- | --- |\n"
        '| Source references | ["1"] |\n'
        "| Content | **销售额** 9 月反弹至 12.04 亿元 [cite:2]，见 [明细](https://x.example/a) |\n"
        "| 营业额_亿元 | 19.9 |\n"
    )
    snippet = subject._plain_text_snippet(chunk, "销售额")
    assert snippet is not None
    assert "sec_" not in snippet
    assert "|" not in snippet
    assert "---" not in snippet
    assert "Source references" not in snippet
    assert '["1"]' not in snippet
    assert "[cite" not in snippet
    assert "**" not in snippet
    assert "https://" not in snippet
    assert "2026年1-9月销售额与环比增长率" in snippet
    assert "销售额 9 月反弹至 12.04 亿元" in snippet
    assert "明细" in snippet
    # Underscored names and numbers are content.
    assert "营业额_亿元 · 19.9" in snippet


def test_plain_text_keeps_ordinary_prose() -> None:
    assert subject._plain_text("# Q3 review\n\nRevenue grew **12%** in East.") == "Q3 review Revenue grew 12% in East."


@pytest.mark.asyncio
async def test_knowledge_semantic_snippet_quotes_around_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    long_prefix = "| 序号 | 说明 |\n|---|---|\n" + "".join(f"| {i} | 无关内容{i} |\n" for i in range(40))

    async def semantic(*_args, **_kwargs):
        return SimpleNamespace(
            chunks=[
                SimpleNamespace(file_id="kbf_report", file_name="report.xlsx", content=long_prefix + "| 结论 | 复购率回升 |"),
            ],
        )

    monkeypatch.setattr(subject.kb_routes, "search", semantic)
    results = await subject._search_knowledge_semantic(SimpleNamespace(), _user(), "复购率", 5)
    assert results[0].snippet is not None
    assert "复购率回升" in results[0].snippet


@pytest.mark.parametrize(
    "prose",
    [
        "t_order_detail 表按 s_region 分组",
        "3*4=12，2*3=6",
        "见 [2024] 年报",
        "2026. 年度回顾",
        "a<b and c>d",
        "营业额_亿元 与 snake_case_name",
    ],
)
def test_plain_text_leaves_ordinary_prose_alone(prose: str) -> None:
    assert subject._plain_text(prose) == prose


def test_plain_text_strips_only_real_markup() -> None:
    assert subject._plain_text("1. **销售额**回升 [2]，见 `sales` <br> 表 cv_0123456789ab") == "销售额回升 ，见 sales 表"
    assert subject._plain_text("- 要点 *一*\n> 引用 ~~旧~~") == "要点 一 引用 旧"
    # An id-shaped token inside prose that is not the generated hex form stays.
    assert subject._plain_text("字段 sec_title 的含义") == "字段 sec_title 的含义"


def test_knowledge_snippet_centres_on_a_query_term_when_the_whole_query_is_absent() -> None:
    chunk = "无关内容。" * 80 + "华东区复购率在九月回升。" + "其他。" * 40
    snippet = subject._plain_text_snippet(chunk, "复购率 走势")
    assert snippet is not None
    assert "复购率在九月回升" in snippet
