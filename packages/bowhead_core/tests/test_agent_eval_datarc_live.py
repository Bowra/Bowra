"""Live read-only Datarc data Tools in evaluation (design agent-eval-datarc-read-only.md §4–§7, §11).

The production handlers of ``bi_query``, ``bi_schema`` and ``bi_concept_resolve`` run here against a
recording fake Datarc client through the evaluation router, as an attempt runs them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from bowhead_bi_bridge.errors import BIAsyncTaskTimeout, BIConnectionError, BIPermissionError, BIValidationError, DatarcError
from bowhead_bi_bridge.types import CutResult, DimensionMeta, DownloadDataResult, KeywordCandidate, KeywordRecall, MeasureMeta
from bowhead_core.agent_eval_datarc import (
    DATARC_LIVE_ROUTE,
    DATARC_READER_METHODS,
    EXCLUDED_DATARC_METHODS,
    EXCLUDED_VECTOR_METHODS,
    MAX_OPERATIONS,
    MAX_QUERY_OPERATIONS,
    EvalDatarcLiveRun,
    EvalDatarcReader,
    EvalDatarcUnavailable,
    EvalVectorReader,
    ResolvedDatarcIdentity,
    data_changed_during_test,
    derive_evidence_key,
    result_digest,
    semantics_digest,
)
from bowhead_core.agent_eval_effects import (
    NOT_RUN_IN_EVAL_DENIAL,
    EvalEffectAdapterRegistry,
    EvalEffectOutcome,
    EvalEffectRouter,
    EvalTraceCollector,
    counts_against_tool_budget,
)
from bowhead_core.agent_eval_scratch_store import EvalScratchArtifactStore
from bowhead_core.agent_version_eval import AttemptEvidence, ComparisonCase, VerdictRule, compare_case
from bowhead_core.agents.bi_concept_resolve import register_bi_concept_resolve_tool
from bowhead_core.tool_registry import _TURN_QUERY_CACHE, ToolExecutionContext, ToolRegistry, build_default_registry

from bowhead_bi_bridge import DatarcClient

pytestmark = pytest.mark.anyio

OWNER_MEMBER = "member-owner-7f3a"
WORKSPACE = "21"
SECRET_ROW_VALUE = "SecretBrandXQ"
QUERY_TEXT = "2026年各品牌营业额按月"


class FakeDatarc:
    """Implements every DatarcClient method; records which ones a Tool actually called."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None, permission: str = "Normal", delay: float = 0.0) -> None:
        self.called: list[str] = []
        self.members: list[str] = []
        self.rows = rows if rows is not None else [{"品牌": SECRET_ROW_VALUE, "营业额": 1234.5}, {"品牌": "耐克", "营业额": 99.0}]
        self.permission = permission
        self.delay = delay
        self.failures: dict[str, list[BaseException]] = {}
        self.active = 0
        self.max_active = 0
        self.datarc_tenant = "tenant1"

    def fail(self, method: str, *errors: BaseException) -> None:
        self.failures.setdefault(method, []).extend(errors)

    async def _enter(self, method: str) -> None:
        self.called.append(method)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            queue = self.failures.get(method)
            if queue:
                raise queue.pop(0)
        finally:
            self.active -= 1

    async def list_measures(self, ws: str) -> list[MeasureMeta]:
        await self._enter("list_measures")
        return [MeasureMeta(id=1, names=["营业额"], used=True), MeasureMeta(id=2, names=["退款率"], used=True)]

    async def list_dimensions(self, ws: str) -> list[DimensionMeta]:
        await self._enter("list_dimensions")
        return [DimensionMeta(id=3, names=["品牌"], used=True)]

    async def get_workspace(self, ws: str) -> Any:
        await self._enter("get_workspace")
        return SimpleNamespace(name="Retail", desc="")

    async def list_models(self, ws: str) -> list[Any]:
        await self._enter("list_models")
        return []

    async def get_model(self, ws: str, uid: str) -> Any:
        await self._enter("get_model")
        return None

    def measure_metadata(self, ws: str, name: str | None) -> dict[str, str] | None:
        self.called.append("measure_metadata")
        return None

    def dimension_metadata(self, ws: str, name: str | None) -> dict[str, Any] | None:
        self.called.append("dimension_metadata")
        return None

    async def cut_question(self, *, datarc_workspace_id: str, member_uid: str, question: str) -> CutResult:
        self.members.append(member_uid)
        await self._enter("cut_question")
        return CutResult(tokens=[])

    async def recall_keywords(self, ws: str, words: list[str], *, n_results: int | None = None) -> list[KeywordRecall]:
        await self._enter("recall_keywords")
        return [
            KeywordRecall(source=word, candidates=[KeywordCandidate(word="耐克", distance=0.01, field="d3", prop="d3=x", tuids="t1")])
            for word in words
        ]

    async def check_token_permissions(self, ws: str, member_uid: str, tokens: list[dict[str, Any]]) -> list[str]:
        self.members.append(member_uid)
        await self._enter("check_token_permissions")
        return [self.permission for _ in tokens]

    async def download_data(
        self, datarc_workspace_id: str, member_uid: str, text: str, *, timeout_sec: float = 60.0, poll_interval_sec: float = 1.5
    ) -> DownloadDataResult:
        self.members.append(member_uid)
        await self._enter("download_data")
        return DownloadDataResult(
            columns=["品牌", "营业额"],
            rows=[dict(row) for row in self.rows],
            total_rows=len(self.rows),
            csv_filename="result.csv",
            task_uid="task_1",
            query_text=text,
            explain_json={"measures": ["营业额"], "dimensions": ["品牌"]},
            datarc_completed_at="2026-10-22T08:00:01Z",
        )


