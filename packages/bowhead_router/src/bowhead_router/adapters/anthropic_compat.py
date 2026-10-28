"""Anthropic Messages API 兼容 adapter.

覆盖：
- Anthropic 官方（api.anthropic.com / v1/messages）
- Volces Ark 字节豆包 coding 镜像（ark.cn-beijing.volces.com/api/coding —— 即
  环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN + ANTHROPIC_MODEL 模式）
- 其他 Anthropic Messages 协议代理（AWS Bedrock Claude 除外 —— 它有自己的 sigv4）

协议要点：
- POST {base_url}/v1/messages
- Headers:
    - `x-api-key: <key>`  (Anthropic 原生)
    - `Authorization: Bearer <key>`  (Volces / 其他代理网关；我们两个都带，谁认谁用)
    - `anthropic-version: 2023-06-01`
    - `content-type: application/json`
- Body 关键差异（vs OpenAI）:
    - `system` 是 top-level 字段，不是 messages 里的一条
    - `messages[]` 只含 user/assistant；content 可以是 string 或 block array
    - tool_use: content block `{type: "tool_use", id, name, input}`
    - tool_result: 作为 user message 的 content block
      `{type: "tool_result", tool_use_id, content}`
    - `max_tokens` **必填** —— Anthropic 严格
    - `tools[]` 形式：`{name, description, input_schema}`（不用 function 包裹）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import httpx2

from bowhead_router.args_parse_hint import args_parse_hint
from bowhead_router.errors import (
    InvocationOutcomeUnknownError,
    ProviderCallError,
    ProviderFailureKind,
    failure_kind_for_error_payload,
    failure_kind_for_status,
    stream_failure_kind_allows_fallback,
    target_failure,
    upstream_error_fields,
    upstream_error_fields_from_payload,
)
from bowhead_router.input_admission import admit_outbound_input
from bowhead_router.invocation import (
    abandon_physical_invocation,
    begin_physical_invocation,
    canonical_transport_digest,
    complete_physical_invocation,
    confirm_physical_not_applied,
    confirm_provider_auth_rejection,
    confirm_provider_rate_limit_rejection,
    exact_authoritative_usage,
    fail_physical_invocation,
    settle_transport_failure,
)
from bowhead_router.outbound import (
    OutboundParams,
    apply_learned_constraints,
    apply_outbound_params,
    learn_constraint,
    rejected_param,
    resolve_outbound_params,
)
from bowhead_router.ssrf_guard import build_guarded_async_client
from bowhead_router.tool_args import loads_tool_arguments
from bowhead_router.tool_names import ToolNameMap, to_wire_tool_name
from bowhead_router.transport import (
    CanonicalProviderTransportPlan,
    build_provider_transport_plan,
    materialize_transport_headers,
)
from bowhead_router.types import (
    FinishReason,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from types import TracebackType

logger = logging.getLogger(__name__)


class AnthropicCompatError(ProviderCallError):
    """Anthropic-compatible provider failure."""


_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_use",
}


@dataclass
class AnthropicCompatibleAdapter:
    """Anthropic Messages API adapter."""

    supports_input_admission: ClassVar[bool] = True

    base_url: str
    api_key: str
    provider_name: str = "anthropic_compat"
    timeout_sec: int = 60
    default_headers: dict[str, str] = field(default_factory=dict)
    anthropic_version: str = "2023-06-01"
    # Proxy mode: append /v1 if base_url lacks it; otherwise leave it unchanged.
    # Defaults to /v1/messages; some proxies may use /messages.
    messages_path: str = "/v1/messages"
    # Anthropic and DeepSeek require tool names to match ^[a-zA-Z0-9_-]{1,64}$. Rewrite
    # non-compliant names on the wire and map them back on inbound tool_use blocks; see
    # bowhead_router.tool_names. Compliant names are never changed.
    sanitize_tool_dots: bool = True
    ssrf_guard: bool = False
    # Team provider definition (`models`, etc.). The registry previously supplied provider_options only to the OpenAI family,
    # leaving this adapter unable to see even model declarations; that is why the Anthropic surface had **no escape hatch at all**.
    provider_options: dict[str, Any] = field(default_factory=dict)
    client_builder: Callable[..., httpx2.AsyncClient] | None = field(
        default=None,
        repr=False,
    )
    _http: httpx2.AsyncClient = field(init=False, repr=False)
    _transport_plan: CanonicalProviderTransportPlan = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._transport_plan = build_provider_transport_plan(
            {
                "type": "anthropic_compatible",
                "base_url": self.base_url,
                "api_key": self.api_key,
                "default_headers": self.default_headers,
                "timeout_sec": self.timeout_sec,
                "extra": {
                    "anthropic_version": self.anthropic_version,
                    "messages_path": self.messages_path,
                },
            },
            purpose="runtime_chat",
        )
        kwargs = {
            "base_url": self.base_url,
            "timeout": self.timeout_sec,
            "headers": materialize_transport_headers(self._transport_plan),
        }
        if self.ssrf_guard:
            builder = self.client_builder or build_guarded_async_client
            self._http = builder(**kwargs)
        else:
            self._http = httpx2.AsyncClient(**kwargs)

    async def __aenter__(self) -> AnthropicCompatibleAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────
    async def invoke(self, req: LLMRequest) -> LLMResponse:
        body = self._build_body(req, stream=False)
        attempt = await begin_physical_invocation(
            provider=self.provider_name,
            model=req.model,
            adapter_contract_key=f"anthropic_compatible/{self._transport_plan.adapter_contract_version}",
            mode="invoke",
            request=body,
            input_request=req,
            input_family="anthropic",
        )
        try:
            resp = await self._http.post(self._transport_plan.request_url, json=body)
        except asyncio.CancelledError:
            await abandon_physical_invocation(
                attempt,
                reason_code="transport_cancelled",
            )
            raise
        except httpx2.TimeoutException as e:
            await settle_transport_failure(attempt, e, output_started=False)
            raise AnthropicCompatError(
                kind=ProviderFailureKind.TIMEOUT,
                retryable=True,
                provider_family="anthropic_compatible",
            ) from e
        except httpx2.HTTPError as e:
            await settle_transport_failure(attempt, e, output_started=False)
            raise AnthropicCompatError(
                kind=ProviderFailureKind.NETWORK,
                retryable=True,
                provider_family="anthropic_compatible",
            ) from e
        except Exception:
            await fail_physical_invocation(
                attempt,
                reason_code="transport_unclassified_error",
            )
            raise

        # φ debug: on 400 + orphan-tool_result error pattern, dump messages[0]
        # so we can trace what's putting tool-role at the head of the list.
        if resp.status_code == 400:
            try:
                body_txt = resp.text or ""
                if "tool_use_id" in body_txt and "tool_result" in body_txt:
                    import json as _phi_dbg_json

                    msgs = body.get("messages", [])
                    msg_count = len(msgs)
                    # also dump roles + brief content type info
                    role_seq = [
                        {
                            "role": m.get("role"),
                            "content_type": (
                                "list"
                                if isinstance(m.get("content"), list)
                                else "str"
                                if isinstance(m.get("content"), str)
                                else type(m.get("content")).__name__
                            ),
                            "content_block_types": (
                                [b.get("type") for b in m.get("content", []) if isinstance(b, dict)] if isinstance(m.get("content"), list) else None
                            ),
                        }
                        for m in msgs[:6]
                    ]
                    logger.error(
                        "phi.adapter_400_orphan_dump msg_count=%d role_seq=%s",
                        msg_count,
                        _phi_dbg_json.dumps(role_seq, ensure_ascii=False)[:1500],
                    )
            except Exception as _phi_dbg_e:
                logger.warning("phi.adapter_400_dump_failed: %s", _phi_dbg_e)

        # Adaptive outbound parameters (same criteria as OpenAI): drop the parameter identified by the provider and retry once.
        # The Anthropic surface previously **had no such path**; a gateway rejecting one parameter made the entire chain unusable.
        rejected_outbound = (
            self._param_rejection_retry(req=req, body=body, error_body=(resp.text or "")[:500], durable=attempt is not None)
            if resp.status_code == 400
            else None
        )
        if rejected_outbound is not None:
            await confirm_physical_not_applied(
                attempt,
                evidence_code=f"outbound_param_rejected/{rejected_outbound}/v1",
                evidence={"status": 400, "param": rejected_outbound},
            )
            if learn_constraint(adapter_type="anthropic_compatible", base_url=self.base_url, model=req.model, param=rejected_outbound):
                logger.warning(
                    "outbound.param_rejected provider=%s model=%s param=%s — dropping it for this model and retrying once.",
                    self.provider_name,
                    req.model,
                    rejected_outbound,
                )
            body = self._build_body(req, stream=False)
            attempt = await begin_physical_invocation(
                provider=self.provider_name,
                model=req.model,
                adapter_contract_key=f"anthropic_compatible/{self._transport_plan.adapter_contract_version}",
                mode="invoke",
                request=body,
                input_request=req,
                input_family="anthropic",
            )
            try:
                resp = await self._http.post(self._transport_plan.request_url, json=body)
            except asyncio.CancelledError:
                await abandon_physical_invocation(attempt, reason_code="transport_cancelled")
                raise
            except httpx2.TimeoutException as e:
                await settle_transport_failure(attempt, e, output_started=False)
                raise AnthropicCompatError(
                    kind=ProviderFailureKind.TIMEOUT,
                    retryable=True,
                    provider_family="anthropic_compatible",
                ) from e
            except httpx2.HTTPError as e:
                await settle_transport_failure(attempt, e, output_started=False)
                raise AnthropicCompatError(
                    kind=ProviderFailureKind.NETWORK,
                    retryable=True,
                    provider_family="anthropic_compatible",
                ) from e
            except Exception:
                await fail_physical_invocation(attempt, reason_code="transport_unclassified_error")
                raise

        if resp.status_code in (401, 403):
            await confirm_provider_auth_rejection(attempt, status_code=resp.status_code)
            attempt = None
        elif resp.status_code == 429:
            await confirm_provider_rate_limit_rejection(attempt, status_code=resp.status_code)
            attempt = None
        try:
            self._raise_for_status(resp)
            try:
                data = resp.json()
            except ValueError as exc:
                raise AnthropicCompatError(
                    kind=ProviderFailureKind.RESPONSE_INVALID,
                    retryable=False,
                    status_code=resp.status_code,
                    provider_family="anthropic_compatible",
                ) from exc
            if not isinstance(data, dict):
                raise AnthropicCompatError(
                    kind=ProviderFailureKind.RESPONSE_INVALID,
                    retryable=False,
                    status_code=resp.status_code,
                    provider_family="anthropic_compatible",
                )
            response = self._parse_response(data, req)
        except InvocationOutcomeUnknownError:
            raise
        except ProviderCallError as exc:
            exc.api_surface = "messages"
            await fail_physical_invocation(
                attempt,
                reason_code="provider_response_unclassified",
                failure=target_failure(
                    exc,
                    adapter=self,
                    provider=self.provider_name,
                    model=req.model,
                    mode="invoke",
                    target_index=1,
                    attempt=1,
                    first_visible_chunk_emitted=False,
                ),
            )
            raise
        except Exception:
            await fail_physical_invocation(attempt, reason_code="provider_response_unclassified")
            raise
        usage_raw = data.get("usage")
        authoritative_usage = exact_authoritative_usage(
            usage_raw if isinstance(usage_raw, dict) else None,
            input_key="input_tokens",
            output_key="output_tokens",
            cached_value=(usage_raw.get("cache_read_input_tokens", 0) if isinstance(usage_raw, dict) else 0),
        )
        await complete_physical_invocation(
            attempt,
            usage=authoritative_usage,
            response=data,
        )
        return response

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        """P4.B/P4.D + spike-2: anthropic event stream w/ tool_call accumulation.

        Events:
          message_start / content_block_start / content_block_delta / content_block_stop / message_delta / message_stop
        Delta types:
          - text_delta: content
          - thinking_delta: thinking content (extended_thinking enabled)
          - input_json_delta: tool_use args (per-block partial_json accumulation)

        Tool-use accumulation: content_block_start with type=tool_use opens a
        per-index buffer; input_json_delta chunks append to that buffer; on
        content_block_stop the JSON is parsed and emitted as a
        ``LLMChunk(tool_call_delta=ToolCall(...))``. Malformed JSON yields a
        ToolCall with empty args + warn log (callers fall back to invoke per
        Sprint κ §4.1.3).
        """
        import json as _json

        body = self._build_body(req, stream=True)
        attempt = await begin_physical_invocation(
            provider=self.provider_name,
            model=req.model,
            adapter_contract_key=f"anthropic_compatible/{self._transport_plan.adapter_contract_version}",
            mode="stream",
            request=body,
            input_request=req,
            input_family="anthropic",
        )
        attempt_settled = False
        # Any yielded output makes a later transport failure unknown, never retried.
        output_started = False
        # Per-content-block accumulators (keyed by index).
        # Each entry: {"id": str, "name": str, "json_buf": str}
        tool_blocks: dict[int, dict[str, Any]] = {}
        tool_names = self._tool_name_map(req)
        try:
            async with self._http.stream(
                "POST",
                self._transport_plan.request_url,
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    if resp.status_code in (401, 403):
                        await confirm_provider_auth_rejection(attempt, status_code=resp.status_code)
                        attempt_settled = True
                        attempt = None
                    elif resp.status_code == 429:
                        await confirm_provider_rate_limit_rejection(attempt, status_code=resp.status_code)
                        attempt_settled = True
                        attempt = None
                    # 0.13.2 fix: 429 is transient (rate limit / brief quota
                    # window), should be retryable. Was 5xx-only before.
                    raise AnthropicCompatError(
                        kind=failure_kind_for_status(resp.status_code),
                        retryable=resp.status_code in (408, 429, 500, 502, 503, 504),
                        status_code=resp.status_code,
                        provider_family="anthropic_compatible",
                        **upstream_error_fields(resp),
                    )
                finish_reason: FinishReason | None = None
                usage: TokenUsage | None = None
                usage_input_seen = False
                usage_output_seen = False
                authoritative_input: object = None
                authoritative_output: object = None
                authoritative_cached: object = 0
                current_event: str | None = None
                terminal_frame_seen = False
                stream_evidence = hashlib.sha256() if attempt is not None else None
                async for line in resp.aiter_lines():
                    line = line.rstrip("\r")
                    if not line:
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        obj = _json.loads(payload)
                    except _json.JSONDecodeError:
                        if attempt is not None:
                            raise AnthropicCompatError(
                                kind=ProviderFailureKind.RESPONSE_INVALID,
                                retryable=False,
                                status_code=resp.status_code,
                                provider_family="anthropic_compatible",
                            ) from None
                        continue
                    if not isinstance(obj, dict):
                        if attempt is not None:
                            raise AnthropicCompatError(
                                kind=ProviderFailureKind.RESPONSE_INVALID,
                                retryable=False,
                                status_code=resp.status_code,
                                provider_family="anthropic_compatible",
                            )
                        continue
                    et = current_event or obj.get("type") or ""
                    if stream_evidence is not None:
                        stream_evidence.update(
                            bytes.fromhex(
                                canonical_transport_digest(
                                    {"event": et, "payload": obj},
                                )
                            ),
                        )
                    if et == "error":
                        kind = failure_kind_for_error_payload(obj)
                        raise AnthropicCompatError(
                            kind=kind,
                            retryable=stream_failure_kind_allows_fallback(kind),
                            provider_family="anthropic_compatible",
                            **upstream_error_fields_from_payload(
                                obj,
                                headers=resp.headers,
                            ),
                        )
                    if et == "content_block_start":
                        idx = int(obj.get("index", 0))
                        block = obj.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            tool_blocks[idx] = {
                                "id": str(block.get("id") or f"toolu_{idx}"),
                                "name": str(block.get("name") or ""),
                                "json_buf": "",
                            }
                    elif et == "content_block_delta":
                        d = obj.get("delta") or {}
                        dt = d.get("type")
                        idx = int(obj.get("index", 0))
                        if dt == "text_delta":
                            text = d.get("text") or ""
                            if text:
                                output_started = True
                                yield LLMChunk(delta=text)
                        elif dt == "thinking_delta":
                            think = d.get("thinking") or ""
                            if think:
                                output_started = True
                                yield LLMChunk(thinking_delta=think)
                        elif dt == "input_json_delta" and idx in tool_blocks:
                            partial = d.get("partial_json") or ""
                            if partial:
                                tool_blocks[idx]["json_buf"] += partial
                                # Sprint chat-redesign-2026-05-21 Phase 1.1 (Spike 1):
                                # yield incremental partial so subagent can drive
                                # `lead_thinking_writing` step UI before block closes.
                                output_started = True
                                yield LLMChunk(
                                    tool_call_partial={
                                        "tool_name": tool_blocks[idx]["name"],
                                        "tool_call_id": tool_blocks[idx]["id"],
                                        "partial_args_json": partial,
                                    }
                                )
                    elif et == "content_block_stop":
                        idx = int(obj.get("index", 0))
                        if idx in tool_blocks:
                            blk = tool_blocks.pop(idx)
                            try:
                                args = loads_tool_arguments(blk["json_buf"]) if blk["json_buf"] else {}
                            except _json.JSONDecodeError as _je:
                                # 116 production 1.0.89 dogfood: this log **could not reach the truncation point**. Its preview had only
                                # 200 characters while real payloads had thousands, so production could not establish whether the cause was
                                # max_output_tokens or a broken stream (an investigation of actual truncation found both the log
                                # and event previews truncated, preventing further diagnosis).
                                # Include buffer length and tail: a length near the limit indicates exhaustion; the tail shows which field was truncated.
                                logger.warning(
                                    "anthropic_compat.stream.tool_args_invalid_json id=%s name=%s buf_len=%d err=%s pos=%s around=%r head=%s tail=%s",
                                    blk["id"],
                                    blk["name"],
                                    len(blk["json_buf"]),
                                    _je.msg,
                                    _je.pos,
                                    blk["json_buf"][max(0, _je.pos - 60) : _je.pos + 60],
                                    blk["json_buf"][:200],
                                    blk["json_buf"][-120:],
                                )
                                # follow-up bugs 0710 ② fix: as in openai_compat, never silently swallow truncated/invalid streaming arguments
                                # as {}; use the existing _args_parse_error channel so the LLM can repair them accurately.
                                # (At content_block_stop, stop_reason has not arrived yet, so omit finish_reason.)
                                args = {
                                    "_args_parse_error": args_parse_hint(_je, blk["json_buf"]),
                                    "_raw_args_preview": blk["json_buf"][:800],
                                }
                            output_started = True
                            yield LLMChunk(
                                tool_call_delta=ToolCall(
                                    id=blk["id"],
                                    name=tool_names.to_canonical(blk["name"]),
                                    arguments=args,
                                ),
                            )
                    elif et == "message_start":
                        # perf: Anthropic streams put input_tokens and cache_read/creation in
                        # message_start.message.usage; message_delta updates only output_tokens.
                        # Reading only message_delta previously lost all streamed input/cached usage (peers with tool messages
                        # use streaming as the main path). Capture initial usage here, then merge output at message_delta.
                        _su = (obj.get("message") or {}).get("usage") or {}
                        if _su:
                            usage_input_seen = "input_tokens" in _su
                            if "input_tokens" in _su:
                                authoritative_input = _su.get("input_tokens")
                            if "cache_read_input_tokens" in _su:
                                authoritative_cached = _su.get(
                                    "cache_read_input_tokens",
                                )
                            usage = TokenUsage(
                                input_tokens=int(_su.get("input_tokens") or 0),
                                output_tokens=int(_su.get("output_tokens") or 0),
                                cached_tokens=int(_su.get("cache_read_input_tokens") or 0),
                            )
                    elif et == "message_delta":
                        d = obj.get("delta") or {}
                        sr = d.get("stop_reason")
                        if sr:
                            finish_reason = self._map_finish_reason(sr) if hasattr(self, "_map_finish_reason") else sr
                        u = obj.get("usage")
                        if u:
                            usage_input_seen = usage_input_seen or "input_tokens" in u
                            usage_output_seen = "output_tokens" in u
                            if "input_tokens" in u:
                                authoritative_input = u.get("input_tokens")
                            if "output_tokens" in u:
                                authoritative_output = u.get("output_tokens")
                            if "cache_read_input_tokens" in u:
                                authoritative_cached = u.get(
                                    "cache_read_input_tokens",
                                )
                            # message_delta carries cumulative output_tokens; retain input/cached values from message_start.
                            _prev = usage or TokenUsage()
                            usage = TokenUsage(
                                input_tokens=int(u.get("input_tokens") or _prev.input_tokens or 0),
                                output_tokens=int(u.get("output_tokens") or _prev.output_tokens or 0),
                                cached_tokens=int(u.get("cache_read_input_tokens") or _prev.cached_tokens or 0),
                            )
                    elif et == "message_stop":
                        terminal_frame_seen = True
                        break
                if not terminal_frame_seen:
                    raise AnthropicCompatError(
                        kind=ProviderFailureKind.RESPONSE_INVALID,
                        retryable=True,
                        status_code=resp.status_code,
                        provider_family="anthropic_compatible",
                    )
                await complete_physical_invocation(
                    attempt,
                    usage=(
                        exact_authoritative_usage(
                            {
                                "input_tokens": authoritative_input,
                                "output_tokens": authoritative_output,
                            },
                            input_key="input_tokens",
                            output_key="output_tokens",
                            cached_value=authoritative_cached,
                        )
                        if attempt is not None
                        else usage
                        if usage_input_seen and usage_output_seen
                        else None
                    ),
                    response={
                        "finish_reason": finish_reason,
                        "stream_evidence_digest": (stream_evidence.hexdigest() if stream_evidence is not None else None),
                        "usage": (
                            {
                                "cached_tokens": usage.cached_tokens,
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                            }
                            if usage is not None
                            else None
                        ),
                    },
                )
                attempt_settled = True
                output_started = True
                yield LLMChunk(finish_reason=finish_reason, usage=usage)
        except GeneratorExit:
            if not attempt_settled:
                await abandon_physical_invocation(
                    attempt,
                    reason_code="stream_consumer_closed",
                )
            raise
        except asyncio.CancelledError:
            if not attempt_settled:
                await abandon_physical_invocation(
                    attempt,
                    reason_code="transport_cancelled",
                )
            raise
        except httpx2.TimeoutException as e:
            await settle_transport_failure(attempt, e, output_started=output_started)
            raise AnthropicCompatError(
                kind=ProviderFailureKind.TIMEOUT,
                retryable=True,
                provider_family="anthropic_compatible",
            ) from e
        except httpx2.HTTPError as e:
            await settle_transport_failure(attempt, e, output_started=output_started)
            raise AnthropicCompatError(
                kind=ProviderFailureKind.NETWORK,
                retryable=True,
                provider_family="anthropic_compatible",
            ) from e
        except InvocationOutcomeUnknownError:
            raise
        except ProviderCallError as exc:
            exc.api_surface = "messages"
            await fail_physical_invocation(
                attempt,
                reason_code="provider_response_unclassified",
                failure=target_failure(
                    exc,
                    adapter=self,
                    provider=self.provider_name,
                    model=req.model,
                    mode="stream",
                    target_index=1,
                    attempt=1,
                    first_visible_chunk_emitted=output_started,
                ),
            )
            raise
        except Exception:
            await fail_physical_invocation(attempt, reason_code="provider_response_unclassified")
            raise

    # ─────────────────────────────────────────────────────────
    # Request construction
    # ─────────────────────────────────────────────────────────
    def _build_body(self, req: LLMRequest, *, stream: bool) -> dict[str, Any]:
        from bowhead_router.output_budget import finalize_output_allowance, initial_output_allowance
        from bowhead_router.runtime_notes import model_messages

        body: dict[str, Any] = {
            "model": req.model,
            "messages": self._convert_messages(model_messages(req.messages), sanitize=self.sanitize_tool_dots),
            "max_tokens": initial_output_allowance(req, self.provider_options),
        }
        if req.system:
            body["system"] = req.system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None and req.top_p != 1.0:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop_sequences"] = req.stop if isinstance(req.stop, list) else [req.stop]
        if stream:
            body["stream"] = True
        if req.tools:
            body["tools"] = [self._convert_tool(t) for t in req.tools]
            # Let the model decide whether to call tools by default, equivalent to OpenAI tool_choice=auto.
            body["tool_choice"] = {"type": req.tool_choice}
            # Sprint χ-1.1 (CTO P0): defensively remove parallel_tool_calls (the Anthropic path does not support
            # this OpenAI-only parameter; explicitly pop it to prevent regressions if future chi flag passthrough reaches this path).
            # The Anthropic equivalent is disable_parallel_tool_use=true (opt-out).
            body.pop("parallel_tool_calls", None)
        # Set thinking explicitly: the official Anthropic default is disabled, but Volces Ark
        # `/api/coding` defaults Doubao-Seed-2.0-pro to enabled, requiring explicit disabled to suppress
        # thinking blocks. Omitting thinking gives different behavior on the two providers, so always set it explicitly.
        if req.extended_thinking:
            thinking_cfg: dict[str, Any] = {"type": "enabled"}
            if req.thinking_budget_tokens:
                thinking_cfg["budget_tokens"] = req.thinking_budget_tokens
            body["thinking"] = thinking_cfg
        else:
            body["thinking"] = {"type": "disabled"}
        # Outbound parameters: use the same semantics as the OpenAI surface, mapped to each protocol's wire fields
        # (`parallel_tool_calls` here is the **inverse** of `tool_choice.disable_parallel_tool_use`).
        from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
        from bowhead_router.runtime_parameters import apply_runtime_parameters

        capability = OpenAICompatibleAdapter._request_capability(req)
        params = self._outbound_params(req.model, capability)
        apply_outbound_params(body, resolve_outbound_params(self.provider_options, model_id=req.model, family="anthropic"), family="anthropic")
        apply_runtime_parameters(body, self.provider_options, model_id=req.model, capability=capability, family="anthropic")
        apply_learned_constraints(
            body,
            adapter_type="anthropic_compatible",
            base_url=self.base_url,
            model=req.model,
            params=params,
            family="anthropic",
        )
        finalize_output_allowance(req, body, self.provider_options, capability=capability, family="anthropic")
        admit_outbound_input(req, body, family="anthropic")
        return body

    def _outbound_params(self, model_id: str, capability: str = "chat") -> OutboundParams:
        from bowhead_router.runtime_parameters import effective_outbound_params

        return effective_outbound_params(
            self.provider_options,
            model_id=model_id,
            capability=capability,
            family="anthropic",
        )

    def _param_rejection_retry(
        self,
        *,
        req: LLMRequest,
        body: dict[str, Any],
        error_body: str,
        durable: bool = False,
    ) -> str | None:
        """400 是不是在指认某个可以安全调整的出站参数？返回参数名（判据与 openai 面同源）。

        `max_tokens` 在 Anthropic 是必填字段，删掉它请求根本不合法 —— 所以这里不放行它，
        由管理员显式声明 `cap`/`fixed` 处理。
        """

        # As on OpenAI, durable accounting requires structured identification (ambiguous prose risks duplicate charges on resend)
        # and a **registered** capture proving "definitely not applied" (the registry is deliberately empty; production fails closed).
        param = rejected_param(error_body, allow_prose=not durable)
        if param is None or param == "max_tokens":
            return None
        from bowhead_router.adapters.openai_compat import _durable_rejection_is_registered

        if durable and not _durable_rejection_is_registered(
            error_body,
            adapter_type="anthropic_compatible",
            adapter_contract_version=self._transport_plan.adapter_contract_version,
            provider_host=(urlsplit(self._transport_plan.request_url).hostname or "").lower(),
        ):
            return None
        if param == "parallel_tool_calls":
            choice = body.get("tool_choice")
            if not isinstance(choice, dict) or "disable_parallel_tool_use" not in choice:
                return None
        elif body.get(param) is None:
            return None
        from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

        if self._outbound_params(req.model, OpenAICompatibleAdapter._request_capability(req)).is_declared(param):
            return None
        return param

    @staticmethod
    def _convert_messages(messages: list[Message], *, sanitize: bool = False) -> list[dict[str, Any]]:
        """Bowhead Message 列表 → Anthropic `messages[]`.

        Anthropic 只认 user/assistant. 把 Bowhead 里的 `tool` role 合并到 user role
        作为 tool_result content block（同官方约定）。合并策略：**连续的 tool 消息合到
        下一个/前一个 user message**，或者独立作为一条 user message（content 是 list）.
        """
        out: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def _emit_user(content: Any) -> None:
            """追加一条 user，**与紧邻的上一条 user 合并**（Anthropic 语义等价）。

            🔍 为什么必须合并：Anthropic 侧把 Bowhead 的 `tool` role 折进 user，所以
            「tool 结果之后再来一条 user」在这里会变成**两条连续 user**。官方 Messages API
            对此是合并成一轮，但**兼容口不保证**（116 生产走的就是兼容口：
            `provider=deepseek_anthropic` / `deepseek-v4-flash`），一旦对端强制交替，
            整轮每一次调用都 400。
            📏 已知会造出这个形状的两条路（都与 provider 无关，属于我们自己的装配）：
              ① hard-bail 软注入（`subagent.py` 的 `messages.append(user, 再不 commit…)`）
                 紧跟在一批 tool 结果之后 —— **这条今天就存在**；
              ② turn-latency A 的 `BOWHEAD_REFS_IN_MESSAGES=1`（refs 清单挂 messages 末尾）。
            ⇒ 在**装配层**收口，而不是让每个 caller 自己小心：合并后线上形状严格交替，
              语义与合并前逐字等价（同一轮里的多个 content block）。
            """
            if out and out[-1]["role"] == "user":
                prev = out[-1]["content"]
                prev_blocks = list(prev) if isinstance(prev, list) else ([{"type": "text", "text": prev}] if prev else [])
                new_blocks = list(content) if isinstance(content, list) else ([{"type": "text", "text": content}] if content else [])
                out[-1]["content"] = prev_blocks + new_blocks
                return
            out.append({"role": "user", "content": content})

        def flush_tool_results() -> None:
            if pending_tool_results:
                _emit_user(list(pending_tool_results))
                pending_tool_results.clear()

        for m in messages:
            if m.role == "tool":
                # Merge tool messages into tool_result content blocks.
                if not m.tool_call_id:
                    raise AnthropicCompatError(
                        kind=ProviderFailureKind.CONFIGURATION,
                        retryable=False,
                        provider_family="anthropic_compatible",
                    )
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content or "",
                    }
                )
                continue

            # Before a non-tool role, flush pending tool_results.
            flush_tool_results()

            if m.role == "assistant":
                # Assistant content may contain text and tool_use.
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        name_out = to_wire_tool_name(tc.name) if sanitize else tc.name
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": name_out,
                                "input": tc.arguments or {},
                            }
                        )
                out.append(
                    {
                        "role": "assistant",
                        "content": blocks if blocks else "",
                    }
                )
                continue

            if m.role == "user":
                # Nonempty user.images produces multiple content blocks (vision).
                # Anthropic format: [{type:"text",text:...},
                #                  {type:"image", source:{type:"base64", media_type:..., data:...}}]
                if m.images:
                    user_blocks: list[dict[str, Any]] = []
                    if m.content:
                        user_blocks.append({"type": "text", "text": m.content})
                    for img in m.images:
                        user_blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": img.mime,
                                    "data": img.base64,
                                },
                            }
                        )
                    _emit_user(user_blocks)
                else:
                    _emit_user(m.content or "")
                continue

            if m.role == "system":
                # system belongs at the top level; ignore it if a caller mistakenly inserts it into messages to avoid conflicts.
                logger.warning(
                    "anthropic_compat: 'system' role message ignored; use LLMRequest.system instead",
                )
                continue

            # Treat unknown roles as user.
            _emit_user(m.content or "")

        flush_tool_results()
        return out

    def _tool_name_map(self, req: LLMRequest) -> ToolNameMap:
        return ToolNameMap.for_request(req) if self.sanitize_tool_dots else ToolNameMap(())

    def _convert_tool(self, tool: ToolSchema) -> dict[str, Any]:
        name = to_wire_tool_name(tool.name) if self.sanitize_tool_dots else tool.name
        return {
            "name": name,
            "description": tool.description or "",
            "input_schema": tool.input_schema
            or {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }

    # ─────────────────────────────────────────────────────────
    # Response parsing
    # ─────────────────────────────────────────────────────────
    def _parse_response(self, data: dict[str, Any], req: LLMRequest) -> LLMResponse:
        blocks = data.get("content") or []
        if not isinstance(blocks, list):
            raise AnthropicCompatError(
                kind=ProviderFailureKind.RESPONSE_INVALID,
                retryable=False,
                provider_family="anthropic_compatible",
            )

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                t = blk.get("text") or ""
                if t:
                    text_parts.append(t)
            elif btype == "thinking":
                # Thinking models（Doubao-Seed-2.0-pro / Claude extended_thinking）:
                # Preserve reasoning traces in LLMResponse.thinking for optional upper-layer printing/UI display.
                tk = blk.get("thinking") or blk.get("text") or ""
                if tk:
                    thinking_parts.append(str(tk))
            elif btype == "tool_use":
                tc_id = str(blk.get("id") or "")
                tc_name = str(blk.get("name") or "")
                tc_name = self._tool_name_map(req).to_canonical(tc_name)
                tc_input = blk.get("input") or {}
                if not isinstance(tc_input, dict):
                    try:
                        tc_input = loads_tool_arguments(tc_input) if isinstance(tc_input, str) else {}
                    except (TypeError, ValueError):
                        tc_input = {}
                tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=tc_input))
            # Ignore other block types (image, etc.).

        stop_reason = str(data.get("stop_reason") or "")
        finish = _STOP_REASON_MAP.get(stop_reason, stop_reason or "stop")
        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("input_tokens") or 0),
            output_tokens=int(usage_raw.get("output_tokens") or 0),
            # perf: anthropic-compat endpoints (deepseek/doubao) automatically cache prefixes; cache hits are reported in
            # cache_read_input_tokens (input_tokens already counts only new, uncached tokens; do not double-count).
            # Reading only input/output previously kept usage.cached_tokens at 0, hiding the true hit rate and overestimating cost.
            cached_tokens=int(usage_raw.get("cache_read_input_tokens") or 0),
        )

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            raw=data,
            provider=self.provider_name,
            model=str(data.get("model") or req.model),
            thinking="\n".join(thinking_parts),
        )

    # ─────────────────────────────────────────────────────────
    # Errors
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def _raise_for_status(resp: httpx2.Response) -> None:
        if resp.status_code // 100 == 2:
            return
        retryable = resp.status_code in (408, 429, 500, 502, 503, 504)
        raise AnthropicCompatError(
            kind=failure_kind_for_status(resp.status_code),
            retryable=retryable,
            status_code=resp.status_code,
            provider_family="anthropic_compatible",
            **upstream_error_fields(resp),
        )


def _truncate_json(obj: Any, max_len: int = 300) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return s[:max_len] + ("…" if len(s) > max_len else "")
