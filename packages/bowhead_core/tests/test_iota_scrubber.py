"""Sprint ι (PRD §5 R-γ): central scrubber tests."""

from __future__ import annotations

import pytest
from bowhead_core.scrubber import scrub, scrub_with_count


# ─── 4 patterns × positive ──────────────────────────────────────
@pytest.mark.parametrize(
    "text,label",
    [
        ("身份证号 110101199003078517 张三", "身份证"),
        ("电话 13912345678 联系", "手机号"),
        ("邮箱 alice@example.com 注册", "邮箱"),
        ("API key sk-abcdefghijklmnop12345 leaked", "api_key"),
    ],
)
def test_positive_match(text: str, label: str) -> None:
    out, counts = scrub_with_count(text)
    assert f"[REDACTED:{label}]" in out
    assert counts.get(label) == 1


# ─── 4 patterns × negative (no false hits on benign text) ──────
@pytest.mark.parametrize(
    "text",
    [
        "短数字 12345",  # too short to look like ID/phone
        "no email here just text",  # no @ sign
        "sk- short",  # missing alphanumeric
        "1234567890",  # 10 digits, not 11
    ],
)
def test_negative_no_hit(text: str) -> None:
    out, counts = scrub_with_count(text)
    assert out == text
    assert counts == {}


# ─── Multi-hit ──────────────────────────────────────────────────
def test_multi_hit_counts() -> None:
    text = "电话 13912345678 和 13800001111 都打不通"
    _, counts = scrub_with_count(text)
    assert counts.get("手机号") == 2


# ─── Idempotence ────────────────────────────────────────────────
def test_idempotent() -> None:
    text = "用户 alice@example.com"
    once = scrub(text)
    twice = scrub(once)
    assert once == twice


def test_empty_input() -> None:
    assert scrub("") == ""
    assert scrub_with_count("") == ("", {})