def _excluded_method(name: str):
    async def _method(self: FakeDatarc, *args: Any, **kwargs: Any) -> Any:
        self.called.append(name)
        return {}

    return _method


# The fake can do everything the real client can, so only the proxy can stop an excluded call.
for _name in EXCLUDED_DATARC_METHODS:
    setattr(FakeDatarc, _name, _excluded_method(_name))


class FakeVector:
    datarc_tenant = "tenant1"

    def __init__(self) -> None:
        self.called: list[str] = []

    async def query(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.called.append("query")
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


for _name in EXCLUDED_VECTOR_METHODS:
    setattr(FakeVector, _name, _excluded_method(_name))


class Resolver:
    def __init__(self, identity: ResolvedDatarcIdentity | None = None, failure: EvalDatarcUnavailable | None = None) -> None:
        self.identity = identity
        self.failure = failure
        self.calls = 0

    async def resolve(self, run: Any) -> ResolvedDatarcIdentity:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.identity is not None
        return self.identity


def _identity(client: Any, vector: Any = None) -> ResolvedDatarcIdentity:
    return ResolvedDatarcIdentity(client=client, vector_client=vector, member_uid=OWNER_MEMBER, workspace_id=WORKSPACE, workspace_name="Retail")


def _run_scope(resolver: Resolver, *, key: bytes | None = b"k" * 32) -> EvalDatarcLiveRun:
    return EvalDatarcLiveRun(resolver=resolver, run=SimpleNamespace(run_id="aer_1", created_by="u_owner"), digest_key=key)


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    register_bi_concept_resolve_tool(registry)
    return registry


@pytest.fixture(autouse=True)
def _fresh_turn_caches() -> Iterator[None]:
    yield
    _TURN_QUERY_CACHE.clear()


def _eval_ctx(turn_id: str | None = None) -> ToolExecutionContext:
    # As _isolated_context builds it: no member, an inert Datarc handle and the fixture project.
    return ToolExecutionContext(
        workspace_id="ws_eval",
        member_uid="",
        user_id="u_owner",
        # Each call its own Turn, so the Tool's Turn-scoped query cache never answers for Datarc.
        turn_id=turn_id or f"t_eval_{uuid4().hex[:12]}",
        datarc=object(),  # type: ignore[arg-type]
        active_datarc_project="eval_fixture",
        session_id="s_eval",
    )


def _router(
    route: Any, *, mode: str = "no_effects", adapter_refs: dict[str, str] | None = None, registry: EvalEffectAdapterRegistry | None = None
) -> tuple[EvalEffectRouter, EvalTraceCollector]:
    trace = EvalTraceCollector()
    router = EvalEffectRouter(
        effect_policy={"mode": mode, "adapter_refs": adapter_refs or {}},
        fixture_ref={},
        fixture_resolver=None,
        adapter_registry=registry or EvalEffectAdapterRegistry(),
        trace=trace,
        datarc_live=route,
    )
    return router, trace


async def _call(router: EvalEffectRouter, name: str, arguments: dict[str, Any], ctx: ToolExecutionContext | None = None) -> Any:
    view = router.build_safe_view(_registry())
    return await view.execute(name, arguments, ctx or _eval_ctx())


# ── routing and the production contract ───────────────────────────────────────


async def test_with_the_switch_off_every_datarc_tool_is_still_denied_as_not_run() -> None:
    client = FakeDatarc()
    router, trace = _router(None)
    for name, arguments in (("bi_query", {"queries": [QUERY_TEXT]}), ("bi_schema", {}), ("bi_concept_resolve", {"tokens": ["耐克"]})):
        invocation = await _call(router, name, arguments)
        assert invocation.result["error"] == NOT_RUN_IN_EVAL_DENIAL
    assert [call["denial_code"] for call in trace.tool_calls] == [NOT_RUN_IN_EVAL_DENIAL] * 3
    assert client.called == []


async def test_the_three_tools_read_live_with_the_owner_member() -> None:
    client = FakeDatarc()
    vector = FakeVector()
    route = _run_scope(Resolver(_identity(client, vector))).open_attempt(reference="aej_1", remaining_ms=lambda: 600_000)
    router, trace = _router(route)

    query = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    schema = await _call(router, "bi_schema", {})
    concept = await _call(router, "bi_concept_resolve", {"tokens": ["耐克"]})

    assert query.error is None and query.result["row_count"] == 2
    assert {row["品牌"] for row in query.result["rows"]} == {SECRET_ROW_VALUE, "耐克"}
    assert schema.error is None and concept.error is None
    assert [call["route"] for call in trace.tool_calls] == [DATARC_LIVE_ROUTE] * 3
    assert all(call["denial_code"] is None for call in trace.tool_calls)
    assert trace.data_read is True and 1234.5 in trace.authorized_numbers
    # Every member-scoped request carried the Owner's member, never the evaluation context's empty one.
    assert client.members and set(client.members) == {OWNER_MEMBER}


@pytest.mark.parametrize(
    "name",
    ["bi_list_measures", "bi_list_dimensions", "tool_list_workspaces", "tool_switch_context"],
)
async def test_other_datarc_tools_stay_not_run(name: str) -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_1", remaining_ms=lambda: 600_000)
    router, trace = _router(route)
    invocation = await _call(router, name, {})
    assert invocation.result["error"] == NOT_RUN_IN_EVAL_DENIAL
    assert client.called == []


async def test_fake_only_mode_admits_only_exact_fakes() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_1", remaining_ms=lambda: 600_000)
    router, _trace = _router(route, mode="fake_only")
    invocation = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result["error"] == NOT_RUN_IN_EVAL_DENIAL
    assert client.called == []


async def test_an_exact_adapter_still_takes_precedence() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_1", remaining_ms=lambda: 600_000)
    adapters = EvalEffectAdapterRegistry()

    async def _fake(tool_name: str, arguments: Any, ctx: Any) -> EvalEffectOutcome:
        return EvalEffectOutcome(result={"rows": [{"x": 1}]}, evidence_ref="fake:bi_query")

    adapters.register("fake.bi_query@1", _fake)
    router, trace = _router(route, adapter_refs={"bi_query": "fake.bi_query@1"}, registry=adapters)
    invocation = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result == {"rows": [{"x": 1}]}
    assert trace.tool_calls[0]["route"] == "exact_adapter"
    assert client.called == []


def test_production_tool_specs_keep_no_effect_contract() -> None:
    registry = _registry()
    specs = {name: registry.get(name) for name in ("bi_query", "bi_schema", "bi_concept_resolve")}
    assert all(spec.effect_spec is None for spec in specs.values())
    handlers = {name: spec.handler for name, spec in specs.items()}
    route = _run_scope(Resolver(_identity(FakeDatarc()))).open_attempt(reference="aej_1", remaining_ms=lambda: 600_000)
    router, _trace = _router(route)
    router.build_safe_view(registry)
    # Building the evaluation view never changes the production registry.
    assert {name: registry.get(name).handler for name in handlers} == handlers
    assert all(registry.get(name).effect_spec is None for name in handlers)


# ── read-only guarantee (§5.2) ────────────────────────────────────────────────


async def _representative_paths(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeDatarc, FakeVector, Any]:
    monkeypatch.setenv("BOWHEAD_CONCEPT_RESOLVE_TIMEOUT_S", "0.05")
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_BACKOFF_S", "0.05")
    client = FakeDatarc(permission="No permission")
    vector = FakeVector()
    route = _run_scope(Resolver(_identity(client, vector))).open_attempt(reference="aej_paths", remaining_ms=lambda: 600_000)
    router, _trace = _router(route)
    await _call(router, "bi_query", {"queries": [QUERY_TEXT]})  # success
    client.rows = []
    await _call(router, "bi_query", {"queries": ["2026年耐克营业额"]})  # empty result
    client.rows = [{"品牌": "耐克", "营业额": 1.0}]
    await _call(router, "bi_schema", {})  # permission-filtered schema
    client.fail("download_data", DatarcError("查询过于频繁，请稍后再试", source="business", throttled=True))
    await _call(router, "bi_query", {"queries": ["2026年各品牌营业额", "2026年各品牌退款率", "2026年各门店营业额"]})  # batch + throttling
    await _call(router, "bi_concept_resolve", {"tokens": ["耐克"]})  # recall
    slow = FakeDatarc(delay=0.2)
    slow_route = _run_scope(Resolver(_identity(slow, vector))).open_attempt(reference="aej_slow", remaining_ms=lambda: 600_000)
    slow_router, _ = _router(slow_route)
    await _call(slow_router, "bi_concept_resolve", {"tokens": ["耐克"]})  # recall timeout
    client.called.extend(slow.called)
    return client, vector, (route, slow_route)


async def test_every_method_the_tools_call_is_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    client, vector, routes = await _representative_paths(monkeypatch)
    assert {"download_data", "list_measures", "list_dimensions", "check_token_permissions", "cut_question", "recall_keywords"} <= set(client.called)
    assert set(client.called) <= DATARC_READER_METHODS
    assert set(vector.called) <= {"query"}
    assert all(route.evidence().get("datarc_method_refused") is None for route in routes)


def _public_methods(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name, None))}


