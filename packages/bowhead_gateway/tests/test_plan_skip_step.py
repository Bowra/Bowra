"""Track B — authed skip_step (受阻恢复). Pins the tenancy + state invariants:
  - owned blocked step → 200, marked SKIPPED
  - cross-tenant / unknown plan → 404 (ownership in get_plan WHERE)
  - non-blocked step → 409
  - flag OFF → 404

Identity is injected via FastAPI dependency_overrides[_current_user] so the test
doesn't need the auth user store (the endpoint only uses user.id for ownership).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.plan_store import PlanStatus, PlanStore, StepStatus, new_step
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import Settings


class _Step:
    def __init__(self, sid: str, status: str) -> None:
        self.id = sid
        self.status = status


class _FakePlanStore:
    def __init__(self, plan: Any, steps: list[_Step], cancel_ok: bool = True) -> None:
        self._plan = plan
        self._steps = steps
        self._cancel_ok = cancel_ok
        self.updated: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def get_plan(self, plan_id: str, *, user_id: str, workspace_id: str) -> Any:
        if self._plan is None or user_id != "u_dev_alice":
            return None
        return self._plan

    async def list_steps(self, plan_id: str) -> list[_Step]:
        return self._steps

    async def update_step_status(self, step_id: str, *, status: str, **_: Any) -> bool:
        self.updated.append((step_id, status))
        return True

    async def update_plan_status(self, plan_id: str, status: str) -> bool:
        self.cancelled.append(status)
        return self._cancel_ok  # False simulates terminal-sticky reject


def _app(store: _FakePlanStore) -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.plan_store = store
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_dev_alice")
    return app


async def _skip(app: Any, plan_id: str, step_id: str, ws: str = "1", expected_rev: int | None = None) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(
            f"/api/v1/plans/{plan_id}/skip_step/{step_id}",
            json={"workspace_id": ws, "expected_rev": expected_rev},
        )


async def _blocked_store(tmp_path):
    store = PlanStore(str(tmp_path / "skip.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="skip")
    step = new_step(plan_id=plan.id, seq=0, agent_name="compute")
    await store.add_steps([step])
    await store.update_step_status(step.id, status="failed")
    await store.update_plan_status(plan.id, "running")
    await store.update_plan_status(plan.id, "blocked")
    return store, plan, step


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_rev", [None, 1])
async def test_skip_blocked_step_ok(monkeypatch, tmp_path, expected_rev) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store, plan, step = await _blocked_store(tmp_path)
    resp = await _skip(_app(store), plan.id, step.id, expected_rev=expected_rev)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert resp.json()["current_rev"] == 2
    assert resp.json()["steps"][0]["status"] == "skipped"
    saved = await store.get_plan(plan.id, user_id="u_dev_alice", workspace_id="1")
    assert saved.current_rev == 2
    assert (await store.list_steps(plan.id))[0].status == StepStatus.SKIPPED
    assert not await store.try_claim_running(plan.id, "t_old", expected_rev=1)
    assert await store.try_claim_running(plan.id, "t_reviewed", expected_rev=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["claim", "revision", "step", "worker_tail"])
async def test_skip_rechecks_idle_plan_revision_and_step_inside_transaction(monkeypatch, tmp_path, race) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store, plan, step = await _blocked_store(tmp_path)
    real_skip = store.skip_blocked_step

    async def concurrent_skip(plan_id, step_id, *, expected_rev):
        if race == "claim":
            assert await store.try_claim_running(plan_id, "t_competing", expected_rev=expected_rev)
        elif race == "revision":
            assert await real_skip(plan_id, step_id, expected_rev=expected_rev)
        elif race == "step":
            await store.update_step_status(step_id, status="done")
        else:
            assert await store.try_claim_running(plan_id, "t_tail", expected_rev=expected_rev)
            await store.update_plan_status(plan_id, "blocked")
        return await real_skip(plan_id, step_id, expected_rev=expected_rev)

    monkeypatch.setattr(store, "skip_blocked_step", concurrent_skip)
    response = await _skip(_app(store), plan.id, step.id, expected_rev=1)
    assert response.status_code == 409
    saved = await store.get_plan(plan.id, user_id="u_dev_alice", workspace_id="1")
    assert saved.current_rev == (2 if race == "revision" else 1)
    assert (await store.list_steps(plan.id))[0].status == {"revision": StepStatus.SKIPPED, "step": StepStatus.DONE}.get(race, StepStatus.FAILED)


@pytest.mark.asyncio
async def test_skip_cross_tenant_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakePlanStore(plan=None, steps=[])  # get_plan → None (not owner)
    resp = await _skip(_app(store), "plan_other", "ps_1")
    assert resp.status_code == 404
    assert store.updated == []


@pytest.mark.asyncio
async def test_skip_non_blocked_step_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakePlanStore(
        plan=SimpleNamespace(status=PlanStatus.RUNNING),
        steps=[_Step("ps_1", StepStatus.RUNNING)],  # running ≠ blocked/failed
    )
    resp = await _skip(_app(store), "plan_x", "ps_1")
    assert resp.status_code == 409
    assert store.updated == []


@pytest.mark.asyncio
async def test_skip_flag_off_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")  # flag now defaults ON; "0" disables
    store = _FakePlanStore(
        plan=SimpleNamespace(status=PlanStatus.BLOCKED),
        steps=[_Step("ps_1", StepStatus.BLOCKED)],
    )
    resp = await _skip(_app(store), "plan_x", "ps_1")
    assert resp.status_code == 404
    assert store.updated == []


# ── track C: durable cancel endpoint ──────────────────────────────────────────


async def _cancel(app: Any, plan_id: str, ws: str = "1") -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(f"/api/v1/plans/{plan_id}/cancel", json={"workspace_id": ws})


@pytest.mark.asyncio
async def test_cancel_running_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakePlanStore(plan=SimpleNamespace(status=PlanStatus.RUNNING), steps=[])
    resp = await _cancel(_app(store), "plan_x")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert store.cancelled == [PlanStatus.CANCELLED]


@pytest.mark.asyncio
async def test_cancel_cross_tenant_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakePlanStore(plan=None, steps=[])  # not owner
    resp = await _cancel(_app(store), "plan_other")
    assert resp.status_code == 404
    assert store.cancelled == []


@pytest.mark.asyncio
async def test_cancel_terminal_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    # update_plan_status returns False → terminal-sticky reject → 409
    store = _FakePlanStore(plan=SimpleNamespace(status=PlanStatus.COMPLETED), steps=[], cancel_ok=False)
    resp = await _cancel(_app(store), "plan_x")
    assert resp.status_code == 409
