from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.notification_store import NotificationStore
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import home
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from bowhead_core import CanvasStore


@pytest.fixture
def user() -> User:
    return cast("User", SimpleNamespace(id="user_1", team_id="team_1"))


@pytest.mark.asyncio
async def test_resolve_scope_accepts_only_canonical_workspace_id(user: User) -> None:
    class Store:
        async def list_workspaces_for_user(self, user_id: str) -> list[dict[str, str]]:
            assert user_id == user.id
            return [
                {
                    "workspace_id": "ws_1",
                    "datarc_workspace_id": "legacy_1",
                    "name": "Operations",
                }
            ]

    store = cast("UserStore", Store())
    scope = await home._resolve_scope(user, store, "ws_1")
    assert scope.workspace_id == "ws_1"
    assert scope.workspace_name == "Operations"
    assert scope.query_workspace_ids == ("ws_1", "legacy_1")

    with pytest.raises(HTTPException) as exc:
        await home._resolve_scope(user, store, "legacy_1")
    assert exc.value.status_code == 404
    assert exc.value.detail == "workspace not found"


@pytest.mark.asyncio
async def test_attention_normalizes_legacy_routine_action_url(user: User) -> None:
    row = SimpleNamespace(
        id="notice_1",
        type="scheduled.failed",
        category="async_job",
        priority="p1",
        title="Routine failed",
        body=None,
        metadata={"jobId": "job_1", "jobName": "Weekly sales"},
        action_url="/settings/scheduled",
        subject_type="scheduled_job",
        subject_id="job_1",
        created_at="2026-07-15T08:00:00Z",
        state="open",
    )

    class Store:
        async def list_for_user(self, **_kwargs: object) -> tuple[list[object], None]:
            return [row], None

        async def notification_count(self, **_kwargs: object) -> object:
            return SimpleNamespace(action_required=1)

    request = cast(
        "Request",
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    notification_center_enabled=True,
                    notification_store=Store(),
                ),
            ),
        ),
    )

    items, count = await home._collect_attention(request, user)

    assert count == 1
    assert items[0].actionUrl == "/team/settings/scheduled/"
    # Frontend uses category/type + metadata to rewrite persisted Chinese snapshots in the current language.
    assert items[0].category == "async_job"
    assert items[0].metadata == {"jobId": "job_1", "jobName": "Weekly sales"}


