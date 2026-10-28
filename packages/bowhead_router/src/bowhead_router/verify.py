"""Provider connection verification (WS4 — models-settings test-connection).

Probes an team-supplied LLM endpoint so the settings UI can validate a base_url +
key BEFORE persisting it, and so key rotation can record a ``verified_by_ping``
flag. Every probe goes through the SSRF-guarded transport
(:func:`bowhead_router.ssrf_guard.build_guarded_async_client`), which
re-resolves and pins the target IP per request — closing the DNS-rebind TOCTOU
window that a plain client (validate-once-then-connect) would leave open.

Protocol probes mirror the real adapters:
  - ``openai_compatible``    → ``GET  {base_url}/models``          (catalog listing)
  - ``anthropic_compatible`` → ``POST {base_url}/v1/messages`` with ``max_tokens: 1``

The result maps to the four brief outcomes (连接正常 / 密钥无效 / 地址不可达 /
目录为空) plus a few finer signals the frontend can surface.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.errors import (
    ProviderCallError,
    ProviderFailureKind,
    stream_error_allows_invoke_fallback,
)
from bowhead_router.ssrf_guard import SsrfBlocked, build_guarded_async_client
from bowhead_router.transport import (
    build_provider_transport_plan,
    materialize_transport_headers,
)
from bowhead_router.types import (
    ImageRef,
    LLMRequest,
    Message,
    ToolSchema,
)

logger = logging.getLogger(__name__)

# A "test connection" button must not hang the UI; keep the probe short.
_DEFAULT_TIMEOUT_SEC = 10
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
_TOOL_PROBE_MAX_TOKENS = 128
_IMAGE_PROBE_TIMEOUT_SEC = 60
_WEB_SEARCH_PROBE_TIMEOUT_SEC = 45
_IMAGE_POLL_INTERVAL_SEC = 2.5
_VISION_PROBE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGP8//8/AymAiSTVDKMaiANMRKqDg1ENxACSNQAAVW0DHUvDbUgAAAAASUVORK5CYII="
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_PROBED_CAPABILITIES = frozenset(
    {"chat", "tool", "vision", "embed", "rerank", "image_gen", "web_search"},
)
# httpx2 bodies can be large; only echo a short slice into audit/error messages.
_SNIPPET_MAX = 200
_DISCOVERY_LIMIT = 1000
_DISCOVERY_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_DISCOVERY_METADATA_FIELDS = frozenset({"owned_by", "object", "created", "display_name"})
_DISCOVERY_METADATA_VALUE_MAX = 200


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a single provider probe.

    ``code`` is a stable machine token the frontend maps to a localized string:
      ok | unauthorized | unreachable | blocked | empty_catalog
      | model_required | model_not_found | bad_request | error
    """

    ok: bool
    code: str
    http_status: int | None = None
    message: str = ""
    model_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "http_status": self.http_status,
            "message": self.message,
            "model_count": self.model_count,
        }


@dataclass(frozen=True)
class VerifyStep:
    """One connection or declared-capability probe result."""

    step: str
    ok: bool
    code: str
    status: str = "passed"
    model: str | None = None
    capability: str | None = None
    phase: str | None = None
    http_status: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "ok": self.ok,
            "code": self.code,
            "status": self.status,
            "model": self.model,
            "capability": self.capability,
            "phase": self.phase,
            "http_status": self.http_status,
            "message": self.message,
        }


async def _probe(
    method: str,
    path: str,
    *,
    base_url: str,
    headers: dict[str, str],
    timeout_sec: int,
    json_body: dict[str, Any] | None = None,
) -> tuple[httpx2.Response | None, VerifyResult | None]:
    """Run one guarded request. Returns ``(response, None)`` on any HTTP reply, or
    ``(None, VerifyResult)`` when the transport failed before a reply (SSRF block,
    timeout, DNS/connection error). The probe never raises into the caller."""
    try:
        async with build_guarded_async_client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_sec,
            headers=headers,
        ) as client:
            resp = await client.request(method, path, json=json_body)
        return resp, None
    except SsrfBlocked:
        return None, VerifyResult(False, "blocked", message="地址被安全策略拦截")
    except httpx2.TimeoutException:
        return None, VerifyResult(False, "unreachable", message="连接超时")
    except (httpx2.HTTPError, OSError) as e:
        # httpx2.ConnectError / ssl errors (HTTPError subclasses) + socket.gaierror
        # (OSError, raised by the guard's resolver on DNS failure).
        return None, VerifyResult(False, "unreachable", message=f"地址不可达 ({type(e).__name__})")
    except Exception as e:  # a probe of arbitrary team input must not 500 the endpoint
        logger.warning("verify_provider unexpected transport error: %r", e)
        return None, VerifyResult(False, "error", message=f"未知错误 ({type(e).__name__})")


