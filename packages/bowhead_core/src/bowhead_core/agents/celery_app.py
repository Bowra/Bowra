"""Celery app singleton + task signature (v2 PR-#1).

App config via env:
- BOWHEAD_REDIS_URL: broker + backend URL (default redis://localhost:6379/0)
- BOWHEAD_CELERY_TASK_QUEUE: queue name (default "bowhead.agent")

Worker entry: `bowhead-agent-worker` (defined in pyproject.scripts when celery dep
ships). Worker process runs:
    celery -A bowhead_core.agents.celery_app worker -Q bowhead.agent

`run_task_celery` 任务体 (worker side) reconstruct RuntimeTurnContext + 调
agent_runner_fn — 但因 fn 不可 pickle, worker 必须有自己的 runner_factory
(通过 worker init signal 装载 entry-point bootstrap).

此模块 lazy import celery — 如未安装 celery, get_celery_app() raise ImportError.
单测 inject 自己的 dispatch_fn, 不依赖此 app.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE = "bowhead.agent"
_DEFAULT_BROKER = "redis://localhost:6379/0"

_app: Any = None


def get_broker_url() -> str:
    return os.environ.get("BOWHEAD_REDIS_URL", _DEFAULT_BROKER)


def get_queue_name() -> str:
    return os.environ.get("BOWHEAD_CELERY_TASK_QUEUE", _DEFAULT_QUEUE)


def get_celery_app() -> Any:
    """Return singleton Celery app. Raises ImportError if celery not installed."""
    global _app  # noqa: PLW0603
    if _app is not None:
        return _app
    try:
        from celery import Celery
    except ImportError as e:
        raise ImportError(
            "celery not installed; pip install 'bowhead-core[celery]' or set BOWHEAD_TASK_RUNNER=local",
        ) from e

    broker = get_broker_url()
    queue = get_queue_name()
    _app = Celery(
        "bowhead_agent",
        broker=broker,
        backend=broker,
    )
    _app.conf.update(
        task_default_queue=queue,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Ensure we don't lose task on worker crash (re-queue)
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # Result expiry (1h — picked up via polling) + cleanup task
        result_expires=3600,
    )
    return _app