def test_every_excluded_datarc_endpoint_is_unreachable_through_the_proxy() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_x", remaining_ms=lambda: 600_000)
    reader = EvalDatarcReader(client, route)
    # Every public DatarcClient method outside the allowlist, plus the design's named exclusions.
    excluded = (_public_methods(DatarcClient) - DATARC_READER_METHODS) | set(EXCLUDED_DATARC_METHODS)
    assert set(EXCLUDED_DATARC_METHODS) <= _public_methods(DatarcClient)
    assert {"create_account", "get_sso_auth_data", "call_llm_async", "plan_query", "ask_question", "fetch_workspace_member"} <= excluded
    for name in sorted(excluded):
        with pytest.raises(AttributeError):
            getattr(reader, name)
    # The pooled client's own state is not reachable as proxy attributes.
    for name in ("client", "_http", "_auth_key", "_base_url"):
        with pytest.raises(AttributeError):
            getattr(reader, name)
    assert client.called == []
    assert set(route.evidence().get("datarc_method_refused", [])) >= {"create_account", "plan_query"}


def test_vector_writes_and_management_are_unreachable() -> None:
    vector = FakeVector()
    route = _run_scope(Resolver(_identity(FakeDatarc(), vector))).open_attempt(reference="aej_v", remaining_ms=lambda: 600_000)
    reader = EvalVectorReader(vector, route)
    for name in EXCLUDED_VECTOR_METHODS:
        with pytest.raises(AttributeError):
            getattr(reader, name)
    assert reader.datarc_tenant == "tenant1"
    assert vector.called == []


