"""Sprint χ-2 runtime — process-wide global semaphore for parallel tool exec.

Single-worker invariant (I-χ-SW1, enforced by chi_flags.assert_consistent_and_single_worker)
makes module-level Semaphore safe. Lazy-init avoids import-time event-loop dependency
(asyncio.Semaphore requires running loop in some Python versions).

Per tech-design-r3 §3.2: double semaphore (per-batch + global) bounds:
- per-batch (new per LLM response, from chi_flags.max_concurrent default 6)
- global (process-wide, from BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT default 12)

Total concurrent = min(per_batch, global). Global prevents cross-turn floods
(N turns × 6 per-batch = could overrun datarc if not capped).
"""

from __future__ import annotations

import asyncio

from bowhead_core import chi_flags

_global_sem: asyncio.Semaphore | None = None


def get_global_sem() -> asyncio.Semaphore:
    """Lazy-init module-level global semaphore. Single-worker safe."""
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(chi_flags.global_max_concurrent())
    return _global_sem


def _reset_for_test() -> None:
    """Test helper — clear singleton so subsequent get_global_sem() re-creates
    with new env-var value."""
    global _global_sem
    _global_sem = None