def _classify_openai(resp: httpx2.Response) -> VerifyResult:
    sc = resp.status_code
    if sc in (401, 403):
        return VerifyResult(False, "unauthorized", http_status=sc, message="密钥无效")
    if sc >= 400:
        return VerifyResult(False, "error", http_status=sc, message="连接测试失败")
    # 2xx — inspect the catalog listing.
    try:
        data: Any = resp.json()
    except ValueError:
        # Reachable + authenticated, but the body is not JSON we recognize. The
        # connection itself succeeded, so don't fail the user on shape alone.
        return VerifyResult(True, "ok", http_status=sc, message="连接正常")
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, list):
        if not items:
            return VerifyResult(False, "empty_catalog", http_status=sc, message="模型目录为空")
        return VerifyResult(True, "ok", http_status=sc, message="连接正常", model_count=len(items))
    return VerifyResult(True, "ok", http_status=sc, message="连接正常")


def _classify_anthropic(resp: httpx2.Response) -> VerifyResult:
    sc = resp.status_code
    if sc in (401, 403):
        return VerifyResult(False, "unauthorized", http_status=sc, message="密钥无效")
    if sc < 400:
        return VerifyResult(True, "ok", http_status=sc, message="连接正常")
    if sc == 429:
        # Rate-limited means the key authenticated and the endpoint is live.
        return VerifyResult(True, "ok", http_status=sc, message="连接正常 (被限流)")
    if sc == 404:
        # Got past auth (would be 401 otherwise) but the model id isn't served here.
        return VerifyResult(False, "model_not_found", http_status=sc, message="model 不存在 (连接与密钥正常)")
    if sc == 400:
        return VerifyResult(False, "bad_request", http_status=sc, message="请求有误")
    return VerifyResult(False, "error", http_status=sc, message="连接测试失败")


async def verify_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str | None = None,
    anthropic_version: str = _DEFAULT_ANTHROPIC_VERSION,
    messages_path: str | None = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> VerifyResult:
    """Probe ``base_url`` with ``api_key`` and report whether the connection works.

    Args:
        provider_type: ``openai_compatible`` or ``anthropic_compatible``.
        base_url: provider endpoint (openai-compat usually includes ``/v1``;
            anthropic-compat is the host root, ``/v1/messages`` is appended).
        api_key: candidate key (plaintext). Never logged; caller audits a fingerprint.
        model: required for anthropic (the minimal completion needs a model id);
            ignored for openai (the catalog listing needs none).
    """
    ptype = (provider_type or "").strip()
    if not isinstance(base_url, str) or not base_url:
        return VerifyResult(False, "unreachable", message="缺少接入地址")
    if not isinstance(api_key, str) or not api_key:
        return VerifyResult(False, "unauthorized", message="缺少密钥")

    if ptype == "openai_compatible":
        plan = build_provider_transport_plan(
            {
                "type": ptype,
                "base_url": base_url,
                "api_key": api_key,
                "timeout_sec": timeout_sec,
            },
            purpose="discovery",
        )
        resp, err = await _probe(
            plan.method,
            plan.request_url,
            base_url=base_url,
            headers=materialize_transport_headers(plan),
            timeout_sec=timeout_sec,
        )
        return err if err is not None else _classify_openai(resp)  # type: ignore[arg-type]

    if ptype == "anthropic_compatible":
        if not model:
            return VerifyResult(
                False,
                "model_required",
                message="anthropic 兼容协议测试连接需要指定一个 model",
            )
        plan = build_provider_transport_plan(
            {
                "type": ptype,
                "base_url": base_url,
                "api_key": api_key,
                "timeout_sec": timeout_sec,
                "extra": {
                    "anthropic_version": anthropic_version or _DEFAULT_ANTHROPIC_VERSION,
                    "messages_path": messages_path or "/v1/messages",
                },
            },
            purpose="probe_chat",
        )
        body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
        # honor the provider's configured messages path (some anthropic-compat proxies,
        # e.g. deepseek's legacy alias, use "/messages" instead of "/v1/messages").
        resp, err = await _probe(
            plan.method,
            plan.request_url,
            base_url=base_url,
            headers=materialize_transport_headers(plan),
            json_body=body,
            timeout_sec=timeout_sec,
        )
        return err if err is not None else _classify_anthropic(resp)  # type: ignore[arg-type]

    return VerifyResult(False, "error", message=f"不支持的协议类型: {ptype!r}")


