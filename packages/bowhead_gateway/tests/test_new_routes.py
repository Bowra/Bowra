"""Memory / Sessions / Files 新 routes 的集成测试."""

from __future__ import annotations

import io

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore, UserMemoryStore


@pytest.fixture
async def app_ctx(tmp_path):
    from bowhead_core import ArtifactStore, LocalBackend

    db = tmp_path / "test.db"
    memory = UserMemoryStore(db)
    sess = SessionStore(db)
    art = ArtifactStore(
        db_path=str(db),
        backend=LocalBackend(root_dir=tmp_path / "artifacts"),
    )
    await memory.initialize()
    await sess.initialize()
    await art.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = memory
    app.state.session_store = sess
    app.state.artifact_store = art
    # Required placeholders (chat endpoint dependencies).
    app.state.orchestrator = None
    app.state.datarc = None
    return app


@pytest.mark.integration
async def test_memory_remember_list_delete(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # list (initially empty)
        r = await c.get("/api/v1/memory/user")
        assert r.status_code == 200
        assert r.json()["entries"] == []

        # remember
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "default_date_range", "value": "last_30_days"},
        )
        assert r.status_code == 200
        entry_id = r.json()["id"]

        # list again
        r = await c.get("/api/v1/memory/user")
        body = r.json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["key"] == "default_date_range"
        assert body["entries"][0]["value"] == "last_30_days"

        # delete
        r = await c.delete(f"/api/v1/memory/user/{entry_id}")
        assert r.status_code == 200

        # list again → empty
        r = await c.get("/api/v1/memory/user")
        assert r.json()["entries"] == []


@pytest.mark.integration
async def test_sessions_list_empty(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/sessions")
        assert r.status_code == 200
        body = r.json()
        assert {g["id"] for g in body["groups"]} == {"today", "yest", "last7", "older"}
        assert all(g["items"] == [] for g in body["groups"])


@pytest.mark.integration
async def test_sessions_list_older_than_week_shows_in_older_bucket(app_ctx) -> None:
    """sidebar-older-bucket (2026-07-20): 之前 >7 天的 session 会被 list_sessions
    直接丢弃、永不出现在响应里（数据仍在库中，只是 API 从不返回）。回归测试确保
    它们现在能通过 "older" 分组重新可见。"""
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    store: SessionStore = app_ctx.state.session_store
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="1",
        user_id=app_ctx.state.settings.dev_default_user,
    )
    await store.touch_session(session_id=sess.id, title_hint="十天前的分析", intent="lookup")

    # Set last_active_at directly to 10 days ago to simulate a stale session (format must match _now(),
    # otherwise route handler's datetime.fromisoformat fails on mixed tz-naive/aware values).
    ten_days_ago = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE sessions SET last_active_at=? WHERE id=?",
            (ten_days_ago, sess.id),
        )
        await db.commit()

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/sessions")
        assert r.status_code == 200
        body = r.json()
        by_id = {g["id"]: g["items"] for g in body["groups"]}
        assert len(by_id["older"]) == 1
        assert by_id["older"][0]["title"] == "十天前的分析"
        assert by_id["today"] == []
        assert by_id["yest"] == []
        assert by_id["last7"] == []