async def test_rows_go_only_to_the_attempt_store_and_end_with_the_attempt() -> None:
    client = FakeDatarc()
    seen: list[Any] = []
    registry = _registry()
    original = registry.get("bi_query").handler

    async def _spy(arguments: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        seen.append(ctx)
        return await original(arguments, ctx)

    from dataclasses import replace

    registry = registry.clone_with_spec_override(replace(registry.get("bi_query"), handler=_spy))
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_store", remaining_ms=lambda: 600_000)
    router, _trace = _router(route)
    ctx = _eval_ctx("t_eval_store")
    invocation = await router.build_safe_view(registry).execute("bi_query", {"queries": [QUERY_TEXT]}, ctx)

    call_ctx = seen[0]
    assert isinstance(call_ctx.artifact_store, EvalScratchArtifactStore)
    assert isinstance(call_ctx.datarc, EvalDatarcReader) and call_ctx.datarc is not client
    assert ctx.artifact_store is None and ctx.member_uid == "" and ctx.eval_datarc is None
    artifact_id = invocation.result["artifact_id"]
    assert artifact_id.startswith("art_") and len(artifact_id) == 36
    assert route.scratch.get(artifact_id) is not None and len(route.scratch) == 1
    assert "t_eval_store" in _TURN_QUERY_CACHE
    route.close()
    assert len(route.scratch) == 0
    assert "t_eval_store" not in _TURN_QUERY_CACHE


# ── identity and the empty member (§3) ────────────────────────────────────────


@pytest.mark.parametrize("member", ["", "   "])
def test_an_identity_without_a_member_is_refused(member: str) -> None:
    with pytest.raises(ValueError):
        ResolvedDatarcIdentity(client=FakeDatarc(), vector_client=None, member_uid=member, workspace_id=WORKSPACE)


async def test_a_refused_identity_makes_no_datarc_request_and_latches_for_the_run() -> None:
    client = FakeDatarc()
    resolver = Resolver(failure=EvalDatarcUnavailable("starter_not_owner", owner_name="Ada"))
    scope = _run_scope(resolver)
    first = scope.open_attempt(reference="aej_a", remaining_ms=lambda: 600_000)
    router, trace = _router(first)
    invocation = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result["error"] == "eval_datarc_unavailable"
    assert first.stop is not None and first.stop.cause == "starter_not_owner" and first.stop_event.is_set()
    assert first.failure_evidence() == {
        "failure": {"category": "data_connection", "cause": "starter_not_owner", "reference": "aej_a", "tool": "bi_query", "owner_name": "Ada"}
    }
    # Refused for the environment: not counted against the case's Tool budget.
    assert not counts_against_tool_budget(trace.tool_calls[0])
    second = scope.open_attempt(reference="aej_b", remaining_ms=lambda: 600_000)
    router_b, _ = _router(second)
    await _call(router_b, "bi_schema", {})
    assert resolver.calls == 1 and second.stop is not None and second.stop.cause == "starter_not_owner"
    assert client.called == []


async def test_identity_is_resolved_again_for_every_attempt() -> None:
    resolver = Resolver(_identity(FakeDatarc()))
    scope = _run_scope(resolver)
    for reference in ("aej_1", "aej_2"):
        router, _ = _router(scope.open_attempt(reference=reference, remaining_ms=lambda: 600_000))
        await _call(router, "bi_schema", {})
        await _call(router, "bi_schema", {})
    assert resolver.calls == 2


# ── failures (§7) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "cause", "run_level"),
    [
        (BIPermissionError("datarc 401: access rejected", source="http"), "connection_rejected", True),
        (BIConnectionError("datarc 5xx: 502"), "datarc_unreachable", False),
        (BIAsyncTaskTimeout("poll timeout"), "datarc_timeout", False),
        (DatarcError("请求过于频繁", source="business", throttled=True), "datarc_busy", False),
    ],
)
async def test_environment_failures_end_the_attempt_with_their_cause(
    error: Exception, cause: str, run_level: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_RETRIES", "0")
    client = FakeDatarc()
    client.fail("download_data", error)
    scope = _run_scope(Resolver(_identity(client)))
    route = scope.open_attempt(reference="aej_env", remaining_ms=lambda: 600_000)
    router, trace = _router(route)
    invocation = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result["error"] == "eval_datarc_unavailable"
    assert route.stop is not None and route.stop.cause == cause
    assert route.failure_evidence()["failure"]["category"] == "data_connection"
    assert (scope.latched is not None) is run_level
    assert trace.tool_calls[0]["denial_code"] == "eval_datarc_unavailable"


async def test_a_latched_connection_failure_makes_no_further_datarc_requests() -> None:
    client = FakeDatarc()
    client.fail("list_measures", BIPermissionError("datarc 403: access rejected", source="http"))
    scope = _run_scope(Resolver(_identity(client)))
    router, _ = _router(scope.open_attempt(reference="aej_1", remaining_ms=lambda: 600_000))
    await _call(router, "bi_schema", {})
    made = list(client.called)
    router_b, _ = _router(scope.open_attempt(reference="aej_2", remaining_ms=lambda: 600_000))
    invocation = await _call(router_b, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result["error"] == "eval_datarc_unavailable"
    assert client.called == made


async def test_agent_errors_and_member_denials_are_returned_to_the_agent_and_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_RETRIES", "0")
    client = FakeDatarc()
    client.fail("download_data", BIValidationError("字段不存在", source="business"))
    client.fail("download_data", BIPermissionError("无权限访问该字段", source="business"))
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_agent", remaining_ms=lambda: 600_000)
    router, trace = _router(route)
    first = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    second = await _call(router, "bi_query", {"queries": ["2026年退款率"]})
    # The Agent sees the Tool's own error, as in chat; the calls ran and are scored.
    assert first.error and "BIValidationError" in first.error
    assert second.error and "BIPermissionError" in second.error
    assert route.stop is None
    assert [call["route"] for call in trace.tool_calls] == [DATARC_LIVE_ROUTE, DATARC_LIVE_ROUTE]
    assert route.owner_permission_denied([frozenset({"bi_query"})]) is None
    third = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_denied", remaining_ms=lambda: 600_000)
    client.fail("download_data", BIPermissionError("无权限", source="business"))
    router_c, _ = _router(third)
    await _call(router_c, "bi_query", {"queries": [QUERY_TEXT]})
    assert third.owner_permission_denied([frozenset({"bi_query"})]) == "bi_query"
    assert third.owner_permission_denied([frozenset({"python_exec"})]) is None


# ── budget (§4.3) ─────────────────────────────────────────────────────────────


async def test_the_query_operation_bound_refuses_without_contacting_datarc() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_budget", remaining_ms=lambda: 600_000)
    router, trace = _router(route)
    for _ in range(2):
        await _call(router, "bi_query", {"queries": [f"2026年第{index}品牌营业额" for index in range(20)]})
    downloads = client.called.count("download_data")
    assert downloads == MAX_QUERY_OPERATIONS
    invocation = await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert invocation.result == {"error": "eval_datarc_budget_exceeded", "error_code": "eval_datarc_budget_exceeded", "ok": False}
    assert client.called.count("download_data") == downloads
    assert trace.tool_budget_exceeded is True
    # One Tool-budget count per Agent-visible call, however many queries it carried.
    assert [counts_against_tool_budget(call) for call in trace.tool_calls] == [True, True, True]


async def test_the_operation_bound_counts_every_datarc_request() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_ops", remaining_ms=lambda: 600_000)
    router, trace = _router(route)
    for _ in range(MAX_OPERATIONS):
        await _call(router, "bi_schema", {})
        if trace.tool_budget_exceeded:
            break
    assert trace.tool_budget_exceeded is True
    assert sum(1 for name in client.called if name in {"list_measures", "list_dimensions", "check_token_permissions"}) <= MAX_OPERATIONS


async def test_the_tools_own_throttle_retries_are_not_extra_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_BACKOFF_S", "0.05")
    client = FakeDatarc()
    client.fail("download_data", DatarcError("查询过于频繁", source="business", throttled=True))
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_retry", remaining_ms=lambda: 600_000)
    router, _ = _router(route)
    result = await _call(router, "bi_query", {"queries": [QUERY_TEXT, "2026年各门店营业额"]})
    assert all(item["ok"] for item in result.result["results"])
    assert client.called.count("download_data") == 3
    assert route._query_operations == 2
    assert route.stop is None


# ── concurrency and time (§4.4, §4.5) ─────────────────────────────────────────


async def test_at_most_two_operations_per_run_and_four_per_connection() -> None:
    client = FakeDatarc(delay=0.05)
    runs = [_run_scope(Resolver(_identity(client))) for _ in range(3)]
    routes = [
        scope.open_attempt(reference=f"aej_{index}_{attempt}", remaining_ms=lambda: 600_000)
        for index, scope in enumerate(runs)
        for attempt in range(4)
    ]
    queries = {"queries": [f"2026年第{index}品牌营业额" for index in range(6)]}

    async def _attempt(route: Any) -> None:
        router, _ = _router(route)
        await _call(router, "bi_query", queries, _eval_ctx(f"t_eval_{route.reference}"))

    await asyncio.gather(*(_attempt(route) for route in routes))
    assert client.max_active <= 4
    single = FakeDatarc(delay=0.05)
    scope = _run_scope(Resolver(_identity(single)))
    await asyncio.gather(*(_attempt(scope.open_attempt(reference=f"aej_one_{index}", remaining_ms=lambda: 600_000)) for index in range(8)))
    assert single.max_active <= 2


async def test_a_call_is_not_started_without_time_left_and_ends_with_the_time_limit() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_late", remaining_ms=lambda: 4_000)
    router, _ = _router(route)
    await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert "download_data" not in client.called
    assert route.stop is not None and route.stop.kind == "time_limit"
    assert route.failure_evidence() is None


# ── evidence and privacy (§6) ─────────────────────────────────────────────────

_ALLOWED_ENTRY_FIELDS = {
    "tool",
    "op",
    "requested_at",
    "completed_at",
    "outcome",
    "datarc_completed_at",
    "row_count",
    "rows_shown",
    "semantics_digest",
    "result_digest",
    "request_id",
    "scratch_store_rejected",
}


async def test_evidence_holds_no_rows_query_text_or_identifiers() -> None:
    client = FakeDatarc()
    route = _run_scope(Resolver(_identity(client, FakeVector()))).open_attempt(reference="aej_ev", remaining_ms=lambda: 600_000)
    router, _ = _router(route)
    await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    await _call(router, "bi_concept_resolve", {"tokens": ["耐克"]})
    evidence = route.evidence()
    assert set(evidence) == {"datarc", "datarc_calls"} and evidence["datarc_calls"] == 2
    for entry in evidence["datarc"]:
        assert set(entry) <= _ALLOWED_ENTRY_FIELDS
    query = next(entry for entry in evidence["datarc"] if entry["op"] == "query")
    assert query["row_count"] == 2 and query["rows_shown"] == 2 and query["outcome"] == "ok"
    assert query["datarc_completed_at"] == "2026-10-22T08:00:01Z"
    assert "request_id" not in query  # the fake Datarc returned none
    encoded = json.dumps(evidence, ensure_ascii=False)
    for secret in (SECRET_ROW_VALUE, "1234.5", QUERY_TEXT, "耐克", OWNER_MEMBER, "Retail", "task_1", "result.csv"):
        assert secret not in encoded


async def test_request_id_is_recorded_only_when_datarc_returns_one() -> None:
    client = FakeDatarc()
    original = client.download_data

    async def _with_request_id(*args: Any, **kwargs: Any) -> DownloadDataResult:
        result = await original(*args, **kwargs)
        result.request_id = "req-42"
        return result

    client.download_data = _with_request_id  # type: ignore[method-assign]
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_rid", remaining_ms=lambda: 600_000)
    router, _ = _router(route)
    await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    assert [entry.get("request_id") for entry in route.evidence()["datarc"] if entry["op"] == "query"] == ["req-42"]


def test_digests_are_keyed_and_deterministic() -> None:
    key = derive_evidence_key("root-a")
    other = derive_evidence_key("root-b")
    assert key is not None and other is not None and derive_evidence_key("") is None
    rows = [{"a": 1, "b": "x"}, {"a": 2.0, "b": "y"}]
    reordered = [{"b": "y", "a": 2}, {"b": "x", "a": 1}]
    assert result_digest(key, columns=["b", "a"], rows=rows) == result_digest(key, columns=["a", "b"], rows=reordered)
    assert result_digest(key, columns=["a", "b"], rows=rows) != result_digest(other, columns=["a", "b"], rows=rows)
    assert result_digest(key, columns=["a", "b"], rows=rows) != result_digest(key, columns=["a", "b"], rows=rows[:1])
    explain = {"measures": ["营业额"]}
    assert semantics_digest(key, workspace_id="1", explain_json=explain) == semantics_digest(key, workspace_id="1", explain_json=dict(explain))
    assert semantics_digest(key, workspace_id="1", explain_json=explain) != semantics_digest(key, workspace_id="2", explain_json=explain)
    assert semantics_digest(key, workspace_id="1", explain_json={}) is None
    # A plain hash of a small result could be confirmed by guessing; the digest never equals one.
    import hashlib

    assert hashlib.sha256(b'{"columns":["a"],"rows":["[1]"]}').hexdigest() not in result_digest(key, columns=["a"], rows=[{"a": 1}])


async def test_the_same_row_query_twice_gives_equal_digests() -> None:
    client = FakeDatarc()
    digests = []
    for reference in ("aej_x", "aej_y"):
        route = _run_scope(Resolver(_identity(client))).open_attempt(reference=reference, remaining_ms=lambda: 600_000)
        router, _ = _router(route)
        await _call(router, "bi_query", {"queries": [QUERY_TEXT]}, _eval_ctx(f"t_{reference}"))
        query = next(entry for entry in route.evidence()["datarc"] if entry["op"] == "query")
        digests.append((query["semantics_digest"], query["result_digest"]))
        route.close()
    assert digests[0] == digests[1]


def _attempt(digests: tuple[tuple[str, str], ...], status: str = "passed") -> AttemptEvidence:
    return AttemptEvidence(
        status=status,  # type: ignore[arg-type]
        subject_provider_ref="model-config",
        subject_provider_hash="sha256:m",
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility="exact",
        datarc_digests=digests,
    )


def _compare(baseline: AttemptEvidence, candidate: AttemptEvidence, *, verdict_rule: VerdictRule = "majority") -> Any:
    case = ComparisonCase(
        suite_revision_id="sr",
        case_revision_id="cr",
        tier="critical",
        required=True,
        attempts=1,
        execution_scope="paired",
        baseline=(baseline,),
        candidate=(candidate,),
    )
    return compare_case(case, pass_threshold=0.8, minimum_delta=0.2, flaky_limit=0.2, verdict_rule=verdict_rule)


@pytest.mark.parametrize("verdict_rule", ["threshold", "majority"])
def test_same_semantics_with_a_different_result_is_not_comparable(verdict_rule: VerdictRule) -> None:
    changed = _compare(_attempt((("s1", "r1"),)), _attempt((("s1", "r2"),), status="failed"), verdict_rule=verdict_rule)
    assert changed.classification == "incomparable"
    assert "data_changed_during_test" in changed.reason_codes
    assert data_changed_during_test([("s1", "r1")], [("s1", "r2")]) is True


@pytest.mark.parametrize(
    ("baseline", "candidate"),
    [
        ((("s1", "r1"),), (("s2", "r2"),)),  # different questions: the Agent's behaviour
        ((), (("s1", "r2"),)),  # no applied semantics on one side
        ((("s1", "r1"),), (("s1", "r1"),)),  # same data
    ],
)
@pytest.mark.parametrize("verdict_rule", ["threshold", "majority"])
def test_different_questions_or_missing_semantics_are_scored_normally(
    baseline: tuple[tuple[str, str], ...], candidate: tuple[tuple[str, str], ...], verdict_rule: VerdictRule
) -> None:
    result = _compare(_attempt(baseline), _attempt(candidate, status="failed"), verdict_rule=verdict_rule)
    assert result.classification == "regressed"
    assert "data_changed_during_test" not in result.reason_codes


# ── release reasons (§7.3) ────────────────────────────────────────────────────


def test_a_datarc_failure_names_its_cause_in_the_release_reason() -> None:
    from bowhead_core.agent_eval_attempt_failure import attempt_failure_detail, incomplete_attempt_reason

    assert incomplete_attempt_reason("infra_failed", "agent_eval_datarc_unavailable", "datarc_busy") == "attempt_datarc_unavailable_datarc_busy"
    # A stored row without a known cause stays an unclassified platform failure rather than inventing one.
    assert incomplete_attempt_reason("infra_failed", "agent_eval_datarc_unavailable", None) == "attempt_infrastructure_incomplete"
    detail = attempt_failure_detail(
        status="infra_failed",
        failure_code="agent_eval_datarc_unavailable",
        deterministic={
            "failure": {"category": "data_connection", "cause": "owner_not_member", "reference": "aej_9", "tool": "bi_query", "workspace": "Retail"}
        },
        reference="fallback",
    )
    assert detail == {
        "category": "data_connection",
        "cause": "owner_not_member",
        "tool": "bi_query",
        "workspace": "Retail",
        "reference": "aej_9",
        "reason_code": "attempt_datarc_unavailable_owner_not_member",
    }
    failed = AttemptEvidence(
        status="infra_failed",
        subject_provider_ref="model-config",
        subject_provider_hash="sha256:m",
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility="exact",
        failure_code="agent_eval_datarc_unavailable",
        failure_cause="connection_rejected",
    )
    result = _compare(failed, failed)
    assert result.classification == "inconclusive"
    assert result.reason_codes == ("attempt_datarc_unavailable_connection_rejected",)


async def test_route_logs_carry_no_query_text_or_member(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_RETRIES", "0")
    client = FakeDatarc()
    client.fail("download_data", BIConnectionError("datarc 5xx: 502"))
    route = _run_scope(Resolver(_identity(client))).open_attempt(reference="aej_log", remaining_ms=lambda: 600_000)
    router, _ = _router(route)
    with caplog.at_level("INFO", logger="bowhead_core.agent_eval_datarc"):
        await _call(router, "bi_query", {"queries": [QUERY_TEXT]})
    messages = [record.getMessage() for record in caplog.records if record.name == "bowhead_core.agent_eval_datarc"]
    assert messages and all(QUERY_TEXT not in message and OWNER_MEMBER not in message for message in messages)
    assert any("aej_log" in message for message in messages)


def test_owner_answers_show_datarc_counts_only_with_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agent_eval_insights import _attempt_datarc_counts

    stored = json.dumps(
        {
            "datarc_calls": 2,
            "datarc": [
                {"tool": "bi_query", "op": "query", "outcome": "ok", "row_count": 38, "rows_shown": 30},
                {"tool": "bi_query", "op": "cut", "outcome": "ok"},
                {"tool": "bi_schema", "op": "schema", "outcome": "ok"},
            ],
        }
    )
    monkeypatch.delenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", raising=False)
    assert _attempt_datarc_counts(stored) == {"calls": 2, "queries": 1, "rows": 38, "rows_shown": 30}
    monkeypatch.setenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", "1")
    assert _attempt_datarc_counts(stored) == {"calls": 2, "queries": 1, "rows": 38, "rows_shown": 30}
    # Rollback: recorded evidence stays stored and is ignored.
    monkeypatch.setenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", "0")
    assert _attempt_datarc_counts(stored) is None
