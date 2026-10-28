"""Server-side invitation intent and activation orchestration."""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bowhead_core._email_utils import mask_email
from bowhead_core.user_store import User, UserStore

from bowhead_gateway.auth_policy import (
    AccountCreationPolicy,
    AccountPolicy,
    EmailVerificationPolicy,
)
from bowhead_gateway.routes.auth import (
    _decode_invite_jwt,
    _invite_row_status,
    _payload_invite_type,
    _row_matches_payload,
)


class InviteFlowError(ValueError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class InviteContext:
    intent_hash: str
    invite_type: str
    invite_id: str
    token_version: int
    row: dict[str, Any]
    team_id: str
    team_name: str
    workspace_id: str | None
    workspace_name: str | None
    inviter_display_name: str | None = None

    @property
    def invited_email(self) -> str:
        return str(self.row.get("invited_email") or "").lower().strip()


async def exchange_invite_token(
    store: UserStore,
    raw_token: str,
) -> tuple[str, dict[str, Any]]:
    decode_status, payload = _decode_invite_jwt(raw_token)
    if decode_status == "expired":
        raise InviteFlowError("invite_expired", status_code=410)
    if decode_status != "ok" or payload is None:
        raise InviteFlowError("invite_invalid", status_code=401)
    invite_type = _payload_invite_type(payload)
    if invite_type is None:
        raise InviteFlowError("invite_invalid", status_code=401)

    invite_id = str(payload.get("invite_id") or "")
    if invite_id:
        row = await store.get_team_invite_by_id(invite_id) if invite_type == "team" else await store.get_workspace_invite_by_id(invite_id)
    else:
        row = (
            await store.get_team_invite_by_token_hash(raw_token)
            if invite_type == "team"
            else await store.get_workspace_invite_by_token_hash(raw_token)
        )
        invite_id = str((row or {}).get("id") or "")
    if row is None or not invite_id or not _row_matches_payload(invite_type, row, payload):
        raise InviteFlowError("invite_invalid", status_code=401)

    exact_hash_row = (
        await store.get_team_invite_by_token_hash(raw_token) if invite_type == "team" else await store.get_workspace_invite_by_token_hash(raw_token)
    )
    if exact_hash_row is None or exact_hash_row.get("id") != invite_id:
        raise InviteFlowError("invite_invalid", status_code=401)

    row_status = _invite_row_status(row)
    if row_status != "valid":
        status_code = 410 if row_status in {"expired", "revoked"} else 409
        raise InviteFlowError(f"invite_{row_status}", status_code=status_code)

    row_version = int(row.get("token_version") or 1)
    signed_version = payload.get("token_version")
    if signed_version is None:
        if row_version != 1:
            raise InviteFlowError("invite_invalid", status_code=401)
    elif int(signed_version) != row_version:
        raise InviteFlowError("invite_invalid", status_code=401)
    if payload.get("typ") not in {None, "bowhead-invite"}:
        raise InviteFlowError("invite_invalid", status_code=401)

    raw_secret = secrets.token_urlsafe(32)
    intent_expiry = min(
        datetime.now(UTC) + timedelta(minutes=15),
        _parse_datetime(str(row["expires_at"])),
    )
    await store.create_invite_intent(
        raw_secret=raw_secret,
        invite_type=invite_type,
        invite_id=invite_id,
        token_version=row_version,
        expires_at=intent_expiry.isoformat(),
    )
    context = await load_invite_context(store, raw_secret=raw_secret)
    return raw_secret, invite_preview(context)


async def load_invite_context(
    store: UserStore,
    *,
    raw_secret: str | None = None,
    intent_hash: str | None = None,
) -> InviteContext:
    if raw_secret:
        intent = await store.get_invite_intent(raw_secret)
    elif intent_hash:
        intent = await store.get_invite_intent_by_hash(intent_hash)
    else:
        intent = None
    if intent is None:
        raise InviteFlowError("invite_intent_missing", status_code=401)

    invite_type = str(intent["invite_type"])
    invite_id = str(intent["invite_id"])
    row = await store.get_team_invite_by_id(invite_id) if invite_type == "team" else await store.get_workspace_invite_by_id(invite_id)
    if row is None:
        raise InviteFlowError("invite_invalid", status_code=401)
    row_status = _invite_row_status(row)
    if row_status != "valid":
        status_code = 410 if row_status in {"expired", "revoked"} else 409
        raise InviteFlowError(f"invite_{row_status}", status_code=status_code)
    token_version = int(intent["token_version"])
    if int(row.get("token_version") or 1) != token_version:
        raise InviteFlowError("invite_invalid", status_code=401)

    workspace_id: str | None = None
    workspace_name: str | None = None
    team_id = str(row.get("team_id") or "")
    if invite_type == "workspace":
        workspace_id = str(row.get("workspace_id") or "")
        workspace = await store.get_workspace(workspace_id)
        if workspace is None:
            raise InviteFlowError("invite_invalid", status_code=401)
        workspace_name = str(workspace.get("name") or "Bowhead space")
        team_id = team_id or str(workspace.get("team_id") or "")
    team = await store.get_team(team_id)
    if team is None:
        raise InviteFlowError("invite_invalid", status_code=401)
    inviter = await inviter_display_name(store, row, team_id)
    return InviteContext(
        intent_hash=str(intent["secret_hash"]),
        invite_type=invite_type,
        invite_id=invite_id,
        token_version=token_version,
        row=row,
        team_id=team_id,
        team_name=team.name,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        inviter_display_name=inviter,
    )


async def next_invite_action(
    store: UserStore,
    context: InviteContext,
    policy: AccountPolicy,
    viewer: User | None,
    *,
    multi_team_enabled: bool = False,
) -> dict[str, Any]:
    existing = await store.get_user_by_email(context.invited_email)
    if viewer is not None and viewer.email.lower().strip() != context.invited_email:
        next_action = "switch_account"
    elif viewer is not None and existing is not None and viewer.id == existing.id:
        target_membership = await store.user_has_active_team_membership(
            existing.id,
            context.team_id,
        )
        if not target_membership and existing.team_id != context.team_id and not multi_team_enabled:
            next_action = "policy_blocked"
        else:
            next_action = "accept"
    elif existing is not None:
        if existing.status != "active":
            next_action = "identity_blocked"
        elif existing.password_hash:
            next_action = "login_password"
        elif existing.oauth_providers:
            next_action = "login_federated"
        else:
            next_action = "identity_blocked"
    elif policy.account_creation is AccountCreationPolicy.ADMIN_ONLY:
        next_action = "policy_blocked"
    elif policy.email_verification is EmailVerificationPolicy.TRUSTED_IDP_REQUIRED:
        next_action = "activate_federated"
    else:
        next_action = "activate_password"
    return {**invite_preview(context), "next_action": next_action}


def invite_preview(context: InviteContext) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "ready",
        "invite_type": context.invite_type,
        "invited_email_mask": mask_email(context.invited_email),
        "expires_at": context.row.get("expires_at"),
        "team": {"name": context.team_name},
    }
    if context.invite_type == "workspace":
        out["workspace"] = {
            "name": context.workspace_name or "Bowhead space",
            "role": context.row.get("role") or "member",
        }
    if context.inviter_display_name:
        out["inviter_display_name"] = context.inviter_display_name
    return out


