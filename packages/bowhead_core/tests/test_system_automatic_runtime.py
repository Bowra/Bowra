"""The actual registered system profile can receive a Skill-only primary."""

from types import SimpleNamespace

import pytest
from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
from cryptography.fernet import Fernet
from test_reference_automatic_runtime import test_actual_runtime_injects_methods_and_records_the_same_primary as _exercise
from test_reference_receiver_candidates import discovery
from test_reference_receiver_choice import chosen
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case
from test_system_reference_baseline import configure

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_optional_unused_invoke"], indirect=True),
]


async def test_registered_system_primary_runs_from_automatic_source(receiver_case, monkeypatch):
    fixture, start = receiver_case, receiver_case.case
    read, kwargs, _ = await discovery(fixture)
    await configure(fixture, monkeypatch)
    peers = await _gather_lead_peers(
        registry=AgentRegistry.bootstrap(),
        workspace_id=fixture.scope.workspace_id,
        custom_store=None,
        user_id=fixture.scope.user_id,
        team_id=fixture.scope.team_id,
    )
    index = await build_system_agent_reference_metadata(start.fixture.db_path, start.runner)
    candidates = await read(lead_peers=[p for p in peers if p.agent_id == "system:compute"], system_agent_index=index)
    admission = await chosen(fixture, candidates, kwargs["input_document"])
    start.args.update(reference_start=admission, user_text=admission.input_document.display_text())
    await start.store.finalize_started_chat_submission(**start.args)
    scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    args = {
        "sources": fixture.sources,
        "cipher": cipher,
        "scope": scope,
        "turn_id": "t_reference",
        "owner_id": "auto-worker",
        "expected_generation": 1,
    }
    case = SimpleNamespace(fixture=fixture, path=start.fixture.db_path, scope=scope, cipher=cipher, args=args, admission=admission)
    assert admission.automatic_receiver.source_kind == "system"
    await _exercise(case)
