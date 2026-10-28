"""LanguageContext 单测：fallback 链 + auto 展开 + accept-language 解析."""

from __future__ import annotations

import pytest
from bowhead_core.language_context import (
    DEFAULT_LANGUAGE,
    DEFAULT_WEEK_START,
    METADATA_KEY,
    LanguageContext,
)


class TestDefault:
    def test_default_product_copy_is_english(self) -> None:
        ctx = LanguageContext.default()
        assert ctx.ui_language == "en-US"
        assert ctx.output_language == "en-US"
        assert ctx.data_language == "zh-CN"
        assert ctx.week_start_day == "monday"


class TestPostInitValidation:
    def test_invalid_ui_language_raises(self) -> None:
        with pytest.raises(ValueError, match="ui_language"):
            LanguageContext(ui_language="ja-JP")

    def test_invalid_output_language_raises(self) -> None:
        with pytest.raises(ValueError, match="output_language"):
            LanguageContext(output_language="ja-JP")

    def test_invalid_data_language_raises(self) -> None:
        with pytest.raises(ValueError, match="data_language"):
            LanguageContext(data_language="de-DE")

    def test_invalid_week_start_raises(self) -> None:
        with pytest.raises(ValueError, match="week_start_day"):
            LanguageContext(week_start_day="tuesday")


class TestResolveFallbackChain:
    def test_no_input_returns_default(self) -> None:
        ctx = LanguageContext.resolve()
        assert ctx.ui_language == "en-US"
        assert ctx.output_language == "en-US"

    def test_accept_language_only(self) -> None:
        ctx = LanguageContext.resolve(accept_language="en-US,en;q=0.9")
        assert ctx.ui_language == "en-US"
        assert ctx.output_language == "en-US"

    def test_german_ui_and_output_language(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
        ctx = LanguageContext.resolve(
            accept_language="de-DE,de;q=0.9,en;q=0.8",
            user_settings={"ui_language": "auto", "output_language": "de-DE"},
        )
        assert ctx.ui_language == "de-DE"
        assert ctx.output_language == "de-DE"
        assert ctx.data_language == "zh-CN"

    def test_german_ai_output_is_closed_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
        ctx = LanguageContext.resolve(
            accept_language="de-DE,de;q=0.9",
            user_settings={"ui_language": "auto", "output_language": "de-DE"},
        )
        assert ctx.ui_language == "de-DE"
        assert ctx.output_language == "en-US"

    def test_german_ai_output_can_be_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "true")
        ctx = LanguageContext.resolve(
            accept_language="de-DE,de;q=0.9",
            user_settings={"ui_language": "auto", "output_language": "de-DE"},
        )
        assert ctx.output_language == "de-DE"

    def test_workspace_default_overrides_accept_language(self) -> None:
        ctx = LanguageContext.resolve(
            workspace_settings={"default_ui_language": "en-US"},
            accept_language="zh-CN",
        )
        assert ctx.ui_language == "en-US"

    def test_user_settings_overrides_workspace(self) -> None:
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "zh-CN"},
            workspace_settings={"default_ui_language": "en-US"},
        )
        assert ctx.ui_language == "zh-CN"

    def test_user_profile_overrides_user_settings(self) -> None:
        ctx = LanguageContext.resolve(
            user_profile_language="en-US",
            user_settings={"ui_language": "zh-CN"},
            workspace_settings={"default_ui_language": "zh-CN"},
        )
        assert ctx.ui_language == "en-US"

    def test_auto_in_user_profile_skipped(self) -> None:
        """前端把 'auto' 透传过来时，应被忽略（fallback 到下一层）."""
        ctx = LanguageContext.resolve(
            user_profile_language="auto",
            user_settings={"ui_language": "en-US"},
        )
        assert ctx.ui_language == "en-US"

    def test_invalid_user_profile_language_skipped(self) -> None:
        """前端传不支持的语言时，应被忽略 fallback."""
        ctx = LanguageContext.resolve(
            user_profile_language="ja-JP",
            user_settings={"ui_language": "zh-CN"},
        )
        assert ctx.ui_language == "zh-CN"


