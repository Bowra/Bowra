from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.scheduled_store import ScheduledJob, ScheduledJobRun
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(store: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")

    from bowhead_gateway.routes import scheduled

    app = FastAPI()
    app.state.scheduled_store = store
    app.state.settings = SimpleNamespace(
        auth_mode="dev",
        dev_default_user="user_1",
    )
    app.include_router(scheduled.router, prefix="/api/v1/scheduled")
    return TestClient(app)


def _job() -> ScheduledJob:
    return ScheduledJob(
        id="job_1",
        user_id="user_1",
        workspace_id="ws_1",
        name="Daily",
        cron="0 9 * * *",
        query="summary",
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )


def test_list_jobs_publishes_typed_failure_without_store_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://internal.example password=raw-secret"

    class Store:
        async def list_for_user(self, _user_id: str) -> list[ScheduledJob]:
            raise RuntimeError(secret)

    response = _app(Store(), monkeypatch).get("/api/v1/scheduled/jobs")

    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["code"] == "scheduled_job_load_failed"
    assert body["detail"]["diagnostics"] == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "scheduled_failure",
        "safe_detail": "phase=load; failure_class=storage",
        "retryable": True,
    }
    assert secret not in response.text


def test_run_history_normalizes_legacy_raw_error_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "RuntimeError: Authorization=Bearer raw-secret"

    class Store:
        async def get(self, _job_id: str) -> ScheduledJob:
            return _job()

        async def list_runs(self, _job_id: str) -> list[ScheduledJobRun]:
            return [
                ScheduledJobRun(
                    id="run_1",
                    job_id="job_1",
                    turn_id=None,
                    started_at="2026-07-30T00:00:00+00:00",
                    finished_at="2026-07-30T00:00:01+00:00",
                    status="failed",
                    error=secret,
                ),
            ]

    response = _app(Store(), monkeypatch).get(
        "/api/v1/scheduled/jobs/job_1/runs",
    )

    assert response.status_code == 200
    run = response.json()["items"][0]
    assert run["error"] == "scheduled_turn_failed"
    assert run["error_diagnostics"]["diagnostic_code"] == "scheduled_failure"
    assert run["error_diagnostics"]["safe_detail"] == ("phase=execute; failure_class=runtime")
    assert secret not in response.text


@pytest.mark.asyncio
async def test_run_now_background_failure_persists_only_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "browser public URL is not registered: token=raw-secret"
    finished: list[dict[str, object]] = []

    async def fail_run(*_args: object) -> tuple[str | None, str | None]:
        raise RuntimeError(secret)

    async def finish_run(run_id: str, **kwargs: object) -> None:
        finished.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(
        "bowhead_gateway._scheduled_runner.run_scheduled_turn",
        fail_run,
    )
    store = SimpleNamespace(finish_run=finish_run)

    from bowhead_gateway.routes.scheduled import _run_now_and_finish

    await _run_now_and_finish(
        SimpleNamespace(),
        store,
        _job(),
        "run_1",
    )

    assert finished == [
        {
            "run_id": "run_1",
            "status": "failed",
            "error": "scheduled_run_dispatch_failed",
        },
    ]
    assert secret not in repr(finished)
