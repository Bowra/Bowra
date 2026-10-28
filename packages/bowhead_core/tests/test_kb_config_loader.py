"""KB model registry behavior tests（KbModelRegistry / ModelSpec）.

credential-readiness R10：yaml 加载路径 `load_kb_registry` 已删（注册表由
team_kb_runtime 从团队 llm config 构建），本文件只守活着的 registry 行为：
active 解析、env override、capability 校验。
"""

from __future__ import annotations

import pytest
from bowhead_core.kb.config_loader import (
    KbModelConfigError,
    KbModelRegistry,
    ModelSpec,
)


def _spec(name: str, capability: str, *, dim: int | None = None) -> ModelSpec:
    return ModelSpec(
        name=name,
        capability=capability,  # type: ignore[arg-type]
        provider="p",
        real_id=f"real/{name}",
        base_url="https://models.example.com/v1",
        api_key="sk-test",
        timeout_sec=60,
        dim=dim,
    )


def _registry() -> KbModelRegistry:
    return KbModelRegistry(
        by_name={
            "v": _spec("v", "vision"),
            "e": _spec("e", "embed", dim=1024),
            "r": _spec("r", "rerank"),
        },
        active={"vision": "v", "embed": "e", "rerank": "r"},
    )


def test_get_active_resolves_bindings(monkeypatch):
    for var in ("BOWHEAD_KB_VISION", "BOWHEAD_KB_EMBED", "BOWHEAD_KB_RERANK"):
        monkeypatch.delenv(var, raising=False)
    reg = _registry()
    assert reg.get_active("vision").real_id == "real/v"
    assert reg.get_active("embed").dim == 1024
    assert reg.get_active("rerank").name == "r"


def test_env_override_active_binding(monkeypatch):
    reg = KbModelRegistry(
        by_name={"v": _spec("v", "vision"), "v2": _spec("v2", "vision")},
        active={"vision": "v"},
    )
    monkeypatch.setenv("BOWHEAD_KB_VISION", "v2")
    assert reg.get_active("vision").name == "v2"


def test_env_override_empty_string_falls_back(monkeypatch):
    monkeypatch.setenv("BOWHEAD_KB_VISION", "")  # empty == not set
    assert _registry().get_active("vision").name == "v"


def test_get_by_name_and_unknown_raises():
    reg = _registry()
    assert reg.get_by_name("v").provider == "p"
    with pytest.raises(KbModelConfigError, match="not in catalog"):
        reg.get_by_name("nonexistent")


def test_get_active_capability_mismatch_raises(monkeypatch):
    monkeypatch.setenv("BOWHEAD_KB_VISION", "r")  # rerank model bound to vision
    with pytest.raises(KbModelConfigError, match="can't bind"):
        _registry().get_active("vision")


def test_active_missing_no_env_raises(monkeypatch):
    monkeypatch.delenv("BOWHEAD_KB_RERANK", raising=False)
    reg = KbModelRegistry(by_name={"v": _spec("v", "vision")}, active={"vision": "v"})
    with pytest.raises(KbModelConfigError, match="kb.active.rerank missing"):
        reg.get_active("rerank")


def test_active_unknown_model_raises(monkeypatch):
    monkeypatch.delenv("BOWHEAD_KB_VISION", raising=False)
    reg = KbModelRegistry(by_name={}, active={"vision": "ghost"})
    with pytest.raises(KbModelConfigError, match="no such model"):
        reg.get_active("vision")
