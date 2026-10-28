"""cca: OpenAPI tool-source engine — slice ④a (parse + tool synthesis).

Turns an team-authored OpenAPI spec into MCP tool definitions. The engine form is
spike-proven (tech-design §2): low-level ``mcp.server.Server`` + explicit
``mcp.types.Tool(input_schema=...)`` + an in-process legacy-protocol client
(NO fastmcp 2.x). This slice covers the deterministic
    OpenAPI op  →  merged JSON-Schema inputSchema (+ param-location map)  →  types.Tool
mapping (§2.1) and the stable ``teammcp.<source>.<op>`` naming (§7.2). The in-process
MCP server, httpx2 execution (SSRF guard / Fernet auth / trim / scrub / isError),
ToolAuthRegistry, and build_team_specs land in later slices.

$ref resolution walks the RAW ``spec_dict`` with our own local JSON-pointer resolver
(``_resolve_ref`` / ``_json_pointer``). A recursive, depth+budget-bounded walk fully
de-references nested $refs so the LLM-facing inputSchema is self-contained ($ref-free),
and a circular / pathologically nested user spec cannot hang the parser (DoS guard —
specs come from team admins, semi-trusted). Spec VALIDATION still uses
``openapi_spec_validator``. (We deliberately do NOT use ``jsonschema_path.SchemaPath``:
its ``pathable`` 0.5.0 dep has a stateful prefix-cache regression that crashes ordinary
navigation — see ``_resolve_ref`` for the full note.)
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx2
import jsonschema
import mcp.types
from mcp import Client
from mcp.server import Server

from bowhead_core.mcp_prompt_cache import (
    MAX_MCP_PROMPT_ARGUMENTS,
    MAX_MCP_PROMPT_CACHE_BYTES,
    MAX_MCP_PROMPT_COUNT,
)
from bowhead_core.scrub import scrub as _scrub  # ④f: shared neutral scrubber (re-exported here)
from bowhead_core.ssrf_guard import Resolver, SsrfBlocked, resolve_and_validate

if TYPE_CHECKING:
    from mcp.client.session import ClientSession

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
# Verbs that mutate state — derive needs_approval (verb-aware, tech-design §10).
_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})
# Bound the $ref-deref walk so a circular / pathologically deep|wide user spec
# cannot hang the parser. Real API schemas nest far shallower than this.
_MAX_DEREF_DEPTH = 40
_MAX_DEREF_NODES = 5000
_MAX_TOOL_NAME = 128
_MAX_PROMPT_PAGES = 256
_MAX_PROMPT_RESPONSE_BYTES = 4 * 1024 * 1024
_MCP_TRANSPORT_LOGS_SUPPRESSED: ContextVar[bool] = ContextVar(
    "mcp_transport_logs_suppressed",
    default=False,
)


class _McpTransportPayloadLogFilter(logging.Filter):
    """Keep the MCP SDK's unsanitized transport records out of application logs."""

    def filter(self, _record: logging.LogRecord) -> bool:
        # The SDK interpolates JSON-RPC payloads, session identifiers and raw URLs
        # directly into records. Its exception tracebacks can also include the
        # invalid response input. Bowhead exposes finite scrubbed failure codes at
        # its own boundary, so no record from these dependency loggers is safe.
        return False


class _McpScopedDependencyLogFilter(logging.Filter):
    """Suppress HTTP dependency records only inside an MCP transport context."""

    def filter(self, _record: logging.LogRecord) -> bool:
        return not _MCP_TRANSPORT_LOGS_SUPPRESSED.get()


def _install_mcp_transport_payload_log_filter() -> None:
    for logger_name in ("mcp.client.sse", "mcp.client.streamable_http"):
        transport_logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _McpTransportPayloadLogFilter) for item in transport_logger.filters):
            transport_logger.addFilter(_McpTransportPayloadLogFilter())
    for logger_name in (
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
        "httpcore2.socks",
        "httpx2",
    ):
        dependency_logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _McpScopedDependencyLogFilter) for item in dependency_logger.filters):
            dependency_logger.addFilter(_McpScopedDependencyLogFilter())


_install_mcp_transport_payload_log_filter()


class OpenApiParseError(ValueError):
    """Raised when an OpenAPI spec is malformed / cannot be parsed."""


class McpResponseTooLarge(ValueError):
    """Raised before an MCP response can be materialized beyond its wire budget."""


async def _limit_mcp_response_bytes(
    response: httpx2.Response,
    *,
    limit: int,
) -> None:
    """Bound uncompressed JSON and SSE bytes before the MCP SDK parses a response."""
    content_encoding = response.headers.get("content-encoding", "").strip().lower()
    if content_encoding and content_encoding != "identity":
        await response.aclose()
        raise McpResponseTooLarge("compressed MCP responses are not accepted")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > limit:
            await response.aclose()
            raise McpResponseTooLarge("MCP response exceeds byte limit")
    original = response.aiter_bytes

    async def bounded(chunk_size: int | None = None):
        consumed = 0
        async for chunk in original(chunk_size):
            consumed += len(chunk)
            if consumed > limit:
                await response.aclose()
                raise McpResponseTooLarge("MCP response exceeds byte limit")
            yield chunk

    # SDK JSON reads and SSE line decoding both resolve this instance method
    # after response hooks run. Counting decoded bytes also covers compression.
    response.aiter_bytes = bounded  # type: ignore[method-assign]


def _mcp_client_headers(
    headers: dict[str, str] | None,
    *,
    response_bytes_limit: int | None,
) -> httpx2.Headers:
    values = httpx2.Headers(headers or {})
    if response_bytes_limit is not None:
        # The byte limiter must see the uncompressed stream. Rejecting a server
        # that ignores this header prevents decompression bombs before HTTPX2 can
        # allocate one expanded decoder chunk.
        values["Accept-Encoding"] = "identity"
    return values


@dataclass(frozen=True)
class OpParam:
    """One OpenAPI operation parameter + where it goes on the wire."""

    name: str
    location: str  # "path" | "query" | "header"
    required: bool
    schema: dict[str, Any]


