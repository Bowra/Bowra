"""Canonical transport plans shared by runtime, discovery and verification.

The plan is deliberately body-agnostic: probes use smaller payloads than real
turns, while target, query, authentication and transport policy stay identical.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
SUPPORTED_RESPONSE_CONTRACTS = frozenset(
    {
        "openai_chat_completions",
        "openai_chat_completions_sse",
        "openai_embeddings",
    }
)
SUPPORTED_TEMPLATE_VARIABLES = frozenset(
    {
        "model",
        "deployment",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "max_output_tokens",
        "input",
    }
)
_CHAT_CAPABILITIES = frozenset({"chat", "tool", "vision"})
_FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "forwarded",
        "via",
        "te",
        "upgrade",
    }
)
_URL_VARIABLE_RE = re.compile(r"\{(base_url|model|deployment)\}")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAX_URL_TEMPLATE_CHARS = 4096
_MAX_TEMPLATE_CHARS = 131072
_MAX_TEMPLATE_DEPTH = 16
_MAX_TEMPLATE_NODES = 1000
_SENSITIVE_HEADER_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)

TransportPurpose = Literal[
    "runtime_chat",
    "runtime_embed",
    "probe_chat",
    "probe_tool",
    "probe_vision",
    "probe_embed",
    "probe_rerank",
    "discovery",
]


@dataclass(frozen=True, slots=True)
class SecretComponent:
    """Secret-bearing request component; value must never be serialized."""

    kind: Literal["header", "query"]
    name: str
    value: str
    semantic: str


@dataclass(frozen=True, slots=True)
class CanonicalProviderTransportPlan:
    adapter_type: str
    adapter_contract_version: str
    purpose: TransportPurpose
    method: str
    request_url: str
    safe_header_semantics: tuple[tuple[str, str], ...]
    secret_components: tuple[SecretComponent, ...]
    request_options: Mapping[str, JsonValue]


def _canonical_request_url(base_url: str, request_path: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider base_url must be an absolute HTTP(S) URL")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    default_port = 443 if scheme == "https" else 80
    port = parsed.port or default_port
    netloc = f"[{host}]" if ":" in host else host
    if port != default_port:
        netloc = f"{netloc}:{port}"

    base_path = parsed.path.rstrip("/")
    suffix = request_path if request_path.startswith("/") else f"/{request_path}"
    path = f"{base_path}{suffix}" or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _provider_value(provider: Any, key: str, default: Any = None) -> Any:
    if isinstance(provider, dict):
        if key in provider:
            return provider.get(key, default)
        extra = provider.get("extra")
        if isinstance(extra, dict) and key in extra:
            return extra.get(key, default)
        return default
    return getattr(provider, key, default)


def _provider_mapping(provider: Any) -> dict[str, Any]:
    if isinstance(provider, dict):
        merged = dict(provider)
        extra = merged.pop("extra", None)
        if isinstance(extra, dict):
            merged = {**extra, **merged}
        return merged
    raw = {
        "type": getattr(provider, "type", None),
        "base_url": getattr(provider, "base_url", None),
        "api_key": getattr(provider, "api_key", None),
        "default_headers": getattr(provider, "default_headers", {}),
        "timeout_sec": getattr(provider, "timeout_sec", 60),
    }
    extra = getattr(provider, "extra", None)
    if isinstance(extra, dict):
        raw = {**extra, **raw}
    return raw


def _model_spec(provider: Any, model_id: str | None) -> dict[str, Any]:
    if not model_id:
        return {}
    for item in _provider_value(provider, "models", []) or []:
        if isinstance(item, str) and item == model_id:
            return {"id": item}
        if isinstance(item, dict) and str(item.get("id") or "") == model_id:
            return item
    return {"id": model_id}


def model_api_surface(provider: Any, model_id: str | None) -> str:
    """模型声明的协议面 —— **全仓唯一实现**，adapter/probe 都从这里读。

    「面」是模型级事实，不是 provider 级：同一网关两条面并存、支持度细到
    模型 × 往返腿（docs/sprints/responses-api-2026-08/tech-design.md §4.1 / §2.6）。
    合法非默认值只有 `"responses"`（保存期校验在 team_llm_config / V2ModelDeclaration）。"""

    surface = str(_model_spec(provider, model_id).get("api_surface") or "").strip()
    return surface or "chat_completions"


def model_request_shape(
    provider: Any,
    *,
    model_id: str,
    capability: str,
    adapter_type: str,
) -> dict[str, Any]:
    """模型级的**出站形状**声明：会改变线上请求、因而必须让旧证据失效的那些字段。

    此前只有 `custom_http` 计算它（`request_semantics_sha256`），于是
    openai / anthropic 面改 `request_body_overrides` **不会**让证据 stale ——
    与权威契约「Body、模型覆盖…变化会使旧 Evidence stale」不符。探针开始消费
    overrides 之后，这个洞会直接变成「证据说验过、而我们发的是另一个形状」。

    只在**确有声明**时才进签名：没配这些字段的部署签名一字不变，不制造无谓的全量重验。
    """

    model = _model_spec(provider, model_id)
    overrides = model.get("request_body_overrides")
    capability_overrides = overrides.get(capability) if isinstance(overrides, dict) else None
    shape: dict[str, Any] = {}
    output_capacity = model.get("max_output_tokens", _provider_value(provider, "max_output_tokens"))
    if output_capacity is not None:
        shape["max_output_tokens"] = output_capacity
    if capability_overrides:
        shape["request_body_overrides"] = capability_overrides
    declared = _outbound_params_declaration(provider, model)
    if declared:
        shape["outbound_params"] = declared
    from bowhead_router.runtime_parameters import runtime_policy_shape

    policy = runtime_policy_shape(provider, model_id, capability)
    if policy:
        shape["runtime_parameters"] = policy
    if adapter_type == "openai_compatible" and model_api_surface(provider, model_id) == "responses":
        # Only openai_compatible consumes this surface (review R2/R1-P2-8): without this guard, a raw API could attach
        # the key to azure/anthropic models and verify evidence under a signature that declares responses,
        # while runtime silently uses chat. Save-time validation is the first gate; this adds defense in depth.
        # The API surface changes the entire request shape: switching surfaces must invalidate existing verification evidence (tech-design §8.3;
        # the chain request_semantics_sha256 → request_options → compute_config_signature
        # has been verified at every step). Add only non-default values to shape, preserving existing deployment signatures exactly.
        shape["api_surface"] = "responses"
    if adapter_type == "custom_http":
        shape.update(_custom_http_request_semantics(provider, model_id=model_id, capability=capability))
    return shape


def _outbound_params_declaration(provider: Any, model: Any) -> dict[str, Any]:
    """provider 级默认 + 模型级覆盖后的**有效**声明（全 follow 视同没声明）。"""

    merged: dict[str, Any] = {}
    for source in (_provider_value(provider, "outbound_params", {}), model.get("outbound_params") if isinstance(model, dict) else None):
        if isinstance(source, dict):
            merged.update(source)
    return {key: value for key, value in merged.items() if isinstance(value, dict) and value.get("mode", "follow") != "follow"}


def _custom_http_request_semantics(
    provider: Any,
    *,
    model_id: str,
    capability: str,
) -> dict[str, Any]:
    """Return only model fields that can change the rendered wire request."""

    model = _model_spec(provider, model_id)
    transport_refs = model.get("capability_transports")
    transport_ref = transport_refs.get(capability) if isinstance(transport_refs, dict) else None
    transports = _provider_value(provider, "transports", {}) or {}
    transport = transports.get(transport_ref) if isinstance(transports, dict) else None
    overrides = model.get("request_body_overrides")
    capability_overrides = overrides.get(capability) if isinstance(overrides, dict) else None
    return {
        "model_id": model_id,
        "deployment": str(model.get("deployment") or model_id),
        "transport_ref": transport_ref,
        "transport": transport,
        "request_body_overrides": capability_overrides,
    }


def uses_max_completion_tokens(model_id: str) -> bool:
    """Return whether an Azure/OpenAI GPT-5 deployment uses the new token field."""

    normalized = re.sub(r"[^a-z0-9]+", "-", model_id.strip().lower())
    return bool(re.match(r"^gpt-5(?:-|$)", normalized) or re.match(r"^gpt-5[0-9]", normalized))


def _validate_outbound_headers(headers: Mapping[str, Any]) -> None:
    if len(headers) > 50:
        raise ValueError("too many custom HTTP headers")
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if (
            not name
            or not _HEADER_NAME_RE.fullmatch(str(raw_name).strip())
            or name in _FORBIDDEN_HEADERS
            or name.startswith(("x-forwarded-", "proxy-"))
        ):
            raise ValueError(f"custom HTTP header is forbidden: {raw_name!r}")
        value = str(raw_value)
        if len(value) > 4096 or "\r" in value or "\n" in value:
            raise ValueError(f"custom HTTP header value is invalid: {raw_name!r}")


def _auth_headers(provider: Any, api_key: str) -> dict[str, str]:
    auth = _provider_value(provider, "auth", {}) or {}
    if not isinstance(auth, dict):
        raise ValueError("provider auth must be an object")
    auth_type = str(auth.get("type") or "bearer")
    secret = str(auth.get("secret") or api_key)
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth_type == "api_key":
        return {str(auth.get("header_name") or "api-key"): secret}
    if auth_type == "secret_header":
        header_name = str(auth.get("header_name") or "")
        if not header_name:
            raise ValueError("secret_header auth requires header_name")
        prefix = str(auth.get("prefix") or "")
        return {header_name: f"{prefix}{secret}"}
    if auth_type == "none":
        return {}
    raise ValueError(f"unsupported provider auth type: {auth_type!r}")


def _append_query(url: str, query: Mapping[str, Any]) -> str:
    parsed = urlsplit(url)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    existing_names = {key for key, _ in existing}
    duplicates = existing_names.intersection(str(key) for key in query)
    if duplicates:
        raise ValueError(f"duplicate query parameter: {sorted(duplicates)[0]}")
    merged = [*existing, *((str(key), str(value)) for key, value in query.items())]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(sorted(merged), doseq=True),
            "",
        )
    )


def _render_url_template(
    template: str,
    *,
    base_url: str,
    model_id: str,
    deployment: str,
) -> str:
    if not template or len(template) > _MAX_URL_TEMPLATE_CHARS:
        raise ValueError("custom HTTP url_template has invalid length")
    unknown = set(re.findall(r"\{([^{}]+)\}", template)) - {
        "base_url",
        "model",
        "deployment",
    }
    if unknown:
        raise ValueError(f"unsupported URL template variable: {sorted(unknown)[0]}")
    values = {
        "base_url": base_url.rstrip("/"),
        "model": quote(model_id, safe=""),
        "deployment": quote(deployment, safe=""),
    }
    rendered = _URL_VARIABLE_RE.sub(lambda match: values[match.group(1)], template)
    parsed = urlsplit(rendered)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("custom HTTP url_template must render an absolute HTTP(S) URL")
    if len(rendered) > _MAX_URL_TEMPLATE_CHARS:
        raise ValueError("rendered custom HTTP URL is too long")
    return rendered


def _custom_transport(
    provider: Any,
    *,
    purpose: TransportPurpose,
    model_id: str,
    capability: str,
) -> tuple[str, str, str, dict[str, str]]:
    model = _model_spec(provider, model_id)
    refs = model.get("capability_transports") or {}
    if not isinstance(refs, dict):
        raise ValueError("model capability_transports must be an object")
    transport_ref = refs.get(capability)
    if not isinstance(transport_ref, str) or not transport_ref:
        raise ValueError(f"model {model_id!r} has no transport for capability {capability!r}")
    transports = _provider_value(provider, "transports", {}) or {}
    transport = transports.get(transport_ref) if isinstance(transports, dict) else None
    if not isinstance(transport, dict):
        raise ValueError(f"unknown custom HTTP transport: {transport_ref!r}")
    contract = str(transport.get("contract") or "")
    if contract not in SUPPORTED_RESPONSE_CONTRACTS:
        raise ValueError(f"unsupported response contract: {contract!r}")
    if capability == "embed" and contract != "openai_embeddings":
        raise ValueError("embed capability requires openai_embeddings contract")
    if capability in _CHAT_CAPABILITIES and contract not in {
        "openai_chat_completions",
        "openai_chat_completions_sse",
    }:
        raise ValueError(f"{capability} capability requires a chat completions contract")
    request = transport.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"transport {transport_ref!r}.request is required")
    method = str(request.get("method") or "POST").upper()
    if method != "POST":
        raise ValueError("custom HTTP inference requests only support POST")
    base_url = str(_provider_value(provider, "base_url") or "").strip()
    template = str(request.get("url_template") or "")
    deployment = str(model.get("deployment") or model_id)
    request_url = _render_url_template(
        template,
        base_url=base_url,
        model_id=model_id,
        deployment=deployment,
    )
    query = request.get("query") or {}
    if not isinstance(query, dict):
        raise ValueError("custom HTTP request.query must be an object")
    if len(query) > 50 or any(not isinstance(value, str | int | float | bool) for value in query.values()):
        raise ValueError("custom HTTP request.query must contain at most 50 scalar values")
    if any(is_sensitive_query_name(str(name)) for name in query):
        raise ValueError("credential-like query parameters are not supported")
    request_url = _append_query(request_url, query)
    headers = request.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("custom HTTP request.headers must be an object")
    _validate_outbound_headers(headers)
    if any(is_sensitive_header_name(str(name)) for name in headers):
        raise ValueError(
            "secret headers must use provider auth, not request.headers",
        )
    return method, request_url, contract, {str(k): str(v) for k, v in headers.items()}


def is_sensitive_header_name(name: str) -> bool:
    """Return whether a header value must never enter a public projection."""

    lowered = name.strip().lower()
    return any(marker in lowered for marker in _SENSITIVE_HEADER_MARKERS)


def is_sensitive_query_name(name: str) -> bool:
    """Return whether a query value is a credential rather than routing data."""

    lowered = name.strip().lower()
    return lowered in {"code", "sig"} or any(
        marker in lowered
        for marker in (
            "authorization",
            "credential",
            "key",
            "password",
            "secret",
            "signature",
            "token",
        )
    )


def _request_shape_option(
    provider: Any,
    *,
    model_id: str | None,
    capability: str | None,
    adapter_type: str,
) -> dict[str, Any]:
    """把出站形状声明折进 `request_options`（进而进 config_signature）。

    没有模型上下文（运行期的静态 plan）或没有任何声明时**不加这个键** ——
    既有部署的签名因此一字不变，不会因为本能力上线就全量 stale。
    """

    if not model_id:
        return {}
    shape = model_request_shape(
        provider,
        model_id=model_id,
        capability=capability or "",
        adapter_type=adapter_type,
    )
    if not shape:
        return {}
    return {
        "request_semantics_sha256": hashlib.sha256(
            json.dumps(shape, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest(),
    }


def build_provider_transport_plan(
    provider: Any,
    *,
    purpose: TransportPurpose,
    model_id: str | None = None,
    capability: str | None = None,
) -> CanonicalProviderTransportPlan:
    """Build the exact compatible-provider transport semantics for a purpose."""

    adapter_type = str(_provider_value(provider, "type") or "").strip()
    base_url = str(_provider_value(provider, "endpoint") or _provider_value(provider, "base_url") or "").strip()
    api_key = str(_provider_value(provider, "api_key") or "")
    default_headers = dict(_provider_value(provider, "default_headers", {}) or {})
    extra = dict(_provider_value(provider, "extra", {}) or {})
    timeout_sec = int(_provider_value(provider, "timeout_sec", 60))

    if adapter_type == "openai_compatible":
        path = "/embeddings" if purpose in {"runtime_embed", "probe_embed"} else "/models" if purpose == "discovery" else "/chat/completions"
        method = "GET" if purpose == "discovery" else "POST"
        contract_version = "openai-compatible-v1"
        # Responses surface: route runtime_chat and probe_chat/tool/vision separately (from batch 2, probes
        # actually call /responses, using the same mode as runtime calls, tech-design §10.1). embed and
        # discovery are independent of the surface.
        if purpose in {"runtime_chat", "probe_chat", "probe_tool", "probe_vision"} and model_api_surface(provider, model_id) == "responses":
            path = "/responses"
            contract_version = "openai-responses-v1"
        built_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **default_headers,
        }
    elif adapter_type == "azure_openai":
        if purpose == "discovery":
            raise ValueError("azure_openai does not support model discovery")
        if not model_id:
            raise ValueError("azure_openai transport requires model_id")
        if "/openai/deployments/" in urlsplit(base_url).path.lower():
            raise ValueError(
                "azure endpoint must be the root endpoint, not a deployment URL",
            )
        model = _model_spec(provider, model_id)
        deployment = str(model.get("deployment") or model_id)
        selected_capability = capability or ("embed" if purpose in {"runtime_embed", "probe_embed"} else "chat")
        leaf = "embeddings" if selected_capability == "embed" else "chat/completions"
        path = f"/openai/deployments/{quote(deployment, safe='')}/{leaf}"
        method = "POST"
        contract_version = "openai_embeddings" if selected_capability == "embed" else "openai_chat_completions_sse"
        api_version = str(_provider_value(provider, "api_version") or "").strip()
        if not api_version:
            raise ValueError("azure_openai requires api_version")
        base_url = _append_query(base_url, {"api-version": api_version})
        built_headers = {
            **_auth_headers(provider, api_key),
            "Content-Type": "application/json",
            "Accept": ("text/event-stream" if contract_version == "openai_chat_completions_sse" else "application/json"),
            **default_headers,
        }
    elif adapter_type == "custom_http":
        if purpose == "discovery":
            raise ValueError("custom_http does not support model discovery")
        if not model_id or not capability:
            raise ValueError("custom_http transport requires model_id and capability")
        method, request_url, contract_version, request_headers = _custom_transport(
            provider,
            purpose=purpose,
            model_id=model_id,
            capability=capability,
        )
        path = ""
        built_headers = {
            **_auth_headers(provider, api_key),
            "Content-Type": "application/json",
            "Accept": ("text/event-stream" if contract_version == "openai_chat_completions_sse" else "application/json"),
            **default_headers,
            **request_headers,
        }
    elif adapter_type == "anthropic_compatible":
        if purpose in {"runtime_embed", "probe_embed", "discovery"}:
            raise ValueError(f"{adapter_type} does not define transport for {purpose}")
        path = str(_provider_value(provider, "messages_path") or extra.get("messages_path") or "/v1/messages")
        method = "POST"
        contract_version = "anthropic-compatible-v1"
        anthropic_version = str(_provider_value(provider, "anthropic_version") or extra.get("anthropic_version") or "2023-06-01")
        built_headers = {
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": anthropic_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
            **default_headers,
        }
    else:
        raise ValueError(f"unsupported provider adapter type: {adapter_type!r}")

    _validate_outbound_headers(built_headers)
    if adapter_type != "custom_http":
        request_url = _canonical_request_url(base_url, path)
    secret_components: list[SecretComponent] = [
        SecretComponent(
            kind="query",
            name=name,
            value=value,
            semantic="query_credential",
        )
        for name, value in parse_qsl(
            urlsplit(request_url).query,
            keep_blank_values=True,
        )
        if is_sensitive_query_name(name)
    ]
    safe_headers: list[tuple[str, str]] = []
    # HTTP header names are case-insensitive. Normalize before classifying so a
    # custom `authorization` reliably overrides the built-in `Authorization`
    # instead of creating two signature entries with one wire value.
    normalized_headers = {name.strip().lower(): str(value) for name, value in built_headers.items()}
    for lowered, text in normalized_headers.items():
        if lowered == "authorization":
            scheme, _, credential = text.partition(" ")
            secret_components.append(
                SecretComponent(
                    kind="header",
                    name=lowered,
                    value=credential or text,
                    semantic=scheme.lower() if credential else "opaque",
                )
            )
            safe_headers.append((lowered, scheme.lower() if credential else "opaque"))
        elif is_sensitive_header_name(lowered):
            secret_components.append(
                SecretComponent(
                    kind="header",
                    name=lowered,
                    value=text,
                    semantic="api_key",
                )
            )
            safe_headers.append((lowered, "secret"))
        else:
            safe_headers.append((lowered, text.strip()))

    return CanonicalProviderTransportPlan(
        adapter_type=adapter_type,
        adapter_contract_version=contract_version,
        purpose=purpose,
        method=method,
        request_url=request_url,
        safe_header_semantics=tuple(sorted(safe_headers)),
        secret_components=tuple(
            sorted(
                secret_components,
                key=lambda item: (item.kind, item.name, item.semantic),
            )
        ),
        request_options={
            "timeout_sec": timeout_sec,
            # Router owns one retry before provider fallback today. Provider
            # config's legacy max_retries field is not consumed by runtime and
            # therefore must not create false signature invalidations.
            "runtime_retry_attempts": 2,
            **_request_shape_option(
                provider,
                model_id=model_id,
                capability=capability,
                adapter_type=adapter_type,
            ),
        },
    )


def _render_template_value(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        if "$bowhead" in value:
            if set(value) - {"$bowhead", "omit_if_empty"}:
                raise ValueError("$bowhead placeholder contains unsupported options")
            variable = value.get("$bowhead")
            if variable not in SUPPORTED_TEMPLATE_VARIABLES:
                raise ValueError(f"unsupported $bowhead variable: {variable!r}")
            rendered = copy.deepcopy(variables.get(str(variable)))
            if value.get("omit_if_empty") and rendered in (None, "", [], {}):
                return _OMIT
            return rendered
        out: dict[str, Any] = {}
        for key, item in value.items():
            rendered = _render_template_value(item, variables)
            if rendered is not _OMIT:
                out[str(key)] = rendered
        return out
    if isinstance(value, list):
        out_list = []
        for item in value:
            rendered = _render_template_value(item, variables)
            if rendered is not _OMIT:
                out_list.append(rendered)
        return out_list
    if isinstance(value, str) and any(marker in value for marker in ("${", "{{", "{%", "<%")):
        raise ValueError("string interpolation and executable templates are not supported")
    return copy.deepcopy(value)


_OMIT = object()


def _validate_json_template_shape(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _MAX_TEMPLATE_CHARS:
        raise ValueError("custom HTTP JSON template is too large")
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_TEMPLATE_NODES or depth > _MAX_TEMPLATE_DEPTH:
            raise ValueError("custom HTTP JSON template is too complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise ValueError("custom HTTP JSON template key is invalid")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif not isinstance(item, (type(None), bool, int, float, str)):
            raise ValueError("custom HTTP JSON template must contain JSON values")

    visit(value, 0)


def _template_variables(value: Any) -> set[str]:
    if isinstance(value, dict):
        variable = value.get("$bowhead")
        if isinstance(variable, str):
            return {variable}
        return set().union(*(_template_variables(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_template_variables(item) for item in value))
    return set()


def render_custom_http_body(
    provider: Any,
    *,
    model_id: str,
    capability: str,
    variables: Mapping[str, Any],
) -> dict[str, Any]:
    """Render a bounded JSON body template using whole-value placeholders."""

    model = _model_spec(provider, model_id)
    refs = model.get("capability_transports") or {}
    transport_ref = refs.get(capability) if isinstance(refs, dict) else None
    transports = _provider_value(provider, "transports", {}) or {}
    transport = transports.get(transport_ref) if isinstance(transports, dict) else None
    request = transport.get("request") if isinstance(transport, dict) else None
    template = request.get("body_template") if isinstance(request, dict) else None
    if not isinstance(template, dict):
        raise ValueError("custom HTTP body_template must be a JSON object")
    _validate_json_template_shape(template)
    contract = str(transport.get("contract") or "")
    template_variables = _template_variables(template)
    required = {"input"} if contract == "openai_embeddings" else {"messages", "stream"} if contract == "openai_chat_completions_sse" else {"messages"}
    missing = required - template_variables
    if missing:
        raise ValueError(
            f"custom HTTP body_template is missing required variable: {sorted(missing)[0]}",
        )
    rendered = _render_template_value(template, variables)
    if not isinstance(rendered, dict):
        raise ValueError("custom HTTP body_template must render a JSON object")

    overrides = model.get("request_body_overrides") or {}
    capability_overrides = overrides.get(capability) if isinstance(overrides, dict) else None
    if capability_overrides is not None:
        if not isinstance(capability_overrides, dict):
            raise ValueError("request_body_overrides capability must be an object")
        _validate_json_template_shape(capability_overrides)
        reserved = {
            "model",
            "deployment",
            "messages",
            "tools",
            "tool_choice",
            "stream",
            "input",
            "max_tokens",
            "max_completion_tokens",
        }
        if reserved.intersection(capability_overrides):
            raise ValueError("request_body_overrides cannot replace runtime fields")
        for key, value in capability_overrides.items():
            if key not in template or _template_variables(template[key]):
                raise ValueError(
                    "request_body_overrides may only replace existing constant fields",
                )
            if _template_variables(value):
                raise ValueError("request_body_overrides cannot add $bowhead variables")
            static_value = _render_template_value(value, {})
            if static_value is _OMIT:
                raise ValueError("request_body_overrides cannot omit fields")
            rendered[str(key)] = static_value
    return rendered


def materialize_transport_headers(
    plan: CanonicalProviderTransportPlan,
) -> dict[str, str]:
    """Reconstruct request headers in memory without serializing secrets."""

    secrets = {component.name.lower(): component for component in plan.secret_components if component.kind == "header"}
    headers: dict[str, str] = {}
    for name, semantic in plan.safe_header_semantics:
        secret = secrets.get(name)
        if secret is None:
            headers[name] = semantic
        elif name == "authorization" and secret.semantic != "opaque":
            headers[name] = f"{secret.semantic.title()} {secret.value}"
        else:
            headers[name] = secret.value
    return headers


def public_transport_preview(
    plan: CanonicalProviderTransportPlan,
) -> dict[str, JsonValue]:
    """Return a secret-free representation suitable for admin previews."""

    parsed = urlsplit(plan.request_url)
    secret_query_names = {secret.name for secret in plan.secret_components if secret.kind == "query"}
    return {
        "method": plan.method,
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "path": parsed.path,
        "non_secret_query": [
            [key, value]
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key not in secret_query_names
        ],
        "auth": [
            {
                "kind": secret.kind,
                "name": secret.name,
                "semantic": secret.semantic,
            }
            for secret in plan.secret_components
        ],
        "protocol_headers": [[key, value] for key, value in plan.safe_header_semantics],
    }
