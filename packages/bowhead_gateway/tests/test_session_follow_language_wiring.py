"""Web chat and channel turns read the previous turn's language with the German / French aware judge.

Web chat and the IM / scheduled channels (through `channel_language`) resolve "same session,
previous turn" from the persisted `final_answer`, so a German or French answer followed by a bare "1" must come back as that language on either
surface (the resolver itself is covered in core's `test_session_follow_language.py`).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from bowhead_gateway.channel_language import session_last_output_language
from bowhead_gateway.routes.chat import _resolve_session_last_output_language

pytestmark = pytest.mark.unit

FRENCH_ANSWER = (
    "Le chiffre d'affaires du mois dernier est en hausse de 5 % par rapport au mois précédent. "
    "Quelle région voulez-vous que j'analyse en détail ? 1. Nord 2. Sud"
)
GERMAN_ANSWER = "Der Umsatz im letzten Monat ist gegenüber dem Vormonat um 5 % gestiegen. Welche Region soll ich mir genauer ansehen? 1. Nord 2. Süd"
ENGLISH_ANSWER = "Revenue last month was up 5% from the previous month. Which region should I look at? 1. North 2. South"


class _Store:
    def __init__(self, *answers: str) -> None:
        self._turns = [SimpleNamespace(final_answer=answer) for answer in answers]

    async def list_turns(self, *, session_id: str, limit: int) -> list[SimpleNamespace]:
        return self._turns[-limit:]


def _app(*answers: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(orchestrator=SimpleNamespace(session_store=_Store(*answers))))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [(FRENCH_ANSWER, "fr-FR"), (GERMAN_ANSWER, "de-DE"), (ENGLISH_ANSWER, "en-US")],
    ids=["fr", "de", "en"],
)
async def test_web_and_channels_follow_the_same_previous_answer_language(answer: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    app = _app(answer)
    web = await _resolve_session_last_output_language(SimpleNamespace(app=app), "sess_1", log=logging.getLogger(__name__))
    channel = await session_last_output_language(app, "sess_1")
    assert web == channel == expected