class TestOutputLanguageBehavior:
    def test_missing_output_preference_follows_the_resolved_ui(self) -> None:
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "zh-CN"},
        )
        assert ctx.output_language == "zh-CN"

    def test_output_explicit_overrides(self) -> None:
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "zh-CN", "output_language": "en-US"},
        )
        assert ctx.ui_language == "zh-CN"
        assert ctx.output_language == "en-US"  # Chinese PM reporting overseas.

    def test_output_auto_falls_back_to_ui(self) -> None:
        """'auto' = 跟 ui."""
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "en-US", "output_language": "auto"},
        )
        assert ctx.output_language == "en-US"


class TestDataLanguageWorkspaceLevel:
    def test_data_language_from_workspace_only(self) -> None:
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "en-US"},
            workspace_settings={"default_data_language": "zh-CN"},
        )
        # BeiGene US division: English UI, Chinese data.
        assert ctx.ui_language == "en-US"
        assert ctx.data_language == "zh-CN"

    def test_data_language_default_zh(self) -> None:
        ctx = LanguageContext.resolve()
        assert ctx.data_language == "zh-CN"

    def test_user_setting_does_not_change_data_language(self) -> None:
        """data_language 是 workspace 属性，user 不可影响."""
        ctx = LanguageContext.resolve(
            user_settings={"data_language": "en-US"},  # Must not be recognized.
            workspace_settings={"default_data_language": "zh-CN"},
        )
        assert ctx.data_language == "zh-CN"


class TestWeekStartDay:
    def test_default_monday(self) -> None:
        ctx = LanguageContext.resolve()
        assert ctx.week_start_day == "monday"

    def test_workspace_sunday(self) -> None:
        ctx = LanguageContext.resolve(
            workspace_settings={"week_start_day": "sunday"},
        )
        assert ctx.week_start_day == "sunday"

    def test_invalid_falls_back_default(self) -> None:
        ctx = LanguageContext.resolve(
            workspace_settings={"week_start_day": "tuesday"},
        )
        assert ctx.week_start_day == "monday"


class TestAcceptLanguageParsing:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("zh-CN,zh;q=0.9,en;q=0.8", "zh-CN"),
            ("en-US,en;q=0.9", "en-US"),
            ("zh", "zh-CN"),
            ("en", "en-US"),
            ("zh-TW", "zh-CN"),  # Language-family match.
            ("en-GB", "en-US"),  # Language-family match.
            ("fr-FR,de;q=0.5", "fr-FR"),  # UI language.
            ("fr-CA", "fr-FR"),  # Language-family match.
            ("ja-JP,de;q=0.5", "de-DE"),  # Use the next supported browser preference.
            ("", None),
            (None, None),
        ],
    )
    def test_parsing(self, header: str | None, expected: str | None) -> None:
        ctx = LanguageContext.resolve(user_settings={"ui_language": "auto"}, accept_language=header)
        if expected is None:
            assert ctx.ui_language == DEFAULT_LANGUAGE
        else:
            assert ctx.ui_language == expected


class TestSerialization:
    def test_to_dict(self) -> None:
        ctx = LanguageContext(
            ui_language="en-US",
            output_language="en-US",
            data_language="zh-CN",
            week_start_day="monday",
        )
        assert ctx.to_dict() == {
            "ui_language": "en-US",
            "output_language": "en-US",
            "data_language": "zh-CN",
            "week_start_day": "monday",
        }

    def test_from_dict_roundtrip(self) -> None:
        original = LanguageContext(
            ui_language="zh-CN",
            output_language="en-US",
            data_language="zh-CN",
            week_start_day="sunday",
        )
        roundtripped = LanguageContext.from_dict(original.to_dict())
        assert roundtripped == original

    def test_from_dict_empty_returns_default(self) -> None:
        assert LanguageContext.from_dict(None) == LanguageContext.default()
        assert LanguageContext.from_dict({}) == LanguageContext.default()

    def test_from_dict_partial(self) -> None:
        ctx = LanguageContext.from_dict({"ui_language": "en-US"})
        assert ctx.ui_language == "en-US"
        assert ctx.output_language == DEFAULT_LANGUAGE  # Missing falls back to default.
        assert ctx.week_start_day == DEFAULT_WEEK_START