async def verify_provider_capabilities(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    models: list[dict[str, Any]],
    anthropic_version: str = _DEFAULT_ANTHROPIC_VERSION,
    messages_path: str | None = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Run real capability probes without requiring a provider model catalog."""
    steps: list[VerifyStep] = []

    ptype = provider_type.strip()
    for item in models:
        model = str(item.get("id") or "").strip()
        capabilities = item.get("capabilities")
        capability_protocol = (
            str(
                item.get("capability_protocol") or item.get("protocol") or "",
            ).strip()
            or None
        )
        if not model or not isinstance(capabilities, list):
            continue
        for capability in capabilities:
            if not isinstance(capability, str):
                continue
            result = await _probe_capability(
                provider_type=ptype,
                base_url=base_url,
                api_key=api_key,
                model=model,
                capability=capability,
                capability_protocol=capability_protocol,
                anthropic_version=anthropic_version,
                messages_path=messages_path,
                timeout_sec=timeout_sec,
            )
            steps.append(
                VerifyStep(
                    step="capability",
                    model=model,
                    capability=capability,
                    ok=result.ok,
                    code=result.code,
                    status=result.status,
                    phase=result.phase,
                    http_status=result.http_status,
                    message=result.message,
                )
            )
    failed = any(step.status == "failed" for step in steps)
    degraded = any(step.status == "degraded" for step in steps)
    return {
        "ok": not failed and not degraded,
        "code": "capability_failed" if failed else "capability_degraded" if degraded else "ok",
        "steps": [step.as_dict() for step in steps],
    }


async def discover_provider_models(*, base_url: str, api_key: str, timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Fetch OpenAI-compatible ``/models`` strictly for optional form autofill."""
    plan = build_provider_transport_plan(
        {
            "type": "openai_compatible",
            "base_url": base_url,
            "api_key": api_key,
            "timeout_sec": timeout_sec,
        },
        purpose="discovery",
    )
    resp, error = await _probe(
        plan.method,
        plan.request_url,
        base_url=base_url,
        headers=materialize_transport_headers(plan),
        timeout_sec=timeout_sec,
    )
    if error is not None:
        return error.as_dict() | {"models": []}
    classified = _classify_openai(resp)  # type: ignore[arg-type]
    if not classified.ok:
        return classified.as_dict() | {"models": []}
    try:
        payload: Any = resp.json()  # type: ignore[union-attr]
        items = payload.get("data") if isinstance(payload, dict) else payload
        models = (
            [item.get("id") for item in items if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()]
            if isinstance(items, list)
            else []
        )
    except ValueError:
        models = []
    return classified.as_dict() | {"models": models}


@dataclass(frozen=True)
class _CapabilityResult:
    status: str
    code: str
    phase: str
    http_status: int | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"


def _discovery_transport_error(exc: Exception) -> str:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return "discovery_tls_error"
        if isinstance(cause, socket.gaierror):
            return "discovery_dns_error"
        cause = cause.__cause__
    return "discovery_tls_error" if "ssl" in str(exc).lower() else "discovery_dns_error"


def _bounded_discovery_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in _DISCOVERY_METADATA_FIELDS:
        value = item.get(key)
        if isinstance(value, bool | int | float):
            metadata[key] = value
        elif isinstance(value, str):
            metadata[key] = value[:_DISCOVERY_METADATA_VALUE_MAX]
    return metadata


async def discover_provider_model_candidates(
    *,
    base_url: str,
    api_key: str,
    default_headers: dict[str, str] | None = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    candidate_limit: int = _DISCOVERY_LIMIT,
    response_max_bytes: int = _DISCOVERY_RESPONSE_MAX_BYTES,
) -> dict[str, Any]:
    """Run exactly one bounded OpenAI-compatible catalog request for V2."""

    plan = build_provider_transport_plan(
        {
            "type": "openai_compatible",
            "base_url": base_url,
            "api_key": api_key,
            "default_headers": default_headers or {},
            "timeout_sec": timeout_sec,
        },
        purpose="discovery",
    )
    try:
        async with (
            build_guarded_async_client(
                timeout=timeout_sec,
                headers=materialize_transport_headers(plan),
            ) as client,
            client.stream(plan.method, plan.request_url) as response,
        ):
            status = response.status_code
            if status in {401, 403}:
                return {
                    "ok": False,
                    "code": "discovery_auth_failed",
                    "http_status": status,
                }
            if status == 404:
                return {
                    "ok": False,
                    "code": "discovery_endpoint_not_found",
                    "http_status": status,
                }
            if status == 429:
                return {
                    "ok": False,
                    "code": "discovery_rate_limited",
                    "http_status": status,
                }
            if status >= 500:
                return {
                    "ok": False,
                    "code": "discovery_invalid_response",
                    "http_status": status,
                }
            if status >= 400:
                return {
                    "ok": False,
                    "code": "discovery_invalid_response",
                    "http_status": status,
                }
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > response_max_bytes:
                    return {
                        "ok": False,
                        "code": "discovery_response_too_large",
                        "http_status": status,
                    }
    except SsrfBlocked:
        return {
            "ok": False,
            "code": "discovery_blocked_by_security_policy",
            "http_status": None,
        }
    except httpx2.TimeoutException:
        return {
            "ok": False,
            "code": "discovery_timeout",
            "http_status": None,
        }
    except httpx2.ConnectError as exc:
        return {
            "ok": False,
            "code": _discovery_transport_error(exc),
            "http_status": None,
        }
    except httpx2.HTTPError:
        return {
            "ok": False,
            "code": "discovery_invalid_response",
            "http_status": None,
        }

    try:
        payload: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "code": "discovery_invalid_response",
            "http_status": status,
        }
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return {
            "ok": False,
            "code": "discovery_invalid_response",
            "http_status": status,
        }

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    valid_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not (normalized := model_id.strip()):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        valid_count += 1
        if len(candidates) < candidate_limit:
            candidates.append(
                {
                    "model_id": normalized,
                    "upstream_metadata": _bounded_discovery_metadata(item),
                }
            )
    return {
        "ok": True,
        "code": "ok",
        "http_status": status,
        "candidates": candidates,
        "returned_count": len(candidates),
        "truncated": valid_count > candidate_limit,
        "limit": candidate_limit,
    }


