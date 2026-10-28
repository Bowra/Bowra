"""Session list paging, content search and Space scoping (GET /api/v1/sessions + unified search).

116 report: the sidebar only ever showed the newest sessions and older ones could
not be found by search. Before this change the list had no cursor (50 recent + 50
older at most), ``q`` matched only the rewritten title, and search filtered
removed-Space rows after ``LIMIT`` with a 100-row cap. Every case below runs as a
real JWT user against real stores; the user was removed from one Space while
keeping the other, so Space scoping is exercised rather than bypassed.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import jwt
import pytest
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import sessions as sessions_routes
from bowhead_gateway.routes import unified_search as unified_search_routes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_SECRET = "session-list-paging-secret-0123456789"


def _token(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user.id, "iat": now, "exp": now + 3600, "pwd_v": int(getattr(user, "pwd_version", 0) or 0)},
        _SECRET,
        algorithm="HS256",
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")

    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Paging", max_seats=10)
    user = await users.create_user(team_id=team.id, email="pager@example.com", password="Passw0rd!", email_verified=True)
    other = await users.create_user(team_id=team.id, email="other@example.com", password="Passw0rd!", email_verified=True)
    ws_keep = await users.create_local_workspace(team_id=team.id, name="Keep", creator_user_id=user.id)
    ws_gone = await users.create_local_workspace(team_id=team.id, name="Gone", creator_user_id=user.id)
    await users.add_member(workspace_id=ws_keep, user_id=other.id, role="member")

    store = SessionStore(db_path)
    await store.initialize()

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(sessions_routes.router, prefix="/api/v1/sessions")
    app.include_router(unified_search_routes.router)
    app.state.user_store = users
    app.state.session_store = store
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")

    now = datetime.now(UTC)

    async def seed(session_id: str, *, workspace_id: str, last_active: datetime, owner=None, title: str | None = None, origin: str = "normal") -> str:
        owner_id = (owner or user).id
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO sessions(id, workspace_id, user_id, title, started_at, last_active_at, origin) VALUES (?,?,?,?,?,?,?)",
                (session_id, workspace_id, owner_id, title or f"Session {session_id}", _iso(last_active), _iso(last_active), origin),
            )
            await db.commit()
        return session_id

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        users=users,
        store=store,
        db_path=db_path,
        user=user,
        other=other,
        ws_keep=ws_keep,
        ws_gone=ws_gone,
        now=now,
        seed=seed,
    )


def _get(ctx, user=None, **params):
    response = ctx.client.get("/api/v1/sessions", params=params, cookies={"bowhead_jwt": _token(user or ctx.user)})
    return response


def _page_ids(body: dict) -> list[str]:
    return [item["id"] for group in body["groups"] for item in group["items"]]


def _walk(ctx, **params) -> list[dict]:
    """Every page of one listing, following next_cursor; fails on a runaway cursor."""
    pages: list[dict] = []
    cursor = None
    for _ in range(200):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = _get(ctx, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body)
        cursor = body["next_cursor"]
        assert body["has_more"] is (cursor is not None)
        if cursor is None:
            return pages
    raise AssertionError("cursor never ended")


def _merged_groups(pages: list[dict]) -> dict[str, list[str]]:
    """What a client builds: items merged by group id, first occurrence of an id wins."""
    merged: dict[str, list[str]] = {}
    seen: set[str] = set()
    for page in pages:
        for group in page["groups"]:
            bucket = merged.setdefault(group["id"], [])
            for item in group["items"]:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    bucket.append(item["id"])
    return merged


async def _seed_mixed_history(ctx, *, recent: int, older: int) -> tuple[list[str], list[str]]:
    """``recent`` sessions two days old and ``older`` ones a month old, in ties of five."""
    recent_ids, older_ids = [], []
    for i in range(recent):
        moment = ctx.now - timedelta(days=2, minutes=i // 5)  # five sessions share each timestamp
        recent_ids.append(await ctx.seed(f"s_r{i:03d}", workspace_id=ctx.ws_keep, last_active=moment))
    for i in range(older):
        moment = ctx.now - timedelta(days=30, minutes=i // 5)
        older_ids.append(await ctx.seed(f"s_o{i:03d}", workspace_id=ctx.ws_keep, last_active=moment))
    return recent_ids, older_ids


def _stream_order(ids: list[str]) -> list[str]:
    """Newest first (smaller tie bucket), then id descending inside a tie."""
    return sorted(ids, key=lambda sid: (int(sid[3:]) // 5, -int(sid[3:])))


async def test_a_user_with_more_sessions_than_one_page_reaches_every_one(ctx) -> None:
    """(a) A user with >50 sessions: the first page alone caps out; the cursor reaches #51+."""
    recent_ids, older_ids = await _seed_mixed_history(ctx, recent=60, older=130)

    first = _get(ctx).json()
    assert len(_page_ids(first)) == 100, "first page keeps the long-standing 50 recent + 50 older shape"
    assert first["has_more"] is True

    pages = _walk(ctx, limit=20)
    every = [sid for page in pages for sid in _page_ids(page)]
    assert set(every) == {*recent_ids, *older_ids}
    assert len(every) == len(set(every)), "the first page's older window is skipped later, not repeated"
    assert all(len(_page_ids(page)) <= 20 for page in pages[1:])
    assert len(_page_ids(pages[0])) == 40  # 20 recent + 20 older

    merged = _merged_groups(pages)
    # Ties share a timestamp; ids break them, so the merged lists are exactly the stream order.
    assert merged["last7"] == _stream_order(recent_ids)
    assert merged["older"] == _stream_order(older_ids)
    assert {group["id"] for page in pages for group in page["groups"]} == {"today", "yest", "last7", "older"}


