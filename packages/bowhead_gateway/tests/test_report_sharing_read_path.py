"""S5 — report 只读闭包路由测试 (content-permissions, tech-design §4.3, PRD §9.9).

by-session 读通路(owner∨grant + 占位契约)、写路由 owner 锁死不受 grant 影响、
artifact 中介(闭包白名单/无 302/越权 404)、shared-with-me 列表、删除级联。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.results_read_model import (
    RESULTS_SORT_VERSION_V1,
    results_query_filter_hash,
    results_scope_fingerprint,
)
from bowhead_core.share_service import add_grant, set_audience
from bowhead_core.user_store import UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.results_contracts import ResultsFiltersV1
from bowhead_gateway.results_cursor import ResultsCursorBinding, ResultsCursorCodec
from bowhead_gateway.routes import canvas as canvas_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BASE = "/api/v1/canvas"


@pytest.fixture(autouse=True)
def _dev_auth(monkeypatch):
    """canvas 路由经 resolve_user_id_sync 取身份 — 测试走显式 dev fallback +
    X-Bowhead-User-Id header(auth.py:299 的双开关 gate)。"""
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")


def _run(coro):
    return asyncio.run(coro)


class _Env:
    def __init__(self, tmp_path):
        db = str(tmp_path / "s.db")
        self.users = UserStore(db)
        _run(self.users.initialize())
        self.access = AccessControlStore(db)
        _run(self.access.initialize())
        self.canvas = CanvasStore(db_path=str(tmp_path / "c.db"))
        _run(self.canvas.initialize())
        self.artifacts = ArtifactStore(
            db_path=str(tmp_path / "a.db"),
            backend=LocalBackend(root_dir=tmp_path / "blobs"),
        )
        _run(self.artifacts.initialize())
        self.team = _run(self.users.create_team(name="Acme", max_seats=10))
        self.alice = _run(self.users.create_user(team_id=self.team.id, email="a@x.com", password="Passw0rd!"))
        self.bob = _run(self.users.create_user(team_id=self.team.id, email="b@x.com", password="Passw0rd!"))
        self.ws_id = _run(self.users.sync_workspace_from_datarc(team_id=self.team.id, datarc_workspace_id="336", name="WS"))
        # Synced spaces lack member rows; production owners get them on initial join or member-add. Owners need membership to read/manage own reports; bob stays a nonmember recipient.
        _run(self.users.add_member(workspace_id=self.ws_id, user_id=self.alice.id, role="admin"))

    def client(self, user) -> TestClient:
        class _EmptySessionStore:
            async def list_turns(self, *, session_id: str):  # type: ignore[no-untyped-def]
                return []

        class _EmptyTurnLogStore:
            async def list_events(self, _turn_id: str):  # type: ignore[no-untyped-def]
                return []

        app = FastAPI()
        register_error_handlers(app)
        app.include_router(canvas_routes.router, prefix=_BASE)
        app.state.canvas_store = self.canvas
        app.state.user_store = self.users
        app.state.access_control_store = self.access
        app.state.artifact_store = self.artifacts
        app.state.session_store = _EmptySessionStore()
        app.state.turn_log_store = _EmptyTurnLogStore()
        app.dependency_overrides[canvas_routes._current_user] = lambda: user
        return TestClient(app, headers={"X-Bowhead-User-Id": user.id})

    def seed_report(self, *, session_id="ses_1", owner=None):
        owner = owner or self.alice
        return _run(self.canvas.ensure_canvas(session_id=session_id, workspace_id=self.ws_id, user_id=owner.id))

    def share_report(self, session_id, to_user):
        return _run(
            add_grant(
                self.access,
                kind="report",
                resource_id=session_id,
                team_id=self.team.id,
                principal_type="user",
                principal_id=to_user.id,
                actor_id=self.alice.id,
            )
        )


def test_by_session_owner_grant_and_placeholder_contract(tmp_path):
    env = _Env(tmp_path)
    cv = env.seed_report()
    a, b = env.client(env.alice), env.client(env.bob)

    assert a.get(f"{_BASE}/by-session/ses_1").status_code == 200  # Owner short-circuit.
    r = b.get(f"{_BASE}/by-session/ses_1")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "report_unavailable"

    g = env.share_report("ses_1", env.bob)
    r = b.get(f"{_BASE}/by-session/ses_1")
    assert r.status_code == 200 and r.json()["canvas"]["id"] == cv.id

    # Revoke → next request returns placeholder contract.
    _run(env.access.revoke_resource_grant(env.team.id, g.id))
    r = b.get(f"{_BASE}/by-session/ses_1")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "report_unavailable"


@pytest.mark.parametrize("mode", ["preview", "raw", "details"])
def test_representations_require_report_grant_and_closure(tmp_path, mode):
    env = _Env(tmp_path)
    cv = env.seed_report()
    art = _run(env.artifacts.put_bytes(content=b"a,b\n1,2\n", filename="rows.csv", mime="text/csv", source="upload", created_by=env.alice.id))
    outside = _run(env.artifacts.put_bytes(content=b"secret", filename="secret.txt", mime="text/plain", source="upload", created_by=env.alice.id))
    _run(env.canvas.append_section(canvas_id=cv.id, kind="artifact", payload={"artifact_id": art.id}))
    client = env.client(env.bob)
    path = f"{_BASE}/by-session/ses_1/artifacts/{art.id}/representations/{mode}"
    denied = client.get(path)
    assert denied.status_code == 404
    grant = env.share_report("ses_1", env.bob)
    result = client.get(path)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["representation"] == mode
    missing = client.get(path.replace(art.id, "not-present"))
    excluded = client.get(path.replace(art.id, outside.id))
    _run(env.access.revoke_resource_grant(env.team.id, grant.id))
    revoked = client.get(path)
    for response in (missing, excluded, revoked):
        assert response.status_code == 404
        assert response.json() == denied.json()


def test_write_routes_stay_owner_only_despite_grant(tmp_path):
    env = _Env(tmp_path)
    cv = env.seed_report()
    env.share_report("ses_1", env.bob)
    b = env.client(env.bob)
    # Write chain (_load_owned) unaffected by read grants: PRD §9.9.
    assert b.post(f"{_BASE}/{cv.id}/title", json={"title": "hax"}).status_code == 403
    assert b.delete(f"{_BASE}/{cv.id}").status_code == 403
    assert b.get(f"{_BASE}/{cv.id}/revisions").status_code == 403  # History outside closure.


@pytest.mark.integration
@pytest.mark.parametrize("current", [False, True])
def test_report_results_export_trust_uses_viewer_and_only_authorized_page(tmp_path, monkeypatch, current):
    import json

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.canvas_export import init_export_jobs
    from bowhead_core.export_copy_contract import CURRENT_MANIFEST_PRODUCER, TRUST_COPY_CONTRACT, output_producer_for
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    env = _Env(tmp_path)
    canvas = env.seed_report()
    manifest = (
        _run(
            env.artifacts.put_bytes(
                content=json.dumps({"trust_copy_contract": TRUST_COPY_CONTRACT, "job_id": "exp_shared", "format": "pdf"}).encode(),
                filename="report.manifest.json",
                mime="application/json",
                source="canvas_export",
                created_by=env.alice.id,
                producer_kind=CURRENT_MANIFEST_PRODUCER,
            )
        )
        if current
        else None
    )
    inside = _run(
        env.artifacts.put_bytes(
            content=b"export",
            filename="report.pdf",
            mime="application/pdf",
            source="canvas_export",
            created_by=env.alice.id,
            canvas_id=canvas.id,
            producer_kind=output_producer_for(engine="durable", fmt="pdf") if current else None,
            manifest_artifact_id=manifest.id if manifest else None,
        )
    )
    if current:

        async def seed_job():
            await init_export_jobs(env.artifacts.db_path)
            async with open_shared_ctx(env.artifacts.db_path) as db:
                await db.execute(
                    "INSERT INTO canvas_export_jobs "
                    "(id,canvas_id,fmt,status,contract_version,artifact_id,manifest_artifact_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("exp_shared", canvas.id, "pdf", "done", 2, inside.id, manifest.id, inside.created_at, inside.created_at),
                )
                await db.commit()

        _run(seed_job())
    outside = _run(
        env.artifacts.put_bytes(
            content=b"private",
            filename="private.pdf",
            mime="application/pdf",
            source="canvas_export",
            created_by=env.alice.id,
            canvas_id=canvas.id,
        )
    )
    _run(env.canvas.append_section(canvas_id=canvas.id, kind="artifact", payload={"artifact_id": inside.id}))
    grant = env.share_report("ses_1", env.bob)
    batches = []
    original = env.artifacts.get_many

    async def many(ids, **kwargs):
        batches.append(list(ids))
        return await original(ids, **kwargs)

    monkeypatch.setattr(env.artifacts, "get_many", many)
    route = f"{_BASE}/by-session/ses_1/results/query"
    for user, expected in ((env.alice, "export_copy_legacy_regenerable"), (env.bob, "export_copy_legacy_no_permission")):
        response = env.client(user).post(route, json={"surface": "explorer"})
        assert response.status_code == 200, response.text
        item = next(x for x in response.json()["items"] if x["key"] == f"artifact:{inside.id}")
        assert item["artifact_metadata"]["trust"] == ("export_copy_current" if current else expected)
        assert all(x["key"] != f"artifact:{outside.id}" for x in response.json()["items"])
    assert all(outside.id not in batch for batch in batches)
    assert sum(batch == [inside.id] for batch in batches) == 2
    _run(env.access.revoke_resource_grant(env.team.id, grant.id))
    batches.clear()
    response = env.client(env.bob).post(route, json={"surface": "explorer"})
    assert response.status_code == 404
    assert batches == []


def test_artifact_mediation_closure_and_no_redirect(tmp_path):
    env = _Env(tmp_path)
    cv = env.seed_report()
    # Persist two artifacts: one referenced by an image section, one unrelated.
    in_art = _run(
        env.artifacts.put_bytes(content=b"PNGDATA", filename="chart.png", mime="image/png", source="canvas_export", created_by=env.alice.id)
    )
    out_art = _run(
        env.artifacts.put_bytes(
            content=b"SECRET", filename="other.bin", mime="application/octet-stream", source="canvas_export", created_by=env.alice.id
        )
    )
    # editable-design poster: background artifact nested in payload["background"]["artifact_id"].
    bg_art = _run(env.artifacts.put_bytes(content=b"BGPNG", filename="bg.png", mime="image/png", source="canvas_export", created_by=env.alice.id))
    csv_art = _run(
        env.artifacts.put_bytes(
            content=b"month,traffic\nJan,100\nmonth,traffic\nFeb,120\n",
            filename="traffic.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=env.alice.id,
        )
    )
    code_in_art = _run(
        env.artifacts.put_bytes(content=b"raw,input\nA,1\n", filename="source.csv", mime="text/csv", source="upload", created_by=env.alice.id)
    )
    code_out_art = _run(
        env.artifacts.put_bytes(
            content=b"metric,value\nRevenue,42\n", filename="summary.csv", mime="text/csv", source="python_sandbox", created_by=env.alice.id
        )
    )
    video_bytes = b"\x00\x00\x00\x18ftypisom" + bytes(range(64))
    video_art = _run(
        env.artifacts.put_bytes(
            content=video_bytes,
            filename="result.mp4",
            mime="video/mp4",
            source="python_sandbox",
            created_by=env.alice.id,
        )
    )
    export_manifest = _run(
        env.artifacts.put_bytes(
            content=b"{}",
            filename="opaque.json",
            mime="application/json",
            source="canvas_export",
            kind="deliverable",
            producer_kind="canvas_export_manifest_b1a_v1",
            created_by=env.alice.id,
        )
    )
    export_output = _run(
        env.artifacts.put_bytes(
            content=b"PDF",
            filename="report.pdf",
            mime="application/pdf",
            source="canvas_export",
            kind="deliverable",
            producer_kind="canvas_export_b1a_v1_pdf_v2",
            manifest_artifact_id=export_manifest.id,
            created_by=env.alice.id,
        )
    )
    untrusted_relation = _run(
        env.artifacts.put_bytes(
            content=b"not an export",
            filename="uploaded.pdf",
            mime="application/pdf",
            source="upload",
            kind="user_upload",
            producer_kind="upload",
            manifest_artifact_id=in_art.id,
            created_by=env.alice.id,
        )
    )
    _run(env.canvas.append_section(canvas_id=cv.id, kind="image", payload={"artifact_id": in_art.id, "caption": "c"}))
    _run(env.canvas.append_section(canvas_id=cv.id, kind="design", payload={"background": {"artifact_id": bg_art.id}, "blocks": []}))
    _run(env.canvas.append_section(canvas_id=cv.id, kind="data_table", payload={"data_artifact_id": csv_art.id}))
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="artifact",
            payload={"artifact_id": video_art.id},
        )
    )
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="artifact",
            payload={"artifact_id": untrusted_relation.id},
        )
    )
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="artifact",
            payload={"artifact_ids": [export_manifest.id, export_output.id]},
        )
    )
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="code_block",
            payload={
                "lang": "python",
                "code": "print('ok')",
                "input_artifact_ids": [code_in_art.id],
                "output_artifact_ids": [code_out_art.id],
                "input_artifacts": [
                    {
                        "artifact_id": code_in_art.id,
                        "filename": "source.csv",
                        "mime": "text/csv",
                    }
                ],
                "output_artifacts": [
                    {
                        "artifact_id": code_out_art.id,
                        "filename": "summary.csv",
                        "mime": "text/csv",
                    }
                ],
            },
        )
    )
    grant = env.share_report("ses_1", env.bob)
    b = env.client(env.bob)

    # Inside closure: fully proxied 200 (direct bytes, no 302), nosniff defense retained.
    r = b.get(f"{_BASE}/by-session/ses_1/artifacts/{in_art.id}?disposition=inline", follow_redirects=False)
    assert r.status_code == 200 and r.content == b"PNGDATA"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    # Nested design-section background artifact also inside closure (#2 fix): 200, recipient can view poster background.
    assert b.get(f"{_BASE}/by-session/ses_1/artifacts/{bg_art.id}", follow_redirects=False).status_code == 200
    prev = b.get(f"{_BASE}/by-session/ses_1/artifacts/{csv_art.id}/preview?mode=rows")
    assert prev.status_code == 200
    assert prev.json()["rows"] == [
        {"month": "Jan", "traffic": "100"},
        {"month": "Feb", "traffic": "120"},
    ]
    assert (
        b.get(
            f"{_BASE}/by-session/ses_1/artifacts/{code_in_art.id}/preview?mode=rows",
        ).status_code
        == 200
    )
    out = b.get(f"{_BASE}/by-session/ses_1/artifacts/{code_out_art.id}")
    assert out.status_code == 200 and out.content == b"metric,value\nRevenue,42\n"
    video = b.get(
        f"{_BASE}/by-session/ses_1/artifacts/{video_art.id}?disposition=inline",
        headers={"Range": "bytes=4-11"},
        follow_redirects=False,
    )
    assert video.status_code == 206
    assert video.content == video_bytes[4:12]
    assert video.headers["content-disposition"] == "inline"
    assert video.headers["content-range"] == f"bytes 4-11/{len(video_bytes)}"
    assert video.headers["accept-ranges"] == "bytes"
    assert video.headers["content-type"].startswith("video/mp4")
    assert "location" not in video.headers
    # Results lists hide the internal validation artifact, but the persisted
    # report closure and exact owner-authorized read path remain intact.
    assert b.get(f"{_BASE}/by-session/ses_1/artifacts/{export_manifest.id}").status_code == 200
    suffix = b.get(
        f"{_BASE}/by-session/ses_1/artifacts/{video_art.id}?disposition=inline",
        headers={"Range": "bytes=-5"},
    )
    assert suffix.status_code == 206
    assert suffix.content == video_bytes[-5:]
    assert suffix.headers["content-range"] == (f"bytes {len(video_bytes) - 5}-{len(video_bytes) - 1}/{len(video_bytes)}")
    open_ended = b.get(
        f"{_BASE}/by-session/ses_1/artifacts/{video_art.id}?disposition=inline",
        headers={"Range": "bytes=12-"},
    )
    assert open_ended.status_code == 206
    assert open_ended.content == video_bytes[12:]
    invalid = b.get(
        f"{_BASE}/by-session/ses_1/artifacts/{video_art.id}?disposition=inline",
        headers={"Range": "bytes=999-"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(video_bytes)}"
    listed = b.get(f"{_BASE}/by-session/ses_1/artifacts").json()["items"]
    assert [item["id"] for item in listed] == [
        in_art.id,
        bg_art.id,
        csv_art.id,
        video_art.id,
        untrusted_relation.id,
        export_output.id,
        code_in_art.id,
        code_out_art.id,
    ]
    assert export_manifest.id not in {item["id"] for item in listed}
    export_item = next(item for item in listed if item["id"] == export_output.id)
    assert export_item["producer_kind"] == "canvas_export_b1a_v1_pdf_v2"
    assert export_item["manifest_artifact_id"] is None
    assert export_item["derived_from"] is None
    assert next(item for item in listed if item["id"] == video_art.id)["preview_available"] is True
    filtered = b.get(f"{_BASE}/by-session/ses_1/artifacts?q=summary").json()["items"]
    assert [item["id"] for item in filtered] == [code_out_art.id]
    # Search/count/group inputs are applied only after closure projection. A
    # matching closure-external filename must look exactly like no match.
    assert b.get(f"{_BASE}/by-session/ses_1/artifacts?q=other.bin").json()["items"] == []
    _run(env.artifacts.soft_delete(csv_art.id, user_id=env.alice.id))
    prev_deleted = b.get(f"{_BASE}/by-session/ses_1/artifacts/{csv_art.id}/preview?mode=rows")
    assert prev_deleted.status_code == 404
    listed_after_delete = b.get(f"{_BASE}/by-session/ses_1/artifacts").json()["items"]
    assert csv_art.id not in {item["id"] for item in listed_after_delete}
    # Outside closure: 404 (no existence leak); PRD §9.9: B cannot directly access unreferenced files via artifact_id.
    outside = b.get(f"{_BASE}/by-session/ses_1/artifacts/{out_art.id}")
    random = b.get(f"{_BASE}/by-session/ses_1/artifacts/art_random_missing")
    deleted = b.get(f"{_BASE}/by-session/ses_1/artifacts/{csv_art.id}")
    assert outside.status_code == random.status_code == deleted.status_code == 404
    assert outside.json() == random.json() == deleted.json()
    assert (
        b.get(
            f"{_BASE}/by-session/ses_1/artifacts/{out_art.id}/preview?mode=text",
        ).status_code
        == 404
    )
    # C without grant: 403 placeholder.
    carol = _run(env.users.create_user(team_id=env.team.id, email="c@x.com", password="Passw0rd!"))
    assert env.client(carol).get(f"{_BASE}/by-session/ses_1/artifacts/{in_art.id}").status_code == 403
    # Revocation invalidates every subsequent projection/read/download. No
    # previously opened viewer URL or Range request becomes a bearer grant.
    _run(env.access.revoke_resource_grant(env.team.id, grant.id))
    assert b.get(f"{_BASE}/by-session/ses_1/artifacts").status_code == 403
    assert b.get(f"{_BASE}/by-session/ses_1/artifacts/{in_art.id}").status_code == 403
    assert (
        b.get(
            f"{_BASE}/by-session/ses_1/artifacts/{in_art.id}/preview?mode=auto",
        ).status_code
        == 403
    )
    revoked_range = b.get(
        f"{_BASE}/by-session/ses_1/artifacts/{video_art.id}?disposition=inline",
        headers={"Range": "bytes=0-3"},
        follow_redirects=False,
    )
    assert revoked_range.status_code == 403
    assert "location" not in revoked_range.headers


def test_report_results_query_is_complete_closure_only_and_non_enumerable(
    tmp_path,
    monkeypatch,
):
    from cryptography.fernet import Fernet

    root_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", root_key)
    env = _Env(tmp_path)
    cv = env.seed_report()
    inside = _run(
        env.artifacts.put_bytes(
            content=b"inside",
            filename="inside.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=env.alice.id,
        ),
    )
    code_output = _run(
        env.artifacts.put_bytes(
            content=b"code",
            filename="code-output.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=env.alice.id,
        ),
    )
    outside = _run(
        env.artifacts.put_bytes(
            content=b"secret",
            filename="exact-secret.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=env.alice.id,
        ),
    )
    internal = _run(
        env.artifacts.put_bytes(
            content=b"{}",
            filename="manifest.json",
            mime="application/json",
            source="canvas_export",
            kind="deliverable",
            producer_kind="canvas_export_manifest_b1a_v1",
            created_by=env.alice.id,
        ),
    )
    chart_section = _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="chart",
            payload={"spec": {"data_artifact_id": inside.id}},
        ),
    )
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="code_block",
            payload={"output_artifact_ids": [code_output.id]},
        ),
    )
    _run(
        env.canvas.append_section(
            canvas_id=cv.id,
            kind="artifact",
            payload={"artifact_id": internal.id},
        ),
    )
    grant = env.share_report("ses_1", env.bob)
    client = env.client(env.bob)
    route = f"{_BASE}/by-session/ses_1/results/query"

    invalid = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "recent_keys": ["x" * 257],
        },
    )
    assert invalid.status_code == 400
    assert invalid.json() == {
        "detail": {"error": {"code": "results_query_invalid"}},
    }
    assert invalid.headers["cache-control"] == "private, no-store"

    first = client.post(
        route,
        json={"dto_version": 1, "surface": "explorer"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["authorized_total"] == 3
    assert {item["object_type"] for item in body["items"]} == {
        "report",
        "artifact",
    }
    assert "code" not in {item["object_type"] for item in body["items"]}
    assert internal.id not in {item["route"].get("artifact_id") for item in body["items"]}
    report = next(item for item in body["items"] if item["object_type"] == "report")
    assert "view_history" not in report["capabilities"]
    assert "export_report" not in report["capabilities"]
    for item in body["items"]:
        assert "reference_in_chat" not in item["capabilities"]
        assert "manage_in_files" not in item["capabilities"]
    assert first.headers["cache-control"] == "private, no-store"

    direct = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "filters": {"lens": "current_report"},
        },
    ).json()
    assert {item["route"]["artifact_id"] for item in direct["items"]} == {
        inside.id,
        code_output.id,
    }
    output_item = next(item for item in direct["items"] if item["route"]["artifact_id"] == code_output.id)
    assert output_item["relation_roles"] == [
        "report_computation",
        "code_output",
    ]

    exact_miss = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "query": outside.filename,
        },
    )
    random_miss = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "query": "definitely-not-present.csv",
        },
    )
    assert exact_miss.status_code == random_miss.status_code == 200
    assert (
        (
            exact_miss.json()["matched_total"],
            exact_miss.json()["items"],
        )
        == (
            random_miss.json()["matched_total"],
            random_miss.json()["items"],
        )
        == (0, [])
    )

    locate_outside = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "locate",
            "anchor_key": f"artifact:{outside.id}",
        },
    )
    locate_random = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "locate",
            "anchor_key": "artifact:art_random_missing",
        },
    )
    assert locate_outside.status_code == locate_random.status_code == 409
    assert locate_outside.json() == locate_random.json()

    quick = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "quick",
            "current_key": f"artifact:{outside.id}",
            "recent_keys": ["artifact:art_random_missing"],
        },
    )
    assert quick.status_code == 200
    assert outside.id not in {item["route"].get("artifact_id") for item in quick.json()["items"]}

    codec = ResultsCursorCodec.from_environment()
    owner_binding = ResultsCursorBinding(
        scope_fingerprint=results_scope_fingerprint(
            route_kind="owner",
            team_id=env.team.id,
            user_id=env.bob.id,
            scope_id="ses_1",
        ),
        query_filter_hash=results_query_filter_hash(
            query="",
            filters=ResultsFiltersV1().model_dump(mode="json"),
        ),
        sort_version=RESULTS_SORT_VERSION_V1,
    )
    owner_snapshot, _ = codec.issue_snapshot(
        high_water_created_at=None,
        high_water_artifact_id=None,
        membership_cutoff_at=datetime.now(tz=UTC).isoformat(
            timespec="microseconds",
        ),
        binding=owner_binding,
        projection_digest="0" * 64,
    )
    replay = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "snapshot_token": owner_snapshot,
        },
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["error"]["code"] == "results_snapshot_stale"
    assert replay.headers["cache-control"] == "private, no-store"

    second_canvas = env.seed_report(session_id="ses_2")
    _run(
        env.canvas.append_section(
            canvas_id=second_canvas.id,
            kind="h1",
            payload={"text": "Second report"},
        ),
    )
    env.share_report("ses_2", env.bob)
    cross_report = client.post(
        f"{_BASE}/by-session/ses_2/results/query",
        json={
            "dto_version": 1,
            "surface": "explorer",
            "snapshot_token": body["snapshot_token"],
        },
    )
    assert cross_report.status_code == 409
    assert cross_report.json()["detail"]["error"]["code"] == ("results_snapshot_stale")

    carol = _run(
        env.users.create_user(
            team_id=env.team.id,
            email="results-carol@x.com",
            password="Passw0rd!",
        ),
    )
    env.share_report("ses_1", carol)
    cross_viewer = env.client(carol).post(
        route,
        json={
            "dto_version": 1,
            "surface": "explorer",
            "snapshot_token": body["snapshot_token"],
        },
    )
    assert cross_viewer.status_code == 409
    assert cross_viewer.json()["detail"]["error"]["code"] == ("results_snapshot_stale")

    # A newly-created artifact outside the report closure cannot influence
    # report-only new-result counts or projection signals.
    _run(
        env.artifacts.put_bytes(
            content=b"late secret",
            filename="late-secret.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=env.alice.id,
        ),
    )
    outside_changes = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "changes",
            "snapshot_token": body["snapshot_token"],
        },
    )
    assert outside_changes.status_code == 200
    assert outside_changes.json()["new_results"] == {
        "count": 0,
        "projection_changed": False,
    }

    assert _run(
        env.artifacts.soft_delete(
            code_output.id,
            user_id=env.alice.id,
        ),
    )
    deleted_quick = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "quick",
            "snapshot_token": body["snapshot_token"],
            "current_key": f"artifact:{code_output.id}",
        },
    )
    deleted_locate = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "locate",
            "snapshot_token": body["snapshot_token"],
            "anchor_key": f"artifact:{code_output.id}",
        },
    )
    assert deleted_quick.status_code == 200
    assert code_output.id not in {item["route"].get("artifact_id") for item in deleted_quick.json()["items"]}
    assert deleted_locate.status_code == 409
    assert deleted_locate.json() == locate_random.json()

    _run(
        env.canvas.delete_section(
            section_id=chart_section.id,
            canvas_id=cv.id,
        ),
    )
    changed = client.post(
        route,
        json={
            "dto_version": 1,
            "surface": "changes",
            "snapshot_token": body["snapshot_token"],
        },
    )
    assert changed.status_code == 200
    assert changed.json()["items"] == []
    assert changed.json()["new_results"]["projection_changed"] is True

    # SourceRef aggregation is part of the report closure authorization input.
    # Complete Results must fail closed instead of returning a partial list.
    client.app.state.turn_log_store = None
    unavailable = client.post(
        route,
        json={"dto_version": 1, "surface": "explorer"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["error"]["code"] == ("results_read_model_unavailable")

    _run(env.access.revoke_resource_grant(env.team.id, grant.id))
    revoked = client.post(
        route,
        json={"dto_version": 1, "surface": "explorer"},
    )
    missing = client.post(
        f"{_BASE}/by-session/missing/results/query",
        json={"dto_version": 1, "surface": "explorer"},
    )
    assert revoked.status_code == missing.status_code == 404
    assert revoked.json() == missing.json()


def test_shared_with_me_list_and_delete_cascade(tmp_path):
    env = _Env(tmp_path)
    cv = env.seed_report()
    env.share_report("ses_1", env.bob)
    b = env.client(env.bob)

    items = b.get(f"{_BASE}?scope=shared").json()["items"]
    assert [i["session_id"] for i in items] == ["ses_1"]
    # 0708 ⑤: shared_by is now the sharer's display name (email-local fallback when
    # display_name is unset), not the raw usr_* id.
    _exp_name = env.alice.display_name or (env.alice.email or "").split("@")[0] or env.alice.id
    assert items[0]["shared_by"] == _exp_name
    assert items[0]["shared_by"] != env.alice.id
    # mine partition excludes others' shares.
    assert b.get(f"{_BASE}?scope=mine").json()["items"] == []

    # Owner deletes → cascade revokes grant → shared list immediately cleared.
    a = env.client(env.alice)
    assert a.delete(f"{_BASE}/{cv.id}").status_code == 200
    assert b.get(f"{_BASE}?scope=shared").json()["items"] == []
    grants = _run(env.access.list_resource_grants(env.team.id, resource_kind="report", resource_id="ses_1"))
    assert grants == []


def test_shared_with_me_excludes_reports_owned_by_viewer(tmp_path):
    env = _Env(tmp_path)
    env.seed_report(session_id="ses_team", owner=env.alice)
    _run(
        set_audience(
            env.access,
            kind="report",
            resource_id="ses_team",
            team_id=env.team.id,
            target="team_wide",
            actor_id=env.alice.id,
        )
    )

    assert env.client(env.alice).get(f"{_BASE}?scope=shared").json()["items"] == []
    bob_items = env.client(env.bob).get(f"{_BASE}?scope=shared").json()["items"]
    assert [i["session_id"] for i in bob_items] == ["ses_team"]
    # 0708 ⑤: resolved display name, not the raw usr_* id.
    _exp_name = env.alice.display_name or (env.alice.email or "").split("@")[0] or env.alice.id
    assert bob_items[0]["shared_by"] == _exp_name
    assert bob_items[0]["shared_by"] != env.alice.id
