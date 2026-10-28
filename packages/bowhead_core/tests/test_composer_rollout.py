from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.composer_rollout import (
    composer_deployment_projection,
    composer_submission_protocol_enabled,
    composer_submission_protocol_ready,
    resolve_composer_capabilities,
    submission_fingerprint_keyring,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_FLAGS = (
    "FEATURE_COMPOSER_MODEL_PANEL_V2",
    "FEATURE_THINKING_EFFORT",
)
_TEST_FINGERPRINT_KEY = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _clear_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in _FLAGS:
        monkeypatch.delenv(flag, raising=False)
        monkeypatch.delenv(f"{flag}_TEAM_IDS", raising=False)
        monkeypatch.delenv(f"{flag}_PCT", raising=False)
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", _TEST_FINGERPRINT_KEY)
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", raising=False)
    monkeypatch.delenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        raising=False,
    )


def test_unreleased_model_controls_are_off_without_team_or_config() -> None:
    result = resolve_composer_capabilities("team-a")
    assert result.model_panel_v2 is False
    assert result.thinking_effort is False


def test_unreleased_model_control_batches_cannot_be_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for flag in _FLAGS:
        monkeypatch.setenv(flag, "1")
        monkeypatch.setenv(f"{flag}_PCT", "0")
        monkeypatch.setenv(f"{flag}_TEAM_IDS", "team-a")

    enabled = resolve_composer_capabilities("team-a")
    assert enabled.model_panel_v2 is False
    assert enabled.thinking_effort is False

    projection = composer_deployment_projection()
    assert projection.capabilities.model_panel_v2 is False
    assert projection.capabilities.thinking_effort is False
    assert projection.matrix_valid is True


def test_deployment_projection_reports_invalid_raw_dependency_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_THINKING_EFFORT", "1")
    projection = composer_deployment_projection()
    assert projection.matrix_valid is False
    assert projection.capabilities.thinking_effort is False


def test_submission_protocol_stays_off_without_any_instance_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY")
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)

    assert composer_submission_protocol_ready() is False
    assert composer_submission_protocol_enabled() is False
    projection = composer_deployment_projection()
    assert projection.matrix_valid is True


def test_submission_keyring_derives_stable_default_from_instance_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = b"f" * 32
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY")
    monkeypatch.setenv(
        "BOWHEAD_FERNET_KEY",
        base64.urlsafe_b64encode(root).decode(),
    )
    expected = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"bowhead/chat-submission-fingerprint/v1",
    ).derive(root)
    first = submission_fingerprint_keyring()
    second = submission_fingerprint_keyring()

    assert [key for _key_id, key in first] == [expected]
    assert second == first
    assert expected != root
    assert composer_submission_protocol_ready() is True
    assert composer_submission_protocol_enabled() is False


@pytest.mark.parametrize("bad_key", ("k" * 32, "not-base64!", "a2V5"))
def test_malformed_submission_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    bad_key: str,
) -> None:
    monkeypatch.setenv(
        "BOWHEAD_FERNET_KEY",
        base64.urlsafe_b64encode(b"f" * 32).decode(),
    )
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", bad_key)
    assert submission_fingerprint_keyring() == ()


def test_submission_keyring_accepts_bounded_previous_epochs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", f'["{previous}"]')
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) + timedelta(days=7)).isoformat(),
    )

    ring = submission_fingerprint_keyring()

    assert [key for _key_id, key in ring] == [b"k" * 32, b"p" * 32]
    assert len({key_id for key_id, _key in ring}) == 2


@pytest.mark.parametrize("bad_previous", ('"not-an-array"', '["short"]', "not-json"))
def test_submission_keyring_fails_closed_on_invalid_previous_epochs(
    monkeypatch: pytest.MonkeyPatch,
    bad_previous: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", bad_previous)
    assert submission_fingerprint_keyring() == ()


def test_submission_keyring_rejects_unbounded_or_duplicate_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    current = _TEST_FINGERPRINT_KEY
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) + timedelta(days=7)).isoformat(),
    )
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS",
        f'["{previous}","{current}"]',
    )
    assert submission_fingerprint_keyring() == ()

    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", f'["{previous}"]')
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) + timedelta(days=9)).isoformat(),
    )
    assert submission_fingerprint_keyring() == ()

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) + timedelta(days=7)).isoformat(),
    )
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS",
        f'["{current}"]',
    )
    assert submission_fingerprint_keyring() == ()


def test_submission_keyring_requires_a_live_bounded_acceptance_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", f'["{previous}"]')
    assert submission_fingerprint_keyring() == ()
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert [key for _key_id, key in submission_fingerprint_keyring()] == [b"k" * 32]
