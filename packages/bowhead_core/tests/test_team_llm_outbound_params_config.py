"""出站参数声明的**保存期**校验。

写错的声明必须在保存那一刻被拒，而不是留到每个请求的 `_build_body` 里炸 ——
那时既没有可操作的提示，也已经太晚（整条聊天链路不可用）。

设计见 `docs/sprints/llm-outbound-params-2026-08/tech-design.md`。
"""

from __future__ import annotations

import pytest
from bowhead_core.team_llm_config import TeamLlmConfigError, validate_v2_config


def _resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _cfg(provider: dict) -> dict:
    return {
        "schema_version": 2,
        "providers": {"p": provider},
        "tiers": {"primary": {"provider": "p", "model": "m"}},
    }


def _openai(models: list[dict]) -> dict:
    return {"type": "openai_compatible", "base_url": "https://api.example.com/v1", "api_key": "k", "models": models}


def _anthropic(models: list[dict]) -> dict:
    return {"type": "anthropic_compatible", "base_url": "https://api.example.com", "api_key": "k", "models": models}


@pytest.mark.unit
def test_accepts_a_declaration_on_provider_and_model() -> None:
    provider = _openai([{"id": "m", "capabilities": ["chat"], "outbound_params": {"temperature": {"mode": "omit"}}}])
    provider["outbound_params"] = {"max_tokens": {"mode": "cap", "value": 4096}}

    validate_v2_config(_cfg(provider), ssrf_resolver=_resolver)


@pytest.mark.unit
def test_rejects_omitting_max_tokens_on_anthropic() -> None:
    """协议差异在校验期拒：Anthropic 的 `max_tokens` 必填，删掉请求根本不合法。"""
    provider = _anthropic([{"id": "m", "capabilities": ["chat"], "outbound_params": {"max_tokens": {"mode": "omit"}}}])

    with pytest.raises(TeamLlmConfigError, match="max_tokens"):
        validate_v2_config(_cfg(provider), ssrf_resolver=_resolver)

    # Positive control: the same declaration is valid for openai (otherwise the preceding test might merely reject it everywhere).
    validate_v2_config(
        _cfg(_openai([{"id": "m", "capabilities": ["chat"], "outbound_params": {"max_tokens": {"mode": "omit"}}}])),
        ssrf_resolver=_resolver,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "declaration",
    [
        {"messages": {"mode": "omit"}},  # Outside the allowlist: changes semantics rather than shape.
        {"model": {"mode": "fixed", "value": "x"}},
        {"temperature": {"mode": "nope"}},
        {"temperature": {"mode": "fixed"}},  # Missing value.
        {"temperature": {"mode": "cap", "value": 1}},  # Only max_tokens supports cap.
        {"temperature": 0.7},  # Not an object.
    ],
)
def test_rejects_illegal_declarations(declaration: dict) -> None:
    provider = _openai([{"id": "m", "capabilities": ["chat"], "outbound_params": declaration}])

    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(_cfg(provider), ssrf_resolver=_resolver)


@pytest.mark.unit
def test_absent_declaration_stays_valid() -> None:
    """默认零变化：不写这一段的配置必须照常保存（既有部署全是这种）。"""
    validate_v2_config(_cfg(_openai([{"id": "m", "capabilities": ["chat"]}])), ssrf_resolver=_resolver)
