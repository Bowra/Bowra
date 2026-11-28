"""Account deletion adapters for KB indexes, secondary databases and packages."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import anyio
from bowhead_core._db import execute_and_close
from bowhead_core.account_deletion import AccountDeletionError
from bowhead_core.account_erasure import _POLICY, _q
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect
from bowhead_core.sqlite_domains import APPENDED_COLUMNS, APPENDED_TABLES, DOMAIN_TARGET_FILES, domain_database_path


def _reviewed_secondary(filename: str, tables: dict[str, list[str]], actual: set[str]) -> dict[str, list[str]]:
    """The reviewed tables and columns one secondary file holds.

    Tables and columns appended after the domain split are absent from the frozen pre-split
    schema, together; any other difference is an unreviewed schema.
    """
    if actual == set(tables):
        return tables
    domain = next((name for name, target in DOMAIN_TARGET_FILES.items() if target == filename), None)
    appended = {table for table, (owner, _revision) in APPENDED_TABLES.items() if owner == domain and table in tables}
    if appended and actual == set(tables) - appended:
        return {
            table: [column for column in columns if column not in APPENDED_COLUMNS.get(table, ((), ""))[0]]
            for table, columns in tables.items()
            if table not in appended
        }
    raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")


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
                    reviewed = _reviewed_secondary(filename, tables, {row[0] for row in await cur.fetchall()})
                for table, columns in reviewed.items():
                    async with db.execute(f"PRAGMA table_info({_q(table)})") as cur:
                        if [row[1] for row in await cur.fetchall()] != columns:
                            raise AccountDeletionError("account_erasure_unreviewed_secondary_schema")

    async def _knowledge_base_successors(self, user_id: str) -> dict[str, str]:
        """The Team owner who takes over each Team's Space and Team knowledge bases the user owns.

        Ownership handover (PRD phase 2 R2) is not built yet; until then the Team owner receives them.
        """
        async with diagnostic_connect(self.root / "kb_files.db") as kb:
            async with kb.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='kb_knowledge_bases'") as cur:
                if await cur.fetchone() is None:
                    return {}
            async with kb.execute("SELECT DISTINCT team_id FROM kb_knowledge_bases WHERE owner_user_id=? AND home!='personal'", (user_id,)) as cur:
                team_ids = [row[0] for row in await cur.fetchall()]
        if not team_ids:
            return {}
        async with (
            diagnostic_connect(self.app.state.user_store.db_path) as db,
            db.execute(
                "SELECT id,created_by_user_id FROM teams WHERE id IN (" + ",".join("?" for _ in team_ids) + ")",  # noqa: S608 -- Placeholders only.
                team_ids,
            ) as cur,
        ):
            owners = {row[0]: row[1] for row in await cur.fetchall() if row[1] and row[1] != user_id}
        # A Team without an eligible owner keeps the knowledge base under the erased placeholder;
        # its automatic managers still manage it.
        return {team_id: owners.get(team_id, "erased") for team_id in team_ids}

    async def quarantine(self, user_id: str, operation_id: str) -> None:
        # Scope, not uploader identity, owns knowledge: a personal file belongs to its
        # scope owner (``KbPermission.owner_of``), as everywhere else. Keep shared
        # knowledge's backing artifact as a Team asset before removing the departed
        # user's content.
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
                "WHERE scope='personal' AND scope_id=? AND parse_error IS NOT 'account_deleted'",
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
                db.execute("SELECT file_id,artifact_id,scope,scope_id FROM kb_files") as cur,
            ):
                for file_id, artifact_id, scope, scope_id in await cur.fetchall():
                    if artifact_id in artifact_ids or (scope == "personal" and scope_id == user_id):
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
        successors = await self._knowledge_base_successors(user_id) if files_path.is_file() else {}
        # Sources are removed last, so a failed extraction-store cleanup cannot
        # lose the identity needed to recover its next attempt.
        for filename, tables in sorted(_POLICY["secondary"].items(), key=lambda item: item[0] == "kb_files.db"):
            path = self.root / filename
            if not path.is_file():
                continue
            async with diagnostic_connect(path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await execute_and_close(db, "PRAGMA main.secure_delete=ON")
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','_phase0_probe')"
                ) as cur:
                    reviewed = _reviewed_secondary(filename, tables, {row[0] for row in await cur.fetchall()})
                for table, expected in reviewed.items():
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
                    if table == "kb_matrix_cell_events":
                        # Events of the departed user's files left with the file above; the history of
                        # shared cells stays, without naming the person.
                        await db.execute("UPDATE kb_matrix_cell_events SET actor_user_id='erased' WHERE actor_user_id=?", (user_id,))
                    if table == "kb_knowledge_base_members":
                        await db.execute("DELETE FROM kb_knowledge_base_members WHERE principal_type='user' AND principal_id=?", (user_id,))
                        await db.execute("UPDATE kb_knowledge_base_members SET added_by='erased' WHERE added_by=?", (user_id,))
                    if table == "kb_knowledge_bases":
                        # My knowledge bases leave with the account; Space and Team ones stay with a new owner.
                        for team_id, successor in successors.items():
                            await db.execute(
                                "UPDATE kb_knowledge_bases SET owner_user_id=?,revision=revision+1 WHERE team_id=? AND owner_user_id=? AND home!='personal'",
                                (successor, team_id, user_id),
                            )
                        # The account's personal files are gone; a remaining row that still names its
                        # My knowledge (only a stale value can) is detached, since NULL already means
                        # the default of the row's current scope.
                        await db.execute(
                            "UPDATE kb_files SET knowledge_base_id=NULL WHERE knowledge_base_id IN "
                            "(SELECT id FROM kb_knowledge_bases WHERE home='personal' AND home_id=?)",
                            (user_id,),
                        )
                        await db.execute("DELETE FROM kb_knowledge_bases WHERE home='personal' AND home_id=?", (user_id,))
                    if "user_id" in expected:
                        await db.execute(f"DELETE FROM {_q(table)} WHERE user_id=?", (user_id,))
                if filename == "kb_matrix.db" and "visibility" in reviewed.get("kb_matrices", ()):
                    # The departed user's only-me matrices (S8) leave with the account: nobody else
                    # could see them. Their cells, runs and cell history go first.
                    for table in ("kb_matrix_cell_events", "kb_matrix_cells", "kb_matrix_runs"):
                        if table in reviewed:
                            await db.execute(
                                f"DELETE FROM {_q(table)} WHERE matrix_id IN "  # noqa: S608 -- Reviewed table name.
                                "(SELECT matrix_id FROM kb_matrices WHERE created_by=? AND visibility='private')",
                                (user_id,),
                            )
                    await db.execute("DELETE FROM kb_matrices WHERE created_by=? AND visibility='private'", (user_id,))
                # Space matrices are shared resources. Remove the departed user's
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
