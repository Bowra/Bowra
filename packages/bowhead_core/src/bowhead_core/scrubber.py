"""Central PII scrubber (Sprint ι R-γ).

Used by `_map_code_block` before persisting `stdout_preview` and full stdout
to `artifact_stdout` table. Patterns conservative — false positive accepted
over false negative (audit visibility > log fidelity).

Patterns covered (per PRD §5 R-γ):
  - 国民身份证 (CN national ID, 18-digit checksum form)
  - 手机号 (CN mobile, 11-digit prefix 1[3-9]xxxxxxxxx)
  - 邮箱 (RFC-light, conservative)
  - API key prefix (sk-* / api-* with ≥20 alphanum)

Out of scope (handled elsewhere or accepted risk):
  - SSN / passport (no Bowhead workspace handles US/global ID surface today)
  - Bearer tokens (gated at gateway level, never reach sandbox stdout)
  - DB connection strings (compute peer doesn't have DB creds in env)
"""

from __future__ import annotations

import re

_CN_ID_RE = re.compile(
    r"\b[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b",
)
_CN_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_APIKEY_RE = re.compile(r"\b(sk-|api-)[A-Za-z0-9_\-]{20,}\b")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_CN_ID_RE, "[REDACTED:身份证]"),
    (_CN_PHONE_RE, "[REDACTED:手机号]"),
    (_EMAIL_RE, "[REDACTED:邮箱]"),
    (_APIKEY_RE, "[REDACTED:api_key]"),
]


def scrub(text: str) -> str:
    """Replace PII matches with [REDACTED:<label>] tokens. Idempotent."""
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_with_count(text: str) -> tuple[str, dict[str, int]]:
    """As scrub() but also returns per-pattern hit counts (for audit log)."""
    if not text:
        return text, {}
    counts: dict[str, int] = {}
    out = text
    for pat, repl in _PATTERNS:
        n = len(pat.findall(out))
        if n:
            label = repl.split(":", 1)[1].rstrip("]")
            counts[label] = n
        out = pat.sub(repl, out)
    return out, counts
