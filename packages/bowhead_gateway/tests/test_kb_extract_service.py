"""Unit tests for the shared per-row extraction trigger (kb-management D5).

Covers the rate-limit gate, idempotency (force vs not), and enqueue — logic that
was previously inlined in kb_admin.trigger_extract_rows with thin coverage.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_gateway.routes.kb_extract_service import trigger_row_extraction


def _file(**over):
    base = {"file_id": "f1", "doc_type_id": "dt_1", "auto_classified_doc_type_id": None, "workspace_id": "ws1"}
    base.update(over)
    return SimpleNamespace(**base)


class _UserStore:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def rate_limit_check(self, key, *, limit, window_s):
        self.calls.append(key)
        return self.results.pop(0) if self.results else True


class _JobsStore:
    def __init__(self, latest=None):
        self.latest = latest
        self.marked = []

    async def get_latest_for_file(self, file_id):
        return self.latest

    async def mark_status(self, job_id, status):
        self.marked.append((job_id, status))


class _RowsStore:
    def __init__(self):
        self.deleted = []

    async def soft_delete_for_attempt(self, *, file_id, doc_type_id, workspace_id, attempt_id_lt):
        self.deleted.append((file_id, doc_type_id, attempt_id_lt))
        return 0


class _Worker:
    def __init__(self):
        self.indexed = []

    async def parse_and_index(self, file_id, *, allow_terminal=False):
        self.indexed.append(file_id)


async def _run(**over):
    deps = {
        "file": _file(),
        "ws_id": "ws1",
        "force": False,
        "rows_store": _RowsStore(),
        "jobs_store": _JobsStore(),
        "user_store": _UserStore(),
        "worker": _Worker(),
        "active_tasks": {},
        "actor_user_id": "u1",
    }
    deps.update(over)
    res = await trigger_row_extraction(**deps)
    await asyncio.sleep(0)  # let the fire-and-forget create_task run
    return res, deps


async def test_no_doc_type_short_circuits() -> None:
    res, deps = await _run(file=_file(doc_type_id=None, auto_classified_doc_type_id=None))
    assert res["status"] == "no_doc_type"
    assert deps["worker"].indexed == []


async def test_rate_limited_per_file() -> None:
    res, deps = await _run(user_store=_UserStore([False]))
    assert res["status"] == "rate_limited_file"
    assert deps["worker"].indexed == []


async def test_rate_limited_per_ws() -> None:
    res, deps = await _run(user_store=_UserStore([True, False]))
    assert res["status"] == "rate_limited_ws"
    assert deps["worker"].indexed == []


async def test_fresh_file_queues_and_enqueues() -> None:
    res, deps = await _run()
    assert res["status"] == "queued"
    assert res["attempt_id"] == 1
    assert deps["worker"].indexed == ["f1"]


async def test_idempotent_already_in_progress() -> None:
    job = SimpleNamespace(status="running", attempt_id=1, job_id="j1")
    res, deps = await _run(jobs_store=_JobsStore(latest=job), force=False)
    assert res["status"] == "already_in_progress"
    assert deps["worker"].indexed == []


async def test_idempotent_already_succeeded() -> None:
    job = SimpleNamespace(status="succeeded", attempt_id=2, job_id="j2")
    res, deps = await _run(jobs_store=_JobsStore(latest=job), force=False)
    assert res["status"] == "already_succeeded"
    assert deps["worker"].indexed == []


async def test_force_reextracts_over_succeeded() -> None:
    job = SimpleNamespace(status="succeeded", attempt_id=2, job_id="j2")
    jobs = _JobsStore(latest=job)
    res, deps = await _run(jobs_store=jobs, force=True)
    assert res["status"] == "queued"
    assert res["attempt_id"] == 3  # latest.attempt_id + 1
    assert deps["worker"].indexed == ["f1"]
    assert ("j2", "cancelled") in jobs.marked


async def test_auto_classified_takes_precedence_for_dt_id() -> None:
    # service uses auto_classified_doc_type_id or doc_type_id for soft-delete targeting
    rows = _RowsStore()
    await _run(file=_file(doc_type_id="dt_explicit", auto_classified_doc_type_id="dt_auto"), rows_store=rows)
    assert rows.deleted and rows.deleted[0][1] == "dt_auto"
