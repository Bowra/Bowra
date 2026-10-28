"""planning-ux-reliability (2026-07) — authed GET /plans/{id}/state.

F5/归档 reconcile 的权威只读入口(replay 无序 → 卡片状态漂移)。Pins:
  - owned plan → 200, {plan, steps} 形状与 dev GET 一致
  - cross-tenant / unknown → 404 (ownership in get_plan WHERE)
  - flag OFF → 404

Identity via dependency_overrides[_current_user] (同 test_plan_skip_step)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.plan_store import PlanStatus, PlanStep
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import Settings


class _FakePlanStore:
    def __init__(self, plan: Any, steps: list[Any]) -> None:
        self._plan = plan
        self._steps = steps
        self.get_calls: list[tuple[str, str, str]] = []
        self.replaced_steps: list[Any] | None = None
        self.bumped = False

    async def get_plan(self, plan_id: str, *, user_id: str, workspace_id: str) -> Any:
        self.get_calls.append((plan_id, user_id, workspace_id))
        if self._plan is None or user_id != "u_dev_alice":
            return None
        return self._plan

    async def list_steps(self, plan_id: str) -> list[Any]:
        return self._steps

    async def get_plan_with_steps(self, plan_id: str, *, user_id: str, workspace_id: str) -> Any:
        plan = await self.get_plan(plan_id, user_id=user_id, workspace_id=workspace_id)
        return (plan, self._steps) if plan is not None else None

    async def replace_steps(self, plan_id: str, steps: list[Any], *, expected_rev: int) -> bool:
        self.replaced_steps = steps
        self._steps = steps
        self.bumped = True
        return True


class _FakeRegistry:
    def list_all(self, workspace_id: str) -> list[str]:
        return ["compute"]

    def get(self, name: str, *, workspace_id: str) -> Any:
        if name != "compute":
            raise KeyError(name)
        return SimpleNamespace(description="compute agent")


def _step(sid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        seq=0,
        level=0,
        agent_name="compute",
        objective="o",
        deliverable="",
        depends_on=(),
        needs_corpus=False,
        status="pending",
        included=True,
        origin="authored",
        error=None,
        error_kind=None,
        result_digest="",
    )


def _app(store: _FakePlanStore, user_id: str = "u_dev_alice") -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.plan_store = store
    app.state.orchestrator = SimpleNamespace(multi_agent_registry=_FakeRegistry())
    app.state.router = object()
    # credential-readiness D2.2: amend now resolves the team router by user.team_id.
    # team_id=None → resolve_team_byo_overrides short-circuits to (None, None) → 409 with chat's code;
    # identity-semantics tests here monkeypatch team_router_or_409 to bypass it (below).
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id=user_id, team_id=None)
    return app


async def _get_state(app: Any, plan_id: str, ws: str = "1") -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.get(f"/api/v1/plans/{plan_id}/state?workspace_id={ws}")


@pytest.mark.asyncio
async def test_displayed_plan_revision_and_steps_share_one_snapshot(tmp_path, monkeypatch) -> None:
    import sqlite3

    from bowhead_core import plan_store as plan_module

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    db_path = str(tmp_path / "plan-preview.db")
    store = plan_module.PlanStore(db_path)
    await store.initialize()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="snapshot")
    step = plan_module.new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="original")
    await store.add_steps([step])
    original_decoder = plan_module._row_to_plan
    edited = False

    def decode_after_other_tab_saves(row):
        nonlocal edited
        decoded = original_decoder(row)
        if not edited:
            edited = True
            # Commit a real second connection precisely between the two reads.
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE plans SET current_rev=current_rev+1 WHERE id=?", (plan.id,))
                db.execute("UPDATE plan_steps SET objective='other tab' WHERE id=?", (step.id,))
        return decoded

    monkeypatch.setattr(plan_module, "_row_to_plan", decode_after_other_tab_saves)
    response = await _get_state(_app(store), plan.id)
    assert response.status_code == 200
    assert response.json()["plan"]["current_rev"] == 1
    assert response.json()["steps"][0]["objective"] == "original"
    refreshed = await _get_state(_app(store), plan.id)
    assert refreshed.json()["plan"]["current_rev"] == 2
    assert refreshed.json()["steps"][0]["objective"] == "other tab"
    assert (await _get_state(_app(store, user_id="u_intruder"), plan.id)).status_code == 404
    assert (await _get_state(_app(store), plan.id, ws="other_space")).status_code == 404


async def _patch_steps(app: Any, plan_id: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.patch(
            f"/api/v1/plans/{plan_id}/steps",
            json={
                "workspace_id": "1",
                "user_id": "u_intruder_from_body",
                "steps": [
                    {
                        "id": "ps_1",
                        "agent": "compute",
                        "objective": "refresh the analysis",
                        "deliverable": "memo",
                        "depends_on": [],
                        "needs_corpus": False,
                        "included": True,
                        "origin": "initial",
                    }
                ],
            },
        )


async def _post_amend(app: Any, plan_id: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(
            f"/api/v1/plans/{plan_id}/amend",
            json={
                "workspace_id": "1",
                "user_id": "u_intruder_from_body",
                "instruction": "make the analysis stricter",
            },
        )


@pytest.mark.asyncio
async def test_state_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    plan = SimpleNamespace(id="plan_x", status="blocked", title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1)
    resp = await _get_state(_app(_FakePlanStore(plan, [_step("ps_1")])), "plan_x")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["status"] == "blocked"
    assert body["steps"][0]["id"] == "ps_1"


@pytest.mark.asyncio
async def test_state_exposes_per_criterion_verify_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """未达标的**依据**必须透到 API —— 否则 FE 只能显示一个 ✕，说不出哪条、为什么。

    2026-08-15 116 实测（plan_388aa20aee9e）：三条判据全 unmet，响应里却只有
    判据原文 + 一个空的 outcome 级 `note`。而依据一直都在：`plan_verify` 的 prompt
    要求「unmet 时用一句话说明缺什么」，解析保留、`to_json` 落库——只是在这里被丢了。
    """
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    step = _step("ps_1")
    step.verify_json = json.dumps(
        {
            "verdict": "fail",
            "items": [
                {"text": "含各月营业额数值", "status": "met", "note": ""},
                {"text": "含各月同比变化率", "status": "unmet", "note": "只给了同比绝对值"},
            ],
            "note": "",
        },
        ensure_ascii=False,
    )
    plan = SimpleNamespace(id="plan_x", status="completed", title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1)
    resp = await _get_state(_app(_FakePlanStore(plan, [step])), "plan_x")
    assert resp.status_code == 200
    verify = resp.json()["steps"][0]["verify"]

    assert verify["verdict"] == "fail"
    # Includes per-item status + evidence.
    assert verify["items"] == [
        {"text": "含各月营业额数值", "status": "met", "note": ""},
        {"text": "含各月同比变化率", "status": "unmet", "note": "只给了同比绝对值"},
    ]
    # Backward compatible: old field unchanged (old FE still reads unmet).
    assert verify["unmet"] == ["含各月同比变化率"]


@pytest.mark.asyncio
async def test_state_cross_tenant_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    plan = SimpleNamespace(id="plan_x", status="proposed", title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1)
    resp = await _get_state(_app(_FakePlanStore(plan, []), user_id="u_intruder"), "plan_x")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_state_flag_off_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    plan = SimpleNamespace(id="plan_x", status="proposed", title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1)
    resp = await _get_state(_app(_FakePlanStore(plan, [])), "plan_x")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_edit_steps_uses_current_user_not_body_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    plan = SimpleNamespace(
        id="plan_x", status=PlanStatus.PROPOSED, title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1
    )
    store = _FakePlanStore(plan, [_step("ps_1")])
    resp = await _patch_steps(_app(store), "plan_x")
    assert resp.status_code == 200
    assert store.get_calls[-1] == ("plan_x", "u_dev_alice", "1")
    assert store.bumped is True
    assert store.replaced_steps is not None
    assert store.replaced_steps[0].objective == "refresh the analysis"


@pytest.mark.asyncio
async def test_amend_preview_uses_current_user_not_body_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bowhead_gateway.routes.plans as plans_routes

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    plan = SimpleNamespace(
        id="plan_x", status=PlanStatus.PROPOSED, title="T", workspace_id="1", deliverable_kind="report", canvas_id=None, current_rev=1
    )
    store = _FakePlanStore(plan, [_step("ps_existing")])

    async def fake_amend_plan(*args: Any, **kwargs: Any) -> list[object]:
        return [object()]

    def fake_to_plan_steps(authored: list[object], plan_id: str, **_kw: Any) -> list[PlanStep]:
        return [
            PlanStep(
                id="ps_preview",
                plan_id=plan_id,
                seq=0,
                agent_name="compute",
                objective="stricter analysis",
                deliverable="memo",
            )
        ]

    async def fake_team_router(
        app_state: Any,
        team_id: Any,
        **_kwargs: Any,
    ) -> Any:
        return SimpleNamespace()  # Test verifies only identity semantics; router never actually invoked.

    monkeypatch.setattr(plans_routes, "team_router_or_409", fake_team_router)
    monkeypatch.setattr(plans_routes, "amend_plan", fake_amend_plan)
    monkeypatch.setattr(plans_routes, "to_plan_steps", fake_to_plan_steps)

    resp = await _post_amend(_app(store), "plan_x")
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview"] is True
    assert body["steps"][0]["id"] == "ps_preview"
    assert store.get_calls[-1] == ("plan_x", "u_dev_alice", "1")
