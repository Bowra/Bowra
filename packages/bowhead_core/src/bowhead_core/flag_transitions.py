"""flag 翻转时刻账本 —— 「先落基线再翻 flag」的机械化替身（上下文工程 sprint，缩小版方案 B）。

批 2 的 `BOWHEAD_CTX_PREFIX_ORDER` 默认开、随 1.2.25 发版即生效，而 tech design 写的
「翻 flag 之前先落一份改前基线」没人做——定顺序的人和发版的人不是同一个，发版这个动作本身就
完成了翻 flag，中间没有任何机械的拦截点。

`llm_usage_daily` 已经永久保留逐日聚合（含 `tokens_cached`），所以「改前的数据」不会丢；
真正缺的只有一件事：**flag 是哪一刻翻开的**。本模块只记这一件：

- 每次 gateway 启动观测一遍登记在册的 flag；
- 某 flag **首次观测到 ON**（`kind=first_seen`——真正翻开的时刻可能更早，这只是上界），
  或此后状态与上次记录**不同**（`kind=flip`），就追加一行；
- 从未 ON 过的 flag 不记（OFF 的首次观测没有基线意义）。

落点是 sqlite 目录旁的 `flag_transitions.jsonl`（append-only，一行一个 JSON），不进库、不加迁移。
事后用行里的 `at` 去切 `llm_usage_daily` / `scripts/llm_cache_baseline.py --split-at` 即可。

📏 docs/sprints/context-engineering-2026-09-04/next.md「一个需要你拍板的取舍」（用户选 B，缩小后的形态）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = "flag_transitions.jsonl"


def _last_states(path: Path) -> dict[str, bool]:
    """按文件顺序读取，每个 flag 只保留最后一行的状态。坏行跳过，不让账本损坏挡住启动。"""
    states: dict[str, bool] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return states
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):  # Review fix: skip valid non-object JSON such as `[]`; do not raise.
            continue
        flag = row.get("flag")
        on = row.get("on")
        if isinstance(flag, str) and isinstance(on, bool):
            states[flag] = on
    return states


def record_flag_transitions(
    flags: Mapping[str, bool],
    *,
    version: str,
    data_dir: Path,
    now: datetime | None = None,
) -> list[str]:
    """把 ``flags`` 里与上次记录不同的状态追加进账本；返回本次新记录的 flag 名。

    - 首次观测且 ON → 记；首次观测且 OFF → 不记；
    - 已有记录且状态变化（任一方向）→ 记；状态不变 → 不记。
    纯函数式：只读写 ``data_dir / FILENAME``，不抛（IO 失败记 warning 返回空）。
    """
    path = data_dir / FILENAME
    at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    try:
        last = _last_states(path)
        new_lines: list[str] = []
        recorded: list[str] = []
        for flag, on in flags.items():
            prev = last.get(flag)
            if prev is None and not on:
                continue
            if prev == on:
                continue
            # Review fix: distinguish "first seen as on" (the actual flip may predate this startup, as in batch 2)
            # from "changed since the previous record". Post-hoc segmentation can trust only flip; first_seen is just an upper bound.
            kind = "first_seen" if prev is None else "flip"
            new_lines.append(json.dumps({"flag": flag, "on": on, "version": version, "at": at, "kind": kind}, ensure_ascii=False))
            recorded.append(flag)
        if new_lines:
            data_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
        return recorded
    except OSError as e:
        logger.warning("flag_transitions.write_failed path=%s err=%s", path, e)
        return []


__all__ = ["FILENAME", "record_flag_transitions"]
