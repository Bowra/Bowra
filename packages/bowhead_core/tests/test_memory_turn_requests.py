"""Per-turn explicit memory requests come from structured writer outcomes, never from text."""

from __future__ import annotations

import ast
import inspect
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.memory_auto_capture import (
    ACTION_NEEDS_CONFIRMATION,
    ACTION_REFUSED,
    ACTION_SETTING_SUGGESTED,
    ACTION_SKIPPED,
    ACTION_WRITTEN,
    REASON_USER_FACING,
    CaptureOutcome,
    capture_modification_request,
)
from bowhead_core.memory_tools import _remember
from bowhead_core.memory_turn_requests import (
    DISPLAYABLE_REASONS,
    ENQUEUE_CREATED,
    ENQUEUE_DUPLICATE_PENDING,
    MemoryTurnRequest,
    MemoryTurnRequestStore,
    from_capture_outcome,
    from_nl_intent_enqueue,
    from_remember_result,
    public_reason,
    select_explicit_request,
)
from bowhead_core.memory_write_gate import (
    REASON_CONTAINS_SECRET,
    REASON_LANGUAGE_IS_A_SETTING,
    REASON_NO_ATTRIBUTION,
    REASON_ONE_OFF,
)
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.types import TurnRequest
from bowhead_core.user_memory import UserMemoryStore


@pytest.fixture(scope="module")
def migrated_core(tmp_path_factory) -> Path:
    from bowhead_cli.migrate import upgrade

    directory = tmp_path_factory.mktemp("memory-turn-requests")
    upgrade(f"sqlite+aiosqlite:///{directory / 'bowhead.db'}", target="core")
    return directory / "bowhead.db"


@pytest.fixture
async def memory(tmp_path, migrated_core) -> UserMemoryStore:
    path = tmp_path / "bowhead.db"
    shutil.copy(migrated_core, path)
    store = UserMemoryStore(path)
    await store.initialize()
    return store


async def _rows(memory: UserMemoryStore, turn_id: str = "t1") -> list[MemoryTurnRequest]:
    return await MemoryTurnRequestStore(memory.db_path).list_for_turn(user_id="u1", turn_id=turn_id)


def _ctx(memory: UserMemoryStore) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws",
        member_uid="m",
        user_id="u1",
        turn_id="t1",
        datarc=None,
        memory_notes_store=None,
        user_memory_store=memory,
    )


# ─── remember_tool ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_a_remember_call_refused_by_the_gate_records_not_remembered_with_its_reason(memory, monkeypatch):
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    result = await _remember({"kind": "preference", "key": "jiangsu_only", "text": "这轮先只看江苏"}, _ctx(memory))
    assert result["ok"] is False
    assert await _rows(memory) == [MemoryTurnRequest("remember_tool", "not_remembered", reason=REASON_ONE_OFF)]


@pytest.mark.integration
async def test_a_stored_remember_call_records_remembered_with_the_entry(memory, monkeypatch):
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    result = await _remember({"kind": "preference", "key": "ranking_chart", "text": "排名图用横向条形图"}, _ctx(memory))
    assert result["ok"] is True
    assert await _rows(memory) == [MemoryTurnRequest("remember_tool", "remembered", entry_id=result["id"])]


@pytest.mark.integration
async def test_a_retried_remember_call_replaces_the_earlier_refusal(memory, monkeypatch):
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    await _remember({"kind": "preference", "text": "排名图用横向条形图"}, _ctx(memory))
    assert (await _rows(memory))[0].reason == "missing_key"
    ok = await _remember({"kind": "preference", "key": "ranking_chart", "text": "排名图用横向条形图"}, _ctx(memory))
    assert await _rows(memory) == [MemoryTurnRequest("remember_tool", "remembered", entry_id=ok["id"])]


async def test_a_missing_ledger_never_fails_the_remember_call(tmp_path, monkeypatch):
    """A receipt write failure is logged; the tool result the Lead relies on is unchanged."""
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    store = UserMemoryStore(tmp_path / "unmigrated.db")
    await store.initialize()
    result = await _remember({"kind": "preference", "key": "ranking_chart", "text": "排名图用横向条形图"}, _ctx(store))
    assert result["ok"] is True


