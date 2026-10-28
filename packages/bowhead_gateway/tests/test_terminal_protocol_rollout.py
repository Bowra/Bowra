from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import bowhead_gateway.terminal_protocol_rollout as terminal_rollout
import pytest
from bowhead_core.terminal_protocol import (
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
)
from bowhead_core.terminal_protocol_store import (
    SessionActiveClaimSummary,
    SessionProtocolFacts,
)
from bowhead_core.terminal_runtime_factory import TerminalWriterSnapshot
from bowhead_gateway.terminal_protocol_rollout import (
    DURABLE_CHAT_EXECUTION_FLAG,
    ChatExecutionWriteProtocol,
    DurableChatExecutionMode,
    PersistedChatExecutionProtocol,
    durable_chat_execution_config,
    fresh_terminal_v1_admission_enabled,
    parse_durable_chat_execution_config,
    resolve_chat_execution_admission,
    resolve_persisted_chat_execution_protocol,
    terminal_v1_attachment_refs_wire_version,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DurableChatExecutionMode.OFF),
        ("off", DurableChatExecutionMode.OFF),
        ("shadow", DurableChatExecutionMode.SHADOW),
        ("rollout", DurableChatExecutionMode.ROLLOUT),
        ("required", DurableChatExecutionMode.REQUIRED),
        ("", DurableChatExecutionMode.INVALID),
        ("on", DurableChatExecutionMode.INVALID),
        ("ROLLOUT", DurableChatExecutionMode.INVALID),
        (" rollout", DurableChatExecutionMode.INVALID),
    ],
)
def test_durable_chat_execution_mode_is_default_off_and_strict(
    raw: str | None,
    expected: DurableChatExecutionMode,
) -> None:
    config = parse_durable_chat_execution_config(
        raw,
        team_ids_raw="team_b, team_a,team_b",
        percentage_raw="25",
    )

    assert config.mode is expected
    if expected in {DurableChatExecutionMode.SHADOW, DurableChatExecutionMode.ROLLOUT}:
        assert config.team_ids == frozenset({"team_a", "team_b"})
        assert config.percentage == 25
    else:
        assert config.team_ids == frozenset()
        assert config.percentage == 0


@pytest.mark.unit
def test_typed_attachment_wire_is_always_advertised() -> None:
    assert terminal_v1_attachment_refs_wire_version() == 1


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["-1", "101", "1.5", "abc", " 5"])
def test_rollout_percentage_is_strict(raw: str) -> None:
    config = parse_durable_chat_execution_config(
        "rollout",
        team_ids_raw="",
        percentage_raw=raw,
    )

    assert config.mode is DurableChatExecutionMode.INVALID


@pytest.mark.unit
def test_config_generation_is_stable_and_does_not_expose_allowlist() -> None:
    first = parse_durable_chat_execution_config(
        "rollout",
        team_ids_raw="team_b,team_a",
        percentage_raw="10",
    )
    second = parse_durable_chat_execution_config(
        "rollout",
        team_ids_raw="team_a,team_b",
        percentage_raw="10",
    )

    assert first.config_generation == second.config_generation
    assert "team_a" not in first.config_generation
    assert "team_b" not in first.config_generation


@pytest.mark.unit
def test_retired_writer_flag_cannot_enable_fresh_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "1")
    monkeypatch.delenv(DURABLE_CHAT_EXECUTION_FLAG, raising=False)

    assert durable_chat_execution_config().mode is DurableChatExecutionMode.OFF
    assert fresh_terminal_v1_admission_enabled() is False

    monkeypatch.setenv(DURABLE_CHAT_EXECUTION_FLAG, "rollout")
    assert fresh_terminal_v1_admission_enabled() is False

    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )
    assert fresh_terminal_v1_admission_enabled() is True


@pytest.mark.unit
def test_never_v1_session_obeys_mode_and_stable_team_cohort() -> None:
    facts = SessionProtocolFacts(has_v1_history=False)
    off = parse_durable_chat_execution_config("off", "team_a", "100")
    shadow = parse_durable_chat_execution_config("shadow", "team_a", "0")
    rollout = parse_durable_chat_execution_config("rollout", "team_a", "0")

    off_decision = resolve_chat_execution_admission(
        config=off,
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )
    shadow_decision = resolve_chat_execution_admission(
        config=shadow,
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )
    rollout_decision = resolve_chat_execution_admission(
        config=rollout,
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )

    assert off_decision.write_protocol is ChatExecutionWriteProtocol.LEGACY_V0
    assert off_decision.cohort_eligible is False
    assert shadow_decision.write_protocol is ChatExecutionWriteProtocol.LEGACY_V0
    assert shadow_decision.cohort_eligible is True
    assert rollout_decision.write_protocol is ChatExecutionWriteProtocol.TERMINAL_V1
    assert rollout_decision.cohort_eligible is True