async def test_older_only_history_pages_without_gaps(ctx) -> None:
    _, older_ids = await _seed_mixed_history(ctx, recent=3, older=47)
    pages = _walk(ctx, limit=10)
    every = [sid for page in pages for sid in _page_ids(page)]
    assert len(every) == len(set(every)) == 50
    assert len(pages) == 5  # 3 recent + 10 older, then 10, 10, 10, 7


async def test_a_pinned_old_session_is_on_the_first_page(ctx) -> None:
    await _seed_mixed_history(ctx, recent=5, older=60)
    oldest = await ctx.seed("s_pinned_ancient", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=400))
    await ctx.store.pin_session(user_id=ctx.user.id, session_id=oldest)

    first = _get(ctx, limit=10).json()
    pinned = [item for group in first["groups"] for item in group["items"] if item["pinned"]]
    assert [item["id"] for item in pinned] == [oldest]
    assert first["has_more"] is True

    # It is repeated where the stream reaches it; clients dedupe by id.
    pages = _walk(ctx, limit=10)
    assert _merged_groups(pages)["older"].count(oldest) == 1


async def test_archived_view_pages_by_archive_time(ctx) -> None:
    ids = [await ctx.seed(f"s_a{i:02d}", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=i)) for i in range(23)]
    for sid in ids:
        assert await ctx.store.archive_session(user_id=ctx.user.id, session_id=sid) == "ok"
    pages = _walk(ctx, view="archived", limit=10)
    assert [len(_page_ids(page)) for page in pages] == [10, 10, 3]
    assert all([group["id"] for group in page["groups"]] == ["archived"] for page in pages)
    every = [sid for page in pages for sid in _page_ids(page)]
    assert sorted(every) == sorted(ids) and len(set(every)) == 23
    # Counted once, on the first page; cursor pages leave it to the client.
    assert [page["archived_count"] for page in pages] == [23, None, None]


async def test_cursor_is_bound_to_its_filters_and_rejects_forgeries(ctx) -> None:
    await _seed_mixed_history(ctx, recent=15, older=0)
    cursor = _get(ctx, limit=5).json()["next_cursor"]
    assert cursor

    def code(response) -> str:
        assert response.status_code == 400, response.text
        return response.json()["detail"]["error"]["code"]

    assert code(_get(ctx, limit=5, cursor=cursor, q="Session")) == "invalid_session_cursor"
    assert code(_get(ctx, limit=5, cursor=cursor, view="archived")) == "invalid_session_cursor"
    assert code(_get(ctx, limit=5, cursor=cursor, workspace_id=ctx.ws_keep)) == "invalid_session_cursor"
    assert code(_get(ctx, limit=5, cursor="not-base64-json")) == "invalid_session_cursor"
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    for forged in ({**payload, "k": [10**30, "s"]}, {**payload, "k": ["x" * 600, "s"]}, {**payload, "x": [["a", "b"]]}):
        raw = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        assert code(_get(ctx, limit=5, cursor=raw)) == "invalid_session_cursor"
    # Same filters: accepted.
    assert _get(ctx, limit=5, cursor=cursor).status_code == 200