@pytest.mark.asyncio
async def test_snapshot_keeps_reports_when_artifact_source_fails(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.delenv("BOWHEAD_HOME_SNAPSHOT", raising=False)
    scope = home._WorkspaceScope("ws_1", "Operations", ("ws_1",))
    report = home.HomeOutputItemDTO(
        id="report:canvas_1",
        kind="report",
        title="Operations review",
        actionUrl="/?session=session_1&report=1",
        sessionId="session_1",
        createdAt="2026-07-15T08:00:00Z",
        updatedAt="2026-07-15T09:00:00Z",
        metadata={"canvasId": "canvas_1"},
    )

    async def resolve_scope(*_args: object) -> home._WorkspaceScope:
        return scope

    async def no_attention(*_args: object) -> tuple[list[home.HomeAttentionItemDTO], int]:
        return [], 0

    async def no_rows(*_args: object) -> list[Any]:
        return []

    async def reports(*_args: object) -> list[home.HomeOutputItemDTO]:
        return [report]

    async def artifact_failure(*_args: object) -> list[home.HomeOutputItemDTO]:
        raise RuntimeError("artifact store unavailable")

    monkeypatch.setattr(home, "_resolve_scope", resolve_scope)
    monkeypatch.setattr(home, "_collect_attention", no_attention)
    monkeypatch.setattr(home, "_sessions_for_scope", no_rows)
    monkeypatch.setattr(home, "_collect_active_routines", no_rows)
    monkeypatch.setattr(home, "_collect_active_matrices", no_rows)
    monkeypatch.setattr(home, "_collect_reports", reports)
    monkeypatch.setattr(home, "_collect_deliverables", artifact_failure)
    source_metrics: list[tuple[str, str]] = []
    snapshot_metrics: list[str] = []
    monkeypatch.setattr(
        home,
        "_record_source_metric",
        lambda source, outcome, _started: source_metrics.append((source, outcome)),
    )
    monkeypatch.setattr(
        home,
        "_record_snapshot_metric",
        lambda outcome, _started: snapshot_metrics.append(outcome),
    )

    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    result = await home.get_home_snapshot(
        "ws_1",
        request,
        user,
        cast("UserStore", object()),
    )

    assert [item.id for item in result.outputs] == ["report:canvas_1"]
    assert result.counts.outputs == 1
    assert result.partial is True
    assert [(issue.source, issue.code) for issue in result.issues] == [
        ("artifacts", "source_unavailable"),
    ]
    assert ("reports", "success") in source_metrics
    assert ("artifacts", "error") in source_metrics
    assert snapshot_metrics == ["partial"]


@pytest.mark.asyncio
async def test_snapshot_projects_native_status_without_invented_progress(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_SNAPSHOT", "1")
    scope = home._WorkspaceScope("ws_1", "Operations", ("ws_1",))
    chat = home.HomeActiveItemDTO(
        id="chat:turn_1",
        engine="chat",
        nativeStatus="running",
        title="Prepare a board update",
        progress=None,
        actionUrl="/?session=session_1",
        sessionId="session_1",
        startedAt="2026-07-15T09:00:00Z",
    )
    matrix = home.HomeActiveItemDTO(
        id="matrix:run_1",
        engine="matrix",
        nativeStatus="running",
        title="Contract matrix",
        progress=home.HomeProgressDTO(completed=4, total=10, failed=1),
        actionUrl="/knowledge?tab=matrix&view=matrix_1",
        startedAt="2026-07-15T08:00:00Z",
    )

    async def resolve_scope(*_args: object) -> home._WorkspaceScope:
        return scope

    async def attention(*_args: object) -> tuple[list[home.HomeAttentionItemDTO], int]:
        return [], 0

    async def sessions(*_args: object) -> list[object]:
        return [object()]

    async def active_turns(*_args: object) -> list[home.HomeActiveItemDTO]:
        return [chat]

    async def no_rows(*_args: object) -> list[Any]:
        return []

    async def matrices(*_args: object) -> list[home.HomeActiveItemDTO]:
        return [matrix]

    monkeypatch.setattr(home, "_resolve_scope", resolve_scope)
    monkeypatch.setattr(home, "_collect_attention", attention)
    monkeypatch.setattr(home, "_sessions_for_scope", sessions)
    monkeypatch.setattr(home, "_collect_active_turns", active_turns)
    monkeypatch.setattr(home, "_collect_active_routines", no_rows)
    monkeypatch.setattr(home, "_collect_active_matrices", matrices)
    monkeypatch.setattr(home, "_collect_reports", no_rows)
    monkeypatch.setattr(home, "_collect_deliverables", no_rows)

    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    result = await home.get_home_snapshot(
        "ws_1",
        request,
        user,
        cast("UserStore", object()),
    )

    assert result.counts.active == 2
    assert result.active[0].engine == "chat"
    assert result.active[0].progress is None
    assert result.active[1].engine == "matrix"
    assert result.active[1].progress == home.HomeProgressDTO(completed=4, total=10, failed=1)


@pytest.mark.asyncio
async def test_snapshot_flag_explicit_off_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_SNAPSHOT", "0")
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    with pytest.raises(HTTPException) as exc:
        await home.get_home_snapshot(
            "ws_1",
            request,
            user,
            cast("UserStore", object()),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == {
        "code": "home_snapshot_disabled",
        "message": "home snapshot disabled",
    }


def test_snapshot_http_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_SNAPSHOT", "1")
    app = FastAPI()
    app.include_router(home.router, prefix="/api/v1/home")
    app.state.settings = SimpleNamespace(auth_mode="jwt")
    app.state.user_store = object()

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/home/snapshot",
        params={"workspace_id": "ws_victim"},
    )

    assert response.status_code == 401


def test_snapshot_http_hides_non_member_workspace(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_SNAPSHOT", "1")

    class Store:
        async def list_workspaces_for_user(self, user_id: str) -> list[dict[str, str]]:
            assert user_id == user.id
            return [{"workspace_id": "ws_member", "name": "Member space"}]

    app = FastAPI()
    app.include_router(home.router, prefix="/api/v1/home")
    app.dependency_overrides[_current_user] = lambda: user
    app.state.user_store = Store()

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/home/snapshot",
        params={"workspace_id": "ws_victim"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "workspace not found"}


@pytest.mark.asyncio
async def test_routine_job_enabled_is_not_a_running_run(user: User) -> None:
    job = SimpleNamespace(id="job_1", name="Weekly review", workspace_id="ws_1", status="active")

    class Store:
        async def list_for_user(self, user_id: str) -> list[object]:
            assert user_id == user.id
            return [job]

        async def list_runs(self, job_id: str, limit: int) -> list[object]:
            assert (job_id, limit) == ("job_1", 1)
            return [SimpleNamespace(id="run_1", status="completed")]

    request = cast(
        "Request",
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduled_store=Store()))),
    )
    scope = home._WorkspaceScope("ws_1", "Operations", ("ws_1",))

    assert await home._collect_active_routines(request, user, scope) == []


