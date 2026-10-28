"""Fail-closed Tool effect routing and content-free Eval trace evidence."""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from bowhead_core.agents._generated.tool_names import SUBAGENT_RETURN
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_NONE
from bowhead_core.terminal_protocol import EffectClass
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from bowhead_core.agent_eval_datarc import EvalDatarcLiveRoute

_EXACT_REF_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}@[1-9][0-9]*$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

# Trace route of the scratch-local control Tool; shared with the budget scorer.
SCRATCH_CONTROL_ROUTE = "scratch_control"
# A Tool that declares itself read-only runs its real handler, with the Eval run's own access.
LIVE_READ_ONLY_ROUTE = "live_read_only"
# The evaluation environment had no safe way to run this Tool. That is a limit of the
# environment, not the Agent's behavior: scorers report it but don't fail on it.
NOT_RUN_IN_EVAL_DENIAL = "eval_effect_adapter_missing"
# FEATURE_AGENT_EVAL_DATARC_READ_ONLY: the three Datarc data Tools read the Team's Datarc live, read-only
# (agent_eval_datarc). A call refused for an environment cause (connection, identity, outage) did nothing.
DATARC_LIVE_ROUTE = "datarc_read_only_live"
DATARC_UNAVAILABLE_DENIAL = "eval_datarc_unavailable"
# Denials that say the environment could not run the call; they never consume the case's Tool budget.
ENVIRONMENT_DENIALS = frozenset({NOT_RUN_IN_EVAL_DENIAL, DATARC_UNAVAILABLE_DENIAL})
_MAX_TRACE_TOOL_CALLS = 200
_MAX_LIVE_AUTHORIZED_NUMBERS = 10_000
# Routes on which a Tool returned data to the Agent; only then can its figures be checked.
DATA_ROUTES = frozenset({"exact_adapter", "versioned_fixture", LIVE_READ_ONLY_ROUTE, DATARC_LIVE_ROUTE})
# Runtime entrances of a Tool the Owner picks under another name: the flat sandbox entrance and the
# restricted skill_invoke both run the python_sandbox Tool ("Run code").
_SANDBOX_TOOL = "python_sandbox"
_FLAT_SANDBOX_TOOL = "python_exec"