async def test_removed_space_sessions_never_appear_and_never_shorten_a_page(ctx) -> None:
    """Newer sessions in a removed Space used to fill LIMIT and then be filtered out."""
    keep = [await ctx.seed(f"s_k{i:02d}", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=3, hours=i)) for i in range(12)]
    gone = [await ctx.seed(f"s_g{i:02d}", workspace_id=ctx.ws_gone, last_active=ctx.now - timedelta(days=1, minutes=i)) for i in range(30)]
    # Positive control: while still a member, the gone Space is listed.
    assert set(gone) <= set(_page_ids(_get(ctx, limit=50).json()))

    assert await ctx.users.remove_member(ctx.ws_gone, ctx.user.id) is True
    pages = _walk(ctx, limit=5)
    assert [len(_page_ids(page)) for page in pages] == [5, 5, 2]
    assert [sid for page in pages for sid in _page_ids(page)] == keep
    assert _get(ctx, workspace_id=ctx.ws_gone).status_code in {403, 404}


async def test_q_matches_the_users_own_messages_not_only_the_title(ctx) -> None:
    """(b) The title is rewritten after the first turn; the user's words live in turns.user_text."""
    session = await ctx.store.ensure_session(session_id="s_content", workspace_id=ctx.ws_keep, user_id=ctx.user.id)
    await ctx.store.record_turn(turn_id="t_content_1", session_id=session.id, user_text="请复盘黄金周促销的华东门店表现")
    await ctx.store.set_title(session_id=session.id, title="华东门店季度分析")
    await ctx.store.finalize_turn(session_id=session.id, current_turn_id="t_content_1", next_turn_id=None)
    await ctx.store.record_turn(turn_id="t_content_2", session_id=session.id, user_text="再按 cohort_size 拆一下")
    await ctx.seed("s_decoy", workspace_id=ctx.ws_keep, last_active=ctx.now, title="unrelated")

    assert _page_ids(_get(ctx, q="黄金周").json()) == [session.id]
    assert _page_ids(_get(ctx, q="cohort_size").json()) == [session.id], "later user messages count too"
    # LIKE wildcards are literal: "_" must not match any single character.
    assert _page_ids(_get(ctx, q="cohort size").json()) == []
    assert _page_ids(_get(ctx, q="%").json()) == []

    search = ctx.client.post("/api/v1/search", json={"query": "黄金周"}, cookies={"bowhead_jwt": _token(ctx.user)}).json()
    session_group = next(group for group in search["groups"] if group["kind"] == "session")
    assert [item["id"] for item in session_group["items"]] == [session.id]
    assert "黄金周" in session_group["items"][0]["snippet"], "a content hit explains itself with the matching message"