@dataclass(frozen=True)
class OpenApiOp:
    """A parsed, $ref-resolved OpenAPI operation ready to become an MCP tool."""

    operation_id: str
    method: str  # lowercase http verb
    path: str  # e.g. "/metrics/{mid}"
    summary: str
    params: tuple[OpParam, ...]
    body_required: bool
    body_schema: dict[str, Any] | None  # resolved application/json requestBody schema, or None
    input_schema: dict[str, Any]  # merged JSON Schema (object) advertised to the LLM
    body_prop_names: frozenset[str]  # top-level input names that map into the JSON body
    tags: tuple[str, ...] = ()  # op-level OpenAPI tags (C8: group-by-tag data source)

    @property
    def is_write(self) -> bool:
        return self.method in _WRITE_METHODS


# ─────────────────────────────────────────────────────────────
# $ref deref (bounded) — raw-dict + manual JSON-pointer resolver
#
# We parse the raw ``spec_dict`` directly (it is already plain JSON) and resolve LOCAL ``$ref``s
# ("#/…") ourselves rather than via ``jsonschema_path.SchemaPath``. The SchemaPath route pulls
# ``pathable`` whose 0.5.0 release has a stateful prefix-cache regression that crashes ordinary
# spec navigation with ``KeyError: 'paths'`` (latent prod bug — every OpenAPI-source create/validate
# would blow up). Owning the walk removes that fragile transitive dependency from the hot path and
# keeps the deref bounded (depth + node budget) against circular / pathological specs.
# ─────────────────────────────────────────────────────────────
def _json_pointer(spec_dict: dict[str, Any], ref: str) -> Any:
    """Resolve a LOCAL JSON pointer (``#/a/b/c``) against ``spec_dict`` → the target node, or None.
    External / non-local refs are unsupported (P1 read slice) and return None."""
    if not ref.startswith("#/"):
        return None
    cur: Any = spec_dict
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")  # RFC 6901 unescape
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            # RFC 6901 array token = "0" | [1-9][0-9]* — reject "-1"/" 1 "/"1_000"/"+1" (int() is too lenient).
            if not part.isdigit():
                return None
            try:
                cur = cur[int(part)]
            except IndexError:
                return None
        else:
            return None
    return cur


def _follow_ref(spec_dict: dict[str, Any], node: Any) -> Any:
    """Follow a TOP-LEVEL local ``$ref`` chain (SHALLOW — does NOT resolve descendants), bounded
    against a ``$ref`` cycle. Used for path-item / operation nodes so each operation's params + body
    get their OWN fresh deref budget in ``_parse_params``/``_parse_body`` — a fat (or unselected) op
    must not starve a selected op by exhausting one shared budget (review regression)."""
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen or len(seen) > _MAX_DEREF_DEPTH:
            return {}
        seen.add(ref)
        node = _json_pointer(spec_dict, ref)
        if node is None:
            return {}
    return node


