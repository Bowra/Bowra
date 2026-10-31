"""Live read-only Datarc data Tools in Agent evaluation (``FEATURE_AGENT_EVAL_DATARC_READ_ONLY``).

Design: ``docs/design/agent-eval-datarc-read-only.md``. With the switch on, an evaluation attempt of a Team
Agent runs ``bi_query``, ``bi_schema`` and ``bi_concept_resolve`` through their **production handlers**
against the Team's Datarc connection, with these guarantees:

- **Identity.** The Agent's current Owner, only when the Owner started the run, re-checked at every
  attempt (the gateway resolver, §3). The member is never empty: an empty member turns the term
  permission filter off, so ``ResolvedDatarcIdentity`` refuses one.
- **Read only.** The handlers get ``EvalDatarcReader``/``EvalVectorReader`` proxies that forward only the
  allowlisted methods (§5.2); everything else raises ``AttributeError``. Row results go to the attempt
  scratch store, never the Space artifact store (§5.4).
- **Evidence.** Counts, times, outcome, cause, reference, Datarc's ``request_id`` and two keyed digests per
  row query (§6). Never rows, query text, member or connection identifiers.
- **Failures.** Environment causes end the attempt with ``agent_eval_datarc_unavailable`` and a cause;
  connection and identity causes latch for the run (§7).

The production ``ToolSpec``s are not touched: this contract table is evaluation-only (§4.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from bowhead_core.agent_eval_attempt_failure import DATARC_CAUSES as _ATTEMPT_DATARC_CAUSES
from bowhead_core.agent_eval_attempt_failure import DATARC_UNAVAILABLE_CODE
from bowhead_core.agent_eval_effects import (
    CALL_RECORDED,
    DATARC_LIVE_ROUTE,
    DATARC_UNAVAILABLE_DENIAL,
    AgentEvalEffectError,
    DatarcToolRaised,
    EvalTraceCollector,
)
from bowhead_core.agent_eval_scratch_store import EvalScratchArtifactStore, EvalScratchStore

if TYPE_CHECKING:
    from bowhead_core.agent_eval_store import EvalRun
    from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec

logger = logging.getLogger(__name__)

FEATURE_FLAG = "FEATURE_AGENT_EVAL_DATARC_READ_ONLY"


def datarc_live_enabled() -> bool:
    """Default on; an explicit ``0`` disables the live Datarc route (rollout register)."""

    return os.environ.get(FEATURE_FLAG, "1").strip() == "1"


# §4.1: exact registry Tool names -> the contract version the reader, evidence format and allowlist
# implement together. A Tool joins only by a reviewed entry here plus its allowlist entries.
EVAL_READ_ONLY_LIVE_CONTRACTS: Mapping[str, str] = {
    "bi_query": "datarc.read_only_live@1",
    "bi_schema": "datarc.read_only_live@1",
    "bi_concept_resolve": "datarc.read_only_live@1",
}
# Effect modes in which a read of external state may run (§4.2); fake_only admits only exact fakes.
_LIVE_MODES = frozenset({"no_effects", "read_only_fixture", "sandbox"})

# Denials the Agent may receive on this route (besides DATARC_UNAVAILABLE_DENIAL).
DATARC_BUDGET_DENIAL = "eval_datarc_budget_exceeded"
# The attempt's failure code (DATARC_UNAVAILABLE_CODE) and its §11.3 category.
DATARC_FAILURE_CATEGORY = "data_connection"

# §7.1 causes.
RUN_LEVEL_CAUSES = frozenset(
    {
        "no_connection",
        "connection_unusable",
        "connection_rejected",
        "starter_not_owner",
        "owner_identity_unverified",
        "no_workspace",
        "owner_not_member",
    }
)
RETRYABLE_CAUSES = frozenset({"datarc_unreachable", "datarc_busy", "datarc_timeout"})
OWNER_PERMISSION_DENIED = "owner_permission_denied"
DATARC_CAUSES = RUN_LEVEL_CAUSES | RETRYABLE_CAUSES | {OWNER_PERMISSION_DENIED}
if DATARC_CAUSES != _ATTEMPT_DATARC_CAUSES:  # pragma: no cover - import-time contract check
    raise RuntimeError("Datarc evaluation causes and attempt-failure causes differ")

# §4.3 operation budget per attempt.
MAX_QUERY_OPERATIONS = 40
MAX_OPERATIONS = 120
# §4.4 evaluation limiter: concurrent Datarc operations per connection (per process) and per run.
CONNECTION_CONCURRENCY = 4
RUN_CONCURRENCY = 2
# §4.5 per-call timeouts.
PRODUCTION_TIMEOUT_S = 60.0
DEADLINE_RESERVE_S = 5.0

# §6.1 digest key purpose, derived from BOWHEAD_FERNET_KEY.
_DIGEST_PURPOSE = b"agent-eval-datarc-evidence"

# §5.2 allowlist: DatarcClient method -> evidence operation name. ``get_workspace`` (GET /workspaces/{ws}/,
# a read) is not in the design table: bi_query's display-name lookup reads it through the shared schema cache,
# and the recording-fake allowlist test found it.
DATARC_READER_OPERATIONS: Mapping[str, str] = {
    "get_workspace": "schema",
    "list_measures": "schema",
    "list_dimensions": "schema",
    "list_models": "schema",
    "get_model": "schema",
    "cut_question": "cut",
    "recall_keywords": "recall",
    "check_token_permissions": "permissions",
    "download_data": "query",
}
# Client-memory metadata reads: no Datarc request, so no operation and no slot.
DATARC_READER_MEMORY_METHODS = frozenset({"measure_metadata", "dimension_metadata"})
DATARC_READER_METHODS = frozenset(DATARC_READER_OPERATIONS) | DATARC_READER_MEMORY_METHODS
VECTOR_READER_METHODS = frozenset({"query"})
# Data attributes the embed recall reads (no request); ``datarc_tenant`` names the schema collection.
VECTOR_READER_ATTRIBUTES = frozenset({"datarc_tenant"})

# §5.2 "Excluded": methods the proxies never forward. Kept as a list so the test can enumerate them.
EXCLUDED_DATARC_METHODS: tuple[str, ...] = (
    # Writes and identity.
    "create_account",
    "get_sso_auth_data",
    "fetch_account",
    "fetch_workspace_member",
    "lookup_user_by_email",
    "list_account_projects",
    # Datarc model work.
    "call_llm_async",
    "get_llm_result",
    "plan_query",
    # Legacy question paths.
    "ask_question",
    "get_card",
    "get_cards",
    "attribute",
    "explain",
    "list_guide_questions",
    # Connection-level reads no data Tool needs.
    "get_scope",
    "list_workspaces",
    "aclose",
)
EXCLUDED_VECTOR_METHODS: tuple[str, ...] = (
    "create_collection",
    "delete_collection",
    "rename_collection",
    "add",
    "add_documents",
    "upsert",
    "update",
    "update_documents",
    "delete",
    "delete_documents",
    "health",
    "list_collections",
    "get_collection",
)

# Primary operations of each Tool: when the last attempt of one of these failed for an environment cause,
# the call failed for that cause. Secondary operations (cut transparency, the fail-open permission check,
# display names) are best effort in the Tool itself.
_PRIMARY_OPERATIONS: Mapping[str, frozenset[str]] = {
    "bi_query": frozenset({"query"}),
    "bi_schema": frozenset({"schema"}),
    "bi_concept_resolve": frozenset({"recall"}),
}
# Environment outcomes, most specific first.
_ENVIRONMENT_OUTCOMES: tuple[str, ...] = ("connection_rejected", "datarc_timeout", "datarc_busy", "datarc_unreachable")


class EvalDatarcUnavailable(Exception):
    """An environment cause the Owner can act on (§7.1); never carries Datarc's message text."""

    def __init__(self, cause: str, *, workspace: str | None = None, owner_name: str | None = None) -> None:
        if cause not in DATARC_CAUSES:
            raise ValueError("unknown Datarc evaluation cause")
        super().__init__(cause)
        self.cause = cause
        self.workspace = workspace
        self.owner_name = owner_name


