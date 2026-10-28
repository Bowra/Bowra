"""Protocol-aware single-Session delete/revive coordinator.

Terminal Protocol v1 Sessions use the migration-0008 lifecycle ledger.  The
operation is a resumable saga: prepare the stable request, fence an uncommitted
generation with the protocol store, then atomically update Session visibility,
delivery fences and the committed operation result.  A retry with the same
request identity resumes that saga; it never guesses the winner from the
current ``deleted_at`` value.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import aiosqlite

from bowhead_core.canvas_store import CanvasStore
from bowhead_core.session_store import SessionStore
from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
from bowhead_core.terminal_protocol import (
    AuthFenceScopeKind,
    TerminalDeleteContext,
    TerminalProtocolMalformedError,
    TerminalScope,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolStore,
    open_terminal_protocol_connection,
)

SESSION_LIFECYCLE_ERROR_CODES = frozenset(
    {
        "canvas_lifecycle_store_missing",
        "linked_canvas_not_found",
        "session_generation_not_fenced",
        "session_lifecycle_commit_conflict",
        "session_lifecycle_conflict",
        "session_lifecycle_operation_missing",
        "session_lifecycle_precondition_changed",
        "session_not_found",
        "session_owner_mismatch",
        "session_active_turn",
        "session_revive_blocked",
        "session_visibility_epoch_changed",
        "session_visibility_epoch_missing",
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
    }
)


class SessionLifecycleError(RuntimeError):
    """Stable internal lifecycle rejection used by routes/channel adapters."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionLifecycleResult:
    operation_id: str | None
    action: Literal["soft_delete", "revive"]
    changed: bool
    protocol_version: int | None
    result_code: str


@dataclass(frozen=True, slots=True)
class SessionLifecycleReplay:
    operation_id: str
    team_id: str
    session_id: str
    actor_audit_ref: str
    result_code: str


@dataclass(frozen=True, slots=True)
class _PreparedLifecycle:
    operation_id: str
    team_id: str
    session_id: str
    action: Literal["soft_delete", "revive"]
    actor_audit_ref: str
    request_id: str
    state: str
    result_code: str | None
    expected_deleted_state: str
    expected_visibility_epoch: int
    open_scope: TerminalScope | None
    generation_row_version: int | None
    candidate_id: str | None
    delivery_fence_epoch: int | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_operation_id(
    *,
    team_id: str,
    session_id: str,
    actor_audit_ref: str,
    request_id: str,
    action: str,
) -> str:
    material = f"session-lifecycle-v1\0{team_id}\0{session_id}\0{actor_audit_ref}\0{request_id}\0{action}"
    return f"slo_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


