"""Installation provenance and atomic Community first-owner setup."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import aiosqlite

from bowhead_core.user_store import User, hash_password

Edition = Literal["community", "business"]


class InstallationError(ValueError):
    """An installation cannot enter the requested edition or setup state."""


@dataclass(frozen=True)
class InstallationIdentity:
    installation_id: str
    origin_edition: Edition
    current_edition: Edition
    setup_complete: bool


def read_installation(db_path: str) -> InstallationIdentity | None:
    """Read existing provenance without creating a database or schema."""
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='installation_identity'").fetchone() is None:
            return None
        row = db.execute(
            "SELECT installation_id, origin_edition, current_edition, setup_complete FROM installation_identity WHERE singleton=1"
        ).fetchone()
    return InstallationIdentity(*row[:3], bool(row[3])) if row else None


def preflight_installation(db_path: str, edition: Edition) -> bool:
    """Refuse unknown/Business data before Community writes; return fresh admission."""
    identity = read_installation(db_path)
    if identity is not None:
        if edition == "community" and identity.current_edition != "community":
            raise InstallationError("Business-to-Community conversion is not supported; use the existing Business build and data.")
        return False
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        if edition == "community" and path.parent.exists():
            siblings = [candidate for candidate in path.parent.glob("*.db") if candidate != path and candidate.stat().st_size > 0]
            if siblings:
                raise InstallationError("Existing database files have unknown installation provenance; Community initialization is refused.")
        return True
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        populated = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone() is not None
    if populated and edition == "community":
        raise InstallationError("Existing database has unknown installation provenance; unattended Community conversion is refused.")
    if not populated and edition == "community" and any(candidate != path and candidate.stat().st_size > 0 for candidate in path.parent.glob("*.db")):
        raise InstallationError("Existing database files have unknown installation provenance; Community initialization is refused.")
    return not populated


async def initialize_installation(db_path: str, edition: Edition, *, admitted_fresh: bool = False) -> InstallationIdentity:
    """Persist provenance after migrations, carrying forward the pre-migration decision."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT installation_id, origin_edition, current_edition, setup_complete FROM installation_identity WHERE singleton=1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                if edition == "community" and not admitted_fresh:
                    raise InstallationError("Community provenance requires fresh pre-migration admission.")
                # Setup data can never be adopted into a newly claimed Community identity.
                if edition == "community":
                    for table in ("users", "teams", "team_license"):
                        async with db.execute(f"SELECT 1 FROM {table} LIMIT 1") as cursor:
                            if await cursor.fetchone() is not None:
                                raise InstallationError("Existing installation data cannot be claimed by Community setup.")
                installation_id = str(uuid4())
                await db.execute(
                    "INSERT INTO installation_identity(singleton,installation_id,origin_edition,current_edition,setup_complete,created_at) VALUES(1,?,?,?,?,?)",
                    (installation_id, edition, edition, int(edition == "business"), datetime.now(UTC).isoformat()),
                )
                identity = InstallationIdentity(installation_id, edition, edition, edition == "business")
            else:
                identity = InstallationIdentity(*row[:3], bool(row[3]))
                if edition == "community" and identity.current_edition != "community":
                    raise InstallationError("Business-to-Community conversion is not supported.")
                if edition == "business" and identity.current_edition == "community":
                    if not identity.setup_complete:
                        raise InstallationError("Complete Community first-owner setup before upgrading to Business.")
                    await db.execute("UPDATE installation_identity SET current_edition='business' WHERE singleton=1")
                    identity = InstallationIdentity(identity.installation_id, identity.origin_edition, "business", True)
            await db.commit()
            return identity
        except BaseException:
            await db.rollback()
            raise


def community_setup_token(identity: InstallationIdentity) -> str:
    """Derive an operator-only setup proof from the persistent instance root."""
    if identity.current_edition != "community" or identity.setup_complete:
        raise InstallationError("Community first-owner setup is not pending.")
    try:
        key = base64.b64decode(os.environ.get("BOWHEAD_FERNET_KEY", "").encode("ascii"), altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError("invalid key size")
    except (ValueError, UnicodeError) as exc:
        raise InstallationError("A valid persistent BOWHEAD_FERNET_KEY is required for Community setup.") from exc
    message = b"bowhead:community-first-owner:v1\x00" + identity.installation_id.encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


async def setup_community_owner(
    db_path: str, *, setup_token: str, team_name: str, email: str, password: str, display_name: str | None = None
) -> User:
    """Claim a pending installation once, committing owner and provenance atomically."""
    now = datetime.now(UTC).isoformat()
    team_id = "team_" + uuid4().hex[:12]
    user = User(
        id="usr_" + uuid4().hex[:12],
        team_id=team_id,
        email=email.lower().strip(),
        email_verified=True,
        display_name=display_name,
        password_hash=hash_password(password),
        tier="pro",
        credit_balance=5000,
        credit_quota=5000,
        credit_period_start=now,
        created_at=now,
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT installation_id, origin_edition, current_edition, setup_complete FROM installation_identity WHERE singleton=1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise InstallationError("Installation provenance is unavailable.")
            identity = InstallationIdentity(*row[:3], bool(row[3]))
            expected = community_setup_token(identity)
            if not setup_token.isascii() or not hmac.compare_digest(setup_token, expected):
                raise PermissionError("Invalid Community setup token.")
            for table in ("users", "teams", "team_license"):
                async with db.execute(f"SELECT 1 FROM {table} LIMIT 1") as cursor:
                    if await cursor.fetchone() is not None:
                        raise InstallationError("Installation already contains account or license data.")
            await db.execute(
                "INSERT INTO teams(id,name,max_seats,default_user_tier,team_credit_balance,team_credit_quota,team_credit_period_start,tier_credits_json,status,created_at,created_by_user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (team_id, team_name, 1, "pro", 30000, 30000, now, json.dumps({"pro": 5000, "ultra": 30000}), "active", now, user.id),
            )
            await db.execute(
                "INSERT INTO users(id,team_id,email,email_verified,display_name,password_hash,oauth_providers_json,tier,credit_balance,credit_quota,credit_period_start,locale,timezone,preferences_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user.id,
                    team_id,
                    user.email,
                    1,
                    display_name,
                    user.password_hash,
                    "[]",
                    user.tier,
                    5000,
                    5000,
                    now,
                    user.locale,
                    user.timezone,
                    json.dumps(user.preferences),
                    "active",
                    now,
                ),
            )
            await db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES(?,?,?,?,?,?)",
                ("om_" + uuid4().hex[:12], user.id, team_id, "team_admin", "active", now),
            )
            await db.execute("UPDATE installation_identity SET setup_complete=1 WHERE singleton=1")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return user
