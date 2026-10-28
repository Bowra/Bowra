"""Real core admission and the gateway history reader share the automatic ledger."""

from types import SimpleNamespace

import pytest
from bowhead_gateway.routes import turns
from fastapi import HTTPException
from test_reference_automatic_admission import prepare_auto
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True),
]


async def test_automatic_history_preserves_origin_and_enforces_real_space_access(receiver_case, monkeypatch):
    fixture, case = receiver_case, receiver_case.case
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    admission = await prepare_auto(fixture)
    await case.store.finalize_started_chat_submission(**case.args)
    user = await case.fixture.users.get_user(fixture.scope.user_id)

    async def current_user(request):
        return user

    monkeypatch.setattr(turns, "_current_user", current_user)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=case.store,
                user_store=case.fixture.users,
                settings=SimpleNamespace(auth_mode="prod", dev_default_user="unused"),
            )
        ),
        headers={},
        cookies={},
    )
    detail = await turns.get_agent_binding_detail("t_reference", request)
    assert detail.root_binding.binding_source == "lead"
    assert detail.root_binding.agent_id == admission.automatic_receiver.id
    assert detail.root_binding.agent_revision_id == admission.automatic_receiver.version
    assert len(detail.executions) == 1 and detail.executions[0].dispatch_source == "lead"
    # The established history policy still governs this new origin.
    import sqlite3

    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (user.id,))
    with pytest.raises(HTTPException) as denied:
        await turns.get_agent_binding_detail("t_reference", request)
    assert denied.value.status_code == 404