class TestRealisticScenarios:
    def test_chinese_interface_answers_and_data(self) -> None:
        """Chinese interface, answers, and source data remain independent choices."""
        ctx = LanguageContext.resolve(
            user_profile_language="zh-CN",
            user_settings={"output_language": "zh-CN"},
            workspace_settings={"default_data_language": "zh-CN"},
        )
        assert (ctx.ui_language, ctx.output_language, ctx.data_language) == ("zh-CN", "zh-CN", "zh-CN")

    def test_english_interface_with_chinese_data(self) -> None:
        """An English interface can coexist with Chinese source data."""
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "en-US"},
            workspace_settings={"default_data_language": "zh-CN"},
        )
        assert ctx.ui_language == "en-US"
        assert ctx.output_language == "en-US"  # Follow UI.
        assert ctx.data_language == "zh-CN"

    def test_china_pm_overseas_report(self) -> None:
        """中国 PM 海外汇报：UI 中文 + 输出英文."""
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "zh-CN", "output_language": "en-US"},
            workspace_settings={"default_data_language": "zh-CN"},
        )
        assert ctx.ui_language == "zh-CN"
        assert ctx.output_language == "en-US"
        assert ctx.data_language == "zh-CN"


def test_metadata_key_constant() -> None:
    """METADATA_KEY 是稳定字符串常量（用于 LLMRequest.metadata 通道）."""
    assert METADATA_KEY == "language_context"


# ─── Batch 4: follow_input setting (scheme A=③) ─────────────────


def test_follow_input_takes_the_carrier_language() -> None:
    from bowhead_core.language_context import FOLLOW_INPUT, LanguageContext

    settings = {"ui_language": "zh-CN", "output_language": FOLLOW_INPUT}
    en = LanguageContext.resolve(user_settings=settings, input_carrier_language="en-US")
    assert en.output_language == "en-US"
    assert en.ui_language == "zh-CN", "界面语言不受这一档影响"
    zh = LanguageContext.resolve(user_settings=settings, input_carrier_language="zh-CN")
    assert zh.output_language == "zh-CN"


def test_follow_input_falls_back_to_ui_when_carrier_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """判不出载体（纯数字/纯符号的一句话）→ 回落界面语言，不空转也不报错。"""
    from bowhead_core.language_context import FOLLOW_INPUT, LanguageContext

    settings = {"ui_language": "en-US", "output_language": FOLLOW_INPUT}
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert LanguageContext.resolve(user_settings=settings, input_carrier_language=None).output_language == "en-US"
    assert LanguageContext.resolve(user_settings=settings, input_carrier_language="").output_language == "en-US"
    # Supported German carrier directly becomes this turn's answer language.
    assert LanguageContext.resolve(user_settings=settings, input_carrier_language="de-DE").output_language == "de-DE"


def test_carrier_language_is_ignored_for_a_concrete_answer_preference() -> None:
    """A saved concrete answer language is not overridden by the question language.

    language-policy.md, AI answers: only unset preferences (missing or personal `auto`)
    and explicit `follow_input` follow the question.
    """
    from bowhead_core.language_context import LanguageContext

    explicit = LanguageContext.resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "zh-CN"},
        input_carrier_language="en-US",
    )
    assert explicit.output_language == "zh-CN"
    auto = LanguageContext.resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "auto"},
        input_carrier_language="en-US",
    )
    assert auto.output_language == "en-US"


