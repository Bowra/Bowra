"""Question-language detection (zh / en / de / fr) and the turn's answer language.

Rule (docs/design/language-policy.md, "AI answers"): one strict detector,
`channel_message_language`, serves every surface. Chinese structure stays Chinese
even with English product names; Latin text counts only when it is confidently
German, French, or English. Without evidence the answer falls back to the previous
answer, then a concrete team default, then the interface language, which is the
expected value in CASES for uncertain questions.
"""

from __future__ import annotations

import pytest
from bowhead_core.effective_language import carrier_language, channel_message_language, latin_language
from bowhead_core.language_context import resolve_turn_language

# (Question, UI language, expected question language)
CASES: list[tuple[str, str, str]] = [
    # ── Chinese: unchanged detection, even with many English terms / product names ──
    ("本月各省的销售额是多少？", "zh-CN", "zh-CN"),
    ("对比一下去年和今年的毛利率", "zh-CN", "zh-CN"),
    ("帮我看下本月各省的 revenue 和 profit margin trend，出个报告。", "zh-CN", "zh-CN"),
    ("Power BI 里的 Sales Dashboard 上个月 GMV 是多少", "zh-CN", "zh-CN"),
    ("好的", "zh-CN", "zh-CN"),
    ("继续", "fr-FR", "zh-CN"),
    ("华东区 Top 10 SKU 的 CTR 和 CVR", "en-US", "zh-CN"),
    ("为什么 Q3 的 churn rate 突然上升？", "de-DE", "zh-CN"),
    ("用法语回答：本月销量", "zh-CN", "zh-CN"),
    ("列出所有门店", "fr-FR", "zh-CN"),
    # ── English: French / Chinese proper nouns, English questions in French / German UIs ──
    ("What were the total sales last month?", "en-US", "en-US"),
    ("Show me revenue by region for this quarter", "en-US", "en-US"),
    ("Compare this year's growth with last year", "zh-CN", "en-US"),
    ("Show me this month's revenue and profit margin for 北京市 and 上海市", "zh-CN", "en-US"),
    ("How did the Société Générale account perform this year?", "fr-FR", "en-US"),
    ("Which stores had the highest foot traffic in Q3?", "fr-FR", "en-US"),
    ("What is the churn rate of our Premium plan?", "de-DE", "en-US"),
    ("ok", "en-US", "en-US"),
    # A one-word Latin reply is no evidence: the interface language applies.
    ("thanks", "zh-CN", "zh-CN"),
    ("Please give me the top 5 products by revenue", "fr-FR", "en-US"),
    # ── German: ambiguous short sentences follow the UI; includes English metrics / German proper nouns ──
    ("Wie hoch war der Umsatz im letzten Monat?", "en-US", "de-DE"),
    ("Zeige mir den Umsatz nach Region für dieses Quartal", "zh-CN", "de-DE"),
    ("Welche Filialen hatten das stärkste Wachstum?", "en-US", "de-DE"),
    ("Vergleiche den Umsatz von Müller GmbH mit dem Vorjahr", "fr-FR", "de-DE"),
    ("Umsatz pro Monat", "de-DE", "de-DE"),
    ("Umsatz pro Monat", "en-US", "en-US"),
    ("Danke", "de-DE", "de-DE"),
    ("Wie ist die Churn Rate im Premium Plan?", "zh-CN", "de-DE"),
    ("Gib mir die Top 5 Produkte nach Umsatz", "en-US", "de-DE"),
    ("Warum ist der Umsatz in Bayern gesunken?", "fr-FR", "de-DE"),
    # ── French: short sentences, many English product / metric names ──
    ("Quel est le chiffre d'affaires du mois dernier ?", "en-US", "fr-FR"),
    ("Montre-moi les ventes par région pour ce trimestre", "zh-CN", "fr-FR"),
    ("Quelles boutiques ont la plus forte croissance ?", "de-DE", "fr-FR"),
    ("Compare le CTR et le CAC du dashboard Marketing avec l'année dernière", "en-US", "fr-FR"),
    ("Ventes par mois", "fr-FR", "fr-FR"),
    ("Ventes par mois", "en-US", "fr-FR"),
    ("Merci", "fr-FR", "fr-FR"),
    ("Merci", "en-US", "en-US"),
    ("Pourquoi le churn rate du plan Premium augmente ?", "zh-CN", "fr-FR"),
    ("Donne-moi le top 5 des produits par chiffre d'affaires", "en-US", "fr-FR"),
    ("Quelle est la croissance des ventes de Carrefour en Île-de-France ?", "en-US", "fr-FR"),
    # ── All caps: lowercase the sentence before scoring; do not skip every word as a proper noun ──
    ("QUELLES SONT LES VENTES DU MOIS DERNIER ?", "en-US", "fr-FR"),
    ("WHAT ARE THE SALES FOR THIS MONTH?", "fr-FR", "en-US"),
    # Exclude `was` from the vocabulary (same form in German/English): German "Was …" remains German.
    ("Was war der Umsatz im letzten Monat?", "en-US", "de-DE"),
]


