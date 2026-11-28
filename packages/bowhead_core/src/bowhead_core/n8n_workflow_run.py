"""Run one approved n8n workflow and wait for its result, inside one MCP session.

The model never sees ``executionMode``, execution IDs to poll or n8n's ``inputs`` union
(tech design D2). Every run uses the published version: it checks ``activeVersionId``
against the approved one before executing and again after the run. Requests to one n8n
host are paced below n8n's per-IP MCP limit (D5). Results use the envelope of D2 step 6.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote, urlsplit

from bowhead_core.n8n_workflows import N8nWorkflowApproval, n8n_instance_origin

INTERACTIVE_DEADLINE_SECONDS: Final = 300.0
SCHEDULED_DEADLINE_SECONDS: Final = 900.0
HOST_REQUEST_LIMIT: Final = 80
HOST_WINDOW_SECONDS: Final = 300.0
START_BUDGET_WAIT_SECONDS: Final = 5.0
POLL_INTERVALS: Final = (1.0, 2.0, 4.0, 8.0, 15.0)
POLL_INTERVAL_MAX: Final = 30.0
RESULT_ITEM_LIMIT: Final = 200
INLINE_OUTPUT_BYTES: Final = 64 * 1024
MESSAGE_CHARS: Final = 500
TERMINAL_STATUSES: Final = frozenset({"success", "error", "crashed", "canceled"})
# One session costs initialize + its notification; every tool call costs one more request.
_SESSION_COST: Final = 2

OpenSession = Callable[[], AbstractAsyncContextManager[Any]]
DescribeFailure = Callable[[BaseException], dict[str, Any]]


class HostRequestPacer:
    """A sliding window of requests per n8n host, below n8n's limit for one client IP.

    All of Bowhead's calls leave from one address and the application is one process, so
    pacing in process is exact. A caller waits for room up to ``max_wait`` seconds.
    """

    def __init__(
        self,
        limit: int = HOST_REQUEST_LIMIT,
        window: float = HOST_WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def wait_needed(self, host: str, cost: int) -> float:
        """Seconds until ``cost`` more requests fit, without taking them."""
        sent = self._sent.setdefault(host, deque())
        now = self._clock()
        while sent and sent[0] <= now - self._window:
            sent.popleft()
        overflow = len(sent) + cost - self._limit
        if overflow <= 0:
            return 0.0
        if cost > self._limit:
            return float("inf")
        return max(sent[overflow - 1] + self._window - now, 0.0)

    async def acquire(self, host: str, cost: int, max_wait: float) -> bool:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            while True:
                wait = self.wait_needed(host, cost)
                if wait == 0.0:
                    now = self._clock()
                    self._sent[host].extend([now] * cost)
                    return True
                if wait > max_wait:
                    return False
                await self._sleep(wait)
                max_wait -= wait


_DEFAULT_PACER = HostRequestPacer()


def default_pacer() -> HostRequestPacer:
    return _DEFAULT_PACER


class _Done(Exception):
    """Ends a run from inside its session with a finished envelope."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("n8n run finished")
        self.result = result


def _nested(exc: BaseException, depth: int = 0) -> Iterator[BaseException]:
    """``exc`` and what it wraps: the MCP SDK's task groups wrap anything raised in a session."""
    if depth > 6:
        return
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _nested(sub, depth + 1)
        return
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        yield from _nested(cause, depth + 1)


@dataclass
class _RunState:
    phase: str = "check"  # check → execute → poll → collect
    execution_id: str | None = None
    status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    version_changed_to: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def tool_payload(result: Any) -> tuple[bool, dict[str, Any]]:
    """``(is_error, body)`` of an MCP tool result. n8n puts its JSON in structured content and text."""
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return is_error, structured
    text = "\n".join(str(getattr(block, "text", "")) for block in getattr(result, "content", None) or [] if getattr(block, "text", None))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    return is_error, parsed if isinstance(parsed, dict) else {"error": text}


def _clean(message: object) -> str:
    from bowhead_core.agents.prompts.peer_block import sanitize_external_tool_description
    from bowhead_core.scrub import scrub

    text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False, default=str)
    return sanitize_external_tool_description(scrub(text or ""), cap=MESSAGE_CHARS) or "no message"


