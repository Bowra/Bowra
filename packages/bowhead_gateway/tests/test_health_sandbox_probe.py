"""`/health` 的沙箱 workdir 探针**永不承重**。

code review 2026-08-11 逮出：`_sandbox_workdir_probe` 的 docstring 声称 fail soft，
但代码里没有任何兜底，而它做两件会抛的事——

1. 函数内 `from bowhead_skill.runtime import ...`：`bowhead_skill` **不是**
   `bowhead_gateway` 的声明依赖（单 venv 的应用镜像里能解析，只装 gateway 的装法会
   ImportError）；
2. 真去 bind mount 上 mkdtemp 探测：宿主策略 / 只读挂载 / 路径含怪字符都可能抛出
   `_workdir_root_unusable_reason` 的 `except OSError` 接不住的东西。

代价不对称是关键：`/health` 是 compose healthcheck 的落点，这里漏一个异常 = 容器永远
不 healthy = 整个部署卡住并且看起来像「应用坏了」，而换来的只是一个诊断字段。所以探
不出来必须退化成「没解析出共享根」（`("", None)`），绝不上抛。

消融：把 `_sandbox_workdir_probe` 里的 try/except 摘掉，这两条必须变红。
"""

from __future__ import annotations

import pytest
from bowhead_gateway.routes.health import _health_runtime_info, _sandbox_workdir_probe


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """探针是 `lru_cache(maxsize=1)` 的——不清缓存，第二条测试读到的是第一条的结果。"""
    _sandbox_workdir_probe.cache_clear()
    yield
    _sandbox_workdir_probe.cache_clear()


def test_probe_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """底层探测炸了 → 返回 `("", None)`，不上抛。"""
    import bowhead_skill.runtime as _rt

    def _boom(*_a, **_kw):
        raise RuntimeError("bind mount 上的宿主策略拒绝了这次探测")

    monkeypatch.setattr(_rt, "_resolve_workdir_root", _boom)

    assert _sandbox_workdir_probe() == ("", None)


def test_health_runtime_info_survives_a_broken_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针炸了，`/health` 的 runtime_info 仍要完整构造出来（含这两个键）。

    直接测 `_health_runtime_info` 而不是打 TestClient：要验的是「异常不会冒到端点」，
    端点自己有没有别的 500 与本条无关。
    """
    import bowhead_skill.runtime as _rt

    monkeypatch.setattr(
        _rt,
        "_resolve_workdir_root",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("EIO")),
    )

    info = _health_runtime_info()

    assert info["sandbox_workdir_writable"] is None


def test_health_runtime_info_only_reports_safe_executor_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor_url = "http://sandbox-executor.internal:8091"
    executor_token = "executor-secret-token"
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", executor_url)
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", executor_token)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", "/srv/secret-sandbox")

    info = _health_runtime_info()

    assert info["sandbox_executor_url_configured"] is True
    assert info["sandbox_executor_token_configured"] is True
    assert executor_url not in str(info)
    assert executor_token not in str(info)
    assert "/srv/secret-sandbox" not in str(info)
