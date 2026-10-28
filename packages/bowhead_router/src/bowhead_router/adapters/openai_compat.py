"""OpenAI-compatible HTTP adapter.

覆盖所有支持 OpenAI `/chat/completions` 协议的厂商：
    - OpenAI 官方
    - 阿里云百炼（DashScope compatible-mode）
    - Kimi / Moonshot
    - DeepSeek
    - 智谱 GLM (open.bigmodel.cn paas/v4)
    - Azure OpenAI（简化模式，不做 deployment 映射 — 有 deployment 用 AzureOpenAIAdapter）
    - 自研 OpenAI 兼容 gateway（vLLM / 其他）

实现要点：
- `LLMRequest` → OpenAI body: messages / tools / temperature / top_p / max_tokens / stop / stream
- `messages` 转换时处理 assistant role 的 `tool_calls`、tool role 的 `tool_call_id`
- `tools` 用 OpenAI 的 `function` 包装
- Response 解析：content / tool_calls（arguments JSON 反解）/ usage / finish_reason
- Streaming：SSE `data: {...}` 累积（M1 Day 2 只实现"整块完成后 yield 多块"简化版）
- 错误映射：401/403 → permission；429 → rate limit；4xx → validation；5xx → connection
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
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
    sanitize_upstream_text,
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
    physical_invocation_required,
    physical_observation_active,
    settle_transport_failure,
)
from bowhead_router.outbound import (
    OutboundParams,
    ProtocolFamily,
    apply_learned_constraints,
    apply_outbound_params,
    learn_constraint,
    outbound_param_in_body,
    rejected_param,
    resolve_outbound_params,
    wire_param_name,
)
from bowhead_router.ssrf_guard import build_guarded_async_client
from bowhead_router.tool_args import loads_tool_arguments
from bowhead_router.tool_names import ToolNameMap, to_wire_tool_name
from bowhead_router.transport import (
    CanonicalProviderTransportPlan,
    build_provider_transport_plan,
    materialize_transport_headers,
    model_api_surface,
    render_custom_http_body,
    uses_max_completion_tokens,
)
from bowhead_router.types import (
    FinishReason,
    LLMAdapter,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    LLMResponseDiagnostics,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from types import TracebackType

logger = logging.getLogger(__name__)

_OPENAI_TOOL_DESCRIPTION_MAX_CHARS = 1024


def _normalize_openai_tool_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove the required-alternatives shape rejected by UPTIMIZE/Azure.

    Bowhead handlers validate their own arguments, so the root ``anyOf`` used
    only to express "at least one of these fields is required" is advisory.
    UPTIMIZE Chat Completions rejects that otherwise-valid JSON Schema with
    HTTP 400. Keep every property and nested constraint; only drop this narrow
    required-only composition shape instead of weakening arbitrary unions.
    """

    normalized = copy.deepcopy(schema)
    alternatives = normalized.get("anyOf")
    if (
        isinstance(alternatives, list)
        and alternatives
        and all(
            isinstance(branch, dict)
            and set(branch) == {"required"}
            and isinstance(branch["required"], list)
            and branch["required"]
            and all(isinstance(name, str) and name for name in branch["required"])
            for branch in alternatives
        )
    ):
        normalized.pop("anyOf")
    return normalized


def _durable_rejection_is_registered(
    error_body: str,
    *,
    adapter_type: str,
    adapter_contract_version: str,
    provider_host: str,
) -> bool:
    """账本开着时，这条 400 是否属于**已登记**的「确定未生效」捕获。

    登记表沿用 `_chi_sniff._DURABLE_REJECTION_MANIFEST_V1`（故意留空、生产 fail closed）
    —— 不新造第二份登记表。它按 (协议, 契约, 主机, param, code) 五元组登记，天然对
    parallel_tool_calls 之外的参数也成立。
    """

    from bowhead_router.adapters import _chi_sniff

    return _chi_sniff.matches_durable_param_rejection_v1(
        error_body,
        adapter_type=adapter_type,
        adapter_contract_version=adapter_contract_version,
        provider_host=provider_host,
    )


# ─────────────────────────────────────────────────────────────
# Errors (handled centrally by the Router; only classified here).
# ─────────────────────────────────────────────────────────────
class OpenAICompatError(ProviderCallError):
    """OpenAI-compatible provider failure."""


# ─────────────────────────────────────────────────────────────
# Finish reason mapping (OpenAI → Bowhead).
# ─────────────────────────────────────────────────────────────
_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filter",
}


def _reasoning_tokens(usage: Any, details_key: str) -> int | None:
    details = usage.get(details_key) if isinstance(usage, dict) else None
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


def _output_token_limit(body: dict[str, Any]) -> int | None:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = body.get(key)
        if type(value) is int and value > 0:
            return value
    return None


@dataclass
class _ResponsesStreamState:
    """Responses 面流式解码的跨帧状态（tech-design §9.1）。

    fc_items: item_id → {call_id, name, args_buf} —— function_call item 在
    `output_item.added` 注册（call_id/name 先到），arguments 增量随
    `function_call_arguments.delta` 累积，`output_item.done` 定稿并发
    `tool_call_delta`。终态字段由 `response.completed`/`response.incomplete` 写。
    """

    fc_items: dict[str, dict[str, str]] = field(default_factory=dict)
    tool_names: ToolNameMap | None = None
    emitted_tool_calls: int = 0
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    usage_input_seen: bool = False
    usage_output_seen: bool = False
    terminal_seen: bool = False
    upstream_finish_reason: str | None = None
    incomplete_reason: str | None = None
    reasoning_tokens: int | None = None


