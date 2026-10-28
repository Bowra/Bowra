"""The model clock is the single source of "today" for prompts and tool advice.

A turn admitted with a valid viewer zone states that zone's date everywhere
(Lead/subagent ``<current_date>``, Router ``<runtime_context>``, BI advice), and
every resume/continuation of the turn restores the same zone from the admitted
language snapshot. Anything without a viewer zone uses the deployment clock.

Zones +14h and -12h are used because their dates always differ from each other
(26 hours apart), so a test cannot pass by coincidence of calendars.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from bowhead_core.documents.continuation import DocumentLanguageSnapshot
from bowhead_core.language_context import RESUME_LANGUAGE_KEY, LanguageContext, language_context_snapshot
from bowhead_core.model_clock import (
    model_now,
    model_today,
    request_time_zone_name,
    turn_clock_scope,
    turn_time_zone_name,
    viewer_clock_scope,
)
from bowhead_core.subagent import _current_date_anchor
from bowhead_core.turn_runner import TurnRunner
from bowhead_core.types import TurnRequest
from bowhead_router.router import _today_banner

from bowhead_core import turn_runner as turn_runner_module

pytestmark = pytest.mark.unit

EAST = "Pacific/Kiritimati"  # UTC+14
WEST = "Etc/GMT+12"  # UTC-12


def _date_in(zone: str) -> str:
    return datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")


def _deploy_date(offset_h: float) -> str:
    return datetime.now(timezone(timedelta(hours=offset_h))).strftime("%Y-%m-%d")


def _anchor_date() -> str:
    return _current_date_anchor().split("<current_date>", 1)[1].split("</current_date>", 1)[0]


def _banner_date() -> str:
    return _today_banner().split("today: ", 1)[1][:10]


# ── helper ────────────────────────────────────────────────────────────────────


def test_viewer_zone_sets_the_model_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "0")
    with viewer_clock_scope(EAST):
        assert model_today().isoformat() == _date_in(EAST)
        assert model_now().utcoffset() == timedelta(hours=14)
        assert turn_time_zone_name() == EAST
    with viewer_clock_scope(WEST):
        assert model_today().isoformat() == _date_in(WEST)


@pytest.mark.parametrize("name", [None, "", "Mars/Base", "America", "UTC+8", "posixrules", 8])
def test_missing_or_invalid_zone_falls_back_to_the_deploy_clock(monkeypatch: pytest.MonkeyPatch, name: object) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "-12")
    with viewer_clock_scope(name):
        assert model_today().isoformat() == _deploy_date(-12)
        assert turn_time_zone_name() == ""


def test_outside_any_turn_the_deploy_clock_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_TZ_OFFSET_HOURS", raising=False)
    assert model_today().isoformat() == _deploy_date(8)
    assert turn_time_zone_name() == ""


def test_scope_is_restored_after_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    with viewer_clock_scope(WEST):
        pass
    assert model_today().isoformat() == _deploy_date(14)


def test_prompt_anchor_banner_and_helper_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Router banner used to be UTC while the anchor used +8: one turn, two todays."""
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    assert _anchor_date() == _banner_date() == model_today().isoformat() == _deploy_date(14)
    with viewer_clock_scope(WEST):
        assert _anchor_date() == _banner_date() == _date_in(WEST)


# ── language snapshot carries the zone ───────────────────────────────────────


def test_snapshot_without_zone_keeps_its_original_shape() -> None:
    ctx = LanguageContext()
    assert "time_zone" not in ctx.to_dict()
    assert LanguageContext.from_dict(ctx.to_dict()) == ctx


def test_snapshot_with_zone_round_trips_exactly() -> None:
    ctx = LanguageContext(ui_language="de-DE").with_time_zone(EAST)
    assert ctx.to_dict()["time_zone"] == EAST
    restored = LanguageContext.from_dict(ctx.to_dict())
    assert restored == ctx
    assert restored.to_dict() == ctx.to_dict()  # terminal envelope round-trip check


