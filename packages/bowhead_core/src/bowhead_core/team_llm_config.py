"""Team-scoped LLM/KB model configuration helpers.

This module is intentionally framework-free. Gateway routes, router runtime,
and tests should all use these helpers so team BYO config has one schema
semantic instead of several ad-hoc interpretations.
"""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING, Any

from bowhead_router.agent_tiers import (
    TierConfigError,
    is_tier_config,
    tier_agent_bindings,
    validate_tiers,
)
from bowhead_router.model_capabilities import (
    CAPABILITY_VOCAB,
    effective_model_capabilities,
    inferred_provider_capabilities,
)
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.transport import (
    build_provider_transport_plan,
    is_sensitive_header_name,
    render_custom_http_body,
)

from bowhead_core.kb.config_loader import (
    CAPABILITIES as KB_CAPABILITIES,
)
from bowhead_core.kb.config_loader import (
    PROTOCOLS as KB_PROTOCOLS,
)
from bowhead_core.ssrf_guard import SsrfBlocked, validate_url

if TYPE_CHECKING:
    from bowhead_router.config_loader import LoadedRoutingConfig


SCHEMA_VERSION = 2
SUPPORTED_PROVIDER_TYPES = frozenset(
    {
        "openai_compatible",
        "anthropic_compatible",
        "azure_openai",
        "custom_http",
    }
)
REQUIRED_AGENT_TYPES = ("lead", "modeler", "analyst", "narrative", "echo", "judge")
SUPPORTED_KB_CAPABILITIES = frozenset(KB_CAPABILITIES)
# WS1: per-model capability vocab for chat/agent model_details (distinct from KB caps).
_CAPABILITY_VOCAB = CAPABILITY_VOCAB
SECRET_KEYS = frozenset(
    {
        "api_key",
        "secret_key",
        "authorization",
        "secret",
        "secret_headers",
    }
)
PROVIDER_KNOWN_FIELDS = frozenset(
    {
        "type",
        "base_url",
        "api_key",
        "api_key_ref",
        "timeout_sec",
        # Legacy per-provider field: accepted and ignored; retries use the top-level `retry` setting.
        "max_retries",
        "default_headers",
        "supports",
        "models",
        "display_name",
    }
)
_LEGACY_AGENT_ALIASES = {
    "composer": "narrative",
    "critic": "judge",
}
_LEGACY_PROVIDER_ALIASES: dict[str, dict[str, Any]] = {
    "deepseek": {
        "target": "deepseek_anthropic",
        "type": "anthropic_compatible",
        "base_url": "https://api.deepseek.com/anthropic",
        "messages_path": "/messages",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "supports": {"tool_use": True, "extended_thinking": True, "vision": False},
    },
    "doubao": {
        "target": "volces_doubao",
        "type": "anthropic_compatible",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding",
        "models": ["Doubao-Seed-2.0-pro"],
        "supports": {"tool_use": True, "vision": False},
    },
    "qwen": {
        "target": "aliyun_qwen",
        "type": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus"],
        "supports": {"tool_use": True, "vision": True},
    },
    "openai": {
        "type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini"],
        "supports": {"tool_use": True, "vision": True},
    },
    "anthropic": {
        "type": "anthropic_compatible",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-3-5-sonnet-latest"],
        "supports": {"tool_use": True, "extended_thinking": True},
    },
}
SsrfResolver = Any


class TeamLlmConfigError(ValueError):
    """Team LLM config is malformed or references missing providers/models."""


def key_fingerprint(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def is_v2_config(config: dict[str, Any] | None) -> bool:
    return bool(isinstance(config, dict) and config.get("schema_version") == SCHEMA_VERSION and isinstance(config.get("providers"), dict))


def migrate_legacy_config_to_v2(  # noqa: PLR0912
    config: dict[str, Any] | None,
    *,
    loaded_config: LoadedRoutingConfig | None = None,
) -> dict[str, Any]:
    """Return a v2-shaped config for legacy team config without mutating storage.

    Legacy rows seen in production are not homogeneous:
    - early BYO design used providers[] with name/base_url/models + agent_routing;
    - the Billing/Settings UI later wrote key-only provider_name/api_key/role rows.

    For key-only rows we preserve the provider id and fill known vendor defaults
    for common built-ins. Missing values stay blank so Settings can surface an
    explicit validation error instead of silently inventing a working config.

    ``loaded_config`` is a compatibility-only keyword and is deliberately
    ignored. A team row must never inherit a deployment Provider template or
    model binding while being migrated.
    """
    if is_v2_config(config):
        return copy.deepcopy(config)
    if not isinstance(config, dict):
        return {}
    raw_providers = config.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        return {}

    model_hints = _legacy_model_hints()
    providers: dict[str, dict[str, Any]] = {}
    roles: dict[str, str] = {}
    for item in raw_providers:
        if not isinstance(item, dict):
            continue
        name = _legacy_provider_name(item)
        if not name:
            continue
        roles[name] = str(item.get("role") or roles.get(name) or "fallback")
        template = _legacy_provider_template(name)
        spec = {k: copy.deepcopy(v) for k, v in template.items() if k != "target"}
        for key in (
            "type",
            "base_url",
            "api_key",
            "timeout_sec",
            "default_headers",
            "supports",
            "models",
            "messages_path",
            "anthropic_version",
            "sanitize_tool_dots",
        ):
            if key in item and item[key] not in (None, ""):
                spec[key] = copy.deepcopy(item[key])
        if "secret_key" in item and "api_key" not in spec:
            spec["api_key"] = item["secret_key"]
        # SECURITY: drop any api_key_ref carried from legacy item/template so
        # migrated team configs never resolve env refs (see _validate_provider).
        spec.pop("api_key_ref", None)
        if "type" not in spec:
            spec["type"] = "openai_compatible"
        if "models" not in spec or not isinstance(spec.get("models"), list) or not spec.get("models"):
            spec["models"] = _legacy_models_for_provider(name, spec, model_hints)
        providers[name] = spec

    if not providers:
        return {}
    agents = _legacy_agents(config.get("agent_routing"), providers, roles)
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "providers": providers,
        "agents": agents,
        "kb": {"models": {}, "active": {}},
    }
    legacy_kb = config.get("kb")
    if isinstance(legacy_kb, dict):
        out["kb"] = copy.deepcopy(legacy_kb)
    return out


