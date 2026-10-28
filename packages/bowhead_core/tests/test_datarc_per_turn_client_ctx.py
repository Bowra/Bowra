"""ws-datarc-decouple P2a (③) — both ToolExecutionContext sites must source `datarc` from the
per-turn `request.datarc_client` (gateway-resolved per-team client), and from **nothing else**:
团队连接收口（9e186d5d）后部署级 `orch.datarc` / `orch.vector_client` 回退是被禁止的入口。

Drift-guard in the same source-parsing style as test_chat_truth_hotfix2_peer_ctx_inline_preview:
the inline `_peer_ctx` and the standalone `build_tool_exec_ctx` factory drifted before (the
inline_preview bug), so both must be asserted to wire the per-turn client or ③ multi-tenant
silently regresses to the single global client on one path.
"""

from __future__ import annotations

import re
from pathlib import Path

_TURN_RUNNER = Path(__file__).parent.parent / "src" / "bowhead_core" / "turn_runner.py"


def _ctx_blocks(src: str) -> list[str]:
    blocks: list[str] = []
    start = 0
    while True:
        idx = src.find("ToolExecutionContext(", start)
        if idx == -1:
            break
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
    return blocks


def test_both_ctx_sites_prefer_per_turn_datarc_client() -> None:
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    blocks = _ctx_blocks(src)
    assert len(blocks) >= 2, f"expected ≥2 ToolExecutionContext sites; found {len(blocks)}"
    # The drift-prone sites are the two that pass `datarc=`. Each must prefer the per-turn
    # request.datarc_client and only then fall back to the orch-global client.
    datarc_blocks = [b for b in blocks if re.search(r"\bdatarc\s*=", b)]
    assert len(datarc_blocks) >= 2, f"expected ≥2 ctx sites passing datarc=; found {len(datarc_blocks)}"
    for j, block in enumerate(datarc_blocks):
        assert 'getattr(request, "datarc_client", None)' in block, (
            f"ctx block #{j} does not prefer per-turn request.datarc_client — ③ multi-tenant "
            f"would regress to the global client on this path. Block head:\n{block[:240]}"
        )
        # After team-connection consolidation (9e186d5d), the guard reversed: datarc clients **must** come from team-saved
        # connections; deployment-level orch.datarc fallback is explicitly forbidden (CLAUDE.md P0). Restoring it creates
        # cross-tenant exposure. The old assertion required fallback; the new assertion requires its absence.
        assert 'getattr(orch, "datarc"' not in block, (
            f"ctx block #{j} 复活了部署级 orch.datarc 回退——团队连接是北极九章 client 的唯一"
            f"来源，回退会让某团队的 BI 打到共享的部署级 client。Block head:\n{block[:240]}"
        )
        # [review P1-b] both sites must fail CLOSED when resolution failed: datarc_unavailable
        # short-circuits to None, else a failed per-team resolution silently leaves a stale
        # client wired to the turn.
        assert 'getattr(request, "datarc_unavailable", False)' in block, (
            f"ctx block #{j} does not honor datarc_unavailable — a FAILED per-team datarc "
            f"resolution would not fail closed (review P1-b fail-open). "
            f"Block head:\n{block[:240]}"
        )
        unavailable_at = block.index('getattr(request, "datarc_unavailable", False)')
        client_at = block.index('getattr(request, "datarc_client", None)')
        assert unavailable_at < client_at, (
            f"ctx block #{j}: the datarc_unavailable guard must come BEFORE the per-turn client "
            f"(None short-circuit), else fail-closed degrades to fail-open. "
            f"Block head:\n{block[:240]}"
        )


def test_both_ctx_sites_prefer_per_turn_vector_client() -> None:
    """embed-via-openapi: the SAME two historically-drifting ctx sites also wire the per-turn
    embed-proxy client (request.vector_client) before the global direct client. P4 (②): the
    embed dictionary IS datarc's dictionary, so both sites must ALSO honor datarc_unavailable
    (None short-circuit BEFORE any client) — no datarc for the turn → no dictionary."""
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    blocks = [b for b in _ctx_blocks(src) if re.search(r"\bdatarc\s*=", b)]
    assert len(blocks) >= 2, f"expected ≥2 ctx sites passing datarc=; found {len(blocks)}"
    for j, block in enumerate(blocks):
        assert 'getattr(request, "vector_client", None)' in block, (
            f"ctx block #{j} does not prefer per-turn request.vector_client — an team's embed "
            f"(BI semantic dictionary) would silently use the global direct client on this "
            f"path. Block head:\n{block[:240]}"
        )
        # Like datarc=: the embed dictionary is the Datarc dictionary; deployment-level orch.vector_client fallback is forbidden too.
        assert 'getattr(orch, "vector_client"' not in block, (
            f"ctx block #{j} 复活了部署级 orch.vector_client 回退——embed 字典随团队连接走，"
            f"回退会让某团队读到共享的部署级字典。Block head:\n{block[:240]}"
        )
        request_at = block.index('getattr(request, "vector_client", None)')
        # P4: vector_client= must short-circuit to None on datarc_unavailable, and the guard
        # must appear BEFORE the per-turn resolution (same shape as the datarc= guard).
        vec_kw_at = block.index("vector_client=")
        vec_segment = block[vec_kw_at:request_at]
        assert 'getattr(request, "datarc_unavailable", False)' in vec_segment, (
            f"ctx block #{j}: vector_client= does not honor datarc_unavailable — an team whose "
            f"datarc is absent/cut would still read the (global) BI semantic dictionary. "
            f"Block head:\n{block[:240]}"
        )