@pytest.mark.asyncio
async def test_active_turns_use_listed_running_signal_without_per_session_reads() -> None:
    idle = [SimpleNamespace(id=f"s_idle_{index}", title=None, active_turn_id=None, last_active_at="2026-09-24T00:00:00Z") for index in range(50)]
    running = SimpleNamespace(
        id="s_run",
        title="Quarterly review",
        active_turn_id="t_run",
        last_active_at="2026-09-24T01:00:00Z",
    )
    stale = SimpleNamespace(id="s_stale", title=None, active_turn_id="t_done", last_active_at="2026-09-24T02:00:00Z")
    turns = {
        "t_run": SimpleNamespace(
            id="t_run",
            status="running",
            user_text="ignored",
            started_at="2026-09-24T01:00:00Z",
            agent_type=None,
        ),
        "t_done": SimpleNamespace(
            id="t_done",
            status="completed",
            user_text="ignored",
            started_at="2026-09-24T00:30:00Z",
            agent_type=None,
        ),
    }
    fetched: list[str] = []

    class Store:
        async def get_active_turn(self, _session_id: str) -> str | None:
            raise AssertionError("the listed session row already carries active_turn_id")

        async def get_turn(self, turn_id: str) -> object | None:
            fetched.append(turn_id)
            return turns.get(turn_id)

    request = cast(
        "Request",
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_store=Store()))),
    )

    items = await home._collect_active_turns(request, [*idle, running, stale])

    assert sorted(fetched) == ["t_done", "t_run"]
    assert [(item.id, item.title, item.sessionId) for item in items] == [
        ("chat:t_run", "Quarterly review", "s_run"),
    ]


