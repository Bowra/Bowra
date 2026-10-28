"""cca block ④e — team tool → ToolSpec wrapping (tech-design §3.2/§3.5/§8).

Bridges the in-process MCP engine (openapi_tool_source) into the per-turn tool
view: each team operation becomes a Bowhead ``ToolSpec`` whose handler dispatches
through the turn's ``ClientSession`` and maps the MCP ``CallToolResult`` to
Bowhead-native ``{ok, error_kind, error|data}`` (§8 — single mapping point, does
NOT touch ``_extract_result_ok``). The returned spec list IS this team's tool set
(the subagent whitelist augmentation in ④ uses it directly; ``overlay_names``).

The LOAD POINT (turn_runner) calls ``native_registry.clone_with_overlay(
build_team_specs(...))`` once per turn and stashes the view on ``ctx.tool_view``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx2
from bowhead_router.types import ToolSchema

from bowhead_core.agents.prompts.peer_block import sanitize_external_tool_description
from bowhead_core.openapi_tool_source import TeamToolEntry
from bowhead_core.scrub import scrub as _scrub
from bowhead_core.tool_registry import TeamToolExecutionBinding, ToolExecutionContext, ToolSpec
from bowhead_core.types import ToolAnnotations

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# §9 per-turn team-tool call cap (abuse guard, D2). Generous — the P1 datarc slice is
# read/GET + low-volume; a turn never legitimately calls team tools 50×.
_DEFAULT_CALL_BUDGET = 50


class ToolCallClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def _args_digest(args: dict[str, Any] | None) -> str | None:
    """Non-reversible digest of the args for the audit log (NEVER store raw args — they
    may carry sensitive values; the table holds args_digest only, §9.6)."""
    if not args:
        return None
    try:
        blob = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _content_text(content: Sequence[Any] | None) -> str:
    """Join the text of an MCP result's content blocks (TextContent.text)."""
    parts: list[str] = []
    for block in content or []:
        t = getattr(block, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def _upstream_auth_error(source_id: str) -> str:
    """credential-readiness R7 (PRD 6.0.3 ②) — the LLM-facing TEAM attribution for a
    credential 4xx. Entirely fixed text: the trusted producer's message (④c fixed
    template) carries no extra information, and a remote MCP server's message is
    untrusted — interpolating either could only inject content into a system-voiced
    instruction (review P3), so neither is included."""
    return (
        f"the upstream rejected this call's credential (HTTP 401/403 — missing, "
        f"expired, or revoked). This tool belongs to TEAM tool source '{source_id}', "
        f"whose credential is managed by team admins in Settings · Tools. This is a "
        f"team-level configuration issue — do NOT suggest the user reconfigure a "
        f"personal account, and do NOT retry (the same credential will fail again). "
        f"Tell the user to ask a team admin to update this tool source's credential."
    )


def is_upstream_auth_error(exc: BaseException, _depth: int = 0) -> bool:
    """True when ``exc`` is (or wraps) an httpx2 HTTP 401/403 status error.

    A remote MCP team source (WP2/WP2b, incl. expired OAuth bearer) surfaces its
    401 as an ``httpx2.HTTPStatusError`` raised inside the SDK's anyio task group —
    i.e. possibly nested in ``ExceptionGroup`` and/or chained via ``__cause__`` —
    so walk both, depth-capped."""
    if _depth > 6:
        return False
    if isinstance(exc, httpx2.HTTPStatusError):
        try:
            return exc.response.status_code in (401, 403)
        except Exception:  # noqa: S110 — malformed error object: not classifiable
            return False
    if isinstance(exc, BaseExceptionGroup):
        return any(is_upstream_auth_error(sub, _depth + 1) for sub in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return is_upstream_auth_error(cause, _depth + 1)
    return False


def _is_timeout_error(exc: BaseException, _depth: int = 0) -> bool:
    """True when ``exc`` is (or wraps) a call timeout.

    mcp-connect-repair F1-a: a slow remote MCP tool now surfaces as ``TimeoutError`` (the caller's
    ``asyncio.wait_for``) or ``httpx2.TimeoutException`` (the transport backstop) — and, like the 401
    path above, the SDK's anyio task group may nest it in an ``ExceptionGroup`` and/or chain it.
    Without this branch it lands in the generic ``protocol`` bucket, whose message
    ("ExceptionGroup") tells the model and the user nothing actionable."""
    if _depth > 6:
        return False
    if isinstance(exc, TimeoutError | httpx2.TimeoutException):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_timeout_error(sub, _depth + 1) for sub in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_timeout_error(cause, _depth + 1)
    return False


def _make_team_handler(
    team_id: str,
    source_id: str,
    full_name: str,
    operation_id: str,
    advertised_name: str,
    client: ToolCallClient,
    budget: list[int],
    call_budget: int,
    audit_store: Any | None,
) -> Any:
    """Closure dispatching one team tool through the turn's ClientSession + mapping the
    result to Bowhead-native form. Captures the owning team_id for the §3.5 dispatch-time
    team-bind, a per-TURN shared call counter (``budget`` — every handler in this turn's
    view shares it, so the cap spans Lead + subagents even across F6 ctx copies), and the
    audit store (§9.6 exec log)."""

    async def handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        # §9 per-turn call cap (abuse guard, D2): one shared counter across this turn's
        # Lead + subagents — closure-captured, so a subagent's copy.copy(ctx) can't reset it.
        # With agent run limits on, the Team's per-turn tool-call limit (counted for every
        # tool in SubagentRunner._execute_tool) replaces this Team-tool-specific cap.
        from bowhead_core.agent_run_limits import get_tool_call_budget

        if get_tool_call_budget() is None:
            budget[0] += 1
            if budget[0] > call_budget:
                return {"ok": False, "error_kind": "rate_limited", "error": "team tool call budget exceeded for this turn"}
        # §3.5: the per-turn view already isolates by team; re-assert the calling ctx
        # belongs to the owning team (fail-closed on falsy / mismatched team_id).
        if not getattr(ctx, "team_id", "") or ctx.team_id != team_id:
            return {"ok": False, "error_kind": "team_denied", "error": "tool not available for this team"}
        t0 = time.perf_counter()
        from bowhead_core.resource_execution_gate import check_team_tool_execution

        res = await check_team_tool_execution(
            ctx,
            team_id=team_id,
            source_id=source_id,
            operation_id=operation_id,
            advertised_name=advertised_name,
        )
        try:
            result = await client.call_tool(full_name, args or {}) if res is None else None
        except Exception as e:
            # credential-readiness R7 review P2: a REMOTE MCP team source (WP2, incl.
            # OAuth) surfaces its 401/403 as a transport exception on this path — the
            # in-process engine's upstream_auth envelope never fires for it. Classify
            # here so both team-source shapes get the same admin attribution.
            if is_upstream_auth_error(e):
                res: dict[str, Any] = {"ok": False, "error_kind": "upstream_auth", "error": _upstream_auth_error(source_id)}
            elif _is_timeout_error(e):
                # F1-a: name the actual failure. The tool is reachable and authorized; it just took
                # too long. Retrying the same call verbatim will normally time out again.
                res = {
                    "ok": False,
                    "error_kind": "timeout",
                    "error": (
                        "The tool did not respond in time. It is reachable — this call was too slow. Narrow the request or use a different tool; do not retry it unchanged."
                    ),
                }
            else:
                res = {"ok": False, "error_kind": "protocol", "error": _scrub(type(e).__name__)}
        else:
            if res is not None:
                result = None
            elif result is None:  # pragma: no cover - defensive protocol guard
                res = {"ok": False, "error_kind": "protocol", "error": "empty tool result"}
            elif result.is_error:
                # ④b surfaces unknown-tool / invalid-args / executor-raise as isError.
                res = {"ok": False, "error_kind": "tool_error", "error": _scrub(_content_text(result.content))}
            else:
                # success. Our in-process OpenAPI engine (④c http_call) emits the NATIVE
                # {ok, error_kind?, data?} envelope as structuredContent → recognise it by the
                # "ok" key and pass it through (so the upstream's own JSON body can't be mistaken
                # for the engine envelope, cca review #5). A REMOTE MCP server (WP2), by contrast,
                # emits its OWN arbitrary structuredContent (no "ok") — wrap that as data so the
                # tool still reads as a success (else res.get("ok") is falsy and the exec-audit
                # below would mislabel a successful remote call as "error"). Live-verified against
                # a real FastMCP server that returns {"result": ...}.
                structured = getattr(result, "structured_content", None)
                if isinstance(structured, dict) and "ok" in structured:
                    res = structured
                    # credential-readiness R7 (PRD 6.0.3 ②): a credential 4xx from a TEAM
                    # tool source is fixable only by a team admin — say so to the LLM here,
                    # at the single mapping point, where the source identity is known.
                    # The error text is REPLACED
                    # with fixed copy, never appended to: a spoofable envelope's own text
                    # must not ride inside a system-voiced instruction (review P3).
                    if res.get("error_kind") == "upstream_auth":
                        res = {**res, "error": _upstream_auth_error(source_id)}
                elif isinstance(structured, dict):
                    res = {"ok": True, "data": structured}
                else:
                    res = {"ok": True, "data": _scrub(_content_text(result.content))}
        # §9.6 exec audit (execute-then-meter; NO decrypted secret, NO raw args — digest only).
        if audit_store is not None:
            try:
                await audit_store.log_tool_call(
                    team_id=team_id,
                    tool_name=full_name,
                    status="ok" if res.get("ok") else "error",
                    user_id=getattr(ctx, "user_id", "") or None,
                    source_id=source_id,
                    args_digest=_args_digest(args),
                    error_kind=res.get("error_kind"),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            except Exception as _audit_exc:
                logger.warning("cca.team_tool_audit_failed team=%s tool=%s err=%s", team_id, full_name, type(_audit_exc).__name__)
        return res

    return handler


def build_team_specs(
    team_id: str,
    entries: list[TeamToolEntry],
    client: ToolCallClient,
    *,
    audit_store: Any | None = None,
    call_budget: int = _DEFAULT_CALL_BUDGET,
    budget: list[int] | None = None,
) -> list[ToolSpec]:
    """Wrap each team operation as a ToolSpec dispatching through ``client`` (§3.2).

    Returns this team's explicit tool list — register it into a per-turn view via
    ``ToolRegistry.clone_with_overlay`` (the view stamps these names as
    ``overlay_names`` for the whitelist augmentation). ``needs_approval`` is derived
    verb-aware (writes flagged for a future Approval Loop, §10 — execution-time
    write-confirm is out of this sprint; P1 datarc slice is read/GET only).

    Metering (§9/§9.6, D2): a per-TURN shared call cap (``call_budget``, default 50 —
    one counter shared by every handler in this view so it spans Lead + subagents) and,
    when ``audit_store`` is given, an exec-audit row per call via ``log_tool_call``
    (no decrypted secret / no raw args). Per-call CREDIT is deferred (D2 — reuse the
    existing post-turn token deduct).
    """
    # WP2 (review P2): a shared per-TURN counter must span the WHOLE view — with multiple sources
    # (OpenAPI + N MCP) build_team_specs is called N+1 times, so the caller threads ONE budget list
    # through every call. Falling back to a fresh [0] preserves the single-call behaviour.
    budget = budget if budget is not None else [0]
    specs: list[ToolSpec] = []
    for entry in entries:
        # advertised_name is the stable teammcp.<src>.<op> identity (LLM schema + whitelist +
        # grants); dispatch_name is what the ClientSession is called with (== advertised for an
        # OpenAPI in-process server; the remote raw tool name for an MCP source, WP2).
        specs.append(
            ToolSpec(
                schema=ToolSchema(
                    name=entry.advertised_name,
                    # 2026-07-22: all three sources (OpenAPI summary/user overrides/remote MCP) are untrusted and
                    # persisted without sanitization. This is their sole convergence point before LLM exposure; sanitizing here covers all
                    # (including existing dirty data; Lead <team_tools> reads this field too, so it is sanitized as well).
                    description=sanitize_external_tool_description(entry.description),
                    input_schema=entry.input_schema,
                ),
                handler=_make_team_handler(
                    team_id,
                    entry.source_id,
                    entry.dispatch_name,
                    entry.op.operation_id if entry.op is not None else entry.dispatch_name,
                    entry.advertised_name,
                    client,
                    budget,
                    call_budget,
                    audit_store,
                ),
                needs_approval=entry.destructive,
                risk_level="medium" if entry.destructive else "low",
                # tools sprint WP2c — role binding correctness. The REAL "which agent may use this
                # team tool" axis is now the per-subagent bundle (overlay ∩ bundle), NOT this role
                # stamp: the Lead is excluded because its team_tool_bundle is () → the whitelist filter
                # drops every teammcp.* from its schema (agent_runner_adapter WP2c), so the stamp no
                # longer carries the Lead-exclusion meaning it used to (spike-role-binding.md).
                # {analyst} is kept purely as a defense-in-depth execute() belt: every spawned peer
                # runs as role='analyst' (F6 role-swap), so it's "spawned-peer-only, never the Lead
                # directly" — it does NOT assert a data-analysis domain. (Replacing this belt with a
                # bundle-membership check needs bundle-in-ctx threading — deliberate follow-up, L2.)
                allowed_for_roles=frozenset({"analyst"}),
                # P2d (tool-domain): team tools self-declare — external (open world, not the
                # connected workspace dataset) so a topical follow-up continues using them rather
                # than defaulting to bi_query. destructive mirrors is_write (= needs_approval).
                # Carried on the spec → stamped onto the invocation (vs the lookup_domain floor).
                source="external",
                annotations=ToolAnnotations(
                    open_world=True,
                    destructive=entry.destructive,
                    read_only=not entry.destructive,
                ),
                team_tool_binding=TeamToolExecutionBinding(
                    team_id=team_id,
                    source_id=entry.source_id,
                    operation_id=entry.op.operation_id if entry.op is not None else entry.dispatch_name,
                    advertised_name=entry.advertised_name,
                ),
            )
        )
    return specs
