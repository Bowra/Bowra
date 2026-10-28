"""Credential scrubbing — neutral low-level helper (cca PRD §6.2 / ④f).

A best-effort denylist that strips credential-shaped substrings from any string
that may reach the LLM / logs / ctx. It lives in its OWN dependency-free module so
the cred-leak chokepoints can share ONE implementation without import cycles:
``openapi_tool_source`` (engine error/success text), ``team_tool_registry_view``
(isError bridge), and ``tool_registry.execute()`` (the funnel where EVERY tool's
raised-exception text becomes ``invocation.error`` — native / MCP / team-created).

Scope (cca engine security disposition): this is the BEST-EFFORT layer. A regex
denylist of credential SHAPES is irreducibly incomplete — the STRUCTURAL guarantees
(error paths echo only bounded safe fields; injected auth headers are scrubbed) are
what's gate-able. Scrub is the belt-and-suspenders net on top.
"""

from __future__ import annotations

import re

_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer [redacted]"),  # \S+ catches base64url JWTs (+/=)
    # Basic auth: the keyword pattern below stops at the space before the b64, so the
    # base64 user:pass would leak (re-review #1) — redact "Basic <b64>" as a whole.
    (re.compile(r"(?i)basic\s+[A-Za-z0-9+/=_-]+"), "Basic [redacted]"),
    # sk-… incl. dashed modern formats (sk-proj-…, sk-ant-api03-…); ._- in the class so
    # the run doesn't stop at the first dash and leak the rest (re-review #sk-dashed).
    (re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9._-]{6,}"), "[redacted-key]"),
    # `auth[_-]?key` added (ws-datarc-decouple R7.5): the datarc connection secret is named
    # `auth_key`; without it `{'auth_key': '<secret>'}` dict-reprs / `auth_key=<secret>` query
    # strings leaked (the `api[_-]?key` branch doesn't match "auth_key"). Value-independent —
    # the datarc key is opaque (no prefix), so we key off the field name, not a key shape.
    (
        re.compile(
            r"(?i)(authorization|auth[_-]?key|api[_-]?key|access[_-]?token|token|secret|password)"
            r"([\"'\s:=]+)[^\s\"',&]+"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"://[^/@\s]+@"), "://[redacted]@"),  # userinfo in URLs
]


def scrub(s: str) -> str:
    """Strip credential-shaped substrings from any string that may reach the LLM /
    logs / ctx (PRD §6.2). The executor never echoes upstream URL / body / headers,
    but error templates + exception text pass through here; ④f re-applies this at
    the ``tool_registry.execute()`` chokepoint for raised paths (belt-and-suspenders)."""
    for pat, repl in _SCRUB_PATTERNS:
        s = pat.sub(repl, s)
    return s
