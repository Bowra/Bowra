"""Native delivery copy through the actual notice and tool execution paths."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.language_context import LanguageContext
from bowhead_core.types import ToolInvocation

from bowhead_core import builtin_google_tool as google
from bowhead_core import google_delivery_tool as delivery
from bowhead_core import send_email_tool as email

pytestmark = pytest.mark.unit

_EMAIL_REASONS = [
    ("email_numbers_unverified", "Zahlen", "nombres"),
    ("email_send_rejected", "abgelehnt", "refusé"),
    ("email_confirmation_expired", "vorgesehenen Zeit", "à temps"),
    ("email_confirmation_unavailable", "Webchat", "conversation web"),
    ("email_service_not_configured", "Teamadministrator", "administrateur d’équipe"),
    ("email_service_unavailable", "vorübergehend", "temporairement"),
    ("email_delivery_failed", "zustellen", "livrer"),
    ("invalid_recipient", "Empfängeradresse", "destinataire"),
    ("invalid_subject", "Betreff", "objet"),
    ("invalid_body", "leer oder zu lang", "vide ou trop long"),
    ("unknown_reason", "Problem", "problème"),
]
_GOOGLE_REASONS = [
    ("google_unsourced_numbers", "Zahlen", "nombres"),
    ("google_web_only", "Webchat", "conversation web"),
    ("google_not_connected", "nicht verbunden", "n’est pas connecté"),
    ("google_reauth_required", "abgelaufen", "expiré"),
    ("google_scope_missing", "aktualisieren", "mettez-la à jour"),
    ("google_revision_conflict", "anderen Person", "autre personne"),
    ("google_range_not_empty", "bereits Daten", "déjà des données"),
    ("google_outcome_unknown", "unbekannt", "inconnu"),
    ("unknown_reason", "Problem", "problème"),
]


def _inv(name: str, code: str, **extra: object) -> ToolInvocation:
    return ToolInvocation(tool_name=name, arguments={"to": "recipient@example.com"}, result={"ok": False, "error": code, **extra})


@pytest.mark.parametrize(("code", "de", "fr"), _EMAIL_REASONS)
@pytest.mark.parametrize("lang", ["de-DE", "fr-FR"])
def test_email_reason_and_next_action_are_native(code: str, de: str, fr: str, lang: str) -> None:
    notice = email.unsent_email_notice([_inv(email.TOOL_NAME, code)], lang)
    assert notice.startswith("**Die E-Mail" if lang == "de-DE" else "**L’e-mail")
    assert (de if lang == "de-DE" else fr) in notice
    assert "recipient@example.com" in notice
    assert "{to}" not in notice
    assert code not in notice


@pytest.mark.parametrize(("code", "de", "fr"), _GOOGLE_REASONS)
@pytest.mark.parametrize("lang", ["de-DE", "fr-FR"])
def test_google_delivery_reason_is_native(code: str, de: str, fr: str, lang: str) -> None:
    notice = delivery.undelivered_google_notice([_inv(delivery.DOC_TOOL, code)], lang)
    assert notice.startswith("**Nicht in Google gespeichert**:" if lang == "de-DE" else "**Non enregistré dans Google** :")
    assert (de if lang == "de-DE" else fr) in notice
    assert ("Google-Dokument" if lang == "de-DE" else "Document Google") in notice
    assert code not in notice
    if code == "google_outcome_unknown":
        assert ("kein automatischer" if lang == "de-DE" else "aucune nouvelle tentative") in notice


@pytest.mark.parametrize(
    ("code", "de", "fr"),
    [
        ("google_not_connected", "Google verbinden", "Connecter Google"),
        ("google_reauth_required", "Google erneut verbinden", "Reconnecter Google"),
        ("google_scope_missing", "Google-Berechtigungen aktualisieren", "Mettre à jour les autorisations Google"),
    ],
)
@pytest.mark.parametrize("lang", ["de-DE", "fr-FR"])
def test_nested_google_connection_reason_is_native(code: str, de: str, fr: str, lang: str) -> None:
    failure = _inv("teammcp.connection." + google.READ_OPERATION, code, error_kind=code, provider="google_workspace")
    parent = ToolInvocation(tool_name="subagent_spawn", arguments={}, nested=[failure])
    notice = google.google_connection_notice([parent], lang)
    assert (de if lang == "de-DE" else fr) in notice
    assert ("Meine Verbindungen" if lang == "de-DE" else "Mes connexions") in notice
    recovered = ToolInvocation(tool_name=failure.tool_name, arguments={}, result={"ok": True, "provider": "google_workspace"})
    assert google.google_connection_notice([parent, recovered], lang) == ""


@pytest.mark.parametrize("lang", ["de-DE", "fr-FR"])
def test_last_success_suppresses_failure_notices(lang: str) -> None:
    for name, notice in ((email.TOOL_NAME, email.unsent_email_notice), (delivery.DOC_TOOL, delivery.undelivered_google_notice)):
        failed = _inv(name, "unknown_reason")
        succeeded = ToolInvocation(tool_name=name, arguments={}, result={"ok": True})
        assert notice([failed, succeeded], lang) == ""
    for tool, label in ((delivery.SLIDES_TOOL, "Google-Präsentation"), (delivery.SHEET_TOOL, "Google-Tabelle")):
        assert label in delivery.undelivered_google_notice([_inv(tool, "unknown_reason")], "de-DE")
    for tool, label in ((delivery.SLIDES_TOOL, "Présentation Google"), (delivery.SHEET_TOOL, "Feuille de calcul Google")):
        assert label in delivery.undelivered_google_notice([_inv(tool, "unknown_reason")], "fr-FR")


@pytest.mark.parametrize(("lang", "canonical"), [("de_AT", "de-DE"), ("fr-CA", "fr-FR"), ("unsupported", "en-US"), (None, "en-US")])
def test_notice_language_aliases_and_english_fallback(lang: str | None, canonical: str) -> None:
    failure = _inv(email.TOOL_NAME, "email_numbers_unverified")
    assert email.unsent_email_notice([failure], lang) == email.unsent_email_notice([failure], canonical)
    failure.arguments = {"to": "invalid\naddress"}
    notice = email.unsent_email_notice([failure], lang)
    assert "invalid" not in notice and "{to}" not in notice
    google_failure = _inv(delivery.DOC_TOOL, "google_outcome_unknown")
    assert delivery.undelivered_google_notice([google_failure], lang) == delivery.undelivered_google_notice([google_failure], canonical)
    connection_failure = _inv("teammcp.connection." + google.READ_OPERATION, "", error_kind="google_not_connected")
    assert google.google_connection_notice([connection_failure], lang) == google.google_connection_notice([connection_failure], canonical)


@pytest.mark.parametrize(
    ("lang", "email_text", "google_text"),
    [("de-DE", "Empfängeradresse", "Parameter"), ("fr-FR", "destinataire", "paramètres"), ("en-US", "recipient", "arguments")],
)
def test_actual_execution_errors_use_output_language(lang: str, email_text: str, google_text: str) -> None:
    context = LanguageContext(ui_language="zh-CN", output_language=lang)
    ctx = SimpleNamespace(language_context=context)
    email_result = asyncio.run(email._handler({"to": "bad"}, ctx))
    assert email_result["error"] == "invalid_recipient" and email_text in email_result["message"]
    google_result = asyncio.run(delivery._guard(delivery._doc)({"op": "invalid"}, ctx))
    assert google_result["error"] == "google_invalid_arguments" and google_text in google_result["message"]
    assert context.data_language == "zh-CN"


@pytest.mark.parametrize("lang", ["de-DE", "fr-FR"])
def test_actual_number_check_preserves_values_and_retry_instructions(monkeypatch: pytest.MonkeyPatch, lang: str) -> None:
    async def unavailable_numbers(text: str, ctx: object) -> list[str]:
        return ["19.75%", "EUR 42.90"]

    monkeypatch.setattr(email, "unsourced_turn_quantities", unavailable_numbers)
    ctx = SimpleNamespace(language_context=LanguageContext(output_language=lang))
    result = asyncio.run(delivery._guard(lambda args, context: delivery._numbers_ok("total", context))({}, ctx))
    assert result["error"] == "google_unsourced_numbers"
    assert result["unverified_numbers"] == ["19.75%", "EUR 42.90"]
    assert "19.75%" in result["message"] and "EUR 42.90" in result["message"]
    assert ("Berechnungstool" if lang == "de-DE" else "outil de calcul") in result["message"]
    assert ("erneut auf" if lang == "de-DE" else "à nouveau cet outil") in result["message"]
