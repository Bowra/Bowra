"""Origin normalization for the mutation same-origin guard.

The guard must distinguish schemes and normalize explicit default ports.
Tests use reserved example hosts and assert origin semantics independently
of the internal tuple representation.
"""

from __future__ import annotations

import pytest
from bowhead_gateway.auth_mutation_security import _origin


def same(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


@pytest.mark.unit
def test_default_port_is_the_same_origin() -> None:
    """★ 本次新逮到的：显式默认端口必须与省略端口同源。"""
    assert same("https://a.example.com", "https://a.example.com:443")
    assert same("http://a.example.com", "http://a.example.com:80")
    # Paths/trailing slashes do not affect origin.
    assert same("https://a.example.com/bowhead/", "https://a.example.com:443")


@pytest.mark.unit
def test_non_default_port_is_a_different_origin() -> None:
    """反向对照：真的换了端口仍然必须判不同源，别把闸修松了。"""
    assert not same("https://a.example.com", "https://a.example.com:8443")
    assert not same("http://a.example.com", "http://a.example.com:8080")
    # Default ports across schemes are not interchangeable (http:80 ≠ https:443).
    assert not same("http://a.example.com:80", "https://a.example.com:443")


@pytest.mark.unit
def test_scheme_mismatch_is_a_different_origin() -> None:
    """Different schemes remain different origins even on the same host."""
    assert not same("https://a.example.com", "http://a.example.com")


@pytest.mark.unit
def test_host_mismatch_is_a_different_origin() -> None:
    """域名 ↔ IP、以及不同主机名。"""
    assert not same("http://192.0.2.10", "http://demo.example.com")
    assert not same("https://a.example.com", "https://b.example.com")


@pytest.mark.unit
def test_case_is_normalized() -> None:
    assert same("HTTPS://A.Example.COM", "https://a.example.com")


@pytest.mark.unit
def test_userinfo_does_not_leak_into_origin() -> None:
    """★ netloc 还含 userinfo —— 按 RFC origin 里没有它。

    旧实现比的是裸 netloc，`https://u:p@a.com` 会被判成与 `https://a.com` 不同源；
    反过来说也意味着攻击者可以在 Origin 里塞 userinfo 来变换字符串。归一到
    (scheme, host, port) 之后两者同源，判定只认真正决定源的那三样。
    """
    assert same("https://user:pass@a.example.com", "https://a.example.com")