def _resolve_ref(
    spec_dict: dict[str, Any],
    node: Any,
    _depth: int = 0,
    _budget: list[int] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> Any:
    """Fully resolve local ``$ref``s in ``node`` → plain JSON. Depth + node-budget caps bound the
    work; ``_seen`` breaks ``$ref`` cycles (a circular / absurdly deep|wide spec degrades to ``{}``
    at the cap instead of hanging — DoS guard)."""
    if _budget is None:
        _budget = [_MAX_DEREF_NODES]
    if _depth > _MAX_DEREF_DEPTH or _budget[0] <= 0:
        return {}
    _budget[0] -= 1
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in _seen:  # cycle
                return {}
            target = _json_pointer(spec_dict, ref)
            if target is None:
                return {}
            return _resolve_ref(spec_dict, target, _depth + 1, _budget, _seen | {ref})
        return {k: _resolve_ref(spec_dict, v, _depth + 1, _budget, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_ref(spec_dict, v, _depth + 1, _budget, _seen) for v in node]
    return node


# ─────────────────────────────────────────────────────────────
# parse
# ─────────────────────────────────────────────────────────────
def _parse_params(spec_dict: dict[str, Any], raw: Any) -> tuple[OpParam, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[OpParam] = []
    for item in raw:
        p = _resolve_ref(spec_dict, item)  # param itself may be a $ref
        if not isinstance(p, dict):
            continue
        loc, name = p.get("in"), p.get("name")
        if loc not in ("path", "query", "header") or not isinstance(name, str) or not name:
            continue  # cookie params + malformed skipped (P1: cookie unsupported)
        schema = p.get("schema")
        out.append(
            OpParam(
                name=name,
                location=loc,
                # path params are always required per OpenAPI; else honour `required`
                required=bool(p.get("required", loc == "path")),
                schema=schema if isinstance(schema, dict) else {},
            )
        )
    return tuple(out)


def _parse_body(spec_dict: dict[str, Any], op: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    rb = _resolve_ref(spec_dict, op.get("requestBody"))  # requestBody may be a $ref
    if not isinstance(rb, dict):
        return False, None
    required = bool(rb.get("required", False))
    content = rb.get("content")
    if not isinstance(content, dict):
        return required, None
    media = next((ck for ck in content if "json" in ck.lower()), None)
    if media is None:
        return required, None  # non-JSON body unsupported in P1
    media_obj = content.get(media)
    schema = _resolve_ref(spec_dict, media_obj.get("schema")) if isinstance(media_obj, dict) else None
    return required, schema if isinstance(schema, dict) else None


def _merge_input_schema(
    params: tuple[OpParam, ...],
    body_required: bool,
    body_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Merge params (path/query/header) + JSON body into ONE object schema (§2.1).

    Body object properties are flattened to top-level (the wire location is kept
    separately in OpenApiOp.body_prop_names); a non-object body becomes a single
    ``body`` input. On a param↔body name collision the param wins (rare in the P1
    read slice). Locations are NOT exposed to the LLM (it sees only the merge).
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        props[p.name] = dict(p.schema)
        if p.required and p.name not in required:
            required.append(p.name)

    body_props: list[str] = []
    if isinstance(body_schema, dict):
        if body_schema.get("type") == "object" and isinstance(body_schema.get("properties"), dict):
            body_req = set(body_schema.get("required") or [])
            for bn, bs in body_schema["properties"].items():
                if bn in props:
                    continue  # param wins on collision
                props[bn] = bs
                body_props.append(bn)
                if bn in body_req and bn not in required:
                    required.append(bn)
        elif "body" not in props:
            props["body"] = body_schema
            body_props.append("body")
            if body_required:
                required.append("body")

    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema, body_props


def parse_openapi_ops(
    spec_dict: dict[str, Any],
    selected_op_ids: list[str] | None = None,
) -> list[OpenApiOp]:
    """Parse an OpenAPI 3.x spec into OpenApiOp[] (deterministic, $ref-resolved).

    ``selected_op_ids`` None → every operation that has an ``operationId``.
    Operations without an operationId are skipped (a stable id is required for the
    tool name, §7.2).
    """
    if not isinstance(spec_dict, dict) or not isinstance(spec_dict.get("paths"), dict):
        raise OpenApiParseError("spec is not an object with a 'paths' map")

    selected = set(selected_op_ids) if selected_op_ids is not None else None
    ops: list[OpenApiOp] = []
    paths = spec_dict["paths"]
    for path_str, path_item_raw in paths.items():
        # SHALLOW-follow a path-item $ref only (review regression fix): deep-resolving the whole path
        # item here would share ONE budget across every op's schemas + run BEFORE the `selected` filter,
        # so a fat/unselected op could starve a selected one → silently dropped. Deep deref stays at
        # param/body granularity below (each with its own fresh budget), after the operationId/selected filter.
        path_item = _follow_ref(spec_dict, path_item_raw)
        if not isinstance(path_item, dict):
            continue
        shared = _parse_params(spec_dict, path_item.get("parameters")) if "parameters" in path_item else ()
        for method, op_raw in path_item.items():
            if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
                continue  # 'parameters' / 'summary' / '$ref' / non-str keys / etc.
            op = _follow_ref(spec_dict, op_raw)  # shallow; params/body deref per-op below
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if not isinstance(op_id, str) or not op_id:
                continue
            if selected is not None and op_id not in selected:
                continue
            summary = ""
            for sk in ("summary", "description"):
                v = op.get(sk)
                if isinstance(v, str) and v.strip():
                    summary = v.strip()
                    break
            tags_raw = op.get("tags")
            tags = (
                tuple(
                    dict.fromkeys(  # CORR-5: de-dup, preserve order
                        t.strip() for t in tags_raw if isinstance(t, str) and t.strip()
                    )
                )
                if isinstance(tags_raw, list)
                else ()
            )
            op_params = shared + (_parse_params(spec_dict, op.get("parameters")) if "parameters" in op else ())
            body_required, body_schema = _parse_body(spec_dict, op) if "requestBody" in op else (False, None)
            input_schema, body_props = _merge_input_schema(op_params, body_required, body_schema)
            ops.append(
                OpenApiOp(
                    operation_id=op_id,
                    method=method.lower(),
                    path=str(path_str),
                    summary=summary,
                    params=op_params,
                    body_required=body_required,
                    body_schema=body_schema,
                    input_schema=input_schema,
                    body_prop_names=frozenset(body_props),
                    tags=tags,
                )
            )
    return ops


def validate_openapi_spec(spec_dict: dict[str, Any]) -> None:
    """Raise OpenApiParseError if not a valid OpenAPI 3.x document.

    Called by the create/preview endpoint layer (⑤) to reject malformed specs
    before tool synthesis. parse_openapi_ops itself stays lenient.
    """
    try:
        from openapi_spec_validator import validate as _validate

        _validate(spec_dict)
    except Exception as e:
        raise OpenApiParseError(f"invalid OpenAPI spec: {type(e).__name__}: {e}") from e


# ─────────────────────────────────────────────────────────────
# naming + types.Tool synthesis (§7.2)
# ─────────────────────────────────────────────────────────────
def _clean_seg(s: str) -> str:
    """Clean ONE tool-name segment (C11/MCP-1): keep ``[A-Za-z0-9_-]``, **collapse
    ``_`` runs to a single ``_``**, and trim leading/trailing separators.

    The collapse is the load-bearing part: the DeepSeek/Doubao adapter sanitizes
    tool names by ``replace(".", "__")`` on the way out and ``replace("__", ".")``
    on the way back (anthropic_compat.py, auto-on for those providers). Any literal
    ``__`` inside a segment would be eaten by the reverse pass → the *primary
    dev/prod model could not dispatch the tool*. Collapsing ``_`` runs guarantees no
    segment ever holds ``__`` (and trimming keeps the dot boundary from synthesizing
    one). Single ``_`` / ``-`` are preserved and round-trip safely.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", s)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_-")


def _slug(s: str, maxlen: int = 48) -> str:
    return _clean_seg(s)[:maxlen].strip("_-") or "x"


def op_tool_name(source_id: str, operation_id: str) -> str:
    """Stable, MCP-compliant tool name ``teammcp.<source_slug>.<op_slug>`` (§7.2).

    Derived from the IMMUTABLE source id (not the editable display name) + the
    operationId, so renaming a source never changes the tool name (the subagent
    whitelist survives). The dedicated ``teammcp.`` prefix keeps team tools off the
    global ``mcp.*`` stdio namespace → no cross-tenant whitelist leak (§7.2). Each
    segment is ``__``-free so the name survives the provider sanitize round-trip
    (C11/MCP-1).
    """
    src = _slug(source_id)
    op_clean = _clean_seg(operation_id)
    op = op_clean[:48].strip("_-") or "x"
    # MCP-3 + CORR-3: append a short stable hash of the FULL operationId when the slug is LOSSY —
    # truncated (>48) OR a '_' run was collapsed ('__' in the raw, which makes 'a_b' and 'a__b'
    # map to the same slug). Deterministic → build_team_mcp_server (dispatch) + build_team_specs
    # (advertise) compute the same name, so no two ops silently collide.
    if len(op_clean) > 48 or "__" in re.sub(r"[^A-Za-z0-9_-]", "-", operation_id):
        h = hashlib.blake2s(operation_id.encode("utf-8"), digest_size=3).hexdigest()
        op = (op_clean[:41].strip("_-") or "x") + "-" + h
    return f"teammcp.{src}.{op}"[:_MAX_TOOL_NAME]


def tool_name_conflicts(source_id: str, operation_ids: Iterable[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Groups of operations (or remote MCP tool names) that ``op_tool_name`` maps to one name.

    Sanitizing an operationId is lossy (``orders.list`` and ``orders-list``, or two Chinese
    operationIds, map to the same slug). Tool names are an interface that Agents and grants
    reference, so they are never renamed here; a source must not expose more than one
    operation of a group, otherwise one tool would silently call another endpoint.
    """
    groups: dict[str, list[str]] = {}
    for operation_id in dict.fromkeys(operation_ids):
        groups.setdefault(op_tool_name(source_id, operation_id), []).append(operation_id)
    return [(name, tuple(sorted(ids))) for name, ids in sorted(groups.items()) if len(ids) > 1]


def op_to_mcp_tool(source_id: str, op: OpenApiOp) -> mcp.types.Tool:
    """OpenApiOp → mcp.types.Tool (explicit inputSchema; the spike-proven primitive)."""
    return mcp.types.Tool(
        name=op_tool_name(source_id, op.operation_id),
        description=(op.summary or f"{op.method.upper()} {op.path}")[:1024],
        input_schema=op.input_schema,
    )


@dataclass(frozen=True)
class TeamToolEntry:
    """One team tool, ready to advertise + dispatch — the single shape the loader,
    ``build_team_specs``, the in-process ``build_team_mcp_server``, the subagent picker and
    the resource-grant check all consume, so those sites are kind-agnostic (tools sprint WP0).

    Two names, deliberately split (the MCP-dispatch prerequisite):
      - ``advertised_name`` — the stable ``teammcp.<src>.<slug>`` identity derived from the
        IMMUTABLE source id. This is what the LLM schema, the subagent whitelist and the
        resource grants key on; it survives a source rename.
      - ``dispatch_name`` — what the turn's ``ClientSession`` is actually called with. For an
        OpenAPI source the in-process MCP server keys on the same prefixed name, so
        ``dispatch_name == advertised_name``. For a remote MCP source (WP2) it is the server's
        OWN raw tool name (e.g. ``search_issues``), which is what the remote session expects.

    ``op`` carries the parsed OpenApiOp for OpenAPI execution (the ``OpExecutor`` needs it) and
    for the picker's OpenAPI-only display fields (method/path); it is ``None`` for an MCP entry.
    """

    source_id: str
    advertised_name: str
    dispatch_name: str
    description: str  # advertised description (override-applied summary, or a method+path fallback)
    input_schema: dict[str, Any]
    destructive: bool
    kind: str = "openapi"
    op: OpenApiOp | None = None
    # Remote MCP authoring risk must distinguish an explicit annotation from an
    # old cache row where the annotation was absent.  ``destructive`` remains
    # for rolling-reader compatibility; new policy uses these tri-state hints.
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None


def openapi_entry(source_id: str, op: OpenApiOp) -> TeamToolEntry:
    """Build an ``TeamToolEntry`` for one OpenAPI operation. ``advertised_name`` and
    ``dispatch_name`` are both the derived ``teammcp.<src>.<op>`` name (the in-process server
    dispatches on it), so every downstream consumer stays byte-identical to the pre-WP0 path."""
    name = op_tool_name(source_id, op.operation_id)
    return TeamToolEntry(
        source_id=source_id,
        advertised_name=name,
        dispatch_name=name,
        description=op.summary or f"{op.method.upper()} {op.path}",
        input_schema=op.input_schema,
        destructive=op.is_write,
        kind="openapi",
        op=op,
    )


def mcp_entry(source_id: str, tool_meta: dict[str, Any]) -> TeamToolEntry:
    """Build an ``TeamToolEntry`` for one remote MCP tool from its CACHED metadata (tools sprint WP2).

    ``advertised_name`` is the stable ``teammcp.<src>.<slug>`` identity (LLM schema / whitelist /
    grants); ``dispatch_name`` is the remote server's OWN raw tool name — that is what the remote
    ``ClientSession.call_tool`` must be called with. ``op`` is ``None`` (no OpenApiOp)."""
    raw = str(tool_meta["name"])
    annotations = tool_meta.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}

    def _hint(name: str) -> bool | None:
        value = annotations.get(name)
        return value if isinstance(value, bool) else None

    return TeamToolEntry(
        source_id=source_id,
        advertised_name=op_tool_name(source_id, raw),
        dispatch_name=raw,
        description=str(tool_meta.get("description") or raw),
        input_schema=dict(tool_meta.get("input_schema") or {"type": "object"}),
        destructive=bool(tool_meta.get("destructive")),
        kind="mcp",
        op=None,
        read_only_hint=_hint("readOnlyHint"),
        destructive_hint=_hint("destructiveHint"),
    )


# ─────────────────────────────────────────────────────────────
# in-process MCP server + client (spike-proven, tech-design §2/§3.4) — slice ④b
# ─────────────────────────────────────────────────────────────
# An OpExecutor performs the actual outbound call for one operation and returns a
# JSON-serializable result. ④b injects a stub for tests; ④c provides the real
# httpx2 executor (SSRF guard + Fernet auth + trim + scrub + isError classification).
OpExecutor = Callable[[str, OpenApiOp, dict[str, Any]], Awaitable[Any]]


def build_team_mcp_server(
    entries: list[TeamToolEntry],
    executor: OpExecutor,
    *,
    server_name: str = "teammcp",
) -> Server[Any]:
    """Build an in-process low-level MCP ``Server`` exposing one team's OpenAPI tools.

    Each entry is an ``TeamToolEntry`` (OpenAPI kind — ``entry.op`` present). ``list_tools``
    advertises the explicit ``types.Tool`` keyed on ``dispatch_name`` (== advertised_name for
    OpenAPI); ``call_tool`` runs the ``OpExecutor`` against ``entry.op`` and returns its
    JSON-serializable result as MCP structuredContent (a dict). An unknown tool name or an
    executor raise surfaces as ``CallToolResult.is_error=True`` (SDK-native); the per-turn
    view's ToolSpec handler maps that to Bowhead's ``{ok/error_kind/error}`` in ④e.
    JSON Schema validation explicitly gates malformed arguments before dispatch. (Remote MCP sources do NOT use this in-process server — they dispatch
    through their own remote session, WP2.)
    """
    index: dict[str, TeamToolEntry] = {e.dispatch_name: e for e in entries if e.op is not None}
    tools = [mcp.types.Tool(name=e.dispatch_name, description=e.description[:1024], input_schema=e.input_schema) for e in entries if e.op is not None]

    async def _list_tools(context: Any, params: Any) -> mcp.types.ListToolsResult:
        return mcp.types.ListToolsResult(tools=list(tools))

    async def _call_tool(context: Any, params: Any) -> mcp.types.CallToolResult:
        try:
            entry = index.get(params.name)
            if entry is None or entry.op is None:
                raise ValueError(f"tool not found: {params.name}")
            arguments = params.arguments or {}
            jsonschema.validate(arguments, entry.input_schema)
            result = await executor(entry.source_id, entry.op, arguments)
            data = result if isinstance(result, dict) else {"data": result}
            return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=json.dumps(data))], structured_content=data)
        except Exception as exc:
            return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=str(exc))], is_error=True)

    return Server(server_name, on_list_tools=_list_tools, on_call_tool=_call_tool)


