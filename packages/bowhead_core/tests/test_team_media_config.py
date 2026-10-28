"""Tests for team-config media (image_gen) — P2 image_gen team 化。

schema（validate_v2_config 接受/拒绝 media）+ build_team_media_registry（team BYO →
ssrf_guard=True、明文 key、protocol 透传）。
"""

from __future__ import annotations

import pytest
from bowhead_core.media.config_loader import build_team_media_registry
from bowhead_core.team_llm_config import TeamLlmConfigError, validate_v2_config


def _resolver(_host: str) -> list[str]:
    # Fake SSRF resolver: any host → public IP, bypassing real DNS (media endpoint base_url is outside the trusted allowlist).
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
            # Media-only provider: no chat models → omit models (an empty list [] is rejected by _validate_provider_models).
            "qwen_media": {"type": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/api/v1", "api_key": "sk-media"},
        },
        "tiers": {"primary": {"provider": "deepseek", "model": "deepseek-v4-pro"}},
        "media": {
            "models": {
                "qwen-image": {"provider": "qwen_media", "real_id": "qwen-image-plus", "capability": "image_gen", "protocol": "dashscope_async"}
            },
            "active": {"image_gen": "qwen-image"},
        },
    }


def test_validate_accepts_media() -> None:
    validate_v2_config(_cfg(), ssrf_resolver=_resolver)  # No exception = media section accepted.


def test_no_media_is_optional() -> None:
    cfg = _cfg()
    del cfg["media"]
    validate_v2_config(cfg, ssrf_resolver=_resolver)  # Missing media is valid (uses global yaml).


def test_build_team_media_registry() -> None:
    reg = build_team_media_registry(_cfg())
    spec = reg.get_active("image_gen")
    assert spec is not None
    assert spec.real_id == "qwen-image-plus"
    assert spec.protocol == "dashscope_async"
    assert spec.provider == "qwen_media"
    assert spec.base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert spec.api_key == "sk-media"  # Team plaintext key.
    assert spec.ssrf_guard is True  # Team BYO requires the SSRF guard.


def test_build_empty_when_no_media() -> None:
    cfg = _cfg()
    del cfg["media"]
    reg = build_team_media_registry(cfg)
    assert reg.get_active("image_gen") is None


def test_validate_rejects_bad_protocol() -> None:
    cfg = _cfg()
    cfg["media"]["models"]["qwen-image"]["protocol"] = "bogus"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_bad_capability() -> None:
    cfg = _cfg()
    cfg["media"]["models"]["qwen-image"]["capability"] = "video_gen"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_unknown_provider() -> None:
    cfg = _cfg()
    cfg["media"]["models"]["qwen-image"]["provider"] = "ghost"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)


def test_validate_rejects_active_dangling() -> None:
    cfg = _cfg()
    cfg["media"]["active"]["image_gen"] = "nope"
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(cfg, ssrf_resolver=_resolver)