def test_unknown_zone_is_dropped_at_admission_and_on_read() -> None:
    assert LanguageContext().with_time_zone("Mars/Base").time_zone == ""
    assert LanguageContext().with_time_zone(None).time_zone == ""
    assert LanguageContext.from_dict({"time_zone": "../etc/passwd"}).time_zone == ""
    with pytest.raises(ValueError):
        LanguageContext(time_zone="Mars/Base")


def test_document_snapshot_keeps_old_digests_and_carries_the_zone() -> None:
    old = DocumentLanguageSnapshot.model_validate(LanguageContext().to_dict())
    assert "time_zone" not in old.model_dump()
    with_zone = DocumentLanguageSnapshot.model_validate(LanguageContext().with_time_zone(WEST).to_dict())
    assert with_zone.model_dump()["time_zone"] == WEST
    assert with_zone.language_context().time_zone == WEST


# ── turn path: live, resumed and subagent executions share one date ─────────


def _request(language_context: LanguageContext | None) -> TurnRequest:
    return TurnRequest(session_id="s_1", workspace_id="ws_1", user_id="u_1", member_uid="", text="今年销量", language_context=language_context)


async def _observed_dates(request: TurnRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real TurnRunner.run until its first awaited step, where a probe reads every date source."""
    seen: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    async def probe(_orch: Any, _turn_id: str) -> None:
        seen["lead_anchor"] = _anchor_date()
        seen["router_banner"] = _banner_date()

        async def subagent() -> str:  # spawned like a peer: a task created inside the turn
            return _anchor_date()

        seen["subagent_anchor"] = await asyncio.create_task(subagent())
        seen["thread_tool"] = await asyncio.to_thread(lambda: model_today().isoformat())
        raise _Stop

    # The first await in TurnRunner.run is the legacy admission check: probe there, then stop.
    monkeypatch.setattr(turn_runner_module, "_require_admitted_legacy_bus", probe)
    with pytest.raises(_Stop):
        await TurnRunner(SimpleNamespace()).run(request)
    return seen


async def test_live_turn_subagents_and_tools_use_the_viewer_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    seen = await _observed_dates(_request(LanguageContext().with_time_zone(WEST)), monkeypatch)
    assert set(seen.values()) == {_date_in(WEST)}
    # The binding ends with the turn.
    assert model_today().isoformat() == _deploy_date(14)


async def test_resumed_and_continued_turns_restore_the_admitted_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    live = _request(LanguageContext().with_time_zone(WEST))
    live_dates = await _observed_dates(live, monkeypatch)

    # Clarification resume / ordinary continuation: snapshot persisted with the card.
    resume_context = {RESUME_LANGUAGE_KEY: language_context_snapshot(live)}
    resumed = _request(LanguageContext.from_dict(resume_context[RESUME_LANGUAGE_KEY]))
    # Terminal execution: the admitted envelope stores the same dict.
    terminal = _request(LanguageContext.from_dict(live.language_context.to_dict()))
    # Native document continuation: the runtime snapshot language.
    document = _request(DocumentLanguageSnapshot.model_validate(live.language_context.to_dict()).language_context())

    for request in (resumed, terminal, document):
        assert request_time_zone_name(request) == WEST
        assert await _observed_dates(request, monkeypatch) == live_dates


async def test_turns_without_a_viewer_zone_use_the_deploy_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduled jobs, IM channels and older clients admit no zone."""
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "-12")
    for request in (_request(None), _request(LanguageContext())):
        assert set((await _observed_dates(request, monkeypatch)).values()) == {_deploy_date(-12)}


def test_turn_clock_scope_ignores_a_tampered_snapshot_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    request = SimpleNamespace(language_context=SimpleNamespace(time_zone="America"))
    with turn_clock_scope(request):
        assert model_today().isoformat() == _deploy_date(14)


# ── i18n disabled: no language snapshot, the zone rides beside it ────────────


