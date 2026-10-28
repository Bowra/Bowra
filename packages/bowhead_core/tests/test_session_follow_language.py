"""Following the previous turn's answer language when the new message carries none.

A user who got a German or French answer and replies with a bare option number ("1") has
no language evidence in the new message, so the turn falls back to "the language of the
previous answer". Before the fix that fallback only knew Chinese and English, so the next
turn switched to English.
"""

from __future__ import annotations

import pytest
from bowhead_core.effective_language import observed_language_of_markdown, session_follow_language_of_markdown
from bowhead_core.language_context import resolve_turn_language

pytestmark = pytest.mark.unit

GERMAN_ANSWER = """Der Umsatz im letzten Monat lag bei 1,24 Mio. EUR und ist gegenüber dem Vormonat um 5,3 % gestiegen.

- **Nord**: 412.000 EUR, das ist der höchste Wert im Quartal.
- **Süd**: 388.000 EUR, ein Rückgang von 2 %.

Welche Region soll ich genauer ansehen?
1. Nord
2. Süd"""

FRENCH_ANSWER = """Le chiffre d'affaires du mois dernier est de 1,24 M EUR, en hausse de 5,3 % par rapport au mois précédent.

- **Nord** : 412 000 EUR, le plus haut niveau du trimestre.
- **Sud** : 388 000 EUR, une baisse de 2 %.

Quelle région voulez-vous que j'analyse en détail ?
1. Nord
2. Sud"""

ENGLISH_ANSWER = """Revenue last month was EUR 1.24M, up 5.3% from the previous month.

- **North**: EUR 412,000, the highest value in the quarter.
- **South**: EUR 388,000, a decline of 2%.

Which region should I look at in more detail?
1. North
2. South"""

# English prose naming German and French companies: capitalized proper nouns are not evidence.
ENGLISH_WITH_FOREIGN_NAMES = (
    "The top customers this quarter were Müller Großhandel, Société Générale and Café Lumière. "
    "Revenue from these three accounts grew by 12% over the last quarter and is now the largest share of the total."
)

CHINESE_ANSWER = "上个月营收为 124 万欧元，环比增长 5.3%。北区最高，南区下降 2%。请问要细看哪个区域？1. 北区 2. 南区"


def _next_turn_language(previous_answer: str, message: str = "1") -> str:
    """Resolve the next turn with an explicit question-following preference."""
    return resolve_turn_language(
        user_settings={"ui_language": "zh-CN", "output_language": "follow_input"},
        message_text=message,
        session_last_language=session_follow_language_of_markdown(previous_answer) or None,
    ).output_language