def _legacy_provider_name(item: dict[str, Any]) -> str:
    for key in ("name", "provider_name", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _legacy_model_hints() -> dict[str, list[str]]:
    hints: dict[str, set[str]] = {}
    for name, defaults in _LEGACY_PROVIDER_ALIASES.items():
        target = defaults.get("target")
        models = defaults.get("models") or []
        if target:
            hints.setdefault(str(target), set()).update(str(m) for m in models)
        hints.setdefault(name, set()).update(str(m) for m in models)
    return {name: sorted(models) for name, models in hints.items()}


def _legacy_provider_template(name: str) -> dict[str, Any]:
    alias = _LEGACY_PROVIDER_ALIASES.get(name)
    if alias:
        return copy.deepcopy({k: v for k, v in alias.items() if k != "target"})
    return {}


def _legacy_models_for_provider(
    name: str,
    spec: dict[str, Any],
    model_hints: dict[str, list[str]],
) -> list[str]:
    if hints := model_hints.get(name):
        return list(hints)
    alias = _LEGACY_PROVIDER_ALIASES.get(name) or {}
    target = alias.get("target")
    if isinstance(target, str) and model_hints.get(target):
        return list(model_hints[target])
    models = alias.get("models")
    if isinstance(models, list) and models:
        return [str(m) for m in models if m]
    model = spec.get("model") or spec.get("default_model")
    return [str(model)] if model else []


def _legacy_agents(
    raw_routing: Any,
    providers: dict[str, dict[str, Any]],
    roles: dict[str, str],
) -> dict[str, Any]:
    default_provider = _provider_for_role(roles, "lead") or next(iter(providers))
    narrative_provider = _provider_for_role(roles, "narrative") or default_provider
    judge_provider = _provider_for_role(roles, "judge") or default_provider
    default_pair = _pair_for_legacy_provider(default_provider, providers)
    narrative_pair = _pair_for_legacy_provider(narrative_provider, providers)
    judge_pair = _pair_for_legacy_provider(judge_provider, providers)
    fallback_pairs = [_pair_for_legacy_provider(name, providers) for name, role in roles.items() if role == "fallback" and name in providers]

    def spec(primary: dict[str, str]) -> dict[str, Any]:
        return {
            "primary": primary,
            "fallback": [p for p in fallback_pairs if p != primary],
        }

    agents: dict[str, Any] = {
        "lead": spec(default_pair),
        "modeler": spec(default_pair),
        "narrative": spec(narrative_pair),
        "echo": spec(default_pair),
        "judge": spec(judge_pair),
        "analyst": {
            "default": spec(default_pair),
            "profiles": {
                "compute": spec(default_pair),
                "narrative": spec(narrative_pair),
            },
        },
    }
    if isinstance(raw_routing, dict):
        for raw_agent, raw_target in raw_routing.items():
            target = _parse_legacy_routing_target(raw_target)
            if target is None:
                continue
            agent = _LEGACY_AGENT_ALIASES.get(str(raw_agent), str(raw_agent))
            if agent == "analyst":
                agents["analyst"]["default"] = spec(target)
            elif agent.startswith("analyst."):
                profile = agent.split(".", 1)[1]
                agents["analyst"].setdefault("profiles", {})[profile] = spec(target)
            elif agent in REQUIRED_AGENT_TYPES:
                agents[agent] = spec(target)
    return agents


def _provider_for_role(roles: dict[str, str], role: str) -> str | None:
    for provider, provider_role in roles.items():
        if provider_role == role:
            return provider
    return None


def _pair_for_legacy_provider(
    provider: str,
    providers: dict[str, dict[str, Any]],
) -> dict[str, str]:
    models = providers.get(provider, {}).get("models") or []
    model = str(models[0]) if models else ""
    return {"provider": provider, "model": model}


def _parse_legacy_routing_target(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and "/" in value:
        provider, model = value.split("/", 1)
        provider = provider.strip()
        model = model.strip()
        return {"provider": provider, "model": model} if provider and model else None
    if isinstance(value, dict):
        provider = value.get("provider")
        model = value.get("model")
        if isinstance(provider, str) and isinstance(model, str) and provider and model:
            return {"provider": provider, "model": model}
    return None


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy config and replace secret values with fingerprints."""
    return _redact_value(copy.deepcopy(config))


def _redact_value(value: Any, *, key_name: str | None = None) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in SECRET_KEYS or lk.endswith("_api_key"):
                if isinstance(v, str) and v:
                    out["fingerprint"] = key_fingerprint(v)
                elif v:
                    out["fingerprint"] = "***"
            elif lk == "default_headers":
                out[k] = _redact_headers(v)
            else:
                out[k] = _redact_value(v, key_name=str(k))
        return out
    if isinstance(value, list):
        return [_redact_value(v, key_name=key_name) for v in value]
    return value


def _redact_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    out: dict[str, Any] = {}
    for k, v in headers.items():
        if is_sensitive_header_name(str(k)):
            out[f"{k}_fingerprint"] = key_fingerprint(str(v)) if v else None
        else:
            out[k] = v
    return out


def compatibility_provider_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the legacy Admin Billing provider list shape, secret-free."""
    if is_v2_config(config):
        roles = _provider_roles_from_agents(config.get("agents") or {})
        providers: list[dict[str, Any]] = []
        for name, spec in (config.get("providers") or {}).items():
            if not isinstance(spec, dict):
                continue
            row = {
                "provider_name": str(name),
                "role": roles.get(str(name), "fallback"),
                "type": spec.get("type"),
                "base_url": spec.get("endpoint") or spec.get("base_url"),
            }
            api_key = spec.get("api_key") or spec.get("secret_key")
            if isinstance(api_key, str) and api_key:
                row["fingerprint"] = key_fingerprint(api_key)
            providers.append({k: v for k, v in row.items() if v is not None})
        return providers

    providers = config.get("providers") if isinstance(config, dict) else []
    if not isinstance(providers, list):
        return []
    rows: list[dict[str, Any]] = []
    for p in providers:
        if not isinstance(p, dict):
            continue
        row = {k: v for k, v in p.items() if k not in {"api_key", "secret_key"}}
        api_key = p.get("api_key") or p.get("secret_key")
        if isinstance(api_key, str) and api_key:
            row["fingerprint"] = key_fingerprint(api_key)
        rows.append(row)
    return rows


def _provider_roles_from_agents(agents: dict[str, Any]) -> dict[str, str]:
    roles: dict[str, str] = {}

    def visit(agent_type: str, spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        primary = spec.get("primary")
        if isinstance(primary, dict):
            provider = primary.get("provider")
            if isinstance(provider, str) and provider:
                roles.setdefault(provider, _legacy_role(agent_type))
        for f in spec.get("fallback") or []:
            if isinstance(f, dict):
                provider = f.get("provider")
                if isinstance(provider, str) and provider:
                    roles.setdefault(provider, "fallback")

    for agent_type, spec in agents.items():
        if agent_type == "analyst" and isinstance(spec, dict):
            visit("analyst", spec.get("default"))
            for profile_spec in (spec.get("profiles") or {}).values():
                visit("analyst", profile_spec)
        else:
            visit(str(agent_type), spec)
    return roles


def _legacy_role(agent_type: str) -> str:
    return agent_type if agent_type in {"lead", "narrative", "judge"} else "fallback"


def validate_v2_config(
    config: dict[str, Any],
    *,
    ssrf_resolver: SsrfResolver | None = None,
    allow_legacy_duplicate_fallbacks: bool = False,
    allow_unassigned_tiers: bool = False,
) -> None:
    if not is_v2_config(config):
        raise TeamLlmConfigError("team LLM config must use schema_version=2")
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise TeamLlmConfigError("providers must be a non-empty object")
    for name, spec in providers.items():
        _validate_provider(str(name), spec, ssrf_resolver=ssrf_resolver)
    _validate_retry(config.get("retry"))
    # Tier-shaped configuration: expand agents from AGENT_TIER_MAP at build time; persist only the tiers section.
    if is_tier_config(config):
        # Management may persist connections before any production assignment.
        # Runtime callers keep the strict default and cannot construct a Router.
        tiers = config.get("tiers")
        unassigned = (
            isinstance(tiers, dict)
            and not tiers.get("primary")
            and all(value in (None, [], {"follow": "primary"}, {"auto": True}) for value in tiers.values())
        )
        if allow_unassigned_tiers and unassigned and not (config.get("kb") or {}).get("active"):
            _validate_kb(config.get("kb") or {}, providers)
            _validate_media(config.get("media") or {}, providers)
            _validate_search(config.get("search") or {}, providers)
            return
        try:
            validate_tiers(
                config.get("tiers"),
                providers,
                allow_legacy_duplicate_fallbacks=(allow_legacy_duplicate_fallbacks),
            )
        except TierConfigError as e:
            raise TeamLlmConfigError(str(e)) from e
        _validate_kb(config.get("kb") or {}, providers)
        _validate_media(config.get("media") or {}, providers)
        _validate_search(config.get("search") or {}, providers)
        return
    agents = config.get("agents")
    if not isinstance(agents, dict):
        raise TeamLlmConfigError("agents must be an object")
    for agent_type in REQUIRED_AGENT_TYPES:
        if agent_type not in agents:
            raise TeamLlmConfigError(f"agents.{agent_type} is required")
    for agent_type, spec in agents.items():
        if agent_type == "analyst":
            if not isinstance(spec, dict) or "default" not in spec:
                raise TeamLlmConfigError("agents.analyst.default is required")
            _validate_agent_spec("analyst", spec["default"], providers)
            for profile_name, profile_spec in (spec.get("profiles") or {}).items():
                _validate_agent_spec(f"analyst.profiles.{profile_name}", profile_spec, providers)
        else:
            _validate_agent_spec(str(agent_type), spec, providers)
    _validate_kb(config.get("kb") or {}, providers)
    _validate_media(config.get("media") or {}, providers)
    _validate_search(config.get("search") or {}, providers)


def _validate_retry(retry: Any) -> None:
    """The single retry setting: retries after a failed first model attempt."""
    from bowhead_router.retry import MAX_RETRIES_LIMIT

    if retry is None:
        return
    value = retry.get("max_retries") if isinstance(retry, dict) else None
    if (
        not isinstance(retry, dict)
        or set(retry) != {"max_retries"}
        or not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_RETRIES_LIMIT
    ):
        raise TeamLlmConfigError(f"retry.max_retries must be an integer from 0 to {MAX_RETRIES_LIMIT}")


def _validate_provider(
    name: str,
    spec: Any,
    *,
    ssrf_resolver: SsrfResolver | None,
) -> None:
    if not name:
        raise TeamLlmConfigError("provider name must be non-empty")
    if not isinstance(spec, dict):
        raise TeamLlmConfigError(f"providers.{name} must be an object")
    # SECURITY: team configs must not carry secret-manager refs. `api_key_ref`
    # (env://VAR) is resolved server-side against os.environ with no allowlist;
    # allowing team input here lets an team_admin exfiltrate arbitrary gateway
    # env vars (incl BOWHEAD_FERNET_KEY) via a self-controlled base_url. Team BYO
    # keys must be plaintext `api_key` (Fernet-encrypted at rest); platform
    # providers keep their env refs via the trusted routing.yaml path.
    if spec.get("api_key_ref"):
        raise TeamLlmConfigError(f"providers.{name}.api_key_ref is not allowed in team config; use api_key instead")
    ptype = spec.get("type")
    if ptype not in SUPPORTED_PROVIDER_TYPES:
        raise TeamLlmConfigError(f"providers.{name}.type unsupported: {ptype!r}")
    base_url = spec.get("endpoint") if ptype == "azure_openai" else spec.get("base_url")
    if ptype == "custom_http" and base_url in {None, ""}:
        base_url = ""
    elif not isinstance(base_url, str) or not base_url:
        field = "endpoint" if ptype == "azure_openai" else "base_url"
        raise TeamLlmConfigError(f"providers.{name}.{field} is required")
    if base_url:
        _validate_provider_base_url(name, base_url, ssrf_resolver=ssrf_resolver)
    if ptype == "azure_openai":
        _validate_azure_provider(name, spec)
    elif ptype == "custom_http":
        _validate_custom_http_provider(
            name,
            spec,
            ssrf_resolver=ssrf_resolver,
        )
    _validate_context_window(f"providers.{name}.context_window", spec.get("context_window"))
    _validate_context_window(f"providers.{name}.max_output_tokens", spec.get("max_output_tokens"))
    _validate_provider_models(name, spec.get("models"), provider_type=ptype)
    _validate_outbound_params(name, spec, ptype)
    from bowhead_router.runtime_parameters import RuntimeParametersError, validate_runtime_parameters

    try:
        for model in spec.get("models") or []:
            if isinstance(model, dict):
                validate_runtime_parameters(spec, model)
    except ValueError as exc:
        if isinstance(exc, RuntimeParametersError):
            raise TeamLlmConfigError(str(exc)) from exc
        raise TeamLlmConfigError(f"providers.{name}.runtime_parameters: invalid parameter policy; check field types and transport constants") from exc


def validate_provider_draft(
    runtime_key: str,
    provider: dict[str, Any],
    *,
    ssrf_resolver: SsrfResolver | None = None,
) -> None:
    """Validate one team Provider without requiring a complete config.

    V2 precheck uses this public boundary before it reserves an identity or
    performs discovery. It intentionally performs no Provider HTTP request.
    """

    _validate_provider(
        runtime_key,
        provider,
        ssrf_resolver=ssrf_resolver,
    )


def _validate_context_window(field: str, value: Any) -> None:
    """Optional token capacities must be positive integers, excluding booleans."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TeamLlmConfigError(f"{field} must be a positive integer (tokens)")


def _validate_provider_models(name: str, models: Any, *, provider_type: str | None = None) -> None:
    """WS1: `models` accepts legacy `list[str]` or richer
    `list[{id, capabilities?, protocol?, cost?, source?, verified?}]`
    (per-model caps for BYO mixed providers). Never require string-only."""
    if models is None:
        return
    if not isinstance(models, list) or not models:
        raise TeamLlmConfigError(f"providers.{name}.models must be a non-empty list")
    seen_model_ids: set[str] = set()
    for m in models:
        if isinstance(m, str):
            model_id = m.strip()
            if not model_id:
                raise TeamLlmConfigError(f"providers.{name}.models entries must be non-empty")
        elif isinstance(m, dict):
            raw_model_id = m.get("id")
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                raise TeamLlmConfigError(f"providers.{name}.models[].id is required")
            model_id = raw_model_id.strip()
            caps = m.get("capabilities")
            if caps is not None and (not isinstance(caps, list) or not all(c in _CAPABILITY_VOCAB for c in caps)):
                raise TeamLlmConfigError(f"providers.{name}.models[{m.get('id')}].capabilities invalid")
            protocol = m.get("protocol")
            if protocol is not None and ("rerank" not in (caps or []) or protocol not in KB_PROTOCOLS):
                raise TeamLlmConfigError(f"providers.{name}.models[{m.get('id')}].protocol invalid")
            # Protocol surface (Responses batch 0): this router passes through unknown model keys (observed in review), so value validation
            # must happen here; misspellings silently executing chat are hazardous (tech-design §8.1 / C-4).
            _validate_context_window(f"providers.{name}.models[{model_id}].context_window", m.get("context_window"))
            _validate_context_window(f"providers.{name}.models[{model_id}].max_output_tokens", m.get("max_output_tokens"))
            from bowhead_router.context_policy import ContextPolicyError, parse_context_policy

            try:
                parse_context_policy(m.get("context_policy"), path=f"providers.{name}.models[{model_id}].context_policy")
            except ContextPolicyError as exc:
                raise TeamLlmConfigError(str(exc)) from exc

            from bowhead_core.rate_card import ModelCreditRates

            try:
                ModelCreditRates.from_config(m.get("credit_rates"))
            except ValueError as exc:
                raise TeamLlmConfigError(f"providers.{name}.models[{model_id}].credit_rates: {exc}") from exc
            surface = m.get("api_surface")
            if surface is not None and surface not in {"chat_completions", "responses"}:
                raise TeamLlmConfigError(f"providers.{name}.models[{m.get('id')}].api_surface invalid; allowed: chat_completions, responses")
            # Only openai_compatible consumes the surface (review R1-P2-8): accepting it for other types means declaring
            # responses but silently running chat, the same hazard as misspelling on a different type axis.
            if surface == "responses" and provider_type is not None and provider_type != "openai_compatible":
                raise TeamLlmConfigError(f"providers.{name}.models[{m.get('id')}].api_surface is only supported for openai_compatible providers")
        else:
            raise TeamLlmConfigError(f"providers.{name}.models entries must be a string or object")
        if model_id in seen_model_ids:
            raise TeamLlmConfigError(
                f"providers.{name}.models contains duplicate model id: {model_id}",
            )
        seen_model_ids.add(model_id)


def _validate_outbound_params(name: str, spec: dict[str, Any], provider_type: str) -> None:
    """出站参数声明必须在**保存期**校验。

    否则一份写错的配置会在每个请求的 `_build_body` 里炸 —— 那时既没有可操作的提示，
    也已经太晚。协议差异同样在这里拒（anthropic 的 `max_tokens` 必填，不能声明 `omit`）。
    """

    from bowhead_router.outbound import OutboundParamsError, parse_outbound_params

    family = "anthropic" if provider_type == "anthropic_compatible" else "openai"
    try:
        parse_outbound_params(
            spec.get("outbound_params"),
            family=family,  # type: ignore[arg-type]
            where=f"providers.{name}.outbound_params",
        )
        for model in spec.get("models") or []:
            if not isinstance(model, dict) or model.get("outbound_params") is None:
                continue
            parse_outbound_params(
                model["outbound_params"],
                family=family,  # type: ignore[arg-type]
                where=f"providers.{name}.models[{model.get('id')}].outbound_params",
            )
    except OutboundParamsError as exc:
        raise TeamLlmConfigError(str(exc)) from exc


def _validate_auth(name: str, auth: Any) -> None:
    if auth is None:
        return
    if not isinstance(auth, dict):
        raise TeamLlmConfigError(f"providers.{name}.auth must be an object")
    auth_type = auth.get("type", "bearer")
    if auth_type not in {"bearer", "api_key", "secret_header", "none"}:
        raise TeamLlmConfigError(f"providers.{name}.auth.type unsupported: {auth_type!r}")
    if auth_type == "secret_header":
        header_name = auth.get("header_name")
        if not isinstance(header_name, str) or not header_name.strip():
            raise TeamLlmConfigError(
                f"providers.{name}.auth.header_name is required",
            )
        if is_sensitive_header_name(header_name) is False:
            raise TeamLlmConfigError(
                f"providers.{name}.auth.header_name must identify a secret header",
            )


def _validate_azure_provider(name: str, spec: dict[str, Any]) -> None:
    endpoint = str(spec.get("endpoint") or "")
    if "/openai/deployments/" in endpoint.lower():
        raise TeamLlmConfigError(
            f"providers.{name}.endpoint must be the Azure root endpoint; remove /openai/deployments/.../chat/completions",
        )
    api_version = spec.get("api_version")
    if not isinstance(api_version, str) or not api_version.strip():
        raise TeamLlmConfigError(f"providers.{name}.api_version is required")
    _validate_auth(name, spec.get("auth"))
    if any(is_sensitive_header_name(str(header_name)) for header_name in (spec.get("default_headers") or {})):
        raise TeamLlmConfigError(
            f"providers.{name}.default_headers cannot contain secrets; use auth",
        )
    _validate_declarative_provider_capabilities(name, spec)


def _validate_custom_http_provider(
    name: str,
    spec: dict[str, Any],
    *,
    ssrf_resolver: SsrfResolver | None,
) -> None:
    _validate_auth(name, spec.get("auth"))
    _validate_declarative_provider_capabilities(name, spec)
    default_headers = spec.get("default_headers") or {}
    if isinstance(default_headers, dict) and any(is_sensitive_header_name(str(header_name)) for header_name in default_headers):
        raise TeamLlmConfigError(
            f"providers.{name}.default_headers cannot contain secrets; use auth",
        )
    transports = spec.get("transports")
    if not isinstance(transports, dict) or not transports:
        raise TeamLlmConfigError(f"providers.{name}.transports must be a non-empty object")
    models = spec.get("models") or []
    for model in models:
        if not isinstance(model, dict) or not model.get("id"):
            raise TeamLlmConfigError(
                f"providers.{name}.models must use object entries for custom_http",
            )
        model_id = str(model["id"])
        caps = model.get("capabilities") or []
        refs = model.get("capability_transports")
        if not isinstance(refs, dict):
            raise TeamLlmConfigError(
                f"providers.{name}.models[{model_id}].capability_transports is required",
            )
        overrides = model.get("request_body_overrides") or {}
        if not isinstance(overrides, dict) or any(capability not in caps or not isinstance(value, dict) for capability, value in overrides.items()):
            raise TeamLlmConfigError(
                f"providers.{name}.models[{model_id}].request_body_overrides must map declared capabilities to JSON objects",
            )
        for capability in caps:
            if capability not in {"chat", "tool", "vision", "embed"}:
                continue
            try:
                purpose = "probe_embed" if capability == "embed" else f"probe_{capability}"
                plan = build_provider_transport_plan(
                    spec,
                    purpose=purpose,  # type: ignore[arg-type]
                    model_id=model_id,
                    capability=capability,
                )
                _validate_provider_base_url(
                    name,
                    plan.request_url,
                    ssrf_resolver=ssrf_resolver,
                )
                render_custom_http_body(
                    spec,
                    model_id=model_id,
                    capability=capability,
                    variables={
                        "model": model_id,
                        "deployment": str(model.get("deployment") or model_id),
                        "messages": [{"role": "user", "content": "probe"}],
                        "tools": [],
                        "tool_choice": None,
                        "stream": False,
                        "max_output_tokens": 1,
                        "input": ["probe"],
                    },
                )
            except ValueError as exc:
                raise TeamLlmConfigError(
                    f"providers.{name}.models[{model_id}].{capability}: {exc}",
                ) from exc


def _validate_declarative_provider_capabilities(
    name: str,
    spec: dict[str, Any],
) -> None:
    supported = {"chat", "tool", "vision", "embed"}
    for model in spec.get("models") or []:
        if not isinstance(model, dict):
            continue
        capabilities = model.get("capabilities") or []
        unsupported = {str(capability) for capability in capabilities if capability not in supported}
        if unsupported:
            raise TeamLlmConfigError(
                f"providers.{name}.models[{model.get('id')}].capabilities unsupported for {spec.get('type')}: {sorted(unsupported)[0]}",
            )


def _validate_provider_base_url(
    name: str,
    base_url: str,
    *,
    ssrf_resolver: SsrfResolver | None,
) -> None:
    """Apply the deployment-mode URL policy to model endpoints."""
    try:
        validate_url(base_url, resolver=ssrf_resolver)
    except (SsrfBlocked, ValueError) as exc:
        raise TeamLlmConfigError(f"providers.{name}.base_url blocked by deployment mode") from exc


def _validate_agent_spec(path: str, spec: Any, providers: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        raise TeamLlmConfigError(f"agents.{path} must be an object")
    _validate_context_window(f"agents.{path}.max_output_tokens", spec.get("max_output_tokens"))
    _pair_from_spec(spec.get("primary"), providers, f"agents.{path}.primary")
    fallback = spec.get("fallback") or []
    if not isinstance(fallback, list):
        raise TeamLlmConfigError(f"agents.{path}.fallback must be a list")
    for idx, item in enumerate(fallback):
        _pair_from_spec(item, providers, f"agents.{path}.fallback[{idx}]")


def _pair_from_spec(spec: Any, providers: dict[str, Any], path: str) -> tuple[str, str]:
    if not isinstance(spec, dict):
        raise TeamLlmConfigError(f"{path} must be an object")
    provider = spec.get("provider")
    model = spec.get("model")
    if not isinstance(provider, str) or not provider:
        raise TeamLlmConfigError(f"{path}.provider must be non-empty")
    if provider not in providers:
        raise TeamLlmConfigError(f"{path}.provider unknown: {provider}")
    if not isinstance(model, str) or not model:
        raise TeamLlmConfigError(f"{path}.model must be non-empty")
    declared = providers[provider].get("models") or []
    if declared:
        matched = next(
            (item for item in declared if (isinstance(item, str) and item == model) or (isinstance(item, dict) and item.get("id") == model)),
            None,
        )
        if isinstance(matched, dict):
            capabilities = matched.get("capabilities")
            if isinstance(capabilities, list) and "chat" not in capabilities:
                raise TeamLlmConfigError(
                    f"{path}.model {model!r} is not chat-capable",
                )
    return provider, model


def _validate_media(media: Any, providers: dict[str, Any]) -> None:
    """校验 team config 的 ``media:`` 段（P2 image_gen team 化，写路径严格）。

    media 是可选能力；缺省 = 无（image_gen 走全局 yaml）。有则 models 引用的 provider 必须
    存在、capability/protocol 在 media 词汇表、active 引用存在。provider **类型不限 chat**——
    media 端点（dashscope api/v1、ark api/v3）声明为 openai_compatible 即可，实际调用由 model
    条目的 ``protocol`` 决定（同 kb rerank 的 dashscope_native 套路）。"""
    if not media:
        return
    from bowhead_core.media.config_loader import MEDIA_CAPABILITIES, MEDIA_PROTOCOLS

    if not isinstance(media, dict):
        raise TeamLlmConfigError("media must be an object")
    models = media.get("models") or {}
    if not isinstance(models, dict):
        raise TeamLlmConfigError("media.models must be an object")
    for name, spec in models.items():
        if not isinstance(spec, dict):
            raise TeamLlmConfigError(f"media.models.{name} must be an object")
        provider = spec.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            raise TeamLlmConfigError(f"media.models.{name}.provider unknown: {provider}")
        real_id = spec.get("real_id") or spec.get("model")
        if not isinstance(real_id, str) or not real_id:
            raise TeamLlmConfigError(f"media.models.{name}.real_id required")
        if spec.get("capability") not in MEDIA_CAPABILITIES:
            raise TeamLlmConfigError(f"media.models.{name}.capability unsupported: {spec.get('capability')!r}")
        if spec.get("protocol") not in MEDIA_PROTOCOLS:
            raise TeamLlmConfigError(f"media.models.{name}.protocol unsupported: {spec.get('protocol')!r}")
    active = media.get("active") or {}
    if active and not isinstance(active, dict):
        raise TeamLlmConfigError("media.active must be an object")
    for cap, model_name in active.items():
        if cap not in MEDIA_CAPABILITIES:
            raise TeamLlmConfigError(f"media.active.{cap} unsupported")
        if model_name not in models:
            raise TeamLlmConfigError(f"media.active.{cap} references unknown model {model_name!r}")


def _validate_search(search: Any, providers: dict[str, Any]) -> None:
    """校验 team config 的 ``search:`` 段（web_search team 化，写路径严格，镜像 _validate_media）。

    search 是可选能力；缺省 = 无（该团队不可用 web_search）。有则 models 引用的 provider 必须
    存在、capability/protocol 在 search 词汇表、active 引用存在。provider 声明为
    openai_compatible 即可，实际端点由 model 条目的 ``protocol`` 决定。"""
    if not search:
        return
    from bowhead_core.search_config import SEARCH_CAPABILITIES, SEARCH_PROTOCOLS

    if not isinstance(search, dict):
        raise TeamLlmConfigError("search must be an object")
    models = search.get("models") or {}
    if not isinstance(models, dict):
        raise TeamLlmConfigError("search.models must be an object")
    for name, spec in models.items():
        if not isinstance(spec, dict):
            raise TeamLlmConfigError(f"search.models.{name} must be an object")
        provider = spec.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            raise TeamLlmConfigError(f"search.models.{name}.provider unknown: {provider}")
        real_id = spec.get("real_id") or spec.get("model")
        if not isinstance(real_id, str) or not real_id:
            raise TeamLlmConfigError(f"search.models.{name}.real_id required")
        if spec.get("capability") not in SEARCH_CAPABILITIES:
            raise TeamLlmConfigError(f"search.models.{name}.capability unsupported: {spec.get('capability')!r}")
        if spec.get("protocol") not in SEARCH_PROTOCOLS:
            raise TeamLlmConfigError(f"search.models.{name}.protocol unsupported: {spec.get('protocol')!r}")
    active = search.get("active") or {}
    if active and not isinstance(active, dict):
        raise TeamLlmConfigError("search.active must be an object")
    for cap, model_name in active.items():
        if cap not in SEARCH_CAPABILITIES:
            raise TeamLlmConfigError(f"search.active.{cap} unsupported")
        if model_name not in models:
            raise TeamLlmConfigError(f"search.active.{cap} references unknown model {model_name!r}")


def capability_config_schema(domain: str) -> dict[str, Any]:
    """Expose non-secret model declarations using the original domain vocabularies."""
    from bowhead_core.kb.retrieval_policy import retrieval_top_k_schema
    from bowhead_core.media.config_loader import MEDIA_CAPABILITIES, MEDIA_PROTOCOLS
    from bowhead_core.search_config import SEARCH_CAPABILITIES, SEARCH_PROTOCOLS

    capabilities, protocols = {
        "kb": (SUPPORTED_KB_CAPABILITIES, KB_PROTOCOLS),
        "media": (MEDIA_CAPABILITIES, MEDIA_PROTOCOLS),
        "search": (SEARCH_CAPABILITIES, SEARCH_PROTOCOLS),
    }[domain]
    properties = {
        "provider": {"type": "string", "description": "Existing Team connection supplying this capability model."},
        "real_id": {"type": "string", "minLength": 1, "description": "Exact remote model identifier exposed by the provider."},
        "model": {"type": "string", "description": "Legacy alias for the remote model identifier; existing records remain readable."},
        "capability": {
            "type": "string",
            "enum": sorted(capabilities),
            "description": "Capability supplied by this declaration; assignments must match it.",
        },
        "protocol": {
            "type": "string",
            "enum": sorted(protocols),
            "description": "Wire protocol supported by this capability and provider; original validation checks compatibility.",
        },
    }
    if domain == "kb":
        properties["dim"] = {
            "type": "integer",
            "minimum": 1,
            "description": "Embedding vector dimension. Changing the active embedding model requires the original reindex review.",
            "x-bowhead-field": {"unit": "count"},
        }
    result = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "models": {
                "type": "object",
                "description": "Capability model declarations keyed by their saved local model key.",
                "additionalProperties": {"type": "object", "properties": properties},
            },
            "active": {
                "type": "object",
                "description": "Active model keys by capability; original readiness and unassignment rules apply.",
                "additionalProperties": False,
                "properties": {
                    cap: {
                        "type": "string",
                        "description": "Select a declared model key with this capability. Use the original assignment workflow to clear or change it.",
                    }
                    for cap in sorted(capabilities)
                },
            },
        },
    }
    if domain == "kb":
        result["properties"]["retrieval_top_k"] = retrieval_top_k_schema()
    return result


def _validate_kb(kb: Any, providers: dict[str, Any]) -> None:  # noqa: PLR0912
    if not kb:
        return
    if not isinstance(kb, dict):
        raise TeamLlmConfigError("kb must be an object")
    from bowhead_core.kb.retrieval_policy import retrieval_top_k

    try:
        retrieval_top_k(kb)
    except ValueError as exc:
        raise TeamLlmConfigError(str(exc)) from exc
    models = kb.get("models") or {}
    if not isinstance(models, dict):
        raise TeamLlmConfigError("kb.models must be an object")
    for name, spec in models.items():
        if not isinstance(spec, dict):
            raise TeamLlmConfigError(f"kb.models.{name} must be an object")
        provider = spec.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            raise TeamLlmConfigError(f"kb.models.{name}.provider unknown: {provider}")
        capability = spec.get("capability")
        if capability not in SUPPORTED_KB_CAPABILITIES:
            raise TeamLlmConfigError(f"kb.models.{name}.capability unsupported: {capability!r}")
        real_id = spec.get("real_id") or spec.get("model")
        if not isinstance(real_id, str) or not real_id:
            raise TeamLlmConfigError(f"kb.models.{name}.real_id required")
        protocol = spec.get("protocol", "openai_compat")
        if protocol not in KB_PROTOCOLS:
            raise TeamLlmConfigError(f"kb.models.{name}.protocol unsupported: {protocol!r}")
        provider_spec = providers[provider]
        provider_type = provider_spec.get("type")
        if provider_type in {"azure_openai", "custom_http"}:
            if capability != "embed":
                raise TeamLlmConfigError(
                    f"kb.models.{name}.{capability} is not supported by {provider_type}",
                )
            declared = next(
                (model for model in provider_spec.get("models") or [] if isinstance(model, dict) and model.get("id") == real_id),
                None,
            )
            if not isinstance(declared, dict) or capability not in (declared.get("capabilities") or []):
                raise TeamLlmConfigError(
                    f"kb.models.{name} requires provider model {real_id!r} with capability {capability!r}",
                )
        if capability == "embed":
            try:
                dim = int(spec.get("dim"))
            except Exception as e:
                raise TeamLlmConfigError(f"kb.models.{name}.dim must be positive") from e
            if dim <= 0:
                raise TeamLlmConfigError(f"kb.models.{name}.dim must be positive")
    active = kb.get("active") or {}
    if active and not isinstance(active, dict):
        raise TeamLlmConfigError("kb.active must be an object")
    for capability, model_name in active.items():
        if capability not in SUPPORTED_KB_CAPABILITIES:
            raise TeamLlmConfigError(f"kb.active.{capability} unsupported")
        if model_name not in models:
            raise TeamLlmConfigError(f"kb.active.{capability} references unknown model {model_name!r}")
        if models[model_name].get("capability") != capability:
            raise TeamLlmConfigError(
                f"kb.active.{capability} references model with capability {models[model_name].get('capability')!r}",
            )
    pending = kb.get("pending_embed")
    if pending is not None:
        if not isinstance(pending, dict):
            raise TeamLlmConfigError("kb.pending_embed must be an object")
        for field in ("model", "target_index_version", "task_id"):
            if not isinstance(pending.get(field), str) or not pending[field]:
                raise TeamLlmConfigError(
                    f"kb.pending_embed.{field} must be non-empty",
                )
        pending_model = models.get(pending["model"])
        if not isinstance(pending_model, dict):
            raise TeamLlmConfigError(
                "kb.pending_embed.model references unknown model",
            )
        if pending_model.get("capability") != "embed":
            raise TeamLlmConfigError(
                "kb.pending_embed.model must reference an embed model",
            )


def provider_configs_from_v2(config: dict[str, Any]) -> list[ProviderConfig]:
    validate_v2_config(config, allow_legacy_duplicate_fallbacks=True)
    out: list[ProviderConfig] = []
    for name, spec in (config.get("providers") or {}).items():
        out.append(
            ProviderConfig(
                name=str(name),
                type=str(spec.get("type")),
                base_url=spec.get("base_url"),
                # SECURITY: never honor team-supplied env refs (arbitrary env
                # exfil incl Fernet key). Team uses plaintext api_key; platform
                # providers resolve refs via the trusted routing.yaml path.
                api_key_ref=None,
                api_key=spec.get("api_key"),
                default_headers=spec.get("default_headers") or {},
                timeout_sec=int(spec.get("timeout_sec", 60) or 60),
                supports=spec.get("supports") or {},
                models=copy.deepcopy(spec.get("models") or []),
                extra={
                    **{k: v for k, v in spec.items() if k not in PROVIDER_KNOWN_FIELDS},
                    "ssrf_guard": True,
                },
            )
        )
    return out


def agent_bindings_from_v2(config: dict[str, Any]) -> list[AgentBinding]:
    validate_v2_config(config, allow_legacy_duplicate_fallbacks=True)
    # Tier-shaped configuration: expand all bindings from AGENT_TIER_MAP (build_router / catalog /
    # default_model all use this function, so this single tier branch covers the entire chain).
    if is_tier_config(config):
        return tier_agent_bindings(config)
    bindings: list[AgentBinding] = []
    agents = config.get("agents") or {}
    for agent_type, spec in agents.items():
        if agent_type == "analyst" and isinstance(spec, dict):
            default = spec.get("default") or {}
            bindings.append(_binding_from_agent_spec("analyst", None, default))
            for profile_name, profile_spec in (spec.get("profiles") or {}).items():
                merged = {**default, **(profile_spec or {})}
                bindings.append(_binding_from_agent_spec("analyst", str(profile_name), merged))
        else:
            bindings.append(_binding_from_agent_spec(str(agent_type), None, spec or {}))
    return bindings


def _binding_from_agent_spec(
    agent_type: str,
    profile: str | None,
    spec: dict[str, Any],
) -> AgentBinding:
    primary = _binding_pair(spec.get("primary") or {})
    fallback = [_binding_pair(f) for f in (spec.get("fallback") or [])]
    return AgentBinding(
        agent_type=agent_type,
        profile=profile,
        primary=primary,
        fallback=fallback,
        extended_thinking=bool(spec.get("extended_thinking", False)),
        thinking_budget_tokens=int(spec.get("thinking_budget_tokens", 0) or 0),
        max_output_tokens=spec.get("max_output_tokens"),
        routing_policy=str(spec.get("routing_policy", "quality_first")),
    )


def _binding_pair(spec: dict[str, Any]) -> tuple[str, str]:
    return str(spec.get("provider", "")), str(spec.get("model", ""))


def build_router_from_v2_config(config: dict[str, Any], *, owner_team_id: str | None = None) -> Router:
    """从团队 v2 配置构建 Router。

    ``owner_team_id``（credit gap23 步1）：把团队身份钉进 router，让 usage chokepoint
    自取归属而不必让每个调用点申报。**唯一生产构造点是 `TeamRouterRegistry`**，它本就
    按 team_id 键控缓存，所以这个值它一直拿着；离线审计工具不传，保持 None。
    """
    if not is_v2_config(config):
        config = migrate_legacy_config_to_v2(config)
    registry = ProviderRegistry()
    registry.load(provider_configs_from_v2(config))
    router = Router(registry, owner_team_id=owner_team_id)
    bindings = agent_bindings_from_v2(config)
    for binding in bindings:
        router.bind(binding)
    router.set_context_windows(context_windows_for_catalog(config))
    from bowhead_core.rate_card import ModelCreditRates

    router.model_credit_rates = {
        (str(provider), str(model["id"])): ModelCreditRates.from_config(model.get("credit_rates")).as_json()
        for provider, spec in (config.get("providers") or {}).items()
        for model in (spec.get("models") or [])
        if isinstance(model, dict)
    }
    router.model_provider_types = {str(provider): str(spec.get("type")) for provider, spec in (config.get("providers") or {}).items()}
    from bowhead_router.retry import configured_max_retries

    router.max_retries = configured_max_retries(config)
    return router


def context_windows_for_catalog(config: dict[str, Any]) -> dict[tuple[str, str], int]:
    """Every catalog model's (provider, model) → context window.

    All catalog models are collected, not only bound targets, because ``user_pref`` lets members pick
    any chat model in the catalog. Undeclared windows use the reviewed catalog value, else the
    131,072 default (design agent-release-beginner-experience §8), so every model has one.
    """
    from bowhead_router.model_capabilities import effective_context_window

    providers = config.get("providers") if isinstance(config, dict) else None
    out: dict[tuple[str, str], int] = {}
    if not isinstance(providers, dict):
        return out
    for provider_name, provider in providers.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        for model in models or []:
            model_id = model if isinstance(model, str) else str((model or {}).get("id") or "") if isinstance(model, dict) else ""
            if not model_id:
                continue
            out[(str(provider_name), model_id)] = effective_context_window(providers, str(provider_name), model_id)
    return out


def build_catalog(
    *,
    config: dict[str, Any] | None = None,
    version: int = 0,
    loaded_config: LoadedRoutingConfig | None = None,
) -> dict[str, Any]:
    """Build a secret-free chat model catalog from team-owned config only.

    ``loaded_config`` remains as a short-lived compatibility keyword for stale
    callers and rollback fixtures, but is deliberately ignored.  A member
    catalog must never inherit a deployment routing manifest when the current
    team's row is missing, malformed, or unreadable.
    """
    if loaded_config is not None:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_team_model_resolution_total",
            labels={
                "entrypoint": "catalog",
                "result": "unsafe_deployment_fallback",
                "reason": "deployment_catalog_ignored",
            },
            help="Finite team model resolution outcomes by authenticated entrypoint.",
        )
    if config:
        candidate = config if is_v2_config(config) else migrate_legacy_config_to_v2(config)
        if is_v2_config(candidate):
            try:
                validate_v2_config(
                    candidate,
                    allow_legacy_duplicate_fallbacks=True,
                )
                return _catalog_from_v2(candidate, version=version)
            except TeamLlmConfigError:
                pass
    return {"version": version, "providers": [], "default_model": None, "kb": {}}


# WS1: per-model capability/cost catalog surface. `models: list[str]` stays
# chat-capable ids for back-compat (chat model-picker / allowed_chat_models read
# it); settings UI reads the richer additive `model_details`.
def _caps_from_supports(supports: dict[str, Any]) -> list[str]:
    """Derive per-model capabilities from provider-level `supports`.

    Legacy `list[str]` models carry no per-model signal → every model is assumed
    chat-capable and inherits the provider's declared tool/vision/think. We NEVER
    infer embed/rerank here (provider `supports` has no such signal and a union
    would mis-tag embedding models as chat); embed/rerank come only from an
    explicit model object or from kb.models.
    """
    order = ("chat", "tool", "vision", "think")
    capabilities = inferred_provider_capabilities(supports)
    return [capability for capability in order if capability in capabilities]


def _provider_model_details(spec: dict[str, Any], extra_ids: set[str]) -> list[dict[str, Any]]:
    inferred = _caps_from_supports(spec.get("supports") or {})
    out: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _add(mid: str, caps: list[str], cost: Any, source: str, verified: bool) -> None:
        if not mid or mid in out:
            return
        out[mid] = {
            "id": mid,
            "capabilities": caps,
            "cost": cost,
            "source": source,
            "verified": verified,
        }
        order.append(mid)

    for m in spec.get("models") or []:
        if isinstance(m, str):
            _add(m, list(inferred), None, "preset", False)
        elif isinstance(m, dict):
            model_id = str(m.get("id") or "")
            caps = effective_model_capabilities(
                {"provider": spec},
                "provider",
                model_id,
            )
            _add(
                model_id,
                list(caps or ()),
                m.get("cost"),
                str(m.get("source") or "manual"),
                bool(m.get("verified", m.get("source") == "preset")),
            )
    for mid in sorted(extra_ids):
        _add(mid, list(inferred), None, "preset", False)
    return [out[mid] for mid in order]


def _chat_capable_ids(details: list[dict[str, Any]]) -> list[str]:
    return sorted(d["id"] for d in details if "chat" in (d.get("capabilities") or []))


def _catalog_from_v2(config: dict[str, Any], *, version: int) -> dict[str, Any]:
    binding_models: dict[str, set[str]] = {}
    for binding in agent_bindings_from_v2(config):
        for provider, model in [binding.primary, *binding.fallback]:
            binding_models.setdefault(provider, set()).add(model)
    providers = []
    for name, spec in (config.get("providers") or {}).items():
        if not isinstance(spec, dict):
            continue
        details = _provider_model_details(spec, binding_models.get(str(name), set()))
        providers.append(
            {
                "id": str(name),
                "display_name": str(name),
                "type": spec.get("type"),
                "models": _chat_capable_ids(details),
                "model_details": details,
                "supports": spec.get("supports") or {},
            }
        )
    default_model = _default_lead_model(agent_bindings_from_v2(config))
    return {
        "version": version,
        "providers": providers,
        "default_model": default_model,
        "kb": _catalog_kb(config.get("kb") or {}),
    }


def _default_lead_model(bindings: list[AgentBinding]) -> dict[str, str] | None:
    for binding in bindings:
        if binding.agent_type == "lead" and binding.profile is None:
            return {"provider": binding.primary[0], "model": binding.primary[1]}
    return None


def _catalog_kb(kb: dict[str, Any]) -> dict[str, Any]:
    active = kb.get("active") or {}
    pending = kb.get("pending_embed")
    state = "pending" if pending else "idle"
    return {
        "active": active,
        "models": {
            name: {
                "provider": spec.get("provider"),
                "real_id": spec.get("real_id"),
                "capability": spec.get("capability"),
                **({"dim": spec.get("dim")} if spec.get("dim") is not None else {}),
            }
            for name, spec in (kb.get("models") or {}).items()
            if isinstance(spec, dict)
        },
        "reindex": {"state": state, "pending_embed": pending},
    }


def allowed_chat_models(catalog: dict[str, Any]) -> set[tuple[str, str]]:
    allowed: set[tuple[str, str]] = set()
    for provider in catalog.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        for model in provider.get("models") or []:
            if isinstance(model, str) and model:
                allowed.add((provider_id, model))
    return allowed


def is_allowed_preferred_model(
    catalog: dict[str, Any],
    provider: str | None,
    model: str | None,
) -> bool:
    if not provider or not model:
        return False
    return (provider, model) in allowed_chat_models(catalog)
