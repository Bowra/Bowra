"""Office upload receipt and fenced confirmation, independent of model output.

These are ordinary uploads, not Document Jobs or template-role declarations.
Only the server-only candidate can become the ready artifact. Receipts survive
artifact deletion so cleanup never loses the two exact object keys.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
import anyio
import structlog

from bowhead_core.artifact_store import ArtifactReadLimitError, _open_db
from bowhead_core.documents.package_io import MAX_OFFICE_BYTES, OfficePackageError, inspect_office_package

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.artifact_store import ArtifactBackend, ArtifactStore

log = structlog.get_logger(__name__)
CHECK_PROFILE = "office-outer-opc-v1"
LEASE_SECONDS = 120
RECOVERY_SECONDS = 24 * 3600
_CONFIRM_LIMITER = anyio.CapacityLimiter(2)


class UploadConfirmationError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


async def has_confirmation_schema(db: aiosqlite.Connection) -> bool:
    cursor = await db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='artifact_upload_confirmations'")
    return await cursor.fetchone() is not None


async def get_confirmation(db_path: str, artifact_id: str) -> aiosqlite.Row | None:
    async with _open_db(db_path) as db:
        if not await has_confirmation_schema(db):
            return None  # Legacy, feature-off stores have no trusted receipt.
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM artifact_upload_confirmations WHERE artifact_id=?", (artifact_id,))
        return await cursor.fetchone()


async def confirmed_content_hash(store: ArtifactStore, artifact, *, user_id: str, workspace_id: str) -> str:
    """Read an immutable Office upload identity without opening its contents."""
    row = await get_confirmation(store.db_path, artifact.id)
    if (
        row is None
        or row["phase"] != "ready"
        or row["check_profile"] != CHECK_PROFILE
        or (row["user_id"], row["workspace_id"]) != (user_id, workspace_id)
        or (artifact.created_by, artifact.workspace_id) != (user_id, workspace_id)
        or artifact.status != "ready"
        or (row["candidate_path"], row["filename"], row["mime"], row["actual_size"])
        != (artifact.path, artifact.filename, artifact.mime, artifact.size)
        or not isinstance(row["candidate_sha256"], str)
        or len(row["candidate_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in row["candidate_sha256"])
    ):
        raise ValueError("attachment_read_unavailable")
    return "sha256:" + row["candidate_sha256"]


async def register_confirmation(
    db: aiosqlite.Connection,
    backend: ArtifactBackend,
    *,
    artifact_id: str,
    quarantine_path: str,
    filename: str,
    mime: str,
    declared_size: int,
    expected_sha256: str | None,
    user_id: str,
    workspace_id: str,
    session_id: str | None,
    expires_at: float,
) -> None:
    """Caller owns the pending-artifact transaction. No object I/O here."""
    await db.execute(
        "INSERT INTO artifact_upload_confirmations(artifact_id, quarantine_path, candidate_path, "
        "filename, mime, declared_size, expected_sha256, user_id, workspace_id, session_id, "
        "upload_expires_at, cleanup_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            quarantine_path,
            backend.immutable_upload_path(uuid4().hex),
            filename,
            mime,
            declared_size,
            expected_sha256.lower() if expected_sha256 else None,
            user_id,
            workspace_id,
            session_id,
            expires_at,
            expires_at + RECOVERY_SECONDS,
        ),
    )


async def _fenced_row(db: aiosqlite.Connection, artifact_id: str, owner: str, epoch: int) -> aiosqlite.Row:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT c.* FROM artifact_upload_confirmations c JOIN artifacts a ON a.id=c.artifact_id "
        "WHERE c.artifact_id=? AND c.lease_owner=? AND c.epoch=? AND c.lease_until>? "
        "AND c.phase IN ('copying','validating') AND a.status='confirming' AND a.deleted_at IS NULL "
        "AND a.path=c.quarantine_path AND a.created_by=c.user_id AND a.filename=c.filename",
        (artifact_id, owner, epoch, time.time()),
    )
    row = await cursor.fetchone()
    if row is None:
        raise UploadConfirmationError(409, "file_confirm_in_progress")
    if row["session_id"]:
        cursor = await db.execute(
            "SELECT 1 FROM sessions WHERE id=? AND user_id=? AND workspace_id=? AND deleted_at IS NULL",
            (row["session_id"], row["user_id"], row["workspace_id"]),
        )
        if await cursor.fetchone() is None:
            raise UploadConfirmationError(403, "file_upload_session_denied")
    return row


async def confirm_upload(
    store: ArtifactStore,
    *,
    artifact_id: str,
    user_id: str,
    session_id: str | None,
    checksum_sha256: str | None,
    authorize: Callable[[], Awaitable[None]],
) -> tuple[aiosqlite.Row, bool]:
    """Claim/recover on explicit POST only. Reads never start recovery work."""
    with anyio.fail_after(60):
        return await _confirm_upload(
            store,
            artifact_id=artifact_id,
            user_id=user_id,
            session_id=session_id,
            checksum_sha256=checksum_sha256,
            authorize=authorize,
        )


async def _confirm_upload(
    store: ArtifactStore,
    *,
    artifact_id: str,
    user_id: str,
    session_id: str | None,
    checksum_sha256: str | None,
    authorize: Callable[[], Awaitable[None]],
) -> tuple[aiosqlite.Row, bool]:
    async with _CONFIRM_LIMITER:
        await authorize()
        owner = uuid4().hex
        async with _open_db(store.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT c.*, a.status AS artifact_status, a.path AS artifact_path, a.size AS artifact_size, a.mime AS artifact_mime, a.deleted_at "
                "FROM artifact_upload_confirmations c JOIN artifacts a ON a.id=c.artifact_id WHERE c.artifact_id=?",
                (artifact_id,),
            )
            row = await cursor.fetchone()
            if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
                raise UploadConfirmationError(404, "file_not_found")
            if row["session_id"] != session_id:
                raise UploadConfirmationError(409, "file_upload_session_conflict")
            expected_sha = row["expected_sha256"]
            if checksum_sha256 and expected_sha and checksum_sha256.lower() != expected_sha:
                raise UploadConfirmationError(415, "file_office_invalid_package")
            if row["phase"] == "ready":
                if (
                    row["artifact_status"] != "ready"
                    or row["artifact_path"] != row["candidate_path"]
                    or row["artifact_size"] != row["actual_size"]
                    or row["artifact_mime"] != row["mime"]
                ):
                    raise UploadConfirmationError(503, "file_service_unavailable")
                if checksum_sha256 and checksum_sha256.lower() != row["candidate_sha256"]:
                    raise UploadConfirmationError(415, "file_office_invalid_package")
                return row, True
            if row["phase"] == "rejected":
                code = row["error_code"]
                raise UploadConfirmationError(413 if code == "file_office_package_limit" else 415, code)
            if row["phase"] == "expired" or time.time() > row["upload_expires_at"] + RECOVERY_SECONDS:
                raise UploadConfirmationError(410, "file_presign_expired")
            if row["lease_until"] > time.time() or row["artifact_status"] not in {"pending_upload", "confirming"}:
                raise UploadConfirmationError(409, "file_confirm_in_progress")
            epoch = row["epoch"] + 1
            await db.execute(
                "UPDATE artifact_upload_confirmations SET epoch=?, lease_owner=?, lease_until=?, "
                "phase=CASE WHEN phase='pending' THEN 'copying' ELSE phase END WHERE artifact_id=?",
                (epoch, owner, time.time() + LEASE_SECONDS, artifact_id),
            )
            await db.execute("UPDATE artifacts SET status='confirming' WHERE id=?", (artifact_id,))
            await db.commit()

        try:
            backend = store.backend
            try:
                content = await backend.read_bounded(row["candidate_path"], limit=MAX_OFFICE_BYTES)
            except FileNotFoundError:
                if row["candidate_sha256"] is not None or row["phase"] == "validating":
                    # Losing a checkpointed candidate is not permission to reread
                    # the mutable source and silently change the frozen input.
                    raise UploadConfirmationError(503, "file_service_unavailable") from None
                try:
                    content = await backend.read_bounded(row["quarantine_path"], limit=MAX_OFFICE_BYTES)
                except FileNotFoundError:
                    expired = time.time() > row["upload_expires_at"]
                    raise UploadConfirmationError(
                        410 if expired else 404, "file_presign_expired" if expired else "file_upload_not_received"
                    ) from None
                copy_digest = hashlib.sha256(content).hexdigest()
                created = await store.put_account_owned_immutable(user_id=user_id, path=row["candidate_path"], content=content, mime=row["mime"])
                # A lost write acknowledgement or another claimant may have won
                # create-if-absent. Only readback, never the PUT input, is truth.
                content = await backend.read_bounded(row["candidate_path"], limit=MAX_OFFICE_BYTES)
                if created and hashlib.sha256(content).hexdigest() != copy_digest:
                    raise OfficePackageError("file_office_invalid_package")
            digest = hashlib.sha256(content).hexdigest()
            async with _open_db(store.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await _fenced_row(db, artifact_id, owner, epoch)
                if current["candidate_sha256"] not in {None, digest}:
                    raise UploadConfirmationError(503, "file_service_unavailable")
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET phase='validating', candidate_sha256=? WHERE artifact_id=?",
                    (digest, artifact_id),
                )
                await db.commit()

            info = await anyio.to_thread.run_sync(partial(inspect_office_package, content, filename=row["filename"], declared_mime=row["mime"]))
            if info.size_bytes != row["declared_size"] or (expected_sha and digest != expected_sha):
                raise OfficePackageError("file_office_invalid_package")
            if checksum_sha256 and digest != checksum_sha256.lower():
                raise OfficePackageError("file_office_invalid_package")
            await authorize()  # Recheck session/space write permission before publish.
            async with _open_db(store.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await _fenced_row(db, artifact_id, owner, epoch)
                now_iso = datetime.now(tz=UTC).isoformat(timespec="microseconds")
                await db.execute(
                    "UPDATE artifacts SET path=?, size=?, mime=?, status='ready', expires_at=NULL, "
                    "kind='user_upload', inject='full', producer_kind='upload', created_at=? WHERE id=?",
                    (row["candidate_path"], info.size_bytes, info.mime, now_iso, artifact_id),
                )
                if session_id:
                    await db.execute(
                        "INSERT INTO session_attachments(session_id, artifact_id, pinned_at, detached_at) VALUES (?, ?, ?, NULL) "
                        "ON CONFLICT(session_id, artifact_id) DO UPDATE SET detached_at=NULL",
                        (session_id, artifact_id, now_iso),
                    )
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET phase='ready', actual_size=?, check_profile=?, package_info_json=?, "
                    "lease_owner=NULL, lease_until=0 WHERE artifact_id=?",
                    (info.size_bytes, CHECK_PROFILE, json.dumps(asdict(info), sort_keys=True), artifact_id),
                )
                await db.commit()
            receipt = await get_confirmation(store.db_path, artifact_id)
            assert receipt is not None
            return receipt, False
        except (OfficePackageError, ArtifactReadLimitError) as exc:
            code = exc.code if isinstance(exc, OfficePackageError) else "file_office_package_limit"
            async with _open_db(store.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await _fenced_row(db, artifact_id, owner, epoch)
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET phase='rejected', error_code=?, lease_owner=NULL, lease_until=0 WHERE artifact_id=?",
                    (code, artifact_id),
                )
                await db.commit()
            raise UploadConfirmationError(413 if code == "file_office_package_limit" else 415, code) from None
        finally:
            # A lost/ambiguous commit never deletes bytes. Hard crashes/cancellation
            # retain the lease; explicit retries recover after expiry.
            with anyio.CancelScope(shield=True):
                async with _open_db(store.db_path) as db:
                    await db.execute(
                        "UPDATE artifact_upload_confirmations SET lease_owner=NULL, lease_until=0 WHERE artifact_id=? AND epoch=? AND lease_owner=?",
                        (artifact_id, epoch, owner),
                    )
                    await db.commit()


async def cleanup_confirmations(store: ArtifactStore, *, now: float, limit: int = 200) -> int:
    """Fenced, exact-key GC, including when the feature flag is disabled.

    Keep receipt/tombstone keys and revisit them daily: an in-flight browser PUT
    may finish after expiry/deletion. No prefix-wide delete of user uploads.
    """
    async with _open_db(store.db_path) as db:
        if not await has_confirmation_schema(db):
            return 0
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT artifact_id FROM artifact_upload_confirmations WHERE cleanup_after<=? ORDER BY cleanup_after, artifact_id LIMIT ?",
            (now, limit),
        )
        ids = [row[0] for row in await cursor.fetchall()]
    cleaned = 0
    for artifact_id in ids:
        async with _open_db(store.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM artifact_upload_confirmations WHERE artifact_id=?", (artifact_id,))
            row = await cursor.fetchone()
            if row["cleanup_after"] > now:
                continue
            if row["lease_until"] > now or row["upload_expires_at"] + RECOVERY_SECONDS > now:
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET cleanup_after=? WHERE artifact_id=?",
                    (max(row["lease_until"], row["upload_expires_at"] + RECOVERY_SECONDS) + 1, artifact_id),
                )
                await db.commit()
                continue
            if row["phase"] not in {"ready", "rejected", "expired"}:
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET phase='expired', epoch=epoch+1, lease_owner=NULL, lease_until=0 WHERE artifact_id=?",
                    (artifact_id,),
                )
            cursor = await db.execute("SELECT path FROM artifacts WHERE id=? AND status='ready'", (artifact_id,))
            artifact = await cursor.fetchone()
            keep_candidate = artifact is not None and artifact["path"] == row["candidate_path"]
            if artifact is not None and not keep_candidate:
                log.warning("documents.upload_cleanup_identity_mismatch", artifact_id=artifact_id)
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET cleanup_after=? WHERE artifact_id=?", (now + RECOVERY_SECONDS, artifact_id)
                )
                await db.commit()
                continue  # An inconsistent published identity is not an orphan.
            if not keep_candidate:
                await db.execute("UPDATE artifacts SET status='expiring' WHERE id=? AND status IN ('pending_upload','confirming')", (artifact_id,))
            await db.commit()  # Terminal phase is the persistent delete claim.
        try:
            await store.backend.delete(row["quarantine_path"])
            if not keep_candidate:
                await store.backend.delete(row["candidate_path"])
            async with _open_db(store.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM artifact_upload_bindings WHERE artifact_id=? AND EXISTS (SELECT 1 FROM artifacts WHERE id=? AND status='expiring')",
                    (artifact_id, artifact_id),
                )
                await db.execute("DELETE FROM artifacts WHERE id=? AND status='expiring'", (artifact_id,))
                await db.execute(
                    "UPDATE artifact_upload_confirmations SET cleanup_after=? WHERE artifact_id=?", (now + RECOVERY_SECONDS, artifact_id)
                )
                await db.commit()
            cleaned += 1
        except Exception as exc:
            log.warning("documents.upload_cleanup_failed", artifact_id=artifact_id, error_type=type(exc).__name__)
    cleanup_temps = getattr(store.backend, "cleanup_immutable_upload_temps", None)
    if cleanup_temps is not None:
        await cleanup_temps(datetime.fromtimestamp(now, tz=UTC) - timedelta(seconds=RECOVERY_SECONDS))
    return cleaned
