"""KB 解析收敛：整体时限、卡住判定、失败原因分类与启动恢复。

知识库体验整治 PRD FR1。解析仍跑在网关进程内（FastAPI BackgroundTasks →
KbWorker），进程一重启（每次发版都会）内存里的任务就没了；这里补三件事：

1. **整体时限** `PARSE_TIMEOUT_S`：worker 从领取到 ``done`` 的整段受此约束，
   超时标 ``failed`` + ``timeout``。
2. **卡住判定**：进行中状态且 ``parse_heartbeat_at`` 早于
   ``STUCK_AFTER_S``。每次状态写入都会刷新心跳，活着的解析最迟在时限到点时
   自己写终态，所以「心跳比时限还旧」只可能是进程已死留下的孤儿。
3. **启动恢复**：只处理心跳早于本进程启动时刻的行（本进程写过的行心跳必然更新），
   按 ``parse_attempts`` 设上限，超过标 ``failed`` + ``interrupted``。

单写者前提：生产 compose 的 ``app`` 服务固定 ``container_name``（不能 scale），
``deploy/start-app.sh`` 以单个 uvicorn 进程启动（无 ``--workers``），compose 先停旧
容器再起新容器 ⇒ 启动时库里进行中的行都没有活着的执行者。即便违背该前提，
worker 的领取仍是 SQL 原子状态迁移，同一文件不会被同时解析两次。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.files_store import KbFilesStore


logger = logging.getLogger(__name__)

# Overall parse deadline (claim→done). Heaviest valid path: long scanned PDF within 200MB limit,
# vision concurrency 4, per-page 120s cap; large text sequential embeds in batches of 25, 60s×3 retries per batch.
# 60 minutes covers hundreds of scanned pages while preventing infinite in-progress state.
PARSE_TIMEOUT_S = 60 * 60
# Heartbeat must be 10 minutes older than deadline for stuck: terminal-write / second-truncation margin.
STUCK_AFTER_S = PARSE_TIMEOUT_S + 10 * 60
# Each claim counts a run: initial +1 automatic recovery (PRD Q4). Manual retry clears count.
MAX_PARSE_RUNS = 2

UNFINISHED_PARSE_STATUSES = ("pending", "queued", "parsing", "chunking", "embedding")

ParseErrorCode = Literal[
    "unsupported_format",
    "empty_content",
    "interrupted",
    "timeout",
    "service_unavailable",
    "source_missing",
    "parse_failed",
]

# parse_error stores code:detail; detail logs/audit only, frontend recognizes code only.
PARSE_ERROR_INTERRUPTED = "interrupted"
PARSE_ERROR_TIMEOUT = "timeout"
PARSE_ERROR_SERVICE_UNAVAILABLE = "service_unavailable"
# Previous parse lacked source file; server reprobes original before retry decides enqueue.
PARSE_ERROR_SOURCE_MISSING = "source_missing"

_ERROR_PREFIXES: tuple[tuple[str, ParseErrorCode], ...] = (
    (f"{PARSE_ERROR_INTERRUPTED}:", "interrupted"),
    (f"{PARSE_ERROR_TIMEOUT}:", "timeout"),
    (f"{PARSE_ERROR_SERVICE_UNAVAILABLE}:", "service_unavailable"),
    (f"{PARSE_ERROR_SOURCE_MISSING}:", "source_missing"),
    ("unsupported mime:", "unsupported_format"),
    ("no chunks after split", "empty_content"),
)


def heartbeat_iso(moment: datetime | None = None) -> str:
    """心跳的唯一格式（与 files_store._now 同形），比较靠字符串序，格式必须一致。"""
    return (moment or datetime.now(tz=UTC)).isoformat(timespec="seconds")


def stuck_cutoff(now: datetime | None = None) -> str:
    return heartbeat_iso((now or datetime.now(tz=UTC)) - timedelta(seconds=STUCK_AFTER_S))


def parse_error_code(parse_status: str, parse_error: str | None) -> ParseErrorCode | None:
    """把内部 parse_error 映射到面向用户的原因分类；非失败状态返回 None。"""
    if parse_status != "failed":
        return None
    text = (parse_error or "").strip()
    for prefix, code in _ERROR_PREFIXES:
        if text.startswith(prefix):
            return code
    return "parse_failed"


def is_parse_stuck(parse_status: str, heartbeat_at: str | None, now: datetime | None = None) -> bool:
    if parse_status not in UNFINISHED_PARSE_STATUSES:
        return False
    return heartbeat_at is None or heartbeat_at < stuck_cutoff(now)


class _ParseWorker(Protocol):
    async def parse_and_index_many(self, file_ids: list[str]) -> None: ...


@dataclass
class ParseRecoveryResult:
    requeued: int = 0
    interrupted: int = 0
    unavailable: int = 0
    tasks: list[asyncio.Task[None]] = field(default_factory=list)


async def recover_unfinished_parses(
    files_store: KbFilesStore,
    resolve_worker: Callable[[str], Awaitable[_ParseWorker | None]],
    *,
    started_before: str,
) -> ParseRecoveryResult:
    """启动恢复：把上一个进程遗留的未完成解析重新排队或收敛为失败。

    ``started_before`` 为本进程启动时刻（``heartbeat_iso`` 格式）；只有心跳早于它的
    行才会被动到，本进程已接手的上传与解析不受影响。重新排队的文件交给所属团队的
    worker，经 ``parse_and_index`` 走与上传相同的并发上限和团队解析闸门。
    """
    result = ParseRecoveryResult()
    candidates = await files_store.list_unfinished_parses(heartbeat_before=started_before)
    workers: dict[str, _ParseWorker | None] = {}
    requeued_by_team: dict[str, list[str]] = {}
    for file in candidates:
        if file.team_id not in workers:
            try:
                workers[file.team_id] = await resolve_worker(file.team_id)
            except Exception:
                logger.warning("kb.parse_recovery worker unavailable team=%s", file.team_id, exc_info=True)
                workers[file.team_id] = None
        if workers[file.team_id] is None:
            if await files_store.fail_unfinished_parse(
                file.file_id,
                parse_error=f"{PARSE_ERROR_SERVICE_UNAVAILABLE}: no kb runtime at startup recovery",
                heartbeat_before=started_before,
            ):
                result.unavailable += 1
                emit_audit("kb.parse_failed", {"file_id": file.file_id, "error": PARSE_ERROR_SERVICE_UNAVAILABLE})
            continue
        outcome = await files_store.requeue_interrupted_parse(
            file.file_id,
            max_runs=MAX_PARSE_RUNS,
            heartbeat_before=started_before,
        )
        if outcome == "requeued":
            result.requeued += 1
            requeued_by_team.setdefault(file.team_id, []).append(file.file_id)
            emit_audit("kb.parse_recovered", {"file_id": file.file_id, "previous_status": file.parse_status})
        elif outcome == "interrupted":
            result.interrupted += 1
            emit_audit("kb.parse_failed", {"file_id": file.file_id, "error": PARSE_ERROR_INTERRUPTED})
    for team_id, file_ids in requeued_by_team.items():
        worker = workers[team_id]
        if worker is None:
            continue
        result.tasks.append(
            asyncio.create_task(
                worker.parse_and_index_many(file_ids),
                name=f"kb_parse_recovery:{team_id}",
            )
        )
    return result
