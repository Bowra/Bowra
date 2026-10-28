"""M158-18 phase 4 framework · project_memory 周期 cron 聚合.

设计文档: docs/M158-paradigm-shift-lead-as-manager.md §6

### 用途

跨 user 聚合 user_memory.preference (e.g. task_plan_pick_*) 写到 project_memory:
- pattern: 哪些 sub_q 在 workspace 内被多用户 pick (跨 user 投票)
- typical_satisfaction: workspace 内 turn satisfaction 平均
- 后续 Lead system prompt 可注入"该 workspace 类似题用户通常想看 X / Y / Z"

### Stub 实现 (phase 4+ 才完整)

现 stub:
- 拿 user_memory kind=preference key 含 'task_plan_pick_*' 全部条目
- 按 workspace_id 分组聚合 (workspace_id 从 turn 拉?). **stub 简化**: 不分 workspace,
  直接全局聚合写 ws='*' (后续真 specialist subagent ramp 后改 workspace 级)
- 写 project_memory subject_id='*' kind='aggregated_pattern' key=label
- 频率: env BOWHEAD_PROJECT_MEMORY_CRON_HOURS, default 168 (7 days)

### Fail-safe

cron 失败不影响 prod. try/except 全包, 下次 cron 再试.

启动: gateway lifespan startup 调 start_project_memory_cron(...) → asyncio.create_task.
关闭: 主进程退出时 task 自然取消 (daemon 行为).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_CRON_HOURS = 168  # 7 days


def _cron_interval_seconds() -> float:
    raw = os.environ.get("BOWHEAD_PROJECT_MEMORY_CRON_HOURS", str(_DEFAULT_CRON_HOURS))
    try:
        hours = float(raw)
    except (ValueError, TypeError):
        hours = float(_DEFAULT_CRON_HOURS)
    return max(60.0, hours * 3600.0)  # Minimum 60s prevents dev-debug overload.


async def aggregate_once(
    user_memory_store: Any,
    project_memory_store: Any,
    target_workspace: str = "*",
) -> dict[str, int]:
    """跑一次聚合. 返 stats {users_scanned, patterns_written, errors}.

    Q2 fix: 真聚合 (不再是 sentinel heartbeat).

    算法:
    1. 拉所有 user_memory kind='preference' key 前缀 'task_plan_pick_' (M158-11 写的)
    2. 跨 user 累计 pick_count 到 pattern_label
    3. 写 project_memory kind='aggregated_pattern' key=label value={total_picks, user_count}

    隐私 (M158 §6.4): 不存原 prompt 文本, key 是 sub_q label (中文短句, hash 也可); 跨 user
    聚合不归属个人.

    target_workspace='*' 现简化为全局聚合 (workspace 级要 user → workspace 反查路径,
    phase 4.5+ 加 user_workspace 关联表后细化).
    """
    stats = {"users_scanned": 0, "patterns_written": 0, "errors": 0, "heartbeat_written": 0}

    # 1. heartbeat (verify cron + last_run_at timestamp; write even without data).
    try:
        await project_memory_store.upsert(
            workspace_id=target_workspace,
            kind="aggregated_pattern",
            key="_cron_heartbeat",
            value={"last_run_at": _utc_now_iso()},
            source="cron_aggregator",
        )
        stats["heartbeat_written"] = 1
    except Exception as e:
        stats["errors"] += 1
        logger.warning("project_memory_aggregator.heartbeat_failed err=%s", e)
        return stats

    # 2. Fetch task_plan_pick data across users.
    try:
        if hasattr(user_memory_store, "list_by_kind_pattern"):
            entries = await user_memory_store.list_by_kind_pattern(
                kind="preference",
                key_prefix="task_plan_pick_",
            )
        else:
            entries = []  # Old store lacks this API: fall back to empty.
    except Exception as e:
        stats["errors"] += 1
        logger.warning("project_memory_aggregator.list_failed err=%s", e)
        return stats

    # 3. Aggregate across users: label → (total_picks, user_count_set).
    label_agg: dict[str, dict[str, Any]] = {}
    user_set: set[str] = set()
    for e in entries:
        user_set.add(e.subject_id)
        # Key shape: "task_plan_pick_竞品份额对比".
        label = e.key.replace("task_plan_pick_", "", 1) if e.key.startswith("task_plan_pick_") else e.key
        try:
            parsed = e.parsed_value()
            pick_count = int(parsed.get("pick_count", 1)) if isinstance(parsed, dict) else 1
        except (ValueError, TypeError, AttributeError):
            pick_count = 1
        agg = label_agg.setdefault(label, {"total_picks": 0, "user_ids": set()})
        agg["total_picks"] += pick_count
        agg["user_ids"].add(e.subject_id)

    stats["users_scanned"] = len(user_set)

    # 4. Sort and write top-20 patterns by descending total_picks. Bound cardinality.
    sorted_labels = sorted(
        label_agg.items(),
        key=lambda kv: (-kv[1]["total_picks"], kv[0]),
    )[:20]
    for label, agg in sorted_labels:
        try:
            await project_memory_store.upsert(
                workspace_id=target_workspace,
                kind="aggregated_pattern",
                key=f"task_plan_pick:{label}"[:200],
                value={
                    "total_picks": agg["total_picks"],
                    "user_count": len(agg["user_ids"]),
                    "label": label,
                    "last_aggregated_at": _utc_now_iso(),
                },
                source="cron_aggregator",
            )
            stats["patterns_written"] += 1
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "project_memory_aggregator.pattern_write_failed label=%s err=%s",
                label[:32],
                e,
            )

    logger.info(
        "project_memory_aggregator.done ws=%s stats=%s",
        target_workspace,
        stats,
    )
    return stats


def start_project_memory_cron(
    user_memory_store: Any,
    project_memory_store: Any,
) -> asyncio.Task:
    """gateway lifespan startup 调一次. 返 task 句柄 — lifespan shutdown 应 cancel + await.

    跑频率: env BOWHEAD_PROJECT_MEMORY_CRON_HOURS, default 168h (7 days).

    Q10 fix: startup 立即跑一次 (delay 5s 防启动 race), 然后 sleep loop. dashboard 部署
    后立刻有数据, 不用等 7 天.
    """
    interval = _cron_interval_seconds()

    async def _loop() -> None:
        logger.info(
            "project_memory_cron.started interval_sec=%.0f initial_delay=5",
            interval,
        )
        # Q10: run once immediately at startup (short delay avoids startup races).
        await asyncio.sleep(5.0)
        try:
            await aggregate_once(user_memory_store, project_memory_store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("project_memory_cron.initial_run_failed err=%s", e)

        while True:
            try:
                await asyncio.sleep(interval)
                await aggregate_once(user_memory_store, project_memory_store)
            except asyncio.CancelledError:
                logger.info("project_memory_cron.cancelled (clean shutdown)")
                raise
            except Exception as e:
                logger.warning("project_memory_cron.tick_failed err=%s", e)
                # Failure does not break the loop; continue next interval.

    return asyncio.create_task(_loop(), name="project_memory_cron")


async def stop_project_memory_cron(task: asyncio.Task | None) -> None:
    """Q12 fix: lifespan shutdown 优雅退出. cancel + await 确保 sleep cycle 退出, 不漏数据."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("project_memory_cron.stop_failed err=%s", e)


def _utc_now_iso() -> str:
    from datetime import datetime

    return datetime.now(tz=UTC).isoformat(timespec="seconds")


__all__ = [
    "aggregate_once",
    "start_project_memory_cron",
    "stop_project_memory_cron",
]