class _DatarcBudgetExceeded(RuntimeError):
    """The attempt's Datarc operation budget is spent; raised inside the Tool, never contacting Datarc."""

    def __init__(self) -> None:
        super().__init__(DATARC_BUDGET_DENIAL)


class _DatarcDeadline(RuntimeError):
    """Too little attempt time is left to start (or wait for) a Datarc operation."""

    def __init__(self) -> None:
        super().__init__("eval_datarc_deadline")


@dataclass(frozen=True, slots=True)
class ResolvedDatarcIdentity:
    """What the gateway resolver hands the route: the Team connection's clients and the Owner's member.

    The clients are the pooled production clients. The route never puts them on a context; Tools see
    them only through the allowlisted proxies.
    """

    client: Any = field(repr=False)
    vector_client: Any | None = field(repr=False)
    member_uid: str = field(repr=False)
    workspace_id: str = ""
    workspace_name: str = ""

    def __post_init__(self) -> None:
        # An empty member turns Datarc's term permission filter off (datarc_permissions.check_tokens).
        if not isinstance(self.member_uid, str) or not self.member_uid.strip():
            raise ValueError("Datarc evaluation identity has no member")
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("Datarc evaluation identity has no workspace")
        if self.client is None:
            raise ValueError("Datarc evaluation identity has no client")


