"""Team-scoped public entry registry and lifecycle.

The registry is the source of truth for browser-visible origins and URI
prefixes. Request matching lives in the gateway; this module owns
normalization, persistence, state transitions, and entry-local audit rows.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx

PublicEntryStatus = Literal["active", "disabled", "pending_verification"]

_CHALLENGE_DOMAIN = b"bowhead/public-entry-challenge/v1\x00"
_ENCODED_AMBIGUOUS_PATH = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_SAFE_PATH = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})*")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_entries (
    id                          TEXT PRIMARY KEY,
    team_id                     TEXT NOT NULL,
    base_url                    TEXT NOT NULL,
    scheme                      TEXT NOT NULL,
    host                        TEXT NOT NULL,
    port                        INTEGER,
    base_path                   TEXT NOT NULL DEFAULT '',
    is_default                  INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
    status                      TEXT NOT NULL CHECK(status IN ('active', 'disabled', 'pending_verification')),
    verification_method         TEXT NOT NULL,
    verification_token_hash     TEXT,
    verification_expires_at     TEXT,
    verified_at                 TEXT,
    created_by                  TEXT NOT NULL,
    updated_by                  TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    UNIQUE(team_id, base_url)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_public_entries_one_default
    ON public_entries(team_id)
    WHERE status = 'active' AND is_default = 1;
CREATE INDEX IF NOT EXISTS idx_public_entries_lookup
    ON public_entries(base_url, status);
CREATE INDEX IF NOT EXISTS idx_public_entries_team
    ON public_entries(team_id, status, is_default);

CREATE TABLE IF NOT EXISTS public_entry_audit (
    id                          TEXT PRIMARY KEY,
    team_id                     TEXT NOT NULL,
    entry_id                    TEXT,
    action                      TEXT NOT NULL,
    actor_id                    TEXT NOT NULL,
    payload_json                TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_public_entry_audit_team
    ON public_entry_audit(team_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public_entry_challenge_attempts (
    entry_id                    TEXT NOT NULL,
    peer_key                    TEXT NOT NULL,
    window_started_at           TEXT NOT NULL,
    attempt_count               INTEGER NOT NULL,
    PRIMARY KEY(entry_id, peer_key)
);
"""


class PublicEntryError(ValueError):
    """A public entry contract or lifecycle transition was rejected."""


@dataclass(frozen=True, slots=True)
class NormalizedPublicUrl:
    base_url: str
    scheme: str
    host: str
    port: int | None
    base_path: str

    @property
    def origin(self) -> str:
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None:
            return f"{self.scheme}://{rendered_host}"
        return f"{self.scheme}://{rendered_host}:{self.port}"

    @property
    def app_root(self) -> str:
        return f"{self.base_url}/"


@dataclass(frozen=True, slots=True)
class PublicEntry:
    id: str
    team_id: str
    base_url: str
    scheme: str
    host: str
    port: int | None
    base_path: str
    is_default: bool
    status: PublicEntryStatus
    verification_method: str
    verification_expires_at: str | None
    verified_at: str | None
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str

    @property
    def app_root(self) -> str:
        return f"{self.base_url}/"


def normalize_public_base_url(value: str) -> NormalizedPublicUrl:
    """Canonicalize a registered public base URL or reject ambiguity."""
    raw = value.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise PublicEntryError("public base URL contains empty/control input")
    try:
        raw.encode("ascii")
    except UnicodeEncodeError as exc:
        raise PublicEntryError("public base URL must use ASCII host/punycode") from exc
    if "\\" in raw:
        raise PublicEntryError("public base URL must not contain backslashes")

    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise PublicEntryError("public base URL scheme must be http or https")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise PublicEntryError("public base URL requires a host and forbids userinfo")
    if parsed.query or parsed.fragment:
        raise PublicEntryError("public base URL must not contain query or fragment")

    host = (parsed.hostname or "").lower()
    if not host or host.endswith(".") or "*" in host or "%" in host:
        raise PublicEntryError("public base URL host is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicEntryError("public base URL port is invalid") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if any(not (part and re.fullmatch(r"[a-z0-9-]+", part) and not part.startswith("-") and not part.endswith("-")) for part in host.split(".")):
            raise PublicEntryError("public base URL hostname is invalid")

    scheme = parsed.scheme.lower()
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None

    path = parsed.path or ""
    if path and not path.startswith("/"):
        raise PublicEntryError("public base path must be absolute")
    if not _SAFE_PATH.fullmatch(path):
        raise PublicEntryError("public base path contains unsafe or malformed characters")
    if "//" in path or _ENCODED_AMBIGUOUS_PATH.search(path):
        raise PublicEntryError("public base path contains ambiguous encoding or repeated slash")
    for encoded in _PERCENT_ESCAPE.findall(path):
        if int(encoded[1:], 16) in _UNRESERVED_BYTES:
            raise PublicEntryError("public base path must not percent-encode unreserved characters")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise PublicEntryError("public base path must not contain dot segments")
    base_path = _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), path).rstrip("/")
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = f":{port}" if port is not None else ""
    base_url = f"{scheme}://{rendered_host}{rendered_port}{base_path}"
    return NormalizedPublicUrl(
        base_url=base_url,
        scheme=scheme,
        host=host,
        port=port,
        base_path=base_path,
    )


class PublicEntryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(SCHEMA)
            await db.commit()

    async def list_for_team(self, team_id: str) -> list[PublicEntry]:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE team_id=? ORDER BY is_default DESC, status, created_at",
                (team_id,),
            ) as cursor:
                return [_entry_from_row(row) for row in await cursor.fetchall()]

    async def get(self, entry_id: str) -> PublicEntry | None:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE id=?",
                (entry_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return _entry_from_row(row) if row else None

    async def get_for_team(self, team_id: str, entry_id: str) -> PublicEntry | None:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE team_id=? AND id=?",
                (team_id, entry_id),
            ) as cursor:
                row = await cursor.fetchone()
                return _entry_from_row(row) if row else None

    async def default_for_team(self, team_id: str) -> PublicEntry | None:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE team_id=? AND status='active' AND is_default=1",
                (team_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return _entry_from_row(row) if row else None

    async def find_active_by_base_url(self, base_url: str) -> list[PublicEntry]:
        normalized = normalize_public_base_url(base_url)
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE base_url=? AND status='active' ORDER BY team_id, id",
                (normalized.base_url,),
            ) as cursor:
                return [_entry_from_row(row) for row in await cursor.fetchall()]

    async def list_active(self) -> list[PublicEntry]:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries WHERE status='active' ORDER BY team_id, is_default DESC, id",
            ) as cursor:
                return [_entry_from_row(row) for row in await cursor.fetchall()]

    async def list_all(self) -> list[PublicEntry]:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM public_entries ORDER BY team_id, status, id",
            ) as cursor:
                return [_entry_from_row(row) for row in await cursor.fetchall()]

    async def create_pending(
        self,
        *,
        team_id: str,
        base_url: str,
        actor_id: str,
        ttl_minutes: int = 15,
    ) -> tuple[PublicEntry, str]:
        normalized = normalize_public_base_url(base_url)
        now = _now()
        token = secrets.token_urlsafe(32)
        entry_id = _id("pe")
        expires_at = (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO public_entries("
                    "id,team_id,base_url,scheme,host,port,base_path,is_default,status,"
                    "verification_method,verification_token_hash,verification_expires_at,"
                    "created_by,updated_by,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        team_id,
                        normalized.base_url,
                        normalized.scheme,
                        normalized.host,
                        normalized.port,
                        normalized.base_path,
                        0,
                        "pending_verification",
                        "inbound_challenge",
                        _challenge_hash(token),
                        expires_at,
                        actor_id,
                        actor_id,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise PublicEntryError("public entry already exists for this team") from exc
            await self._write_audit(
                db,
                team_id=team_id,
                entry_id=entry_id,
                action="public_entry.created",
                actor_id=actor_id,
                payload={"base_url": normalized.base_url, "status": "pending_verification"},
            )
            await db.commit()
            entry = await self._get_in_transaction(db, entry_id)
        return entry, token

    async def reissue_challenge(
        self,
        *,
        team_id: str,
        entry_id: str,
        actor_id: str,
        ttl_minutes: int = 15,
    ) -> tuple[PublicEntry, str]:
        token = secrets.token_urlsafe(32)
        now = _now()
        expires_at = (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            entry = await self._get_in_transaction(db, entry_id)
            if entry.team_id != team_id:
                raise PublicEntryError("public entry not found")
            if entry.status not in {"disabled", "pending_verification"}:
                raise PublicEntryError("only disabled or pending entries can be reverified")
            await db.execute(
                "UPDATE public_entries SET status='pending_verification', is_default=0,"
                "verification_method='inbound_challenge', verification_token_hash=?,"
                "verification_expires_at=?, verified_at=NULL, updated_by=?, updated_at=? "
                "WHERE id=?",
                (_challenge_hash(token), expires_at, actor_id, now, entry_id),
            )
            await self._write_audit(
                db,
                team_id=team_id,
                entry_id=entry_id,
                action="public_entry.challenge_reissued",
                actor_id=actor_id,
                payload={"expires_at": expires_at},
            )
            await db.commit()
            entry = await self._get_in_transaction(db, entry_id)
        return entry, token

    async def verify_challenge(
        self,
        *,
        entry_id: str,
        token: str,
    ) -> PublicEntry:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            entry = await self._get_in_transaction(db, entry_id)
            if entry.status != "pending_verification":
                raise PublicEntryError("public entry is not pending verification")
            async with db.execute(
                "SELECT verification_token_hash FROM public_entries WHERE id=?",
                (entry_id,),
            ) as cursor:
                token_row = await cursor.fetchone()
            expected_hash = str(token_row[0] or "") if token_row else ""
            if not expected_hash or not hmac.compare_digest(expected_hash, _challenge_hash(token)):
                raise PublicEntryError("invalid challenge token")
            if not entry.verification_expires_at or entry.verification_expires_at < now:
                raise PublicEntryError("challenge expired")
            await self._assert_no_active_overlap(db, entry)
            async with db.execute(
                "SELECT 1 FROM public_entries WHERE team_id=? AND status='active' LIMIT 1",
                (entry.team_id,),
            ) as cursor:
                has_active = await cursor.fetchone() is not None
            await db.execute(
                "UPDATE public_entries SET status='active', is_default=?,"
                "verification_token_hash=NULL, verification_expires_at=NULL,"
                "verified_at=?, updated_at=? WHERE id=?",
                (0 if has_active else 1, now, now, entry_id),
            )
            await self._write_audit(
                db,
                team_id=entry.team_id,
                entry_id=entry_id,
                action="public_entry.verified",
                actor_id=entry.updated_by,
                payload={"base_url": entry.base_url, "is_default": not has_active},
            )
            await db.commit()
            return await self._get_in_transaction(db, entry_id)

    async def set_default(self, *, team_id: str, entry_id: str, actor_id: str) -> PublicEntry:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            entry = await self._get_in_transaction(db, entry_id)
            if entry.team_id != team_id or entry.status != "active":
                raise PublicEntryError("new default must be an active team entry")
            await db.execute(
                "UPDATE public_entries SET is_default=0, updated_by=?, updated_at=? WHERE team_id=? AND is_default=1",
                (actor_id, now, team_id),
            )
            await db.execute(
                "UPDATE public_entries SET is_default=1, updated_by=?, updated_at=? WHERE id=?",
                (actor_id, now, entry_id),
            )
            await self._write_audit(
                db,
                team_id=team_id,
                entry_id=entry_id,
                action="public_entry.default_changed",
                actor_id=actor_id,
                payload={"base_url": entry.base_url},
            )
            await db.commit()
            return await self._get_in_transaction(db, entry_id)

    async def disable(
        self,
        *,
        team_id: str,
        entry_id: str,
        actor_id: str,
        new_default_entry_id: str | None = None,
        disable_last: bool = False,
    ) -> PublicEntry:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            entry = await self._get_in_transaction(db, entry_id)
            if entry.team_id != team_id or entry.status != "active":
                raise PublicEntryError("only an active team entry can be disabled")
            if entry.is_default:
                async with db.execute(
                    "SELECT COUNT(*) FROM public_entries WHERE team_id=? AND status='active' AND id<>?",
                    (team_id, entry_id),
                ) as cursor:
                    other_active = int((await cursor.fetchone())[0])
                if other_active:
                    if not new_default_entry_id:
                        raise PublicEntryError("a new default entry is required")
                    replacement = await self._get_in_transaction(db, new_default_entry_id)
                    if replacement.id == entry.id or replacement.team_id != team_id or replacement.status != "active":
                        raise PublicEntryError("new default must be an active team entry")
                    await db.execute(
                        "UPDATE public_entries SET is_default=0 WHERE id=?",
                        (entry_id,),
                    )
                    await db.execute(
                        "UPDATE public_entries SET is_default=1, updated_by=?, updated_at=? WHERE id=?",
                        (actor_id, now, replacement.id),
                    )
                elif not disable_last:
                    raise PublicEntryError("disable_last=true is required for the final active entry")
            await db.execute(
                "UPDATE public_entries SET status='disabled', is_default=0,updated_by=?, updated_at=? WHERE id=?",
                (actor_id, now, entry_id),
            )
            await self._write_audit(
                db,
                team_id=team_id,
                entry_id=entry_id,
                action="public_entry.disabled",
                actor_id=actor_id,
                payload={
                    "base_url": entry.base_url,
                    "new_default_entry_id": new_default_entry_id,
                    "disable_last": disable_last,
                },
            )
            await db.commit()
            return await self._get_in_transaction(db, entry_id)

    async def delete(self, *, team_id: str, entry_id: str, actor_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            entry = await self._get_in_transaction(db, entry_id)
            if entry.team_id != team_id:
                raise PublicEntryError("public entry not found")
            if entry.status == "active":
                raise PublicEntryError("active public entry must be disabled before deletion")
            await self._write_audit(
                db,
                team_id=team_id,
                entry_id=entry_id,
                action="public_entry.deleted",
                actor_id=actor_id,
                payload={"base_url": entry.base_url, "status": entry.status},
            )
            await db.execute(
                "DELETE FROM public_entry_challenge_attempts WHERE entry_id=?",
                (entry_id,),
            )
            await db.execute("DELETE FROM public_entries WHERE id=?", (entry_id,))
            await db.commit()

    async def migrate_legacy(self, base_url: str) -> int:
        """Idempotently seed one active default entry for every existing team."""
        normalized = normalize_public_base_url(base_url)
        now = _now()
        inserted = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT id FROM teams ORDER BY id") as cursor:
                    teams = await cursor.fetchall()
            except aiosqlite.OperationalError:
                await db.rollback()
                return 0
            for team in teams:
                team_id = str(team["id"])
                async with db.execute(
                    "SELECT 1 FROM public_entries WHERE team_id=? LIMIT 1",
                    (team_id,),
                ) as cursor:
                    if await cursor.fetchone():
                        continue
                entry_id = _id("pe")
                await db.execute(
                    "INSERT INTO public_entries("
                    "id,team_id,base_url,scheme,host,port,base_path,is_default,status,"
                    "verification_method,verified_at,created_by,updated_by,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        team_id,
                        normalized.base_url,
                        normalized.scheme,
                        normalized.host,
                        normalized.port,
                        normalized.base_path,
                        1,
                        "active",
                        "legacy_migration",
                        now,
                        "system",
                        "system",
                        now,
                        now,
                    ),
                )
                await self._write_audit(
                    db,
                    team_id=team_id,
                    entry_id=entry_id,
                    action="public_entry.legacy_migrated",
                    actor_id="system",
                    payload={"base_url": normalized.base_url},
                )
                inserted += 1
            await db.commit()
        return inserted

    async def create_bootstrap_active(
        self,
        *,
        team_id: str,
        base_url: str,
        actor_id: str,
        verification_method: str = "trusted_proxy_bootstrap",
        connection: aiosqlite.Connection | None = None,
    ) -> PublicEntry:
        """Create the first active/default entry for a newly created team."""
        if verification_method not in {
            "trusted_proxy_bootstrap",
            "license_browser_bootstrap",
            "legacy_migration",
        }:
            raise PublicEntryError("invalid bootstrap verification method")
        normalized = normalize_public_base_url(base_url)
        now = _now()
        if connection is not None:
            connection.row_factory = aiosqlite.Row
            return await self._create_bootstrap_active_in_transaction(
                connection,
                team_id=team_id,
                normalized=normalized,
                actor_id=actor_id,
                verification_method=verification_method,
                now=now,
            )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                entry = await self._create_bootstrap_active_in_transaction(
                    db,
                    team_id=team_id,
                    normalized=normalized,
                    actor_id=actor_id,
                    verification_method=verification_method,
                    now=now,
                )
                await db.commit()
                return entry
            except Exception:
                await db.rollback()
                raise

    async def adopt_browser_entry(
        self,
        *,
        team_id: str,
        base_url: str,
        actor_id: str,
    ) -> PublicEntry:
        """Register the URL an authenticated owner is actually browsing from.

        ``create_bootstrap_active`` is idempotent *per team*: it returns the
        existing row untouched once the team owns any entry at all. That makes it
        useless for the failure this method exists for — a private deployment
        whose only registered entry no longer matches the address operators
        actually serve (renamed host, added URI prefix, http→https). Every
        browser request then fails ``421`` and the entry admin page is itself
        unreachable, so the instance cannot be repaired from the product.

        This adds the observed URL as an *additional* active entry rather than
        rewriting the stale one, because the schema models several public entries
        per team on purpose. It only becomes the default when the team has no
        active default left to speak of; otherwise outbound absolute URLs keep
        whatever the operator already chose.

        Callers own the trust decision (local deployment + authenticated team
        owner). This method never infers authority from the URL itself.
        """
        normalized = normalize_public_base_url(base_url)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # `UNIQUE(team_id, base_url)` ignores status, so this lookup must
                # too: a disabled or never-verified row for the same URL is the
                # likely leftover of an earlier attempt to register it, and
                # inserting beside it would raise IntegrityError instead of
                # recovering.
                async with db.execute(
                    "SELECT * FROM public_entries WHERE team_id=? AND base_url=?",
                    (team_id, normalized.base_url),
                ) as cursor:
                    already = await cursor.fetchone()

                async with db.execute(
                    "SELECT 1 FROM public_entries WHERE team_id=? AND status='active' AND is_default=1 LIMIT 1",
                    (team_id,),
                ) as cursor:
                    has_default = await cursor.fetchone() is not None

                if already is not None:
                    existing = _entry_from_row(already)
                    if existing.status == "active":
                        await db.commit()
                        return existing
                    # Reactivating carries the same authority as creating: the
                    # owner is browsing from this address right now.
                    await self._assert_no_active_overlap(db, existing)
                    await db.execute(
                        "UPDATE public_entries SET status='active', is_default=?, verified_at=?, updated_by=?, updated_at=? WHERE id=?",
                        (
                            0 if has_default else 1,
                            now,
                            actor_id,
                            now,
                            existing.id,
                        ),
                    )
                    await self._write_audit(
                        db,
                        team_id=team_id,
                        entry_id=existing.id,
                        action="public_entry.browser_adopted",
                        actor_id=actor_id,
                        payload={
                            "base_url": normalized.base_url,
                            "became_default": not has_default,
                            "reactivated_from": existing.status,
                        },
                    )
                    entry = await self._get_in_transaction(db, existing.id)
                    await db.commit()
                    return entry

                entry_id = _id("pe")
                candidate = PublicEntry(
                    id=entry_id,
                    team_id=team_id,
                    base_url=normalized.base_url,
                    scheme=normalized.scheme,
                    host=normalized.host,
                    port=normalized.port,
                    base_path=normalized.base_path,
                    is_default=not has_default,
                    status="active",
                    verification_method="license_browser_bootstrap",
                    verification_expires_at=None,
                    verified_at=now,
                    created_by=actor_id,
                    updated_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                # An overlapping active path would make entry selection ambiguous
                # for every later request; refuse instead of trading one broken
                # state for a subtler one.
                await self._assert_no_active_overlap(db, candidate)
                await db.execute(
                    "INSERT INTO public_entries("
                    "id,team_id,base_url,scheme,host,port,base_path,is_default,status,"
                    "verification_method,verified_at,created_by,updated_by,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate.id,
                        candidate.team_id,
                        candidate.base_url,
                        candidate.scheme,
                        candidate.host,
                        candidate.port,
                        candidate.base_path,
                        1 if candidate.is_default else 0,
                        candidate.status,
                        candidate.verification_method,
                        candidate.verified_at,
                        candidate.created_by,
                        candidate.updated_by,
                        candidate.created_at,
                        candidate.updated_at,
                    ),
                )
                await self._write_audit(
                    db,
                    team_id=team_id,
                    entry_id=entry_id,
                    action="public_entry.browser_adopted",
                    actor_id=actor_id,
                    payload={
                        "base_url": normalized.base_url,
                        "became_default": candidate.is_default,
                    },
                )
                entry = await self._get_in_transaction(db, entry_id)
                await db.commit()
                return entry
            except Exception:
                await db.rollback()
                raise

    async def consume_challenge_attempt(
        self,
        *,
        entry_id: str,
        peer_key: str,
        limit: int = 10,
        window_minutes: int = 5,
    ) -> bool:
        """Atomically reserve one verification attempt without storing secrets."""
        now = datetime.now(UTC)
        cutoff = (now - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
        now_text = now.isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT window_started_at,attempt_count FROM public_entry_challenge_attempts WHERE entry_id=? AND peer_key=?",
                (entry_id, peer_key),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or str(row["window_started_at"]) < cutoff:
                await db.execute(
                    "INSERT INTO public_entry_challenge_attempts("
                    "entry_id,peer_key,window_started_at,attempt_count"
                    ") VALUES (?,?,?,1) ON CONFLICT(entry_id,peer_key) DO UPDATE SET "
                    "window_started_at=excluded.window_started_at,attempt_count=1",
                    (entry_id, peer_key, now_text),
                )
                await db.commit()
                return True
            if int(row["attempt_count"]) >= limit:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE public_entry_challenge_attempts SET attempt_count=attempt_count+1 WHERE entry_id=? AND peer_key=?",
                (entry_id, peer_key),
            )
            await db.commit()
            return True

    async def clear_challenge_attempts(self, entry_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM public_entry_challenge_attempts WHERE entry_id=?",
                (entry_id,),
            )
            await db.commit()

    async def _create_bootstrap_active_in_transaction(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        normalized: NormalizedPublicUrl,
        actor_id: str,
        verification_method: str,
        now: str,
    ) -> PublicEntry:
        async with db.execute(
            "SELECT * FROM public_entries WHERE team_id=? LIMIT 1",
            (team_id,),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            return _entry_from_row(existing)
        entry_id = _id("pe")
        await db.execute(
            "INSERT INTO public_entries("
            "id,team_id,base_url,scheme,host,port,base_path,is_default,status,"
            "verification_method,verified_at,created_by,updated_by,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id,
                team_id,
                normalized.base_url,
                normalized.scheme,
                normalized.host,
                normalized.port,
                normalized.base_path,
                1,
                "active",
                verification_method,
                now,
                actor_id,
                actor_id,
                now,
                now,
            ),
        )
        await self._write_audit(
            db,
            team_id=team_id,
            entry_id=entry_id,
            action="public_entry.bootstrap_created",
            actor_id=actor_id,
            payload={
                "base_url": normalized.base_url,
                "verification_method": verification_method,
            },
        )
        return await self._get_in_transaction(db, entry_id)

    async def _get_in_transaction(self, db: aiosqlite.Connection, entry_id: str) -> PublicEntry:
        async with db.execute(
            "SELECT * FROM public_entries WHERE id=?",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise PublicEntryError("public entry not found")
        return _entry_from_row(row)

    async def _assert_no_active_overlap(
        self,
        db: aiosqlite.Connection,
        candidate: PublicEntry,
    ) -> None:
        async with db.execute(
            "SELECT * FROM public_entries WHERE team_id=? AND status='active' AND scheme=? AND host=? AND COALESCE(port,-1)=COALESCE(?,-1) AND id<>?",
            (
                candidate.team_id,
                candidate.scheme,
                candidate.host,
                candidate.port,
                candidate.id,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            existing = _entry_from_row(row)
            if _paths_overlap(existing.base_path, candidate.base_path):
                raise PublicEntryError("active public entry has an overlapping base path")

    async def _write_audit(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        entry_id: str | None,
        action: str,
        actor_id: str,
        payload: dict[str, object],
    ) -> None:
        await db.execute(
            "INSERT INTO public_entry_audit(id,team_id,entry_id,action,actor_id,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                _id("pea"),
                team_id,
                entry_id,
                action,
                actor_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _challenge_hash(token: str) -> str:
    return hashlib.sha256(_CHALLENGE_DOMAIN + token.encode("utf-8")).hexdigest()


def _paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return True
    return left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _entry_from_row(row: aiosqlite.Row) -> PublicEntry:
    return PublicEntry(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        base_url=str(row["base_url"]),
        scheme=str(row["scheme"]),
        host=str(row["host"]),
        port=int(row["port"]) if row["port"] is not None else None,
        base_path=str(row["base_path"]),
        is_default=bool(row["is_default"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        verification_method=str(row["verification_method"]),
        verification_expires_at=row["verification_expires_at"],
        verified_at=row["verified_at"],
        created_by=str(row["created_by"]),
        updated_by=str(row["updated_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