@asynccontextmanager
async def connect_team_mcp(server: Server[Any]) -> AsyncIterator[ClientSession]:
    """Open an initialized legacy-protocol client against an in-process server."""
    async with Client(server, mode="legacy") as client:
        yield client


# mcp-connect-repair F1-a — connect and CALL are two different budgets.
# Before this split a single ``timeout`` became the whole httpx2 client timeout, so it bounded not
# just the handshake but every tool call's read: anything slower than 5s was severed mid-flight
# (116 dogfood: deepwiki `read_wiki_structure` 3.5s worked, `ask_question` 14.9s did not — and the
# useful remote MCP tools, search / doc-QA / cross-system queries, are nearly all over 5s).
REMOTE_MCP_CONNECT_TIMEOUT_SEC = 5.0
# Generous by design: a remote tool is allowed to think. This is a runaway guard, not a latency SLO
# (docs/sprints/mcp-connect-repair-2026-09-04/). Per-source configurability is a follow-up.
REMOTE_MCP_CALL_TIMEOUT_SEC = 120.0


@asynccontextmanager
async def connect_remote_mcp(
    base_url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = REMOTE_MCP_CONNECT_TIMEOUT_SEC,
    call_timeout: float | None = None,
    auth: Any = None,
    response_bytes_limit: int | None = None,
) -> AsyncIterator[ClientSession]:
    """Open an SSRF-guarded remote MCP ``ClientSession`` over Streamable HTTP (tools sprint WP2).

    This is the ``kind="mcp"`` counterpart of ``connect_team_mcp`` (which is in-process for OpenAPI):
    it connects to an EXTERNAL remote MCP server. Every outbound request is pinned to a freshly
    re-validated PUBLIC IP (``build_guarded_async_client`` → ``SsrfGuardedAsyncTransport``) and
    redirects are OFF — a 302-to-internal is the classic SSRF escape, and the SDK's default httpx2
    factory would otherwise follow redirects. ``headers`` carries the team's DECRYPTED auth
    (bearer / api_key), never logged.
    There is deliberately NO per-call loopback/private-network override: the outbound policy is
    decided solely by ``BOWHEAD_DEPLOYMENT_MODE`` (saas = public HTTPS only, local = intranet is
    the point of the deployment). See ``bowhead_router.ssrf_guard.validate_url``.
    ``auth`` is an optional ``httpx2.Auth`` (WP2b: the OAuth 2.1 provider that adds/refreshes the bearer)
    — used instead of static ``headers`` for an ``auth_type="oauth"`` source; it too rides the guarded
    transport (its token/refresh requests are SSRF-pinned). The yielded session is already initialized.

    TWO budgets (F1-a), because they answer different questions:
      - ``timeout`` bounds REACHING the server: the TCP/TLS connect plus the ``initialize`` handshake
        (``asyncio.wait_for`` below). A dead / unreachable server fails closed fast.
      - ``call_timeout`` (default ``REMOTE_MCP_CALL_TIMEOUT_SEC``) bounds a SINGLE request's read, so a
        legitimately slow tool is allowed to finish. Callers bound the call itself as well — see
        ``RemoteMcpToolClient.call_tool`` — this is the transport-level backstop.
    Collapsing the two (the pre-repair behaviour) makes the connect budget silently cap every tool.
    """
    from mcp.client.session import ClientSession as _ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from bowhead_core.ssrf_guard import build_guarded_async_client

    log_token = _MCP_TRANSPORT_LOGS_SUPPRESSED.set(True)
    try:
        client = build_guarded_async_client(
            follow_redirects=False,
            headers=_mcp_client_headers(
                headers,
                response_bytes_limit=response_bytes_limit,
            ),
            # read/write/pool ride the CALL budget; only `connect` keeps the short reach-the-server budget.
            timeout=httpx2.Timeout(
                call_timeout if call_timeout is not None else REMOTE_MCP_CALL_TIMEOUT_SEC,
                connect=timeout,
            ),
            auth=auth,
            event_hooks=(
                {
                    "response": [
                        functools.partial(
                            _limit_mcp_response_bytes,
                            limit=response_bytes_limit,
                        )
                    ]
                }
                if response_bytes_limit is not None
                else None
            ),
        )
        async with (
            client,
            streamable_http_client(base_url, http_client=client) as (
                read,
                write,
            ),
            _ClientSession(read, write) as session,
        ):
            await asyncio.wait_for(session.initialize(), timeout)
            yield session
    finally:
        _MCP_TRANSPORT_LOGS_SUPPRESSED.reset(log_token)


