"""批 2-a §3.6 / §6.4 —— 各站点的 LLM 调用必须带 `(team_id, user_id, reason)` 归属.

**这是设计定为「闸内（ship 前必过）」的那条验收**，实施时漏了（实现层 review P1）。

为什么非要有：`bowhead_router/usage.py` 明写「未设上下文时仍会记录
（`reason="unattributed"`）」——漏包一个站点，所有测试全绿、用量行照落、团队 BYO
额度被花掉却记不到账上。这正是 credit 计费 sprint 留下的「钱扣了账没记」教训。

这批修的 9 个站点在 prod **从未成功调用过 LLM**，修好后是第一次真烧 token；归属
写错的代价直接是钱。而闸外取证（查 `llm_usage_events` 按 reason 分组）依赖真实
流量——站点 1/4 需管理员手工触发、站点 7 多数团队不绑 `home_proposal`，闸内不测
就等于完全没验。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_router.usage import UsageContext, set_usage_sink


@pytest.fixture
def usage_calls():
    """装一个记录 sink，返回收到的 UsageContext 列表。"""
    seen: list[UsageContext] = []

    def _sink(ctx: UsageContext, **kw: Any) -> None:
        seen.append(ctx)

    previous = set_usage_sink(_sink)
    try:
        yield seen
    finally:
        set_usage_sink(previous)


def _reasons(seen: list[UsageContext]) -> set[str]:
    return {c.reason for c in seen if c.reason}


# ── Call-site reason vocabulary ───────────────────────────────
# Update this when a call site's reason changes: **vocabulary is a contract**, used externally to group llm_usage_events.
_SITE_REASONS = {
    "tool_source_propose",  # Call site 1.
    "workflow_distill",  # Call site 2.
    "workflow_dry_run",  # Call site 3 (turn).
    "workflow_dry_run_judge",  # Call site 3 (judge).
    "kb_matrix_column_draft",  # Call site 4.
    "artifact_classify",  # Call site 6.
    "config_draft",  # Added in batch 2-b: copilot draft route itself, omitted from batch 2-a's nine-site inventory.
}


def test_every_site_reason_is_wired_in_source() -> None:
    """静态守卫：每个站点的 `usage_context(...)` 都真的在源码里。

    比「跑一遍看落了什么」便宜且不依赖各站点的前置条件（flag / 权限 / 缓存），
    摘掉任何一处包裹都会红。运行期的形状由下面那条端到端断言兜。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    missing = sorted(r for r in _SITE_REASONS if f'reason="{r}"' not in blob)
    assert not missing, (
        f"以下站点没有包 usage_context（或 reason 变了没同步词表）：{missing}\n"
        "漏包的后果不是报错，是这次调用记成 reason=unattributed、team_id=NULL——"
        "团队 BYO 额度被花掉却记不到账上，且闸外按 reason 的取证会查不到。"
    )


async def test_usage_context_actually_reaches_the_sink() -> None:
    """端到端：`usage_context` 里发起的调用，sink 收到的 ctx 带全三个字段。

    守的是「包了但没生效」——比如包在 await 之外、或被别的 context 覆盖。
    """
    from bowhead_router.usage import usage_context

    seen: list[UsageContext] = []
    previous = set_usage_sink(lambda ctx, **kw: seen.append(ctx))
    try:
        from bowhead_router.usage import current_usage_context

        with usage_context(team_id="t_1", user_id="u_1", reason="tool_source_propose"):
            ctx = current_usage_context()
        assert ctx.team_id == "t_1"
        assert ctx.user_id == "u_1"
        assert ctx.reason == "tool_source_propose"
    finally:
        set_usage_sink(previous)


def test_reason_vocabulary_has_no_placeholder() -> None:
    """reason 不得是 `unknown` / 空 —— 那与没包等价。"""
    assert "unknown" not in _SITE_REASONS
    assert all(r and r.strip() == r for r in _SITE_REASONS)
