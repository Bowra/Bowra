"""Tests for bowhead_core.url_provenance (web_fetch 防 exfil gate)."""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.url_provenance import (
    extract_hosts,
    host_allowed,
    host_of,
    register_seen_hosts,
    seed_seen_hosts,
)


def test_host_of_normalizes() -> None:
    assert host_of("https://Docs.Python.ORG/3/x?a=1") == "docs.python.org"
    assert host_of("example.com.") == "example.com"  # Bare domain + trailing dot
    assert host_of("HTTP://EXAMPLE.com:8443/p") == "example.com"
    assert host_of("") == ""


def test_extract_hosts_urls_and_bare_domains() -> None:
    h = extract_hosts("看下 https://example.com/a 和 docs.python.org 这两个，还有 foo@bar.io")
    assert "example.com" in h
    assert "docs.python.org" in h
    assert "bar.io" in h  # Email domains count too (trusted source; extra allowances are harmless)


def test_extract_hosts_excludes_non_domains() -> None:
    h = extract_hosts("Python 3.13 的新特性，比如 e.g. 这种缩写，版本 v1.2")
    assert "3.13" not in h  # Nonalphabetic TLD
    assert not any(x.startswith("3.") for x in h)
    # 'e.g' has label/TLD too short to count as a domain.
    assert "e.g" not in h


def test_seed_and_host_allowed() -> None:
    ctx = SimpleNamespace()
    seed_seen_hosts(ctx, "打开 https://example.com 看看")
    assert host_allowed(ctx, "https://example.com/deep/path?q=1") is True  # Same host, different path
    assert host_allowed(ctx, "https://evil.com/leak") is False  # Unseen → reject


def test_host_allowed_none_when_unwired() -> None:
    # No seed → missing set → gate off (fail-open)
    assert host_allowed(SimpleNamespace(), "https://anything.com") is None


def test_register_seen_hosts_extends() -> None:
    ctx = SimpleNamespace()
    seed_seen_hosts(ctx, "no urls here")
    assert host_allowed(ctx, "https://docs.python.org/x") is False
    register_seen_hosts(ctx, ["https://docs.python.org/3/whatsnew/3.13.html", None, ""])
    assert host_allowed(ctx, "https://docs.python.org/x") is True  # Allow after web_search adds it


def test_register_seen_hosts_noop_when_unwired() -> None:
    ctx = SimpleNamespace()  # Not seeded
    register_seen_hosts(ctx, ["https://x.com"])  # Must not raise
    assert host_allowed(ctx, "https://x.com") is None