async def test_content_search_keeps_list_visibility_rules(ctx) -> None:
    """Deleted, archived, internal, another user's and removed-Space sessions stay out of q results."""
    text = "needle-phrase in the question"
    specs = {
        "s_visible": (ctx.ws_keep, ctx.user, "normal"),
        "s_deleted": (ctx.ws_keep, ctx.user, "normal"),
        "s_archived": (ctx.ws_keep, ctx.user, "normal"),
        "s_internal": (ctx.ws_keep, ctx.user, "agent_eval"),
        "s_foreign": (ctx.ws_keep, ctx.other, "normal"),
        "s_removed": (ctx.ws_gone, ctx.user, "normal"),
    }
    for sid, (workspace_id, owner, origin) in specs.items():
        await ctx.seed(sid, workspace_id=workspace_id, owner=owner, last_active=ctx.now - timedelta(hours=1), title="plain title", origin=origin)
        async with aiosqlite.connect(ctx.db_path) as db:
            await db.execute(
                "INSERT INTO turns(id, session_id, seq, user_text, started_at, status) VALUES (?,?,?,?,?, 'done')",
                (f"t_{sid}", sid, 1, text, _iso(ctx.now)),
            )
            await db.commit()
    await ctx.store.soft_delete_session("s_deleted")
    assert await ctx.store.archive_session(user_id=ctx.user.id, session_id="s_archived") == "ok"
    assert await ctx.users.remove_member(ctx.ws_gone, ctx.user.id) is True

    assert _page_ids(_get(ctx, q="needle-phrase").json()) == ["s_visible"]
    assert _page_ids(_get(ctx, q="needle-phrase", view="archived").json()) == ["s_archived"]
    search = ctx.client.post("/api/v1/search", json={"query": "needle-phrase"}, cookies={"bowhead_jwt": _token(ctx.user)}).json()
    assert next(group for group in search["groups"] if group["kind"] == "session")["items"][0]["id"] == "s_visible"
    assert len(next(group for group in search["groups"] if group["kind"] == "session")["items"]) == 1


async def test_unified_search_is_not_emptied_by_many_newer_hidden_hits(ctx) -> None:
    """(c) 120 newer matches in a removed Space used to exhaust the 100-row widening cap."""
    keep = [
        await ctx.seed(f"s_k{i}", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=60, minutes=i), title=f"alpha keep {i}")
        for i in range(2)
    ]
    for i in range(120):
        await ctx.seed(f"s_g{i:03d}", workspace_id=ctx.ws_gone, last_active=ctx.now - timedelta(minutes=i), title=f"alpha gone {i}")
    assert await ctx.users.remove_member(ctx.ws_gone, ctx.user.id) is True

    body = ctx.client.post("/api/v1/search", json={"query": "alpha", "limit": 5}, cookies={"bowhead_jwt": _token(ctx.user)}).json()
    group = next(group for group in body["groups"] if group["kind"] == "session")
    assert group["unavailable"] is False and group["timed_out"] is False
    assert [item["id"] for item in group["items"]] == keep


async def _two_space_history(ctx) -> None:
    await ctx.seed("s_keep", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=2))
    await ctx.seed("s_other_space", workspace_id=ctx.ws_gone, last_active=ctx.now - timedelta(days=3))


async def test_a_failed_space_check_keeps_its_status_instead_of_becoming_a_500(ctx, monkeypatch) -> None:
    """Concurrent checks must re-raise the HTTPException itself, not an ExceptionGroup (bare 500)."""
    await _two_space_history(ctx)
    real = sessions_routes._require_workspace_access

    async def lookup_fails(request, *, user_id, workspace_id, operation):
        if workspace_id == ctx.ws_gone:
            raise HTTPException(503, {"error": {"code": "authz_lookup_failed", "workspace_id": workspace_id}})
        return await real(request, user_id=user_id, workspace_id=workspace_id, operation=operation)

    monkeypatch.setattr(sessions_routes, "_require_workspace_access", lookup_fails)
    response = _get(ctx)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"]["code"] == "authz_lookup_failed"


async def test_an_inactive_user_gets_401_through_the_concurrent_checks(ctx, monkeypatch) -> None:
    await _two_space_history(ctx)
    assert _get(ctx).status_code == 200  # positive control

    async def no_team(_user_id):
        return None

    monkeypatch.setattr(ctx.users, "get_user_team_id", no_team)
    response = _get(ctx)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["error"]["code"] == "user_not_active"


async def test_space_checks_share_one_membership_lookup_per_request(ctx, monkeypatch) -> None:
    """Every Space check of one list request reads the caller's memberships once."""
    for i in range(4):
        workspace_id = await ctx.users.create_local_workspace(team_id=ctx.user.team_id, name=f"Extra {i}", creator_user_id=ctx.user.id)
        await ctx.seed(f"s_extra{i}", workspace_id=workspace_id, last_active=ctx.now - timedelta(days=1, minutes=i))
    await _two_space_history(ctx)
    calls = {"roles": 0, "single": 0}
    real_roles, real_single = ctx.users.active_member_roles_for_user, ctx.users.get_member_role

    async def roles(user_id):
        calls["roles"] += 1
        return await real_roles(user_id)

    async def single(workspace_id, user_id):
        calls["single"] += 1
        return await real_single(workspace_id, user_id)

    monkeypatch.setattr(ctx.users, "active_member_roles_for_user", roles)
    monkeypatch.setattr(ctx.users, "get_member_role", single)
    body = _get(ctx).json()
    assert len(_page_ids(body)) == 6
    assert calls == {"roles": 1, "single": 0}


