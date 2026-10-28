"""DEP-1 — current-date anchor injected into every agent's system prompt.

LLM has no built-in "today"; without this it mislabels relative/range periods
(real incident: a column ending 2026-05-31 labeled "2025年"). The anchor is the
minimal baseline grounding (NOT scaffolding). TZ defaults to Beijing (+8), env
BOWHEAD_TZ_OFFSET_HOURS overrides — and must accept 0 (UTC) / negative offsets,
which env_float (timeout semantics) would have wrongly rejected.
"""

import re
from datetime import datetime, timedelta, timezone

from bowhead_core.subagent import _current_date_anchor


def _expect(offset_h: float) -> str:
    d = datetime.now(timezone(timedelta(hours=offset_h))).strftime("%Y-%m-%d")
    return f"<current_date>{d}</current_date>"


def test_anchor_has_current_date_tag():
    a = _current_date_anchor()
    assert re.search(r"<current_date>\d{4}-\d{2}-\d{2}</current_date>", a)
    # carries a usage hint, not just the bare date
    assert "今年" in a or "相对时间" in a


def test_anchor_default_is_beijing(monkeypatch):
    monkeypatch.delenv("BOWHEAD_TZ_OFFSET_HOURS", raising=False)
    assert _expect(8.0) in _current_date_anchor()


def test_anchor_accepts_zero_and_negative_offsets(monkeypatch):
    # regression: env_float rejects <=0 → would silently fall back to +8.
    for off in ("0", "-5", "5.5"):
        monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", off)
        assert _expect(float(off)) in _current_date_anchor()


def test_anchor_junk_offset_falls_back_to_beijing(monkeypatch):
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "notanumber")
    assert _expect(8.0) in _current_date_anchor()
