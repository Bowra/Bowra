"""从 `routing.dev.yaml` 加载 Router 配置.

YAML 格式见 TD §4.3 / config/bi-connections.dev.yaml（BI 那份是同构的）。
M1 Day 2 支持：
    - providers 列表（name / type / base_url / api_key_ref / supports）
    - agents 绑定（primary / fallback / profile overrides）
    - Jinja-style `${VAR}` / `${VAR:default}` 环境变量替换
    - top-level `extends: <path>` 继承（child deep-merge 覆盖 base，list 整段替换）

启动校验：build_router_from_config 默认要求 REQUIRED_AGENT_TYPES 全绑定 + 每个
binding 引用的 provider 在 registry 里存在。缺失 raise RoutingConfigError。
切语言模型时漏配立刻爆，绝不再 runtime 静默降级到 heuristic（参考 #139）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router

if TYPE_CHECKING:
    from collections.abc import Iterable

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")


# All agent_type values actually used by the system; build_router_from_config validates them when models/YAML change.
# echo / judge are utility agents (intent classifier / eval judge); omitting them makes higher layers
# silently degrade (intent → keyword heuristic, judge → skip verification), so they must be listed here.
REQUIRED_AGENT_TYPES: tuple[str, ...] = (
    "lead",
    "modeler",
    "analyst",
    "echo",
    "judge",
    # Writing consolidation T3: the report writer (compose_report) uses the narrative binding; missing configuration fails at startup,
    # rather than raising KeyError on the first report composition (#139 fail-fast principle).
    "narrative",
)


class RoutingConfigError(ValueError):
    """yaml 配置不合法 / 必备 agent_type 缺绑 / provider 引用不存在."""


def _interpolate(value: Any) -> Any:
    """递归把 ``${VAR}`` / ``${VAR:default}`` 替换为环境变量值."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            var = m.group(1)
            default = m.group(2) or ""
            return os.environ.get(var, default)

        return _ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@dataclass
class LoadedRoutingConfig:
    providers: list[ProviderConfig]
    agent_bindings: list[AgentBinding]
    raw: dict[str, Any] = field(default_factory=dict)


def _validated_models(name: str, spec: dict) -> list:
    """routing.yaml 的模型声明在 boot 期校验 api_surface——fail-fast（review R4-P1）。

    这是第三条写入路（平台 provider 信任路），team-config 两条路都有值校验而这里
    没有 = 「拼写错静默跑 chat 面」的雷原样留着；与本模块「绝不 runtime 静默降级」
    的原则对齐：错就 boot 爆，不留到请求期。"""

    models = spec.get("models") or []
    capacity = spec.get("max_output_tokens")
    if capacity is not None and (type(capacity) is not int or capacity <= 0):
        raise ValueError(f"provider {name!r}.max_output_tokens must be a positive integer")
    for m in models:
        if not isinstance(m, dict):
            continue
        capacity = m.get("max_output_tokens")
        if capacity is not None and (type(capacity) is not int or capacity <= 0):
            raise ValueError(f"provider {name!r} models[{m.get('id')}].max_output_tokens must be a positive integer")
        surface = m.get("api_surface")
        if surface is not None and surface not in {"chat_completions", "responses"}:
            raise ValueError(f"provider {name!r} models[{m.get('id')}].api_surface invalid; allowed: chat_completions, responses")
        if surface == "responses" and str(spec.get("type") or "") != "openai_compatible":
            raise ValueError(f"provider {name!r} models[{m.get('id')}].api_surface is only supported for openai_compatible providers")
    return models


def load_routing_config(path: str | Path) -> LoadedRoutingConfig:
    """读 YAML 文件并返回结构化配置.

    支持 top-level ``extends: <path>`` 继承（路径相对当前 yaml 所在目录解析），
    base 先加载、child deep-merge 覆盖。dict 递归合并、list/标量整段替换
    （fallback 列表 / primary 元组这种不该拼接）。

    校验在 ``build_router_from_config`` 里做，loader 本身保持宽松（允许
    部分配置用于测试 / 单 agent 实验）.
    """
    raw = _load_raw_with_extends(Path(path), seen=set())
    raw = _interpolate(raw)

    # ─── Providers ───────────────────────────────────────────
    providers_section = raw.get("providers") or {}
    providers: list[ProviderConfig] = []
    for name, spec in providers_section.items():
        if not isinstance(spec, dict):
            raise ValueError(f"provider {name!r} spec must be mapping")
        providers.append(
            ProviderConfig(
                name=name,
                type=spec.get("type", "mock"),
                base_url=spec.get("base_url"),
                api_key_ref=spec.get("api_key_ref"),
                default_headers=spec.get("default_headers") or {},
                timeout_sec=int(spec.get("timeout_sec", 60) or 60),
                supports=spec.get("supports") or {},
                models=_validated_models(name, spec),
                extra={
                    k: v
                    for k, v in spec.items()
                    if k
                    not in {
                        "type",
                        "base_url",
                        "api_key_ref",
                        "default_headers",
                        "timeout_sec",
                        "max_retries",
                        "supports",
                        "models",
                    }
                },
            )
        )

    # ─── Agent bindings ──────────────────────────────────────
    agents_section = raw.get("agents") or {}
    bindings: list[AgentBinding] = []
    for agent_type, spec in agents_section.items():
        if not isinstance(spec, dict):
            continue

        # The analyst default + profiles structure.
        if agent_type == "analyst" and "default" in spec:
            default = spec["default"]
            bindings.append(_build_binding("analyst", None, default))
            for profile_name, profile_spec in (spec.get("profiles") or {}).items():
                # Merge the profile configuration with default (profile overrides default).
                merged = {**default, **(profile_spec or {})}
                bindings.append(_build_binding("analyst", profile_name, merged))
        else:
            bindings.append(_build_binding(agent_type, None, spec))

    return LoadedRoutingConfig(
        providers=providers,
        agent_bindings=bindings,
        raw=raw,
    )


