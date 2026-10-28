"""discoverability regression test (2026-07-16, 真 LLM dogfood 逮出).

Prod 0.82.2 真 turn: 用户上传 skill（`skill_upload` → enabled）后，模型在 turn 里调
`skill_list` 只见 37 个内置 skill、看不到上传的 skill → 永不 `skill_read`/`skill_invoke`
→ command 桥接（本 follow-up 的核心）不可达。DIAG 坐实 `ctx.skill_instance_store=None`。

根因：`turn_runner.py` 的 **inline Lead-path `_peer_ctx`**（Lead 实际运行的就是这个，非
factory `_ctx`）漏了 `skill_instance_store` / `team_skill_store`。skill 工具的 `_list`/`_resolve`
读 `ctx.skill_instance_store` + `ctx.team_skill_store`——缺失 → user/team 上传 skill 既不进
`skill_list`（section 2 `if store is not None` 跳过）也无法 `_resolve` 调用。存量 user skill=0
时没暴露，第一个真上传 skill 撞出。同 `inline_preview_enabled` / `kb_doc_type_store` /
`scheduled_store` 的历史 `_peer_ctx` retrofit 漂移。

本测强制 turn_runner 里**每个** `ToolExecutionContext(` 构造都带 `skill_instance_store=` +
`team_skill_store=`，防再漂。
"""

from __future__ import annotations

import re
from pathlib import Path

_TURN_RUNNER = Path(__file__).parent.parent / "src" / "bowhead_core" / "turn_runner.py"


def _tool_ctx_blocks(src: str) -> list[str]:
    blocks: list[str] = []
    start = 0
    while True:
        idx = src.find("ToolExecutionContext(", start)
        if idx == -1:
            break
        depth = 1
        i = idx + len("ToolExecutionContext(")
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


def test_all_turn_runner_ctx_blocks_wire_skill_stores() -> None:
    """factory `_ctx` 与 inline Lead `_peer_ctx` 都必须带 skill_instance_store + team_skill_store，
    否则 user/team 上传 skill 在该路 turn 里既不可见（skill_list）也不可调（skill_invoke/_resolve）。"""
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    blocks = _tool_ctx_blocks(src)
    assert len(blocks) >= 2, f"expected ≥2 ToolExecutionContext( sites; found {len(blocks)}"
    for j, block in enumerate(blocks):
        assert "skill_instance_store=" in block, (
            f"ToolExecutionContext block #{j} 漏 `skill_instance_store=` —— user 上传 skill 在该 turn 路不可见/不可调（discoverability regression）。block start:\n{block[:200]}"
        )
        assert "team_skill_store=" in block, (
            f"ToolExecutionContext block #{j} 漏 `team_skill_store=` —— team 共享 skill 在该 turn 路不可见/不可调。block start:\n{block[:200]}"
        )


def test_skill_instance_store_reads_from_orch() -> None:
    """propagate 自 orch（getattr 安全默认），与 factory 同源不漂。"""
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    pattern = re.compile(r"skill_instance_store\s*=\s*getattr\(orch,\s*[\"']skill_instance_store[\"'],\s*None\)")
    assert len(pattern.findall(src)) >= 2, "expected ≥2 `skill_instance_store=getattr(orch, ..., None)` propagation sites"
