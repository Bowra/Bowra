"""Owner-less artifact denials match a missing id across every space header and caller kind."""

# ruff: noqa: F811 - pytest injects the imported `env` fixture by parameter name.
from __future__ import annotations

from typing import Any

import pytest
from test_file_existence_concealment_blackbox import _HEADERS, MISSING, OUTSIDER, OWNER, PROBER, _ownerless, env  # noqa: F401, F811


def _cmp(env, method, tpl, rid, user, headers, **kw):  # noqa: ANN001, ANN202
    h = {"X-Bowhead-User-Id": user, **headers}
    a = env.client.request(method, tpl.format(id=rid), headers=h, **kw)
    b = env.client.request(method, tpl.format(id=MISSING), headers=h, **kw)
    return a, b


@pytest.mark.parametrize("header", list(_HEADERS))
@pytest.mark.parametrize("user", [PROBER, OWNER, OUTSIDER])
async def test_ownerless_mutations_all_headers(env, header, user) -> None:
    headers = _HEADERS[header]
    live = await _ownerless(env)
    referenced = await _ownerless(env, filename="r.csv", in_kb=True)
    trashed = await _ownerless(env, filename="t.csv", deleted=True)
    cases: list[tuple[str, str, str, dict[str, Any]]] = [
        ("POST", "/api/v1/files/{id}/category", live, {"json": {"category": "dataset"}}),
        ("DELETE", "/api/v1/files/{id}", live, {}),
        ("DELETE", "/api/v1/files/{id}", referenced, {}),
        ("POST", "/api/v1/files/{id}/restore", trashed, {}),
    ]
    bad = []
    for m, tpl, rid, kw in cases:
        a, b = _cmp(env, m, tpl, rid, user, headers, **kw)
        if (a.status_code, dict(a.headers), a.content) != (b.status_code, dict(b.headers), b.content):
            bad.append((m, tpl, a.status_code, a.content[:200], b.status_code, b.content[:200]))
    h = {"X-Bowhead-User-Id": user, **headers}
    for scope in ("personal", "space"):
        a = env.client.post("/api/v1/kb/files/from-artifact", json={"artifact_id": live, "scope": scope}, headers=h)
        b = env.client.post("/api/v1/kb/files/from-artifact", json={"artifact_id": MISSING, "scope": scope}, headers=h)
        if (a.status_code, dict(a.headers), a.content) != (b.status_code, dict(b.headers), b.content):
            bad.append(("promote", scope, a.status_code, a.content[:200], b.status_code, b.content[:200]))
    assert not bad, bad


@pytest.mark.parametrize("header", list(_HEADERS))
async def test_ownerless_reads_outsider_all_headers(env, header) -> None:
    aid = await _ownerless(env)
    bad = []
    for suffix in ("", "/preview", "/usages", "/representations/details", "/shares"):
        a, b = _cmp(env, "GET", "/api/v1/files/{id}" + suffix, aid, OUTSIDER, _HEADERS[header])
        if (a.status_code, dict(a.headers), a.content) != (b.status_code, dict(b.headers), b.content):
            bad.append((suffix, a.status_code, a.content[:200], b.status_code, b.content[:200]))
    assert not bad, bad


@pytest.mark.parametrize("header", list(_HEADERS))
async def test_ownerless_share_mutation_all_headers(env, header) -> None:
    aid = await _ownerless(env)
    bad = []
    for user in (PROBER, OWNER, OUTSIDER):
        a, b = _cmp(env, "POST", "/api/v1/files/{id}/share", aid, user, _HEADERS[header], json={"subject_kind": "workspace", "subject_id": "ws_main"})
        if (a.status_code, a.content) != (b.status_code, b.content):
            bad.append((user, a.status_code, a.content[:200], b.status_code, b.content[:200]))
    assert not bad, bad


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("header", list(_HEADERS))
async def test_ownerless_team_admin(env, header, role, monkeypatch) -> None:
    async def team_role(self, user_id, team_id):  # noqa: ANN001, ANN202
        return role

    from test_file_existence_concealment_blackbox import _MembershipStore

    monkeypatch.setattr(_MembershipStore, "get_team_role", team_role)
    live = await _ownerless(env)
    trashed = await _ownerless(env, filename="t.csv", deleted=True)
    bad = []
    for m, tpl, rid, kw in [
        ("POST", "/api/v1/files/{id}/category", live, {"json": {"category": "dataset"}}),
        ("DELETE", "/api/v1/files/{id}", live, {}),
        ("POST", "/api/v1/files/{id}/restore", trashed, {}),
        ("POST", "/api/v1/files/{id}/share", live, {"json": {"subject_kind": "workspace", "subject_id": "ws_main"}}),
        ("GET", "/api/v1/files/{id}/shares", live, {}),
        ("DELETE", "/api/v1/files/{id}/share/workspace/ws_main", live, {}),
    ]:
        for user in (PROBER, OUTSIDER):
            a, b = _cmp(env, m, tpl, rid, user, _HEADERS[header], **kw)
            if (a.status_code, dict(a.headers), a.content) != (b.status_code, dict(b.headers), b.content):
                bad.append((m, tpl, user, a.status_code, a.content[:160], b.status_code, b.content[:160]))
    assert not bad, bad
