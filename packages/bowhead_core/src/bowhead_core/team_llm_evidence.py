"""Team LLM identity, signature and Evidence projection primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from bowhead_router.transport import is_sensitive_query_name
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from bowhead_router.transport import CanonicalProviderTransportPlan

SIGNATURE_VERSION = "llm-config-signature-v1"
PROVIDER_ID_PREFIX = "llmp_"
_SIGNATURE_KEY_DOMAIN = b"bowhead/llm-evidence/signature-v1"
_PROVIDER_ID_KEY_DOMAIN = b"bowhead/llm-provider-identity/v1"
_CONTROL_TOKEN_KEY_DOMAIN = b"bowhead/llm-evidence/control-token-v1"

Capability = Literal["chat", "tool", "vision", "embed", "rerank"]
EvidenceOutcome = Literal["verified", "failed", "not_testable"]
FactState = Literal[
    "unknown",
    "verified",
    "aged",
    "failed",
    "not_testable",
    "stale",
]
AttemptState = Literal[
    "queued",
    "verifying",
    "errored",
    "cancelled",
    "interrupted",
]


class TeamLlmSignatureKeyError(ValueError):
    """The evidence root key is missing or malformed."""


def _root_key(value: str | bytes | None) -> bytes:
    configured = value if value is not None else os.environ.get("BOWHEAD_FERNET_KEY")
    if not configured:
        raise TeamLlmSignatureKeyError("BOWHEAD_FERNET_KEY is required for V2 evidence")
    raw = configured.encode("ascii") if isinstance(configured, str) else configured
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise TeamLlmSignatureKeyError("invalid BOWHEAD_FERNET_KEY") from exc
    if len(decoded) != 32:
        raise TeamLlmSignatureKeyError("BOWHEAD_FERNET_KEY must decode to 32 bytes")
    return decoded


def _derive_key(root: str | bytes | None, domain: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=domain,
    ).derive(_root_key(root))


def control_token_key(
    *,
    root_key: str | bytes | None = None,
) -> bytes:
    """Return the domain-separated key for short-lived control tokens."""

    return _derive_key(root_key, _CONTROL_TOKEN_KEY_DOMAIN)


def new_provider_id() -> str:
    return f"{PROVIDER_ID_PREFIX}{uuid4().hex}"


def legacy_provider_id(
    team_id: str,
    runtime_key: str,
    *,
    root_key: str | bytes | None = None,
) -> str:
    """Return a deterministic, tenant-scoped ID for an existing Provider."""

    normalized_team = team_id.strip()
    normalized_key = runtime_key.strip()
    if not normalized_team or not normalized_key:
        raise ValueError("team_id and runtime_key are required")
    digest = hmac.new(
        _derive_key(root_key, _PROVIDER_ID_KEY_DOMAIN),
        f"{normalized_team}\0{normalized_key}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{PROVIDER_ID_PREFIX}{digest}"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compute_config_signature(
    transport: CanonicalProviderTransportPlan,
    *,
    provider_id: str,
    model_id: str,
    capability: Capability,
    testability_contract_version: str,
    probe_contract_version: str | None,
    probe_execution_policy_version: str,
    request_options: dict[str, Any] | None = None,
    root_key: str | bytes | None = None,
) -> str:
    """Hash the request semantics proven by one capability Evidence."""

    normalized_model = model_id.strip()
    if not provider_id.startswith(PROVIDER_ID_PREFIX) or not normalized_model:
        raise ValueError("provider_id and model_id are invalid")

    signing_key = _derive_key(root_key, _SIGNATURE_KEY_DOMAIN)
    secret_rows = [
        {
            "kind": component.kind,
            "name": component.name.lower(),
            "semantic": component.semantic,
            "digest": hmac.new(
                signing_key,
                f"{component.kind}\0{component.name.lower()}\0{component.semantic}\0{component.value}".encode(),
                hashlib.sha256,
            ).hexdigest(),
        }
        for component in transport.secret_components
    ]
    credential_digest = hashlib.sha256(canonical_json(secret_rows).encode()).hexdigest()
    parsed = urlsplit(transport.request_url)
    scheme = parsed.scheme.lower()
    effective_port = parsed.port or (443 if scheme == "https" else 80)
    effective_request = {
        "method": transport.method.upper(),
        "scheme": scheme,
        "host": (parsed.hostname or "").encode("idna").decode("ascii").lower(),
        "port": effective_port,
        "path": parsed.path or "/",
        "non_secret_query": [
            [key, value]
            for key, value in sorted(
                parse_qsl(parsed.query, keep_blank_values=True),
            )
            if not is_sensitive_query_name(key)
        ],
        "auth_scheme": [component.semantic for component in transport.secret_components],
        "credential_digest": credential_digest,
        "protocol_headers": [[key.lower(), value] for key, value in sorted(transport.safe_header_semantics)],
    }
    payload = {
        "signature_version": SIGNATURE_VERSION,
        "adapter_type": transport.adapter_type,
        "adapter_contract_version": transport.adapter_contract_version,
        "provider_id": provider_id,
        "effective_request": effective_request,
        "model_id": normalized_model,
        "capability": capability,
        "testability_contract_version": testability_contract_version,
        "probe_contract_version": probe_contract_version,
        "probe_execution_policy_version": probe_execution_policy_version,
        "request_options": {
            **dict(transport.request_options),
            **(request_options or {}),
        },
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    config_signature: str
    outcome: EvidenceOutcome
    safe_code: str
    observed_at: datetime
    fresh_until: datetime
    #: Provider-reported model ID (if different from config, it tells us what was actually returned); since 2026-08-25,
    #: differing names no longer fail the check; historical rows may still carry `model_identity_mismatch`.
    #: Rows stored before 0018 have None: read as "not recorded", not "matching".
    actual_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    state: AttemptState
    safe_code: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvidenceProjection:
    fact_state: FactState
    evidence_id: str | None
    observed_at: datetime | None
    fresh_until: datetime | None
    safe_code: str | None
    attempt_state: AttemptState | None
    attempt_safe_code: str | None
    actual_model_id: str | None = None


def project_latest_evidence(
    evidence: list[EvidenceRecord],
    *,
    current_signature: str,
    now: datetime,
    attempt: AttemptRecord | None = None,
) -> EvidenceProjection:
    """Project append-only facts without letting an Attempt overwrite them."""

    ordered = sorted(
        evidence,
        key=lambda row: (_utc(row.observed_at), row.evidence_id),
        reverse=True,
    )
    exact = next(
        (row for row in ordered if hmac.compare_digest(row.config_signature, current_signature)),
        None,
    )
    if exact is None:
        state: FactState = "stale" if ordered else "unknown"
    elif exact.outcome == "verified":
        state = "verified" if _utc(now) <= _utc(exact.fresh_until) else "aged"
    else:
        state = exact.outcome
    return EvidenceProjection(
        fact_state=state,
        evidence_id=exact.evidence_id if exact else None,
        observed_at=exact.observed_at if exact else None,
        fresh_until=exact.fresh_until if exact else None,
        safe_code=exact.safe_code if exact else None,
        attempt_state=attempt.state if attempt else None,
        attempt_safe_code=attempt.safe_code if attempt else None,
        actual_model_id=exact.actual_model_id if exact else None,
    )