def test_turn_without_language_snapshot_uses_viewer_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "14")
    request = _request(None)
    request.viewer_time_zone = WEST
    assert request_time_zone_name(request) == WEST
    with turn_clock_scope(request):
        assert _anchor_date() == _banner_date() == _date_in(WEST)
    # A language snapshot's zone wins over the side carrier.
    request.language_context = LanguageContext().with_time_zone(EAST)
    assert request_time_zone_name(request) == EAST


def test_start_envelope_keeps_the_zone_only_beside_a_missing_language_snapshot() -> None:
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.terminal_runtime_factory import web_start_execution_intent

    source = ChatTurnUserSource(
        turn_id="t_1", session_id="s_1", user_id="u_1", workspace_id="ws_1", user_text="q", attachment_ids=(), attachment_refs=(), parts=None
    )
    request = _request(None)
    request.viewer_time_zone = WEST
    assert web_start_execution_intent(request, source)["viewer_time_zone"] == WEST
    request.viewer_time_zone = ""
    assert "viewer_time_zone" not in web_start_execution_intent(request, source)
    request.language_context = LanguageContext().with_time_zone(WEST)
    intent = web_start_execution_intent(request, source)
    assert "viewer_time_zone" not in intent
    assert intent["language_context"]["time_zone"] == WEST


def test_document_runtime_snapshot_takes_the_bound_zone_and_keeps_old_digests() -> None:
    """document_service builds the snapshot language from the Turn's bound zone (i18n-off Turns included)."""
    from bowhead_core.documents.continuation import DocumentRuntimeSnapshot
    from bowhead_core.documents.store import _digest, _json

    def snapshot(language: dict[str, Any]) -> DocumentRuntimeSnapshot:
        return DocumentRuntimeSnapshot(
            schema_version=1, image_id="sha256:" + "0" * 64, model_config_version=1, planner_profile="pptx-grounded-plan-v5", language=language
        )

    base = LanguageContext()
    legacy = snapshot(base.to_dict())  # shape persisted before the zone existed
    unbound = snapshot(base.with_time_zone(turn_time_zone_name()).to_dict())
    assert _digest(_json(unbound.model_dump())) == _digest(_json(legacy.model_dump()))
    with viewer_clock_scope(WEST):  # an i18n-off Turn: zone bound, language context without one
        bound = snapshot(base.with_time_zone(turn_time_zone_name()).to_dict())
    assert bound.language.language_context().time_zone == WEST
    assert _digest(_json(bound.model_dump())) != _digest(_json(legacy.model_dump()))


def test_resume_snapshot_keeps_the_zone_without_a_language_snapshot() -> None:
    from bowhead_core.language_context import (
        RESUME_TIME_ZONE_KEY,
        add_resume_time_zone,
        resume_language_context,
        resume_viewer_time_zone,
    )

    with viewer_clock_scope(WEST):
        bare: dict[str, Any] = {}
        add_resume_time_zone(bare)
        with_language = {RESUME_LANGUAGE_KEY: LanguageContext().with_time_zone(WEST).to_dict()}
        add_resume_time_zone(with_language)
    assert bare == {RESUME_TIME_ZONE_KEY: WEST}
    assert RESUME_TIME_ZONE_KEY not in with_language  # the language snapshot already carries it
    assert resume_viewer_time_zone(bare) == resume_viewer_time_zone(with_language) == WEST
    assert resume_viewer_time_zone({RESUME_TIME_ZONE_KEY: "Mars/Base"}) == ""
    # A language snapshot rejected for a locale mismatch still keeps the zone.
    mismatched = {RESUME_LANGUAGE_KEY: LanguageContext(ui_language="de-DE").with_time_zone(WEST).to_dict()}
    assert resume_language_context(mismatched, presentation_locale="en-US").time_zone == WEST
    # Outside a viewer turn nothing is added.
    empty: dict[str, Any] = {}
    add_resume_time_zone(empty)
    assert empty == {}