async def _probe_capability(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    capability: str,
    capability_protocol: str | None,
    anthropic_version: str,
    messages_path: str | None,
    timeout_sec: int,
) -> _CapabilityResult:
    if capability not in _PROBED_CAPABILITIES:
        return _CapabilityResult("failed", "probe_not_supported", "prepare", message="该能力尚无此协议的验证器")
    if capability in {"rerank", "image_gen", "web_search"}:
        if provider_type != "openai_compatible":
            return _CapabilityResult(
                "failed",
                "probe_not_supported",
                "prepare",
                message="该能力仅支持 OpenAI 兼容模型来源验证",
            )
        result = await _probe_special_capability(
            base_url=base_url,
            api_key=api_key,
            model=model,
            capability=capability,
            capability_protocol=capability_protocol,
            timeout_sec=timeout_sec,
        )
        return _capability_from_verify_result(result, phase=capability)
    if provider_type == "anthropic_compatible" and capability == "embed":
        return _CapabilityResult(
            "failed",
            "probe_not_supported",
            "prepare",
            message="Anthropic 兼容协议不支持嵌入验证",
        )

    if capability == "embed":
        if provider_type != "openai_compatible":
            return _CapabilityResult("failed", "probe_not_supported", "prepare", message="该协议不支持能力验证")
        response, error = await _probe(
            "POST",
            "/embeddings",
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            json_body={"model": model, "input": "bowhead connection probe"},
            timeout_sec=timeout_sec,
        )
        if error is not None:
            return _capability_transport_error(error, phase="embed")
        assert response is not None
        if response.status_code in (401, 403):
            return _CapabilityResult("failed", "unauthorized", "embed", response.status_code, "密钥无效")
        if response.status_code >= 400:
            return _CapabilityResult("failed", "error", "embed", response.status_code, "嵌入请求失败")
        try:
            payload = response.json()
        except ValueError:
            return _CapabilityResult("failed", "invalid_response", "embed", response.status_code, "响应不是有效 JSON")
        if not _has_embedding(payload):
            return _CapabilityResult("failed", "embedding_missing", "embed", response.status_code, "响应不含数值向量")
        return _CapabilityResult("passed", "ok", "embed", response.status_code, "验证通过")

    try:
        adapter: OpenAICompatibleAdapter | AnthropicCompatibleAdapter
        if provider_type == "openai_compatible":
            adapter = OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=api_key,
                provider_name="settings_probe",
                timeout_sec=timeout_sec,
                ssrf_guard=True,
                client_builder=build_guarded_async_client,
            )
        elif provider_type == "anthropic_compatible":
            adapter = AnthropicCompatibleAdapter(
                base_url=base_url,
                api_key=api_key,
                provider_name="settings_probe",
                timeout_sec=timeout_sec,
                anthropic_version=anthropic_version or _DEFAULT_ANTHROPIC_VERSION,
                messages_path=messages_path or "/v1/messages",
                ssrf_guard=True,
                client_builder=build_guarded_async_client,
            )
        else:
            return _CapabilityResult("failed", "probe_not_supported", "prepare", message="该协议不支持能力验证")
    except (httpx2.InvalidURL, ValueError):
        return _CapabilityResult(
            "failed",
            "bad_request",
            "prepare",
            message="模型服务地址无效",
        )

    try:
        request = _runtime_probe_request(model, capability)
        if capability == "tool":
            try:
                selection = await adapter.invoke(request)
            except Exception as exc:
                return _capability_exception(exc, phase="tool_selection")
            if not selection.tool_calls:
                return _CapabilityResult("failed", "tool_call_missing", "tool_selection", message="模型未返回工具调用")
            call = selection.tool_calls[0]
            request = LLMRequest(
                model=model,
                messages=[
                    *request.messages,
                    Message(
                        role="assistant",
                        content=selection.content,
                        tool_calls=[call],
                    ),
                    Message(
                        role="tool",
                        content='{"ok":true}',
                        name=call.name,
                        tool_call_id=call.id,
                    ),
                ],
                tools=request.tools,
                temperature=0.0,
                max_output_tokens=16,
            )
            phase = "tool_continuation"
        else:
            phase = "stream"

        try:
            await _drain_runtime_stream(adapter, request)
        except Exception as stream_error:
            if not stream_error_allows_invoke_fallback(stream_error):
                return _capability_exception(stream_error, phase=phase)
            try:
                await adapter.invoke(request)
            except Exception as invoke_error:
                return _capability_exception(invoke_error, phase=phase)
            return _CapabilityResult(
                "degraded",
                "stream_degraded",
                phase,
                getattr(stream_error, "status_code", None),
                "非流式调用可用，但流式调用失败",
            )
        return _CapabilityResult("passed", "ok", phase, 200, "验证通过")
    finally:
        await adapter.close()