@pytest.mark.parametrize(("text", "ui", "expected"), CASES)
def test_question_language(text: str, ui: str, expected: str) -> None:
    assert (channel_message_language(text) or ui) == expected


@pytest.mark.parametrize(("text", "ui", "expected"), CASES)
def test_chinese_judgement_is_unchanged(text: str, ui: str, expected: str) -> None:
    if carrier_language(text) == "zh-CN":
        assert expected == "zh-CN"
        assert channel_message_language(text) == "zh-CN"


@pytest.mark.parametrize(("text", "ui", "expected"), CASES)
@pytest.mark.parametrize("unset", [{}, {"output_language": "auto"}])
def test_unset_web_answer_language_follows_the_question(
    monkeypatch: pytest.MonkeyPatch, text: str, ui: str, expected: str, unset: dict[str, str]
) -> None:
    # language-policy.md, AI answers: without a saved concrete answer preference or lock
    # (missing or personal `auto`), web answers follow the question with the same strict
    # detector as `follow_input` and channels; uncertain questions fall back to the interface.
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    ctx = resolve_turn_language(user_settings=unset, user_profile_language=ui, message_text=text)
    assert ctx.output_language == expected
    # The interface language itself never follows the question.
    assert ctx.ui_language == ui


@pytest.mark.parametrize(("text", "ui", "expected"), CASES)
def test_explicit_follow_input_answer_language_follows_the_question(monkeypatch: pytest.MonkeyPatch, text: str, ui: str, expected: str) -> None:
    # language-policy.md, AI answers: an explicit `follow_input` choice follows the question;
    # uncertain questions fall back to the interface (the expected values of CASES).
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    ctx = resolve_turn_language(
        user_settings={"output_language": "follow_input"},
        user_profile_language=ui,
        message_text=text,
    )
    assert ctx.output_language == expected


_WEAK_OR_CHINESE_WITH_TERMS = ["ok", "SKU TOP10", "Q3 GMV YoY by SKU 怎么样", "帮我用 Power BI dashboard 看一下 Top 10 SKU 的 revenue"]
_TEAM_ZH_DEFAULT = {"version": 1, "defaults": {"output_language": "zh-CN"}}


@pytest.mark.parametrize("text", _WEAK_OR_CHINESE_WITH_TERMS)
@pytest.mark.parametrize("settings", [{}, {"output_language": "follow_input"}], ids=["unset", "follow_input"])
def test_weak_latin_or_chinese_with_english_terms_keeps_the_previous_chinese_answer(text: str, settings: dict[str, str]) -> None:
    # language-policy.md, AI answers: the strict detector never flips these to English.
    ctx = resolve_turn_language(user_settings=settings, accept_language="en-US", message_text=text, session_last_language="zh-CN")
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "zh-CN")


@pytest.mark.parametrize("text", _WEAK_OR_CHINESE_WITH_TERMS)
def test_weak_latin_or_chinese_with_english_terms_uses_the_chinese_team_default(text: str) -> None:
    # No previous answer: "ok"/"SKU TOP10" carry no evidence, so the concrete team default
    # applies; the two Chinese sentences are Chinese evidence themselves.
    ctx = resolve_turn_language(accept_language="en-US", message_text=text, team_language_policy=_TEAM_ZH_DEFAULT)
    assert ctx.output_language == "zh-CN"


def test_confident_english_still_follows_the_question() -> None:
    # Positive control for the two tests above: real English evidence wins over both fallbacks.
    ctx = resolve_turn_language(
        accept_language="zh-CN",
        message_text="Show me revenue by region for this quarter",
        session_last_language="zh-CN",
        team_language_policy=_TEAM_ZH_DEFAULT,
    )
    assert ctx.output_language == "en-US"


def test_german_question_still_passes_the_german_output_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # language-policy.md: following the question never bypasses the German output gate.
    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    ctx = resolve_turn_language(user_profile_language="fr-FR", message_text="Wie hoch war der Umsatz im letzten Monat?")
    assert ctx.output_language == "fr-FR"  # German not admitted: fall back to the UI language, consistent with resolve.
    # Positive control: with the gate open the same question is answered in German.
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    ctx = resolve_turn_language(user_profile_language="fr-FR", message_text="Wie hoch war der Umsatz im letzten Monat?")
    assert ctx.output_language == "de-DE"


@pytest.mark.parametrize("text", ["ok", "Merci", "Danke", "Q3", "Umsatz pro Monat", "Power BI"])
def test_short_latin_questions_are_not_confident(text: str) -> None:
    assert latin_language(text) == ""
