"""Team-scoped rollout policy for unreleased Composer model controls.

The responsive Composer is the only product UI.  This module retains only the
future model-control batches and the submission-recovery key substrate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MODEL_PANEL_FLAG = "FEATURE_COMPOSER_MODEL_PANEL_V2"
_THINKING_FLAG = "FEATURE_THINKING_EFFORT"
_SUBMISSION_FINGERPRINT_KEY = "BOWHEAD_SUBMISSION_FINGERPRINT_KEY"
_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS = "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS"
_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL = "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL"
_FERNET_ROOT_KEY = "BOWHEAD_FERNET_KEY"
_DERIVED_SUBMISSION_KEY_DOMAIN = b"bowhead/chat-submission-fingerprint/v1"
_ROLLOUT_SALT = "composer-model-controls-v1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_MODEL_PANEL_RELEASED = False
_THINKING_EFFORT_RELEASED = False
# The crash-recovery submission journal is retained for compatibility, but it
# is no longer part of the default Composer send path. Re-enable only through
# a separately reviewed protocol release, not through key readiness.
_SUBMISSION_PROTOCOL_ENABLED = False


@dataclass(frozen=True)
class ComposerCapabilities:
    model_panel_v2: bool
    thinking_effort: bool


@dataclass(frozen=True)
class ComposerDeploymentProjection:
    capabilities: ComposerCapabilities
    matrix_valid: bool


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in _TRUE_VALUES


def _decode_fingerprint_key(raw: str) -> bytes | None:
    if not raw:
        return None
    try:
        decoded = base64.b64decode(
            f"{raw}{'=' * (-len(raw) % 4)}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return None
    return decoded if len(decoded) >= 32 else None


def _fingerprint_key_id(key: bytes) -> str:
    return hashlib.sha256(b"bowhead/chat-submission-key-id/v1\0" + key).hexdigest()


def _derived_fingerprint_key() -> bytes | None:
    """Derive a stable default without introducing a deployment setting."""

    root = _decode_fingerprint_key(os.environ.get(_FERNET_ROOT_KEY, "").strip())
    if root is None or len(root) != 32:
        return None
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_DERIVED_SUBMISSION_KEY_DOMAIN,
    ).derive(root)


def submission_fingerprint_keyring() -> tuple[tuple[str, bytes], ...]:
    """Return current then accepted previous HMAC keys with opaque key IDs.

    An explicit current key remains available for deployments that require an
    independently rotated secret. When it is absent or blank, the default is
    derived from the instance-stable Fernet root with a dedicated HKDF domain,
    so existing installations need no new setting. A malformed non-empty
    override still fails closed. Previous keys are a JSON array of base64url
    strings. Their raw values are never persisted; receipts retain only the
    derived SHA-256 key ID. Keeping the prior key available for the receipt
    retention window makes a same-ID retry stable across normal secret
    rotation.
    """
    raw_current = os.environ.get(_SUBMISSION_FINGERPRINT_KEY, "").strip()
    current = _decode_fingerprint_key(raw_current) if raw_current else _derived_fingerprint_key()
    if current is None:
        return ()
    keys = [current]
    raw_previous = os.environ.get(_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS, "").strip()
    if raw_previous:
        try:
            decoded_previous = json.loads(raw_previous)
        except json.JSONDecodeError:
            return ()
        if not isinstance(decoded_previous, list):
            return ()
        if len(decoded_previous) > 1:
            return ()
        if decoded_previous:
            raw_accept_until = os.environ.get(
                _SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL,
                "",
            ).strip()
            try:
                accept_until = datetime.fromisoformat(
                    raw_accept_until.replace("Z", "+00:00"),
                )
            except ValueError:
                return ()
            if accept_until.tzinfo is None:
                return ()
            now = datetime.now(tz=UTC)
            if accept_until > now + timedelta(days=8):
                return ()
            # Retirement is not a global outage.  Once the immutable eight-day
            # retry window has elapsed, ignore the stale previous-key setting
            # and continue serving with the valid current epoch.
            if accept_until > now:
                for raw in decoded_previous:
                    if not isinstance(raw, str):
                        return ()
                    key = _decode_fingerprint_key(raw.strip())
                    if key is None:
                        return ()
                    keys.append(key)
    unique: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for key in keys:
        key_id = _fingerprint_key_id(key)
        if key_id in seen:
            return ()
        seen.add(key_id)
        unique.append((key_id, key))
    return tuple(unique)


def submission_fingerprint_key() -> bytes | None:
    """Compatibility accessor for the active fingerprint key."""
    ring = submission_fingerprint_keyring()
    return ring[0][1] if ring else None


def submission_fingerprint_previous_accept_until() -> str | None:
    """Return the canonical active previous deadline, never an expired setting."""
    if len(submission_fingerprint_keyring()) != 2:
        return None
    raw = os.environ.get(
        _SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL,
        "",
    ).strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC).isoformat()


def _submission_protocol_ready() -> bool:
    return bool(submission_fingerprint_keyring())


def composer_submission_protocol_ready() -> bool:
    """Public, content-free readiness for pending recovery across UI rollback."""
    return _submission_protocol_ready()


def composer_submission_protocol_enabled() -> bool:
    """Whether new browser submissions may opt into the recovery protocol."""
    return _SUBMISSION_PROTOCOL_ENABLED


def _percentage(name: str) -> int:
    raw = os.environ.get(f"{name}_PCT", "0").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= 100 else 0


def _team_allowlist(name: str) -> frozenset[str]:
    return frozenset(team_id.strip() for team_id in os.environ.get(f"{name}_TEAM_IDS", "").split(",") if team_id.strip())


def stable_composer_team_bucket(feature: str, team_id: str) -> int:
    payload = f"{_ROLLOUT_SALT}\0{feature}\0{team_id}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _team_gate(feature: str, team_id: str) -> bool:
    if not team_id or not _flag_enabled(feature):
        return False
    if team_id in _team_allowlist(feature):
        return True
    percentage = _percentage(feature)
    return percentage == 100 or (percentage > 0 and stable_composer_team_bucket(feature, team_id) < percentage)


def team_rollout_enabled(feature: str, team_id: str) -> bool:
    """Shared team-scoped rollout gate for ``FEATURE_<NAME>`` style switches.

    The master flag must be on; ``<feature>_TEAM_IDS`` allowlists teams first,
    then ``<feature>_PCT`` selects a stable per-feature team bucket. Other
    rollout modules reuse this instead of copying the selection logic.
    """

    return _team_gate(feature, team_id)


def resolve_composer_capabilities(team_id: str) -> ComposerCapabilities:
    """Resolve only the unreleased model-control capabilities."""

    # Batch B/C remain design-only. Environment values are intentionally unable
    # to advertise capabilities until their member-safe APIs and UI ship.
    model_panel_v2 = _MODEL_PANEL_RELEASED and _team_gate(_MODEL_PANEL_FLAG, team_id)
    thinking_effort = model_panel_v2 and _THINKING_EFFORT_RELEASED and _team_gate(_THINKING_FLAG, team_id)
    return ComposerCapabilities(
        model_panel_v2=model_panel_v2,
        thinking_effort=thinking_effort,
    )


def composer_deployment_projection() -> ComposerDeploymentProjection:
    """Return only deployment master state for operational diagnostics."""

    requested_model_panel = _flag_enabled(_MODEL_PANEL_FLAG)
    requested_thinking = _flag_enabled(_THINKING_FLAG)
    raw_model_panel = _MODEL_PANEL_RELEASED and requested_model_panel
    raw_thinking = raw_model_panel and _THINKING_EFFORT_RELEASED and requested_thinking
    return ComposerDeploymentProjection(
        capabilities=ComposerCapabilities(
            model_panel_v2=raw_model_panel,
            thinking_effort=raw_thinking,
        ),
        matrix_valid=not requested_thinking or requested_model_panel,
    )


__all__ = [
    "ComposerCapabilities",
    "ComposerDeploymentProjection",
    "composer_deployment_projection",
    "composer_submission_protocol_enabled",
    "composer_submission_protocol_ready",
    "resolve_composer_capabilities",
    "submission_fingerprint_key",
    "submission_fingerprint_keyring",
    "stable_composer_team_bucket",
    "team_rollout_enabled",
]
