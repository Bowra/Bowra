"""KB 能力的「声明了吗」必须问 `kb.models`，不能问 `provider["models"]`。

116 2026-08-15 真机：配置里 `aliyun_qwen.models = ["qwen3.6-plus", "qwen3.6-flash"]`，
而 KB 的 embed/vision 用的是 `text-embedding-v4` / `qwen-vl-ocr-latest`，rerank 用的
`aliyun_dashscope_native` **连 models 键都没有**。于是 readiness 对这三条恒判
`required_capability_not_declared` 并**硬拦**，团队永远存不上模型配置。

而配置校验的权威实现 `_validate_kb` 判 KB 模型合法只看 `kb.models[*].capability` +
provider 存在，从不要求它出现在 provider 的 models 列表里 —— 于是同一个问题
（这个能力声明了吗）有两份实现、结论相反：页面左边「配置检查：没有阻塞问题，可以应用」，
右边「N 项阻止应用」。用户最初报的「同时说通过又说不让存」就是这么来的。

判据收敛到 KB 自己的声明。本文件同时钉住**没有跟着放松**的那一面：tier 行仍然照旧查
provider 声明。
"""

from __future__ import annotations

import base64

import pytest
from bowhead_core.team_llm_readiness import build_required_assignments

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
PROVIDER_IDS = {"main": f"llmp_{'1' * 32}", "native": f"llmp_{'2' * 32}"}


@pytest.fixture(autouse=True)
def _fernet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)


def _config() -> dict:
    """复刻 116 的形状：KB 的 real_id 不在 provider 的 models 列表里。"""
    return {
        "schema_version": 2,
        "providers": {
            "main": {
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "secret",
                # Lists only chat models, as observed on 116.
                "models": ["gpt-main", "gpt-fast"],
            },
            # The aliyun_dashscope_native provider on 116 has no models key at all.
            "native": {
                "type": "openai_compatible",
                "base_url": "https://api.example.com/api/v1",
                "api_key": "secret",
            },
        },
        "tiers": {
            "primary": {"provider": "main", "model": "gpt-main"},
            "fast": {"follow": "primary"},
            "judge": {"provider": "main", "model": "gpt-main"},
        },
        "kb": {
            "models": {
                "embed-1": {"provider": "main", "real_id": "text-embedding-v4", "capability": "embed", "dim": 1024},
                "vision-1": {"provider": "main", "real_id": "vl-ocr-latest", "capability": "vision"},
                "rerank-1": {"provider": "native", "real_id": "gte-rerank-v2", "capability": "rerank"},
            },
            "active": {"embed": "embed-1", "vision": "vision-1", "rerank": "rerank-1"},
        },
    }


def _by_role(config: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for a in build_required_assignments(config, provider_ids=PROVIDER_IDS):
        out.setdefault(a.role_key, []).append(a)
    return out


def test_kb_capabilities_are_declared_by_the_kb_block() -> None:
    roles = _by_role(_config())

    for role in ("kb.embed", "kb.vision", "kb.rerank"):
        assert roles[role], f"缺少 {role} 行"
        assert all(a.declared for a in roles[role]), f"{role} 被判为未声明 —— 它的声明在 kb.models 里，不在 provider['models'] 里"


def test_tier_declaration_still_comes_from_the_provider() -> None:
    """负对照：本次只该改 KB 行。tier 行引用一个 provider 没列的模型仍须判未声明。

    没有这条，把 declared 一律置真也会绿。
    """
    config = _config()
    config["tiers"]["primary"] = {"provider": "main", "model": "not-in-provider-list"}

    roles = _by_role(config)

    assert roles["tier.primary"]
    assert all(not a.declared for a in roles["tier.primary"])


def test_row_extractor_declaration_does_not_depend_on_the_provider_list() -> None:
    """row_extractor 同属 KB：即便它的 real_id 不在 provider 列表里也算已声明。"""
    config = _config()
    config["kb"]["models"]["row-1"] = {
        "provider": "main",
        "real_id": "some-row-model",
        "capability": "row_extractor",
    }
    config["kb"]["active"]["row_extractor"] = "row-1"

    roles = _by_role(config)

    assert roles["kb.row_extractor"]
    assert {a.capability for a in roles["kb.row_extractor"]} == {"chat", "tool"}
    assert all(a.declared for a in roles["kb.row_extractor"])