def _capability_from_verify_result(
    result: VerifyResult,
    *,
    phase: str,
) -> _CapabilityResult:
    return _CapabilityResult(
        "passed" if result.ok else "failed",
        result.code,
        phase,
        result.http_status,
        result.message,
    )


async def _probe_special_capability(
    *,
    base_url: str,
    api_key: str,
    model: str,
    capability: str,
    capability_protocol: str | None,
    timeout_sec: int,
) -> VerifyResult:
    if capability == "rerank":
        return await _probe_rerank(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=capability_protocol or "openai_compat",
            timeout_sec=timeout_sec,
        )
    if capability == "image_gen":
        return await _probe_image_generation(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=capability_protocol or "dashscope_async",
            timeout_sec=max(timeout_sec, _IMAGE_PROBE_TIMEOUT_SEC),
        )
    return await _probe_web_search(
        base_url=base_url,
        api_key=api_key,
        model=model,
        protocol=capability_protocol or "dashscope_compat",
        # Real search is materially slower than a minimal chat completion because
        # the upstream service also has to retrieve and synthesize sources. Keep
        # the ordinary connection probe short while avoiding a false
        # "unreachable" result for a healthy search model.
        timeout_sec=max(timeout_sec, _WEB_SEARCH_PROBE_TIMEOUT_SEC),
    )


