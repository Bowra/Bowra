"""团队反馈办案的长期产品设置。"""

from __future__ import annotations

from typing import Any

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Team flag (tech-design §2.8.3 ③): the background coach uses customers' own provider keys, so they must be able to disable it.
# Stored in team_settings K/V; default ON. Affects only background casework (cron/major cases), never owner-initiated adoption drafting.
KEY_TEAM_FEEDBACK_CASEWORK_ENABLED = "feedback_casework_enabled"


def parse_casework_enabled(raw: str | None) -> bool:
    """team_settings 里的字符串值 → 布尔；缺省与不认识的值都按开。"""

    return (raw or "").strip().lower() not in _FALSE_VALUES


async def team_casework_enabled(team_settings_store: Any | None, team_id: str) -> bool:
    if team_settings_store is None:
        return True
    try:
        raw = await team_settings_store.get(team_id, KEY_TEAM_FEEDBACK_CASEWORK_ENABLED)
    except Exception:
        return True  # Unreadable settings must not stop background work; the write side uses the same criterion.
    return parse_casework_enabled(raw)