@pytest.mark.unit
def test_rollout_old_client_stays_legacy_but_stale_action_fails_closed() -> None:
    config = parse_durable_chat_execution_config("rollout", "team_a", "0")
    facts = SessionProtocolFacts(has_v1_history=False)

    old_client = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=facts,
        client_capable=False,
        action_identity_complete=False,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )
    stale_action = resolve_chat_execution_admission(
        config=parse_durable_chat_execution_config("off", "", "0"),
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=True,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )
    incomplete_action_client = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )

    assert old_client.write_protocol is ChatExecutionWriteProtocol.LEGACY_V0
    assert incomplete_action_client.write_protocol is ChatExecutionWriteProtocol.LEGACY_V0
    assert stale_action.write_protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED


@pytest.mark.unit
def test_required_mode_never_falls_back_when_capability_or_writer_is_missing() -> None:
    config = parse_durable_chat_execution_config("required", "ignored", "not-read")
    facts = SessionProtocolFacts(has_v1_history=False)

    refresh = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=facts,
        client_capable=False,
        action_identity_complete=False,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )
    unavailable = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=facts,
        client_capable=True,
        action_identity_complete=True,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=False,
    )

    assert refresh.write_protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED
    assert unavailable.write_protocol is ChatExecutionWriteProtocol.UNAVAILABLE


@pytest.mark.unit
def test_existing_v1_session_is_sticky_across_mode_changes() -> None:
    config = parse_durable_chat_execution_config("off", "", "0")
    idle = SessionProtocolFacts(has_v1_history=True)
    active = SessionProtocolFacts(
        has_v1_history=True,
        v1_active_claim=SessionActiveClaimSummary(
            turn_id="turn_1",
            protocol_version=1,
            generation=2,
            claim_epoch=3,
        ),
    )

    idle_ready = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=idle,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=True,
        fresh_writer_ready=False,
    )
    active_ready = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=active,
        client_capable=True,
        action_identity_complete=True,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=False,
    )
    old_client = resolve_chat_execution_admission(
        config=config,
        team_id="team_a",
        session_facts=active,
        client_capable=False,
        action_identity_complete=False,
        require_action_identity=True,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )

    assert idle_ready.write_protocol is ChatExecutionWriteProtocol.TERMINAL_V1
    assert active_ready.write_protocol is ChatExecutionWriteProtocol.TERMINAL_V1
    assert old_client.write_protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED


@pytest.mark.unit
def test_invalid_config_is_unavailable() -> None:
    decision = resolve_chat_execution_admission(
        config=parse_durable_chat_execution_config("enabled", "", "0"),
        team_id="team_a",
        session_facts=None,
        client_capable=True,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=True,
        fresh_writer_ready=True,
    )

    assert decision.write_protocol is ChatExecutionWriteProtocol.UNAVAILABLE


@pytest.mark.unit
def test_persisted_protocol_resolver_is_mode_independent_and_fail_closed() -> None:
    assert resolve_persisted_chat_execution_protocol(None) is PersistedChatExecutionProtocol.LEGACY_V0
    assert resolve_persisted_chat_execution_protocol(1) is PersistedChatExecutionProtocol.TERMINAL_V1
    with pytest.raises(TerminalProtocolFutureVersionError):
        resolve_persisted_chat_execution_protocol(2)
    with pytest.raises(TerminalProtocolMalformedError):
        resolve_persisted_chat_execution_protocol("1")


@pytest.mark.unit
def test_durable_chat_flag_has_one_runtime_source_of_truth() -> None:
    source_root = Path(__file__).parents[1] / "src" / "bowhead_gateway"
    hits = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if DURABLE_CHAT_EXECUTION_FLAG in path.read_text(encoding="utf-8")
    )

    assert hits == ["terminal_protocol_rollout.py"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_bootstrap_keeps_persisted_v1_sticky_when_mode_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.routes.sessions import (
        _session_chat_execution_bootstrap,
    )

    monkeypatch.setenv(DURABLE_CHAT_EXECUTION_FLAG, "off")
    facts = SessionProtocolFacts(has_v1_history=True)

    class ProtocolStore:
        async def read_session_protocol_facts(self, **_kwargs):
            return facts

    class UserStore:
        async def get_workspace_team_id(self, _workspace_id: str) -> str:
            return "team_1"

    class Writer:
        ready = False
        fresh_admission_ready = False

        async def ensure_writer_ready(self) -> bool:
            self.ready = True
            return True

        def writer_snapshot(self) -> TerminalWriterSnapshot:
            return TerminalWriterSnapshot(
                instance_id="gateway_1",
                lease_epoch=1,
                admission_epoch=2,
                rollout_config_generation="config_1",
                recovery_only=True,
            )

    writer = Writer()
    state = SimpleNamespace(
        terminal_protocol_store=ProtocolStore(),
        terminal_writer_service=writer,
        user_store=UserStore(),
    )
    session = SimpleNamespace(
        id="session_1",
        user_id="user_1",
        workspace_id="workspace_1",
    )

    capable = await _session_chat_execution_bootstrap(
        SimpleNamespace(
            app=SimpleNamespace(state=state),
            headers={"x-bowhead-terminal-protocol-capability": "1"},
        ),
        session,
    )
    stale = await _session_chat_execution_bootstrap(
        SimpleNamespace(
            app=SimpleNamespace(state=state),
            headers={},
        ),
        session,
    )

    assert capable.write_protocol == "terminal_v1"
    assert capable.attachment_refs_wire_version == 1
    assert stale.write_protocol == "refresh_required"
