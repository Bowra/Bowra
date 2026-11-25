"""Saving one connection asks only about the risks that connection's draft carries.

2026-11-25 (docs/sprints/model-settings-save-flow-2026-11-25): editing `deepseek_anthropic` on 116 opened a
Team-wide review because of `aliyun_qwen`'s results. A failed, stale or unknown result on a connection the
draft leaves alone was already in the applied configuration; it is reported, not signed for again. Risks on
the edited connection, and capabilities newly put to use, still need acceptance.
"""

from __future__ import annotations

import base64
import copy

import pytest
from bowhead_core.team_llm_readiness import (
    ReadinessFact,
    build_required_assignments,
    compute_team_llm_readiness,
    touched_provider_ids,
)

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
IDS = {"main": f"llmp_{'1' * 32}", "backup": f"llmp_{'2' * 32}"}


@pytest.fixture(autouse=True)
def _fernet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)


def _config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "main": {
                "type": "openai_compatible",
                "base_url": "https://main.example.com/v1",
                "api_key": "main-secret",
                "models": [{"id": "gpt-main", "capabilities": ["chat", "tool"]}],
            },
            "backup": {
                "type": "openai_compatible",
                "base_url": "https://backup.example.com/v1",
                "api_key": "backup-secret",
                "models": [{"id": "gpt-backup", "capabilities": ["chat", "tool"]}],
            },
        },
        "tiers": {
            "primary": {"provider": "main", "model": "gpt-main"},
            "primary_fallback": [{"provider": "backup", "model": "gpt-backup"}],
        },
    }


def _decide(state_by_provider: dict[str, str], *, touched: frozenset[str] | None, previous=None, accepted=frozenset()):
    rows = build_required_assignments(_config(), provider_ids=IDS)
    by_id = {value: key for key, value in IDS.items()}
    facts = {row.scope_key: ReadinessFact(state=state_by_provider[by_id[row.provider_id]], evidence_id="e") for row in rows}
    decision = compute_team_llm_readiness(
        expected_version=1,
        assignments=rows,
        facts=facts,
        previous_scopes=frozenset(row.scope_key for row in rows) if previous is None else previous(rows),
        active_acceptances=accepted(rows) if callable(accepted) else accepted,
        touched_provider_ids=touched,
    )
    return rows, decision


@pytest.mark.unit
@pytest.mark.parametrize("state", ["failed", "stale", "unknown"])
def test_risk_on_an_untouched_connection_is_a_warning(state: str) -> None:
    _rows, decision = _decide({"main": "verified", "backup": state}, touched=frozenset({IDS["main"]}))

    assert decision.may_apply
    assert decision.required_acceptances == ()
    backup = [row for row in decision.rows if row.assignment.provider_id == IDS["backup"]]
    assert {row.status for row in backup} == {"warning"}
    assert {issue.action_code for issue in decision.warnings} == {"reverify"}


@pytest.mark.unit
@pytest.mark.parametrize("state", ["failed", "stale", "unknown"])
def test_risk_on_the_edited_connection_still_needs_acceptance(state: str) -> None:
    _rows, decision = _decide({"main": "verified", "backup": state}, touched=frozenset({IDS["backup"]}))

    assert not decision.may_apply
    assert {request.assignment.provider_id for request in decision.required_acceptances} == {IDS["backup"]}


@pytest.mark.unit
def test_capability_newly_put_to_use_needs_acceptance_on_an_untouched_connection() -> None:
    _rows, decision = _decide(
        {"main": "verified", "backup": "failed"},
        touched=frozenset(),
        previous=lambda rows: frozenset(row.scope_key for row in rows if row.provider_id == IDS["main"]),
    )

    assert not decision.may_apply
    assert {request.assignment.provider_id for request in decision.required_acceptances} == {IDS["backup"]}


@pytest.mark.unit
def test_omitted_touched_set_keeps_asking_for_every_risk() -> None:
    _rows, decision = _decide({"main": "verified", "backup": "failed"}, touched=None)

    assert not decision.may_apply
    assert {request.assignment.provider_id for request in decision.required_acceptances} == {IDS["backup"]}
    assert len(decision.required_acceptances) == len([row for row in decision.rows if row.assignment.provider_id == IDS["backup"]])


@pytest.mark.unit
def test_an_accepted_risk_stays_accepted_whatever_is_touched() -> None:
    def accepted(rows):
        return frozenset(row.acceptance_key_for("capability_failed") for row in rows if row.provider_id == IDS["backup"])

    _rows, decision = _decide({"main": "verified", "backup": "failed"}, touched=frozenset({IDS["backup"]}), accepted=accepted)

    assert decision.may_apply
    assert {row.status for row in decision.rows if row.assignment.provider_id == IDS["backup"]} == {"risk_accepted"}


@pytest.mark.unit
def test_touched_connections_follow_stored_settings() -> None:
    current = _config()
    assert touched_provider_ids(current, copy.deepcopy(current), IDS) == frozenset()

    with_fingerprint = copy.deepcopy(current)
    with_fingerprint["providers"]["main"]["fingerprint"] = "sha256:display-only"
    assert touched_provider_ids(current, with_fingerprint, IDS) == frozenset()

    added_capability = copy.deepcopy(current)
    added_capability["providers"]["main"]["models"][0]["capabilities"].append("vision")
    assert touched_provider_ids(current, added_capability, IDS) == frozenset({IDS["main"]})

    new_key = copy.deepcopy(current)
    new_key["providers"]["backup"]["api_key"] = "rotated"
    assert touched_provider_ids(current, new_key, IDS) == frozenset({IDS["backup"]})

    new_connection = copy.deepcopy(current)
    new_connection["providers"]["extra"] = {"type": "openai_compatible", "base_url": "https://x.example.com/v1"}
    assert touched_provider_ids(current, new_connection, {**IDS, "extra": f"llmp_{'3' * 32}"}) == frozenset({f"llmp_{'3' * 32}"})