def _build_binding(
    agent_type: str,
    profile: str | None,
    spec: dict[str, Any],
) -> AgentBinding:
    allowance = spec.get("max_output_tokens")
    if allowance is not None and (type(allowance) is not int or allowance <= 0):
        raise ValueError("Agent max_output_tokens must be a positive integer or automatic")
    primary = _pair(spec.get("primary") or {})
    fallback = [_pair(f) for f in (spec.get("fallback") or [])]
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


def _pair(spec: dict[str, Any]) -> tuple[str, str]:
    return (str(spec.get("provider", "")), str(spec.get("model", "")))


def _load_raw_with_extends(path: Path, *, seen: set[Path]) -> dict[str, Any]:
    """加载 yaml；若有 ``extends:`` 则先递归加载 base 再 deep-merge."""
    if not path.exists():
        raise FileNotFoundError(f"routing config not found: {path}")
    real = path.resolve()
    if real in seen:
        chain = " → ".join(str(p) for p in [*seen, real])
        raise RoutingConfigError(f"routing config extends cycle detected: {chain}")
    seen = seen | {real}

    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"routing config at {path} must be a YAML mapping")

    extends = raw.pop("extends", None)
    if extends is None:
        return raw

    if not isinstance(extends, str):
        raise RoutingConfigError(f"routing config {path}: 'extends' must be a string path, got {type(extends).__name__}")
    base_path = (path.parent / extends).resolve()
    base = _load_raw_with_extends(base_path, seen=seen)
    return _deep_merge(base, raw)


def _deep_merge(base: Any, override: Any) -> Any:
    """dict 递归合并、list/标量整段替换.

    list 不拼接的理由：fallback / primary 这类配置语义是"按这条链跑"，
    拼接会让继承链上的兜底意外残留，比硬替换更难排查.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(out[k], v) if k in out else v
        return out
    return override


def build_router_from_config(
    path: str | Path,
    *,
    required_agents: Iterable[str] | None = None,
) -> tuple[Router, ProviderRegistry, LoadedRoutingConfig]:
    """一行等价：load yaml → create registry → create router + bindings → 校验.

    校验：
      - ``required_agents``（默认 REQUIRED_AGENT_TYPES）每个都得有绑定，缺一就 raise；
      - 每个 binding 的 primary + fallback 引用的 provider 必须在 registry 里登记过.
    测试可以传 ``required_agents=()`` 跳过强制校验，但 prod 路径走默认.
    """
    config = load_routing_config(path)
    registry = ProviderRegistry()
    # media_only providers (image generation and other media endpoints, multimodal-v1) are consumed only by
    # bowhead_core.media.config_loader; registry.load eagerly builds adapters,
    # and unknown adapter types raise, so filter them here. An agent binding that mistakenly references a
    # media_only provider is rejected as an unknown provider by _validate_router_config below.
    registry.load([p for p in config.providers if p.type != "media_only"])
    router = Router(registry)
    for binding in config.agent_bindings:
        router.bind(binding)

    required = tuple(REQUIRED_AGENT_TYPES if required_agents is None else required_agents)
    _validate_router_config(config, registry, required_agents=required, source=str(path))
    return router, registry, config


def _validate_router_config(
    config: LoadedRoutingConfig,
    registry: ProviderRegistry,
    *,
    required_agents: tuple[str, ...],
    source: str,
) -> None:
    bound_types = {b.agent_type for b in config.agent_bindings}
    missing = [a for a in required_agents if a not in bound_types]
    if missing:
        raise RoutingConfigError(
            f"routing config {source}: missing required agent_type bindings: "
            f"{missing}. 切语言模型时漏配最容易踩这条 — 用 `extends:` 继承"
            f" routing.dev.yaml 只覆盖要换的 agent，别手抄全部."
        )

    known_providers = set(registry.list_names())
    bad: list[str] = []
    for b in config.agent_bindings:
        for prov, _model in [b.primary, *b.fallback]:
            if prov and prov not in known_providers:
                bad.append(f"{b.agent_type}/{b.profile or '*'} → unknown provider {prov!r}")
    if bad:
        raise RoutingConfigError(f"routing config {source}: bindings reference unknown providers:\n  - " + "\n  - ".join(bad))