async def test_content_snippet_comes_from_the_earliest_matching_message(ctx) -> None:
    await ctx.seed("s_two", workspace_id=ctx.ws_keep, last_active=ctx.now, title="plain")
    long_second = "second mentions needle " + "x" * 5000
    async with aiosqlite.connect(ctx.db_path) as db:
        for seq, text in ((3, "third mentions needle"), (1, "first has no match"), (2, long_second)):
            await db.execute(
                "INSERT INTO turns(id, session_id, seq, user_text, started_at, status) VALUES (?,?,?,?,?, 'done')",
                (f"t_two_{seq}", "s_two", seq, text, _iso(ctx.now)),
            )
        await db.commit()
    hits = await ctx.store.first_matching_user_texts(session_ids=["s_two", "s_missing"], q="needle", max_chars=100)
    assert hits == {"s_two": long_second[:100]}


def _search(ctx, user=None, **body):
    return ctx.client.post("/api/v1/search", json=body, cookies={"bowhead_jwt": _token(user or ctx.user)})


async def test_unified_search_session_group_counts_and_pages_only_readable_matches(ctx) -> None:
    """Sidebar search says how many sessions match and pages the rest (Header, Language & Paging spec 4b)."""
    keep = [
        await ctx.seed(f"s_k{i:02d}", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=40, minutes=i), title=f"repurchase {i}")
        for i in range(12)
    ]
    for i in range(30):
        await ctx.seed(f"s_h{i:02d}", workspace_id=ctx.ws_gone, last_active=ctx.now - timedelta(minutes=i), title=f"repurchase hidden {i}")
    await ctx.seed("s_other_user", workspace_id=ctx.ws_keep, owner=ctx.other, last_active=ctx.now, title="repurchase foreign")
    await ctx.seed("s_unrelated", workspace_id=ctx.ws_keep, last_active=ctx.now, title="unrelated")
    assert await ctx.users.remove_member(ctx.ws_gone, ctx.user.id) is True

    first = _search(ctx, query="repurchase", limit=5)
    assert first.status_code == 200, first.text
    group = next(group for group in first.json()["groups"] if group["kind"] == "session")
    # The total counts the same rows the pages walk: no removed Space, no other user's sessions.
    assert group["total"] == 12
    assert [item["id"] for item in group["items"]] == keep[:5]
    assert {item["match"] for item in group["items"]} == {"title"}
    # Other groups carry no total (they cannot be counted without their per-row checks).
    assert all(other["total"] is None and other["next_cursor"] is None for other in first.json()["groups"][1:])

    seen = [item["id"] for item in group["items"]]
    cursor = group["next_cursor"]
    while cursor:
        page = _search(ctx, query="repurchase", limit=20, kinds=["session"], cursor=cursor)
        assert page.status_code == 200, page.text
        body = page.json()
        assert [g["kind"] for g in body["groups"]] == ["session"], "a cursor page searches sessions only"
        session_page = body["groups"][0]
        # Counted once, on the first page; clients keep that value.
        assert session_page["total"] is None
        seen.extend(item["id"] for item in session_page["items"])
        cursor = session_page["next_cursor"]
    assert seen == keep


async def test_unified_search_rejects_cursors_from_other_queries_or_sources(ctx) -> None:
    for i in range(3):
        await ctx.seed(f"s_a{i}", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=30, minutes=i), title=f"alpha beta {i}")
    first = next(group for group in _search(ctx, query="alpha", limit=1).json()["groups"] if group["kind"] == "session")
    cursor = first["next_cursor"]
    assert cursor

    # Bound to its query: reusing it for another query is a 400, never a silently restarted list.
    assert _search(ctx, query="beta", limit=1, kinds=["session"], cursor=cursor).status_code == 400
    # A sidebar-list cursor is not a search cursor.
    list_cursor = _get(ctx, q="alpha", limit=1).json()["next_cursor"]
    assert list_cursor
    assert _search(ctx, query="alpha", kinds=["session"], cursor=list_cursor).status_code == 400
    # A cursor continues sessions only.
    assert _search(ctx, query="alpha", cursor=cursor).status_code == 422
    assert _search(ctx, query="alpha", kinds=["session", "report"], cursor=cursor).status_code == 422
    assert _search(ctx, query="alpha", kinds=["session"], cursor="not-a-cursor").status_code == 400


