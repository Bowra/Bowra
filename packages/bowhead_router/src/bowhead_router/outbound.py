"""Apply outbound parameter declarations and bounded provider-driven adaptation.

The outbound layer handles model constraints without changing caller defaults or
inferring capabilities from user-defined model names. Precedence is base body,
request_body_overrides, explicit OutboundParams, then adaptation. Adaptation acts
only on parameters left in follow mode; an administrator's declaration wins."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

ParamMode = Literal["follow", "fixed", "omit", "cap"]
ProtocolFamily = Literal["openai", "openai_responses", "anthropic"]

# Parameters that can be declared or adjusted adaptively. **Intentionally limited to sampling and tool switches**:
# `messages` / `tools` / `model` are excluded: changing them changes request **semantics**, not shape.
OUTBOUND_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "max_tokens",
    "parallel_tool_calls",
)
_CAP_CAPABLE: frozenset[str] = frozenset({"max_tokens"})


class OutboundParamsError(ValueError):
    """配置形状非法 —— 由配置校验层转成可操作的保存期错误。"""


@dataclass(frozen=True, slots=True)
class ParamRule:
    """Define one parameter's treatment.

    Follow means no intervention: restoring the caller's value would overwrite an
    already-applied request_body_overrides value."""

    mode: ParamMode = "follow"
    value: float | int | bool | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"follow", "fixed", "omit", "cap"}:
            raise OutboundParamsError("unsupported outbound parameter mode; use follow, fixed, omit or cap")
        if self.mode in {"fixed", "cap"} and self.value is None:
            raise OutboundParamsError(f"outbound param mode {self.mode!r} requires a value")
        if self.mode in {"follow", "omit"} and self.value is not None:
            raise OutboundParamsError(f"outbound param mode {self.mode!r} must not carry a value")


FOLLOW = ParamRule()


@dataclass(frozen=True, slots=True)
class OutboundParams:
    """一个 (provider, model) 的完整声明。缺省全 `follow` = 今天的行为。"""

    rules: dict[str, ParamRule]

    def rule(self, param: str) -> ParamRule:
        return self.rules.get(param, FOLLOW)

    def is_declared(self, param: str) -> bool:
        """管理员是否对该参数表过态。自适应只在**没表态**时才插手。"""

        return self.rule(param).mode != "follow"


EMPTY_PARAMS = OutboundParams(rules={})


def parse_outbound_params(raw: Any, *, family: ProtocolFamily, where: str, strict_values: bool = False) -> OutboundParams:
    """Parse declarations, retaining legacy value semantics unless opted in."""

    if raw in (None, {}):
        return EMPTY_PARAMS
    if not isinstance(raw, dict):
        raise OutboundParamsError(f"{where} must be an object")
    rules: dict[str, ParamRule] = {}
    for param, spec in raw.items():
        if param not in OUTBOUND_PARAMS:
            raise OutboundParamsError(f"{where}.{param} is not an outbound parameter; allowed: {', '.join(OUTBOUND_PARAMS)}")
        if not isinstance(spec, dict):
            raise OutboundParamsError(f"{where}.{param} must be an object like {{'mode': 'omit'}}")
        mode = spec.get("mode", "follow")
        value = spec.get("value")
        if mode == "cap" and param not in _CAP_CAPABLE:
            raise OutboundParamsError(f"{where}.{param} does not support mode 'cap'")
        rule = ParamRule(mode=mode, value=value)  # type: ignore[arg-type]
        if strict_values and rule.mode in {"fixed", "cap"}:
            if param == "parallel_tool_calls":
                if not isinstance(value, bool):
                    raise OutboundParamsError(f"{where}.{param}.value must be a JSON boolean")
            elif isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise OutboundParamsError(f"{where}.{param}.value must be a finite number")
            elif param == "max_tokens" and (not isinstance(value, int) or value <= 0):
                raise OutboundParamsError(f"{where}.{param}.value must be a positive integer")
            elif param == "temperature" and not 0 <= value <= (1 if family == "anthropic" else 2):
                raise OutboundParamsError(f"{where}.{param}.value is outside the protocol range")
            elif param == "top_p" and not 0 < value <= 1:
                raise OutboundParamsError(f"{where}.{param}.value must be greater than zero and at most one")
        # Reject protocol differences at **validation time**, not runtime: Anthropic requires max_tokens.
        if family == "anthropic" and param == "max_tokens" and rule.mode == "omit":
            raise OutboundParamsError(f"{where}.max_tokens cannot be omitted: the Anthropic messages API requires it")
        rules[param] = rule
    return OutboundParams(rules=rules)


def resolve_outbound_params(
    provider: Any,
    *,
    model_id: str,
    family: ProtocolFamily,
) -> OutboundParams:
    """合并 provider 级默认与模型级覆盖（**模型级胜出**）。

    粒度必须到模型：同一个网关可以同时挂 GPT-5（拒 temperature）与 GPT-4（接受）。
    """

    provider_raw = provider.get("outbound_params") if isinstance(provider, dict) else getattr(provider, "outbound_params", None)
    merged = dict(parse_outbound_params(provider_raw, family=family, where="providers.outbound_params").rules)
    models = provider.get("models") if isinstance(provider, dict) else getattr(provider, "models", None)
    for model in models or []:
        if not isinstance(model, dict) or str(model.get("id") or "") != model_id:
            continue
        merged.update(
            parse_outbound_params(
                model.get("outbound_params"),
                family=family,
                where=f"providers.models[{model_id}].outbound_params",
            ).rules
        )
    return OutboundParams(rules=merged)


def apply_outbound_params(
    body: dict[str, Any],
    params: OutboundParams,
    *,
    family: ProtocolFamily,
) -> dict[str, Any]:
    """就地应用声明。`follow` 不动这个字段（见 `ParamRule` 的注释）。"""

    for param in OUTBOUND_PARAMS:
        rule = params.rule(param)
        if rule.mode == "follow":
            continue
        if rule.mode == "omit":
            _erase_param(body, param, family=family)
        elif rule.mode == "fixed":
            _write_param(body, param, rule.value, family=family)
        elif rule.mode == "cap":
            current = _read_param(body, param, family=family)
            if isinstance(current, int | float) and not isinstance(current, bool) and rule.value is not None and current > rule.value:
                _write_param(body, param, rule.value, family=family)
    return body


# ── Wire field mapping ─────────────────────────────────────────────────────────
# Map the same semantics to each protocol's fields. Anthropic parallel control is an **inverted** opt-out,
# nested in the tool_choice object (`disable_parallel_tool_use`).


# Responses surface: the semantic name remains `max_tokens` (admin declarations, learned constraints, and the allowlist all use it);
# the wire field is `max_output_tokens` (tech-design §8.4 follow-up 1). The other three parameters have the same names on both surfaces.
_RESPONSES_WIRE_NAMES: dict[str, str] = {"max_tokens": "max_output_tokens"}


def _wire_name(param: str, family: ProtocolFamily) -> str:
    if family == "openai_responses":
        return _RESPONSES_WIRE_NAMES.get(param, param)
    return param


def _write_param(body: dict[str, Any], param: str, value: Any, *, family: ProtocolFamily) -> None:
    if param == "parallel_tool_calls" and family == "anthropic":
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            choice["disable_parallel_tool_use"] = not bool(value)
        return
    body[_wire_name(param, family)] = value


def _erase_param(body: dict[str, Any], param: str, *, family: ProtocolFamily) -> None:
    if param == "parallel_tool_calls" and family == "anthropic":
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            choice.pop("disable_parallel_tool_use", None)
        return
    body.pop(_wire_name(param, family), None)


def _read_param(body: dict[str, Any], param: str, *, family: ProtocolFamily) -> Any:
    if param == "parallel_tool_calls" and family == "anthropic":
        choice = body.get("tool_choice")
        disabled = choice.get("disable_parallel_tool_use") if isinstance(choice, dict) else None
        return None if disabled is None else (not disabled)
    return body.get(_wire_name(param, family))


def wire_param_name(param: str, family: ProtocolFamily) -> str:
    """语义参数名 → 该 family 的线上字段名（chat/anthropic 恒等；responses 映射 max_tokens）。

    公开给 adapter/probe 的 request_body_overrides 合并用：管理员写的是 chat 语义键
    （存量 override 全长这样），翻面后不做翻译会双键并存 + 自适应重试删错键
    （实现 review R1-P1-1 实测链条）。"""

    return _wire_name(param, family)


def outbound_param_in_body(body: dict[str, Any], param: str, family: ProtocolFamily) -> bool:
    """`param`（语义名）是否真的在这次要发的 body 里 —— 按 family 的**线上名**查。

    给 adapter 的参数拒绝重试用：「丢掉它什么都没改变」的判定不能裸 `body.get(param)`，
    responses 面的 `max_tokens` 在 body 里叫 `max_output_tokens`（tech-design §8.7）。"""

    return _read_param(body, param, family=family) is not None


def outbound_params_digest(params: OutboundParams) -> str:
    """进 `config_signature` —— 声明变了，旧证据必须 stale。"""

    canonical = {param: [rule.mode, rule.value] for param, rule in sorted(params.rules.items()) if rule.mode != "follow"}
    if not canonical:
        return ""
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


# ── Adaptation: let provider errors correct the request shape ───────────────────
#
# Replaces `_chi_sniff` (only recognizes parallel_tool_calls, keys by provider name rather than model,
# `_HARDCODED_PATTERNS` is an empty list, `_DURABLE_REJECTION_MANIFEST_V1` is an empty frozenset
# ⇒ the durable path never matches). Two adaptive implementations must not coexist.
#
# State is in process memory, consistent with the existing single-worker assumption I-χ-SW1. After restart, the first request incurs another retry round trip,
# **but the user still sees success**. Persistence happens when a person clicks "Persist as configuration" in settings.

# The key is **transport identity** (protocol + host + model), not the provider name:
# `ProviderConfig.name` is a team-chosen configuration key, **without a team namespace**. Two teams
# can both have a source with the same local name pointing to completely different gateways. Keying by name applies constraints learned
# for team A to team B's requests. Transport identity is inherently correct: the same model on the same gateway has the same constraints.
_learned: dict[tuple[str, str, str], dict[str, datetime]] = {}
_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class LearnedAdaptation:
    """一条已生效的自适应约束（供设置页展示与「固化为配置」）。"""

    param: str
    since: datetime


def _identity(adapter_type: str, base_url: str, model_id: str) -> tuple[str, str, str]:
    return (adapter_type, (urlsplit(base_url).hostname or base_url).lower(), model_id)


# Fallback when no structured `param` is present (older providers return only prose). **Register only empirically verified patterns**,
# requiring nearby "unsupported" context to avoid false triggers such as `rate limit on parallel requests`.
_FALLBACK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "parallel_tool_calls",
        re.compile(
            r"parallel[_\s\-]?tool[_\s\-]?calls"
            r"|parallel.{0,30}(?:not[_\s\-]?support|unknown|unrecognized|invalid|unsupported)"
            r"|(?:unknown|unrecognized|invalid).{0,30}parallel",
            re.IGNORECASE,
        ),
    ),
    (
        "temperature",
        re.compile(
            r"temperature.{0,60}(?:not[_\s\-]?support|unsupported|only.{0,20}default|must be)"
            r"|(?:unsupported|invalid).{0,30}temperature",
            re.IGNORECASE,
        ),
    ),
)


def rejected_param(
    error_body: str | bytes | None,
    payload: Any = None,
    *,
    allow_prose: bool = True,
    family: ProtocolFamily | None = None,
) -> str | None:
    """从 400 正文里认出**被拒的参数名**。

    以 provider 的结构化 `error.param` 为准（OpenAI 兼容错误体本就带 `type`/`code`/`param`）；
    没有它才退回关键词。返回值一定在 `OUTBOUND_PARAMS` 白名单内 —— 上游正文是不可信输入，
    绝不能让它点名 `messages` 之类然后被我们照做。

    ★ `allow_prose=False` 时**只认结构化指认**。调用方在有 durable attempt（这次调用已经
    进了账本）时必须这么传：散文里出现参数名是**歧义**的 —— `rate limit while processing
    parallel_tool_calls` 说的是限流，请求很可能**已经生效**，此时重发就是「钱扣了两次」。
    没有账本时（重发不产生记账歧义）才允许按散文猜。
    """

    if payload is None and error_body:
        raw = error_body.decode("utf-8", errors="replace") if isinstance(error_body, bytes) else error_body
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        detail = error if isinstance(error, dict) else payload
        param = str(detail.get("param") or "").strip()
        if family == "openai_responses":
            # Normalize wire names to semantic names (`max_output_tokens` → `max_tokens`) **only in the Responses
            # surface caller context**. Two independent reviews caught the original unconditional normalization (R1-P2-1 / R2-B4):
            # chat overrides can legitimately put `max_output_tokens` in the body (Gemini-style gateways);
            # unconditional normalization makes a 400 remove valid `max_tokens` and permanently learn the wrong in-process constraint.
            # "No chat provider uses this name" is an empirical assumption, not a structural guarantee.
            param = next((k for k, v in _RESPONSES_WIRE_NAMES.items() if v == param), param)
        if param in OUTBOUND_PARAMS:
            return param
    if not error_body or not allow_prose:
        return None
    text = error_body.decode("utf-8", errors="replace") if isinstance(error_body, bytes) else error_body
    for param, pattern in _FALLBACK_PATTERNS:
        if pattern.search(text):
            return param
    return None


def learn_constraint(*, adapter_type: str, base_url: str, model: str, param: str) -> bool:
    """记住「该网关的该模型不接受这个参数」。返回 True 表示首次（调用方据此打一条日志）。"""

    if param not in OUTBOUND_PARAMS:
        raise OutboundParamsError(f"refusing to learn a non-outbound parameter: {param!r}")
    key = _identity(adapter_type, base_url, model)
    with _lock:
        learned = _learned.setdefault(key, {})
        first = param not in learned
        if first:
            learned[param] = datetime.now(UTC)
    return first


def learned_constraints(*, adapter_type: str, base_url: str, model: str) -> frozenset[str]:
    with _lock:
        return frozenset(_learned.get(_identity(adapter_type, base_url, model), {}))


def learned_adaptations(*, adapter_type: str, base_url: str, model: str) -> tuple[LearnedAdaptation, ...]:
    """供设置页读取：这个模型上有哪些参数被自动适配掉了，各自从什么时候起。

    只返回参数名与时间 —— 没有地址、没有凭据、没有 provider 的原始报错正文。
    """

    with _lock:
        learned = dict(_learned.get(_identity(adapter_type, base_url, model), {}))
    return tuple(LearnedAdaptation(param=param, since=since) for param, since in sorted(learned.items()))


def apply_learned_constraints(
    body: dict[str, Any],
    *,
    adapter_type: str,
    base_url: str,
    model: str,
    params: OutboundParams,
    family: ProtocolFamily,
) -> dict[str, Any]:
    """把已学到的约束应用到 body —— **只碰管理员没表态的参数**。"""

    for param in learned_constraints(adapter_type=adapter_type, base_url=base_url, model=model):
        if params.is_declared(param):
            continue  # Human declarations > machine observations.
        _erase_param(body, param, family=family)
    return body


def reset_learned_for_test() -> None:
    with _lock:
        _learned.clear()


__all__ = [
    "EMPTY_PARAMS",
    "FOLLOW",
    "OUTBOUND_PARAMS",
    "LearnedAdaptation",
    "OutboundParams",
    "OutboundParamsError",
    "ParamRule",
    "apply_learned_constraints",
    "apply_outbound_params",
    "learn_constraint",
    "learned_adaptations",
    "learned_constraints",
    "outbound_param_in_body",
    "outbound_params_digest",
    "parse_outbound_params",
    "rejected_param",
    "reset_learned_for_test",
    "resolve_outbound_params",
    "wire_param_name",
]
