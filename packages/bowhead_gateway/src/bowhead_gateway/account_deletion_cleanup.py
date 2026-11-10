"""Account deletion adapters for KB indexes, secondary databases and packages."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import anyio
from bowhead_core.account_deletion import AccountDeletionError
from bowhead_core.account_erasure import _POLICY, _q
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect
from bowhead_core.sqlite_domains import domain_database_path


class AccountExternalCleanup:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.root = Path(app.state.user_store.db_path).parent

    async def check_ready(self) -> None:
        for filename, tables in _POLICY["secondary"].items():
            path = self.root / filename
            if not path.is_file():
                raise AccountDeletionError("account_erasure_secondary_missing")
            async with diagnostic_connect(path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','_phase0_probe')"
                ) as cur:
                    if {row[0] for row in await cur.fetchall()} != set(tables):
                        raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")
                for table, columns in tables.items():
                    async with db.execute(f"PRAGMA table_info({_q(table)})") as cur:
                        if [row[1] for row in await cur.fetchall()] != columns:
                            raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")

    async def quarantine(self, user_id: str, operation_id: str) -> None:
        # Scope, not uploader identity, owns shared knowledge. Keep its backing
        # artifact as a Team asset before removing the departed user's content.
        path = self.root / "kb_files.db"
        if not path.is_file():
            return
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute("SELECT purge_after,requested_at FROM account_deletions WHERE operation_id=?", (operation_id,)) as cur,
        ):
            deletion = await cur.fetchone()
        if deletion is None:
            raise AccountDeletionError("account_deletion_operation_missing")
        async with diagnostic_connect(path) as kb:
            await kb.execute("INSERT OR IGNORE INTO account_retention_holds VALUES (?,?)", (user_id, deletion[0]))
            await kb.execute(
                "UPDATE kb_files SET soft_deleted_at=COALESCE(soft_deleted_at,?),version=version+1,parse_status='failed',parse_error='account_deleted' "
                "WHERE uploaded_by=? AND scope='personal' AND parse_error IS NOT 'account_deleted'",
                (deletion[1], user_id),
            )
            await kb.commit()
        async with (
            diagnostic_connect(path) as kb,
            kb.execute("SELECT DISTINCT artifact_id FROM kb_files WHERE scope IN ('space','workspace','team')") as cur,
        ):
            shared = [row[0] for row in await cur.fetchall()]
        async with diagnostic_connect(domain_database_path(self.app.state.user_store.db_path, "content")) as db:
            for artifact_id in shared:
                await db.execute("UPDATE artifacts SET scope='team' WHERE id=? AND created_by=?", (artifact_id, user_id))
            await db.commit()

    async def __call__(self, user_id: str, operation_id: str) -> None:
        await self.check_ready()
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute(
                "SELECT json_extract(row_key,'$[0]') FROM account_deletion_rows WHERE operation_id=? AND table_name='artifacts'", (operation_id,)
            ) as cur,
        ):
            artifact_ids = {row[0] for row in await cur.fetchall()}
        file_ids: set[str] = set()
        files_path = self.root / "kb_files.db"
        if files_path.is_file():
            async with (
                diagnostic_connect(files_path) as db,
                db.execute("SELECT file_id,artifact_id,scope,uploaded_by FROM kb_files") as cur,
            ):
                for file_id, artifact_id, scope, uploaded_by in await cur.fetchall():
                    if artifact_id in artifact_ids or (scope == "personal" and uploaded_by == user_id):
                        file_ids.add(file_id)
        # Each source file ID is retained in the core operation until every
        # secondary store and historical index has acknowledged deletion.
        async with diagnostic_connect(self.app.state.user_store.db_path) as db:
            for file_id in file_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO account_deletion_objects(operation_id,storage_kind,object_key) VALUES (?,'kb_file',?)",
                    (operation_id, file_id),
                )
            async with db.execute(
                "SELECT object_key FROM account_deletion_objects WHERE operation_id=? AND storage_kind='kb_file'", (operation_id,)
            ) as cur:
                file_ids |= {row[0] for row in await cur.fetchall()}
            await db.commit()
        if file_ids:
            await self._vectors(file_ids)
        # Sources are removed last, so a failed extraction-store cleanup cannot
        # lose the identity needed to recover its next attempt.
        for filename, tables in sorted(_POLICY["secondary"].items(), key=lambda item: item[0] == "kb_files.db"):
            path = self.root / filename
            if not path.is_file():
                continue
            async with diagnostic_connect(path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("PRAGMA main.secure_delete=ON")
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','_phase0_probe')"
                ) as cur:
                    actual = {row[0] for row in await cur.fetchall()}
                if actual != set(tables):
                    raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")
                for table, expected in tables.items():
                    async with db.execute(f"PRAGMA table_info({_q(table)})") as cur:
                        if [row[1] for row in await cur.fetchall()] != expected:
                            raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")
                    if "file_id" in expected:
                        for file_id in file_ids:
                            await db.execute(f"DELETE FROM {_q(table)} WHERE file_id=?", (file_id,))
                    if table == "kb_source_description_jobs":
                        await db.execute("DELETE FROM kb_source_description_jobs WHERE actor_user_id=?", (user_id,))
                    if table == "kb_source_categories":
                        await db.execute("DELETE FROM kb_source_categories WHERE scope='personal' AND scope_id=?", (user_id,))
                    if table in {"kb_source_categories", "kb_source_descriptions"}:
                        await db.execute(f"UPDATE {_q(table)} SET actor_user_id='erased' WHERE actor_user_id=?", (user_id,))
                    if table == "kb_membership_operations":
                        await db.execute("DELETE FROM kb_membership_operations WHERE actor_user_id=?", (user_id,))
                    if "user_id" in expected:
                        await db.execute(f"DELETE FROM {_q(table)} WHERE user_id=?", (user_id,))
                # Matrices are shared resources. Remove the departed user's
                # private source entries without deleting the matrix itself.
                if filename == "kb_matrix.db":
                    async with db.execute("SELECT matrix_id,files_json FROM kb_matrices") as cur:
                        matrices = await cur.fetchall()
                    for matrix_id, encoded in matrices:
                        entries = json.loads(encoded)
                        retained = [item for item in entries if (item.get("file_id") if isinstance(item, dict) else item) not in file_ids]
                        if retained != entries:
                            await db.execute("UPDATE kb_matrices SET files_json=? WHERE matrix_id=?", (json.dumps(retained), matrix_id))
                await db.commit()
                await db.execute("VACUUM")
                async with db.execute("PRAGMA main.wal_checkpoint(TRUNCATE)") as cur:
                    checkpoint = await cur.fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise AccountDeletionError("account_storage_checkpoint_pending")
        await self._packages(user_id, operation_id)
        async with diagnostic_connect(self.app.state.user_store.db_path) as db:
            await db.execute(
                "UPDATE account_deletion_objects SET completed_at=datetime('now') WHERE operation_id=? AND storage_kind='kb_file'", (operation_id,)
            )
            await db.commit()

    async def _vectors(self, file_ids: set[str]) -> None:
        # All active and historical embedding versions share this physical
        # client. Do not inspect only the Team's currently selected runtime.
        client = self.app.state.kb_store._client
        collections = await anyio.to_thread.run_sync(client.list_collections)
        for item in collections:
            name = item if isinstance(item, str) else item.name
            if not name.startswith("kb_"):
                continue
            collection = await anyio.to_thread.run_sync(lambda name=name: client.get_collection(name))
            for file_id in file_ids:
                await anyio.to_thread.run_sync(lambda collection=collection, file_id=file_id: collection.delete(where={"file_id": file_id}))
                observed = await anyio.to_thread.run_sync(
                    lambda collection=collection, file_id=file_id: collection.get(where={"file_id": file_id}, limit=1)
                )
                if observed["ids"]:
                    raise AccountDeletionError("account_vector_cleanup_unconfirmed")

    async def _packages(self, user_id: str, operation_id: str) -> None:
        from bowhead_core.resource_file_quarantine import QuarantinedPath, finalize_cleanup

        if Path(user_id).name != user_id or user_id in {".", ".."}:
            raise AccountDeletionError("account_package_identity_invalid")
        root = Path(".data/user_skills").resolve()
        personal = root / user_id
        if personal.is_symlink():
            raise AccountDeletionError("account_package_path_invalid")
        if personal.exists():
            await anyio.to_thread.run_sync(shutil.rmtree, personal)
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute(
                "SELECT s.source_path FROM team_skill s WHERE json_array(s.id) IN (SELECT row_key FROM account_deletion_rows WHERE operation_id=? AND table_name='team_skill') "
                "UNION SELECT s.candidate_path FROM team_skill s WHERE s.candidate_path IS NOT NULL AND json_array(s.id) IN (SELECT row_key FROM account_deletion_rows WHERE operation_id=? AND table_name='team_skill')",
                (operation_id, operation_id),
            ) as cur,
        ):
            paths = [row[0] for row in await cur.fetchall()]
        team_root = Path(".data/team_skills").resolve()
        for encoded in paths:
            if not encoded:
                continue
            path = Path(encoded)
            if path.is_symlink() or not path.resolve().is_relative_to(team_root):
                raise AccountDeletionError("account_package_path_invalid")
            if path.is_dir():
                await anyio.to_thread.run_sync(shutil.rmtree, path)
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute(
                "SELECT cleanup_id,original_path,quarantine_path FROM resource_file_cleanup_receipt WHERE actor_user_id=? AND state='pending'",
                (user_id,),
            ) as cur,
        ):
            pending = await cur.fetchall()
        entries = []
        for identity, original, quarantine in pending:
            path = Path(quarantine)
            if path.is_symlink() or path.parent.name != ".bowhead-quarantine":
                raise AccountDeletionError("account_package_path_invalid")
            entries.append(QuarantinedPath(identity, Path(original), path))
        await finalize_cleanup(str(self.app.state.user_store.db_path), entries)
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute("SELECT 1 FROM resource_file_cleanup_receipt WHERE actor_user_id=? AND state='pending' LIMIT 1", (user_id,)) as cur,
        ):
            if await cur.fetchone():
                raise AccountDeletionError("account_package_cleanup_pending")
