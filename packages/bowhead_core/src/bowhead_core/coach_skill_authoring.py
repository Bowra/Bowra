"""Exact personal Skill edits with staged packages and durable save receipts.

The operation lock fences workers across processes. A prepared plan is committed
before canonical files move. Recovery never guesses from the most recent Skill:
it checks original metadata and input/output hashes under both locks and a fresh
write transaction. Uncertain or conflicting packages retain their recovery data.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from bowhead_skill.bundle_files import bundle_file_path

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.skill_bundle_lock import skill_bundle_lock, skill_write_conflict
from bowhead_core.skill_import.hook import import_review_summary
from bowhead_core.skill_instance_store import SkillInstance, SkillInstanceStore
from bowhead_core.skill_upload import (
    MAX_SKILL_SIZE,
    NAME_PATTERN,
    SkillUploadError,
    _check_user_quota,
    _reject_executables_and_symlinks,
    _validate_skill_dir,
    bundle_sha256,
)

Authorize = Callable[[aiosqlite.Connection, str, bool], Awaitable[None]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _hash(path: Path) -> str | None:
    if not path.exists():
        return None
    _files(path)
    return "sha256:" + bundle_sha256(path)


def _files(path: Path) -> list[Path]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("skill_package_unavailable")
    files, size = [], 0
    for item in path.rglob("*"):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError("skill_package_unsafe_path")
        if item.is_file():
            files.append(item)
            size += item.stat().st_size
        if len(files) > 2000 or size > MAX_SKILL_SIZE:
            raise ValueError("skill_package_limit")
    return sorted(files)


def _target(name: str, instance: SkillInstance | None, revision: str | None) -> dict:
    return {"kind": "personal_skill", "name": name, "id": instance.id if instance else None, "revision": revision}


def _patch(stage: Path, changes: list[dict]) -> None:
    if not 1 <= len(changes) <= 32 or sum(len((x["content"] or "").encode()) for x in changes) > 512 * 1024:
        raise ValueError("skill_patch_limit")
    seen = set()
    for change in changes:
        name, content = change["path"], change["content"]
        if name in seen:
            raise ValueError("skill_duplicate_patch_path")
        seen.add(name)
        if name == "SKILL.md":
            path = stage / name
            if content is None:
                raise ValueError("skill_manifest_required")
        else:
            kind, separator, filename = name.partition("/")
            if not separator:
                raise ValueError("skill_patch_path_unsupported")
            path = bundle_file_path(stage, kind, filename)
        if content is None:
            if not path.is_file():
                raise ValueError("skill_file_not_found")
            path.unlink()
        else:
            if "\x00" in content:
                raise ValueError("skill_file_not_text")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


class CoachSkillAuthoring:
    def __init__(self, db_path: str, root: Path, scope: ReferenceRequestScope, authorize: Authorize) -> None:
        self.db_path, self.root, self.scope, self.authorize = db_path, root.resolve(), scope, authorize
        self.instances = SkillInstanceStore(db_path)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_shared_ctx(self.db_path) as db:
            yield db

    async def _quota(self, db: aiosqlite.Connection, name: str, size: int) -> None:
        cursor = await db.execute(
            "SELECT name,size_bytes FROM skill_instances WHERE user_id=? AND source_type IN ('user_upload','user_generated')",
            (self.scope.user_id,),
        )
        _check_user_quota({r[0]: int(r[1] or 0) for r in await cursor.fetchall()}, name=name, new_size_bytes=size)

    def _path(self, name: str, instance: SkillInstance | None) -> Path:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("skill_name_invalid")
        candidate = Path(instance.source_path) if instance else self.root / name
        if instance and instance.source_type not in {"user_generated", "user_upload"}:
            raise ValueError("skill_personal_target_required")
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root) or candidate.resolve() == self.root:
            raise ValueError("skill_package_unavailable")
        return candidate.resolve()

    async def _current(self, db: aiosqlite.Connection, name: str) -> SkillInstance | None:
        return await self.instances.get(user_id=self.scope.user_id, name=name, connection=db)

    async def inspect(self, *, name: str, offset: int = 0, limit: int = 100, finding_offset: int = 0) -> dict:
        async with self._connection() as db:
            await db.execute("BEGIN")
            await self.authorize(db, name, False)
            instance = await self._current(db, name)
            path = self._path(name, instance)
            with skill_bundle_lock(path, shared=True):
                revision = _hash(path)
                if bool(instance) != bool(revision):
                    raise skill_write_conflict()
                files = _files(path) if instance else []
                inventory = [
                    {"path": p.relative_to(path).as_posix(), "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in files[offset : offset + limit]
                ]
        return {
            "ok": True,
            "target": _target(name, instance, revision),
            "files": inventory,
            "next_offset": offset + limit if offset + limit < len(files) else None,
            "import_review": import_review_summary(instance, finding_offset),
            "enabled": instance.enabled if instance else None,
            "import_status": instance.import_status if instance else None,
            "authoring": {
                "scope": "personal",
                "text_patch_roots": ["SKILL.md", "references/", "scripts/", "evals/"],
                "preserves_other_files": True,
                "max_patch_bytes": 512 * 1024,
            },
            "execution_authority": False,
        }

    async def _check_target(self, db: aiosqlite.Connection, target: dict) -> tuple[SkillInstance | None, Path]:
        current = await self._current(db, target["name"])
        path = self._path(target["name"], current)
        if _target(target["name"], current, _hash(path)) != target:
            raise skill_write_conflict()
        if bool(current) != path.exists():
            raise skill_write_conflict()
        return current, path

    async def read(self, *, target: dict, file: str, offset: int = 0, limit: int = 16000) -> dict:
        async with self._connection() as db:
            await db.execute("BEGIN")
            await self.authorize(db, target["name"], False)
            current = await self._current(db, target["name"])
            with skill_bundle_lock(self._path(target["name"], current), shared=True):
                _, path = await self._check_target(db, target)
                files = {p.relative_to(path).as_posix(): p for p in _files(path)}
                if file not in files or files[file].stat().st_size > 1024 * 1024:
                    raise ValueError("skill_file_unavailable")
                try:
                    content = files[file].read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("skill_file_not_text") from exc
                if "\x00" in content:
                    raise ValueError("skill_file_not_text")
                return {
                    "ok": True,
                    "target": target,
                    "file": file,
                    "content": content[offset : offset + limit],
                    "offset": offset,
                    "next_offset": offset + limit if offset + limit < len(content) else None,
                    "offset_unit": "unicode_characters",
                    "untrusted_content": True,
                }

    def _stage(self, path: Path, stage: Path, changes: list[dict], name: str):
        if path.exists():
            _files(path)
            shutil.copytree(path, stage)
        else:
            stage.mkdir(parents=True)
        _patch(stage, changes)
        _files(stage)
        _reject_executables_and_symlinks(stage)
        validated = _validate_skill_dir(stage)
        if validated.name != name:
            raise ValueError("skill_name_mismatch")
        return validated

    async def validate(self, *, target: dict, changes: list[dict]) -> dict:
        from bowhead_core.coach_validation import authoring_validation_report

        async with self._connection() as db:
            await db.execute("BEGIN")
            await self.authorize(db, target["name"], False)
            current = await self._current(db, target["name"])
            with skill_bundle_lock(self._path(target["name"], current)), tempfile.TemporaryDirectory(prefix="coach-skill-check-") as tmp:
                _, path = await self._check_target(db, target)
                validated = self._stage(path, Path(tmp) / "package", changes, target["name"])
                return {
                    "ok": True,
                    "validation_report": authoring_validation_report(
                        target=target,
                        candidate_hash=validated.content_hash,
                        structure="passed",
                        dependencies="not_run",
                    ),
                    "target": target,
                    "candidate_revision": validated.content_hash,
                    "structural_validation": "passed",
                    "warnings": validated.warnings,
                    "import_inspection": "not_run",
                    "trial": "not_run",
                    "saved": False,
                }

    async def _row(self, db: aiosqlite.Connection, operation_id: str) -> dict | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM coach_skill_operation WHERE id=? AND team_id=? AND user_id=? AND workspace_id=?",
            (operation_id, self.scope.team_id, self.scope.user_id, self.scope.workspace_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _receipt(row: dict) -> dict:
        return {
            "ok": row["state"] == "succeeded",
            "operation_id": row["id"],
            "source_session_id": row["session_id"],
            "source_turn_id": row["turn_id"],
            "state": row["state"],
            "outcome": "unknown" if row["state"] in {"reserved", "prepared"} else row["state"],
            "original_target": json.loads(row["target_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "retry": "recover_original_operation" if row["state"] in {"reserved", "prepared"} else "read_original_receipt",
        }

    async def get(self, *, operation_id: str, recover: bool = False) -> dict:
        async with self._connection() as db:
            await db.execute("BEGIN")
            row = await self._row(db, operation_id)
            if row is None:
                raise ValueError("skill_operation_unavailable")
            await self.authorize(db, json.loads(row["target_json"])["name"], recover)
        if not recover:
            return self._receipt(row)
        with skill_bundle_lock(self.root / ".coach-operations" / operation_id):
            return await self._recover(operation_id)

    async def cancel(self, *, operation_id: str) -> dict:
        """Stop unapplied work; reconcile published bytes without undoing a save."""
        if re.fullmatch(r"csw_[0-9a-f]{64}", operation_id) is None:
            raise ValueError("skill_operation_unavailable")
        op_dir = self.root / ".coach-operations" / operation_id
        reconcile_published = False
        with skill_bundle_lock(op_dir):
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await self._row(db, operation_id)
                if row is None:
                    raise ValueError("skill_operation_unavailable")
                target = json.loads(row["target_json"])
                await self.authorize(db, target["name"], True)
                if row["state"] == "prepared":
                    plan = json.loads(row["plan_json"])
                    current = await self._current(db, target["name"])
                    path = self._path(target["name"], current)
                    if str(path) != plan["path"] or (asdict(current) if current else None) != plan["baseline"]:
                        return {**self._receipt(row), "cancellation_status": "unknown", "error": "skill_recovery_conflict"}
                    with skill_bundle_lock(path):
                        actual, before, after = _hash(path), target["revision"], plan["revision"]
                        backup = op_dir / "backup"
                        if actual == after:
                            reconcile_published = True
                        elif actual == before or (actual is None and before is not None and _hash(backup) == before):
                            # Restoring a moved original is not overwriting a
                            # published candidate. A restart can repeat this check.
                            if actual is None and before is not None:
                                backup.rename(path)
                            original = plan.get("original_review")
                            if original is not None:
                                await db.execute(
                                    "UPDATE skill_instances SET import_status=?,import_report_json=?,verdict=?,verdict_json=? WHERE id=?",
                                    (
                                        original["import_status"],
                                        original["import_report_json"],
                                        original["verdict"],
                                        original["verdict_json"],
                                        current.id,
                                    ),
                                )
                            await self._record_cancellation(db, operation_id)
                            await db.commit()
                        else:
                            return {**self._receipt(row), "cancellation_status": "unknown", "error": "skill_recovery_conflict"}
                if row["state"] == "reserved":
                    await self._record_cancellation(db, operation_id)
                    await db.commit()
                result = self._receipt(await self._row(db, operation_id))
            if reconcile_published:
                result = await self._recover(operation_id)
            if result["state"] in {"succeeded", "failed"}:
                shutil.rmtree(op_dir, ignore_errors=True)
            return {
                **result,
                "cancellation_status": "already_applied"
                if result["state"] == "succeeded"
                else "cancelled"
                if (result.get("result") or {}).get("error") == "skill_save_cancelled"
                else "already_terminal"
                if result["state"] == "failed"
                else "unknown",
            }

    @staticmethod
    async def _record_cancellation(db: aiosqlite.Connection, operation_id: str) -> None:
        await db.execute(
            "UPDATE coach_skill_operation SET state='failed',result_json=?,updated_at=? WHERE id=?",
            (_json({"error": "skill_save_cancelled", "effect": "not_applied"}), datetime.now(UTC).isoformat(), operation_id),
        )

    async def save(self, *, target: dict, changes: list[dict], request_id: str, turn_id: str) -> dict:
        operation_id = "csw_" + _digest([self.scope.model_dump(), turn_id, request_id])[7:]
        intent = _digest({"target": target, "changes": changes})
        op_dir = self.root / ".coach-operations" / operation_id
        with skill_bundle_lock(op_dir):
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await self.authorize(db, target["name"], True)
                row = await self._row(db, operation_id)
                if row:
                    if row["request_hash"] != intent:
                        raise ValueError("skill_idempotency_conflict")
                    return self._receipt(row)
                cursor = await db.execute(
                    "SELECT id FROM coach_skill_operation WHERE user_id=? AND state IN ('reserved','prepared') AND json_extract(target_json,'$.name')=?",
                    (self.scope.user_id, target["name"]),
                )
                if await cursor.fetchone():
                    raise ValueError("skill_pending_save_requires_recovery")
                cursor = await db.execute(
                    "SELECT count(*) FROM coach_skill_operation WHERE user_id=? AND state IN ('reserved','prepared')", (self.scope.user_id,)
                )
                if (await cursor.fetchone())[0] >= 8:
                    raise ValueError("skill_pending_operation_limit")
                now = datetime.now(UTC).isoformat()
                await db.execute(
                    """INSERT INTO coach_skill_operation(id,team_id,user_id,workspace_id,session_id,turn_id,request_hash,target_json,state,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,'reserved',?,?)""",
                    (
                        operation_id,
                        self.scope.team_id,
                        self.scope.user_id,
                        self.scope.workspace_id,
                        self.scope.session_id or "",
                        turn_id,
                        intent,
                        _json(target),
                        now,
                        now,
                    ),
                )
                await db.commit()
            try:
                async with self._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    await self.authorize(db, target["name"], True)
                    current = await self._current(db, target["name"])
                    with skill_bundle_lock(self._path(target["name"], current)):
                        current, path = await self._check_target(db, target)
                        validated = self._stage(path, op_dir / "package", changes, target["name"])
                        await self._quota(db, target["name"], validated.size_bytes)
                        original_review = asdict(current) if current else None
                        if current:
                            # Quarantine before the first filesystem effect is
                            # visible. A crash cannot expose new bytes under an
                            # old import override or business verdict.
                            await db.execute(
                                "UPDATE skill_instances SET import_status='pending',import_report_json=NULL,verdict=NULL,verdict_json=NULL WHERE id=?",
                                (current.id,),
                            )
                            current = await self._current(db, target["name"])
                        plan = {
                            "original_review": original_review,
                            "path": str(path),
                            "baseline": asdict(current) if current else None,
                            "revision": validated.content_hash,
                            "name": validated.name,
                            "version": validated.version,
                            "description": validated.description,
                            "size_bytes": validated.size_bytes,
                            "risk_report": validated.risk_report,
                        }
                        await db.execute(
                            "UPDATE coach_skill_operation SET state='prepared',plan_json=?,updated_at=? WHERE id=? AND state='reserved'",
                            (_json(plan), datetime.now(UTC).isoformat(), operation_id),
                        )
                        await db.commit()
                return await self._recover(operation_id)
            except Exception as exc:
                # A commit can fail after durable success. Read the ledger before
                # classifying anything; never clean up an uncertain publication.
                async with self._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    row = await self._row(db, operation_id)
                    if row and row["state"] == "reserved":
                        code = exc.code if isinstance(exc, SkillUploadError) else "skill_preparation_failed"
                        await db.execute(
                            "UPDATE coach_skill_operation SET state='failed',result_json=? WHERE id=?", (_json({"error": code}), operation_id)
                        )
                        await db.commit()
                    row = await self._row(db, operation_id)
                if row is None:
                    raise
                if row["state"] == "failed":
                    shutil.rmtree(op_dir, ignore_errors=True)
                return self._receipt(row)

    async def _recover(self, operation_id: str) -> dict:
        op_dir = self.root / ".coach-operations" / operation_id
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._row(db, operation_id)
            if row is None:
                raise ValueError("skill_operation_unavailable")
            target = json.loads(row["target_json"])
            await self.authorize(db, target["name"], True)
            if row["state"] == "reserved":
                await db.execute(
                    "UPDATE coach_skill_operation SET state='failed',result_json=? WHERE id=?",
                    (_json({"error": "skill_preparation_interrupted"}), operation_id),
                )
            elif row["state"] == "prepared":
                plan = json.loads(row["plan_json"])
                current = await self._current(db, target["name"])
                path = self._path(target["name"], current)
                if str(path) != plan["path"] or (asdict(current) if current else None) != plan["baseline"]:
                    return {**self._receipt(row), "error": "skill_recovery_conflict"}
                with skill_bundle_lock(path):
                    actual, before, after = _hash(path), target["revision"], plan["revision"]
                    stage, backup = op_dir / "package", op_dir / "backup"
                    if actual != after:
                        if _hash(stage) != after or (actual != before and not (actual is None and before is not None and _hash(backup) == before)):
                            return {**self._receipt(row), "error": "skill_recovery_conflict"}
                    await self._quota(db, target["name"], plan["size_bytes"])
                    if actual != after:
                        if path.exists():
                            if backup.exists():
                                return {**self._receipt(row), "error": "skill_recovery_conflict"}
                            path.rename(backup)
                        stage.rename(path)
                    instance = await self.instances.upsert(
                        user_id=self.scope.user_id,
                        name=target["name"],
                        source_type=current.source_type if current else "user_generated",
                        source_path=str(path),
                        version=plan["version"],
                        description=plan["description"],
                        enabled=current.enabled if current else True,
                        source_turn_id=row["turn_id"],
                        size_bytes=plan["size_bytes"],
                        risk_report_json=_json(plan["risk_report"]),
                        reset_verdict=True,
                        expected_content_hash=after,
                        connection=db,
                    )
                    # New bytes cannot inherit an old override or execution verdict.
                    await db.execute("UPDATE skill_instances SET import_status='pending',import_report_json=NULL WHERE id=?", (instance.id,))
                    result = {
                        "effect": "personal_skill_saved",
                        "target": _target(target["name"], instance, after),
                        "import_status_at_save": "pending",
                        "trial": "not_run",
                        "team_published": False,
                    }
                    await db.execute(
                        "UPDATE coach_skill_operation SET state='succeeded',result_json=?,updated_at=? WHERE id=?",
                        (_json(result), datetime.now(UTC).isoformat(), operation_id),
                    )
                    await db.commit()
            await db.commit()
            result = self._receipt(await self._row(db, operation_id))
        # Only committed terminal operations may discard staged data. Cleanup
        # failures cannot turn a successful save into a request to save again.
        if result["state"] in {"succeeded", "failed"}:
            shutil.rmtree(op_dir, ignore_errors=True)
        return result
