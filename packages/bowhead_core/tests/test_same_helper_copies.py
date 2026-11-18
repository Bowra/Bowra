"""A helper's last saved copy of each declared deliverable, per Turn (same-name-results-versions-2026-11-17 §3.1).

Core keeps the record because only Core knows the calling helper (a context variable); the sandbox tool
reads and writes it through two hooks on the tool context.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents._lead_context import reset_current_subagent_id, set_current_subagent_id

from bowhead_core import skill_bridge


@pytest.fixture(autouse=True)
def _fresh_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_bridge, "_HELPER_COPIES", type(skill_bridge._HELPER_COPIES)())


def _as(helper: str | None, call):  # type: ignore[no-untyped-def]
    token = set_current_subagent_id(helper) if helper else None
    try:
        return call()
    finally:
        if token is not None:
            reset_current_subagent_id(token)


def test_each_helper_sees_only_its_own_copy_of_an_entry_in_its_turn() -> None:
    ctx = SimpleNamespace(turn_id="t_game")
    skill_bridge._install_helper_copy_hooks(ctx)
    _as("sub_compute", lambda: ctx.record_same_helper_copy("dragonball_kinto_un.html", "art_1"))
    _as("sub_compute", lambda: ctx.record_same_helper_copy("dragonball_kinto_un.html", "art_2"))

    assert _as("sub_compute", lambda: ctx.same_helper_previous_copy("dragonball_kinto_un.html")) == "art_2"
    assert _as("sub_other", lambda: ctx.same_helper_previous_copy("dragonball_kinto_un.html")) is None, "Parallel helpers stay apart"
    assert _as(None, lambda: ctx.same_helper_previous_copy("dragonball_kinto_un.html")) is None, "Lead is its own helper"
    assert _as("sub_compute", lambda: ctx.same_helper_previous_copy("site/blog/index.html")) is None, "Another entry is another file"

    later = SimpleNamespace(turn_id="t_next")
    skill_bridge._install_helper_copy_hooks(later)
    assert _as("sub_compute", lambda: later.same_helper_previous_copy("dragonball_kinto_un.html")) is None, "Another Turn starts fresh"


def test_the_record_keeps_only_the_newest_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_bridge, "_HELPER_COPIES_TURNS", 2)
    contexts = [SimpleNamespace(turn_id=f"t_{index}") for index in range(3)]
    for ctx in contexts:
        skill_bridge._install_helper_copy_hooks(ctx)
        _as("sub_compute", lambda ctx=ctx: ctx.record_same_helper_copy("report.html", f"art_{ctx.turn_id}"))
    assert list(skill_bridge._HELPER_COPIES) == ["t_1", "t_2"]
    assert _as("sub_compute", lambda: contexts[0].same_helper_previous_copy("report.html")) is None


def test_a_context_without_a_turn_gets_no_hooks() -> None:
    ctx = SimpleNamespace(turn_id=None)
    skill_bridge._install_helper_copy_hooks(ctx)
    assert not hasattr(ctx, "same_helper_previous_copy")