# ─── capture ────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_a_written_capture_outcome_records_remembered(memory, monkeypatch):
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")

    async def drafter(_text: str) -> str:
        return "排名图默认用横向条形图"

    outcome = await capture_modification_request(
        user_text="记一下：以后排名类图表都用横向条形图",
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=memory,
        drafter=drafter,
        input_is_tainted=False,
        user_paused=False,
        rate_limiter=SimpleNamespace(take=lambda _uid: True),
    )
    assert outcome.action == ACTION_WRITTEN
    request = from_capture_outcome(outcome)
    assert request == MemoryTurnRequest("capture", "remembered", entry_id=outcome.entry_id)
    await MemoryTurnRequestStore(memory.db_path).record(user_id="u1", turn_id="t1", request=request)
    assert await _rows(memory) == [request]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CaptureOutcome(ACTION_SKIPPED, reason="flag_off"), None),
        (CaptureOutcome(ACTION_SKIPPED, reason="learning_paused"), None),
        (CaptureOutcome(ACTION_SKIPPED, reason="trigger_none"), None),
        (
            CaptureOutcome(ACTION_SETTING_SUGGESTED, reason="trigger_language_setting", suggested_setting="output_language"),
            MemoryTurnRequest("language_setting", "not_remembered", reason="trigger_language_setting"),
        ),
        (
            CaptureOutcome(ACTION_NEEDS_CONFIRMATION, reason="tainted_input", entry_id="cand_1"),
            MemoryTurnRequest("capture", "candidate", candidate_id="cand_1"),
        ),
        (
            CaptureOutcome(ACTION_NEEDS_CONFIRMATION, reason="tainted_input"),
            MemoryTurnRequest("capture", "candidate", reason=ENQUEUE_DUPLICATE_PENDING),
        ),
        (CaptureOutcome(ACTION_SKIPPED, reason="rate_limited"), MemoryTurnRequest("capture", "not_remembered", reason="rate_limited")),
        (CaptureOutcome(ACTION_REFUSED, reason="draft_too_long"), MemoryTurnRequest("capture", "not_remembered", reason="draft_too_long")),
        (CaptureOutcome(ACTION_SKIPPED, reason="draft_failed"), MemoryTurnRequest("capture", "not_remembered", reason="draft_failed")),
    ],
)
def test_capture_outcomes_map_to_requests_only_for_real_requests(outcome, expected):
    assert from_capture_outcome(outcome) == expected


@pytest.mark.unit
def test_the_capture_wiring_records_the_structured_outcome():
    """The capture closure must hand `CaptureOutcome` to the receipt writer, not any text."""
    from bowhead_core import turn_runner

    source = inspect.getsource(turn_runner)
    tree = ast.parse(source)
    closure = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_modification_request")
    calls = [n for n in ast.walk(closure) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "from_capture_outcome"]
    assert len(calls) == 1
    assert [a.id for a in calls[0].args if isinstance(a, ast.Name)] == ["_outcome"]


# ─── nl_intent ──────────────────────────────────────────────────────────


class _Intent:
    detected = True
    confidence = 0.95
    kind = "preference"
    proposed_content = "我们财年7月开始"


class _Classifier:
    async def classify(self, _text, **_kw):
        return _Intent()


class _Candidates:
    def __init__(self, outcome: str):
        self.outcome = outcome

    async def enqueue_if_new_with_outcome(self, **_kw):
        if self.outcome == ENQUEUE_CREATED:
            return SimpleNamespace(id="cand_nl"), self.outcome
        return None, self.outcome


def _orch(memory, outcome: str):
    return SimpleNamespace(
        nl_memory_intent_classifier=_Classifier(),
        memory_candidate_store=_Candidates(outcome),
        user_memory_store=memory,
    )


def _request() -> TurnRequest:
    return TurnRequest(session_id="s", workspace_id="w", user_id="u1", member_uid="m", text="顺便说下我们公司财年7月开始", turn_id="t1")


@pytest.mark.integration
async def test_a_suppressed_nl_intent_enqueue_records_not_remembered(memory):
    from bowhead_core.turn_runner import maybe_capture_nl_intent_post_turn

    capture = await maybe_capture_nl_intent_post_turn(_orch(memory, "suppressed"), _request())
    assert capture is not None and capture.enqueue_outcome == "suppressed"
    assert await _rows(memory) == [MemoryTurnRequest("nl_intent", "not_remembered", reason="suppressed")]


@pytest.mark.integration
async def test_a_created_nl_intent_candidate_records_candidate(memory):
    from bowhead_core.turn_runner import maybe_capture_nl_intent_post_turn

    await maybe_capture_nl_intent_post_turn(_orch(memory, ENQUEUE_CREATED), _request())
    assert await _rows(memory) == [MemoryTurnRequest("nl_intent", "candidate", candidate_id="cand_nl")]


@pytest.mark.integration
async def test_an_nl_intent_without_a_recorded_enqueue_outcome_records_nothing(memory):
    from bowhead_core.turn_runner import maybe_capture_nl_intent_post_turn

    class _Down:
        async def enqueue_if_new_with_outcome(self, **_kw):
            raise RuntimeError("db down")

    orch = _orch(memory, ENQUEUE_CREATED)
    orch.memory_candidate_store = _Down()
    assert await maybe_capture_nl_intent_post_turn(orch, _request()) is None
    assert await _rows(memory) == []


# ─── selection and public reasons ───────────────────────────────────────


