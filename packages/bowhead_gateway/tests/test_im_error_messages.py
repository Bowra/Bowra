"""IM error localization must stay finite and aligned with the runtime catalog."""

from __future__ import annotations

import re
from string import Formatter

import pytest
from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
from bowhead_gateway.im.error_messages import (
    IM_ERROR_LABELS,
    IM_ERROR_MESSAGES,
    IM_INTERFACE_MESSAGES,
    SUPPORTED_IM_LOCALES,
    im_action_recorded,
    im_error_message,
    im_interface_message,
    normalize_im_locale,
)


def test_im_error_messages_match_runtime_catalog_in_all_languages() -> None:
    catalog = load_error_catalog(default_error_catalog_dir())

    assert set(IM_ERROR_MESSAGES["zh-CN"]) == set(IM_ERROR_MESSAGES["en-US"])
    for locale, messages in IM_ERROR_MESSAGES.items():
        for code, expected in messages.items():
            params = {"choice": "Option A"} if "{choice}" in expected else {}
            assert catalog.has(code, locale)
            assert catalog.render(code, locale, **params) == expected.format(**params)
            assert im_error_message(
                code,
                locale,
                **params,
            ) == expected.format(**params)


def test_im_locale_normalization_and_unknown_error_fail_closed() -> None:
    assert normalize_im_locale("en-GB") == "en-US"
    assert normalize_im_locale("zh-TW") == "zh-CN"
    assert normalize_im_locale("fr-FR") == "fr-FR"
    assert normalize_im_locale("de_DE") == "de-DE"
    assert normalize_im_locale("ja-JP") == "en-US"

    secret = "HTTP 500 Authorization: Bearer raw-secret"
    rendered = im_error_message(secret, "en-US")
    assert rendered == "A system error interrupted this turn. Please try again later."
    assert secret not in rendered

    recorded = im_action_recorded(
        "<script>\nraw",
        "en-US",
    )
    assert recorded == "Recorded: script  raw"
    assert "<" not in recorded


@pytest.mark.parametrize("locale", sorted(SUPPORTED_IM_LOCALES))
@pytest.mark.parametrize("code", ["document_routing_invalid", "document_routing_uncertain", "document_routing_source_unavailable"])
def test_document_routing_failure_keeps_its_specific_im_guidance(locale: str, code: str) -> None:
    catalog = load_error_catalog(default_error_catalog_dir())
    rendered = im_error_message(code, locale)
    assert rendered == catalog.render(code, locale)
    assert rendered != IM_ERROR_MESSAGES[locale]["lead_internal_error"]
    assert code not in rendered


@pytest.mark.parametrize("locale, expected", [("de-DE", "Erfasst: script  raw"), ("fr-FR", "Enregistré : script  raw")])
def test_translated_action_receipts_sanitize_choices(locale: str, expected: str) -> None:
    assert im_action_recorded("<script>\nraw", locale) == expected


def test_all_im_copy_preserves_catalog_keys_and_format_fields() -> None:
    for catalogs in (IM_ERROR_MESSAGES, IM_ERROR_LABELS, IM_INTERFACE_MESSAGES):
        assert set(catalogs) == SUPPORTED_IM_LOCALES
        reference = catalogs["en-US"]
        for locale, messages in catalogs.items():
            assert set(messages) == set(reference)
            for key, message in messages.items():
                fields = {name for _, name, _, _ in Formatter().parse(message) if name}
                expected_fields = {name for _, name, _, _ in Formatter().parse(reference[key]) if name}
                assert fields == expected_fields, (locale, key)
                rendered = message.format(**dict.fromkeys(fields, "sample"))
                assert "{choice}" not in rendered
                if locale != "zh-CN":
                    assert re.search(r"[\u3400-\u9fff]", message) is None, (locale, key)


@pytest.mark.parametrize(
    "locale, expected",
    [
        ("de-DE", "Abgebrochen."),
        ("fr-FR", "Annulé."),
    ],
)
def test_translated_errors_use_the_selected_locale(locale: str, expected: str) -> None:
    assert im_error_message("user_cancel", locale) == expected
    assert im_error_message("raw HTTP 500 secret", locale) == IM_ERROR_MESSAGES[locale]["lead_internal_error"]
    assert im_interface_message("choice_resume_failed", locale, message=expected).endswith(expected)