_INVITER_DISPLAY_NAME_MAX = 80
# Characters treated as invisible: format controls (zero-width space/joiner, direction marks, etc., Cf) and all whitespace.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Zs", "Zl", "Zp", "Cc"})
# Characters that extend the previous grapheme cluster: combining marks, zero-width joiner, variation selectors, skin-tone modifiers.
_EXTENDING_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


def _extends_cluster(char: str) -> bool:
    code = ord(char)
    return (
        unicodedata.category(char) in _EXTENDING_CATEGORIES
        or code == 0x200D
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or 0x1F3FB <= code <= 0x1F3FF
    )


def _truncate_clusters(text: str, limit: int) -> str:
    """Truncate by grapheme cluster (approximating UAX #29 extended clusters) without splitting combining marks, ZWJ sequences or variation selectors."""

    clusters = 0
    end = 0
    joined = False
    for index, char in enumerate(text):
        if index and (_extends_cluster(char) or joined):
            joined = ord(char) == 0x200D
            end = index + 1
            continue
        if clusters == limit:
            break
        clusters += 1
        joined = ord(char) == 0x200D
        end = index + 1
    return text[:end]


async def inviter_display_name(
    store: UserStore,
    row: dict[str, Any],
    team_id: str,
) -> str | None:
    """Return the inviter's display name for the public invitation preview.

    Only the display name is exposed, never the inviter's email or id. Returns None (and the
    page simply omits the "{name} invited you" line) unless the inviter account is active and
    still an active member of the invitation's team, and the name is visible text that does not
    look like an email address (full-width ＠ included). The name is NFC-normalised, whitespace
    collapsed and cut to 80 grapheme clusters without splitting combining sequences.
    """

    inviter_id = str(row.get("invited_by") or "").strip()
    if not inviter_id or not team_id:
        return None
    inviter = await store.get_user(inviter_id)
    if inviter is None or inviter.status != "active":
        return None
    if not await store.user_has_active_team_membership(inviter.id, team_id):
        return None
    name = unicodedata.normalize("NFC", str(inviter.display_name or ""))
    name = " ".join(name.split())
    if not any(unicodedata.category(char) not in _INVISIBLE_CATEGORIES for char in name):
        return None
    if "@" in unicodedata.normalize("NFKC", name):
        return None
    return _truncate_clusters(name, _INVITER_DISPLAY_NAME_MAX)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