@pytest.mark.unit
def test_the_most_specific_source_wins():
    rows = [
        MemoryTurnRequest("nl_intent", "candidate"),
        MemoryTurnRequest("language_setting", "not_remembered"),
        MemoryTurnRequest("capture", "remembered"),
    ]
    assert select_explicit_request(rows) == MemoryTurnRequest("capture", "remembered")
    assert select_explicit_request([*rows, MemoryTurnRequest("remember_tool", "remembered")]).source == "remember_tool"
    # A refusal never outranks a landed outcome, even from a more specific source.
    assert select_explicit_request([*rows, MemoryTurnRequest("remember_tool", "not_remembered")]).source == "capture"
    assert select_explicit_request([]) is None


@pytest.mark.unit
def test_remember_results_map_without_reading_message_text():
    assert from_remember_result({"ok": True, "id": "mem_1", "message": "没有记住"}) == MemoryTurnRequest(
        "remember_tool", "remembered", entry_id="mem_1"
    )
    assert from_remember_result({"ok": False, "error": "locked", "message": "已记住"}).outcome == "not_remembered"
    assert from_remember_result("garbled") == MemoryTurnRequest("remember_tool", "not_remembered")


@pytest.mark.unit
def test_nl_intent_outcomes_map_to_finite_requests():
    assert from_nl_intent_enqueue(None, ENQUEUE_DUPLICATE_PENDING) == MemoryTurnRequest("nl_intent", "candidate", reason=ENQUEUE_DUPLICATE_PENDING)
    assert from_nl_intent_enqueue(None, "gate_refused") == MemoryTurnRequest("nl_intent", "not_remembered", reason="gate_refused")


@pytest.mark.unit
def test_only_user_facing_reasons_are_displayable():
    """Shown reasons are the write-gate codes, `locked`, and capture refusals marked user-facing."""
    gate = {REASON_LANGUAGE_IS_A_SETTING, REASON_ONE_OFF, REASON_NO_ATTRIBUTION, REASON_CONTAINS_SECRET}
    capture = {reason for reason, facing in REASON_USER_FACING.items() if facing} - {"tainted_input"}
    assert gate | capture | {"locked", "entry_locked"} == DISPLAYABLE_REASONS
    assert public_reason("draft_failed") is None
    assert public_reason("suppressed") is None
    assert public_reason("rate_limited") == "rate_limited"


@pytest.mark.unit
def test_enqueue_codes_match_the_candidate_store():
    from bowhead_memory import candidates

    assert (ENQUEUE_CREATED, ENQUEUE_DUPLICATE_PENDING) == (candidates.ENQUEUE_CREATED, candidates.ENQUEUE_DUPLICATE_PENDING)


# ─── review follow-ups ──────────────────────────────────────────────────


@pytest.mark.unit
def test_a_landed_outcome_from_another_source_beats_a_refusal():
    """Capture skipped the sentence but nl_intent turned it into a card: no "not remembered" receipt."""
    rows = [
        MemoryTurnRequest("capture", "not_remembered", reason="draft_says_not_lasting"),
        MemoryTurnRequest("nl_intent", "candidate", candidate_id="cand_1"),
    ]
    assert select_explicit_request(rows) == MemoryTurnRequest("nl_intent", "candidate", candidate_id="cand_1")
    duplicate = [rows[0], MemoryTurnRequest("nl_intent", "candidate", reason=ENQUEUE_DUPLICATE_PENDING)]
    assert select_explicit_request(duplicate).outcome == "candidate"
    # Among refusals only, the most specific source still wins.
    refusals = [rows[0], MemoryTurnRequest("remember_tool", "not_remembered", reason="one_off")]
    assert select_explicit_request(refusals).source == "remember_tool"


@pytest.mark.integration
async def test_remembered_is_sticky_against_a_later_refusal_from_the_same_source(memory, monkeypatch):
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    ok = await _remember({"kind": "preference", "key": "ranking_chart", "text": "排名图用横向条形图"}, _ctx(memory))
    refused = await _remember({"kind": "preference", "key": "jiangsu_only", "text": "这轮先只看江苏"}, _ctx(memory))
    assert ok["ok"] is True and refused["ok"] is False
    assert await _rows(memory) == [MemoryTurnRequest("remember_tool", "remembered", entry_id=ok["id"])]


@pytest.mark.integration
async def test_a_locked_canonical_entry_reports_entry_locked_not_the_editing_lock(memory, monkeypatch):
    monkeypatch.setattr("bowhead_core.memory_readiness.memory_storage_ready", lambda: True)
    await memory.upsert(user_id="u1", kind="preference", key="ranking_chart", value="原有锁定条目", source="admin", locked=True)
    result = await _remember({"kind": "preference", "key": "ranking_chart", "text": "排名图用横向条形图"}, _ctx(memory))
    assert result["ok"] is False and result["error"] == "entry_locked"
    assert await _rows(memory) == [MemoryTurnRequest("remember_tool", "not_remembered", reason="entry_locked")]
    assert public_reason("entry_locked") == "entry_locked"