def execution_url(base_url: str, workflow_id: str, execution_id: str | None) -> str | None:
    if not execution_id:
        return None
    return f"{n8n_instance_origin(base_url)}/workflow/{quote(workflow_id, safe='')}/executions/{quote(execution_id, safe='')}"


def _last_items(data: Mapping[str, Any], node: str) -> list[Any]:
    """The ``json`` of every item the node's last run emitted, over all of its outputs."""
    result = data.get("resultData") if isinstance(data, Mapping) else None
    runs = (result or {}).get("runData", {}).get(node) if isinstance(result, Mapping) else None
    if not isinstance(runs, list) or not runs:
        return []
    last = runs[-1] if isinstance(runs[-1], Mapping) else {}
    outputs = (last.get("data") or {}).get("main") if isinstance(last.get("data"), Mapping) else None
    items: list[Any] = []
    for output in outputs or []:
        for item in output or []:
            if isinstance(item, Mapping) and "json" in item:
                items.append(item["json"])
    return items


def _fit(items: list[Any]) -> tuple[list[Any], bool]:
    """Keep leading items while the JSON stays within the inline cap."""
    kept: list[Any] = []
    size = 2
    for item in items:
        blob = json.dumps(item, ensure_ascii=False, default=str)
        if size + len(blob.encode("utf-8")) + 1 > INLINE_OUTPUT_BYTES:
            return kept, True
        kept.append(item)
        size += len(blob.encode("utf-8")) + 1
    return kept, False


