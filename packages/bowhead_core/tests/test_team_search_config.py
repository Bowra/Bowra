"""Tests for team-config search (web_search) — web_search team 化。

schema（validate_v2_config 接受/拒绝 search）+ build_team_search_registry（team BYO →
ssrf_guard=True、明文 key、protocol 透传）。整体镜像 test_team_media_config。
"""

from __future__ import annotations

import pytest
from bowhead_core.search_config import build_team_search_registry
from bowhead_core.team_llm_config import TeamLlmConfigError, validate_v2_config


def _resolver(_host: str) -> list[str]:
    # Fake SSRF resolver: any host → public IP, skipping real DNS.
    return ["93.184.216.34"]


def _cfg() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "deepseek": {
                "type": "anthropic_compatible",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "k",
                "models": ["deepseek-v4-pro"],
            },
            "aliyun": {"type": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-search"},
        },
        "tiers": {"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}},
        "search": {
            "models": {
                "qwen-plus-search": {"provider": "aliyun", "real_id": "qwen-plus", "capability": "web_search", "protocol": "dashscope_compat"}
            },
            "active": {"web_search": "qwen-plus-search"},
        },
    }


def test_validate_accepts_search() -> None:
    validate_v2_config(_cfg(), ssrf_resolver=_resolver)  # No exception = search section accepted.


def test_no_search_is_optional() -> None:
    cfg = _cfg()
    del cfg["search"]
    validate_v2_config(cfg, ssrf_resolver=_resolver)  # Omitting search is valid (web_search is unavailable to this team).


def test_build_team_search_registry() -> None:
    reg = build_team_search_registry(_cfg())
    spec = reg.get_active("web_search")
    assert spec is not None
    assert spec.real_id == "qwen-plus"
    assert spec.protocol == "dashscope_compat"
    assert spec.provider == "aliyun"
    assert spec.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert spec.api_key == "sk-search"  # Team plaintext key
    assert spec.ssrf_guard is True  # Team BYO → SSRF guard required.


def test_build_empty_when_no_search() -> None:
    cfg = _cfg()
    del cfg["search"]
    reg = build_team_search_registry(cfg)
    assert reg.get_active("web_search") is None


def test_build_empty_when_key_missing() -> None:
    """provider 无 key → spec.api_key 空 = soft-disabled（注册/handler 视为未配置）。"""
    cfg = _cfg()
    cfg["providers"]["aliyun"]["api_key"] = ""
    reg = build_team_search_registry(cfg)
    spec = reg.get_active("web_search")
    assert spec is not None and spec.api_key == ""


def test_validate_rejects_bad_protocol() -> None:
    cfg = _cfg()
    cfg["search"]["models"]["qwen-plus-search"]["protocol"] = "bogus"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_bad_capability() -> None:
    cfg = _cfg()
    cfg["search"]["models"]["qwen-plus-search"]["capability"] = "news_search"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_unknown_provider() -> None:
    cfg = _cfg()
    cfg["search"]["models"]["qwen-plus-search"]["provider"] = "ghost"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_active_dangling() -> None:
    cfg = _cfg()
    cfg["search"]["active"]["web_search"] = "nope"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)