class RemoteMcpToolClient:
    """A ``call_tool``-only stand-in for a remote MCP ``ClientSession``: connects PER CALL, in the
    CALLER's own task (tools sprint WP2 lazy-connect, reshaped by mcp-connect-repair F1-b).

    ``build_team_specs`` / ``_make_team_handler`` only ever call ``.call_tool``, so this duck-types a
    session. The per-turn view is still built from the cached tool list (ZERO network at turn start)
    and a turn that never invokes this source still pays nothing — the lazy-connect TTFB property
    (spike-latency) is preserved. What changed is WHO OWNS the connection.

    F1-b — why not hold one session on the turn's exit stack (the previous shape): the MCP SDK's
    ``streamable_http_client`` / ``ClientSession`` are anyio task groups, and a cancel scope belongs
    to the task that entered it. The old code entered the session on the TURN's ``AsyncExitStack``
    (turn_runner's task) but first-connected and dispatched from a SUBAGENT's task, then unwound back
    in the turn's task — three tasks for one scope. Instrumented result: ``RuntimeError: Attempted to
    exit cancel scope in a different task than it was entered in``. Reproducing it needed BOTH the
    exit-stack hand-off AND the cross-task dispatch (either alone stayed clean) — and production had
    both. Connecting per call keeps enter / use / exit inside one task, which is what anyio requires.

    The cost is one handshake per call (~30ms locally) and NO Streamable-HTTP session reuse — a tool
    call cannot rely on state left by a previous call. That is the correct default for the tool-call
    shape we expose (each ``call_tool`` is independent), but it IS a semantic change: a server that
    requires session continuity across calls would need the held-session shape back, redesigned so a
    single task owns it end to end.

    ``auth`` (WP2b) is an optional ``httpx2.Auth`` — the OAuth 2.1 provider for an ``auth_type="oauth"``
    source; when set it drives the bearer (and self-refreshes) in place of static ``headers``, and a
    never-authorized source fails closed at connect (the SDK raises with no interactive handler → the
    handler maps it to a native error).
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        timeout: float = REMOTE_MCP_CONNECT_TIMEOUT_SEC,
        call_timeout: float = REMOTE_MCP_CALL_TIMEOUT_SEC,
        auth: Any = None,
    ) -> None:
        self._base_url = base_url
        self._headers = headers
        self._timeout = timeout
        self._call_timeout = call_timeout
        self._auth = auth

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async def _once() -> Any:
            async with connect_remote_mcp(
                self._base_url,
                self._headers,
                timeout=self._timeout,
                call_timeout=self._call_timeout,
                auth=self._auth,
            ) as session:
                return await session.call_tool(name, arguments)

        # Explicit outer bound (F1-a): the httpx2 read timeout is the transport backstop, but a server
        # that dribbles bytes forever would never trip it. ``connect`` + ``call`` is the worst case a
        # single dispatch may cost the turn; past that the handler surfaces a native timeout envelope.
        return await asyncio.wait_for(_once(), self._timeout + self._call_timeout)


def _mcp_tool_to_cache(tool: Any) -> dict[str, Any]:
    """Serialize one advertised MCP ``types.Tool`` into the ``tools_schema_json`` cache shape
    (name / description / input_schema / destructive). ``destructive`` mirrors the MCP
    ``destructiveHint`` annotation (default False — the FE still defaults write tools unchecked)."""
    ann = getattr(tool, "annotations", None)
    destructive = bool(getattr(ann, "destructive_hint", False)) if ann is not None else False
    cached = {
        "name": tool.name,
        "description": (getattr(tool, "description", "") or "")[: _MAX_TOOL_NAME * 8],
        "input_schema": getattr(tool, "input_schema", None) or {"type": "object"},
        "destructive": destructive,
    }
    if ann is not None:
        read_only = getattr(ann, "read_only_hint", None)
        destructive_hint = getattr(ann, "destructive_hint", None)
        annotations: dict[str, bool] = {}
        if isinstance(read_only, bool):
            annotations["readOnlyHint"] = read_only
        if isinstance(destructive_hint, bool):
            annotations["destructiveHint"] = destructive_hint
        if annotations:
            cached["annotations"] = annotations
    return cached


def _mcp_prompt_to_cache(prompt: Any) -> dict[str, Any]:
    """Serialize prompts/list metadata without retaining expansion content."""
    name = getattr(prompt, "name", None)
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ValueError("invalid MCP Prompt name")
    title = getattr(prompt, "title", None)
    description = getattr(prompt, "description", None)
    cached_arguments: list[dict[str, Any]] = []
    seen: set[str] = set()
    arguments = getattr(prompt, "arguments", None) or []
    if not isinstance(arguments, list) or len(arguments) > MAX_MCP_PROMPT_ARGUMENTS:
        raise ValueError("MCP Prompt argument count limit exceeded")
    for argument in arguments:
        argument_name = getattr(argument, "name", None)
        if not isinstance(argument_name, str) or not argument_name or len(argument_name) > 256 or argument_name in seen:
            raise ValueError("invalid MCP Prompt argument")
        seen.add(argument_name)
        argument_description = getattr(argument, "description", None)
        cached_arguments.append(
            {
                "name": argument_name,
                "description": (argument_description[:2048] if isinstance(argument_description, str) else ""),
                "required": getattr(argument, "required", None) is True,
            }
        )
    return {
        "name": name,
        "title": title[:256] if isinstance(title, str) else "",
        "description": description[:4096] if isinstance(description, str) else "",
        "arguments": cached_arguments,
    }


async def enumerate_remote_mcp_tools(
    base_url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 5.0,
    auth: Any = None,
) -> list[dict[str, Any]]:
    """Connect to a remote MCP server and return its advertised tools in the cache shape
    (tools sprint WP2 — used by preview / create / refresh to populate ``tools_schema_json``,
    and by validate for a reachability probe). SSRF-guarded; bounded by ``timeout``. ``auth``
    (WP2b) is an optional ``httpx2.Auth`` (the OAuth provider) used in place of static ``headers``
    for an ``auth_type="oauth"`` source — e.g. the callback re-enumerating with a fresh token."""
    async with connect_remote_mcp(base_url, headers, timeout=timeout, auth=auth) as session:
        res = await session.list_tools()
        return [_mcp_tool_to_cache(t) for t in res.tools]


async def enumerate_remote_mcp_prompts(
    base_url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 5.0,
    auth: Any = None,
) -> list[dict[str, Any]]:
    """Return bounded prompts/list metadata from an MCP server.

    Servers that do not advertise the Prompt capability return an empty list.
    Expansion content is deliberately not requested during discovery.
    """

    async def _enumerate() -> list[dict[str, Any]]:
        async with connect_remote_mcp(
            base_url,
            headers,
            timeout=timeout,
            call_timeout=timeout,
            auth=auth,
            response_bytes_limit=_MAX_PROMPT_RESPONSE_BYTES,
        ) as session:
            capabilities = session.server_capabilities
            if getattr(capabilities, "prompts", None) is None:
                return []
            cached: list[dict[str, Any]] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            seen_names: set[str] = set()
            encoded_bytes = 2  # surrounding JSON list brackets
            pages = 0
            while True:
                pages += 1
                if pages > _MAX_PROMPT_PAGES:
                    raise ValueError("MCP Prompt page limit exceeded")
                result = await session.list_prompts(params=mcp.types.PaginatedRequestParams(cursor=cursor))
                if len(result.prompts) > MAX_MCP_PROMPT_COUNT - len(cached):
                    raise ValueError("MCP Prompt count limit exceeded")
                for prompt in result.prompts:
                    item = _mcp_prompt_to_cache(prompt)
                    if item["name"] in seen_names:
                        raise ValueError("duplicate MCP Prompt name")
                    encoded_item = json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    encoded_bytes += len(encoded_item) + (1 if cached else 0)
                    if encoded_bytes > MAX_MCP_PROMPT_CACHE_BYTES:
                        raise ValueError("MCP Prompt cache limit exceeded")
                    seen_names.add(item["name"])
                    cached.append(item)
                next_cursor = getattr(result, "next_cursor", None)
                if next_cursor is None:
                    return cached
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors or len(next_cursor) > 2048:
                    raise ValueError("invalid MCP Prompt cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor

    # Discovery is metadata work on a request path, not a model Tool call. Bound
    # connect, all pages and teardown with one absolute deadline so a drip-fed or
    # endlessly paginated server cannot inherit the 120-second Tool budget per page.
    return await asyncio.wait_for(_enumerate(), timeout=timeout)


# ─────────────────────────────────────────────────────────────
# SSRF-guarded httpx2 executor + scrub/trim/classify (§5/§6.2/§8/§9) — slice ④c
# ─────────────────────────────────────────────────────────────
# _scrub now lives in the neutral bowhead_core.scrub module (④f) so tool_registry can
# share it without an import cycle; imported above as _scrub for this module's call sites.


def _trim(text: str, max_bytes: int) -> str:
    """Truncate an in-memory string to a byte budget (PRD §9)."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    return f"{raw[:max_bytes].decode('utf-8', 'ignore')}\n…[truncated, {len(raw)} bytes total]"