@pytest.mark.integration
async def test_sessions_list_returns_recorded(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    # Prerecord a session + turn.
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="1",
        user_id=app_ctx.state.settings.dev_default_user,
    )
    await store.record_turn(turn_id="t_x", session_id=sess.id, user_text="今年 GMV")
    await store.touch_session(
        session_id=sess.id,
        title_hint="今年 GMV",
        intent="lookup",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/sessions")
        items = r.json()["groups"][0]["items"]
        assert len(items) == 1
        assert items[0]["title"] == "今年 GMV"
        assert items[0]["intents"] == ["lookup"]

        # detail
        r2 = await c.get(f"/api/v1/sessions/{sess.id}")
        detail = r2.json()
        assert detail["session"]["id"] == sess.id
        assert len(detail["turns"]) == 1


@pytest.mark.integration
async def test_session_detail_omits_legacy_failure_answers(app_ctx, tmp_path) -> None:
    """Chat turn feedback D3 (design §8 Q1): an old failed turn's system failure sentence is
    omitted from the DTO, decided from its persisted final_answer event; a real answer stays."""
    from bowhead_core.turn_log_store import TurnLogStore

    store: SessionStore = app_ctx.state.session_store
    log = TurnLogStore(tmp_path / "test.db")
    await log.initialize()
    cases = {
        "t_legacy_fallback": ("silent_done_fallback", "failed"),
        "t_legacy_reason": ("wallclock_exceeded", "failed"),
        "t_real_answer": ("canvas_pointer_reconciled", "failed"),
        "t_completed_empty": ("silent_done_fallback", "completed"),
    }
    session_by_turn: dict[str, str] = {}
    for turn_id, (reason_code, status) in cases.items():
        # One session per turn: a session admits one active turn at a time.
        sess = await store.ensure_session(session_id=None, workspace_id="1", user_id=app_ctx.state.settings.dev_default_user)
        session_by_turn[turn_id] = sess.id
        await store.record_turn(turn_id=turn_id, session_id=sess.id, user_text="q")
        text = f"answer of {turn_id}"
        await log.record(turn_id=turn_id, event_type="final_answer", payload={"text": text, "reason_code": reason_code})
        if status == "failed":
            await store.mark_failed(turn_id=turn_id, error=reason_code, error_kind="timeout", final_answer=text)
        else:
            await store.mark_completed(turn_id=turn_id, intent="lookup", final_answer=text, tokens_input=0, tokens_output=0, cost_usd=0.0)

    answers: dict[str, str | None] = {}
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        for turn_id, session_id in session_by_turn.items():
            detail = (await c.get(f"/api/v1/sessions/{session_id}")).json()
            answers[turn_id] = next(turn["final_answer"] for turn in detail["turns"] if turn["id"] == turn_id)

    assert answers["t_legacy_fallback"] == ""
    assert answers["t_legacy_reason"] == ""
    assert answers["t_real_answer"] == "answer of t_real_answer"
    assert answers["t_completed_empty"] == "answer of t_completed_empty"


@pytest.mark.integration
async def test_sessions_seen_pin_unpin_endpoints(app_ctx) -> None:
    """W12-3 (#74) + W12-3b (#87): /seen + /pin (POST) + /pin (DELETE) endpoint."""
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="1",
        user_id=uid,
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # 1. seen
        r = await c.post(f"/api/v1/sessions/{sess.id}/seen")
        assert r.status_code == 204
        # last_seen_at persisted after list.
        r2 = await c.get("/api/v1/sessions")
        items = r2.json()["groups"][0]["items"]
        item = next(i for i in items if i["id"] == sess.id)
        # User viewed it → unread = False (last_seen_at >= last_active_at).
        assert item["unread"] is False

        # 2. pin
        r = await c.post(f"/api/v1/sessions/{sess.id}/pin")
        assert r.status_code == 204
        r2 = await c.get("/api/v1/sessions")
        items = r2.json()["groups"][0]["items"]
        item = next(i for i in items if i["id"] == sess.id)
        assert item["pinned"] is True

        # 3. unpin
        r = await c.delete(f"/api/v1/sessions/{sess.id}/pin")
        assert r.status_code == 204
        r2 = await c.get("/api/v1/sessions")
        items = r2.json()["groups"][0]["items"]
        item = next(i for i in items if i["id"] == sess.id)
        # pinned: bool | None = None; list_sessions computes None when pinned_at is absent.
        assert item["pinned"] is None

        # 4. seen / pin for missing session → 404.
        r = await c.post("/api/v1/sessions/s_nope/seen")
        assert r.status_code == 404
        r = await c.post("/api/v1/sessions/s_nope/pin")
        assert r.status_code == 404


@pytest.mark.integration
async def test_file_upload_size_and_mime_checks(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # Valid PNG upload.
        r = await c.post(
            "/api/v1/files/upload",
            files={"file": ("test.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 100), "image/png")},
        )
        assert r.status_code == 200
        uploaded_file_id = r.json()["file_id"]
        assert uploaded_file_id.startswith("art_")

        # Option A V3 review: file deep links search by stable artifact id so
        # filenames never need to appear in browser URLs or proxy access logs.
        listed = await c.get(
            "/api/v1/files/",
            params={"q": uploaded_file_id, "source": "all"},
        )
        assert listed.status_code == 200
        assert [item["file_id"] for item in listed.json()["items"]] == [
            uploaded_file_id,
        ]

        # Rejected MIME.
        r = await c.post(
            "/api/v1/files/upload",
            files={"file": ("bad.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
        )
        assert r.status_code == 415

        # text/data MIME must match an allowed extension; MIME alone is not enough.
        r = await c.post(
            "/api/v1/files/upload",
            files={"file": ("page.html", io.BytesIO(b"<script>x()</script>"), "text/plain")},
        )
        assert r.status_code == 415

        # Browser MIME fallbacks are normalized before the MIME/extension pairing check.
        r = await c.post(
            "/api/v1/files/upload",
            files={"file": ("notes.md", io.BytesIO(b"# Notes\n"), "text/plain")},
        )
        assert r.status_code == 200
        assert r.json()["mime"] == "text/markdown"

        r = await c.post(
            "/api/v1/files/upload",
            files={"file": ("events.jsonl", io.BytesIO(b'{"a":1}\n'), "application/json")},
        )
        assert r.status_code == 200
        assert r.json()["mime"] == "application/x-ndjson"


@pytest.mark.integration
async def test_upload_session_pin_idor_and_kind_validation(app_ctx) -> None:
    """Sprint pptx-from-template M1 review (P1 IDOR + P2 kind allowlist):
    /upload?session_id= must reject pinning into a session the caller does not
    own, and reject an unknown kind."""
    store: SessionStore = app_ctx.state.session_store

    def png():
        return io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 50)

    victim = await store.ensure_session(
        session_id=None,
        workspace_id="1",
        user_id="u_victim",
    )
    mine = await store.ensure_session(
        session_id=None,
        workspace_id="1",
        user_id=app_ctx.state.settings.dev_default_user,
    )
    foreign_manifest = await app_ctx.state.artifact_store.put_bytes(
        content=b"foreign",
        filename="foreign.json",
        mime="application/json",
        source="canvas_export",
        created_by="u_victim",
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # caller (dev_default) pins into u_victim's session → 403
        r = await c.post(
            f"/api/v1/files/upload?session_id={victim.id}",
            files={"file": ("x.png", png(), "image/png")},
        )
        assert r.status_code == 403

        # own session → allowed
        r2 = await c.post(
            f"/api/v1/files/upload?session_id={mine.id}&kind=user_upload",
            files={"file": ("y.png", png(), "image/png")},
        )
        assert r2.status_code == 200

        # own session but bogus kind → 400
        r3 = await c.post(
            f"/api/v1/files/upload?session_id={mine.id}&kind=evil_kind",
            files={"file": ("z.png", png(), "image/png")},
        )
        assert r3.status_code == 400

        # Provenance links cannot cross the upload caller's ownership boundary,
        # even when the caller already knows the foreign artifact ID.
        r4 = await c.post(
            f"/api/v1/files/upload?manifest_artifact_id={foreign_manifest.id}",
            files={"file": ("linked.png", png(), "image/png")},
        )
        assert r4.status_code == 400


@pytest.mark.integration
async def test_upload_allows_same_turn_sandbox_pin_for_non_http_user(app_ctx) -> None:
    """Feishu/tool-runtime uploads do not carry the browser user, but same-turn
    sandbox outputs should still be able to pin artifacts into their session."""
    store: SessionStore = app_ctx.state.session_store
    owner_session = await store.ensure_session(
        session_id="feishu:tenant:chat:open",
        workspace_id="1",
        user_id="u_feishu",
    )
    other_session = await store.ensure_session(
        session_id="feishu:tenant:chat:other",
        workspace_id="1",
        user_id="u_other",
    )
    turn = await store.record_turn(
        turn_id="t_feishu_upload",
        session_id=owner_session.id,
        user_text="make a csv",
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            (f"/api/v1/files/upload?session_id={owner_session.id}&source=python_sandbox&kind=sandbox_output&created_by_turn={turn.id}"),
            files={"file": ("out.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        )
        assert r.status_code == 200
        artifact = await app_ctx.state.artifact_store.get(r.json()["file_id"])
        assert artifact is not None
        assert artifact.created_by == "u_feishu"
        assert artifact.created_by_turn == turn.id

        cross = await c.post(
            (f"/api/v1/files/upload?session_id={other_session.id}&source=python_sandbox&kind=sandbox_output&created_by_turn={turn.id}"),
            files={"file": ("evil.csv", io.BytesIO(b"a\n1\n"), "text/csv")},
        )
        assert cross.status_code == 403

        await store.mark_completed(
            turn_id=turn.id,
            intent="analysis",
            final_answer="done",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0,
        )
        after_done = await c.post(
            (f"/api/v1/files/upload?session_id={owner_session.id}&source=python_sandbox&kind=sandbox_output&created_by_turn={turn.id}"),
            files={"file": ("late.csv", io.BytesIO(b"a\n1\n"), "text/csv")},
        )
        assert after_done.status_code == 403
