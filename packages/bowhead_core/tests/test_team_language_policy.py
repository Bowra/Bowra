"""团队级输出语言策略（多语言批6-C）。

背景：批6-B 的 `follow_input` 默认**只对新注册的人生效**，存量团队没有任何口子
（`PUT /api/v1/user-settings/{key}` 只能改自己，admin 侧无代改端点）。本模块补的是
团队这一层，形状对齐 theme policy 的 `defaults` / `locks`。
"""

from __future__ import annotations

import pytest
from bowhead_core.language_context import LanguageContext
from bowhead_core.team_language_policy import (
    SOURCE_LOCK,
    SOURCE_NONE,
    SOURCE_TEAM_DEFAULT,
    SOURCE_USER,
    TeamLanguagePolicyError,
    normalize_team_language_policy,
    output_language_decision,
    parse_team_language_policy,
)

_FOLLOW = '{"version":1,"defaults":{"output_language":"follow_input"}}'
_LOCKED_EN = '{"version":1,"defaults":{"output_language":"en-US"},"locks":{"output_language":true}}'


# ─────────── Mode selection ───────────


def test_no_policy_keeps_user_setting_byte_for_byte() -> None:
    """没有策略 = 旧行为。这是不挂 flag 的依据，必须钉住。"""
    assert output_language_decision({"output_language": "zh-CN"}, None) == ("zh-CN", SOURCE_USER)
    assert output_language_decision({"output_language": "zh-CN"}, {}) == ("zh-CN", SOURCE_USER)
    assert output_language_decision({}, {}) == (None, SOURCE_NONE)


def test_defaults_only_fill_when_user_has_none() -> None:
    """`defaults` 是团队建议：用户自己设过就不动他。"""
    policy = parse_team_language_policy(_FOLLOW)
    assert output_language_decision({}, policy) == ("follow_input", SOURCE_TEAM_DEFAULT)
    assert output_language_decision({"output_language": "zh-CN"}, policy) == ("zh-CN", SOURCE_USER)


def test_locks_override_even_an_explicit_user_setting() -> None:
    """Team locks override an existing explicit member language preference."""
    policy = parse_team_language_policy(_LOCKED_EN)
    assert output_language_decision({"output_language": "zh-CN"}, policy) == ("en-US", SOURCE_LOCK)
    assert output_language_decision({}, policy) == ("en-US", SOURCE_LOCK)


def test_source_separates_team_suggestion_from_explicit_choice() -> None:
    """批7（乙）的新语义：`defaults` 是"建议"、`locks` 与用户设置是"显式选择"。

    来源分不出来时（批7 之前），团队一设 default 就等于把全团队钉死、再也跟不了提问语言。
    """
    team_only = parse_team_language_policy('{"version":1,"defaults":{"output_language":"en-US"}}')
    assert output_language_decision({}, team_only) == ("en-US", SOURCE_TEAM_DEFAULT)
    assert output_language_decision({"output_language": "en-US"}, team_only) == ("en-US", SOURCE_USER)


# ─────────── Wiring to LanguageContext.resolve ───────────


def test_resolve_uses_team_default_for_a_user_who_never_set_one() -> None:
    ctx = LanguageContext.resolve(
        user_settings={"ui_language": "zh-CN"},
        team_language_policy=parse_team_language_policy(_FOLLOW),
        input_carrier_language="en-US",
    )
    assert ctx.output_language == "en-US"  # Follow the question.
    assert ctx.ui_language == "zh-CN"  # UI remains unaffected.


def test_resolve_lock_beats_user_setting() -> None:
    ctx = LanguageContext.resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "zh-CN"},
        team_language_policy=parse_team_language_policy(_LOCKED_EN),
    )
    assert ctx.output_language == "en-US"


def test_resolve_without_policy_is_unchanged() -> None:
    """回归闸：不传策略时与改动前逐字节一致。"""
    ctx = LanguageContext.resolve(user_settings={"ui_language": "zh-CN", "output_language": "en-US"})
    assert ctx.output_language == "en-US"
    ctx2 = LanguageContext.resolve(user_settings={"ui_language": "en-US"})
    assert ctx2.output_language == "en-US"


def test_follow_input_without_carrier_falls_back_to_ui_not_empty() -> None:
    """载体判不出来（纯数字提问）→ 回落 ui，不空转。"""
    ctx = LanguageContext.resolve(
        user_settings={"ui_language": "zh-CN"},
        team_language_policy=parse_team_language_policy(_FOLLOW),
        input_carrier_language=None,
    )
    assert ctx.output_language == "zh-CN"


# ─────────── Write validation ───────────


def test_normalize_rejects_unknown_language_slot() -> None:
    """管理员写错一个拼写会静默让全队回落界面语言——所以在写入时就拒。"""
    with pytest.raises(TeamLanguagePolicyError):
        normalize_team_language_policy('{"defaults":{"output_language":"en_US"}}')


def test_german_team_output_policy_requires_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    policy = '{"defaults":{"output_language":"de-DE"}}'
    with pytest.raises(TeamLanguagePolicyError, match="not enabled"):
        normalize_team_language_policy(policy)

    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert normalize_team_language_policy(policy) == '{"version":1,"defaults":{"output_language":"de-DE"}}'


def test_normalize_rejects_lock_without_default() -> None:
    with pytest.raises(TeamLanguagePolicyError):
        normalize_team_language_policy('{"locks":{"output_language":true}}')


def test_normalize_is_idempotent_and_compact() -> None:
    once = normalize_team_language_policy(_LOCKED_EN)
    assert normalize_team_language_policy(once) == once
    assert " " not in once


def test_normalize_rejects_non_object() -> None:
    for bad in ('"just a string"', "[1,2]", "not json"):
        with pytest.raises(TeamLanguagePolicyError):
            normalize_team_language_policy(bad)


def test_parse_never_raises_on_bad_stored_value() -> None:
    """读路径不因一行坏配置打断对话。"""
    for bad in (None, "", "not json", '"a string"', "[1]"):
        assert parse_team_language_policy(bad) == {}
