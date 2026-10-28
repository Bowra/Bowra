"""Report library list (``GET /api/v1/canvas``): true totals and keyset pagination.

The fixture uses real JWT authentication (no development bypass) and one user with
two Spaces: ``ws_keep`` (still a member) and ``ws_gone`` (membership removed), plus a
second user in the same Space. Timestamps are pinned with SQL so several reports share
the same ``updated_at`` and section count, which is where an unstable sort would skip
or repeat rows across pages.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import aiosqlite
import jwt
import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import canvas as canvas_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SECRET = "report-list-pagination-secret-0123456789"
_TIE_STAMP = "2026-03-01T00:00:00+00:00"


def _token(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user.id, "iat": now, "exp": now + 3600, "pwd_v": int(getattr(user, "pwd_version", 0) or 0)},
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def list_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")

    users = UserStore(tmp_path / "bowhead.db")
    await users.initialize()
    team = await users.create_team(name="Report Paging", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!", email_verified=True)
    other = await users.create_user(team_id=team.id, email="other@example.com", password="Passw0rd!", email_verified=True)
    ws_keep = await users.create_local_workspace(team_id=team.id, name="Keep", creator_user_id=owner.id)
    ws_gone = await users.create_local_workspace(team_id=team.id, name="Gone", creator_user_id=owner.id)
    await users.add_member(workspace_id=ws_keep, user_id=other.id)

    canvas_db = tmp_path / "canvas.db"
    canvases = CanvasStore(db_path=canvas_db)
    await canvases.initialize()

    async def _seed(user_id: str, workspace_id: str, label: str, *, sections: int, stamp: str, title: str | None) -> str:
        canvas = await canvases.ensure_canvas(
            session_id=f"s_{label}",
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
        )
        for i in range(sections):
            await canvases.append_section(canvas_id=canvas.id, kind="section", payload={"h2": f"{label} part {i}"})
        async with aiosqlite.connect(canvas_db) as db:
            await db.execute("UPDATE canvases SET updated_at=? WHERE id=?", (stamp, canvas.id))
            await db.commit()
        return canvas.id

    keep: list[str] = []
    # Seven reports in ws_keep: four share one timestamp and five share one section count.
    keep.append(await _seed(owner.id, ws_keep, "k0", sections=2, stamp=_TIE_STAMP, title="Gamma"))
    keep.append(await _seed(owner.id, ws_keep, "k1", sections=2, stamp=_TIE_STAMP, title="Alpha"))
    keep.append(await _seed(owner.id, ws_keep, "k2", sections=2, stamp=_TIE_STAMP, title="Alpha"))
    keep.append(await _seed(owner.id, ws_keep, "k3", sections=0, stamp=_TIE_STAMP, title=None))
    keep.append(await _seed(owner.id, ws_keep, "k4", sections=3, stamp="2026-02-01T00:00:00+00:00", title="Beta"))
    keep.append(await _seed(owner.id, ws_keep, "k5", sections=2, stamp="2026-04-01T00:00:00+00:00", title="Delta"))
    keep.append(await _seed(owner.id, ws_keep, "k6", sections=0, stamp="2026-05-01T00:00:00+00:00", title="Epsilon"))
    gone = [await _seed(owner.id, ws_gone, f"g{i}", sections=1, stamp="2026-09-01T00:00:00+00:00", title=f"Alpha gone {i}") for i in range(3)]
    others = [await _seed(other.id, ws_keep, f"o{i}", sections=1, stamp=_TIE_STAMP, title=f"Alpha other {i}") for i in range(2)]

    assert await users.remove_member(ws_gone, owner.id) is True

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(canvas_routes.router, prefix="/api/v1/canvas")
    app.state.user_store = users
    app.state.canvas_store = canvases
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        canvases=canvases,
        owner=owner,
        other=other,
        ws_keep=ws_keep,
        ws_gone=ws_gone,
        keep=keep,
        gone=gone,
        others=others,
    )


def _get(ctx, user, **params: Any):
    return ctx.client.get("/api/v1/canvas", params=params, cookies={"bowhead_jwt": _token(user)})


def _walk(ctx, user, **params: Any) -> tuple[list[str], list[int]]:
    """Follow ``next_cursor`` to the end; return every id in order and the first page's ``total``.

    Cursor pages must report ``total: null`` (only the first page counts).
    """
    ids: list[str] = []
    totals: list[int] = []
    cursor: str | None = None
    for _ in range(50):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        resp = _get(ctx, user, **query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids.extend(item["id"] for item in body["items"])
        if cursor:
            assert body["total"] is None
        else:
            totals.append(body["total"])
        cursor = body["next_cursor"]
        assert body["has_more"] is (cursor is not None)
        if cursor is None:
            return ids, totals
    raise AssertionError("pagination did not terminate")


async def _expected_order(ctx, sort: str, ids: list[str]) -> list[str]:
    rows = {item["id"]: item for item in await ctx.canvases.list_for_user(user_id=ctx.owner.id, limit=500)}
    chosen = [rows[i] for i in ids]
    if sort == "updated_desc":
        chosen.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
    elif sort == "sections_desc":
        chosen.sort(key=lambda r: (r["sections_count"], r["updated_at"], r["id"]), reverse=True)
    else:
        chosen.sort(key=lambda r: (r["title"] or "", r["id"]))
    return [r["id"] for r in chosen]


async def test_total_is_the_true_count_not_the_page_size(list_ctx) -> None:
    resp = _get(list_ctx, list_ctx.owner, limit=2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    # Seven readable reports of the owner; the removed Space and the other user's rows are not counted.
    assert body["total"] == 7
    assert body["next_cursor"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"include_empty": "false"}, {"k0", "k1", "k2", "k4", "k5"}),
        ({"q": "Alpha"}, {"k1", "k2"}),
        ({"q": "Alpha", "include_empty": "false"}, {"k1", "k2"}),
        ({"workspace_id": "__keep__"}, {"k0", "k1", "k2", "k3", "k4", "k5", "k6"}),
    ],
)
async def test_total_matches_the_same_filters_as_the_items(list_ctx, params, expected) -> None:
    params = {k: (list_ctx.ws_keep if v == "__keep__" else v) for k, v in params.items()}
    expected_ids = {list_ctx.keep[int(label[1:])] for label in expected}
    ids, totals = _walk(list_ctx, list_ctx.owner, limit=2, **params)
    assert set(ids) == expected_ids
    assert len(ids) == len(expected_ids)
    assert set(totals) == {len(expected_ids)}


@pytest.mark.parametrize("sort", ["updated_desc", "sections_desc", "title_asc"])
@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_cursor_pages_cover_every_row_once_in_sort_order(list_ctx, sort, limit) -> None:
    ids, totals = _walk(list_ctx, list_ctx.owner, sort=sort, limit=limit)
    assert ids == await _expected_order(list_ctx, sort, list_ctx.keep)
    assert len(ids) == len(set(ids)) == 7
    assert set(totals) == {7}


async def test_first_page_order_matches_the_full_list(list_ctx) -> None:
    """A paged walk and a single large page agree, so old clients see the same order."""
    whole = _get(list_ctx, list_ctx.owner, sort="sections_desc", limit=200).json()
    paged, _ = _walk(list_ctx, list_ctx.owner, sort="sections_desc", limit=2)
    assert [item["id"] for item in whole["items"]] == paged
    assert whole["next_cursor"] is None


async def test_removed_space_is_excluded_from_total_and_pages(list_ctx) -> None:
    ids, totals = _walk(list_ctx, list_ctx.owner, limit=2)
    assert not set(ids) & set(list_ctx.gone)
    assert set(totals) == {7}
    # Asking for the removed Space directly is refused, not answered with a count.
    resp = _get(list_ctx, list_ctx.owner, workspace_id=list_ctx.ws_gone)
    assert resp.status_code in (403, 404), resp.text
    assert "total" not in resp.json()


async def test_a_user_never_sees_another_users_reports_or_counts(list_ctx) -> None:
    ids, totals = _walk(list_ctx, list_ctx.other, limit=1)
    assert set(ids) == set(list_ctx.others)
    assert set(totals) == {2}
    owner_ids, owner_totals = _walk(list_ctx, list_ctx.owner, limit=5, q="Alpha")
    assert not set(owner_ids) & set(list_ctx.others)
    assert set(owner_totals) == {2}


async def test_shared_scope_counts_only_reports_shared_in(list_ctx, monkeypatch) -> None:
    ctx = list_ctx
    # The other user's two reports are shared to the owner, plus the owner's own report
    # (which must not appear in "shared with me").
    shared_sessions = {"s_o0", "s_o1", "s_k0"}

    async def _shared(_request, _user_id):
        return shared_sessions

    monkeypatch.setattr(canvas_routes, "_shared_report_session_ids", _shared)
    ids, totals = _walk(ctx, ctx.owner, scope="shared", limit=1)
    assert set(ids) == set(ctx.others)
    assert set(totals) == {2}
    first = _get(ctx, ctx.owner, scope="shared", limit=1).json()
    assert first["items"][0]["shared_by"]


async def test_first_page_reports_hidden_empty_reports_over_the_same_readable_set(list_ctx) -> None:
    # k3 and k6 have no sections; the removed Space and the other user's reports never count.
    first = _get(list_ctx, list_ctx.owner, include_empty="false", limit=2).json()
    assert first["total"] == 5
    assert first["empty_total"] == 2
    searched = _get(list_ctx, list_ctx.owner, include_empty="false", q="Epsilon").json()
    assert (searched["total"], searched["empty_total"]) == (0, 1)
    # Only computed on a first page that hides empty reports.
    assert _get(list_ctx, list_ctx.owner, include_empty="true").json()["empty_total"] is None
    nxt = _get(list_ctx, list_ctx.owner, include_empty="false", limit=2, cursor=first["next_cursor"]).json()
    assert nxt["total"] is None and nxt["empty_total"] is None
    assert _get(list_ctx, list_ctx.other, include_empty="false").json()["empty_total"] == 0


async def test_requests_without_cursor_keep_the_legacy_shape(list_ctx) -> None:
    body = _get(list_ctx, list_ctx.owner).json()
    assert len(body["items"]) == 7
    assert body["total"] == 7
    assert body["next_cursor"] is None
    clamped = _get(list_ctx, list_ctx.owner, limit=10_000).json()
    assert len(clamped["items"]) == 7
    assert clamped["next_cursor"] is None


async def test_page_size_default_and_clamp(list_ctx) -> None:
    for i in range(60):
        await list_ctx.canvases.ensure_canvas(session_id=f"s_bulk_{i}", workspace_id=list_ctx.ws_keep, user_id=list_ctx.owner.id)
    body = _get(list_ctx, list_ctx.owner).json()
    assert len(body["items"]) == 50
    assert body["total"] == 67
    assert body["next_cursor"]
    rest = _get(list_ctx, list_ctx.owner, cursor=body["next_cursor"]).json()
    assert len(rest["items"]) == 17
    assert rest["next_cursor"] is None
    assert not {i["id"] for i in body["items"]} & {i["id"] for i in rest["items"]}


@pytest.mark.parametrize("cursor", ["not-a-cursor", "e30", "eyJzIjoidXBkYXRlZF9kZXNjIiwidiI6WzEsMl19"])
async def test_malformed_cursor_is_rejected(list_ctx, cursor) -> None:
    resp = _get(list_ctx, list_ctx.owner, cursor=cursor)
    assert resp.status_code == 400, resp.text
    assert "invalid_report_cursor" in resp.text


async def test_cursor_from_another_sort_is_rejected(list_ctx) -> None:
    cursor = _get(list_ctx, list_ctx.owner, sort="updated_desc", limit=1).json()["next_cursor"]
    resp = _get(list_ctx, list_ctx.owner, sort="title_asc", cursor=cursor)
    assert resp.status_code == 400, resp.text


def _forged_cursor(sort: str, values: list[Any], **filters: Any) -> str:
    """A cursor with the correct filter digest, so only the values decide validity."""
    key = canvas_routes._report_list_filter_key(
        scope=filters.get("scope", "mine"),
        workspace_id=filters.get("workspace_id"),
        q=filters.get("q"),
        include_empty=filters.get("include_empty", True),
        sort=sort,
    )
    return canvas_routes._encode_report_list_cursor(sort, key, values)


@pytest.mark.parametrize(
    ("sort", "values"),
    [
        ("sections_desc", [10**30, "a", "b"]),
        ("sections_desc", [-1, "a", "b"]),
        ("sections_desc", [2**31, "a", "b"]),
        ("updated_desc", ["x" * 513, "cv_1"]),
        ("title_asc", ["Alpha", True]),
    ],
)
async def test_out_of_range_cursor_values_are_a_400_not_a_500(list_ctx, sort, values) -> None:
    resp = _get(list_ctx, list_ctx.owner, sort=sort, cursor=_forged_cursor(sort, values))
    assert resp.status_code == 400, resp.text
    assert "invalid_report_cursor" in resp.text


async def test_forged_cursor_with_valid_values_is_accepted(list_ctx) -> None:
    """Positive control for the forgery helper: in-range values page normally."""
    resp = _get(list_ctx, list_ctx.owner, sort="sections_desc", cursor=_forged_cursor("sections_desc", [3, "9999", "zz"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] is None


@pytest.mark.parametrize(
    "changed",
    [{"q": "Beta"}, {"include_empty": "false"}, {"scope": "shared"}, {"workspace_id": "__keep__"}],
)
async def test_cursor_is_bound_to_the_filters_it_was_issued_with(list_ctx, monkeypatch, changed) -> None:
    async def _shared(_request, _user_id):
        return {"s_o0", "s_o1"}

    monkeypatch.setattr(canvas_routes, "_shared_report_session_ids", _shared)
    cursor = _get(list_ctx, list_ctx.owner, limit=1).json()["next_cursor"]
    assert cursor
    params = {k: (list_ctx.ws_keep if v == "__keep__" else v) for k, v in changed.items()}
    resp = _get(list_ctx, list_ctx.owner, limit=1, cursor=cursor, **params)
    assert resp.status_code == 400, resp.text
    assert "invalid_report_cursor" in resp.text


async def test_space_check_errors_surface_as_themselves_not_as_an_exception_group(list_ctx, monkeypatch) -> None:
    """Concurrent Space checks re-raise the original error (an ExceptionGroup maps to no handler)."""
    real = canvas_routes._require_canvas_lifecycle

    async def flaky(request, *, workspace_id, **kwargs):
        if workspace_id == list_ctx.ws_gone:
            raise LookupError("space lookup failed")
        return await real(request, workspace_id=workspace_id, **kwargs)

    monkeypatch.setattr(canvas_routes, "_require_canvas_lifecycle", flaky)
    request = SimpleNamespace(app=list_ctx.client.app)
    with pytest.raises(LookupError, match="space lookup failed"):
        await canvas_routes._readable_report_workspaces(request, list_ctx.owner.id, [list_ctx.ws_keep, list_ctx.ws_gone])


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(503, "authz_lookup_failed"), (401, "user_not_active")],
)
async def test_a_failed_space_check_fails_the_list_with_its_status(list_ctx, monkeypatch, status_code, code) -> None:
    """Only a denial (403/404) hides a Space; a lookup failure must not shrink the list and total silently."""
    from fastapi import HTTPException

    real = canvas_routes._require_canvas_lifecycle

    async def failing(request, *, workspace_id, **kwargs):
        if workspace_id == list_ctx.ws_gone:
            raise HTTPException(status_code, {"error": {"code": code}})
        return await real(request, workspace_id=workspace_id, **kwargs)

    monkeypatch.setattr(canvas_routes, "_require_canvas_lifecycle", failing)
    resp = _get(list_ctx, list_ctx.owner)
    assert resp.status_code == status_code, resp.text
    assert resp.json()["detail"]["error"]["code"] == code


@pytest.mark.parametrize("status_code", [403, 404])
async def test_a_denied_space_is_hidden_and_the_rest_listed(list_ctx, monkeypatch, status_code) -> None:
    from fastapi import HTTPException

    async def denied(request, *, workspace_id, **kwargs):
        if workspace_id == list_ctx.ws_keep:
            raise HTTPException(status_code, {"error": {"code": "denied"}})
        # Stub: treat ws_gone as readable so the surviving Space is visible.

    monkeypatch.setattr(canvas_routes, "_require_canvas_lifecycle", denied)
    resp = _get(list_ctx, list_ctx.owner, limit=50)
    assert resp.status_code == 200, resp.text
    assert sorted(item["id"] for item in resp.json()["items"]) == sorted(list_ctx.gone)
    assert resp.json()["total"] == len(list_ctx.gone)


async def test_report_space_checks_share_one_membership_lookup(list_ctx, monkeypatch) -> None:
    users = list_ctx.client.app.state.user_store
    calls = {"roles": 0, "single": 0}
    real_roles, real_single = users.active_member_roles_for_user, users.get_member_role

    async def roles(user_id):
        calls["roles"] += 1
        return await real_roles(user_id)

    async def single(workspace_id, user_id):
        calls["single"] += 1
        return await real_single(workspace_id, user_id)

    monkeypatch.setattr(users, "active_member_roles_for_user", roles)
    monkeypatch.setattr(users, "get_member_role", single)
    ids, totals = _walk(list_ctx, list_ctx.owner, limit=50)
    # Same result as without the cache (see test_removed_space_is_excluded_from_total_and_pages).
    assert sorted(ids) == sorted(list_ctx.keep)
    assert totals == [7]
    assert calls == {"roles": 1, "single": 0}
