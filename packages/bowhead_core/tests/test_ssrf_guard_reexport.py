"""`bowhead_core.ssrf_guard` re-export 兼容面守卫。

policy 实现与全量单测矩阵在 `bowhead_router`（tests/test_ssrf_guard.py）；
这里只守两件事：(1) 历史 import 路径的公共符号一个不少且与 router 侧同一对象
（不是第二份实现）；(2) shim 路径上 policy 真的生效（一 allow 一 block 冒烟）。
"""

from __future__ import annotations

import pytest

from bowhead_core import ssrf_guard as core_ssrf
from bowhead_router import ssrf_guard as router_ssrf

_PUBLIC_SYMBOLS = [
    "SMTP_PORTS",
    "Resolver",
    "SmtpPortBlocked",
    "SsrfBlocked",
    "SsrfGuardedAsyncTransport",
    "ValidatedTarget",
    "build_guarded_async_client",
    "resolve_and_validate",
    "validate_smtp_target",
    "validate_url",
]


@pytest.mark.parametrize("name", _PUBLIC_SYMBOLS)
def test_reexport_is_same_object(name):
    assert getattr(core_ssrf, name) is getattr(router_ssrf, name)


def test_saas_policy_effective_via_core_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    t = core_ssrf.validate_url("https://api.datarc.cn/v1", resolver=lambda host: ["8.8.8.8"])
    assert t.ips == ("8.8.8.8",)
    with pytest.raises(core_ssrf.SsrfBlocked):
        core_ssrf.validate_url("https://evil.example/", resolver=lambda host: ["10.0.0.5"])