class EvalDatarcIdentityResolver(Protocol):
    """Resolve the attempt's Datarc identity now, or raise ``EvalDatarcUnavailable`` with the cause."""

    async def resolve(self, run: EvalRun) -> ResolvedDatarcIdentity: ...


@dataclass(frozen=True, slots=True)
class EvalDatarcBinding:
    """``ToolExecutionContext.eval_datarc``: the attempt's Datarc access, as the Tools see it."""

    contract: str
    workspace_id: str
    reader: EvalDatarcReader = field(repr=False)
    vector_reader: EvalVectorReader | None = field(repr=False)


def derive_evidence_key(root_secret: str | bytes | None) -> bytes | None:
    """The §6.1 digest key for the fixed purpose, from ``BOWHEAD_FERNET_KEY``; None without a root."""

    if not root_secret:
        return None
    seed = root_secret.encode("utf-8") if isinstance(root_secret, str) else root_secret
    return hmac.new(seed, _DIGEST_PURPOSE, hashlib.sha256).digest()


def _digest(key: bytes, value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    return "hmac-sha256:" + hmac.new(key, encoded.encode("utf-8"), hashlib.sha256).hexdigest()


def semantics_digest(key: bytes, *, workspace_id: str, explain_json: Mapping[str, Any] | None) -> str | None:
    """Keyed digest of Datarc's applied semantics with the workspace; None when Datarc reported none."""

    if not isinstance(explain_json, Mapping) or not explain_json:
        return None
    return _digest(key, {"explain": explain_json, "workspace": str(workspace_id)})


def result_digest(key: bytes, *, columns: Iterable[Any], rows: Iterable[Mapping[str, Any]]) -> str:
    """Keyed digest of the canonical full result: sorted columns and every row, in a canonical order."""

    ordered = sorted(str(column) for column in columns)
    canonical_rows = sorted(json.dumps([_canonical_cell(row.get(column)) for column in ordered], ensure_ascii=False, default=str) for row in rows)
    return _digest(key, {"columns": ordered, "rows": canonical_rows})


def _canonical_cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── Evaluation limiter (§4.4) ──────────────────────────────────────────────────
# One semaphore per pooled connection client: the pool keeps one client per (base_url, key), so the client
# object is the connection's non-reversible pool identity. Weak keys drop entries with the client.
_CONNECTION_SLOTS: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _connection_slots(client: Any) -> asyncio.Semaphore:
    slots = _CONNECTION_SLOTS.get(client)
    if slots is None:
        slots = asyncio.Semaphore(CONNECTION_CONCURRENCY)
        _CONNECTION_SLOTS[client] = slots
    return slots


# ── Call ledger (§5.3) ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class _CallScope:
    index: int
    tool: str
    budget_hit: bool = False
    deadline_hit: bool = False


_CURRENT_CALL: contextvars.ContextVar[_CallScope | None] = contextvars.ContextVar("agent_eval_datarc_call", default=None)


@dataclass(slots=True)
class _LedgerEntry:
    call_index: int
    tool: str
    op: str
    method: str
    requested_at: str
    completed_at: str | None = None
    outcome: str = "pending"
    # Memory only: which query of a batch this is (a digest of its text, never stored) and the exception.
    query_key: str | None = None
    error_class: str | None = None
    row_count: int | None = None
    rows_shown: int | None = None
    datarc_completed_at: str | None = None
    semantics_digest: str | None = None
    result_digest: str | None = None
    request_id: str | None = None
    scratch_store_rejected: bool = False

    def evidence(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "tool": self.tool,
            "op": self.op,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome,
        }
        if self.op == "query":
            for key in ("datarc_completed_at", "row_count", "rows_shown", "semantics_digest", "result_digest", "request_id"):
                value = getattr(self, key)
                if value not in (None, ""):
                    document[key] = value
            if self.scratch_store_rejected:
                document["scratch_store_rejected"] = True
        return document


def _failure_outcome(exc: BaseException) -> str:
    """The ledger outcome of a failed operation, from typed facts only (never message text)."""

    from bowhead_bi_bridge.errors import BIAsyncTaskTimeout, BIConnectionError, BIPermissionError, DatarcError

    if isinstance(exc, BIPermissionError):
        return "connection_rejected" if getattr(exc, "source", None) == "http" else "member_denied"
    if isinstance(exc, DatarcError) and getattr(exc, "throttled", False):
        return "datarc_busy"
    if isinstance(exc, BIAsyncTaskTimeout):
        return "datarc_timeout"
    if isinstance(exc, BIConnectionError):
        return "datarc_unreachable"
    return "agent_error"


# ── Proxies (§5.2) ─────────────────────────────────────────────────────────────
class EvalDatarcReader:
    """Forwards only the allowlisted ``DatarcClient`` reads; any other attribute raises ``AttributeError``."""

    __slots__ = ("_client", "_route")

    def __init__(self, client: Any, route: EvalDatarcLiveRoute) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_route", route)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(name)

    def __getattr__(self, name: str) -> Any:
        # Only names outside the allowlist reach here (allowed ones are defined below).
        if not name.startswith("__"):
            self._route.record_refused(name)
        raise AttributeError(name)

    async def get_workspace(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("get_workspace", self._client.get_workspace, args, kwargs)

    async def list_measures(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("list_measures", self._client.list_measures, args, kwargs)

    async def list_dimensions(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("list_dimensions", self._client.list_dimensions, args, kwargs)

    async def list_models(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("list_models", self._client.list_models, args, kwargs)

    async def get_model(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("get_model", self._client.get_model, args, kwargs)

    async def cut_question(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("cut_question", self._client.cut_question, args, kwargs)

    async def recall_keywords(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("recall_keywords", self._client.recall_keywords, args, kwargs)

    async def check_token_permissions(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("check_token_permissions", self._client.check_token_permissions, args, kwargs)

    async def download_data(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("download_data", self._client.download_data, args, kwargs)

    def measure_metadata(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.measure_metadata(*args, **kwargs)

    def dimension_metadata(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.dimension_metadata(*args, **kwargs)


class EvalVectorReader:
    """Forwards only the embed ``query`` read for the schema collection, plus the tenant it names."""

    __slots__ = ("_client", "_route")

    def __init__(self, client: Any, route: EvalDatarcLiveRoute) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_route", route)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(name)

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("__"):
            self._route.record_refused(f"vector.{name}")
        raise AttributeError(name)

    @property
    def datarc_tenant(self) -> Any:
        return getattr(self._client, "datarc_tenant", None)

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        return await self._route.operation("vector_query", self._client.query, args, kwargs, op="recall")


# ── Run scope and attempt route ───────────────────────────────────────────────
@dataclass(slots=True)
class EvalDatarcLiveRun:
    """One evaluation run's Datarc scope: the identity resolver, the run's slots and its latched cause."""

    resolver: EvalDatarcIdentityResolver
    run: EvalRun
    digest_key: bytes | None
    run_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(RUN_CONCURRENCY))
    # A run-level cause (connection, identity) stops later attempts at their first Datarc call.
    latched: EvalDatarcUnavailable | None = None

    def open_attempt(self, *, reference: str, remaining_ms: Callable[[], int]) -> EvalDatarcLiveRoute:
        return EvalDatarcLiveRoute(run_scope=self, reference=reference, remaining_ms=remaining_ms)

    def latch(self, failure: EvalDatarcUnavailable) -> None:
        if failure.cause in RUN_LEVEL_CAUSES and self.latched is None:
            self.latched = failure


@dataclass(slots=True)
class _Stop:
    kind: str  # "unavailable" | "time_limit"
    cause: str | None = None
    tool: str | None = None
    workspace: str | None = None
    owner_name: str | None = None


@dataclass(slots=True)
class EvalDatarcLiveRoute:
    """One attempt's ``datarc_read_only_live`` route (§4.2): binding, ledger, budget, slots and store."""

    run_scope: EvalDatarcLiveRun
    reference: str
    remaining_ms: Callable[[], int]
    scratch: EvalScratchStore = field(default_factory=EvalScratchStore)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop: _Stop | None = None
    _identity: ResolvedDatarcIdentity | None = None
    _binding: EvalDatarcBinding | None = None
    _identity_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ledger: list[_LedgerEntry] = field(default_factory=list)
    # (tool, outcome) per Agent-visible call on this route.
    _calls: list[tuple[str, str]] = field(default_factory=list)
    _operations: int = 0
    _query_operations: int = 0
    _throttled_queries: set[str] = field(default_factory=set)
    _refused: list[str] = field(default_factory=list)
    _turn_ids: set[str] = field(default_factory=set)
    _bi_outage: Any = None
    # The attempt's own schema TTL cache, as chat has the gateway's: without it every query re-reads the
    # schema for display names. Schema metadata only, never rows; it ends with the attempt.
    _schema_cache: Any = None

    def handles(self, tool_name: str, mode: object) -> bool:
        return tool_name in EVAL_READ_ONLY_LIVE_CONTRACTS and mode in _LIVE_MODES

    # ── the Agent-visible call ──
    async def run(
        self,
        *,
        tool_name: str,
        original: ToolSpec,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
        trace: EvalTraceCollector,
        effect_class: str,
        started: float,
        approval_attempted: bool,
    ) -> Any:
        if self.stop is not None:
            raise AgentEvalEffectError(DATARC_UNAVAILABLE_DENIAL)
        if self._operations >= MAX_OPERATIONS or self._query_operations + _query_count(tool_name, arguments) > MAX_QUERY_OPERATIONS:
            trace.tool_budget_exceeded = True
            raise AgentEvalEffectError(DATARC_BUDGET_DENIAL)
        binding = await self._bind(tool_name)
        call_ctx = self._call_context(ctx, binding)
        scope = _CallScope(index=len(self._calls), tool=tool_name)
        self._calls.append((tool_name, "pending"))
        token = _CURRENT_CALL.set(scope)
        raised: Exception | None = None
        result: Any = None
        try:
            result = await original.handler(arguments, call_ctx)
        except Exception as exc:  # the Tool's own failure; chat shows it the same way
            raised = exc
        finally:
            _CURRENT_CALL.reset(token)
            self._turn_ids.add(str(getattr(call_ctx, "turn_id", "") or ""))
        if scope.deadline_hit:
            self._calls[scope.index] = (tool_name, "time_limit")
            raise AgentEvalEffectError(DATARC_UNAVAILABLE_DENIAL)
        if scope.budget_hit:
            self._calls[scope.index] = (tool_name, "budget_exceeded")
            trace.tool_budget_exceeded = True
            raise AgentEvalEffectError(DATARC_BUDGET_DENIAL)
        cause = self._environment_cause(scope)
        if cause is not None:
            self._calls[scope.index] = (tool_name, cause)
            failure = EvalDatarcUnavailable(cause)
            self.run_scope.latch(failure)
            self._halt(_Stop(kind="unavailable", cause=cause, tool=tool_name))
            raise AgentEvalEffectError(DATARC_UNAVAILABLE_DENIAL)
        self._calls[scope.index] = (tool_name, self._call_outcome(scope))
        if raised is None and tool_name == "bi_query":
            self._record_rows_shown(scope, result)
        numbers: set[float] = set()
        if raised is None:
            from bowhead_core.number_verifier import extract_numbers

            extract_numbers(result, numbers)
        trace.record(
            tool_name=tool_name,
            arguments=arguments,
            effect_class=effect_class,
            route=DATARC_LIVE_ROUTE,
            duration_ms=_elapsed_ms(started),
            evidence_ref=f"datarc:{self.reference}:{scope.index}"[:200],
            approval_attempted=approval_attempted,
            authorized_numbers=tuple(sorted(n for n in numbers if math.isfinite(n))[:10_000]),
        )
        recorded = CALL_RECORDED.get()
        if recorded is not None:
            recorded[0] = True
        if raised is not None:
            raise DatarcToolRaised(raised)
        return result

    async def _bind(self, tool_name: str) -> EvalDatarcBinding:
        async with self._identity_lock:
            if self._binding is not None:
                return self._binding
            latched = self.run_scope.latched
            if latched is not None:
                self._halt(_Stop(kind="unavailable", cause=latched.cause, tool=tool_name, workspace=latched.workspace, owner_name=latched.owner_name))
                raise AgentEvalEffectError(DATARC_UNAVAILABLE_DENIAL)
            try:
                identity = await self.run_scope.resolver.resolve(self.run_scope.run)
            except EvalDatarcUnavailable as failure:
                logger.warning(
                    "agent_eval.datarc_unavailable reference=%s cause=%s tool=%s",
                    self.reference,
                    failure.cause,
                    tool_name,
                )
                self.run_scope.latch(failure)
                self._halt(_Stop(kind="unavailable", cause=failure.cause, tool=tool_name, workspace=failure.workspace, owner_name=failure.owner_name))
                raise AgentEvalEffectError(DATARC_UNAVAILABLE_DENIAL) from None
            if not isinstance(identity, ResolvedDatarcIdentity):
                raise AgentEvalEffectError("eval_effect_adapter_invalid")
            from bowhead_core.datarc_schema_cache import DatarcSchemaCache
            from bowhead_core.tool_registry import BIOutageLatch

            self._identity = identity
            self._bi_outage = BIOutageLatch()
            self._schema_cache = DatarcSchemaCache()
            self._binding = EvalDatarcBinding(
                contract=EVAL_READ_ONLY_LIVE_CONTRACTS[tool_name],
                workspace_id=identity.workspace_id,
                reader=EvalDatarcReader(identity.client, self),
                vector_reader=EvalVectorReader(identity.vector_client, self) if identity.vector_client is not None else None,
            )
            return self._binding

    def _call_context(self, ctx: ToolExecutionContext, binding: EvalDatarcBinding) -> ToolExecutionContext:
        """A copy of the evaluation context carrying the binding (§5.1); never the pooled client."""

        identity = self._identity
        assert identity is not None
        call_ctx = copy.copy(ctx)
        call_ctx.datarc_run_state = None
        call_ctx.datarc_execution_scope = None
        call_ctx.datarc_binding = None
        call_ctx.datarc = binding.reader  # type: ignore[assignment]
        call_ctx.vector_client = binding.vector_reader
        call_ctx.member_uid = identity.member_uid
        call_ctx.active_datarc_project = identity.workspace_id
        call_ctx.active_datarc_workspace_name = identity.workspace_name
        call_ctx.artifact_store = EvalScratchArtifactStore(self.scratch, on_rejected=self._scratch_rejected)
        call_ctx.bi_outage = self._bi_outage
        call_ctx.schema_cache = self._schema_cache
        call_ctx.eval_datarc = binding
        return call_ctx

    # ── one Datarc operation, from inside a Tool ──
    async def operation(
        self,
        method: str,
        fn: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        op: str | None = None,
    ) -> Any:
        scope = _CURRENT_CALL.get()
        tool = scope.tool if scope is not None else "unknown"
        operation = op or DATARC_READER_OPERATIONS[method]
        query_key = _query_key(args, kwargs) if method == "download_data" else None
        # The Tool's own throttle retries are not extra operations (§4.3).
        counted = not (query_key is not None and query_key in self._throttled_queries)
        if counted:
            if self._operations >= MAX_OPERATIONS or (operation == "query" and self._query_operations >= MAX_QUERY_OPERATIONS):
                if scope is not None:
                    scope.budget_hit = True
                raise _DatarcBudgetExceeded
            self._operations += 1
            if operation == "query":
                self._query_operations += 1
        identity = self._identity
        client = identity.client if identity is not None else None
        try:
            async with self._slot(client):
                budget_s = self._time_left_s()
                if method == "download_data":
                    # The export task's poll window: the production 60 s, or what the attempt has left.
                    kwargs = {**kwargs, "timeout_sec": min(float(kwargs.get("timeout_sec", PRODUCTION_TIMEOUT_S)), budget_s)}
                entry = _LedgerEntry(
                    call_index=scope.index if scope is not None else -1,
                    tool=tool,
                    op=operation,
                    method=method,
                    requested_at=_now(),
                    query_key=query_key,
                )
                self._ledger.append(entry)
                try:
                    result = await asyncio.wait_for(fn(*args, **kwargs), timeout=min(budget_s, _outer_timeout(method)))
                except TimeoutError:
                    entry.completed_at = _now()
                    entry.outcome = "cancelled"
                    if self._time_left_s() <= 0:
                        raise _DatarcDeadline from None
                    entry.outcome = "datarc_unreachable"
                    raise
                except asyncio.CancelledError:
                    entry.completed_at = _now()
                    entry.outcome = "cancelled"
                    raise
                except Exception as exc:
                    entry.completed_at = _now()
                    entry.outcome = _failure_outcome(exc)
                    entry.error_class = type(exc).__name__
                    if entry.outcome == "datarc_busy" and query_key is not None:
                        self._throttled_queries.add(query_key)
                    logger.info(
                        "agent_eval.datarc_operation_failed reference=%s tool=%s op=%s outcome=%s err_type=%s",
                        self.reference,
                        tool,
                        operation,
                        entry.outcome,
                        entry.error_class,
                    )
                    raise
                entry.completed_at = _now()
                if query_key is not None:
                    self._throttled_queries.discard(query_key)
                    self._record_query_result(entry, result)
                else:
                    entry.outcome = "ok"
                return result
        except _DatarcDeadline:
            if scope is not None:
                scope.deadline_hit = True
            self._halt(_Stop(kind="time_limit", tool=tool))
            raise

    @contextlib.asynccontextmanager
    async def _slot(self, client: Any):
        """At most 2 Datarc operations per run and 4 per connection; waiting counts against the deadline."""

        connection = _connection_slots(client) if client is not None else None
        acquired: list[asyncio.Semaphore] = []
        try:
            for slots in (self.run_scope.run_slots, connection):
                if slots is None:
                    continue
                wait_s = self._time_left_s()
                if wait_s <= 0:
                    raise _DatarcDeadline
                try:
                    await asyncio.wait_for(slots.acquire(), timeout=wait_s)
                except TimeoutError:
                    raise _DatarcDeadline from None
                acquired.append(slots)
            if self._time_left_s() <= 0:
                raise _DatarcDeadline
            yield
        finally:
            for slots in reversed(acquired):
                slots.release()

    def _time_left_s(self) -> float:
        return self.remaining_ms() / 1000 - DEADLINE_RESERVE_S

    def _record_query_result(self, entry: _LedgerEntry, result: Any) -> None:
        rows = list(getattr(result, "rows", None) or [])
        columns = list(getattr(result, "columns", None) or [])
        total = getattr(result, "total_rows", None)
        entry.row_count = int(total) if isinstance(total, int) and not isinstance(total, bool) else len(rows)
        entry.outcome = "empty" if entry.row_count == 0 else "ok"
        completed = getattr(result, "datarc_completed_at", "")
        entry.datarc_completed_at = str(completed)[:64] if isinstance(completed, str) and completed else None
        request_id = getattr(result, "request_id", "")
        entry.request_id = str(request_id)[:128] if isinstance(request_id, str) and request_id else None
        key = self.run_scope.digest_key
        if key is not None and self._identity is not None:
            explain = getattr(result, "explain_json", None)
            entry.semantics_digest = semantics_digest(
                key, workspace_id=self._identity.workspace_id, explain_json=explain if isinstance(explain, Mapping) else None
            )
            entry.result_digest = result_digest(key, columns=columns, rows=[row for row in rows if isinstance(row, Mapping)])

    def _record_rows_shown(self, scope: _CallScope, result: Any) -> None:
        """How many rows the Agent saw inline, per row query of this call (matched in memory, never stored)."""

        queries = [entry for entry in self._ledger if entry.call_index == scope.index and entry.op == "query" and entry.outcome in {"ok", "empty"}]
        if not queries or not isinstance(result, Mapping):
            return
        items = result.get("results")
        if not isinstance(items, list):
            shown = result.get("rows_shown")
            if isinstance(shown, int) and not isinstance(shown, bool):
                queries[-1].rows_shown = shown
            return
        shown_by_key: dict[str, int] = {}
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("query_text"), str):
                sample = item.get("sample_rows")
                shown_by_key[_text_key(str(item["query_text"]))] = len(sample) if isinstance(sample, list) else 0
        for entry in queries:
            if entry.query_key in shown_by_key:
                entry.rows_shown = shown_by_key[entry.query_key]

    def _scratch_rejected(self) -> None:
        scope = _CURRENT_CALL.get()
        for entry in reversed(self._ledger):
            if scope is not None and entry.call_index == scope.index and entry.op == "query":
                entry.scratch_store_rejected = True
                return

    def record_refused(self, method: str) -> None:
        """A Tool asked for a method outside the allowlist: a code defect (§5.2); logged and kept."""

        if method not in self._refused:
            self._refused.append(method[:80])
        logger.error("agent_eval.datarc_method_refused reference=%s method=%s", self.reference, method[:80])

    # ── classification (§7) ──
    def _environment_cause(self, scope: _CallScope) -> str | None:
        primary = _PRIMARY_OPERATIONS.get(scope.tool, frozenset())
        last: dict[str, _LedgerEntry] = {}
        for entry in self._ledger:
            if entry.call_index == scope.index and entry.op in primary:
                last[entry.query_key or entry.method] = entry
        outcomes = {entry.outcome for entry in last.values()}
        return next((cause for cause in _ENVIRONMENT_OUTCOMES if cause in outcomes), None)

    def _call_outcome(self, scope: _CallScope) -> str:
        primary = _PRIMARY_OPERATIONS.get(scope.tool, frozenset())
        outcomes = [entry.outcome for entry in self._ledger if entry.call_index == scope.index and entry.op in primary]
        if any(outcome in {"ok", "empty"} for outcome in outcomes):
            return "ok"
        if outcomes and all(outcome == "member_denied" for outcome in outcomes):
            return "member_denied"
        return "agent_error" if outcomes else "ok"

    def owner_permission_denied(self, required_groups: Iterable[frozenset[str]]) -> str | None:
        """The Datarc Tool a case requires when every call to it failed for lack of the Owner's permission."""

        for group in required_groups:
            datarc_tools = {tool for tool in group if tool in EVAL_READ_ONLY_LIVE_CONTRACTS}
            if not datarc_tools:
                continue
            calls = [outcome for tool, outcome in self._calls if tool in group]
            if calls and all(outcome == "member_denied" for outcome in calls):
                return sorted(datarc_tools)[0]
        return None

    def _halt(self, stop: _Stop) -> None:
        if self.stop is None:
            self.stop = stop
            self.stop_event.set()

    # ── evidence (§6.1) ──
    @property
    def used(self) -> bool:
        return bool(self._calls)

    def evidence(self) -> dict[str, Any]:
        """``deterministic.datarc`` and the call count; empty when the attempt made no Datarc call."""

        if not self._calls and not self._ledger and not self._refused:
            return {}
        document: dict[str, Any] = {
            "datarc": [entry.evidence() for entry in self._ledger],
            "datarc_calls": len(self._calls),
        }
        if self._refused:
            document["datarc_method_refused"] = list(self._refused)
        return document

    def failure_evidence(self) -> dict[str, Any] | None:
        """The §11.3 safe facts for an attempt this route stopped for an environment cause."""

        stop = self.stop
        if stop is None or stop.kind != "unavailable" or stop.cause is None:
            return None
        return failure_document(stop.cause, reference=self.reference, tool=stop.tool, workspace=stop.workspace, owner_name=stop.owner_name)

    def close(self) -> None:
        """Clear the rows and the Tool's Turn-scoped caches; called in the attempt's ``finally``."""

        from bowhead_core.tool_registry import cleanup_turn_query_cache

        for turn_id in self._turn_ids:
            if turn_id:
                cleanup_turn_query_cache(turn_id)
        self.scratch.clear()


def failure_document(
    cause: str, *, reference: str, tool: str | None = None, workspace: str | None = None, owner_name: str | None = None
) -> dict[str, Any]:
    document: dict[str, Any] = {"category": DATARC_FAILURE_CATEGORY, "cause": cause, "reference": reference}
    if tool:
        document["tool"] = tool[:120]
    if workspace and cause == "owner_not_member":
        document["workspace"] = workspace[:120]
    if owner_name and cause == "starter_not_owner":
        document["owner_name"] = owner_name[:120]
    return {"failure": document}


def _query_count(tool_name: str, arguments: Mapping[str, Any]) -> int:
    if tool_name != "bi_query":
        return 0
    queries = arguments.get("queries")
    if isinstance(queries, list):
        return len(queries)
    return 1 if arguments.get("text") else 0


def _text_key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _query_key(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    text = kwargs.get("text")
    if text is None and len(args) >= 3:
        text = args[2]
    return _text_key(str(text or ""))


def _outer_timeout(method: str) -> float:
    # download_data creates, polls (its own window) and downloads: three production bounds in sequence.
    return PRODUCTION_TIMEOUT_S * 3 if method == "download_data" else PRODUCTION_TIMEOUT_S


def _elapsed_ms(started: float) -> int:
    import time

    return int((time.perf_counter() - started) * 1000)


def datarc_query_digests(deterministic: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """(semantics, result) digest pairs of an attempt's row queries, for the §6.3 comparison rule."""

    entries = deterministic.get("datarc") if isinstance(deterministic, Mapping) else None
    if not isinstance(entries, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("op") != "query":
            continue
        semantics, result = entry.get("semantics_digest"), entry.get("result_digest")
        if isinstance(semantics, str) and semantics and isinstance(result, str) and result:
            pairs.append((semantics, result))
    return tuple(pairs)


def data_changed_during_test(baseline: Iterable[tuple[str, str]], candidate: Iterable[tuple[str, str]]) -> bool:
    """§6.3: a baseline and a candidate row query with the same semantics but a different result."""

    baseline_results: dict[str, set[str]] = {}
    for semantics, result in baseline:
        baseline_results.setdefault(semantics, set()).add(result)
    for semantics, result in candidate:
        seen = baseline_results.get(semantics)
        if seen and result not in seen:
            return True
    return False


__all__ = [
    "CALL_RECORDED",
    "DATARC_CAUSES",
    "DATARC_FAILURE_CATEGORY",
    "DATARC_LIVE_ROUTE",
    "DATARC_UNAVAILABLE_CODE",
    "DATARC_UNAVAILABLE_DENIAL",
    "DATARC_BUDGET_DENIAL",
    "EVAL_READ_ONLY_LIVE_CONTRACTS",
    "EXCLUDED_DATARC_METHODS",
    "EXCLUDED_VECTOR_METHODS",
    "FEATURE_FLAG",
    "OWNER_PERMISSION_DENIED",
    "RETRYABLE_CAUSES",
    "RUN_LEVEL_CAUSES",
    "DatarcToolRaised",
    "EvalDatarcBinding",
    "EvalDatarcIdentityResolver",
    "EvalDatarcLiveRoute",
    "EvalDatarcLiveRun",
    "EvalDatarcReader",
    "EvalDatarcUnavailable",
    "EvalVectorReader",
    "ResolvedDatarcIdentity",
    "data_changed_during_test",
    "datarc_live_enabled",
    "datarc_query_digests",
    "derive_evidence_key",
    "failure_document",
    "result_digest",
    "semantics_digest",
]