@pytest.mark.parametrize("unset", [{}, {"output_language": "auto"}])
def test_unset_preference_keeps_the_previous_answer_language_for_a_bare_number(unset: dict[str, str]) -> None:
    # language-policy.md, AI answers: an unset answer preference follows the question; a
    # question without evidence ("1") follows the session's previous answer, then the interface.
    previous = session_follow_language_of_markdown(CHINESE_ANSWER) or None
    ctx = resolve_turn_language(user_settings=unset, accept_language="en-US", message_text="1", session_last_language=previous)
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "zh-CN")
    # Without a previous answer the interface language applies.
    assert resolve_turn_language(user_settings=unset, accept_language="en-US", message_text="1").output_language == "en-US"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (GERMAN_ANSWER, "de-DE"),
        (FRENCH_ANSWER, "fr-FR"),
        (ENGLISH_ANSWER, "en-US"),
        (CHINESE_ANSWER, "zh-CN"),
    ],
    ids=["de", "fr", "en", "zh"],
)
def test_a_bare_option_number_keeps_the_previous_answers_language(answer: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert _next_turn_language(answer) == expected


def test_german_is_not_followed_while_german_output_is_not_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the German gate closed the previous verdict (English) stays, instead of dropping to the UI language."""
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "0")
    assert session_follow_language_of_markdown(GERMAN_ANSWER) == "en-US"
    assert _next_turn_language(GERMAN_ANSWER) == "en-US"
    assert _next_turn_language(FRENCH_ANSWER) == "fr-FR"


def test_the_new_message_still_wins_over_the_previous_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert _next_turn_language(GERMAN_ANSWER, message="Show me the revenue by region for last month") == "en-US"


def test_english_with_foreign_proper_nouns_stays_english() -> None:
    assert session_follow_language_of_markdown(ENGLISH_WITH_FOREIGN_NAMES) == "en-US"


def test_a_short_latin_answer_without_evidence_keeps_the_previous_verdict() -> None:
    assert session_follow_language_of_markdown("OK.") == observed_language_of_markdown("OK.") == "en-US"


def test_code_blocks_are_not_language_evidence() -> None:
    answer = "Done.\n\n```python\nfor le in les: print(le, du, une, et, sont)\n```"
    assert session_follow_language_of_markdown(answer) == "en-US"


def test_entity_dense_english_stays_undecidable() -> None:
    """The mixed-script band is unchanged: no verdict, so the turn keeps its own language."""
    answer = "The top 10 stores by foot traffic are: 东湖街(1200), 南湖花园城(1100), 西岸商城(990), 北滨步行街区(950)."
    assert observed_language_of_markdown(answer) == ""
    assert session_follow_language_of_markdown(answer) == ""


def test_the_answer_language_probe_is_not_affected() -> None:
    """Only the follow-language fallback learns German / French; the probe keeps the zh/en judge."""
    assert observed_language_of_markdown(GERMAN_ANSWER) == "en-US"
    assert observed_language_of_markdown(FRENCH_ANSWER) == "en-US"


# English answers that carry French / German data: quotes, guillemets, blockquotes, tables,
# option lists, glosses, inline code. The next turn must stay English after each of them.
ENGLISH_SHAPES: dict[str, str] = {
    "french_customer_quote": (
        "Top complaint this quarter, in the customer's words: \"le produit est très bien mais les délais de livraison "
        'sont trop longs pour nous et pour nos clients dans chaque région, sans aucune explication du service." '
        "Delivery complaints rose 18%."
    ),
    "german_lowercase_table": (
        "Status by job:\n\n"
        "| job | status | note |\n| --- | --- | --- |\n"
        "| 1 | nicht verfügbar | für den export gesperrt |\n"
        "| 2 | in bearbeitung | wird noch von dem system geprüft |\n"
        "| 3 | abgeschlossen | keine weiteren schritte bei der prüfung |\n"
        "| 4 | fehler | ist auf dem server nicht gefunden |\n\n"
        "Two jobs need attention."
    ),
    "quoted_german_sentence": (
        "The survey question was „wie zufrieden sind Sie mit dem Service und der Lieferung?“ and 64% of respondents answered positively."
    ),
    "french_option_list": (
        "Which segment should I drill into?\n"
        "1. les ventes du nord\n2. les ventes du sud\n3. le chiffre d'affaires par canal\n4. la croissance de chaque mois"
    ),
    "french_column_headers": (
        "| région | chiffre d'affaires | croissance du mois | ventes par canal | part des ventes |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Nord | 412 000 | 5 % | 34 000 | 52 % |\n| Sud | 388 000 | -2 % | 32 000 | 48 % |\n\n"
        "North leads."
    ),
    "bilingual_labels": (
        "Revenue (chiffre d'affaires) rose 5%; growth (croissance du mois) in the north (le nord) was strongest; "
        "sales per channel (ventes par canal) and the share (part des ventes) are below."
    ),
    "german_blockquote": (
        "The supplier replied:\n\n> wir können die ware leider nicht vor dem ende des monats liefern, "
        "da es bei der produktion zu verzögerungen kommt und die teile noch nicht von dem lager gekommen sind.\n\n"
        "Launch moves to next month."
    ),
    "french_guillemets": (
        "The French team's summary reads « les ventes sont en hausse dans chaque région et le mois dernier "
        "était très bon pour nous » and matches the numbers above."
    ),
    "capitalized_foreign_names": (
        "The top customers this quarter were Müller Großhandel, Société Générale and Café Lumière. "
        "Revenue from these accounts grew by 12% over the last quarter."
    ),
    "short_english": "Done. The report is ready.",
    "english_kpi_list": ("Key numbers for last month:\n- Revenue: EUR 1.24M\n- Growth: 5.3%\n- Orders: 12,480\n\nWhat would you like to see next?"),
    "english_options": ("I can break this down in two ways. Which one do you want?\n1. By region\n2. By product line"),
    "inline_code_french": ("Filter with `WHERE le_pays = 'France' AND les ventes du mois > 0 AND par canal` and the result drops to 42 rows."),
    # Short unquoted fragments inside the prose itself: enough for a question-strength verdict
    # (at least 2 points and 2x), not for the answer-strength one (at least 4 points and 3x).
    "unquoted_french_label": "Label used in the source file: les chiffres du nord et sud. Nothing else changed.",
    "unquoted_german_note": "Column note reads: nicht für den export. Everything else is fine.",
}

# French / German answers of the usual shapes: prose with options, a table with an intro,
# bullets with a closing question, a short question, long prose. Each must still be followed.
FOREIGN_SHAPES: dict[str, tuple[str, str]] = {
    "fr_prose_and_options": (
        "fr-FR",
        (
            "Le chiffre d'affaires du mois dernier est de 1,24 M EUR, en hausse de 5,3 % par rapport au mois précédent.\n\n"
            "Quelle région voulez-vous que j'analyse en détail ?\n1. Nord\n2. Sud"
        ),
    ),
    "fr_table_with_intro": (
        "fr-FR",
        (
            "Voici les ventes par région pour le dernier trimestre. Le nord reste en tête et la croissance est plus forte que prévu.\n\n"
            "| Région | Ventes |\n| --- | --- |\n| Nord | 412 000 |\n| Sud | 388 000 |\n\n"
            "Voulez-vous que je compare avec le trimestre précédent ?"
        ),
    ),
    "fr_bullets_mostly": (
        "fr-FR",
        (
            "Voici les points clés pour ce mois :\n- Ventes : 1,24 M EUR\n- Croissance : 5,3 %\n- Commandes : 12 480\n\n"
            "Quelle analyse voulez-vous voir ensuite ?"
        ),
    ),
    "fr_short_question": ("fr-FR", "Je peux détailler les ventes par région ou par canal. Que préférez-vous ?\n1. Par région\n2. Par canal"),
    "fr_long_prose": (
        "fr-FR",
        (
            "Les ventes ont progressé dans toutes les régions au cours du trimestre. La hausse la plus forte vient du nord, "
            "où les commandes ont augmenté de 12 % par rapport au trimestre précédent. Le sud reste stable, avec une légère "
            "baisse des marges. Je vous recommande de suivre l'évolution des prix dans cette région."
        ),
    ),
    "de_prose_and_options": (
        "de-DE",
        (
            "Der Umsatz im letzten Monat lag bei 1,24 Mio. EUR und ist gegenüber dem Vormonat um 5,3 % gestiegen.\n\n"
            "Welche Region soll ich genauer ansehen?\n1. Nord\n2. Süd"
        ),
    ),
    "de_table_with_intro": (
        "de-DE",
        (
            "Hier ist der Umsatz nach Region für das letzte Quartal. Der Norden liegt vorn und das Wachstum ist höher als erwartet.\n\n"
            "| Region | Umsatz |\n| --- | --- |\n| Nord | 412.000 |\n| Süd | 388.000 |\n\n"
            "Soll ich das mit dem Vorquartal vergleichen?"
        ),
    ),
    "de_short_question": (
        "de-DE",
        "Ich kann den Umsatz nach Region oder nach Kanal aufschlüsseln. Was ist Ihnen lieber?\n1. Nach Region\n2. Nach Kanal",
    ),
    "de_long_prose": (
        "de-DE",
        (
            "Der Umsatz ist in allen Regionen im Laufe des Quartals gestiegen. Das stärkste Wachstum kommt aus dem Norden, "
            "wo die Bestellungen gegenüber dem Vorquartal um 12 % gestiegen sind. Der Süden bleibt stabil, mit einem leichten "
            "Rückgang der Margen. Ich empfehle, die Entwicklung der Preise in dieser Region zu verfolgen."
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(ENGLISH_SHAPES))
def test_english_answers_carrying_french_or_german_data_stay_english(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert session_follow_language_of_markdown(ENGLISH_SHAPES[name]) == "en-US"
    assert _next_turn_language(ENGLISH_SHAPES[name]) == "en-US"


@pytest.mark.parametrize("name", sorted(FOREIGN_SHAPES))
def test_french_and_german_answers_are_still_followed(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    expected, answer = FOREIGN_SHAPES[name]
    assert _next_turn_language(answer) == expected
