from __future__ import annotations

import base64

import pytest
from bowhead_gateway.results_cursor import (
    ResultsCursorBinding,
    ResultsCursorCodec,
    ResultsCursorTokenError,
    ResultsCursorUnavailable,
    results_cursor_configured,
)
from cryptography.fernet import Fernet, InvalidToken

_MEMBERSHIP_CUTOFF = "2026-08-24T12:00:00.000000+00:00"


def _root_key() -> bytes:
    return base64.urlsafe_b64decode(Fernet.generate_key())


def _binding(suffix: str = "") -> ResultsCursorBinding:
    return ResultsCursorBinding(
        scope_fingerprint=f"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef{suffix}",
        query_filter_hash="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        sort_version="results-sort-v1",
    )


@pytest.mark.unit
def test_snapshot_and_page_cursor_round_trip() -> None:
    now = [1_000.0]
    codec = ResultsCursorCodec(_root_key(), now=lambda: now[0])
    token, snapshot = codec.issue_snapshot(
        high_water_created_at="2026-08-24T12:00:00+00:00",
        high_water_artifact_id="art_high_water",
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
        ttl_seconds=60,
    )

    decoded = codec.read_snapshot(token, expected=_binding())
    page_token = codec.issue_page_cursor(
        snapshot_token=token,
        expected=_binding(),
        direction="next",
        sort_tuple=(0, "2026-08-24T12:00:00+00:00", "artifact:art_high_water"),
    )
    page = codec.read_page_cursor(
        page_token,
        snapshot_token=token,
        expected=_binding(),
    )

    assert decoded == snapshot
    assert decoded.expires_at == 1_060
    assert decoded.membership_cutoff_at == _MEMBERSHIP_CUTOFF
    assert page.direction == "next"
    assert page.sort_tuple[-1] == "artifact:art_high_water"


@pytest.mark.unit
def test_empty_snapshot_and_key_domain_are_explicit() -> None:
    root = _root_key()
    codec = ResultsCursorCodec(root, now=lambda: 1_000.0)
    token, snapshot = codec.issue_snapshot(
        high_water_created_at=None,
        high_water_artifact_id=None,
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
    )

    assert snapshot.high_water_created_at is None
    assert snapshot.high_water_artifact_id is None
    assert "art_high_water" not in token
    with pytest.raises(InvalidToken):
        Fernet(base64.urlsafe_b64encode(root)).decrypt(token.encode("ascii"))

    with pytest.raises(ValueError, match="both be set"):
        codec.issue_snapshot(
            high_water_created_at="2026-08-24T12:00:00+00:00",
            high_water_artifact_id=None,
            membership_cutoff_at=_MEMBERSHIP_CUTOFF,
            binding=_binding(),
            projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
        )


@pytest.mark.unit
def test_cursor_binding_and_snapshot_reference_fail_closed() -> None:
    codec = ResultsCursorCodec(_root_key(), now=lambda: 1_000.0)
    token, _ = codec.issue_snapshot(
        high_water_created_at="2026-08-24T12:00:00+00:00",
        high_water_artifact_id="art_high_water",
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
    )
    other_token, _ = codec.issue_snapshot(
        high_water_created_at="2026-08-24T12:00:00+00:00",
        high_water_artifact_id="art_high_water",
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="ffeeddccbbaa00998877665544332211ffeeddccbbaa00998877665544332211",
    )
    page_token = codec.issue_page_cursor(
        snapshot_token=token,
        expected=_binding(),
        direction="previous",
        sort_tuple=(1, "artifact:art_high_water"),
    )

    with pytest.raises(ResultsCursorTokenError, match="stale") as binding_error:
        codec.read_snapshot(token, expected=_binding("-other"))
    with pytest.raises(ResultsCursorTokenError, match="stale") as snapshot_error:
        codec.read_page_cursor(
            page_token,
            snapshot_token=other_token,
            expected=_binding(),
        )

    assert binding_error.value.reason == "binding_mismatch"
    assert snapshot_error.value.reason == "snapshot_mismatch"


@pytest.mark.unit
def test_cursor_expiry_and_tamper_fail_closed() -> None:
    now = [1_000.0]
    codec = ResultsCursorCodec(_root_key(), now=lambda: now[0])
    token, _ = codec.issue_snapshot(
        high_water_created_at="2026-08-24T12:00:00+00:00",
        high_water_artifact_id="art_high_water",
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
        ttl_seconds=10,
    )
    tampered = f"{token[:-2]}{'A' if token[-2] != 'A' else 'B'}{token[-1]}"

    with pytest.raises(ResultsCursorTokenError) as tamper_error:
        codec.read_snapshot(tampered, expected=_binding())
    now[0] = 1_010.0
    with pytest.raises(ResultsCursorTokenError) as expiry_error:
        codec.read_snapshot(token, expected=_binding())

    assert tamper_error.value.reason == "tampered"
    assert expiry_error.value.reason == "expired"


@pytest.mark.unit
def test_future_cursor_fails_closed() -> None:
    root = _root_key()
    issuer = ResultsCursorCodec(root, now=lambda: 1_100.0)
    reader = ResultsCursorCodec(root, now=lambda: 1_000.0)
    token, _ = issuer.issue_snapshot(
        high_water_created_at=None,
        high_water_artifact_id=None,
        membership_cutoff_at=_MEMBERSHIP_CUTOFF,
        binding=_binding(),
        projection_digest="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
    )

    with pytest.raises(ResultsCursorTokenError) as error:
        reader.read_snapshot(token, expected=_binding())

    assert error.value.reason == "future"


@pytest.mark.unit
def test_cursor_environment_readiness_requires_valid_fernet_root(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    assert results_cursor_configured() is False
    with pytest.raises(ResultsCursorUnavailable):
        ResultsCursorCodec.from_environment()

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "invalid")
    assert results_cursor_configured() is False

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    assert results_cursor_configured() is True