async def _probe_rerank(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    timeout_sec: int,
) -> VerifyResult:
    if protocol == "dashscope_native":
        path = "/services/rerank/text-rerank/text-rerank"
        body = {
            "model": model,
            "input": {
                "query": "Bowhead capability probe",
                "documents": [
                    "Bowhead capability probe",
                    "An unrelated document",
                ],
            },
            "parameters": {"top_n": 1, "return_documents": False},
        }
    elif protocol == "openai_compat":
        path = "/rerank"
        body = {
            "model": model,
            "query": "Bowhead capability probe",
            "documents": [
                "Bowhead capability probe",
                "An unrelated document",
            ],
            "top_n": 1,
        }
    else:
        return _unsupported_capability_protocol("rerank", protocol)

    response, error = await _probe(
        "POST",
        path,
        base_url=base_url,
        headers=_bearer_headers(api_key),
        json_body=body,
        timeout_sec=timeout_sec,
    )
    if error is not None:
        return error
    assert response is not None
    payload, response_error = _json_capability_payload(response)
    if response_error is not None:
        return response_error
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) and isinstance(payload, dict):
        output = payload.get("output")
        results = output.get("results") if isinstance(output, dict) else None
    if not _has_rerank_result(results):
        return VerifyResult(
            False,
            "rerank_results_missing",
            http_status=response.status_code,
            message="重排响应不含有效 results",
        )
    return VerifyResult(True, "ok", http_status=response.status_code, message="验证通过")


async def _probe_image_generation(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    timeout_sec: int,
) -> VerifyResult:
    headers = _bearer_headers(api_key)
    if protocol == "dashscope_async":
        path = "/services/aigc/text2image/image-synthesis"
        headers["X-DashScope-Async"] = "enable"
        body = {
            "model": model,
            "input": {"prompt": "A small blue square, capability probe"},
            "parameters": {"size": "1328*1328", "n": 1},
        }
    elif protocol == "ark_sync":
        path = "/images/generations"
        body = {
            "model": model,
            "prompt": "A small blue square, capability probe",
            "size": "2048x2048",
            "response_format": "b64_json",
            "n": 1,
        }
    else:
        return _unsupported_capability_protocol("image_gen", protocol)

    response, error = await _probe(
        "POST",
        path,
        base_url=base_url,
        headers=headers,
        json_body=body,
        timeout_sec=timeout_sec,
    )
    if error is not None:
        return error
    assert response is not None
    payload, response_error = _json_capability_payload(response)
    if response_error is not None:
        return response_error
    if protocol == "dashscope_async":
        output = payload.get("output") if isinstance(payload, dict) else None
        task_id = str(output.get("task_id") or "").strip() if isinstance(output, dict) else ""
        if not task_id:
            return VerifyResult(
                False,
                "image_output_missing",
                http_status=response.status_code,
                message="图像生成响应不含 task_id",
            )
        return await _poll_dashscope_image_task(
            base_url=base_url,
            api_key=api_key,
            task_id=task_id,
            timeout_sec=timeout_sec,
        )

    data = payload.get("data") if isinstance(payload, dict) else None
    if not _has_generated_image(data):
        return VerifyResult(
            False,
            "image_output_missing",
            http_status=response.status_code,
            message="图像生成响应不含有效图片",
        )
    return VerifyResult(True, "ok", http_status=response.status_code, message="验证通过")


