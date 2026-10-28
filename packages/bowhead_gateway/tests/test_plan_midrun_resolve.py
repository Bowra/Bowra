"""批2 S3: plan 卡 resolve 统一分派 helper 测试 (batch2-hitl-design §1-D2).

三值分派表驱动 + 读失败兜底=ack(review P1-5: reapprove 最坏报错+误复活)。
三处分派点(主/重放/recovery)共用同一 helper——wiring 由 grep 断言钉住。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_gateway.routes.clarifications import _plan_card_action


class _FakePlanStore:
    def __init__(self, status: str | None = "running", raises: bool = False) -> None:
        self._status, self._raises = status, raises
        self.calls: list[tuple[str, str, str]] = []

    async def get_plan(self, plan_id: str, *, user_id: str, workspace_id: str):
        self.calls.append((plan_id, user_id, workspace_id))
        if self._raises:
            raise RuntimeError("db down")
        if self._status is None:
            return None
        return SimpleNamespace(status=self._status)


def _req(store) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(plan_store=store)))


@pytest.mark.asyncio
async def test_non_plan_card_returns_none() -> None:
    assert await _plan_card_action(_req(_FakePlanStore()), rc=None, user_id="u", workspace_id="w") is None
    assert await _plan_card_action(_req(_FakePlanStore()), rc={"user_text": "x"}, user_id="u", workspace_id="w") is None


@pytest.mark.asyncio
async def test_preflight_reapprove_shape_unchanged() -> None:
    got = await _plan_card_action(_req(_FakePlanStore()), rc={"plan_reapprove_plan_id": "p1"}, user_id="u", workspace_id="w")
    assert got == ("plan_reapprove", "p1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", ("plan_midrun_ack", None)),  # Answer while running → ack, driver picks up.
        ("blocked", ("plan_reapprove", "p1")),  # Answer after convergence → automatic reapproval and resume.
        ("cancelled", ("plan_midrun_ack", None)),  # Never revive a just-cancelled plan.
        ("completed", ("plan_midrun_ack", None)),
        ("failed", ("plan_midrun_ack", None)),
        (None, ("plan_midrun_ack", None)),  # Not found.
    ],
)
async def test_midrun_three_value_dispatch(status, expected) -> None:
    store = _FakePlanStore(status)
    got = await _plan_card_action(_req(store), rc={"plan_midrun_plan_id": "p1"}, user_id="u", workspace_id="w")
    assert got == expected
    if status is not None:
        assert store.calls == [("p1", "u", "w")]  # Tenant parameters passed through.


@pytest.mark.asyncio
async def test_midrun_store_error_falls_back_to_ack() -> None:
    """读失败兜底=ack(最坏多点一次), 绝不 reapprove(最坏报错+误复活)."""
    got = await _plan_card_action(_req(_FakePlanStore(raises=True)), rc={"plan_midrun_plan_id": "p1"}, user_id="u", workspace_id="w")
    assert got == ("plan_midrun_ack", None)


@pytest.mark.asyncio
async def test_midrun_no_store_falls_back_to_ack() -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(plan_store=None)))
    got = await _plan_card_action(req, rc={"plan_midrun_plan_id": "p1"}, user_id="u", workspace_id="w")
    assert got == ("plan_midrun_ack", None)


def test_wiring_all_three_dispatch_sites_use_helper() -> None:
    """review P0-2 钉子: 主/重放/recovery 三处分派点必须走 _plan_card_action——
    漏任何一处=双击从幂等成功变 409。就绪门与 resume_turn 守卫必须豁免/拦截
    plan_midrun 形状(review P0-1)。grep 断言比 route 级 harness 便宜且不掉。"""
    src = (Path(__file__).parent.parent / "src/bowhead_gateway/routes/clarifications.py").read_text()
    turn_service_src = (Path(__file__).parent.parent / "src/bowhead_gateway/turn_service.py").read_text()
    assert src.count("await _plan_card_action(") == 3, "三处分派点必须全走统一 helper"
    # Readiness exemption (P0-1): without it mid-run resolve always returns 409.
    assert 'not resume_context.get("plan_midrun_plan_id")' in turn_service_src
    # resume_turn explicit interception (defense for old FE).
    assert 'rc.get("plan_reapprove_plan_id") or rc.get("plan_midrun_plan_id")' in src
    # Batch 3 (R1-F11): expired mid-run cards have a dedicated ack branch (behavior asserted in
    # test_clarifications_plan_reapprove.test_expired_midrun_card_resolves_to_ack)
    assert 'pre.status == "expired"' in src and '"plan_midrun_ack"' in src