class _Run:
    def __init__(
        self,
        approval: N8nWorkflowApproval,
        arguments: dict[str, Any],
        *,
        source_id: str,
        source_label: str,
        base_url: str,
        deadline_seconds: float,
        pacer: HostRequestPacer,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[Any]],
    ) -> None:
        self.approval = approval
        self.arguments = arguments
        self.source_id = source_id
        self.source_label = source_label
        self.base_url = base_url
        self.host = urlsplit(base_url).netloc.lower()
        self.pacer = pacer
        self.clock = clock
        self.sleep = sleep
        self.start = clock()
        self.deadline = self.start + deadline_seconds
        self.state = _RunState()

    # ── envelope ──────────────────────────────────────────────────────────
    def remaining(self) -> float:
        return max(self.deadline - self.clock(), 0.0)

    def url(self) -> str | None:
        return execution_url(self.base_url, self.approval.workflow_id, self.state.execution_id)

    def envelope(self, **values: Any) -> dict[str, Any]:
        approval = self.approval
        url = self.url()
        result: dict[str, Any] = {
            "status": self.state.status,
            "workflow_id": approval.workflow_id,
            "workflow_name": approval.name,
            "execution_id": self.state.execution_id,
            "execution_url": url,
            "approved_version_id": approval.approved_version_id,
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "source": {
                "kind": "n8n",
                "source_id": self.source_id,
                "source_label": self.source_label,
                "workflow_id": approval.workflow_id,
                "execution_id": self.state.execution_id,
                "execution_url": url,
            },
        }
        result.update(values)
        return result

    def not_run(self, error_kind: str, error: str) -> dict[str, Any]:
        return self.envelope(ok=False, error_kind=error_kind, error=f"Nothing ran. {error}")

    # ── calls ─────────────────────────────────────────────────────────────
    async def pace(self, cost: int, max_wait: float) -> bool:
        return await self.pacer.acquire(self.host, cost, max_wait)

    async def call(self, session: Any, name: str, arguments: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        return tool_payload(await session.call_tool(name, arguments))

    async def details(self, session: Any) -> dict[str, Any] | str:
        """The workflow summary, or n8n's refusal text."""
        is_error, body = await self.call(session, "get_workflow_details", {"workflowId": self.approval.workflow_id, "detailLevel": "execution"})
        workflow = body.get("workflow")
        if is_error or not isinstance(workflow, dict):
            return _clean(body.get("error") or body)
        return workflow

    def unavailable(self, workflow: dict[str, Any] | str) -> str | None:
        name = self.approval.name
        if isinstance(workflow, str):
            return f"n8n refused to read workflow '{name}': {workflow}. Check in n8n that it still exists and is available in MCP."
        if workflow.get("isArchived") is True:
            return f"Workflow '{name}' is archived in n8n."
        if workflow.get("active") is not True or not workflow.get("activeVersionId"):
            return f"Workflow '{name}' has no published version in n8n. Publish it in n8n."
        return None

    # ── phases ────────────────────────────────────────────────────────────
    async def run(self, session: Any) -> dict[str, Any]:
        state = self.state
        if state.phase in ("check", "execute"):
            await self.check_and_execute(session)
        if state.phase == "poll":
            await self.poll(session)
        return await self.collect(session)

    async def check_and_execute(self, session: Any) -> None:
        approval, state = self.approval, self.state
        if not await self.pace(2, self.remaining()):
            raise _Done(self.not_run("rate_limited", "Bowhead's request budget for this n8n server is used up; try again in a few minutes."))
        workflow = await self.details(session)
        reason = self.unavailable(workflow)
        if reason:
            raise _Done(self.not_run("not_available", reason))
        assert isinstance(workflow, dict)
        current = str(workflow.get("activeVersionId"))
        if current != approval.approved_version_id:
            raise _Done(
                self.not_run(
                    "version_changed",
                    f"The published version of n8n workflow '{approval.name}' changed after a team administrator approved it "
                    f"(approved {approval.approved_version_id}, now {current}). A team owner or administrator must review and approve "
                    "the new version under Team settings · Tools · n8n workflows.",
                )
            )
        arguments: dict[str, Any] = {"workflowId": approval.workflow_id, "executionMode": "production", "triggerNodeName": approval.trigger_node}
        if approval.takes_body:
            arguments["inputs"] = {"webhookData": {"body": self.arguments}}
        state.phase = "execute"
        is_error, body = await self.call(session, "execute_workflow", arguments)
        execution_id = body.get("executionId")
        if is_error or body.get("status") != "started" or not isinstance(execution_id, str) or not execution_id:
            raise _Done(self.not_run("workflow_not_started", f"n8n did not start workflow '{approval.name}': {_clean(body.get('error') or body)}"))
        state.execution_id = execution_id
        state.status = "running"
        state.phase = "poll"

    async def poll(self, session: Any) -> None:
        state = self.state
        index = 0
        while True:
            remaining = self.remaining()
            if remaining <= 0:
                minutes = max(round((self.clock() - self.start) / 60), 1)
                raise _Done(
                    self.envelope(
                        ok=True,
                        status="running",
                        note=(
                            f"The workflow is still running in n8n after about {minutes} minute(s). Its result is not available yet. "
                            "Tell the user it is running and give the execution link; do not start it again."
                        ),
                    )
                )
            interval = POLL_INTERVALS[index] if index < len(POLL_INTERVALS) else POLL_INTERVAL_MAX
            index += 1
            await self.sleep(min(interval, remaining))
            if not await self.pace(1, self.remaining()):
                continue
            is_error, body = await self.call(
                session, "get_workflow_execution", {"workflowId": self.approval.workflow_id, "executionId": state.execution_id}
            )
            execution = body.get("execution")
            if is_error or not isinstance(execution, dict):
                raise _Done(
                    self.envelope(
                        ok=False,
                        error_kind="result_unavailable",
                        error=(
                            f"The workflow was started in n8n, but its status could not be read: {_clean(body.get('error') or body)}. "
                            "Do not start it again; give the user the execution link."
                        ),
                    )
                )
            state.status = str(execution.get("status") or "unknown")
            state.started_at = execution.get("startedAt")
            state.finished_at = execution.get("stoppedAt")
            if state.status in TERMINAL_STATUSES:
                state.phase = "collect"
                return

    async def collect(self, session: Any) -> dict[str, Any]:
        approval, state = self.approval, self.state
        await self.pace(3, START_BUDGET_WAIT_SECONDS)
        workflow = await self.details(session)
        changed = isinstance(workflow, dict) and str(workflow.get("activeVersionId")) != approval.approved_version_id
        version_note: dict[str, Any] = {"version_changed_during_run": changed}
        if changed and isinstance(workflow, dict):
            version_note["published_version_id_after_run"] = workflow.get("activeVersionId")
        base = {"workflowId": approval.workflow_id, "executionId": state.execution_id, "includeData": True}
        is_error, body = await self.call(session, "get_workflow_execution", {**base, "nodeNames": []})
        data = body.get("data") if not is_error else None
        result_data = data.get("resultData") if isinstance(data, dict) else None
        result_data = result_data if isinstance(result_data, dict) else {}
        last_node = result_data.get("lastNodeExecuted")
        if state.status != "success":
            failure = result_data.get("error")
            failure = failure if isinstance(failure, dict) else {}
            failed_node = (failure.get("node") or {}).get("name") if isinstance(failure.get("node"), dict) else None
            node = failed_node or last_node
            message = _clean(failure.get("message") or failure.get("description") or "n8n gave no error message")
            where = f" at node '{node}'" if isinstance(node, str) and node else ""
            return self.envelope(
                ok=False,
                error_kind="workflow_failed",
                error=(
                    f"n8n workflow '{approval.name}' ran and ended with status '{state.status}'{where}: {message}. "
                    "Give the user the execution link to see the details in n8n."
                ),
                **version_note,
            )
        items: list[Any] = []
        if isinstance(last_node, str) and last_node:
            is_error, body = await self.call(session, "get_workflow_execution", {**base, "nodeNames": [last_node], "truncateData": RESULT_ITEM_LIMIT})
            if not is_error and isinstance(body.get("data"), dict):
                items = _last_items(body["data"], last_node)
        output, truncated = _fit(items)
        return self.envelope(
            ok=True,
            output=output,
            item_count=len(items),
            truncated=truncated or len(items) >= RESULT_ITEM_LIMIT,
            last_node=last_node,
            **version_note,
        )


async def run_n8n_workflow(
    open_session: OpenSession,
    approval: N8nWorkflowApproval,
    arguments: dict[str, Any],
    *,
    source_id: str,
    source_label: str,
    base_url: str,
    deadline_seconds: float,
    describe_failure: DescribeFailure,
    pacer: HostRequestPacer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    """Run ``approval``'s workflow with ``arguments`` (already validated) and return the envelope.

    ``describe_failure`` maps a transport or sign-in exception to ``{ok, error_kind, error}``
    the same way raw team tools do; this function adds what had happened by then. A 429 on a
    read (details, poll, result) is waited out once when ``Retry-After`` fits the deadline;
    ``execute_workflow`` is never repeated.
    """
    run = _Run(
        approval,
        arguments,
        source_id=source_id,
        source_label=source_label,
        base_url=base_url,
        deadline_seconds=deadline_seconds,
        pacer=pacer or _DEFAULT_PACER,
        clock=clock,
        sleep=sleep,
    )
    if not await run.pacer.acquire(run.host, _SESSION_COST, START_BUDGET_WAIT_SECONDS):
        return run.not_run("rate_limited", "Bowhead's request budget for this n8n server is used up; try again in a few minutes.")
    retried = False
    while True:
        try:
            async with open_session() as session:
                return await run.run(session)
        except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
            done = next((item for item in _nested(exc) if isinstance(item, _Done)), None)
            if done is not None:
                return done.result
            failure = describe_failure(exc)
            retry_after = failure.get("retry_after_seconds")
            phase = run.state.phase
            if (
                failure.get("error_kind") == "rate_limited"
                and phase != "execute"
                and not retried
                and isinstance(retry_after, int)
                and retry_after < run.remaining()
                and await run.pacer.acquire(run.host, _SESSION_COST, run.remaining())
            ):
                retried = True
                await run.sleep(float(retry_after))
                continue
            return _failed(run, failure)


def _failed(run: _Run, failure: dict[str, Any]) -> dict[str, Any]:
    phase = run.state.phase
    error = str(failure.get("error") or "the call failed")
    extra = {key: value for key, value in failure.items() if key not in ("ok", "error", "error_kind")}
    if phase == "check":
        return run.envelope(ok=False, error_kind=failure.get("error_kind", "protocol"), error=f"Nothing ran. {error}", **extra)
    if phase == "execute":
        if failure.get("error_kind") in ("rate_limited", "upstream_auth", "auth_refresh_unavailable"):
            return run.envelope(ok=False, error_kind=failure["error_kind"], error=f"Nothing ran. {error}", **extra)
        return run.envelope(
            ok=False,
            error_kind=failure.get("error_kind", "protocol"),
            error=(
                f"Whether n8n started the workflow is unknown: {error} "
                "Do not run it again automatically; ask the user to check the workflow's executions in n8n."
            ),
            **extra,
        )
    return run.envelope(
        ok=False,
        error_kind="result_unavailable",
        error=f"The workflow was started in n8n, but its result could not be read: {error} Do not start it again; give the user the execution link.",
        **extra,
    )