async def _poll_dashscope_image_task(
    *,
    base_url: str,
    api_key: str,
    task_id: str,
    timeout_sec: int,
) -> VerifyResult:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    path = f"/tasks/{quote(task_id, safe='')}"
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return VerifyResult(
                False,
                "image_generation_timeout",
                message="图像生成任务在验证时限内未完成",
            )
        response, error = await _probe(
            "GET",
            path,
            base_url=base_url,
            headers=_bearer_headers(api_key),
            timeout_sec=max(1, min(_DEFAULT_TIMEOUT_SEC, int(remaining))),
        )
        if error is not None:
            return error
        assert response is not None
        payload, response_error = _json_capability_payload(response)
        if response_error is not None:
            return response_error
        output = payload.get("output") if isinstance(payload, dict) else None
        if not isinstance(output, dict):
            return VerifyResult(
                False,
                "image_output_missing",
                http_status=response.status_code,
                message="图像生成任务响应不含 output",
            )
        status = str(output.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            results = output.get("results")
            if _has_https_image_url(results):
                return VerifyResult(
                    True,
                    "ok",
                    http_status=response.status_code,
                    message="验证通过",
                )
            return VerifyResult(
                False,
                "image_output_missing",
                http_status=response.status_code,
                message="图像生成任务完成但未返回图片",
            )
        if status in {"FAILED", "CANCELED", "CANCELLED"}:
            return VerifyResult(
                False,
                "image_generation_failed",
                http_status=response.status_code,
                message="图像生成任务失败",
            )
        if status not in {"", "PENDING", "RUNNING"}:
            return VerifyResult(
                False,
                "invalid_response",
                http_status=response.status_code,
                message="图像生成任务返回未知状态",
            )
        await asyncio.sleep(min(_IMAGE_POLL_INTERVAL_SEC, max(0.0, remaining)))


async def _probe_web_search(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    timeout_sec: int,
) -> VerifyResult:
    if protocol == "dashscope_compat":
        probe_base_url = base_url
        path = "/chat/completions"
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": ("Use web search. Return one short public fact with its source URL."),
                },
                {"role": "user", "content": "OpenAI official website"},
            ],
            "enable_search": True,
            "search_options": {"return_citations": True},
            "max_tokens": 32,
        }
    elif protocol == "dashscope_native":
        probe_base_url = _dashscope_api_root(base_url)
        path = "/api/v1/services/aigc/text-generation/generation"
        body = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "system",
                        "content": ("Use web search. Return one short public fact with its source URL."),
                    },
                    {"role": "user", "content": "OpenAI official website"},
                ],
            },
            "parameters": {
                "enable_search": True,
                "search_options": {
                    "enable_source": True,
                    "enable_citation": True,
                    "citation_format": "[ref_<number>]",
                },
            },
        }
    else:
        return _unsupported_capability_protocol("web_search", protocol)

    response, error = await _probe(
        "POST",
        path,
        base_url=probe_base_url,
        headers=_bearer_headers(api_key),
        json_body=body,
        timeout_sec=timeout_sec,
    )
    if error is not None:
        return error
    assert response is not None
    payload, response_error = _json_capability_payload(response)
    if response_error is not None:
        return response_error
    if not _has_web_search_output(protocol, payload):
        return VerifyResult(
            False,
            "web_search_output_missing",
            http_status=response.status_code,
            message="联网搜索响应不含可用输出",
        )
    return VerifyResult(True, "ok", http_status=response.status_code, message="验证通过")


def _json_capability_payload(
    response: httpx2.Response,
) -> tuple[Any, VerifyResult | None]:
    if response.status_code in (401, 403):
        return None, VerifyResult(
            False,
            "unauthorized",
            http_status=response.status_code,
            message="密钥无效",
        )
    if response.status_code >= 400:
        return None, VerifyResult(
            False,
            "error",
            http_status=response.status_code,
            message="能力探针请求失败",
        )
    try:
        return response.json(), None
    except ValueError:
        return None, VerifyResult(
            False,
            "invalid_response",
            http_status=response.status_code,
            message="响应不是有效 JSON",
        )


def _bearer_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _unsupported_capability_protocol(
    capability: str,
    protocol: str,
) -> VerifyResult:
    return VerifyResult(
        False,
        "probe_not_supported",
        message=f"{capability} 尚无 {protocol} 协议的验证器",
    )


def _dashscope_api_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    for suffix in ("/compatible-mode/v1", "/api/v1"):
        if root.endswith(suffix):
            return root[: -len(suffix)]
    return root


def _has_rerank_result(results: Any) -> bool:
    return isinstance(results, list) and any(
        isinstance(item, dict) and isinstance(item.get("index"), int) and isinstance(item.get("relevance_score"), (int, float)) for item in results
    )


def _has_generated_image(data: Any) -> bool:
    if not isinstance(data, list):
        return False
    for item in data:
        if not isinstance(item, dict):
            continue
        if _is_https_url(item.get("url")):
            return True
        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if image.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or (image.startswith(b"RIFF") and image[8:12] == b"WEBP"):
            return True
    return False


def _has_https_image_url(data: Any) -> bool:
    return isinstance(data, list) and any(isinstance(item, dict) and _is_https_url(item.get("url")) for item in data)