async def test_unified_search_marks_content_matches(ctx) -> None:
    session = await ctx.store.ensure_session(session_id="s_content", workspace_id=ctx.ws_keep, user_id=ctx.user.id)
    await ctx.store.record_turn(turn_id="t_c1", session_id=session.id, user_text="看一下老客复购周期")
    await ctx.store.set_title(session_id=session.id, title="2026年6月会员分析")
    await ctx.seed("s_title", workspace_id=ctx.ws_keep, last_active=ctx.now - timedelta(days=30), title="会员复购率下滑原因")

    group = next(group for group in _search(ctx, query="复购").json()["groups"] if group["kind"] == "session")
    by_id = {item["id"]: item for item in group["items"]}
    assert group["total"] == 2
    assert by_id["s_content"]["match"] == "content"
    assert "复购" in by_id["s_content"]["snippet"]
    assert by_id["s_title"]["match"] == "title"
    assert by_id["s_title"]["snippet"] is None


# ─── Viewer time zone: groups follow the viewer's calendar days ─────────────

# 2026-10-08 03:00 UTC = Oct 7 20:00 in Los Angeles, Oct 8 05:00 in Berlin, Oct 8 11:00 in Shanghai.
_FIXED_NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return _FIXED_NOW if tz is None else _FIXED_NOW.astimezone(tz)


def _get_in_zone(ctx, zone: str | None, *, via: str = "header", **params):
    headers = {"X-Bowhead-Time-Zone": zone} if zone is not None and via == "header" else {}
    if zone is not None and via == "query":
        params["tz"] = zone
    return ctx.client.get("/api/v1/sessions", params=params, headers=headers, cookies={"bowhead_jwt": _token(ctx.user)})


def _group_of(body: dict) -> dict[str, str]:
    return {item["id"]: group["id"] for group in body["groups"] for item in group["items"]}


def _time_of(body: dict) -> dict[str, str]:
    return {item["id"]: item["time"] for group in body["groups"] for item in group["items"]}