async def _read_capped(resp: httpx2.Response, max_bytes: int) -> str:
    """Stream an upstream body up to ``max_bytes`` then stop — never buffer a huge /
    slow-drip body fully into memory (cca review #6/#8). The RETAINED buffer is capped
    per chunk: a boundary chunk is sliced to the remaining budget before it is kept, so
    a body that stays under the cap then sends one large chunk (chunked / no
    content-length) can't be materialised + re-joined past ``max_bytes`` (re-review #9).
    The single transient yielded chunk is bounded by httpx2's read size (a decompression
    bomb is bounded separately by the content-length precheck + the executor semaphore).
    Truncation is marked."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        remaining = max_bytes - total
        if len(chunk) >= remaining:
            chunks.append(chunk[:remaining])  # keep only up to the budget, never the whole chunk
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    text = b"".join(chunks).decode("utf-8", "replace")
    return f"{text}\n…[truncated, >{max_bytes} bytes]" if truncated else text


def _swap_host(url: str, ip: str) -> str:
    """Rewrite the URL host to a validated IP, preserving scheme/port/path/query
    (IPv6 bracketed) so we connect to the pinned IP without a re-resolve (no TOCTOU).
    The caller restores Host header + TLS SNI to the original host."""
    parts = urlsplit(url)
    host_ip = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host_ip}:{parts.port}" if parts.port else host_ip
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def _build_call(
    op: OpenApiOp,
    arguments: dict[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, str], Any]:
    """Map validated LLM arguments → (method, path, query, headers, json_body) using
    each input's recorded wire location (§2.1/§8). Path params are URL-encoded into
    the template; body props are re-collected into the JSON body."""
    path = op.path
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    for p in op.params:
        if p.name not in arguments:
            continue
        v = arguments[p.name]
        if p.location == "path":
            path = path.replace("{" + p.name + "}", quote(str(v), safe=""))
        elif p.location == "query":
            query[p.name] = v
        elif p.location == "header":
            headers[p.name] = str(v)
    body: Any = None
    if op.body_schema is not None:
        is_object_body = op.body_schema.get("type") == "object" and isinstance(op.body_schema.get("properties"), dict)
        body = {k: arguments[k] for k in op.body_prop_names if k in arguments} if is_object_body else arguments.get("body")
    return op.method.upper(), path, query, headers, body


async def http_call(
    base_url: str,
    op: OpenApiOp,
    arguments: dict[str, Any],
    auth_headers: dict[str, str],
    *,
    dns_resolver: Resolver | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """Execute one team tool call as an SSRF-guarded outbound HTTP request → Bowhead
    native ``{ok, error_kind?, error?/data?}`` (the ④e view handler passes it
    through; never raises for expected failures so creds can't leak via a raw
    exception, §6.2/§8).

    Security: resolve+validate EVERY call (re-resolve → TOCTOU) then connect to the
    pinned IP with Host header + TLS SNI restored to the original host (DNS-rebind
    safe, §5 F5). No raise_for_status; upstream status → fixed template (never echoes
    URL / body / headers); redirects NOT followed (redirect-to-internal SSRF). All
    error text scrubbed of credential shapes.
    """
    method, path, query, param_headers, body = _build_call(op, arguments)
    full_url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    try:
        vt = resolve_and_validate(full_url, resolver=dns_resolver)
    except SsrfBlocked as e:
        return {"ok": False, "error_kind": "blocked", "error": _scrub(str(e))}

    ip_url = _swap_host(full_url, vt.ip)
    host_header = vt.host if vt.port in (80, 443) else f"{vt.host}:{vt.port}"
    req_headers: dict[str, str] = {**param_headers, **auth_headers, "Host": host_header}
    # pin TLS SNI + cert verification to the original host (we connect to the IP).
    extensions: dict[str, Any] = {"sni_hostname": vt.host} if vt.scheme == "https" else {}
    _to = httpx2.Timeout(timeout, connect=min(timeout, 10.0))
    try:
        async with (
            httpx2.AsyncClient(
                transport=transport,
                timeout=_to,
                follow_redirects=False,
            ) as client,
            client.stream(
                method,
                ip_url,
                params=query or None,
                headers=req_headers,
                json=body if body is not None else None,
                extensions=extensions,
            ) as resp,
        ):
            status = resp.status_code
            # classify error statuses WITHOUT reading the body: no echo of an
            # error body + no huge / slow-drip body read on an error response.
            if 300 <= status < 400:
                return {"ok": False, "error_kind": "redirect", "error": f"upstream returned redirect {status}; not followed"}
            if status >= 500:
                return {"ok": False, "error_kind": "upstream_5xx", "error": f"upstream returned {status}"}
            if status in (401, 403):
                # credential-readiness R7: credential 4xx split from the generic bucket so
                # ④e can attach who-can-fix guidance (team source vs per-user MCP differ,
                # PRD 6.0.3 ②). Same fixed template — still no body echo.
                return {
                    "ok": False,
                    "error_kind": "upstream_auth",
                    "error": f"upstream returned {status}: credential missing, invalid, or expired",
                }
            if status >= 400:
                return {"ok": False, "error_kind": "upstream_4xx", "error": f"upstream returned {status}"}
            # reject a known-oversized success body before reading a byte.
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                return {"ok": False, "error_kind": "too_large", "error": "upstream response exceeds size limit"}
            # stream ≤ max_bytes (early-abort) under an overall read deadline —
            # defeats unbounded / slow-drip bodies (cca review #6/#8).
            raw = await asyncio.wait_for(_read_capped(resp, max_bytes), timeout=timeout)
    except (TimeoutError, httpx2.TimeoutException):
        return {"ok": False, "error_kind": "timeout", "error": "upstream request timed out"}
    except httpx2.HTTPError as e:  # connect / protocol / TLS errors
        return {"ok": False, "error_kind": "connection", "error": _scrub(type(e).__name__)}

    # scrub the SUCCESS body too — an upstream can reflect our injected auth header
    # or return creds in its own body (cca review #2); never hand raw creds to the LLM.
    return {"ok": True, "status": status, "data": _scrub(raw)}


async def ssrf_get_text(
    url: str,
    *,
    dns_resolver: Resolver | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: float = 15.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> str:
    """SSRF-guarded GET of a USER-SUPPLIED URL (create-time spec fetch — the FIRST SSRF
    point, PRD §6.1). Same protections as the execute-time http_call: resolve+validate →
    connect to the pinned IP (Host + TLS SNI restored to the original host, DNS-rebind
    safe) → NO redirects → size-capped stream. Raises ``SsrfBlocked`` on policy reject,
    ``httpx2.HTTPError`` on a transport/status failure, ``ValueError`` on oversize."""
    vt = resolve_and_validate(url, resolver=dns_resolver)
    ip_url = _swap_host(url, vt.ip)
    host_header = vt.host if vt.port in (80, 443) else f"{vt.host}:{vt.port}"
    extensions: dict[str, Any] = {"sni_hostname": vt.host} if vt.scheme == "https" else {}
    _to = httpx2.Timeout(timeout, connect=min(timeout, 10.0))
    async with (
        httpx2.AsyncClient(
            transport=transport,
            timeout=_to,
            follow_redirects=False,
        ) as client,
        client.stream(
            "GET",
            ip_url,
            headers={"Host": host_header},
            extensions=extensions,
        ) as resp,
    ):
        if resp.status_code >= 400:
            raise httpx2.HTTPStatusError(
                f"spec fetch returned {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        cl = resp.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            raise ValueError("spec exceeds size limit")
        return await asyncio.wait_for(_read_capped(resp, max_bytes), timeout=timeout)


# (team_id, source_id) -> (base_url, auth_headers). ④d's ToolAuthRegistry resolves
# the per-team Fernet-decrypted headers. team_id is passed EXPLICITLY (not derived from
# the call ctx) so an executor built for one team can never resolve another team's
# base_url / creds even if its source_id collides — closes cca review #3 (the resolver
# is no longer keyed on source_id alone) and backstops the handler team-bind (#4).
SourceConfigResolver = Callable[[str, str], Awaitable[tuple[str, dict[str, str]]]]


def make_http_executor(
    team_id: str,
    resolver: SourceConfigResolver,
    *,
    dns_resolver: Resolver | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
    max_bytes: int = 64 * 1024,
    max_concurrency: int = 8,
) -> OpExecutor:
    """Build an OpExecutor (for build_team_mcp_server) bound to ``team_id`` at the LOAD
    POINT. Resolves each source's (base_url, auth_headers) via ``resolver(team_id,
    source_id)`` then performs the SSRF-guarded http_call, capped at
    ``max_concurrency`` in-flight outbound calls (FD / socket-exhaustion guard —
    cca review #7; full per-team rate-limit/credit is §9, deferred this slice)."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _exec(source_id: str, op: OpenApiOp, arguments: dict[str, Any]) -> Any:
        base_url, auth_headers = await resolver(team_id, source_id)
        async with sem:
            return await http_call(
                base_url,
                op,
                arguments,
                auth_headers,
                dns_resolver=dns_resolver,
                transport=transport,
                timeout=timeout,
                max_bytes=max_bytes,
            )

    return _exec
