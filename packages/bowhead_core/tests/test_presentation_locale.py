from __future__ import annotations

import json
from pathlib import Path

from bowhead_core.presentation_locale import canonicalize_supported_locale, resolve_presentation_locale

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_presentation_locale_v1.json"


def test_presentation_locale_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["contract"] == "presentation-locale/B1a-v1"
    for case in fixture["canonical_cases"]:
        assert canonicalize_supported_locale(case["input"]) == case["expected"]
    for case in fixture["resolve_cases"]:
        assert resolve_presentation_locale(*case["candidates"]) == case["expected"]


def test_french_presentation_copy() -> None:
    from bowhead_core.source_linkage_copy import (
        SOURCE_GENERIC_TYPE_COPY,
        SOURCE_LINKAGE_COPY,
        format_source_linkage,
        generic_source_type_copy,
    )
    from bowhead_core.source_linkage_presentation import SourceLinkagePresentation

    for table in (SOURCE_GENERIC_TYPE_COPY, SOURCE_LINKAGE_COPY):
        assert set(table["fr-FR"]) == set(table["en-US"])
    assert generic_source_type_copy("document", "fr-FR") == "Document de la base de connaissances"
    assert format_source_linkage(SourceLinkagePresentation(1, 2, 3), "fr-FR", include_definition=False) == (
        "1 référence à des sources · 2 nombres à vérifier · 3 sources"
    )


def test_non_chinese_interfaces_get_english_taxonomy_labels() -> None:
    from bowhead_core.agent_taxonomy import platform_taxonomy

    chinese = platform_taxonomy("zh-CN")
    english = platform_taxonomy("en-US")
    assert platform_taxonomy(None) == english
    assert chinese != english
    for locale in ("fr-FR", "de-DE"):
        assert platform_taxonomy(locale) == english
    # Unsupported or missing locales use English.
    assert platform_taxonomy("ja-JP") == english


def test_home_proposals_keep_german_and_french_locales() -> None:
    from bowhead_core.home_proposals import normalize_locale

    assert normalize_locale("fr-FR") == "fr-FR"
    assert normalize_locale("de-DE") == "de-DE"
    assert normalize_locale("en-US") == "en-US"
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("ja-JP") == "en-US"


def test_french_output_language_is_wired_like_german() -> None:
    from bowhead_core.analysis_payload import _humanize_fabrication_loc
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.language_directive import _LANGUAGE_LABELS
    from bowhead_core.overclaim_guard import caveat_for
    from bowhead_core.report_chrome_text import _CHROME, chrome_lang, chrome_text
    from bowhead_core.team_language_policy import normalize_team_language_policy

    assert set(_CHROME["fr-FR"]) == set(_CHROME["en-US"])
    assert chrome_text("sources_label", "fr-FR") == "Sources de données"
    assert chrome_text("updated_on", "fr-FR", date="2026-09-30") == "Mis à jour le 2026-09-30"
    assert chrome_lang(LanguageContext(ui_language="fr-FR", output_language="fr-FR")) == "fr-FR"
    assert "Aucun fichier téléchargeable" in caveat_for(("file",), "fr-FR")
    assert _humanize_fabrication_loc("kpis#0.value", "12", "fr-FR") == "12 dans le KPI 1"
    assert _LANGUAGE_LABELS["fr-FR"]["en"] == "French"
    assert '"fr-FR"' in normalize_team_language_policy('{"defaults":{"output_language":"fr-FR"}}')


def test_french_numeric_caveat_remains_recognizable_after_persistence() -> None:
    from bowhead_core.analysis_payload import (
        check_fabrication_back_reference,
        non_provenance_caveats,
        provenance_caveats,
    )

    payload = {"kpis": [{"label": "Revenu", "value": "987"}]}
    caveats = check_fabrication_back_reference(payload, {12.0}, lang="fr-FR")
    assert len(caveats) == 1
    assert caveats[0].startswith("Les valeurs suivantes n’ont pu être rattachées")
    assert "987 dans le KPI 1" in caveats[0]
    stored = json.loads(json.dumps({"caveats": [*caveats, "Les données couvrent un seul mois."]}))
    assert provenance_caveats(stored) == caveats
    assert non_provenance_caveats(stored) == ["Les données couvrent un seul mois."]


def test_memory_notices_use_native_german_and_french_copy() -> None:
    from bowhead_core.memory_auto_capture import REASON_MESSAGES, reason_user_message

    reason = next(iter(REASON_MESSAGES["en-US"]))
    english = reason_user_message(reason, lang="en-US")
    for lang in ("de-DE", "fr-FR"):
        localized = reason_user_message(reason, lang=lang)
        assert localized == REASON_MESSAGES[lang][reason]
        assert localized != english
    # Missing or unsupported languages use the English fallback.
    assert reason_user_message(reason, lang="ja-JP") == english
    assert reason_user_message(reason) == english
