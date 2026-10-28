from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime

from bowhead_gateway import agent_eval_runtime


@pytest.mark.asyncio
async def test_critical_regression_proposes_restore(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE agent_eval_run(id TEXT,team_id TEXT,agent_id TEXT,release_id TEXT)")
        db.execute("INSERT INTO agent_eval_run VALUES('aer_1','team_1','osa_1','arl_failed')")
        db.commit()
    proposals: list[dict] = []

    class _Proposals:
        async def create_restore_proposal(self, **kwargs):
            proposals.append(kwargs)
            return SimpleNamespace(id="acp_restore")

    monkeypatch.setattr(agent_eval_runtime, "AgentChangeProposalStore", lambda _db_path: _Proposals())
    runtime = AgentEvalRuntime(db_path=str(db_path), state=SimpleNamespace())
    notification = SimpleNamespace(
        agent_id="osa_1",
        recipient_user_id="owner_1",
        run_id="aer_1",
        run_status="failed",
        team_id="team_1",
    )

    await runtime._propose_restore_for_notification(notification)

    assert proposals == [
        {
            "team_id": "team_1",
            "agent_id": "osa_1",
            "failed_release_id": "arl_failed",
            "effective_owner_user_id": "owner_1",
            "regression_run_id": "aer_1",
            "reason_code": "regression_failed",
        }
    ]
