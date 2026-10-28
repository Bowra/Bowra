"""Transaction-only storage for reference inputs and captured material.

These primitives are not an admission service or an executable resource grant.
The reference HTTP writer stays closed. Material readers return encrypted bytes;
current per-resource authorization must precede opening them in a future resolver.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    ContentHash,
    Identity,
    InputDocument,
    NodeId,
    ReferenceNode,
    ResourceRef,
    SkillResourceRef,
    _bounded_json,
    decode_reference_json,
)
from bowhead_core.chat_turn_source import ChatTurnUserSource, chat_turn_user_source_projection
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

MAX_REFERENCE_STORAGE_BYTES = 1024 * 1024
MAX_REFERENCE_RETENTION_DAYS = 180


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("reference_timestamp_requires_timezone")
    return parsed.astimezone(UTC)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value: object) -> object:
        _bounded_json(value, max_depth=20, max_bytes=MAX_REFERENCE_STORAGE_BYTES)
        if "schema_version" in cls.model_fields and (not isinstance(value, dict) or type(value.get("schema_version")) is not int):
            raise ValueError("reference_schema_version_required")
        return value


class ReferenceScope(_Record):
    team_id: Identity
    user_id: Identity
    workspace_id: Identity
    session_id: Identity
    policy_epoch: int = Field(ge=0)

    def identity(self) -> dict[str, str]:
        return self.model_dump(exclude={"policy_epoch"})


class ReferenceAdmission(_Record):
    node_id: NodeId
    resource: ResourceRef
    operation: Literal["delegate", "read", "invoke", "execute", "expand", "provide"]
    policy_digest: ContentHash

    @model_validator(mode="after")
    def operation_matches(self) -> Self:
        supported = {
            "agent": {"delegate"},
            "skill": {"read", "invoke"},
            "capability": {"execute"},
            "mcp_tool": {"invoke"},
            "mcp_prompt": {"expand"},
            "mcp_source": {"provide"},
        }
        if self.operation not in supported[self.resource.kind]:
            raise ValueError("reference_operation_mismatch")
        return self


def supports_lead_reference_resources(resources: Iterable[ResourceRef]) -> bool:
    """Check the persisted Lead resource shape, without granting access or selecting a receiver."""
    resources = tuple(resources)
    agents = sum(resource.kind == "agent" for resource in resources)
    sources = sum(resource.kind == "mcp_source" for resource in resources)
    tools = sum(resource.kind == "mcp_tool" for resource in resources)
    if sources or tools:
        return agents == 1 and sources <= 3 and tools <= 3 and len(resources) == agents + sources + tools
    prompts = sum(resource.kind == "mcp_prompt" for resource in resources)
    if prompts:
        return agents == 1 and prompts <= 3 and len(resources) == agents + prompts
    skills = sum(resource.kind == "skill" for resource in resources)
    capabilities = [resource for resource in resources if isinstance(resource, CapabilityResourceRef)]
    return (
        bool(resources)
        and agents <= 1
        and skills <= 3
        and len(capabilities) <= 1
        and len(resources) == agents + skills + len(capabilities)
        and all(resource.id == "cap_visualize" and resource.version == 1 for resource in capabilities)
    )


def supports_lead_reference_receiver(receiver: AgentResourceRef | None, resources: Iterable[ResourceRef]) -> bool:
    """Share receiver scope across admission and persisted queue decoding."""
    resources = tuple(resources)
    if receiver is None or not supports_lead_reference_resources(resources):
        return False
    return receiver.source_kind == "team" or (
        receiver.source_kind == "system" and receiver in resources and all(resource.kind in {"agent", "skill"} for resource in resources)
    )


class ReferencePlan(_Record):
    schema_version: Literal[1]
    orchestration_contract: Literal["lead-delegation.v1"] | None = None
    source_turn_id: Identity
    logical_source_turn_id: Identity
    snapshot_owner_turn_id: Identity
    source_hash: ContentHash
    admissions: Annotated[list[ReferenceAdmission], Field(max_length=10)]

    @model_validator(mode="after")
    def supported_orchestration(self) -> Self:
        if self.orchestration_contract is not None and not supports_lead_reference_resources(item.resource for item in self.admissions):
            raise ValueError("reference_lead_contract_unsupported")
        return self


class QueuedReferenceInput(_Record):
    schema_version: Literal[1]
    # The accepted execution contract must not follow later rollout changes.
    orchestration_contract: Literal["lead-delegation.v1"] | None = None
    input_document: InputDocument
    # A Skill/Capability-only submission keeps the user's source free of a synthetic @Agent.
    # The server-selected receiver is nevertheless part of the immutable queued
    # execution intent, so materialization cannot silently choose a different
    # Agent after waiting behind the active Turn.
    automatic_receiver: AgentResourceRef | None = None
    attachment_ids: Annotated[list[Identity], Field(max_length=100)]
    # Same stored wire as ChatTurnUserSource; attachment authorization is external.
    attachment_refs: Annotated[list[dict[str, JsonValue]], Field(max_length=32)]

    @model_validator(mode="after")
    def valid_task(self) -> Self:
        self.input_document.validate_task(has_attachments=bool(self.attachment_ids))
        references = [node for node in self.input_document.nodes if isinstance(node, ReferenceNode)]
        explicit_receivers = [node for node in references if isinstance(node.resource, AgentResourceRef)]
        if self.orchestration_contract is not None:
            from bowhead_core.chat_reference_attachment_snapshot import supports_lead_attachment_refs

            receiver = explicit_receivers[0].resource if len(explicit_receivers) == 1 else self.automatic_receiver
            if not supports_lead_reference_receiver(receiver, (node.resource for node in references)) or not supports_lead_attachment_refs(
                self.attachment_refs, self.attachment_ids
            ):
                raise ValueError("reference_lead_contract_unsupported")
        if self.automatic_receiver is not None:
            skills = [node for node in references if isinstance(node.resource, SkillResourceRef)]
            capabilities = [node for node in references if isinstance(node.resource, CapabilityResourceRef)]
            if (
                explicit_receivers
                or len(skills) > 3
                or len(capabilities) > 1
                or not (skills or capabilities)
                or len(references) != len(skills) + len(capabilities)
            ):
                raise ValueError("reference_automatic_receiver_invalid")
        ids: list[str] = []
        for ref in self.attachment_refs:
            children = ref.get("bundleArtifactIds") or [ref.get("fileId")]
            if not isinstance(children, list) or any(type(item) is not str or not item for item in children):
                raise ValueError("reference_attachment_identity_invalid")
            ids.extend(children)
        if self.attachment_refs and list(dict.fromkeys(ids)) != self.attachment_ids:
            raise ValueError("reference_attachment_identity_mismatch")
        return self


@dataclass(frozen=True, slots=True)
class QueuedReferenceRecovery:
    """Maintenance-only view of one accepted queue record.

    ``payload`` is present only while the immutable encrypted source is still
    retained.  This record is not an authorization result and must never be
    passed to an execution path.
    """

    scope: ReferenceScope
    submission_id: str
    accepted_parent_turn_id: str
    payload_hash: str
    payload: QueuedReferenceInput | None
    state: Literal["queued", "materialized", "cancelled"]
    cancel_reason: Literal["user_cancelled", "source_deleted", "retention_expired"] | None
    row_version: int


class ContentDescriptor(_Record):
    schema_version: Literal[1]
    content_key: Identity
    node_id: NodeId | None = None
    receiver_source: Literal["lead"] | None = None
    resource: ResourceRef
    content_kind: Literal["skill_read", "skill_read_closure", "skill_baseline", "skill_extension", "prompt_result", "delegation_task"]
    materialization_hash: ContentHash
    content_hash: ContentHash
    byte_count: int = Field(ge=0, le=MAX_REFERENCE_STORAGE_BYTES)

    @model_validator(mode="after")
    def supported_material(self) -> Self:
        if (self.receiver_source == "lead") != (self.node_id is None) or (
            self.receiver_source == "lead" and self.content_kind not in {"skill_baseline", "skill_extension", "delegation_task"}
        ):
            raise ValueError("reference_content_receiver_origin")
        if (self.content_kind, self.resource.kind) not in {
            ("skill_read", "skill"),
            ("skill_read_closure", "skill"),
            ("skill_baseline", "agent"),
            ("skill_extension", "agent"),
            ("prompt_result", "mcp_prompt"),
            ("delegation_task", "agent"),
        }:
            raise ValueError("reference_content_kind_mismatch")
        return self

    def matches_admission(self, item: ReferenceAdmission) -> bool:
        operations = {"delegate"} if self.content_kind in {"skill_baseline", "skill_extension", "delegation_task"} else {"read", "expand"}
        return item.node_id == self.node_id and item.resource == self.resource and item.operation in operations


async def _content_is_admitted(
    db: aiosqlite.Connection, scope: ReferenceScope, turn_id: str, descriptor: ContentDescriptor, plan: ReferencePlan
) -> bool:
    if descriptor.receiver_source is None:
        return any(descriptor.matches_admission(item) for item in plan.admissions)
    # Automatic baseline material belongs to the exact primary root, never to a
    # fabricated user node. The canonical receiver and worker still authorize use.
    root = await _row(
        db,
        "SELECT * FROM turn_agent_binding WHERE turn_id=? AND team_id=? AND user_id=? AND binding_source='lead' AND EXISTS (SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_automatic_material')",
        (turn_id, scope.team_id, scope.user_id),
    )
    resource = descriptor.resource
    return bool(
        root is not None
        and root["primary_execution_id"]
        and (root["requested_agent_id"], root["agent_source_kind"], root["revision_ref"], root["release_id"], root["assignment_id"])
        == (resource.id, resource.source_kind, resource.version, resource.release_id, resource.assignment_id)
        and 1 <= len(plan.admissions) <= 4
        and sum(item.resource.kind == "skill" for item in plan.admissions) <= 3
        and sum(item.resource.kind == "capability" for item in plan.admissions) <= 1
        and all(
            (item.resource.kind == "skill" and item.operation == "read") or (item.resource.kind == "capability" and item.operation == "execute")
            for item in plan.admissions
        )
    )


class ReferenceCipher:
    """Purpose- and scope-bound Fernet envelope, without a plaintext fallback.

    Construct outside transactions using the existing instance Fernet root.
    Opening an envelope proves integrity only, never current resource permission.
    """

    def __init__(self, fernet_root: str) -> None:
        try:
            if not isinstance(fernet_root, str):
                raise ValueError("invalid root type")
            root = base64.b64decode(fernet_root.encode("ascii"), altchars=b"-_", validate=True)
            if len(root) != 32:
                raise ValueError("invalid root length")
        except (ValueError, UnicodeError) as exc:
            raise ValueError("reference_encryption_unavailable") from exc
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bowhead/chat-reference-storage/v1").derive(root)
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, *, scope: ReferenceScope, purpose: str, identity: str, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise ValueError("reference_content_requires_bytes")
        if len(content) > MAX_REFERENCE_STORAGE_BYTES:
            raise ValueError("reference_content_too_large")
        envelope = {
            "schema_version": 1,
            "scope": scope.identity(),
            "purpose": purpose,
            "identity": identity,
            "content": base64.b64encode(content).decode(),
        }
        return self._fernet.encrypt(_canonical(envelope)).decode()

    def open(self, *, scope: ReferenceScope, purpose: str, identity: str, ciphertext: str) -> bytes:
        if not isinstance(ciphertext, str) or len(ciphertext) > 2 * MAX_REFERENCE_STORAGE_BYTES:
            raise ValueError("reference_ciphertext_invalid")
        try:
            raw = self._fernet.decrypt(ciphertext.encode("ascii"))
            envelope = decode_reference_json(raw, max_bytes=2 * MAX_REFERENCE_STORAGE_BYTES)
            if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "scope", "purpose", "identity", "content"}:
                raise ValueError("invalid envelope")
            if (
                type(envelope["schema_version"]) is not int
                or envelope["schema_version"] != 1
                or envelope["scope"] != scope.identity()
                or envelope["purpose"] != purpose
                or envelope["identity"] != identity
            ):
                raise ValueError("envelope binding mismatch")
            content = base64.b64decode(envelope["content"], validate=True)
            if len(content) > MAX_REFERENCE_STORAGE_BYTES:
                raise ValueError("content too large")
            return content
        except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("reference_ciphertext_invalid") from exc


def reference_source_hash(source: ChatTurnUserSource) -> str:
    return _digest(_canonical(chat_turn_user_source_projection(source)))


def _validate_plan_source(plan: ReferencePlan, source: ChatTurnUserSource | None, scope: ReferenceScope) -> None:
    if source is None or source.session_id != scope.session_id or source.input_document is None or reference_source_hash(source) != plan.source_hash:
        raise ValueError("reference_source_changed")
    selected = [
        (node.node_id, node.resource.model_dump(exclude_none=True)) for node in source.input_document.nodes if isinstance(node, ReferenceNode)
    ]
    admitted = [(item.node_id, item.resource.model_dump(exclude_none=True)) for item in plan.admissions]
    if selected != admitted:
        raise ValueError("reference_admission_source_mismatch")


def _snapshot[T: BaseModel](value: T) -> T:
    # Python projection preserves malformed mutated types for strict validation.
    return type(value).model_validate(value.model_dump(mode="python", exclude_none=True, warnings=False))


def _transaction(connection: aiosqlite.Connection) -> None:
    if not connection.in_transaction:
        raise ValueError("reference_transaction_required")


async def _row(connection: aiosqlite.Connection, sql: str, args: tuple[object, ...]) -> aiosqlite.Row | None:
    async with connection.execute(sql, args) as cursor:
        cursor.row_factory = aiosqlite.Row
        return await cursor.fetchone()


async def get_cancelled_queued_input_for_recovery_in_connection(
    connection: aiosqlite.Connection,
    *,
    session_id: str,
    accepted_parent_turn_id: str,
    submission_id: str,
) -> QueuedReferenceRecovery | None:
    """Read a durable queue cancellation without requiring decryption keys."""
    _transaction(connection)
    row = await _row(
        connection,
        """
        SELECT q.*,COALESCE(p.epoch,0) AS policy_epoch
        FROM chat_reference_queued_inputs q
        JOIN sessions s ON s.id=q.session_id
        JOIN workspaces w ON w.id=q.workspace_id AND w.id=s.workspace_id
        LEFT JOIN access_policy_state p ON p.team_id=q.team_id
        WHERE q.session_id=? AND q.accepted_parent_turn_id=?
          AND q.client_submission_id=? AND q.user_id=s.user_id
          AND q.team_id=w.team_id AND q.state='cancelled'
        """,
        (session_id, accepted_parent_turn_id, submission_id),
    )
    if row is None:
        return None
    cancel_reason = row["cancel_reason"]
    if cancel_reason not in {"user_cancelled", "source_deleted", "retention_expired"}:
        raise ValueError("reference_queued_input_changed")
    return QueuedReferenceRecovery(
        scope=ReferenceScope(
            team_id=str(row["team_id"]),
            user_id=str(row["user_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            policy_epoch=int(row["policy_epoch"]),
        ),
        submission_id=submission_id,
        accepted_parent_turn_id=accepted_parent_turn_id,
        payload_hash=str(row["payload_hash"]),
        payload=None,
        state="cancelled",
        cancel_reason=cancel_reason,
        row_version=int(row["row_version"]),
    )


async def _scope(connection: aiosqlite.Connection, scope: ReferenceScope, *, write: bool) -> None:
    row = await _row(
        connection,
        """
        SELECT w.status AS workspace_status,s.deleted_at,COALESCE(p.epoch,0) AS policy_epoch
        FROM sessions s JOIN workspaces w ON w.id=s.workspace_id
        JOIN users u ON u.id=s.user_id AND u.status='active'
        JOIN teams t ON t.id=w.team_id AND t.status='active'
        JOIN team_members tm ON tm.team_id=t.id AND tm.user_id=u.id AND tm.status='active'
        JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=u.id AND wm.status='active'
        LEFT JOIN access_policy_state p ON p.team_id=t.id
        WHERE s.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=?
    """,
        (scope.session_id, scope.user_id, scope.workspace_id, scope.team_id),
    )
    if row is None or row["deleted_at"] is not None or row["workspace_status"] not in ({"active"} if write else {"active", "archived"}):
        raise ValueError("reference_scope_unavailable")
    if row["policy_epoch"] != scope.policy_epoch:
        raise ValueError("reference_policy_changed")


async def _turn(connection: aiosqlite.Connection, scope: ReferenceScope, turn_id: str) -> aiosqlite.Row:
    row = await _row(connection, "SELECT * FROM turns WHERE id=? AND session_id=?", (turn_id, scope.session_id))
    if row is None:
        raise ValueError("reference_turn_unavailable")
    return row


async def _require_queue_parent_material(connection: aiosqlite.Connection, turn_id: str) -> None:
    # A plain-text parent need not have a reference plan. If it does, a queue
    # cannot use its own later expiry to outlive deleted/expired parent material.
    from bowhead_core.chat_reference_retention import reference_material_available_in_connection

    row = await _row(connection, "SELECT 1 FROM chat_reference_plans WHERE turn_id=?", (turn_id,))
    if row is not None and not await reference_material_available_in_connection(connection, turn_id=turn_id, now=_now()):
        raise ValueError("reference_source_unavailable")


async def _fence(connection: aiosqlite.Connection, scope: ReferenceScope, turn_id: str, generation: int, owner_id: str) -> None:
    row = await _row(
        connection,
        """
        SELECT d.owner_id,d.lease_expires_at,d.lease_execution_generation,t.execution_generation,t.status,d.state
        FROM chat_turn_spawn_dispatch d JOIN turns t ON t.id=d.turn_id
        WHERE d.turn_id=? AND d.session_id=? AND t.session_id=?
    """,
        (turn_id, scope.session_id, scope.session_id),
    )
    if (
        row is None
        or row["owner_id"] != owner_id
        or row["execution_generation"] != generation
        or row["lease_execution_generation"] != generation
        or row["status"] != "running"
        or row["state"] not in {"claimed", "spawned"}
        or _time(row["lease_expires_at"]) <= _now()
    ):
        raise ValueError("reference_execution_fence_lost")


class ChatReferenceStore:
    def __init__(self, cipher: ReferenceCipher) -> None:
        self._cipher = cipher

    async def bi_pause_recoverable_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        turn_id: str,
    ) -> bool:
        from bowhead_core.chat_reference_bi_continuation import pause_recoverable

        return await pause_recoverable(
            connection,
            turn_id=turn_id,
            cipher=self._cipher,
        )

    @staticmethod
    async def put_plan_in_connection(
        connection: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str, plan: ReferencePlan, expires_at: str
    ) -> bool:
        from bowhead_core.reference_retention_policy import read_retention_policy_in_connection

        _transaction(connection)
        scope, plan = _snapshot(scope), _snapshot(plan)
        encoded = _canonical(plan.model_dump(mode="json", exclude_none=True)).decode()
        await _scope(connection, scope, write=True)
        await _turn(connection, scope, turn_id)
        # Only initial sources have a proved lineage in this storage increment.
        # Future replay/Plan adapters must establish an explicit continuation edge.
        if {plan.source_turn_id, plan.logical_source_turn_id, plan.snapshot_owner_turn_id} != {turn_id}:
            raise ValueError("reference_lineage_unavailable")
        source = await _read_chat_turn_user_source_in_tx(
            connection, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
        )
        _validate_plan_source(plan, source, scope)
        existing = await _row(connection, "SELECT * FROM chat_reference_plans WHERE turn_id=?", (turn_id,))
        if existing is not None:
            if existing["plan_json"] != encoded or existing["expires_at"] != expires_at:
                raise ValueError("reference_plan_conflict")
            await _require_queue_parent_material(connection, turn_id)
            return False
        deadline = _time(expires_at)
        if not _now() < deadline <= _now() + timedelta(days=MAX_REFERENCE_RETENTION_DAYS):
            raise ValueError("reference_retention_invalid")
        policy = await read_retention_policy_in_connection(connection, team_id=scope.team_id)
        await connection.execute(
            """
            INSERT INTO chat_reference_plans(turn_id,schema_version,source_turn_id,logical_source_turn_id,snapshot_owner_turn_id,
                source_hash,plan_json,created_policy_epoch,retention_state,created_at,expires_at,retention_policy_version)
            VALUES (?,1,?,?,?,?,?,?,'available',?,?,?)
        """,
            (
                turn_id,
                plan.source_turn_id,
                plan.logical_source_turn_id,
                plan.snapshot_owner_turn_id,
                plan.source_hash,
                encoded,
                scope.policy_epoch,
                _now().isoformat(),
                expires_at,
                policy.version,
            ),
        )
        return True

    @staticmethod
    async def get_plan_in_connection(connection: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str) -> ReferencePlan | None:
        _transaction(connection)
        scope = _snapshot(scope)
        await _scope(connection, scope, write=False)
        await _turn(connection, scope, turn_id)
        row = await _row(connection, "SELECT * FROM chat_reference_plans WHERE turn_id=?", (turn_id,))
        if row is None:
            return None
        await _require_queue_parent_material(connection, turn_id)
        plan = ReferencePlan.model_validate(decode_reference_json(row["plan_json"], max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20))
        if {plan.source_turn_id, plan.logical_source_turn_id, plan.snapshot_owner_turn_id} != {turn_id}:
            raise ValueError("reference_lineage_unavailable")
        source = await _read_chat_turn_user_source_in_tx(
            connection, turn_id=plan.source_turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
        )
        _validate_plan_source(plan, source, scope)
        return plan

    async def put_content_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        scope: ReferenceScope,
        turn_id: str,
        descriptor: ContentDescriptor,
        content: bytes,
        generation: int,
        owner_id: str,
        expires_at: str,
    ) -> bool:
        _transaction(connection)
        scope, descriptor = _snapshot(scope), _snapshot(descriptor)
        if not isinstance(content, bytes):
            raise ValueError("reference_content_requires_bytes")
        if descriptor.byte_count != len(content) or descriptor.content_hash != _digest(content):
            raise ValueError("reference_content_changed")
        content.decode("utf-8", errors="strict")
        encoded = _canonical(descriptor.model_dump(mode="json", exclude_none=True)).decode()
        await _scope(connection, scope, write=True)
        await _fence(connection, scope, turn_id, generation, owner_id)
        plan = await self.get_plan_in_connection(connection, scope=scope, turn_id=turn_id)
        if plan is None or not await _content_is_admitted(connection, scope, turn_id, descriptor, plan):
            raise ValueError("reference_content_not_admitted")
        plan_row = await _row(connection, "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (turn_id,))
        if not _now() < _time(expires_at) <= _time(plan_row["expires_at"]):
            raise ValueError("reference_retention_invalid")
        existing = await _row(
            connection, "SELECT * FROM chat_reference_content_snapshots WHERE turn_id=? AND content_key=?", (turn_id, descriptor.content_key)
        )
        if existing is not None:
            if existing["descriptor_json"] != encoded or existing["expires_at"] != expires_at:
                raise ValueError("reference_content_conflict")
            if existing["retention_state"] != "available":
                raise ValueError("reference_content_unavailable")
            original = self._cipher.open(
                scope=scope, purpose="content", identity=f"{turn_id}/{descriptor.content_key}", ciphertext=existing["ciphertext"]
            )
            if original != content:
                raise ValueError("reference_content_changed")
            return False
        ciphertext = self._cipher.seal(scope=scope, purpose="content", identity=f"{turn_id}/{descriptor.content_key}", content=content)
        await connection.execute(
            """
            INSERT INTO chat_reference_content_snapshots(turn_id,content_key,schema_version,descriptor_json,content_hash,byte_count,
                ciphertext,retention_state,created_generation,created_policy_epoch,created_at,expires_at)
            VALUES (?,?,1,?,?,?,?,'available',?,?,?,?)
        """,
            (
                turn_id,
                descriptor.content_key,
                encoded,
                descriptor.content_hash,
                len(content),
                ciphertext,
                generation,
                scope.policy_epoch,
                _now().isoformat(),
                expires_at,
            ),
        )
        return True

    async def get_encrypted_content_in_connection(
        self, connection: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str, content_key: str
    ) -> tuple[ContentDescriptor, str] | None:
        """Return a scoped sealed record; this is intentionally not a plaintext read API."""
        _transaction(connection)
        scope = _snapshot(scope)
        plan = await self.get_plan_in_connection(connection, scope=scope, turn_id=turn_id)
        if plan is None:
            return None
        row = await _row(connection, "SELECT * FROM chat_reference_content_snapshots WHERE turn_id=? AND content_key=?", (turn_id, content_key))
        if row is None:
            return None
        if row["retention_state"] != "available" or _time(row["expires_at"]) <= _now():
            raise ValueError("reference_content_unavailable")
        descriptor = ContentDescriptor.model_validate(decode_reference_json(row["descriptor_json"]))
        if not await _content_is_admitted(connection, scope, turn_id, descriptor, plan):
            raise ValueError("reference_content_not_admitted")
        return descriptor, row["ciphertext"]

    async def put_queued_input_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        scope: ReferenceScope,
        submission_id: str,
        accepted_parent_turn_id: str,
        accepted_generation: int,
        payload: QueuedReferenceInput,
        expires_at: str,
    ) -> bool:
        """Store one accepted source; the caller owns receipt and buffer-index atomicity.

        SessionStore's internal reference-steer writer now supplies that shared
        transaction. Gateway admission and segmented materialization stay closed.
        """
        from bowhead_core.reference_retention_policy import read_retention_policy_in_connection

        _transaction(connection)
        scope, payload = _snapshot(scope), _snapshot(payload)
        raw = _canonical(payload.model_dump(mode="json", exclude_none=True))
        payload_hash = _digest(raw)
        await _scope(connection, scope, write=True)
        parent = await _turn(connection, scope, accepted_parent_turn_id)
        await _require_queue_parent_material(connection, accepted_parent_turn_id)
        existing = await _row(
            connection,
            "SELECT * FROM chat_reference_queued_inputs WHERE team_id=? AND user_id=? AND client_submission_id=?",
            (scope.team_id, scope.user_id, submission_id),
        )
        if existing is not None:
            if (
                existing["session_id"],
                existing["workspace_id"],
                existing["accepted_parent_turn_id"],
                existing["accepted_generation"],
                existing["payload_hash"],
                existing["expires_at"],
            ) != (scope.session_id, scope.workspace_id, accepted_parent_turn_id, accepted_generation, payload_hash, expires_at):
                raise ValueError("reference_queued_input_conflict")
            return False
        active = await _row(connection, "SELECT active_turn_id FROM sessions WHERE id=?", (scope.session_id,))
        if (
            parent["execution_generation"] != accepted_generation
            or parent["status"] != "running"
            or active["active_turn_id"] != accepted_parent_turn_id
        ):
            raise ValueError("reference_parent_changed")
        receipt = await _row(
            connection,
            "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
            (scope.team_id, scope.user_id, submission_id),
        )
        if (
            receipt is None
            or (
                receipt["workspace_id"],
                receipt["requested_session_id"],
                receipt["actual_session_id"],
                receipt["turn_id"],
                receipt["submission_kind"],
                receipt["submission_contract_version"],
                receipt["model_control_batch"],
                receipt["outcome"],
            )
            != (
                scope.workspace_id,
                scope.session_id,
                scope.session_id,
                accepted_parent_turn_id,
                "steer",
                "chat-references.v1",
                "references",
                "buffered",
            )
            or _time(receipt["expires_at"]) <= _now()
        ):
            raise ValueError("reference_receipt_required")
        if not _now() < _time(expires_at) <= _now() + timedelta(days=MAX_REFERENCE_RETENTION_DAYS):
            raise ValueError("reference_retention_invalid")
        ciphertext = self._cipher.seal(scope=scope, purpose="queued_input", identity=submission_id, content=raw)
        policy = await read_retention_policy_in_connection(connection, team_id=scope.team_id)
        await connection.execute(
            """
            INSERT INTO chat_reference_queued_inputs(team_id,user_id,client_submission_id,session_id,workspace_id,schema_version,
                accepted_parent_turn_id,accepted_generation,payload_hash,ciphertext,state,row_version,retention_state,created_at,expires_at,retention_policy_version)
            VALUES (?,?,?,?,?,1,?,?,?,?,'queued',1,'available',?,?,?)
        """,
            (
                scope.team_id,
                scope.user_id,
                submission_id,
                scope.session_id,
                scope.workspace_id,
                accepted_parent_turn_id,
                accepted_generation,
                payload_hash,
                ciphertext,
                _now().isoformat(),
                expires_at,
                policy.version,
            ),
        )
        return True

    async def get_queued_input_in_connection(
        self, connection: aiosqlite.Connection, *, scope: ReferenceScope, submission_id: str
    ) -> tuple[QueuedReferenceInput, str, int] | None:
        _transaction(connection)
        scope = _snapshot(scope)
        await _scope(connection, scope, write=False)
        row = await _row(
            connection,
            "SELECT * FROM chat_reference_queued_inputs WHERE team_id=? AND user_id=? AND client_submission_id=? AND workspace_id=? AND session_id=?",
            (scope.team_id, scope.user_id, submission_id, scope.workspace_id, scope.session_id),
        )
        if row is None:
            return None
        effective = await _row(
            connection,
            "SELECT available AND COALESCE(deadline>julianday(?),0) AS live FROM chat_reference_queue_retention "
            "WHERE team_id=? AND user_id=? AND client_submission_id=?",
            (_now().isoformat(), scope.team_id, scope.user_id, submission_id),
        )
        if effective is None or not effective["live"]:
            raise ValueError("reference_source_unavailable")
        await _require_queue_parent_material(connection, row["accepted_parent_turn_id"])
        raw = self._cipher.open(scope=scope, purpose="queued_input", identity=submission_id, ciphertext=row["ciphertext"])
        if _digest(raw) != row["payload_hash"]:
            raise ValueError("reference_source_changed")
        payload = QueuedReferenceInput.model_validate(decode_reference_json(raw, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20))
        return payload, row["state"], row["row_version"]

    async def get_queued_input_for_recovery_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        session_id: str,
        accepted_parent_turn_id: str,
        submission_id: str,
    ) -> QueuedReferenceRecovery | None:
        """Open an accepted source for maintenance without granting execution.

        Owner-loss recovery must preserve a message even after the actor loses
        current resource access.  It therefore validates the immutable queue,
        Session and parent identities rather than calling ``_scope``.  The
        caller may only materialize a failed audit child from the returned
        payload; current authorization is deliberately not reconstructed here.
        """
        _transaction(connection)
        cancelled = await get_cancelled_queued_input_for_recovery_in_connection(
            connection,
            session_id=session_id,
            accepted_parent_turn_id=accepted_parent_turn_id,
            submission_id=submission_id,
        )
        if cancelled is not None:
            return cancelled
        row = await _row(
            connection,
            """
            SELECT q.*,COALESCE(p.epoch,0) AS policy_epoch
            FROM chat_reference_queued_inputs q
            JOIN sessions s ON s.id=q.session_id
            JOIN workspaces w ON w.id=q.workspace_id AND w.id=s.workspace_id
            LEFT JOIN access_policy_state p ON p.team_id=q.team_id
            WHERE q.session_id=? AND q.accepted_parent_turn_id=?
              AND q.client_submission_id=? AND q.user_id=s.user_id
              AND q.team_id=w.team_id
            """,
            (session_id, accepted_parent_turn_id, submission_id),
        )
        if row is None:
            return None
        scope = ReferenceScope(
            team_id=str(row["team_id"]),
            user_id=str(row["user_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            policy_epoch=int(row["policy_epoch"]),
        )
        state = str(row["state"])
        if state not in {"queued", "materialized", "cancelled"}:
            raise ValueError("reference_queued_input_changed")
        cancel_reason = row["cancel_reason"]
        if (state == "cancelled") != (cancel_reason in {"user_cancelled", "source_deleted", "retention_expired"}):
            raise ValueError("reference_queued_input_changed")
        payload = None
        if state == "queued":
            effective = await _row(
                connection,
                "SELECT available AND COALESCE(deadline>julianday(?),0) AS live "
                "FROM chat_reference_queue_retention WHERE team_id=? AND user_id=? "
                "AND client_submission_id=?",
                (_now().isoformat(), scope.team_id, scope.user_id, submission_id),
            )
            if effective is None or not effective["live"] or row["retention_state"] != "available" or row["ciphertext"] is None:
                raise ValueError("reference_source_unavailable")
            raw = self._cipher.open(
                scope=scope,
                purpose="queued_input",
                identity=submission_id,
                ciphertext=str(row["ciphertext"]),
            )
            if _digest(raw) != row["payload_hash"]:
                raise ValueError("reference_source_changed")
            payload = QueuedReferenceInput.model_validate(
                decode_reference_json(
                    raw,
                    max_bytes=MAX_REFERENCE_STORAGE_BYTES,
                    max_depth=20,
                )
            )
        return QueuedReferenceRecovery(
            scope=scope,
            submission_id=submission_id,
            accepted_parent_turn_id=accepted_parent_turn_id,
            payload_hash=str(row["payload_hash"]),
            payload=payload,
            state=state,
            cancel_reason=cancel_reason,
            row_version=int(row["row_version"]),
        )

    async def settle_blocked_queued_input_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        recovery: QueuedReferenceRecovery,
        blocked_turn_id: str,
    ) -> bool:
        """CAS a retained queued source to a non-executable failed child."""
        _transaction(connection)
        if recovery.state != "queued" or recovery.payload is None:
            raise ValueError("reference_queue_recovery_invalid")
        source = await _read_chat_turn_user_source_in_tx(
            connection,
            turn_id=blocked_turn_id,
            expected_user_id=recovery.scope.user_id,
            expected_workspace_id=recovery.scope.workspace_id,
        )
        turn = await _row(
            connection,
            "SELECT status,error_kind FROM turns WHERE id=? AND session_id=?",
            (blocked_turn_id, recovery.scope.session_id),
        )
        if (
            source is None
            or source.input_document != recovery.payload.input_document
            or list(source.attachment_ids) != recovery.payload.attachment_ids
            or list(source.attachment_refs) != recovery.payload.attachment_refs
            or turn is None
            or turn["status"] != "failed"
            or turn["error_kind"] != "reference_queue_blocked"
            or await _row(connection, "SELECT 1 FROM chat_reference_plans WHERE turn_id=?", (blocked_turn_id,)) is not None
            or await _row(connection, "SELECT 1 FROM turn_agent_binding WHERE turn_id=?", (blocked_turn_id,)) is not None
            or await _row(connection, "SELECT 1 FROM turn_agent_execution WHERE turn_id=?", (blocked_turn_id,)) is not None
        ):
            raise ValueError("reference_blocked_source_changed")
        cursor = await connection.execute(
            """
            UPDATE chat_reference_queued_inputs
            SET state='materialized',row_version=row_version+1,
                materialized_turn_id=?,cancel_reason=NULL
            WHERE team_id=? AND user_id=? AND client_submission_id=?
              AND workspace_id=? AND session_id=?
              AND accepted_parent_turn_id=? AND payload_hash=?
              AND state='queued' AND row_version=?
            """,
            (
                blocked_turn_id,
                recovery.scope.team_id,
                recovery.scope.user_id,
                recovery.submission_id,
                recovery.scope.workspace_id,
                recovery.scope.session_id,
                recovery.accepted_parent_turn_id,
                recovery.payload_hash,
                recovery.row_version,
            ),
        )
        return cursor.rowcount == 1

    async def settle_queued_input_in_connection(
        self,
        connection: aiosqlite.Connection,
        *,
        scope: ReferenceScope,
        submission_id: str,
        expected_version: int,
        materialized_turn_id: str | None = None,
        cancel_reason: Literal["user_cancelled", "source_deleted", "retention_expired"] | None = None,
    ) -> bool:
        _transaction(connection)
        scope = _snapshot(scope)
        if (materialized_turn_id is None) == (cancel_reason is None):
            raise ValueError("reference_queue_decision_invalid")
        if cancel_reason is not None:
            return await self.cancel_queued_input_in_connection(
                connection,
                scope=scope,
                submission_id=submission_id,
                expected_version=expected_version,
                cancel_reason=cancel_reason,
            )
        assert materialized_turn_id is not None
        await _scope(connection, scope, write=True)
        saved = await self.get_queued_input_in_connection(connection, scope=scope, submission_id=submission_id)
        queued = await _row(
            connection,
            "SELECT accepted_parent_turn_id FROM chat_reference_queued_inputs WHERE team_id=? AND user_id=? AND client_submission_id=?",
            (scope.team_id, scope.user_id, submission_id),
        )
        plan = await self.get_plan_in_connection(connection, scope=scope, turn_id=materialized_turn_id)
        source = await _read_chat_turn_user_source_in_tx(
            connection, turn_id=materialized_turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
        )
        if (
            saved is None
            or queued is None
            or materialized_turn_id == queued["accepted_parent_turn_id"]
            or plan is None
            or source is None
            or source.input_document != saved[0].input_document
            or list(source.attachment_ids) != saved[0].attachment_ids
            or list(source.attachment_refs) != saved[0].attachment_refs
        ):
            raise ValueError("reference_materialized_source_changed")
        cursor = await connection.execute(
            """
            UPDATE chat_reference_queued_inputs SET state='materialized',row_version=row_version+1,materialized_turn_id=?,cancel_reason=NULL
            WHERE team_id=? AND user_id=? AND client_submission_id=? AND workspace_id=? AND session_id=? AND state='queued' AND row_version=?
        """,
            (
                materialized_turn_id,
                scope.team_id,
                scope.user_id,
                submission_id,
                scope.workspace_id,
                scope.session_id,
                expected_version,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    async def cancel_queued_input_in_connection(
        connection: aiosqlite.Connection,
        *,
        scope: ReferenceScope,
        submission_id: str,
        expected_version: int,
        cancel_reason: Literal["user_cancelled", "source_deleted", "retention_expired"],
    ) -> bool:
        """Settle a queued input as cancelled without opening its ciphertext."""
        _transaction(connection)
        scope = _snapshot(scope)
        await _scope(connection, scope, write=True)
        cursor = await connection.execute(
            """
            UPDATE chat_reference_queued_inputs
            SET state='cancelled',row_version=row_version+1,
                materialized_turn_id=NULL,cancel_reason=?
            WHERE team_id=? AND user_id=? AND client_submission_id=?
              AND workspace_id=? AND session_id=?
              AND state='queued' AND row_version=?
            """,
            (
                cancel_reason,
                scope.team_id,
                scope.user_id,
                submission_id,
                scope.workspace_id,
                scope.session_id,
                expected_version,
            ),
        )
        return cursor.rowcount == 1

    async def purge_in_connection(self, connection: aiosqlite.Connection, *, scope: ReferenceScope) -> None:
        """Explicit authenticated material purge in the caller's transaction.

        Session deletion also purges through migration-owned lifecycle triggers;
        background expiry uses chat_reference_retention without user credentials.
        This helper does not delete canonical messages or materialize queue work.
        """
        _transaction(connection)
        scope = _snapshot(scope)
        await _scope(connection, scope, write=True)
        await connection.execute(
            "UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?) AND retention_state='available'",
            (scope.session_id,),
        )
        await connection.execute(
            "UPDATE chat_reference_content_snapshots SET retention_state='purged',ciphertext=NULL WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?) AND retention_state='available'",
            (scope.session_id,),
        )
        await connection.execute(
            """
            UPDATE chat_reference_queued_inputs SET retention_state='purged',ciphertext=NULL,
                state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
                cancel_reason=CASE WHEN state='queued' THEN 'source_deleted' ELSE cancel_reason END,row_version=row_version+1
            WHERE team_id=? AND user_id=? AND workspace_id=? AND session_id=? AND retention_state='available'
        """,
            (scope.team_id, scope.user_id, scope.workspace_id, scope.session_id),
        )
