"""chat-truth-hotfix-2 regression test (2026-05-15).

Prod 0.20.0 trace t_0fecb869b349: peer (compute) called bi_schema + bi_query,
both returned valid data, but tool_result events had `preview_payload: null`,
meaning the LLM finding micro-call was skipped at the `no_preview` gate → FE
chat trace tool tile showed "—" placeholders.

Root cause: `turn_runner.py:_run_inner._peer_ctx` ToolExecutionContext
construction was missing the `inline_preview_enabled` field, so it defaulted
to False from the dataclass. The standalone factory `build_tool_exec_ctx`
at line 371 had it; the inline path was missing it. Out of sync.

This test enforces both ctx-construction sites in turn_runner.py propagate
`inline_preview_enabled` from `orch` so they don't drift again.
"""

from __future__ import annotations

import re
from pathlib import Path

_TURN_RUNNER = Path(__file__).parent.parent / "src" / "bowhead_core" / "turn_runner.py"


def test_turn_runner_has_inline_preview_in_all_ctx_blocks():
    """Both ToolExecutionContext( construction sites must include
    `inline_preview_enabled=` so the peer's tool execution can build
    preview_payload (chat-v3 finding requires non-null preview)."""
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    # Find all ToolExecutionContext( ... ) blocks.
    blocks = []
    start = 0
    while True:
        idx = src.find("ToolExecutionContext(", start)
        if idx == -1:
            break
        # Locate the matching closing paren at the same indentation column.
        depth = 0
        i = idx + len("ToolExecutionContext(")
        depth = 1
        while i < len(src) and depth > 0:
            c = src[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        blocks.append(src[idx:i])
        start = i

    assert len(blocks) >= 2, f"expected ≥2 ToolExecutionContext( sites in turn_runner.py; found {len(blocks)}"
    for j, block in enumerate(blocks):
        assert "inline_preview_enabled" in block, (
            f"ToolExecutionContext block #{j} missing `inline_preview_enabled` field — chat-truth-hotfix-2 regression. Block start:\n{block[:200]}"
        )


def test_inline_preview_enabled_reads_from_orch_with_safe_default():
    """Field should propagate via getattr(orch, ..., False) so missing attr
    falls back safely (defensive plumbing)."""
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    # Expect the exact form to match both ctx sites.
    pattern = re.compile(r"inline_preview_enabled\s*=\s*getattr\(orch,\s*[\"']inline_preview_enabled[\"'],\s*False\)")
    matches = pattern.findall(src)
    assert len(matches) >= 2, f"expected ≥2 `inline_preview_enabled=getattr(orch, ..., False)` propagation sites; found {len(matches)}"