async def _seed_zone_boundaries(ctx, monkeypatch) -> None:
    monkeypatch.setattr(sessions_routes, "datetime", _FrozenDatetime)
    # Six hours ago: Oct 7 in UTC and Berlin's previous day, but still "today" in Los Angeles and Shanghai.
    await ctx.seed("s_six_hours", workspace_id=ctx.ws_keep, last_active=datetime(2026, 10, 7, 21, 0, tzinfo=UTC))
    # Before the UTC and Berlin week starts (Oct 1 00:00 local) but inside the last seven local days in
    # Los Angeles and Shanghai.
    await ctx.seed("s_week_edge", workspace_id=ctx.ws_keep, last_active=datetime(2026, 9, 30, 20, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("zone", "six_hours", "week_edge", "edge_date"),
    [
        (None, "yest", "older", "2026-09-30"),
        ("UTC", "yest", "older", "2026-09-30"),
        ("America/Los_Angeles", "today", "last7", "2026-09-30"),
        ("Europe/Berlin", "yest", "older", "2026-09-30"),
        ("Asia/Shanghai", "today", "last7", "2026-10-01"),
        # Fractional offsets: +5:30, and +13:45 (Chatham summer time; +12:45 standard).
        ("Asia/Kolkata", "today", "last7", "2026-10-01"),
        ("Pacific/Chatham", "today", "last7", "2026-10-01"),
    ],
)
async def test_groups_use_the_viewers_calendar_days(ctx, monkeypatch, zone, six_hours, week_edge, edge_date) -> None:
    """116 dogfood: a session from six hours ago sat under Yesterday because days were cut in UTC."""
    await _seed_zone_boundaries(ctx, monkeypatch)
    body = _get_in_zone(ctx, zone).json()
    assert _group_of(body) == {"s_six_hours": six_hours, "s_week_edge": week_edge}
    times = _time_of(body)
    assert times["s_six_hours"] == "6h ago", "elapsed time does not depend on the zone"
    assert times["s_week_edge"] == edge_date, "an absolute date is the viewer's calendar date"


async def test_items_carry_an_iso_last_activity_on_first_cursor_and_archived_pages(ctx, monkeypatch) -> None:
    """116 dogfood: the sidebar said "1d ago" where other lists said "Yesterday" because rows had no timestamp.

    Every row carries its last activity as a UTC instant whatever the viewer zone, so the client can apply the
    shared calendar-day list rule; the legacy ``time`` string stays for older clients.
    """
    monkeypatch.setattr(sessions_routes, "datetime", _FrozenDatetime)
    for i in range(5):
        await ctx.seed(f"s_iso{i}", workspace_id=ctx.ws_keep, last_active=datetime(2026, 10, 7, 21, 0, i, tzinfo=UTC))
    first = _get_in_zone(ctx, "Asia/Shanghai", limit=2).json()
    second = _get_in_zone(ctx, "Asia/Shanghai", limit=2, cursor=first["next_cursor"]).json()
    rows = {item["id"]: item for page in (first, second) for group in page["groups"] for item in group["items"]}
    assert len(rows) == 4
    for sid, item in rows.items():
        offset = int(sid.removeprefix("s_iso"))
        assert item["updated_at"] == f"2026-10-07T21:00:{offset:02d}+00:00"
        assert item["time"] in {"5h ago", "6h ago"}, "the legacy relative string stays for older clients"
    await ctx.store.archive_session(session_id="s_iso0", user_id=ctx.user.id)
    archived = _get_in_zone(ctx, "Asia/Shanghai", view="archived").json()
    assert [item["updated_at"] for group in archived["groups"] for item in group["items"]] == ["2026-10-07T21:00:00+00:00"]


async def test_tz_query_parameter_is_accepted(ctx, monkeypatch) -> None:
    await _seed_zone_boundaries(ctx, monkeypatch)
    body = _get_in_zone(ctx, "America/Los_Angeles", via="query").json()
    assert _group_of(body)["s_six_hours"] == "today"


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "../../etc/passwd", "/etc/localtime", "", "x" * 200, "UTC+8", "America", "Etc", "posixrules"])
async def test_an_unknown_or_malformed_zone_falls_back_to_utc(ctx, monkeypatch, zone) -> None:
    await _seed_zone_boundaries(ctx, monkeypatch)
    response = _get_in_zone(ctx, zone)
    assert response.status_code == 200, response.text
    assert _group_of(response.json()) == {"s_six_hours": "yest", "s_week_edge": "older"}


async def test_cursor_paging_in_a_viewer_zone_reaches_every_session_once(ctx, monkeypatch) -> None:
    """The first page splits its windows at the viewer's week start; cursor pages continue without gaps."""
    monkeypatch.setattr(sessions_routes, "datetime", _FrozenDatetime)
    ids = []
    for i in range(45):
        # Hourly steps across the gap between the Los Angeles and UTC week starts (Sep 30 07:00Z, Oct 1 00:00Z).
        moment = datetime(2026, 10, 1, 6, 0, tzinfo=UTC) - timedelta(hours=i)
        ids.append(await ctx.seed(f"s_z{i:03d}", workspace_id=ctx.ws_keep, last_active=moment))
    for zone in ("America/Los_Angeles", "Asia/Shanghai", "UTC"):
        pages: list[dict] = []
        cursor = None
        for _ in range(50):
            params = {"limit": 7, **({"cursor": cursor} if cursor else {})}
            body = _get_in_zone(ctx, zone, **params).json()
            pages.append(body)
            cursor = body["next_cursor"]
            if cursor is None:
                break
        every = [sid for page in pages for sid in _page_ids(page)]
        assert sorted(every) == sorted(ids), zone
        assert len(every) == len(set(every)), zone
        assert len(pages) > 2, "the walk must cross both first-page windows"