def test_follow_input_never_leaks_into_the_field() -> None:
    """它是设置取值不是语言：直接塞进字段必须 raise（全仓 locale 表只认中英）。"""
    import pytest as _pytest
    from bowhead_core.language_context import FOLLOW_INPUT, LanguageContext

    with _pytest.raises(ValueError):
        LanguageContext(output_language=FOLLOW_INPUT)
    # Must not leak through resolve either.
    ctx = LanguageContext.resolve(user_settings={"output_language": FOLLOW_INPUT})
    assert ctx.output_language in ("zh-CN", "en-US")


class TestFrench:
    """French interface and explicit French AI preferences remain supported."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"user_settings": {"ui_language": "fr-FR"}},
            {"user_profile_language": "fr-FR"},
            {"workspace_settings": {"default_ui_language": "fr-FR"}},
            {"user_settings": {"ui_language": "auto"}, "accept_language": "fr-FR,fr;q=0.9,en;q=0.8"},
        ],
    )
    def test_every_ui_source_recognizes_french(self, kwargs: dict) -> None:
        ctx = LanguageContext.resolve(**kwargs)
        assert (ctx.ui_language, ctx.output_language) == ("fr-FR", "fr-FR")

    def test_auto_output_follows_french_ui(self) -> None:
        ctx = LanguageContext.resolve(user_settings={"ui_language": "fr-FR", "output_language": "auto"})
        assert ctx.output_language == "fr-FR"

    def test_explicit_french_output_needs_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
        ctx = LanguageContext.resolve(user_settings={"ui_language": "zh-CN", "output_language": "fr-FR"})
        assert ctx.output_language == "fr-FR"

    def test_follow_input_carries_french(self) -> None:
        ctx = LanguageContext.resolve(
            user_settings={"ui_language": "zh-CN", "output_language": "follow_input"},
            input_carrier_language="fr-FR",
        )
        assert ctx.output_language == "fr-FR"

    def test_german_gate_off_with_french_ui_answers_in_french(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
        ctx = LanguageContext.resolve(user_settings={"ui_language": "fr-FR", "output_language": "de-DE"})
        assert ctx.output_language == "fr-FR"

    def test_round_trips_through_metadata(self) -> None:
        ctx = LanguageContext.resolve(user_settings={"ui_language": "fr-FR"})
        assert LanguageContext.from_dict(ctx.to_dict()) == ctx


def test_missing_preferences_follow_browser_language() -> None:
    # Interface follows the browser; without question evidence the answer follows the interface.
    ctx = LanguageContext.resolve(accept_language="zh-CN,zh;q=0.9")
    assert ctx.ui_language == ctx.output_language == "zh-CN"
    # language-policy.md, AI answers: an unset answer preference follows the question instead.
    ctx = LanguageContext.resolve(accept_language="zh-CN,zh;q=0.9", input_carrier_language="en-US")
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "en-US")


def test_explicit_chinese_preferences_override_english_defaults() -> None:
    ctx = LanguageContext.resolve(user_settings={"ui_language": "zh-CN", "output_language": "zh-CN"}, accept_language="en-US")
    assert ctx.ui_language == ctx.output_language == "zh-CN"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("EN-gb", "en-US"),
        ("ja-JP,fr-CA;q=0.8,en;q=0.5", "fr-FR"),
        ("zh-CN;q=0.3,fr-FR;q=0.9", "fr-FR"),
        ("zh-CN;q=0,en-US;q=0.5", "en-US"),
        ("zh-CN;q=invalid,fr-FR;q=0.7", "fr-FR"),
        ("ja-JP", "en-US"),
        (None, "en-US"),
    ],
)
def test_browser_language_negotiation(header: str | None, expected: str) -> None:
    ctx = LanguageContext.resolve(accept_language=header)
    assert ctx.ui_language == ctx.output_language == expected
    assert ctx.data_language == "zh-CN"