def _is_https_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("https://")


def _has_web_search_output(protocol: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if protocol == "dashscope_compat":
        if _has_items(payload.get("search_results")) or _has_items(payload.get("citations")):
            return True
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                if _has_items(message.get("citations")):
                    return True
                if _HTTP_URL_RE.search(str(message.get("content") or "")):
                    return True
        return False
    output = payload.get("output")
    if not isinstance(output, dict):
        return False
    search_info = output.get("search_info")
    return isinstance(search_info, dict) and _has_items(search_info.get("search_results"))


def _has_items(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


async def _drain_runtime_stream(
    adapter: OpenAICompatibleAdapter | AnthropicCompatibleAdapter,
    request: LLMRequest,
) -> None:
    """Require a protocol-level completion signal, not merely HTTP 2xx."""

    response_signal_seen = False
    async for chunk in adapter.stream(request):
        response_signal_seen = response_signal_seen or bool(
            chunk.delta or chunk.thinking_delta or chunk.tool_call_delta is not None or chunk.finish_reason is not None
        )
    if not response_signal_seen:
        raise ProviderCallError(
            kind=ProviderFailureKind.RESPONSE_INVALID,
            retryable=False,
        )


def _runtime_probe_request(model: str, capability: str) -> LLMRequest:
    images: list[ImageRef] = []
    content = "Reply with OK."
    tools: list[ToolSchema] = []
    max_output_tokens = 8
    if capability == "vision":
        images = [
            ImageRef(
                mime="image/png",
                base64=_VISION_PROBE_PNG_BASE64,
            )
        ]
    elif capability == "tool":
        content = "You must call the bowhead_connection_probe tool now. Do not answer with text."
        tools = [
            ToolSchema(
                name="bowhead_connection_probe",
                description="connection probe",
                input_schema={"type": "object", "properties": {}},
            )
        ]
        max_output_tokens = _TOOL_PROBE_MAX_TOKENS
    return LLMRequest(
        model=model,
        messages=[Message(role="user", content=content, images=images)],
        tools=tools,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
    )


def _capability_transport_error(error: VerifyResult, *, phase: str) -> _CapabilityResult:
    return _CapabilityResult(
        "failed",
        error.code,
        phase,
        error.http_status,
        error.message,
    )


def _capability_exception(exc: BaseException, *, phase: str) -> _CapabilityResult:
    if isinstance(exc, ProviderCallError):
        code_by_kind = {
            ProviderFailureKind.AUTH: "unauthorized",
            ProviderFailureKind.PERMISSION: "unauthorized",
            ProviderFailureKind.RATE_LIMITED: "rate_limited",
            ProviderFailureKind.TIMEOUT: "unreachable",
            ProviderFailureKind.NETWORK: "unreachable",
            ProviderFailureKind.REQUEST_REJECTED: "bad_request",
            ProviderFailureKind.RESPONSE_INVALID: "invalid_response",
            ProviderFailureKind.UPSTREAM: "error",
            ProviderFailureKind.CONFIGURATION: "bad_request",
            ProviderFailureKind.CONTEXT_OVERFLOW: "bad_request",
            ProviderFailureKind.UNKNOWN: "error",
        }
        message_by_code = {
            "unauthorized": "密钥无效",
            "rate_limited": "模型服务当前请求过多",
            "unreachable": "模型服务不可达或响应超时",
            "bad_request": "模型服务拒绝了探针请求",
            "invalid_response": "模型服务响应格式无效",
            "error": "模型服务调用失败",
        }
        code = code_by_kind[exc.kind]
        return _CapabilityResult(
            "failed",
            code,
            phase,
            exc.status_code,
            message_by_code[code],
        )
    if isinstance(exc, SsrfBlocked):
        return _CapabilityResult("failed", "blocked", phase, message="地址被安全策略拦截")
    if isinstance(exc, (httpx2.TimeoutException, TimeoutError)):
        return _CapabilityResult("failed", "unreachable", phase, message="连接超时")
    return _CapabilityResult("failed", "error", phase, message="模型服务调用失败")


def _has_embedding(payload: Any) -> bool:
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, list) and any(
        isinstance(item, dict)
        and isinstance(item.get("embedding"), list)
        and item["embedding"]
        and all(isinstance(value, (int, float)) for value in item["embedding"])
        for item in data
    )
