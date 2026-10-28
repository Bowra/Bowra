"""Sprint χ-1 — `parallel_tool_calls` rejection sniffer.

Per tech-design-r3 §2.2 + ENG-χ-5 (real provider 400 string verify):

DeepSeek + Qwen 都 OpenAI-compat 但实际是否接受 `parallel_tool_calls=true`
未文档化。Qwen 历史上必须显式开 (per 调研); DeepSeek 文档称 "supports 128
parallel" 但参数未明示。χ-1 probe 抓真 400 error string,落 fixture; runtime
sniff match → drop flag for that provider + log warn once。

Single-worker invariant (I-χ-SW1) makes module-level set safe.
"""

from __future__ import annotations

import json
import logging
import re
import threading

_LOG = logging.getLogger(__name__)

# Providers where we observed 400 reject — drop flag, no retry needed next time.
_dropped: set[str] = set()
_warned: set[str] = set()
_lock = threading.Lock()

# Hard-coded patterns from `scripts/chi/probe_parallel_param.py` real captures.
# Add new ones as discovered. All case-insensitive.
_HARDCODED_PATTERNS: list[re.Pattern[str]] = [
    # Placeholders — fill in from probe-script output (commit
    # `tests/router/fixtures/chi_sniff_strings.json` for evidence).
    # Examples (hypothetical until probe runs):
    # re.compile(r"parallel_tool_calls.*not\s+supported", re.IGNORECASE),
    # re.compile(r"unknown\s+(?:parameter|argument).*parallel", re.IGNORECASE),
]

# Defensive fallback: classify a 400 body as a parallel_tool_calls rejection only when "parallel"
# **and** nearby unsupported context are present (review P0: avoid dropping on unrelated 400s such as 'rate limit on parallel
# requests' / 'max parallel sessions').
_FALLBACK_PAT: re.Pattern[str] = re.compile(
    r"parallel[_\s\-]?tool[_\s\-]?calls"  # Parameter name (underscore/hyphen/space variants).
    r"|parallel.{0,30}(?:not[_\s\-]?support|unknown|unrecognized|invalid|unsupported)"
    r"|(?:unknown|unrecognized|invalid).{0,30}parallel",
    re.IGNORECASE,
)

# Intentionally empty until a redacted real-provider capture with provenance is
# committed. Tests inject an exact fixture entry; production fails closed.
_DURABLE_REJECTION_MANIFEST_V1: frozenset[tuple[str, str, str, str, str]] = frozenset()


# Warn-once tracking for sniff-miss ERROR log (review-r1.1 P1: storm risk + body leak).
_sniff_missed: set[str] = set()


def mark_sniff_missed(provider: str) -> bool:
    """Return True iff this is the FIRST sniff-miss for `provider` (caller logs ERROR).

    Subsequent misses return False → caller demotes to DEBUG to avoid log storm.
    """
    with _lock:
        if provider in _sniff_missed:
            return False
        _sniff_missed.add(provider)
        return True


def matches_param_unknown(error_body: str) -> bool:
    """Return True if 400 error body suggests `parallel_tool_calls` was rejected."""
    if not error_body:
        return False
    for pat in _HARDCODED_PATTERNS:
        if pat.search(error_body):
            return True
    return bool(_FALLBACK_PAT.search(error_body))


def matches_durable_param_rejection_v1(
    error_body: str,
    *,
    adapter_type: str,
    adapter_contract_version: str,
    provider_host: str,
) -> bool:
    """Match only a Bowhead-owned, versioned provider capture."""

    if not error_body:
        return False
    try:
        payload = json.loads(error_body)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            param = str(error.get("param") or "").strip().lower()
            code = str(error.get("code") or error.get("type") or "").strip().lower()
            identity = (
                adapter_type,
                adapter_contract_version,
                provider_host,
                param,
                code,
            )
            return identity in _DURABLE_REJECTION_MANIFEST_V1
    return False


def mark_dropped(provider: str) -> None:
    """Record provider as having rejected the flag.

    Subsequent `is_dropped()` returns True so adapter skips the param.
    Warn-once: only first observation logs at WARN level.
    """
    with _lock:
        first = provider not in _warned
        _dropped.add(provider)
        if first:
            _warned.add(provider)
    if first:
        _LOG.warning(
            "chi.parallel_tool_calls dropped for provider=%s (observed 400 reject). Adapter will skip param going forward.",
            provider,
        )


def is_dropped(provider: str) -> bool:
    """Has this provider rejected the flag before?"""
    with _lock:
        return provider in _dropped


def _reset_for_test() -> None:
    """Test helper — clear sniff state between tests."""
    with _lock:
        _dropped.clear()
        _warned.clear()
        _sniff_missed.clear()