class SessionLifecycleCoordinator:
    """Only production owner of Session deleted-state mutations."""

    def __init__(
        self,
        session_store: SessionStore,
        terminal_store: TerminalProtocolStore,
        canvas_store: CanvasStore | None = None,
    ) -> None:
        if session_store.db_path != terminal_store.db_path:
            raise ValueError("Session lifecycle stores must share one database")
        if canvas_store is not None and str(canvas_store.db_path) != session_store.db_path:
            raise ValueError("Canvas lifecycle store must share the Session database")
        self._sessions = session_store
        self._terminal = terminal_store
        self._canvases = canvas_store
        self._db_path = session_store.db_path

    async def visibility_request_id(self, *, session_id: str, user_id: str, action: Literal["soft_delete", "revive"]) -> str:
        """Reuse an idle Web mutation's identity until visibility changes, including retries."""
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                "SELECT e.epoch FROM sessions s LEFT JOIN workspaces w ON w.id=s.workspace_id "
                "LEFT JOIN terminal_auth_scope_epochs e ON e.scope_kind='session_visibility' "
                "AND e.team_id=w.team_id AND e.scope_ref=s.id WHERE s.id=? AND s.user_id=?",
                (session_id, user_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise SessionLifecycleError("session_not_found")
        return f"session-{action}:{session_id}:{row[0] if row[0] is not None else 'legacy'}"

    async def soft_delete(
        self,
        *,
        session_id: str,
        actor_audit_ref: str,
        request_id: str,
        expected_user_id: str | None = None,
        authz_snapshot_digest: str = "session_lifecycle_authorized",
        linked_canvas_id: str | None = None,
        require_idle: bool = False,
    ) -> SessionLifecycleResult:
        self._validate_identity(session_id, actor_audit_ref, request_id)
        protocol_version = await self._session_protocol_version(
            session_id,
            expected_user_id=expected_user_id,
        )
        if protocol_version is None:
            protocol_version, changed = await self._legacy_transition_if_still_legacy(
                session_id=session_id,
                expected_user_id=expected_user_id,
                action="soft_delete",
                require_idle=require_idle,
            )
            if protocol_version is None:
                return SessionLifecycleResult(
                    operation_id=None,
                    action="soft_delete",
                    changed=changed,
                    protocol_version=None,
                    result_code="deleted" if changed else "already_deleted",
                )
        prepared = await self._prepare(
            session_id=session_id,
            actor_audit_ref=actor_audit_ref,
            request_id=request_id,
            action="soft_delete",
            require_idle=require_idle,
        )
        replay = self._committed_result(prepared, protocol_version=protocol_version)
        if replay is not None:
            return replay
        if prepared.expected_deleted_state == "deleted":
            return await self._commit_operation(
                prepared,
                result_code="already_deleted",
                changed=False,
                linked_canvas_id=linked_canvas_id,
            )
        if prepared.open_scope is not None:
            assert prepared.generation_row_version is not None
            assert prepared.delivery_fence_epoch is not None
            identity = {
                "action": "soft_delete",
                "operation_id": prepared.operation_id,
                "scope": {
                    "generation": prepared.open_scope.generation,
                    "turn_id": prepared.open_scope.turn_id,
                },
            }
            digest = canonical_digest_v1(identity)
            try:
                await self._terminal.commit_protocol_delete(
                    TerminalDeleteContext(
                        scope=prepared.open_scope,
                        deletion_id=f"tdel_{prepared.operation_id[4:]}",
                        expected_generation_row_version=prepared.generation_row_version,
                        expected_candidate_id=prepared.candidate_id,
                        expected_delivery_fence_epoch=prepared.delivery_fence_epoch,
                        actor_audit_ref=actor_audit_ref,
                        authz_snapshot_digest=authz_snapshot_digest,
                        policy_code="session_soft_delete",
                        effect_identity_digest=digest,
                        effect_bundle_digest=digest,
                        effect_audit_key_version="v1",
                        effect_key_required_until=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    )
                )
            except TerminalProtocolMalformedError as exc:
                if not await self._same_generation_has_committed_receipt(prepared.open_scope):
                    raise SessionLifecycleError("terminal_protocol_malformed") from exc
            else:
                await self._mark_fenced(prepared.operation_id)
        return await self._commit_operation(
            prepared,
            result_code="deleted",
            changed=True,
            linked_canvas_id=linked_canvas_id,
        )

    async def canvas_delete_committed(
        self,
        *,
        canvas_id: str,
        actor_audit_ref: str,
    ) -> bool:
        """Recognize a committed HTTP retry after the Canvas row is gone."""

        return (
            await self.canvas_delete_replay(
                canvas_id=canvas_id,
                actor_audit_ref=actor_audit_ref,
            )
            is not None
        )

    async def canvas_delete_replay(
        self,
        *,
        canvas_id: str,
        actor_audit_ref: str,
    ) -> SessionLifecycleReplay | None:
        """Return stable cleanup identity after atomic Canvas/Session commit."""

        request_id = f"canvas-delete:{canvas_id}"
        self._validate_identity(canvas_id, actor_audit_ref, request_id)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT operation_id,team_id,session_audit_ref,
                       actor_audit_ref,result_code
                FROM session_lifecycle_operations
                WHERE actor_audit_ref=? AND request_id=? AND action='soft_delete'
                  AND state='committed' AND result_code IN ('deleted','already_deleted')
                """,
                (actor_audit_ref, request_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return SessionLifecycleReplay(
            operation_id=str(row["operation_id"]),
            team_id=str(row["team_id"]),
            session_id=str(row["session_audit_ref"]),
            actor_audit_ref=str(row["actor_audit_ref"]),
            result_code=str(row["result_code"]),
        )

    async def revive(
        self,
        *,
        session_id: str,
        actor_audit_ref: str,
        request_id: str,
        expected_user_id: str,
    ) -> SessionLifecycleResult:
        self._validate_identity(session_id, actor_audit_ref, request_id)
        protocol_version = await self._session_protocol_version(
            session_id,
            expected_user_id=expected_user_id,
        )
        if protocol_version is None:
            protocol_version, changed = await self._legacy_transition_if_still_legacy(
                session_id=session_id,
                expected_user_id=expected_user_id,
                action="revive",
            )
            if protocol_version is None:
                return SessionLifecycleResult(
                    operation_id=None,
                    action="revive",
                    changed=changed,
                    protocol_version=None,
                    result_code="revived" if changed else "already_active",
                )
        prepared = await self._prepare(
            session_id=session_id,
            actor_audit_ref=actor_audit_ref,
            request_id=request_id,
            action="revive",
        )
        replay = self._committed_result(prepared, protocol_version=protocol_version)
        if replay is not None:
            return replay
        if prepared.expected_deleted_state == "active":
            return await self._commit_operation(
                prepared,
                result_code="already_active",
                changed=False,
            )
        await self._assert_revivable(prepared)
        return await self._commit_operation(
            prepared,
            result_code="revived",
            changed=True,
        )

    @staticmethod
    def _validate_identity(
        session_id: str,
        actor_audit_ref: str,
        request_id: str,
    ) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (session_id, actor_audit_ref, request_id)):
            raise ValueError("Session lifecycle identity is missing")
        if len(request_id) > 256:
            raise ValueError("Session lifecycle request identity is too long")

    async def _session_protocol_version(
        self,
        session_id: str,
        *,
        expected_user_id: str | None,
    ) -> int | None:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT s.user_id,s.active_turn_id,s.active_turn_protocol_version
                FROM sessions s WHERE s.id=?
                """,
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise SessionLifecycleError("session_not_found")
            if expected_user_id is not None and row["user_id"] != expected_user_id:
                raise SessionLifecycleError("session_owner_mismatch")
            async with connection.execute(
                """
                SELECT DISTINCT terminal_protocol_version
                FROM turns
                WHERE session_id=? AND terminal_protocol_version IS NOT NULL
                ORDER BY terminal_protocol_version
                """,
                (session_id,),
            ) as cursor:
                raw_versions = [item[0] async for item in cursor]
        protocol_version = self._classify_protocol_versions(raw_versions)
        self._validate_active_turn_protocol(
            protocol_version=protocol_version,
            active_turn_id=row["active_turn_id"],
            active_turn_protocol_version=row["active_turn_protocol_version"],
        )
        return protocol_version

    @staticmethod
    def _classify_protocol_versions(raw_versions: list[object]) -> int | None:
        if not raw_versions:
            return None
        try:
            versions = {int(value) for value in raw_versions}
        except (TypeError, ValueError) as exc:
            raise SessionLifecycleError("terminal_protocol_malformed") from exc
        if versions == {1}:
            return 1
        if any(version > 1 for version in versions):
            raise SessionLifecycleError("terminal_protocol_future_version")
        raise SessionLifecycleError("terminal_protocol_malformed")

    @staticmethod
    def _validate_active_turn_protocol(
        *,
        protocol_version: int | None,
        active_turn_id: object,
        active_turn_protocol_version: object,
    ) -> None:
        if active_turn_id is None:
            if active_turn_protocol_version is not None:
                raise SessionLifecycleError("terminal_protocol_malformed")
            return
        if protocol_version == 1 and active_turn_protocol_version != 1:
            # A rollback must not let a legacy worker remain writable while a
            # v1 Session lifecycle operation clears the active pointer.
            raise SessionLifecycleError("terminal_protocol_malformed")
        if active_turn_protocol_version not in {None, 1}:
            try:
                active_version = int(active_turn_protocol_version)
            except (TypeError, ValueError) as exc:
                raise SessionLifecycleError("terminal_protocol_malformed") from exc
            if active_version > 1:
                raise SessionLifecycleError("terminal_protocol_future_version")
            raise SessionLifecycleError("terminal_protocol_malformed")

    async def _legacy_transition_if_still_legacy(
        self,
        *,
        session_id: str,
        expected_user_id: str | None,
        action: Literal["soft_delete", "revive"],
        require_idle: bool = False,
    ) -> tuple[int | None, bool]:
        """Linearize legacy mutation against a concurrent first v1 admission."""

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    """
                    SELECT user_id,active_turn_id,active_turn_protocol_version
                    FROM sessions WHERE id=?
                    """,
                    (session_id,),
                ) as cursor:
                    session = await cursor.fetchone()
                if session is None:
                    raise SessionLifecycleError("session_not_found")
                if expected_user_id is not None and session["user_id"] != expected_user_id:
                    raise SessionLifecycleError("session_owner_mismatch")
                if require_idle:
                    await self._assert_session_idle(connection, session_id)
                async with connection.execute(
                    """
                    SELECT DISTINCT terminal_protocol_version
                    FROM turns
                    WHERE session_id=? AND terminal_protocol_version IS NOT NULL
                    ORDER BY terminal_protocol_version
                    """,
                    (session_id,),
                ) as cursor:
                    versions = [item[0] async for item in cursor]
                protocol_version = self._classify_protocol_versions(versions)
                self._validate_active_turn_protocol(
                    protocol_version=protocol_version,
                    active_turn_id=session["active_turn_id"],
                    active_turn_protocol_version=session["active_turn_protocol_version"],
                )
                if protocol_version is not None:
                    await connection.rollback()
                    return protocol_version, False
                if action == "soft_delete":
                    cursor = await connection.execute(
                        "UPDATE sessions SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                        (_now(), session_id),
                    )
                else:
                    cursor = await connection.execute(
                        "UPDATE sessions SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
                        (session_id,),
                    )
                changed = cursor.rowcount > 0
                await connection.commit()
                return None, changed
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def _assert_session_idle(self, connection: aiosqlite.Connection, session_id: str) -> None:
        """Keep user deletion's running-turn guard inside the lifecycle transaction."""
        async with connection.execute(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE id=? AND active_turn_id IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM turns WHERE session_id=? AND status='running')",
            (session_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and bool(row[0]):
            raise SessionLifecycleError("session_active_turn")

    async def _prepare(
        self,
        *,
        session_id: str,
        actor_audit_ref: str,
        request_id: str,
        action: Literal["soft_delete", "revive"],
        require_idle: bool = False,
    ) -> _PreparedLifecycle:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._read_request_operation(
                    connection,
                    session_id=session_id,
                    actor_audit_ref=actor_audit_ref,
                    request_id=request_id,
                    action=action,
                )
                if existing is not None:
                    if require_idle and existing["state"] != "committed":
                        await self._assert_session_idle(connection, session_id)
                    result = await self._prepared_from_row(connection, existing)
                    await connection.rollback()
                    return result
                async with connection.execute(
                    """
                    SELECT s.deleted_at,w.team_id,
                           s.active_turn_id,s.active_turn_protocol_version
                    FROM sessions s JOIN workspaces w ON w.id=s.workspace_id
                    WHERE s.id=?
                    """,
                    (session_id,),
                ) as cursor:
                    session = await cursor.fetchone()
                if session is None:
                    raise SessionLifecycleError("session_not_found")
                if require_idle:
                    await self._assert_session_idle(connection, session_id)
                team_id = str(session["team_id"])
                if session["active_turn_id"] is not None and session["active_turn_protocol_version"] != 1:
                    raise SessionLifecycleError("terminal_protocol_malformed")
                async with connection.execute(
                    """
                    SELECT epoch FROM terminal_auth_scope_epochs
                    WHERE scope_kind='session_visibility' AND team_id=? AND scope_ref=?
                    """,
                    (team_id, session_id),
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                if epoch_row is None:
                    raise SessionLifecycleError("session_visibility_epoch_missing")
                epoch = int(epoch_row["epoch"])
                operation_id = _stable_operation_id(
                    team_id=team_id,
                    session_id=session_id,
                    actor_audit_ref=actor_audit_ref,
                    request_id=request_id,
                    action=action,
                )
                now = _now()
                try:
                    await connection.execute(
                        """
                        INSERT INTO session_lifecycle_operations(
                          operation_id,team_id,session_audit_ref,account_audit_ref,
                          parent_operation_id,request_id,action,state,
                          expected_deleted_state,new_deleted_state,
                          expected_session_visibility_epoch,new_session_visibility_epoch,
                          target_session_count,target_sessions_digest,next_child_ordinal,
                          prior_detached_session_count,actor_audit_ref,operation_claim_epoch,
                          created_at,updated_at
                        ) VALUES (?,?,?,NULL,NULL,?,?,'prepared',?,?,?,NULL,
                                  1,?,1,0,?,0,?,?)
                        """,
                        (
                            operation_id,
                            team_id,
                            session_id,
                            request_id,
                            action,
                            "deleted" if session["deleted_at"] is not None else "active",
                            "deleted" if action == "soft_delete" else "active",
                            epoch,
                            canonical_digest_v1({"session_id": session_id}),
                            actor_audit_ref,
                            now,
                            now,
                        ),
                    )
                except aiosqlite.IntegrityError as exc:
                    raise SessionLifecycleError("session_lifecycle_conflict") from exc
                async with connection.execute(
                    "SELECT * FROM session_lifecycle_operations WHERE operation_id=?",
                    (operation_id,),
                ) as cursor:
                    inserted = await cursor.fetchone()
                assert inserted is not None
                result = await self._prepared_from_row(connection, inserted)
                await connection.commit()
                return result
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def _read_request_operation(
        self,
        connection: aiosqlite.Connection,
        *,
        session_id: str,
        actor_audit_ref: str,
        request_id: str,
        action: str,
    ) -> aiosqlite.Row | None:
        async with connection.execute(
            """
            SELECT * FROM session_lifecycle_operations
            WHERE session_audit_ref=? AND actor_audit_ref=? AND request_id=? AND action=?
            """,
            (session_id, actor_audit_ref, request_id, action),
        ) as cursor:
            return await cursor.fetchone()

    async def _prepared_from_row(
        self,
        connection: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> _PreparedLifecycle:
        async with connection.execute(
            """
            SELECT t.id AS turn_id,t.current_generation,t.delivery_fence_epoch,
                   g.row_version,g.terminal_candidate_id,g.state
            FROM turns t JOIN turn_generations g
              ON g.turn_id=t.id AND g.generation=t.current_generation
            WHERE t.session_id=? AND t.terminal_protocol_version=1
              AND g.protocol_version=1 AND g.state!='closed'
            ORDER BY t.seq DESC LIMIT 1
            """,
            (row["session_audit_ref"],),
        ) as cursor:
            generation = await cursor.fetchone()
        scope = None
        if generation is not None:
            scope = TerminalScope(
                team_id=str(row["team_id"]),
                session_id=str(row["session_audit_ref"]),
                turn_id=str(generation["turn_id"]),
                generation=int(generation["current_generation"]),
            )
        return _PreparedLifecycle(
            operation_id=str(row["operation_id"]),
            team_id=str(row["team_id"]),
            session_id=str(row["session_audit_ref"]),
            action=str(row["action"]),  # type: ignore[arg-type]
            actor_audit_ref=str(row["actor_audit_ref"]),
            request_id=str(row["request_id"]),
            state=str(row["state"]),
            result_code=str(row["result_code"]) if row["result_code"] else None,
            expected_deleted_state=str(row["expected_deleted_state"]),
            expected_visibility_epoch=int(row["expected_session_visibility_epoch"]),
            open_scope=scope,
            generation_row_version=(int(generation["row_version"]) if generation is not None else None),
            candidate_id=(str(generation["terminal_candidate_id"]) if generation is not None and generation["terminal_candidate_id"] else None),
            delivery_fence_epoch=(int(generation["delivery_fence_epoch"]) if generation is not None else None),
        )

    @staticmethod
    def _committed_result(
        prepared: _PreparedLifecycle,
        *,
        protocol_version: int,
    ) -> SessionLifecycleResult | None:
        if prepared.state != "committed":
            return None
        code = prepared.result_code or "committed"
        return SessionLifecycleResult(
            operation_id=prepared.operation_id,
            action=prepared.action,
            changed=code in {"deleted", "revived"},
            protocol_version=protocol_version,
            result_code=code,
        )

    async def _mark_fenced(self, operation_id: str) -> None:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    UPDATE session_lifecycle_operations
                    SET state='fenced',row_version=row_version+1,updated_at=?
                    WHERE operation_id=? AND state='prepared'
                    """,
                    (_now(), operation_id),
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def _same_generation_has_committed_receipt(
        self,
        scope: TerminalScope,
    ) -> bool:
        """Recognize the one safe prepare-vs-finalize race.

        A lifecycle prepare can observe an open generation immediately before
        the worker commits its terminal receipt.  Protocol delete must reject
        that now-closed generation, but Session deletion may continue because
        the exact generation has durable terminal truth.  Every other fence or
        shape change remains fail-closed.
        """

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT g.state,g.terminal_receipt_id,t.current_terminal_receipt_id,
                       r.receipt_id,r.protocol_version
                FROM turns t
                JOIN turn_generations g
                  ON g.turn_id=t.id AND g.generation=?
                LEFT JOIN turn_terminal_receipts r
                  ON r.turn_id=t.id AND r.generation=g.generation
                WHERE t.id=? AND t.session_id=?
                  AND t.terminal_protocol_version=1 AND g.protocol_version=1
                """,
                (scope.generation, scope.turn_id, scope.session_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return False
        receipt_id = row["receipt_id"]
        return bool(
            row["state"] == "closed"
            and receipt_id is not None
            and row["protocol_version"] == 1
            and row["terminal_receipt_id"] == receipt_id
            and row["current_terminal_receipt_id"] == receipt_id
        )

    async def _assert_revivable(self, prepared: _PreparedLifecycle) -> None:
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT
                  EXISTS(
                    SELECT 1 FROM turns t JOIN turn_generations g
                      ON g.turn_id=t.id AND g.generation=t.current_generation
                    WHERE t.session_id=? AND t.terminal_protocol_version=1
                      AND g.state!='closed'
                  ),
                  EXISTS(
                    SELECT 1 FROM sessions s
                    WHERE s.id=? AND s.recovery_lock_turn_id IS NOT NULL
                  ),
                  EXISTS(
                    SELECT 1 FROM turn_effect_attempts e JOIN turns t ON t.id=e.turn_id
                    WHERE t.session_id=? AND e.state!='settled'
                  ),
                  EXISTS(
                    SELECT 1 FROM turn_delivery_intents d
                    JOIN turn_terminal_receipts r ON r.receipt_id=d.receipt_id
                    JOIN turns t ON t.id=r.turn_id
                    WHERE t.session_id=? AND d.status IN ('sending','unknown')
                  )
            """,
                (
                    prepared.session_id,
                    prepared.session_id,
                    prepared.session_id,
                    prepared.session_id,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None or any(bool(value) for value in row):
            raise SessionLifecycleError("session_revive_blocked")

    async def _commit_operation(
        self,
        prepared: _PreparedLifecycle,
        *,
        result_code: str,
        changed: bool,
        linked_canvas_id: str | None = None,
    ) -> SessionLifecycleResult:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    "SELECT * FROM session_lifecycle_operations WHERE operation_id=?",
                    (prepared.operation_id,),
                ) as cursor:
                    operation = await cursor.fetchone()
                if operation is None:
                    raise SessionLifecycleError("session_lifecycle_operation_missing")
                if operation["state"] == "committed":
                    await connection.rollback()
                    replay = self._committed_result(
                        await self._prepared_from_row(connection, operation),
                        protocol_version=1,
                    )
                    assert replay is not None
                    return replay
                if operation["state"] not in {"prepared", "fenced"}:
                    raise SessionLifecycleError("session_lifecycle_conflict")
                async with connection.execute(
                    "SELECT deleted_at FROM sessions WHERE id=?",
                    (prepared.session_id,),
                ) as cursor:
                    session = await cursor.fetchone()
                if session is None:
                    raise SessionLifecycleError("session_not_found")
                current_deleted_state = "deleted" if session["deleted_at"] is not None else "active"
                if current_deleted_state != prepared.expected_deleted_state:
                    raise SessionLifecycleError("session_lifecycle_precondition_changed")
                async with connection.execute(
                    """
                    SELECT epoch FROM terminal_auth_scope_epochs
                    WHERE scope_kind='session_visibility' AND team_id=? AND scope_ref=?
                    """,
                    (prepared.team_id, prepared.session_id),
                ) as cursor:
                    before_epoch = await cursor.fetchone()
                if before_epoch is None or int(before_epoch["epoch"]) != prepared.expected_visibility_epoch:
                    raise SessionLifecycleError("session_visibility_epoch_changed")
                if linked_canvas_id is not None:
                    if self._canvases is None:
                        raise SessionLifecycleError("canvas_lifecycle_store_missing")
                    deleted_canvas = await self._canvases.delete_canvas_in_transaction(
                        connection,
                        canvas_id=linked_canvas_id,
                        expected_session_id=prepared.session_id,
                        expected_user_id=prepared.actor_audit_ref,
                    )
                    if not deleted_canvas:
                        raise SessionLifecycleError("linked_canvas_not_found")
                if changed and prepared.action == "soft_delete":
                    open_count = await self._open_generation_count(connection, prepared.session_id)
                    if open_count:
                        raise SessionLifecycleError("session_generation_not_fenced")
                    session_update = await connection.execute(
                        """
                        UPDATE sessions SET deleted_at=?,
                          active_turn_id=NULL,active_turn_protocol_version=NULL,
                          active_turn_generation=NULL,active_turn_claim_epoch=NULL
                        WHERE id=? AND deleted_at IS NULL
                        """,
                        (_now(), prepared.session_id),
                    )
                elif changed and prepared.action == "revive":
                    if await self._open_generation_count(connection, prepared.session_id):
                        raise SessionLifecycleError("session_revive_blocked")
                    if await self._unresolved_revival_count(connection, prepared.session_id):
                        raise SessionLifecycleError("session_revive_blocked")
                    session_update = await connection.execute(
                        "UPDATE sessions SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
                        (prepared.session_id,),
                    )
                else:
                    session_update = None
                if changed and (session_update is None or session_update.rowcount != 1):
                    raise SessionLifecycleError("session_lifecycle_precondition_changed")
                if changed:
                    now = _now()
                    await connection.execute(
                        """
                        UPDATE turns
                        SET delivery_fence_epoch=delivery_fence_epoch+1,delivery_fenced_at=?
                        WHERE session_id=? AND terminal_protocol_version=1
                        """,
                        (now, prepared.session_id),
                    )
                    await bump_terminal_auth_epoch(
                        connection,
                        scope_kind=AuthFenceScopeKind.SESSION_VISIBILITY,
                        team_id=prepared.team_id,
                        scope_ref=prepared.session_id,
                        now=now,
                    )
                async with connection.execute(
                    """
                    SELECT epoch FROM terminal_auth_scope_epochs
                    WHERE scope_kind='session_visibility' AND team_id=? AND scope_ref=?
                    """,
                    (prepared.team_id, prepared.session_id),
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                if epoch_row is None:
                    raise SessionLifecycleError("session_visibility_epoch_missing")
                committed_at = _now()
                update = await connection.execute(
                    """
                    UPDATE session_lifecycle_operations
                    SET state='committed',new_session_visibility_epoch=?,result_code=?,
                        row_version=row_version+1,updated_at=?,committed_at=?
                    WHERE operation_id=? AND state IN ('prepared','fenced')
                    """,
                    (
                        int(epoch_row["epoch"]),
                        result_code,
                        committed_at,
                        committed_at,
                        prepared.operation_id,
                    ),
                )
                if update.rowcount != 1:
                    raise SessionLifecycleError("session_lifecycle_commit_conflict")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return SessionLifecycleResult(
            operation_id=prepared.operation_id,
            action=prepared.action,
            changed=changed,
            protocol_version=1,
            result_code=result_code,
        )

    @staticmethod
    async def _open_generation_count(
        connection: aiosqlite.Connection,
        session_id: str,
    ) -> int:
        async with connection.execute(
            """
            SELECT COUNT(*) FROM turns t JOIN turn_generations g
              ON g.turn_id=t.id AND g.generation=t.current_generation
            WHERE t.session_id=? AND t.terminal_protocol_version=1 AND g.state!='closed'
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _unresolved_revival_count(
        connection: aiosqlite.Connection,
        session_id: str,
    ) -> int:
        async with connection.execute(
            """
            SELECT
              EXISTS(
                SELECT 1 FROM sessions s
                WHERE s.id=? AND s.recovery_lock_turn_id IS NOT NULL
              ) +
              EXISTS(
                SELECT 1 FROM turn_effect_attempts e JOIN turns t ON t.id=e.turn_id
                WHERE t.session_id=? AND e.state!='settled'
              ) +
              EXISTS(
                SELECT 1 FROM turn_delivery_intents d
                JOIN turn_terminal_receipts r ON r.receipt_id=d.receipt_id
                JOIN turns t ON t.id=r.turn_id
                WHERE t.session_id=? AND d.status IN ('sending','unknown')
              )
            """,
            (session_id, session_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "SESSION_LIFECYCLE_ERROR_CODES",
    "SessionLifecycleCoordinator",
    "SessionLifecycleError",
    "SessionLifecycleResult",
]