def eval_tool_identity(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """The Tool a call runs, named the way the Owner picks it on a required action.

    Renamed Tools resolve to their current name, and both runtime entrances of the sandbox count as
    ``python_sandbox``. Required actions compare these, never the raw call names, so the Tool the Owner
    chose and the call the Agent made agree.
    """

    from bowhead_core.agents._generated.tool_renames import canonical_tool_name

    name = canonical_tool_name(tool_name)
    if name == _FLAT_SANDBOX_TOOL:
        return _SANDBOX_TOOL
    if name == "skill_invoke" and isinstance(arguments, Mapping) and arguments.get("name") == _SANDBOX_TOOL:
        return _SANDBOX_TOOL
    return name


def counts_against_tool_budget(call: Mapping[str, Any]) -> bool:
    """Whether a recorded call consumed the case's Tool budget."""
    return call.get("route") != SCRATCH_CONTROL_ROUTE and call.get("denial_code") not in ENVIRONMENT_DENIALS


# Set by the evaluation registry view around one Tool call: True once the live Datarc route recorded the
# call itself, so a Tool error the registry turns into ``invocation.error`` is not recorded again as a denial.
CALL_RECORDED: contextvars.ContextVar[list[bool] | None] = contextvars.ContextVar("agent_eval_call_recorded", default=None)


class DatarcToolRaised(Exception):
    """Carries a Datarc Tool's own exception past the evaluation router after the call was recorded.

    The registry then turns it into the same ``invocation.error`` chat shows the Agent.
    """

    def __init__(self, original: Exception) -> None:
        super().__init__(type(original).__name__)
        self.original = original


class AgentEvalEffectError(RuntimeError):
    """A finite, publishable Eval effect-routing failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvalEffectOutcome:
    """Adapter output plus bounded evidence used by deterministic scorers."""

    result: Any
    evidence_ref: str
    authorized_numbers: tuple[float, ...] = ()
    citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _IDENTITY_RE.fullmatch(self.evidence_ref) is None:
            raise ValueError("Agent Eval effect evidence reference is invalid")
        if len(self.authorized_numbers) > 10_000 or len(self.citations) > 1_000:
            raise ValueError("Agent Eval effect evidence is too large")
        if any(not _is_finite_number(value) for value in self.authorized_numbers):
            raise ValueError("Agent Eval authorized number is invalid")
        if any(not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None for value in self.citations):
            raise ValueError("Agent Eval citation identity is invalid")


class EvalFixtureResolver(Protocol):
    async def resolve(
        self,
        *,
        fixture_ref: Mapping[str, Any],
        tool_name: str,
        arguments: Mapping[str, Any],
        ctx: ToolExecutionContext,
    ) -> EvalEffectOutcome: ...


EvalEffectAdapter = Callable[
    [str, Mapping[str, Any], ToolExecutionContext],
    Awaitable[EvalEffectOutcome],
]


@dataclass(slots=True)
class EvalEffectAdapterRegistry:
    """Exact-version fake/sandbox adapters; there is deliberately no fallback."""

    _adapters: dict[str, EvalEffectAdapter] = field(default_factory=dict)

    def register(self, exact_ref: str, adapter: EvalEffectAdapter) -> None:
        if _EXACT_REF_RE.fullmatch(exact_ref) is None:
            raise ValueError("Agent Eval effect adapter must use an exact version")
        if exact_ref in self._adapters:
            raise ValueError("Agent Eval effect adapter is already registered")
        self._adapters[exact_ref] = adapter

    async def invoke(
        self,
        *,
        exact_ref: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        ctx: ToolExecutionContext,
    ) -> EvalEffectOutcome:
        adapter = self._adapters.get(exact_ref)
        if adapter is None:
            raise AgentEvalEffectError("eval_effect_adapter_missing")
        return await adapter(tool_name, arguments, ctx)


@dataclass(slots=True)
class EvalTraceCollector:
    """Bounded scorer evidence; never retains raw Tool arguments or results."""

    max_tool_calls: int = 200
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    authorized_numbers: set[float] = field(default_factory=set)
    citations: set[str] = field(default_factory=set)
    approval_attempts: int = 0
    denials: int = 0
    production_handler_called: bool = False
    tool_budget_exceeded: bool = False
    data_read: bool = False
    # The case task itself supplied figures, so the answer's figures have a source to match.
    numeric_input: bool = False
    # The last Tool whose call raised instead of returning; names the Tool when the attempt then fails.
    failed_tool: str | None = None

    def record(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_class: str,
        route: str,
        duration_ms: int,
        evidence_ref: str | None = None,
        denial_code: str | None = None,
        approval_attempted: bool = False,
        authorized_numbers: tuple[float, ...] = (),
        citations: tuple[str, ...] = (),
    ) -> None:
        # The scratch-local control Tool (subagent_return) is how an Agent finishes; it has no effect,
        # so it must not consume the case's Tool budget (a zero-Tool case would otherwise fail every
        # Agent whose contract ends with subagent_return). A call the environment could not run did
        # nothing either. Both stay bounded by the global trace cap.
        uncounted = not counts_against_tool_budget({"route": route, "denial_code": denial_code})
        counted = sum(1 for call in self.tool_calls if counts_against_tool_budget(call))
        if (uncounted and len(self.tool_calls) >= _MAX_TRACE_TOOL_CALLS) or (not uncounted and counted >= self.max_tool_calls):
            self.tool_budget_exceeded = True
            raise AgentEvalEffectError("eval_tool_budget_exceeded")
        if approval_attempted:
            self.approval_attempts += 1
        if denial_code is not None:
            self.denials += 1
        elif route in DATA_ROUTES:
            self.data_read = True
        self.authorized_numbers.update(float(value) for value in authorized_numbers)
        self.citations.update(citations)
        self.tool_calls.append(
            {
                "args_hash": _hash_document(arguments),
                "denial_code": denial_code,
                "duration_ms": max(0, int(duration_ms)),
                "effect_class": effect_class,
                "evidence_ref": evidence_ref,
                "route": route,
                "tool_name": tool_name,
                "tool_ref": eval_tool_identity(tool_name, arguments),
            }
        )

    def to_scorer_trace(self) -> dict[str, Any]:
        return {
            "approval_attempts": self.approval_attempts,
            "authorized_numbers": sorted(self.authorized_numbers),
            "citations": sorted(self.citations),
            "data_read": self.data_read,
            "numeric_input": self.numeric_input,
            "denials": self.denials,
            "production_handler_called": self.production_handler_called,
            "tool_budget_exceeded": self.tool_budget_exceeded,
            "tool_calls": list(self.tool_calls),
        }


class EvalToolRegistryView(ToolRegistry):
    """Safe Tool view that also captures Registry-level fail-closed denials."""

    def __init__(
        self,
        base: ToolRegistry,
        *,
        trace: EvalTraceCollector,
        effect_classes: Mapping[str, str],
    ) -> None:
        super().__init__()
        self._specs = dict(base._specs)
        self._overlay_names = base.overlay_names
        self._eval_trace = trace
        self._eval_effect_classes = dict(effect_classes)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
        *,
        propagate_ambiguous_transport: bool = False,
    ) -> Any:
        recorded = [False]
        token = CALL_RECORDED.set(recorded)
        try:
            invocation = await super().execute(
                name,
                arguments,
                ctx,
                propagate_ambiguous_transport=propagate_ambiguous_transport,
            )
        except Exception:
            # A Tool (sandbox, data connection) that raises can end the attempt; the Owner is told which.
            self._eval_trace.failed_tool = name
            raise
        finally:
            CALL_RECORDED.reset(token)
        # The live Datarc route already recorded a call whose Tool failed the way it fails in chat.
        if invocation.error and not recorded[0]:
            self._eval_trace.record(
                tool_name=name,
                arguments=arguments,
                effect_class=self._eval_effect_classes.get(
                    name,
                    EffectClass.SIDE_EFFECT_POSSIBLE.value,
                ),
                route="denied",
                duration_ms=max(0, int(invocation.duration_ms or 0)),
                denial_code=_registry_denial_code(invocation.error),
            )
        return invocation


@dataclass(slots=True)
class EvalEffectRouter:
    """Replace every executable Tool handler before an Eval Agent can run.

    Only the scratch-local ``subagent_return`` control handler and Tools that declare
    ``READ_ONLY`` run their real handler; a read-only Tool still prefers an exact adapter
    or the case's versioned fixture. Every other Tool resolves via an exact fake / sandbox
    adapter or is denied as not run. Missing metadata is treated as side-effect-capable.
    """

    effect_policy: Mapping[str, Any]
    fixture_ref: Mapping[str, Any]
    fixture_resolver: EvalFixtureResolver | None
    adapter_registry: EvalEffectAdapterRegistry
    trace: EvalTraceCollector
    authority_check: Callable[[], Awaitable[None]] | None = None
    control_passthrough: frozenset[str] = frozenset({SUBAGENT_RETURN})
    # FEATURE_AGENT_EVAL_DATARC_READ_ONLY: the attempt's live read-only Datarc route; None keeps every
    # Datarc call denied as not run (switch off, system Agents).
    datarc_live: EvalDatarcLiveRoute | None = None

    def __post_init__(self) -> None:
        mode = self.effect_policy.get("mode")
        if mode not in {"no_effects", "read_only_fixture", "sandbox", "fake_only"}:
            raise ValueError("Agent Eval effect mode is invalid")
        adapter_refs = self.effect_policy.get("adapter_refs", {})
        if not isinstance(adapter_refs, Mapping):
            raise ValueError("Agent Eval adapter refs must be an object")
        for tool_name, exact_ref in adapter_refs.items():
            if not isinstance(tool_name, str) or _IDENTITY_RE.fullmatch(tool_name) is None:
                raise ValueError("Agent Eval adapter Tool identity is invalid")
            if not isinstance(exact_ref, str) or _EXACT_REF_RE.fullmatch(exact_ref) is None:
                raise ValueError("Agent Eval effect adapter must use an exact version")

    def build_safe_view(self, registry: ToolRegistry) -> ToolRegistry:
        """Return a schema-identical view whose effect contracts and handlers are safe."""

        view = registry
        effect_classes: dict[str, str] = {}
        for name in registry.list_names():
            original = registry.get(name)
            effect_classes[name] = (
                original.effect_spec.effect_class.value if original.effect_spec is not None else EffectClass.SIDE_EFFECT_POSSIBLE.value
            )
            safe = replace(
                original,
                handler=self._safe_handler(name, original),
                # The normal approval gate runs before registry.execute.  Eval records
                # the attempt itself and never creates a real approval request.
                needs_approval=False,
                # Prevent Terminal effect journaling from treating a fake call as a
                # provider-side write before our safe handler is reached.
                effect_spec=TOOL_EFFECT_NONE,
            )
            view = view.clone_with_spec_override(safe)
        return EvalToolRegistryView(
            view,
            trace=self.trace,
            effect_classes=effect_classes,
        )

    def _safe_handler(self, tool_name: str, original: ToolSpec) -> Callable[..., Awaitable[Any]]:
        async def _handler(arguments: dict[str, Any], ctx: ToolExecutionContext) -> Any:
            if self.authority_check is not None:
                await self.authority_check()
            started = time.perf_counter()
            effect_class = original.effect_spec.effect_class.value if original.effect_spec is not None else EffectClass.SIDE_EFFECT_POSSIBLE.value
            approval_attempted = bool(original.needs_approval)
            try:
                if tool_name in self.control_passthrough:
                    if original.effect_spec is None or original.effect_spec.effect_class is not EffectClass.NONE:
                        raise AgentEvalEffectError("eval_control_tool_contract_invalid")
                    result = await original.handler(arguments, ctx)
                    self.trace.record(
                        tool_name=tool_name,
                        arguments=arguments,
                        effect_class=effect_class,
                        route=SCRATCH_CONTROL_ROUTE,
                        duration_ms=_elapsed_ms(started),
                        approval_attempted=approval_attempted,
                    )
                    return result

                exact_ref = self._adapter_ref(tool_name)
                if exact_ref is not None:
                    outcome = await self.adapter_registry.invoke(
                        exact_ref=exact_ref,
                        tool_name=tool_name,
                        arguments=arguments,
                        ctx=ctx,
                    )
                    if not isinstance(outcome, EvalEffectOutcome):
                        raise AgentEvalEffectError("eval_effect_adapter_invalid")
                    self._record_outcome(
                        tool_name=tool_name,
                        arguments=arguments,
                        effect_class=effect_class,
                        route="exact_adapter",
                        outcome=outcome,
                        started=started,
                        approval_attempted=approval_attempted,
                    )
                    return outcome.result

                if (
                    self.effect_policy.get("mode") == "read_only_fixture"
                    and original.effect_spec is not None
                    and original.effect_spec.effect_class is EffectClass.READ_ONLY
                    and self.fixture_ref
                    and self.fixture_resolver is not None
                ):
                    outcome = await self.fixture_resolver.resolve(
                        fixture_ref=self.fixture_ref,
                        tool_name=tool_name,
                        arguments=arguments,
                        ctx=ctx,
                    )
                    if not isinstance(outcome, EvalEffectOutcome):
                        raise AgentEvalEffectError("eval_effect_adapter_invalid")
                    self._record_outcome(
                        tool_name=tool_name,
                        arguments=arguments,
                        effect_class=effect_class,
                        route="versioned_fixture",
                        outcome=outcome,
                        started=started,
                        approval_attempted=approval_attempted,
                    )
                    return outcome.result

                if self.datarc_live is not None and self.datarc_live.handles(tool_name, self.effect_policy.get("mode")):
                    # The production handler runs with an allowlisted read-only Datarc binding; the route
                    # records the call and classifies its outcome from the call ledger.
                    return await self.datarc_live.run(
                        tool_name=tool_name,
                        original=original,
                        arguments=arguments,
                        ctx=ctx,
                        trace=self.trace,
                        effect_class=effect_class,
                        started=started,
                        approval_attempted=approval_attempted,
                    )

                if original.effect_spec is not None and original.effect_spec.effect_class is EffectClass.READ_ONLY:
                    try:
                        result = await original.handler(arguments, ctx)
                    except Exception:
                        # A failing read (e.g. a search timeout) is something the Agent must cope
                        # with, as in production; it is not an effect-policy violation.
                        result = {"ok": False, "error": "tool_failed", "error_code": "tool_failed"}
                    # Live data is the only source the answer's figures can come from.
                    from bowhead_core.number_verifier import extract_numbers

                    numbers: set[float] = set()
                    extract_numbers(result, numbers)
                    self.trace.record(
                        tool_name=tool_name,
                        arguments=arguments,
                        effect_class=effect_class,
                        route=LIVE_READ_ONLY_ROUTE,
                        duration_ms=_elapsed_ms(started),
                        approval_attempted=approval_attempted,
                        authorized_numbers=tuple(sorted(n for n in numbers if math.isfinite(n))[:_MAX_LIVE_AUTHORIZED_NUMBERS]),
                    )
                    return result

                raise AgentEvalEffectError(NOT_RUN_IN_EVAL_DENIAL)
            except DatarcToolRaised as exc:
                # A Datarc Tool's own failure, already recorded: chat shows it to the Agent the same way.
                raise exc.original from None
            except AgentEvalEffectError as exc:
                self._record_denial(
                    tool_name=tool_name,
                    arguments=arguments,
                    effect_class=effect_class,
                    started=started,
                    denial_code=exc.code,
                    approval_attempted=approval_attempted,
                )
                return {
                    "error": exc.code,
                    "error_code": exc.code,
                    "ok": False,
                }
            except Exception:
                code = "eval_effect_adapter_failed"
                self._record_denial(
                    tool_name=tool_name,
                    arguments=arguments,
                    effect_class=effect_class,
                    started=started,
                    denial_code=code,
                    approval_attempted=approval_attempted,
                )
                return {
                    "error": code,
                    "error_code": code,
                    "ok": False,
                }

        return _handler

    def _adapter_ref(self, tool_name: str) -> str | None:
        refs = self.effect_policy.get("adapter_refs", {})
        value = refs.get(tool_name) if isinstance(refs, Mapping) else None
        return str(value) if value is not None else None

    def _record_outcome(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_class: str,
        route: str,
        outcome: EvalEffectOutcome,
        started: float,
        approval_attempted: bool,
    ) -> None:
        self.trace.record(
            tool_name=tool_name,
            arguments=arguments,
            effect_class=effect_class,
            route=route,
            duration_ms=_elapsed_ms(started),
            evidence_ref=outcome.evidence_ref,
            approval_attempted=approval_attempted,
            authorized_numbers=outcome.authorized_numbers,
            citations=outcome.citations,
        )

    def _record_denial(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_class: str,
        started: float,
        denial_code: str,
        approval_attempted: bool,
    ) -> None:
        self.trace.record(
            tool_name=tool_name,
            arguments=arguments,
            effect_class=effect_class,
            route="denied",
            duration_ms=_elapsed_ms(started),
            denial_code=denial_code,
            approval_attempted=approval_attempted,
        )


def _hash_document(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _registry_denial_code(error: str) -> str:
    if error.startswith("ToolPermissionError:"):
        return "tool_permission_denied"
    if error == "pbi_connection_unavailable":
        return "pbi_connection_unavailable"
    if error.startswith("DatarcUnavailableError:"):
        return "datarc_connection_unavailable"
    return "eval_tool_execution_failed"
