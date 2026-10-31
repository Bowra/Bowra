"""Encrypted snapshot and page cursor codec for Results queries."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_ROOT_KEY_ENV = "BOWHEAD_FERNET_KEY"
_KEY_DOMAIN = b"bowhead/results-cursor/v1"
_TOKEN_VERSION = 2
_MAX_TTL_SECONDS = 2 * 60 * 60
_MAX_CLOCK_SKEW_SECONDS = 60

ResultsCursorDirection = Literal["next", "previous"]
ResultsCursorFailureReason = Literal[
    "expired",
    "future",
    "malformed",
    "tampered",
    "binding_mismatch",
    "snapshot_mismatch",
]
SortValue = str | int | float | bool | None


class ResultsCursorUnavailable(RuntimeError):
    """The instance-stable Fernet root is missing or malformed."""


class ResultsCursorTokenError(ValueError):
    """A cursor is unsafe to reuse and must be treated as stale."""

    def __init__(self, reason: ResultsCursorFailureReason) -> None:
        super().__init__("results cursor is stale")
        self.reason = reason


@dataclass(frozen=True)
class ResultsCursorBinding:
    scope_fingerprint: str
    query_filter_hash: str
    sort_version: str


@dataclass(frozen=True)
class ResultsSnapshot:
    issued_at: int
    expires_at: int
    high_water_created_at: str | None
    high_water_artifact_id: str | None
    membership_cutoff_at: str
    scope_fingerprint: str
    query_filter_hash: str
    sort_version: str
    projection_digest: str
    presentation_high_water: int = 0


@dataclass(frozen=True)
class ResultsPageCursor:
    issued_at: int
    expires_at: int
    direction: ResultsCursorDirection
    sort_tuple: tuple[SortValue, ...]


class _TokenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _SnapshotPayload(_TokenModel):
    version: Literal[1, 2] = _TOKEN_VERSION
    kind: Literal["snapshot"] = "snapshot"
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    high_water_created_at: str | None = Field(default=None, min_length=1, max_length=64)
    high_water_artifact_id: str | None = Field(default=None, min_length=1, max_length=128)
    membership_cutoff_at: str = Field(min_length=1, max_length=64)
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_filter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sort_version: str = Field(min_length=1, max_length=64)
    projection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    presentation_high_water: int = Field(default=0, ge=0)


class _PagePayload(_TokenModel):
    version: Literal[1, 2] = _TOKEN_VERSION
    kind: Literal["page"] = "page"
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    snapshot_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: ResultsCursorDirection
    sort_tuple: list[SortValue] = Field(min_length=1, max_length=32)


def _decode_root_key(raw: str) -> bytes | None:
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
    return decoded if len(decoded) == 32 else None


def _derive_fernet(root_key: bytes) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_KEY_DOMAIN,
    ).derive(root_key)
    return Fernet(base64.urlsafe_b64encode(derived))


def _canonical_json(payload: BaseModel) -> bytes:
    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _snapshot_ref(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class ResultsCursorCodec:
    def __init__(self, root_key: bytes, *, now: Callable[[], float] | None = None) -> None:
        if len(root_key) != 32:
            raise ResultsCursorUnavailable("invalid Fernet root")
        self._fernet = _derive_fernet(root_key)
        self._now = now or time.time

    @classmethod
    def from_environment(cls, *, now: Callable[[], float] | None = None) -> ResultsCursorCodec:
        root_key = _decode_root_key(os.environ.get(_ROOT_KEY_ENV, "").strip())
        if root_key is None:
            raise ResultsCursorUnavailable("BOWHEAD_FERNET_KEY unavailable")
        return cls(root_key, now=now)

    def issue_snapshot(
        self,
        *,
        high_water_created_at: str | None,
        high_water_artifact_id: str | None,
        membership_cutoff_at: str,
        binding: ResultsCursorBinding,
        projection_digest: str,
        presentation_high_water: int = 0,
        ttl_seconds: int = _MAX_TTL_SECONDS,
    ) -> tuple[str, ResultsSnapshot]:
        if not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("snapshot ttl must be between 1 and 7200 seconds")
        if (high_water_created_at is None) != (high_water_artifact_id is None):
            raise ValueError("snapshot high-water fields must both be set or both be null")
        issued_at = int(self._now())
        payload = _SnapshotPayload(
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            high_water_created_at=high_water_created_at,
            high_water_artifact_id=high_water_artifact_id,
            membership_cutoff_at=membership_cutoff_at,
            scope_fingerprint=binding.scope_fingerprint,
            query_filter_hash=binding.query_filter_hash,
            sort_version=binding.sort_version,
            projection_digest=projection_digest,
            presentation_high_water=presentation_high_water,
        )
        token = self._fernet.encrypt(_canonical_json(payload)).decode("ascii")
        return token, self._snapshot_from_payload(payload)

    def read_snapshot(
        self,
        token: str,
        *,
        expected: ResultsCursorBinding,
    ) -> ResultsSnapshot:
        payload = self._read_payload(token, _SnapshotPayload)
        self._validate_time(payload.issued_at, payload.expires_at)
        if (
            payload.scope_fingerprint != expected.scope_fingerprint
            or payload.query_filter_hash != expected.query_filter_hash
            or payload.sort_version != expected.sort_version
        ):
            raise ResultsCursorTokenError("binding_mismatch")
        return self._snapshot_from_payload(payload)

    def issue_page_cursor(
        self,
        *,
        snapshot_token: str,
        expected: ResultsCursorBinding,
        direction: ResultsCursorDirection,
        sort_tuple: tuple[SortValue, ...],
    ) -> str:
        snapshot = self.read_snapshot(snapshot_token, expected=expected)
        payload = _PagePayload(
            issued_at=snapshot.issued_at,
            expires_at=snapshot.expires_at,
            snapshot_ref=_snapshot_ref(snapshot_token),
            direction=direction,
            sort_tuple=list(sort_tuple),
        )
        return self._fernet.encrypt(_canonical_json(payload)).decode("ascii")

    def read_page_cursor(
        self,
        token: str,
        *,
        snapshot_token: str,
        expected: ResultsCursorBinding,
    ) -> ResultsPageCursor:
        payload = self._read_payload(token, _PagePayload)
        self._validate_time(payload.issued_at, payload.expires_at)
        if payload.snapshot_ref != _snapshot_ref(snapshot_token):
            raise ResultsCursorTokenError("snapshot_mismatch")
        snapshot = self.read_snapshot(snapshot_token, expected=expected)
        if payload.issued_at != snapshot.issued_at or payload.expires_at != snapshot.expires_at:
            raise ResultsCursorTokenError("snapshot_mismatch")
        return ResultsPageCursor(
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            direction=payload.direction,
            sort_tuple=tuple(payload.sort_tuple),
        )

    def _read_payload[PayloadT: BaseModel](
        self,
        token: str,
        model: type[PayloadT],
    ) -> PayloadT:
        try:
            decrypted = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise ResultsCursorTokenError("tampered") from exc
        try:
            return model.model_validate_json(decrypted)
        except ValidationError as exc:
            raise ResultsCursorTokenError("malformed") from exc

    def _validate_time(self, issued_at: int, expires_at: int) -> None:
        now = int(self._now())
        if issued_at > now + _MAX_CLOCK_SKEW_SECONDS:
            raise ResultsCursorTokenError("future")
        if expires_at <= issued_at or now >= expires_at:
            raise ResultsCursorTokenError("expired")

    @staticmethod
    def _snapshot_from_payload(payload: _SnapshotPayload) -> ResultsSnapshot:
        return ResultsSnapshot(
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            high_water_created_at=payload.high_water_created_at,
            high_water_artifact_id=payload.high_water_artifact_id,
            membership_cutoff_at=payload.membership_cutoff_at,
            scope_fingerprint=payload.scope_fingerprint,
            query_filter_hash=payload.query_filter_hash,
            sort_version=payload.sort_version,
            projection_digest=payload.projection_digest,
            presentation_high_water=payload.presentation_high_water,
        )


def results_cursor_configured() -> bool:
    try:
        ResultsCursorCodec.from_environment()
    except ResultsCursorUnavailable:
        return False
    return True


__all__ = [
    "ResultsCursorBinding",
    "ResultsCursorCodec",
    "ResultsCursorDirection",
    "ResultsCursorTokenError",
    "ResultsCursorUnavailable",
    "ResultsPageCursor",
    "ResultsSnapshot",
    "results_cursor_configured",
]