@pytest.mark.asyncio
async def test_snapshot_projects_records_from_real_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_SNAPSHOT", "1")

    shared_db = tmp_path / "shared.db"
    user_store = UserStore(shared_db)
    session_store = SessionStore(shared_db)
    notification_store = NotificationStore(str(shared_db))
    scheduled_store = ScheduledJobStore(str(shared_db))
    canvas_store = CanvasStore(tmp_path / "canvas.db")
    matrix_store = KbMatrixStore(tmp_path / "kb_matrix.db")
    artifact_store = ArtifactStore(
        db_path=str(tmp_path / "artifacts.db"),
        backend=LocalBackend(root_dir=tmp_path / "files"),
    )
    for store in (
        user_store,
        session_store,
        notification_store,
        scheduled_store,
        canvas_store,
        artifact_store,
        matrix_store,
    ):
        await store.initialize()

    team = await user_store.create_team(name="Acme", max_seats=10)
    real_user = await user_store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="correct horse battery staple",
    )
    workspace_id = await user_store.create_local_workspace(
        team_id=team.id,
        name="Operations",
        creator_user_id=real_user.id,
    )
    other_user = await user_store.create_user(
        team_id=team.id,
        email="other@example.com",
        password="correct horse battery staple",
    )
    session = await session_store.ensure_session(
        session_id="s_home",
        workspace_id=workspace_id,
        user_id=real_user.id,
    )
    await session_store.record_turn(
        turn_id="t_home",
        session_id=session.id,
        user_text="Prepare the board update",
    )

    canvas = await canvas_store.ensure_canvas(
        session_id=session.id,
        workspace_id=workspace_id,
        user_id=real_user.id,
        title="Board update",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="text",
        payload={"text": "Board update content"},
    )

    notice = await notification_store.upsert_item(
        team_id=team.id,
        category="access",
        type="approval.required",
        priority="high",
        title="Review access request",
        body="Finance space access",
        subject_type="approval",
        subject_id="ap_1",
        actor_user_id=None,
        action_url="/admin/approvals/",
        actions=None,
        metadata=None,
        dedupe_key="approval:ap_1",
    )
    await notification_store.add_recipients(notice.id, [(real_user.id, "approver")])
    other_notice = await notification_store.upsert_item(
        team_id=team.id,
        category="access",
        type="access_requested",
        priority="high",
        title="Other user's request",
        body=None,
        subject_type="access_request",
        subject_id="ar_other",
        actor_user_id=None,
        action_url="/admin/access/",
        actions=None,
        metadata=None,
        dedupe_key="access:ar_other",
    )
    await notification_store.add_recipients(other_notice.id, [(other_user.id, "approver")])

    request = cast(
        "Request",
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    notification_center_enabled=True,
                    notification_store=notification_store,
                    session_store=session_store,
                    scheduled_store=scheduled_store,
                    canvas_store=canvas_store,
                    artifact_store=artifact_store,
                    kb_matrix_store=matrix_store,
                ),
            )
        ),
    )
    result = await home.get_home_snapshot(
        workspace_id,
        request,
        real_user,
        user_store,
    )

    assert result.scope == home.HomeScopeDTO(
        workspaceId=workspace_id,
        workspaceName="Operations",
    )
    assert [item.title for item in result.attention] == ["Review access request"]
    assert [(item.engine, item.title, item.progress) for item in result.active] == [
        ("chat", "Prepare the board update", None),
    ]
    assert [(item.kind, item.title) for item in result.outputs] == [
        ("report", "Board update"),
    ]
    assert result.counts == home.HomeCountsDTO(attention=1, active=1, outputs=1)
    assert result.partial is False
    assert result.issues == []


@pytest.mark.asyncio
async def test_recent_outputs_not_starved_by_empty_canvases(
    user: User,
    tmp_path: Path,
) -> None:
    """empty-canvas 清理批: 最近 8 个 canvas 全是空壳时,首页仍要显出真报告。

    此前 `_collect_reports` 是「limit=8 先取 → 再过滤 sections_count>0」, 空壳占坑
    就少显卡片(116 实测最重空间 8 取 5, 首页最终只取 top-4)。
    """
    import sqlite3

    db = tmp_path / "canvas.db"
    canvas_store = CanvasStore(db)
    await canvas_store.initialize()
    real = await canvas_store.ensure_canvas(
        session_id="s_real",
        workspace_id="ws_1",
        user_id=user.id,
        title="Board update",
    )
    await canvas_store.append_section(
        canvas_id=real.id,
        kind="text",
        payload={"text": "Board update content"},
    )
    for i in range(8):
        await canvas_store.ensure_canvas(
            session_id=f"s_shell_{i}",
            workspace_id="ws_1",
            user_id=user.id,
        )
    # canvas_store._now() has only second precision; explicitly sort the empty shell ahead of the real report.
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE canvases SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (real.id,))
        con.execute("UPDATE canvases SET updated_at='2026-02-01T00:00:00+00:00' WHERE id!=?", (real.id,))
        con.commit()
    finally:
        con.close()

    request = cast(
        "Request",
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(canvas_store=canvas_store))),
    )
    scope = home._WorkspaceScope(
        workspace_id="ws_1",
        workspace_name="Operations",
        query_workspace_ids=("ws_1",),
    )

    items = await home._collect_reports(request, user, scope)
    assert [(item.kind, item.title) for item in items] == [("report", "Board update")]


def test_untitled_report_title_is_left_to_the_localized_client_fallback() -> None:
    from bowhead_gateway.routes.home import _report_title

    assert _report_title({"title": " Board update ", "first_section_title": "Intro"}) == "Board update"
    # A blank stored title falls through to the first section heading.
    assert _report_title({"title": "   ", "first_section_title": "Intro"}) == "Intro"
    # No hard-coded English "Untitled report": the client formats "<date> Report" in the UI language.
    assert _report_title({"title": None, "first_section_title": None, "id": "cv_123"}) == ""
