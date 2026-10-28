"""GA readiness gate for the member Agent Marketplace surface."""

from __future__ import annotations

from typing import Any


async def subagent_management_v2_ready(
    assignment_store: Any,
    *,
    schema_ready: bool,
) -> bool:
    """Return the single consumer-surface admission decision.

    The route and capability advertisement both use this helper. The GA
    surface is available only after complete schema/constraint checks,
    reader/writer compatibility admission, and the one-time legacy cutover.
    """

    if not schema_ready or assignment_store is None:
        return False
    try:
        return bool(await assignment_store.marketplace_ready())
    except Exception:
        return False


__all__ = ["subagent_management_v2_ready"]