@dataclass
class OpenAICompatibleAdapter:
    """OpenAI `/chat/completions` 协议 adapter.

    使用：

        adapter = OpenAICompatibleAdapter(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="sk-...",
            provider_name="aliyun_qwen",
        )
        resp = await adapter.invoke(LLMRequest(
            model="qwen-plus",
            messages=[Message(role="user", content="hello")],
        ))
        await adapter.close()
    """

    supports_input_admission: ClassVar[bool] = True

    base_url: str
    api_key: str
    provider_name: str = "openai_compat"
    timeout_sec: int = 60
    default_headers: dict[str, str] = field(default_factory=dict)
    ssrf_guard: bool = False
    # Rewrite tool names that providers reject (dots, over-long) on the wire and map them back
    # on inbound tool calls; see bowhead_router.tool_names. Compliant names are never changed.
    sanitize_tool_dots: bool = True
    client_builder: Callable[..., httpx2.AsyncClient] | None = field(
        default=None,
        repr=False,
    )
    adapter_type: str = "openai_compatible"
    provider_options: dict[str, Any] = field(default_factory=dict)
    # httpx2.AsyncClient is created by __post_init__.
    _http: httpx2.AsyncClient = field(init=False, repr=False)
    _transport_plan: CanonicalProviderTransportPlan = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        provider = self._provider_definition()
        # Compatible endpoints have one static URL. Azure and Custom HTTP
        # resolve deployment/transport per model at request time.
        self._transport_plan = (
            build_provider_transport_plan(
                provider,
                purpose="runtime_chat",
            )
            if self.adapter_type == "openai_compatible"
            else CanonicalProviderTransportPlan(
                adapter_type=self.adapter_type,
                adapter_contract_version="deferred-per-model",
                purpose="runtime_chat",
                method="POST",
                request_url=self.base_url,
                safe_header_semantics=(),
                secret_components=(),
                request_options={"timeout_sec": self.timeout_sec},
            )
        )
        kwargs: dict[str, Any] = {"timeout": self.timeout_sec}
        if self.adapter_type == "openai_compatible":
            kwargs["base_url"] = self.base_url
            kwargs["headers"] = materialize_transport_headers(self._transport_plan)
        if self.ssrf_guard:
            builder = self.client_builder or build_guarded_async_client
            self._http = builder(**kwargs)
        else:
            self._http = httpx2.AsyncClient(**kwargs)

    async def __aenter__(self) -> OpenAICompatibleAdapter:
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
        responses_surface = self._responses_surface(req.model)
        body = self._build_body(req, stream=False)
        transport = self._transport_for(req)
        headers = materialize_transport_headers(transport)
        attempt = await begin_physical_invocation(
            provider=self.provider_name,
            model=req.model,
            adapter_contract_key=f"{self.adapter_type}/{transport.adapter_contract_version}",
            mode="invoke",
            request=body,
            input_request=req,
            input_family="openai_responses" if responses_surface else "openai",
        )
        try:
            resp = await self._http.post(
                transport.request_url,
                json=body,
                headers=headers,
            )
        except asyncio.CancelledError:
            await abandon_physical_invocation(
                attempt,
                reason_code="transport_cancelled",
            )
            raise
        except httpx2.TimeoutException as e:
            await settle_transport_failure(attempt, e, output_started=False)
            raise OpenAICompatError(
                kind=ProviderFailureKind.TIMEOUT,
                retryable=True,
                provider_family=self.adapter_type,
            ) from e
        except httpx2.HTTPError as e:
            await settle_transport_failure(attempt, e, output_started=False)
            raise OpenAICompatError(
                kind=ProviderFailureKind.NETWORK,
                retryable=True,
                provider_family=self.adapter_type,
            ) from e
        except Exception:
            await fail_physical_invocation(
                attempt,
                reason_code="transport_unclassified_error",
            )
            raise

        # Adaptive outbound parameters (takes over when enabled; `parallel_tool_calls` is just one allowed parameter):
        # Drop the parameter identified by the provider, retry once, and remember it so the user sees success rather than failure.
        rejected_outbound = (
            self._param_rejection_retry(
                req=req,
                body=body,
                error_body=resp.text[:500],
                durable=attempt is not None,
                transport_contract_version=transport.adapter_contract_version,
                provider_host=(urlsplit(transport.request_url).hostname or "").lower(),
            )
            if resp.status_code == 400
            else None
        )
        if rejected_outbound is not None:
            if attempt is not None:
                await confirm_physical_not_applied(
                    attempt,
                    evidence_code=f"outbound_param_rejected/{rejected_outbound}/v1",
                    evidence={"status": 400, "param": rejected_outbound},
                )
            if learn_constraint(adapter_type=self.adapter_type, base_url=self.base_url, model=req.model, param=rejected_outbound):
                logger.warning(
                    "outbound.param_rejected provider=%s model=%s param=%s — dropping it for this model and retrying once.",
                    self.provider_name,
                    req.model,
                    rejected_outbound,
                )
            body_retry = self._build_body(req, stream=False)
            attempt = await begin_physical_invocation(
                provider=self.provider_name,
                model=req.model,
                adapter_contract_key=f"{self.adapter_type}/{transport.adapter_contract_version}",
                mode="invoke",
                request=body_retry,
                input_request=req,
                input_family="openai_responses" if responses_surface else "openai",
            )
            try:
                resp = await self._http.post(
                    transport.request_url,
                    json=body_retry,
                    headers=headers,
                )
            except asyncio.CancelledError:
                await abandon_physical_invocation(attempt, reason_code="transport_cancelled")
                raise
            except httpx2.TimeoutException as e:
                await settle_transport_failure(attempt, e, output_started=False)
                raise OpenAICompatError(
                    kind=ProviderFailureKind.TIMEOUT,
                    retryable=True,
                    provider_family=self.adapter_type,
                ) from e
            except httpx2.HTTPError as e:
                await settle_transport_failure(attempt, e, output_started=False)
                raise OpenAICompatError(
                    kind=ProviderFailureKind.NETWORK,
                    retryable=True,
                    provider_family=self.adapter_type,
                ) from e
            except Exception:
                await fail_physical_invocation(attempt, reason_code="transport_unclassified_error")
                raise
        # Sprint χ-1 (existing behavior when disabled, preserved verbatim): 400 + a parallel-related body.
        # reject → drop flag + retry once without parallel_tool_calls。
        # ⚠ Chat surface only: chi patterns are curated for chat bodies; unrelated Responses 400s (such as qwen-plus
        # tool-result submissions) would trigger the "ADD this body fragment" ERROR and mislead ops (review P2-8).
        elif not responses_surface and resp.status_code == 400 and "parallel_tool_calls" in body:
            from bowhead_router.adapters import _chi_sniff

            err_body = resp.text[:500]
            rejected = (
                _chi_sniff.matches_durable_param_rejection_v1(
                    err_body,
                    adapter_type=self.adapter_type,
                    adapter_contract_version=transport.adapter_contract_version,
                    provider_host=(urlsplit(transport.request_url).hostname or "").lower(),
                )
                if attempt is not None
                else _chi_sniff.matches_param_unknown(err_body)
            )
            if rejected:
                if attempt is not None:
                    await confirm_physical_not_applied(
                        attempt,
                        evidence_code="parallel_tool_calls_parameter_rejected/v1",
                        evidence=json.loads(err_body),
                    )
                _chi_sniff.mark_dropped(self.provider_name)
                # Retry without the flag — rebuild body (sniff drop now in effect)
                body_retry = self._build_body(req, stream=False)
                attempt = await begin_physical_invocation(
                    provider=self.provider_name,
                    model=req.model,
                    adapter_contract_key=f"{self.adapter_type}/{transport.adapter_contract_version}",
                    mode="invoke",
                    request=body_retry,
                    input_request=req,
                    input_family="openai_responses" if responses_surface else "openai",
                )
                try:
                    resp = await self._http.post(
                        transport.request_url,
                        json=body_retry,
                        headers=headers,
                    )
                except asyncio.CancelledError:
                    await abandon_physical_invocation(
                        attempt,
                        reason_code="transport_cancelled",
                    )
                    raise
                except httpx2.TimeoutException as e:
                    await settle_transport_failure(attempt, e, output_started=False)
                    raise OpenAICompatError(
                        kind=ProviderFailureKind.TIMEOUT,
                        retryable=True,
                        provider_family=self.adapter_type,
                    ) from e
                except httpx2.HTTPError as e:
                    await settle_transport_failure(attempt, e, output_started=False)
                    raise OpenAICompatError(
                        kind=ProviderFailureKind.NETWORK,
                        retryable=True,
                        provider_family=self.adapter_type,
                    ) from e
                except Exception:
                    await fail_physical_invocation(
                        attempt,
                        reason_code="transport_unclassified_error",
                    )
                    raise
            # Review P0: sniff miss but param was sent → silent fail mode.
            # Warn-once per provider (χ-1.2 P1 mitigation: storm + body leak).
            # First miss: ERROR with body excerpt (ops gets full signal once).
            # Subsequent misses: DEBUG only (storm-safe).
            elif _chi_sniff.mark_sniff_missed(self.provider_name):
                logger.error(
                    "chi.sniff_miss provider=%s status=%d body=%r — "
                    "parallel_tool_calls in payload but error did not match "
                    "sniff patterns; adapter will keep retrying every request. "
                    "ADD this body fragment to _chi_sniff._HARDCODED_PATTERNS.",
                    self.provider_name,
                    resp.status_code,
                    sanitize_upstream_text(err_body, max_length=200),
                )
            else:
                logger.debug(
                    "chi.sniff_miss.repeat provider=%s status=%d",
                    self.provider_name,
                    resp.status_code,
                )

        if responses_surface and resp.status_code in {400, 404, 405}:
            # Surface-specific diagnostics belong to batch 2 (§5-5); avoid sending administrators in the wrong
            # direction with "request rejected, upstream_code=…" (review P2-7: qwen-plus /responses tool-result
            # submission 400s would incorrectly suggest a parameter issue).
            logger.warning(
                "openai_compat.responses.surface_error provider=%s model=%s status=%d — if this gateway/model lacks the /responses surface, set the model's api_surface back to chat_completions.",
                self.provider_name,
                req.model,
                resp.status_code,
            )
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
                raise OpenAICompatError(
                    kind=ProviderFailureKind.RESPONSE_INVALID,
                    retryable=False,
                    status_code=resp.status_code,
                    provider_family=self.adapter_type,
                ) from exc
            if not isinstance(data, dict):
                raise OpenAICompatError(
                    kind=ProviderFailureKind.RESPONSE_INVALID,
                    retryable=False,
                    status_code=resp.status_code,
                    provider_family=self.adapter_type,
                )
            response = self._parse_responses_payload(data, req) if responses_surface else self._parse_response(data, req)
            if response.diagnostics is not None:
                response.diagnostics = dataclasses.replace(response.diagnostics, output_token_limit=_output_token_limit(body))
        except InvocationOutcomeUnknownError:
            raise
        except ProviderCallError as exc:
            exc.api_surface = "responses" if responses_surface else "chat_completions"
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
        # Keys differ by surface (Responses: input_/output_tokens, cached in input_tokens_details;
        # missing details yields 0, invalid types yield None on both surfaces; tech-design §8.7).
        details_key = "input_tokens_details" if responses_surface else "prompt_tokens_details"
        details = usage_raw.get(details_key) if isinstance(usage_raw, dict) else None
        cached_value = details.get("cached_tokens", 0) if isinstance(details, dict) else 0 if details is None else None
        authoritative_usage = exact_authoritative_usage(
            usage_raw if isinstance(usage_raw, dict) else None,
            input_key="input_tokens" if responses_surface else "prompt_tokens",
            output_key="output_tokens" if responses_surface else "completion_tokens",
            cached_value=cached_value,
        )
        await complete_physical_invocation(
            attempt,
            usage=authoritative_usage,
            response=data,
        )
        return response

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        """P4.B + spike-2: 真 SSE stream w/ tool_call accumulation.

        OpenAI 兼容 chat/completions stream=true returns SSE lines:
            data: {"choices":[{"delta":{"content":"hello"}}]}\n\n
            data: {"choices":[{"delta":{"content":" world"}}]}\n\n
            data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","function":{"name":"f","arguments":""}}]}}]}\n\n
            data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"a\\":"}}]}}]}\n\n
            data: [DONE]\n\n

        Tool-call accumulation: per-index buffer (id+name from first delta,
        arguments string concatenated across deltas). On finish_reason="tool_calls"
        OR stream end with non-empty buffers, parse + emit each as
        ``LLMChunk(tool_call_delta=ToolCall(...))``. Malformed JSON yields empty
        args + warn log (callers fall back to invoke per Sprint κ §4.1.3).
        """
        import json as _json

        transport = self._transport_for(req)
        # Batch 1: decode real SSE on the Responses surface (frame dispatch is inside the loop; see responses_surface).
        # The batch 0 invoke fallback has served its purpose and was removed (tech-design §9).
        responses_surface = self._responses_surface(req.model)
        if self.adapter_type == "custom_http" and transport.adapter_contract_version == "openai_chat_completions":
            response = await self.invoke(req)
            if response.content:
                yield LLMChunk(delta=response.content)
            for tool_call in response.tool_calls:
                yield LLMChunk(tool_call_delta=tool_call)
            yield LLMChunk(
                finish_reason=response.finish_reason,
                usage=response.usage,
                diagnostics=response.diagnostics,
            )
            return

        durable_usage = physical_invocation_required() or physical_observation_active()
        # Trust the configured protocol, not a hostname list. Durable Chat
        # Completions requests ask for usage and settle only observed usage.
        body = self._build_body(
            req,
            stream=True,
            durable_stream_usage=durable_usage,
        )
        headers = materialize_transport_headers(transport)
        # Per-index tool_call accumulators.
        # Each entry: {"id": str, "name": str, "args_buf": str}
        tool_calls_buf: dict[int, dict[str, Any]] = {}
        tool_names = self._tool_name_map(req)
        responses_state = _ResponsesStreamState(tool_names=tool_names)
        # Sprint χ-1.1: 2 attempts max (initial + sniff-drop retry).
        attempt = None
        attempt_settled = False
        # Any yielded output makes a later transport failure unknown, never retried.
        output_started = False
        try:
            for _attempt in range(2):
                attempt = None
                attempt_settled = False
                attempt = await begin_physical_invocation(
                    provider=self.provider_name,
                    model=req.model,
                    adapter_contract_key=(f"{self.adapter_type}/{transport.adapter_contract_version}/authoritative-stream-usage-v1"),
                    mode="stream",
                    request=body,
                    input_request=req,
                    input_family="openai_responses" if responses_surface else "openai",
                )
                async with self._http.stream(
                    "POST",
                    transport.request_url,
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        err_text = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                        if responses_surface and resp.status_code in {400, 404, 405}:
                            # Use the same targeted hint as invoke (review R2: after batch 1, tool-result submissions use stream;
                            # qwen-plus submission 400s reach this path, so an invoke-only hint would never run).
                            logger.warning(
                                "openai_compat.responses.surface_error provider=%s model=%s status=%d — if this gateway/model lacks the /responses surface, set the model's api_surface back to chat_completions.",
                                self.provider_name,
                                req.model,
                                resp.status_code,
                            )
                        # Adaptive outbound parameters (the streaming path uses the same criteria as invoke).
                        rejected_outbound = (
                            self._param_rejection_retry(
                                req=req,
                                body=body,
                                error_body=err_text,
                                durable=attempt is not None,
                                transport_contract_version=transport.adapter_contract_version,
                                provider_host=(urlsplit(transport.request_url).hostname or "").lower(),
                            )
                            if _attempt == 0 and resp.status_code == 400
                            else None
                        )
                        if rejected_outbound is not None:
                            if attempt is not None:
                                await confirm_physical_not_applied(
                                    attempt,
                                    evidence_code=f"outbound_param_rejected/{rejected_outbound}/v1",
                                    evidence={"status": 400, "param": rejected_outbound},
                                )
                                attempt_settled = True
                                attempt = None
                            if learn_constraint(adapter_type=self.adapter_type, base_url=self.base_url, model=req.model, param=rejected_outbound):
                                logger.warning(
                                    "outbound.param_rejected provider=%s model=%s param=%s mode=stream — dropping it for this model and retrying once.",
                                    self.provider_name,
                                    req.model,
                                    rejected_outbound,
                                )
                            body = self._build_body(
                                req,
                                stream=True,
                                durable_stream_usage=durable_usage,
                            )
                            continue  # retry once
                        # Sprint χ-1.1 stream sniff (review P0-1): mirror invoke logic.
                        # ⚠ Same guard as invoke: chi patterns are curated for chat bodies; unrelated Responses
                        # 400s would trigger the "ADD this body fragment" ERROR (review P2-8).
                        if not responses_surface and _attempt == 0 and resp.status_code == 400 and "parallel_tool_calls" in body:
                            from bowhead_router.adapters import _chi_sniff

                            rejected = (
                                _chi_sniff.matches_durable_param_rejection_v1(
                                    err_text,
                                    adapter_type=self.adapter_type,
                                    adapter_contract_version=(transport.adapter_contract_version),
                                    provider_host=(urlsplit(transport.request_url).hostname or "").lower(),
                                )
                                if attempt is not None
                                else _chi_sniff.matches_param_unknown(err_text)
                            )
                            if rejected:
                                if attempt is not None:
                                    await confirm_physical_not_applied(
                                        attempt,
                                        evidence_code="parallel_tool_calls_parameter_rejected/v1",
                                        evidence=_json.loads(err_text),
                                    )
                                attempt_settled = True
                                attempt = None
                                _chi_sniff.mark_dropped(self.provider_name)
                                body = self._build_body(
                                    req,
                                    stream=True,
                                    durable_stream_usage=durable_usage,
                                )  # rebuild w/o flag
                                continue  # retry once
                        if resp.status_code in (401, 403):
                            await confirm_provider_auth_rejection(attempt, status_code=resp.status_code)
                            attempt_settled = True
                            attempt = None
                        elif resp.status_code == 429:
                            await confirm_provider_rate_limit_rejection(attempt, status_code=resp.status_code)
                            attempt_settled = True
                            attempt = None
                        raise OpenAICompatError(
                            kind=failure_kind_for_status(resp.status_code),
                            retryable=resp.status_code in (408, 429) or resp.status_code >= 500,
                            status_code=resp.status_code,
                            provider_family=self.adapter_type,
                            **upstream_error_fields(resp),
                        )
                    finish_reason: FinishReason | None = None
                    upstream_finish_reason = None
                    reasoning_tokens = None
                    legacy_function_call = False
                    usage: TokenUsage | None = None
                    usage_input_seen = False
                    usage_output_seen = False
                    terminal_frame_seen = False
                    stream_evidence = hashlib.sha256() if attempt is not None else None
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            if responses_surface:
                                # Responses termination requires response.completed (review R1-P2-6):
                                # if a compatible gateway sends [DONE] as in chat but omits completed, marking a terminal frame here
                                # would falsely succeed with finish=None/usage=None. This defect should produce
                                # RESPONSE_INVALID and allow fallback. Ignore it as an ordinary frame.
                                continue
                            terminal_frame_seen = True
                            break
                        try:
                            obj = _json.loads(payload)
                        except _json.JSONDecodeError:
                            if durable_usage:
                                raise OpenAICompatError(
                                    kind=ProviderFailureKind.RESPONSE_INVALID,
                                    retryable=False,
                                    status_code=resp.status_code,
                                    provider_family=self.adapter_type,
                                ) from None
                            continue
                        if not isinstance(obj, dict):
                            if durable_usage:
                                raise OpenAICompatError(
                                    kind=ProviderFailureKind.RESPONSE_INVALID,
                                    retryable=False,
                                    status_code=resp.status_code,
                                    provider_family=self.adapter_type,
                                )
                            continue
                        if stream_evidence is not None:
                            stream_evidence.update(
                                bytes.fromhex(canonical_transport_digest(obj)),
                            )
                        if responses_surface:
                            if responses_state.terminal_seen:
                                # Ignore all frames after termination (review R1-P2-4): a late error
                                # frame would fail an already delivered stream, and an out-of-order done frame would produce
                                # the contradictory combination of finish=stop with tool calls.
                                continue
                            # Dispatch Responses frames (tech-design §9.2), bypassing chat-specific in-band
                            # sniffing, prompt_tokens usage, choices decoding, and [DONE].
                            for chunk in self._responses_stream_chunks(
                                obj,
                                responses_state,
                                durable_usage=durable_usage,
                            ):
                                output_started = True
                                yield chunk
                            if responses_state.terminal_seen:
                                finish_reason = responses_state.finish_reason
                                usage = responses_state.usage
                                usage_input_seen = responses_state.usage_input_seen
                                usage_output_seen = responses_state.usage_output_seen
                                upstream_finish_reason = responses_state.upstream_finish_reason
                                terminal_frame_seen = True
                            continue
                        if obj.get("error") is not None or (not obj.get("choices") and "message" in obj and ("id" in obj or "type" in obj)):
                            kind = failure_kind_for_error_payload(obj)
                            raise OpenAICompatError(
                                kind=kind,
                                retryable=stream_failure_kind_allows_fallback(kind),
                                provider_family=self.adapter_type,
                                **upstream_error_fields_from_payload(
                                    obj,
                                    headers=resp.headers,
                                ),
                            )
                        usage_raw = obj.get("usage")
                        if isinstance(usage_raw, dict):
                            reasoning_tokens = _reasoning_tokens(usage_raw, "completion_tokens_details")
                        if durable_usage and isinstance(usage_raw, dict):
                            details = usage_raw.get("prompt_tokens_details")
                            cached_value = details.get("cached_tokens", 0) if isinstance(details, dict) else 0 if details is None else None
                            usage = exact_authoritative_usage(
                                usage_raw,
                                input_key="prompt_tokens",
                                output_key="completion_tokens",
                                cached_value=cached_value,
                            )
                            usage_input_seen = usage is not None
                            usage_output_seen = usage is not None
                        choices = obj.get("choices") or []
                        if not choices:
                            if not durable_usage and isinstance(usage_raw, dict) and usage_raw:
                                usage_input_seen = "prompt_tokens" in usage_raw
                                usage_output_seen = "completion_tokens" in usage_raw
                                usage = TokenUsage(
                                    input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                                )
                            continue
                        delta = choices[0].get("delta") or {}
                        legacy_function_call = legacy_function_call or isinstance(delta.get("function_call"), dict)
                        if choices[0].get("finish_reason") is not None:
                            upstream_finish_reason = choices[0]["finish_reason"]
                        content = delta.get("content") or ""
                        # P4.D: DashScope qwen3.6 thinking and OpenAI o1-like reasoning use reasoning_content.
                        # Keep it separate from content; usually only one is nonempty in a given chunk.
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if content or reasoning:
                            output_started = True
                            yield LLMChunk(delta=content, thinking_delta=reasoning)
                        # spike-2: accumulate tool_call deltas keyed by index.
                        tc_deltas = delta.get("tool_calls") or []
                        for tc in tc_deltas:
                            idx = int(tc.get("index", 0))
                            slot = tool_calls_buf.setdefault(
                                idx,
                                {"id": "", "name": "", "args_buf": ""},
                            )
                            if tc.get("id"):
                                slot["id"] = str(tc["id"])
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = str(fn["name"])
                            if fn.get("arguments"):
                                partial = str(fn["arguments"])
                                slot["args_buf"] += partial
                                # Sprint chat-redesign-2026-05-21 Phase 1.1 (Spike 1):
                                # yield incremental partial for `lead_thinking_writing` UX.
                                # OpenAI ordering: id+name arrive in earlier chunk, so by
                                # the time arguments stream we usually have both. Defensive
                                # fallback `call_<idx>` matches the final-flush convention.
                                if slot["name"]:
                                    output_started = True
                                    yield LLMChunk(
                                        tool_call_partial={
                                            "tool_name": slot["name"],
                                            "tool_call_id": slot["id"] or f"call_{idx}",
                                            "partial_args_json": partial,
                                        }
                                    )
                        fr = choices[0].get("finish_reason")
                        if fr:
                            finish_reason = fr
                            terminal_frame_seen = True
                    if not terminal_frame_seen:
                        raise OpenAICompatError(
                            kind=ProviderFailureKind.RESPONSE_INVALID,
                            retryable=True,
                            status_code=resp.status_code,
                            provider_family=self.adapter_type,
                        )
                    # Stream end: emit accumulated tool_calls (parse args JSON each).
                    # OpenAI spec: tool_calls finalized when finish_reason="tool_calls"
                    # but be defensive — emit on any non-empty buffer.
                    for idx in sorted(tool_calls_buf.keys()):
                        slot = tool_calls_buf[idx]
                        if not slot["name"]:
                            continue  # skip incomplete
                        try:
                            args = loads_tool_arguments(slot["args_buf"]) if slot["args_buf"] else {}
                        except _json.JSONDecodeError as _je:
                            # As in anthropic_compat, include the decoder's own **error reason and position**
                            # along with the buffer length/tail: two failures measured on 116 had only 3317/2454 characters
                            # and ended with a closing `}`, so they were not truncated for excessive length; only err/pos could distinguish the cause.
                            logger.warning(
                                "openai_compat.stream.tool_args_invalid_json id=%s name=%s finish_reason=%s buf_len=%d err=%s pos=%s around=%r head=%s tail=%s",
                                slot["id"],
                                slot["name"],
                                finish_reason,
                                len(slot["args_buf"]),
                                _je.msg,
                                _je.pos,
                                slot["args_buf"][max(0, _je.pos - 60) : _je.pos + 60],
                                slot["args_buf"][:200],
                                slot["args_buf"][-120:],
                            )
                            # follow-up bugs 0710 ② fix: **never silently swallow truncated/invalid streaming arguments as {}**.
                            # That would make downstream report a missing parameter and wrongly blame the model (live evidence: skill_invoke supplied
                            # name and the entire subagent_return payload was swallowed because max-tokens truncated the JSON).
                            # Use the existing _args_parse_error channel instead (the same key as tool_registry raw-unwrap,
                            # already consumed by skill_invoke/subagent_return/canvas_*), so the LLM receives an accurate diagnosis and can repair and resend.
                            args = {
                                "_args_parse_error": args_parse_hint(_je, slot["args_buf"], finish_reason),
                                "_raw_args_preview": slot["args_buf"][:800],
                            }
                        output_started = True
                        yield LLMChunk(
                            tool_call_delta=ToolCall(
                                id=slot["id"] or f"call_{idx}",
                                name=tool_names.to_canonical(slot["name"]),
                                arguments=args,
                            ),
                        )
                    await complete_physical_invocation(
                        attempt,
                        usage=(usage if usage_input_seen and usage_output_seen else None),
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
                    yield LLMChunk(
                        finish_reason=finish_reason,
                        usage=usage,
                        diagnostics=LLMResponseDiagnostics(
                            transport_mode="stream",
                            upstream_tool_call_count=len(tool_calls_buf) if not responses_surface else None,
                            upstream_finish_reason=upstream_finish_reason,
                            legacy_function_call=legacy_function_call if not responses_surface else None,
                            incomplete_reason=responses_state.incomplete_reason if responses_surface else None,
                            reasoning_tokens=responses_state.reasoning_tokens if responses_surface else reasoning_tokens,
                            output_token_limit=_output_token_limit(body),
                        ),
                    )
                    return  # χ-1.1: stream consumed OK, don't retry the outer for
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
            raise OpenAICompatError(
                kind=ProviderFailureKind.TIMEOUT,
                retryable=True,
                provider_family=self.adapter_type,
            ) from e
        except httpx2.HTTPError as e:
            await settle_transport_failure(attempt, e, output_started=output_started)
            raise OpenAICompatError(
                kind=ProviderFailureKind.NETWORK,
                retryable=True,
                provider_family=self.adapter_type,
            ) from e
        except InvocationOutcomeUnknownError:
            raise
        except ProviderCallError as exc:
            exc.api_surface = "responses" if responses_surface else "chat_completions"
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
    def _provider_definition(self) -> dict[str, Any]:
        return {
            **self.provider_options,
            "type": self.adapter_type,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "default_headers": self.default_headers,
            "timeout_sec": self.timeout_sec,
        }

    def _responses_surface(self, model_id: str) -> bool:
        """Responses 面判定：模型级声明，仅 openai_compatible 消费（tech-design §8.1）。

        面解析全仓只有 `transport.model_api_surface` 一份实现——这里不许再写第二份读取。
        azure_openai 的 deployment 路径逻辑与 Responses 每条都相反，声明了也不生效；
        custom_http 走自己的模板契约 seam。"""

        return self.adapter_type == "openai_compatible" and model_api_surface(self._provider_definition(), model_id) == "responses"

    def _family_for(self, model_id: str) -> ProtocolFamily:
        return "openai_responses" if self._responses_surface(model_id) else "openai"

    def _outbound_params(self, model_id: str, capability: str = "chat") -> OutboundParams:
        from bowhead_router.runtime_parameters import effective_outbound_params

        return effective_outbound_params(
            self._provider_definition(),
            model_id=model_id,
            capability=capability,
            family=self._family_for(model_id),
        )

    def _param_rejection_retry(
        self,
        *,
        req: LLMRequest,
        body: dict[str, Any],
        error_body: str,
        durable: bool = False,
        transport_contract_version: str = "",
        provider_host: str = "",
    ) -> str | None:
        """Return the rejected outbound parameter when a bounded retry is safe.

        The parameter must be allowlisted by rejected_param, present in this request,
        and left undeclared by the administrator. Messages, tools, and model are never
        adapted; explicit administrator choices take precedence over observations."""

        # With durable accounting enabled, accept only structured parameter identification: prose mentions are ambiguous,
        # and this call is already in the ledger, so resending could charge twice.
        param = rejected_param(error_body, allow_prose=not durable, family=self._family_for(req.model))
        if param is None:
            return None
        if durable and not _durable_rejection_is_registered(
            error_body,
            adapter_type=self.adapter_type,
            adapter_contract_version=transport_contract_version,
            provider_host=provider_host,
        ):
            # With the ledger enabled, do not infer "definitely not applied" ourselves: only registered provider captures
            # qualify (`_chi_sniff._DURABLE_REJECTION_MANIFEST_V1`, deliberately empty so production fails closed).
            # Treat everything else as "possibly applied": prefer an unknown call outcome over resending.
            return None
        if not outbound_param_in_body(body, param, self._family_for(req.model)):
            return None
        if self._outbound_params(req.model, self._request_capability(req)).is_declared(param):
            return None
        return param

    def _transport_for(self, req: LLMRequest) -> CanonicalProviderTransportPlan:
        if self.adapter_type == "openai_compatible":
            if self._responses_surface(req.model):
                # The Responses surface is model-specific; the static plan targets chat, so rebuild it per request.
                # ⚠ Changing the ledger adapter_contract_key to openai_compatible/openai-responses-v1
                # depends entirely on this branch (tech-design §8.2 / review B-3).
                return build_provider_transport_plan(
                    self._provider_definition(),
                    purpose="runtime_chat",
                    model_id=req.model,
                    capability=self._request_capability(req),
                )
            return self._transport_plan
        return build_provider_transport_plan(
            self._provider_definition(),
            purpose="runtime_chat",
            model_id=req.model,
            capability=self._request_capability(req),
        )

    @staticmethod
    def _request_capability(req: LLMRequest) -> str:
        if req.tools:
            return "tool"
        if any(message.images for message in req.messages):
            return "vision"
        return "chat"

    def _build_body(
        self,
        req: LLMRequest,
        *,
        stream: bool,
        durable_stream_usage: bool = False,
    ) -> dict[str, Any]:
        responses_surface = self._responses_surface(req.model)
        family: ProtocolFamily = "openai_responses" if responses_surface else "openai"
        from bowhead_router.runtime_notes import model_messages

        messages = model_messages(req.messages)
        if responses_surface:
            # Responses head (tech-design §8.4/§8.5): only the head differs; tail steps 1-6 are shared with chat.
            body: dict[str, Any] = {
                "model": req.model,
                "input": self._convert_input_items(messages, has_instructions=bool(req.system)),
                # Provider-side retention defaults to enabled (DashScope was verified to default store to true); an enterprise product
                # must not silently enable it on a surface switch. Revisit in batch 3 for previous_response_id (review P1-5).
                "store": False,
            }
            if req.system:
                body["instructions"] = req.system
            if stream:
                # false is the documented default; explicit false has not been verified, so conservatively send only True.
                body["stream"] = True
        else:
            body = {
                "model": req.model,
                "messages": self._convert_messages(messages, req.system),
                "stream": stream,
            }
        # Add optional fields as needed (shared by both surfaces; the output-limit key differs).
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None and req.top_p != 1.0:
            body["top_p"] = req.top_p
        from bowhead_router.output_budget import initial_output_allowance

        body["max_output_tokens" if responses_surface else "max_tokens"] = initial_output_allowance(req, self._provider_definition())
        if req.stop:
            if responses_surface:
                # Responses has no stop parameter (tech-design §8.4); drop it and log the omission.
                logger.debug(
                    "openai_compat.responses.stop_dropped provider=%s model=%s n=%d",
                    self.provider_name,
                    req.model,
                    len(req.stop),
                )
            else:
                body["stop"] = req.stop
        if req.tools:
            body["tools"] = [(self._convert_tool_flat(t) if responses_surface else self._convert_tool(t)) for t in self._wire_tools(req.tools)]
            body["tool_choice"] = req.tool_choice
            # Sprint χ-1: pass through parallel_tool_calls when enabled and the provider has not
            # rejected it before (sniff drop). Qwen defaults to false, so enable it explicitly;
            # DeepSeek documents support for 128 parallel calls without naming the parameter, so enable it there too as a fallback.
            from bowhead_core import chi_flags  # type: ignore[import-untyped]
            from bowhead_router.adapters import _chi_sniff

            if req.tool_choice == "auto" and chi_flags.parallel_emit_enabled() and not _chi_sniff.is_dropped(self.provider_name):
                body["parallel_tool_calls"] = True
        # Pass through response_format: DashScope/OpenAI support `{"type": "json_object"}` to enforce JSON.
        # The Responses equivalent is text.format (json_object was verified on both real endpoints;
        # samples/*-jsonobject.json). About 19 invoke call sites depend on it (review P1-4).
        if req.response_format:
            if responses_surface:
                body["text"] = {"format": req.response_format}
            else:
                body["response_format"] = req.response_format
        # Per-model request_body_overrides (an existing V2ModelDeclaration field previously consumed only by custom_http):
        # shallow-merge by capability key into body; protected runtime fields cannot be overridden. A typical use is DashScope
        # reasoning models with {"chat": {"enable_thinking": false}} (thinking defaults to enabled; a nonstreaming call takes 30s+).
        model_overrides = next(
            (
                item.get("request_body_overrides")
                for item in self.provider_options.get("models", [])
                if isinstance(item, dict) and item.get("id") == req.model
            ),
            None,
        )
        if isinstance(model_overrides, dict):
            capability_overrides = model_overrides.get(self._request_capability(req))
            if isinstance(capability_overrides, dict):
                protected = (
                    # Protect store: overrides must not change the unconditional store:false back to
                    # true (review R4; revisit in batch 3 for previous_response_id).
                    {"model", "input", "instructions", "stream", "store", "tools", "tool_choice"}
                    if responses_surface
                    else {"model", "messages", "stream", "tools", "tool_choice"}
                )
                for key, value in capability_overrides.items():
                    if key not in protected:
                        # Existing overrides use chat semantic keys ({"chat":{"max_tokens":…}}); failing to translate them
                        # after a surface switch leaves duplicate keys and makes adaptive retries delete the wrong one (the verified
                        # chain in review R1-P1-1). On chat, wire_param_name is the identity mapping, preserving behavior.
                        body[wire_param_name(key, family)] = value
        # Outbound parameters: apply explicit administrator declarations first, then learned constraints (only to undeclared parameters).
        # Order determines precedence: human declarations > machine observations. Declarations allow "no intervention/override/drop/cap",
        # so apply them after overrides as the final explicit human action.
        from bowhead_router.runtime_parameters import apply_runtime_parameters

        params = self._outbound_params(req.model, self._request_capability(req))
        apply_outbound_params(body, resolve_outbound_params(self._provider_definition(), model_id=req.model, family=family), family=family)
        if self.adapter_type != "custom_http":
            apply_runtime_parameters(body, self._provider_definition(), model_id=req.model, capability=self._request_capability(req), family=family)
        apply_learned_constraints(
            body,
            adapter_type=self.adapter_type,
            base_url=self.base_url,
            model=req.model,
            params=params,
            family=family,
        )
        if durable_stream_usage and not responses_surface:
            # Responses supplies native terminal usage; Chat Completions must
            # request it before the final outbound body is bound to its receipt.
            body["stream_options"] = {"include_usage": True}
        from bowhead_router.output_budget import finalize_output_allowance

        if self.adapter_type == "azure_openai" and uses_max_completion_tokens(
            req.model,
        ):
            max_tokens = body.pop("max_tokens", None)
            if max_tokens is not None:
                body["max_completion_tokens"] = max_tokens
        if self.adapter_type == "custom_http":
            admit_outbound_input(req, body, family="unsupported_custom_http")
            rendered = render_custom_http_body(
                self._provider_definition(),
                model_id=req.model,
                capability=self._request_capability(req),
                variables={
                    "model": req.model,
                    "deployment": next(
                        (
                            str(item.get("deployment") or req.model)
                            for item in self.provider_options.get("models", [])
                            if isinstance(item, dict) and item.get("id") == req.model
                        ),
                        req.model,
                    ),
                    "messages": body.get("messages"),
                    "tools": body.get("tools"),
                    "tool_choice": body.get("tool_choice"),
                    "stream": stream,
                    "max_output_tokens": body.get("max_tokens"),
                    "input": None,
                },
            )
            rendered = apply_runtime_parameters(
                rendered, self._provider_definition(), model_id=req.model, capability=self._request_capability(req), family=family
            )
            if durable_stream_usage:
                rendered["stream_options"] = {"include_usage": True}
            definition = self._provider_definition()
            capability = self._request_capability(req)
            from bowhead_router.runtime_parameters import model_spec

            transport_ref = (model_spec(definition, req.model).get("capability_transports") or {}).get(capability)
            template = ((definition.get("transports") or {}).get(transport_ref, {}).get("request") or {}).get("body_template") or {}
            constant_output = any(
                key in template and template[key] != {"$bowhead": "max_output_tokens"}
                for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            )
            finalize_output_allowance(req, rendered, definition, capability=capability, family=family, constant_output=constant_output)
            return rendered
        finalize_output_allowance(req, body, self._provider_definition(), capability=self._request_capability(req), family=family)
        admit_outbound_input(req, body, family=family)
        return body

    def _convert_messages(
        self,
        messages: list[Message],
        system: str | None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for m in messages:
            entry: dict[str, Any] = {"role": m.role}

            # content: OpenAI requires a string or null; the tool role requires a string.
            # If m.images is nonempty, convert the user message to multiple content blocks (vision).
            if m.role == "user" and m.images:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for img in m.images:
                    # OpenAI-compatible format (accepted by both Qwen VL and GPT-4V):
                    # {type: image_url, image_url: {url: "data:image/png;base64,..."}}
                    blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{img.mime};base64,{img.base64}"},
                        }
                    )
                entry["content"] = blocks
            elif m.content:
                entry["content"] = m.content
            elif m.role == "assistant" and m.tool_calls:
                # tool_calls without content is valid.
                entry["content"] = None
            else:
                entry["content"] = ""

            if m.role == "tool":
                if not m.tool_call_id:
                    raise OpenAICompatError(
                        kind=ProviderFailureKind.CONFIGURATION,
                        retryable=False,
                        provider_family=self.adapter_type,
                    )
                entry["tool_call_id"] = m.tool_call_id
                if m.name:
                    entry["name"] = self._wire_tool_name(m.name) if m.role == "tool" else m.name

            if m.role == "assistant" and m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": self._wire_tool_name(tc.name),
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]

            # Same normalization as anthropic_compat: **merge adjacent user messages into one**.
            #
            # 🔍 Both surfaces need this: `BOWHEAD_REFS_IN_MESSAGES=1` emits
            # `[user(task), user(refs)]` on the first iteration and `[…, tool, user(refs), user(bail)]` on the bail iteration.
            # The OpenAI specification allows consecutive messages with the same role, but **that was an unverified assumption**.
            # The previous failure came from assuming official Anthropic API behavior also applied to the compatible endpoint used in production.
            # ⇒ Make outbound messages consistently alternate at assembly time instead of relying on each provider.
            # ⚠ **Merge multimodal messages too**, not just plain text.
            #   The previous version excluded users with image blocks because their structure differed and the benefit seemed too small for the risk.
            #   Live review rejected that: with images on the Lead turn (`_lead_message_images`) and a nonempty allowlist
            #   (from the second turn onward), the first iteration emits `[user(task,images), user(refs)]`,
            #   still **two consecutive user messages**. Enabling the flag by default made this former opt-in edge path the default,
            #   contradicting three code/documentation claims that outbound messages always alternate without relying on provider tolerance.
            #   ⇒ If either side is a block list, normalize both to block lists and concatenate (OpenAI
            #     `{"type":"text","text":…}` blocks are peers of image_url blocks; preserve their order).
            if out and out[-1]["role"] == "user" and entry["role"] == "user":

                def _blocks(c):
                    if isinstance(c, list):
                        return list(c)
                    return [{"type": "text", "text": c}] if c else []

                prev, cur = out[-1].get("content"), entry.get("content")
                if isinstance(prev, str) and isinstance(cur, str):
                    # Both sides are plain text: keep the string representation instead of needlessly converting to block lists.
                    out[-1]["content"] = "\n\n".join(x for x in (prev, cur) if x)
                else:
                    out[-1]["content"] = _blocks(prev) + _blocks(cur)
                continue

            out.append(entry)
        return out

    def _wire_tool_name(self, name: str) -> str:
        return to_wire_tool_name(name) if self.sanitize_tool_dots else name

    def _wire_tools(self, tools: list[ToolSchema]) -> list[ToolSchema]:
        if not self.sanitize_tool_dots:
            return list(tools)
        return [dataclasses.replace(tool, name=to_wire_tool_name(tool.name)) for tool in tools]

    def _tool_name_map(self, req: LLMRequest) -> ToolNameMap:
        return ToolNameMap.for_request(req) if self.sanitize_tool_dots else ToolNameMap(())

    @staticmethod
    def _convert_tool(tool: ToolSchema) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                # Retain the legacy OpenAI/Azure endpoint compatibility bound
                # in characters, not tokens. This is not a universal model limit.
                "description": tool.description[:_OPENAI_TOOL_DESCRIPTION_MAX_CHARS],
                "parameters": _normalize_openai_tool_parameters(
                    tool.input_schema,
                ),
            },
        }

    @staticmethod
    def _convert_tool_flat(tool: ToolSchema) -> dict[str, Any]:
        """Flatten Responses tools with the same legacy description compatibility bound."""

        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description[:_OPENAI_TOOL_DESCRIPTION_MAX_CHARS],
            "parameters": _normalize_openai_tool_parameters(tool.input_schema),
        }

    def _convert_input_items(
        self,
        messages: list[Message],
        *,
        has_instructions: bool,
    ) -> list[dict[str, Any]]:
        """chat 形状的 Message 列表 → Responses `input` item 数组（tech-design §8.5）。

        - 工具结果不是 role=tool 的 message，是 function_call_output item，配对用 call_id；
          `name` 字段在 item 上无处安放，静默丢弃
        - assistant.tool_calls 逐个翻成 function_call item——只带 call_id 不带 id
          （两家真端点实测被收，samples/*-request.json）；正文非空先出 message item
        - system：`req.system` 已走顶层 instructions；messages 里的 system item 除
          「首位且无 instructions」外一律**降级为 user**（内容与位置保留）——DashScope
          /responses 实测 400「no more than one system message… must be the first」，
          Preserve later system-message content and position as user data rather than dropping it.
        - 相邻 user 合并与 `_convert_messages` 同理由同款（出线恒定严格交替，不赌
          provider 宽容度）；system 降级产生的新相邻 user 也进同一合并
        """

        out: list[dict[str, Any]] = []

        def _append_message(entry: dict[str, Any]) -> None:
            if out and out[-1].get("role") == "user" and entry.get("role") == "user":

                def _blocks(c):
                    if isinstance(c, list):
                        return list(c)
                    return [{"type": "input_text", "text": c}] if c else []

                prev, cur = out[-1].get("content"), entry.get("content")
                if isinstance(prev, str) and isinstance(cur, str):
                    out[-1]["content"] = "\n\n".join(x for x in (prev, cur) if x)
                else:
                    out[-1]["content"] = _blocks(prev) + _blocks(cur)
                return
            out.append(entry)

        for m in messages:
            if m.role == "tool":
                if not m.tool_call_id:
                    raise OpenAICompatError(
                        kind=ProviderFailureKind.CONFIGURATION,
                        retryable=False,
                        provider_family=self.adapter_type,
                    )
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id,
                        "output": m.content or "",
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                if m.content:
                    _append_message({"role": "assistant", "content": m.content})
                for tc in m.tool_calls:
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": self._wire_tool_name(tc.name),
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        }
                    )
                continue
            role: str = m.role
            if role == "system" and (out or has_instructions):
                role = "user"
            entry: dict[str, Any] = {"role": role}
            if m.role == "user" and m.images:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "input_text", "text": m.content})
                for img in m.images:
                    # ⚠ Responses image_url is a **string**, not an object (unlike the chat surface);
                    # this is the documented shape, with no real-endpoint sample (tech-design §8.5).
                    blocks.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{img.mime};base64,{img.base64}",
                        }
                    )
                entry["content"] = blocks
            else:
                entry["content"] = m.content or ""
            _append_message(entry)
        return out

    # ─────────────────────────────────────────────────────────
    # Response parsing
    # ─────────────────────────────────────────────────────────
    def _parse_response(self, data: dict[str, Any], req: LLMRequest) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise OpenAICompatError(
                kind=ProviderFailureKind.RESPONSE_INVALID,
                retryable=False,
                provider_family=self.adapter_type,
            )

        choice = choices[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or ""
        raw_tool_calls = msg.get("tool_calls") or []

        tool_names = self._tool_name_map(req)
        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or "",
                    name=tool_names.to_canonical(str(fn.get("name") or "")),
                    arguments=self._parse_tool_call_arguments(
                        fn.get("arguments", "{}"),
                        fn.get("name"),
                    ),
                )
            )

        usage_raw = data.get("usage") or {}
        cached = 0
        details = usage_raw.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0) or 0)
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            cached_tokens=cached,
        )

        finish = _FINISH_REASON_MAP.get(choice.get("finish_reason", "stop"), "stop")

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,  # type: ignore[arg-type]
            usage=usage,
            raw=data,
            provider=self.provider_name,
            model=data.get("model", req.model),
            diagnostics=LLMResponseDiagnostics(
                transport_mode="invoke",
                upstream_tool_call_count=len(raw_tool_calls),
                upstream_finish_reason=choice.get("finish_reason"),
                legacy_function_call=isinstance(msg.get("function_call"), dict),
                reasoning_tokens=_reasoning_tokens(usage_raw, "completion_tokens_details"),
            ),
        )

    def _parse_tool_call_arguments(self, raw_args: Any, tool_name: Any) -> dict[str, Any]:
        """arguments JSON 反解 + 失败兜底 —— invoke 两面共用（语义与原 chat inline 版逐字一致）。

        Qwen 某些场景下 arguments 可能不是严格 JSON，保底传 raw 字符串。"""

        try:
            args = loads_tool_arguments(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            logger.warning(
                "openai_compat.tool_args_not_json provider=%s tool=%s args=%r",
                self.provider_name,
                tool_name,
                str(raw_args)[:200],  # Model output may embed user content; truncate under the same sanitization rules (review R4).
            )
            args = {"_raw": raw_args}
        return args if isinstance(args, dict) else {"_value": args}

    def _responses_stream_chunks(
        self,
        obj: dict[str, Any],
        state: _ResponsesStreamState,
        *,
        durable_usage: bool,
    ) -> list[LLMChunk]:
        """单帧 Responses 事件 → LLMChunk 列表（tech-design §9.2 的映射表，逐行对应）。

        真流样本：docs/sprints/responses-api-2026-08/samples/dashscope-stream*.txt /
        deepseek-stream.txt。未知事件 type 静默跳过（厂商方言：DashScope 的 reasoning
        事件名就与官方不同）。错误帧直接 raise —— 框架的异常路径（fail_physical /
        重试判定）与 chat 面共用。
        """

        etype = obj.get("type")
        if etype is None:
            # Bare error bodies (no type): DashScope gateway format {request_id, code, message}
            # (the same family as dashscope-toolreturn.json) or standard {"error": {…}}.
            if obj.get("error") is not None or (obj.get("code") is not None and obj.get("message") is not None):
                kind = failure_kind_for_error_payload(obj)
                raise OpenAICompatError(
                    kind=kind,
                    retryable=stream_failure_kind_allows_fallback(kind),
                    provider_family=self.adapter_type,
                    **upstream_error_fields_from_payload(obj),
                )
            return []
        if etype == "response.output_text.delta":
            delta = str(obj.get("delta") or "")
            return [LLMChunk(delta=delta)] if delta else []
        if etype in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            # The first is a DashScope dialect, the second the official name; route both to the thinking stream.
            delta = str(obj.get("delta") or "")
            return [LLMChunk(thinking_delta=delta)] if delta else []
        if etype == "response.refusal.delta":
            delta = str(obj.get("delta") or "")
            return [LLMChunk(delta=delta)] if delta else []
        if etype == "response.output_item.added":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                item_id = str(item.get("id") or f"item_{obj.get('output_index', 0)}")
                state.fc_items[item_id] = {
                    "call_id": str(item.get("call_id") or ""),
                    "name": str(item.get("name") or ""),
                    "args_buf": str(item.get("arguments") or ""),
                }
            return []
        if etype == "response.function_call_arguments.delta":
            item_id = str(obj.get("item_id") or "")
            slot = state.fc_items.setdefault(
                item_id,
                {"call_id": "", "name": "", "args_buf": ""},
            )
            partial = str(obj.get("delta") or "")
            if not partial:
                return []
            slot["args_buf"] += partial
            if slot["name"]:
                # Same gate as chat: emit partials only once name is known (consumed by the lead_thinking_writing UX).
                return [
                    LLMChunk(
                        tool_call_partial={
                            "tool_name": slot["name"],
                            "tool_call_id": slot["call_id"] or item_id,
                            "partial_args_json": partial,
                        }
                    )
                ]
            return []
        if etype == "response.output_item.done":
            item = obj.get("item")
            if not (isinstance(item, dict) and item.get("type") == "function_call"):
                return []
            item_id = str(item.get("id") or "")
            slot = state.fc_items.pop(item_id, {"call_id": "", "name": "", "args_buf": ""})
            call_id = str(item.get("call_id") or slot["call_id"] or item_id)
            name = str(item.get("name") or slot["name"])
            if state.tool_names is not None:
                name = state.tool_names.to_canonical(name)
            # Use truthiness: if the done frame explicitly supplies an empty string, use the accumulated buffer (review R1-P2-3).
            # Using is not None would let the empty string overwrite streamed arguments and execute the tool with {}.
            raw_args = item.get("arguments") or slot["args_buf"]
            try:
                args = loads_tool_arguments(raw_args) if raw_args else {}
            except json.JSONDecodeError as _je:
                buf = str(raw_args)
                logger.warning(
                    "openai_compat.responses.tool_args_invalid_json call_id=%s name=%s buf_len=%d err=%s pos=%s",
                    call_id,
                    name,
                    len(buf),
                    _je.msg,
                    _je.pos,
                )
                # Match chat flush semantics: do not swallow truncated/invalid arguments as {}; use the _args_parse_error channel
                # so the LLM receives an accurate diagnosis and can repair and resend.
                # item.status=="incomplete" is definitive evidence that the item was truncated. The terminal frame has not arrived yet,
                # so state.finish_reason is still None (review R1-P2-7); preserve the length context.
                hint_finish = "length" if str(item.get("status") or "") == "incomplete" else state.finish_reason
                args = {
                    "_args_parse_error": args_parse_hint(_je, buf, hint_finish),
                    "_raw_args_preview": buf[:800],
                }
            if not isinstance(args, dict):
                args = {"_value": args}
            state.emitted_tool_calls += 1
            return [LLMChunk(tool_call_delta=ToolCall(id=call_id, name=name, arguments=args))]
        if etype in {"response.completed", "response.incomplete", "response.failed"}:
            resp_obj = obj.get("response") if isinstance(obj.get("response"), dict) else {}
            if etype == "response.failed":
                error = resp_obj.get("error") if isinstance(resp_obj.get("error"), dict) else None
                payload = {"error": error or {"message": "response.failed"}}
                kind = failure_kind_for_error_payload(payload)
                raise OpenAICompatError(
                    kind=kind,
                    retryable=stream_failure_kind_allows_fallback(kind),
                    provider_family=self.adapter_type,
                    **upstream_error_fields_from_payload(payload),
                )
            state.upstream_finish_reason = resp_obj.get("status") or etype.removeprefix("response.")
            usage_raw = resp_obj.get("usage")
            state.reasoning_tokens = _reasoning_tokens(usage_raw, "output_tokens_details")
            if durable_usage and isinstance(usage_raw, dict):
                details = usage_raw.get("input_tokens_details")
                cached_value = details.get("cached_tokens", 0) if isinstance(details, dict) else 0 if details is None else None
                state.usage = exact_authoritative_usage(
                    usage_raw,
                    input_key="input_tokens",
                    output_key="output_tokens",
                    cached_value=cached_value,
                )
                state.usage_input_seen = state.usage is not None
                state.usage_output_seen = state.usage is not None
            elif isinstance(usage_raw, dict) and usage_raw:
                state.usage_input_seen = "input_tokens" in usage_raw
                state.usage_output_seen = "output_tokens" in usage_raw
                details = usage_raw.get("input_tokens_details")
                cached = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
                state.usage = TokenUsage(
                    input_tokens=int(usage_raw.get("input_tokens") or 0),
                    output_tokens=int(usage_raw.get("output_tokens") or 0),
                    cached_tokens=cached,
                )
            if etype == "response.incomplete":
                # ⚠ Terminal state with no observed samples (tech-design §9.2); map defensively.
                details = resp_obj.get("incomplete_details")
                reason = str(details.get("reason") or "") if isinstance(details, dict) else ""
                state.incomplete_reason = reason or None
                state.finish_reason = "length" if reason == "max_output_tokens" else ("content_filter" if reason == "content_filter" else "stop")
            else:
                state.finish_reason = "tool_use" if state.emitted_tool_calls else "stop"
            state.terminal_seen = True
            return []
        if etype == "error":
            detail = {key: obj.get(key) for key in ("code", "message", "param") if obj.get(key) is not None}
            payload = {"error": detail or {"message": "stream error event"}}
            kind = failure_kind_for_error_payload(payload)
            raise OpenAICompatError(
                kind=kind,
                retryable=stream_failure_kind_allows_fallback(kind),
                provider_family=self.adapter_type,
                **upstream_error_fields_from_payload(payload),
            )
        # created / in_progress / content_part.* / output_text.done / reasoning terminal frames
        # / summary_part.*, etc.: no incremental semantics; skip silently (also applies to unknown dialects).
        return []

    def _parse_responses_payload(self, data: dict[str, Any], req: LLMRequest) -> LLMResponse:
        """Responses 面响应解析：`output[]` 有类型 item 流（tech-design §8.6；样本
        docs/sprints/responses-api-2026-08/samples/）。

        - message item 可能不止一个（DeepSeek 在 function_call 前出 commentary），按序拼接
        - 工具配对用 `call_id`（`id` 另有其字段：DashScope 给 msg_ 前缀、DeepSeek 裸 UUID）
        - reasoning item 取 `summary[]` 中 `type=="summary_text"` block 的 **`text`** 键
          ⇒ thinking（`summary_text` 是 type 的值不是键名——样本对拍逮过一次措辞错）
        - 键可缺、可 null 双防：DashScope 省略未设键、DeepSeek 置 null
        - 未知 item type / 未知字段（`phase`、`x_details`）静默容忍
        """

        output = data.get("output")
        # Empty output is invalid, just like missing output or empty chat choices. Silently accepting an empty response
        # feeds the production failures that falsely report success with empty payloads; RESPONSE_INVALID allows fallback.
        if not isinstance(output, list) or not output:
            raise OpenAICompatError(
                kind=ProviderFailureKind.RESPONSE_INVALID,
                retryable=False,
                provider_family=self.adapter_type,
            )
        item_texts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                joined = "".join(
                    str(block.get("text") or block.get("refusal") or "")
                    for block in (item.get("content") or [])
                    if isinstance(block, dict) and block.get("type") in {"output_text", "refusal"}
                )
                if joined:
                    item_texts.append(joined)
            elif itype == "function_call":
                name = self._tool_name_map(req).to_canonical(str(item.get("name") or ""))
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=name,
                        arguments=self._parse_tool_call_arguments(item.get("arguments", "{}"), name),
                    )
                )
            elif itype == "reasoning":
                for block in item.get("summary") or []:
                    if isinstance(block, dict) and block.get("type") == "summary_text" and block.get("text"):
                        thinking_parts.append(str(block["text"]))
        status = str(data.get("status") or "completed")
        if not item_texts and not tool_calls and not thinking_parts and status not in {"incomplete", "failed"}:
            # A "completed" response with nonempty output containing only garbage/unknown items is invalid, just like an empty list
            # (review R1-P2-5: this feeds failures that falsely report success with empty payloads; raising for empty lists
            # but not nonempty garbage leaves a gap). Empty output on incomplete/failed is honest and allowed.
            raise OpenAICompatError(
                kind=ProviderFailureKind.RESPONSE_INVALID,
                retryable=False,
                provider_family=self.adapter_type,
            )
        usage_raw = data.get("usage")
        cached = 0
        if isinstance(usage_raw, dict):
            details = usage_raw.get("input_tokens_details")
            if isinstance(details, dict):
                cached = int(details.get("cached_tokens", 0) or 0)
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0) or 0) if isinstance(usage_raw, dict) else 0,
            output_tokens=int(usage_raw.get("output_tokens", 0) or 0) if isinstance(usage_raw, dict) else 0,
            cached_tokens=cached,
        )
        incomplete = data.get("incomplete_details")
        incomplete_reason = str(incomplete.get("reason") or "") if isinstance(incomplete, dict) else ""
        finish: FinishReason
        if status == "incomplete":
            # incomplete must override tool_use (review R1-P2-2): reporting a function_call with truncated arguments
            # as tool_use makes downstream execute garbage arguments and bypass length handling. On chat,
            # the provider returns finish_reason="length" in this case; both surfaces must have the same semantics.
            # ⚠ Branch with no observed samples (only completed/queued/in_progress were sampled); map defensively.
            finish = "length" if incomplete_reason == "max_output_tokens" else ("content_filter" if incomplete_reason == "content_filter" else "stop")
        elif tool_calls:
            finish = "tool_use"
        elif status == "failed":
            finish = "error"
        else:
            finish = "stop"
        return LLMResponse(
            content="\n\n".join(item_texts),
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            raw=data,
            provider=self.provider_name,
            model=str(data.get("model") or req.model),
            thinking="\n\n".join(thinking_parts),
            diagnostics=LLMResponseDiagnostics(
                transport_mode="invoke",
                upstream_tool_call_count=sum(isinstance(item, dict) and item.get("type") == "function_call" for item in output),
                upstream_finish_reason=status,
                incomplete_reason=incomplete_reason or None,
                reasoning_tokens=_reasoning_tokens(usage_raw, "output_tokens_details"),
            ),
        )

    # ─────────────────────────────────────────────────────────
    # Error mapping
    # ─────────────────────────────────────────────────────────
    def _raise_for_status(self, resp: httpx2.Response) -> None:
        if 200 <= resp.status_code < 300:
            return

        status_code = resp.status_code
        raise OpenAICompatError(
            kind=failure_kind_for_status(status_code),
            retryable=status_code in (408, 429) or status_code >= 500,
            status_code=status_code,
            provider_family=self.adapter_type,
            **upstream_error_fields(resp),
        )


# ─────────────────────────────────────────────────────────────
# Protocol conformance check
# ─────────────────────────────────────────────────────────────
_: type[LLMAdapter] = OpenAICompatibleAdapter  # type: ignore[assignment,type-abstract]


def _truncate_json(obj: Any, max_len: int = 300) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "..."
