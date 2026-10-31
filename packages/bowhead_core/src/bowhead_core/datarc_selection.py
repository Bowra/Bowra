"""Native Datarc run ownership, accepted execution scopes and source evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import anyio
from pydantic import BaseModel, ConfigDict, model_validator


class DatarcCommitUnknown(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Datarc preference commit acknowledgement is unknown")


async def require_datarc_context_migration(db: Any) -> None:
    try:
        async with db.execute("SELECT version_num FROM alembic_version") as cursor:
            row = await cursor.fetchone()
        if row is None or int(str(row[0]).split("_", 1)[0]) < 205:
            raise ValueError("Datarc context migration required")
    except Exception:
        raise ValueError("datarc_context_migration_required") from None


@dataclass(frozen=True)
class DatarcConnectionBinding:
    team_id: str
    connection_id: str
    revision: str
    client: Any = field(repr=False, compare=False)
    vector_resolver: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class DatarcPreferenceScope:
    team_id: str
    connection_id: str

    def __post_init__(self) -> None:
        if any(not item or len(item) > 128 or "/" in item for item in (self.team_id, self.connection_id)):
            raise ValueError("Invalid Datarc preference scope")

    def key(self, session_id: str | None = None) -> str:
        base = f"datarc/workspace@v2/{self.team_id}/{self.connection_id}"
        return f"{base}/session/{hashlib.sha256(session_id.encode()).hexdigest()}" if session_id else f"{base}/default"


@dataclass(frozen=True)
class DatarcPersistenceReceipt:
    status: Literal["saved", "skipped", "failed", "unknown", "not_attempted"]
    scope: Literal["session", "default"]
    reason: str = ""

    def __bool__(self) -> bool:
        return self.status == "saved"

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "scope": self.scope, "reason": self.reason}


class DatarcSelectionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal["datarc-selection.v1"] = "datarc-selection.v1"
    state: Literal["selected", "unselected", "unavailable"]
    team_id: str
    user_id: str
    connection_id: str = ""
    connection_revision: str = ""
    datarc_workspace_id: str | None = None
    name: str = ""
    member_uid: str = ""

    @model_validator(mode="after")
    def coherent_binding(self) -> DatarcSelectionSnapshot:
        fields = (
            self.team_id,
            self.user_id,
            self.connection_id,
            self.connection_revision,
            self.datarc_workspace_id or "",
            self.name,
            self.member_uid,
        )
        if any(len(item) > 1024 for item in fields):
            raise ValueError("Datarc source evidence exceeds its bounds")
        if not (self.team_id and self.user_id):
            raise ValueError("Missing Datarc source owner")
        if self.state != "unavailable" and not (self.team_id and self.user_id and self.connection_id and self.connection_revision):
            raise ValueError("Missing accepted Datarc connection")
        if self.state == "selected" and not (self.datarc_workspace_id and self.member_uid):
            raise ValueError("Missing accepted Datarc identity")
        if self.state != "selected" and (self.datarc_workspace_id or self.member_uid):
            raise ValueError("Unselected Datarc scope contains an identity")
        if self.state == "unavailable" and (self.connection_id or self.connection_revision or self.name):
            raise ValueError("Unavailable Datarc scope contains a connection")
        return self


@dataclass(frozen=True)
class DatarcSelection:
    binding: DatarcConnectionBinding | None
    user_id: str
    datarc_workspace_id: str | None
    name: str
    member_uid: str
    client: Any = field(repr=False, compare=False)
    vector_client: Any = field(default=None, repr=False, compare=False)

    def snapshot(self, *, team_id: str = "") -> DatarcSelectionSnapshot:
        if self.client is not None and self.binding is None:
            raise ValueError("datarc_resume_scope_missing")
        return DatarcSelectionSnapshot(
            state="unavailable" if self.client is None else "selected" if self.datarc_workspace_id else "unselected",
            team_id=self.binding.team_id if self.binding else team_id,
            user_id=self.user_id,
            connection_id=self.binding.connection_id if self.binding and self.client is not None else "",
            connection_revision=self.binding.revision if self.binding and self.client is not None else "",
            datarc_workspace_id=self.datarc_workspace_id if self.client is not None else None,
            name=self.name if self.client is not None else "",
            member_uid=self.member_uid if self.client is not None and self.datarc_workspace_id else "",
        )

    def source_key(self, *, team_id: str = "") -> str:
        # Explicit Core-only callers have no registry evidence. Their client instance
        # still isolates ephemeral caches; it can never establish durable authority.
        identity = (
            self.binding.team_id if self.binding else team_id,
            self.user_id,
            self.binding.connection_id if self.binding else str(id(self.client)),
            self.binding.revision if self.binding else "",
            self.datarc_workspace_id,
            self.member_uid,
        )
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class DatarcExecutionScope:
    selection: DatarcSelection
    revision: int


@dataclass
class DatarcRunState:
    current: DatarcExecutionScope
    team_id: str = ""
    lock: anyio.Lock = field(default_factory=anyio.Lock, repr=False)

    def publish(self, selection: DatarcSelection) -> DatarcExecutionScope:
        self.current = DatarcExecutionScope(selection, self.current.revision + 1)
        return self.current


class DatarcSwitchService(Protocol):
    async def prepare_selection(self, identifier: str, name: str, *, current: DatarcSelection) -> DatarcSelection: ...

    async def save_preference(self, selection: DatarcSelection, *, remember: bool) -> DatarcPersistenceReceipt: ...


def ensure_datarc_run(ctx: Any) -> DatarcRunState:
    state = getattr(ctx, "datarc_run_state", None)
    if state is None:
        selection = DatarcSelection(
            binding=getattr(ctx, "datarc_binding", None),
            user_id=getattr(ctx, "user_id", ""),
            datarc_workspace_id=getattr(ctx, "active_datarc_project", None),
            name=getattr(ctx, "active_datarc_workspace_name", ""),
            member_uid=getattr(ctx, "member_uid", ""),
            client=getattr(ctx, "datarc", None),
            vector_client=getattr(ctx, "vector_client", None),
        )
        state = DatarcRunState(DatarcExecutionScope(selection, 0), team_id=getattr(ctx, "team_id", "") or "")
        ctx.datarc_run_state = state
    _scope_schema_cache(ctx, getattr(ctx, "datarc_execution_scope", None) or state.current)
    return state


def current_datarc_selection(ctx: Any) -> DatarcSelection:
    return ensure_datarc_run(ctx).current.selection


def accepted_datarc_selection(ctx: Any) -> DatarcSelection:
    scope = getattr(ctx, "datarc_execution_scope", None)
    return scope.selection if scope is not None else current_datarc_selection(ctx)


def project_datarc_scope(ctx: Any, scope: DatarcExecutionScope) -> None:
    selection = scope.selection
    ctx.datarc_execution_scope = scope
    ctx.datarc_binding = selection.binding
    ctx.active_datarc_project = selection.datarc_workspace_id
    ctx.active_datarc_workspace_name = selection.name
    ctx.member_uid = selection.member_uid
    ctx.datarc = selection.client
    ctx.vector_client = selection.vector_client
    _scope_schema_cache(ctx, scope)


def _scope_schema_cache(ctx: Any, scope: DatarcExecutionScope) -> None:
    from bowhead_core.datarc_schema_cache import DatarcSchemaCache

    cache = getattr(ctx, "schema_cache", None)
    # Eval owns a private cache for its immutable, budgeted attempt identity.
    if isinstance(cache, DatarcSchemaCache) and getattr(ctx, "eval_datarc", None) is None:
        ctx.schema_cache = cache.scoped(scope.selection.source_key(team_id=getattr(ctx, "team_id", "") or ""))


def fork_datarc_run(ctx: Any, *, selection: DatarcSelection | None = None) -> Any:
    child = copy.copy(ctx)
    scope = DatarcExecutionScope(selection or accepted_datarc_selection(ctx), 0)
    child._switch_streak_box = {"n": 0}
    child.datarc_run_state = DatarcRunState(scope, team_id=getattr(ctx, "team_id", "") or "")
    project_datarc_scope(child, scope)
    return child


def bind_datarc_runtime(request: Any, ctx: Any) -> None:
    """Attach the root owner to the original Terminal source producer only."""
    if (ctx.datarc_binding is None or ctx.datarc is None) and getattr(request, "datarc_source_evidence", None) is None:
        return
    runtime = getattr(request, "terminal_runtime", None)
    binder = getattr(getattr(runtime, "bus", None), "bind_datarc_run", None)
    if binder is not None:
        binder(ensure_datarc_run(ctx))


def datarc_execution_intent(state: DatarcRunState) -> dict[str, Any]:
    selection = state.current.selection
    result = {"active_datarc_project": selection.datarc_workspace_id, "member_uid": selection.member_uid}
    if selection.binding is not None or selection.client is None:
        result["datarc_selection"] = selection.snapshot(team_id=state.team_id).model_dump(mode="json")
    return result


async def switch_datarc_selection(ctx: Any, identifier: str, name: str, *, remember: bool, streak_pause_at: int | None = None) -> dict[str, Any]:
    state = ensure_datarc_run(ctx)
    scoped_call = getattr(ctx, "datarc_execution_scope", None) is not None
    expected = getattr(ctx, "datarc_execution_scope", None) or state.current
    service: DatarcSwitchService = ctx.datarc_context_switcher
    async with state.lock:
        if state.current.revision != expected.revision:
            return {
                "ok": False,
                "error": "datarc_selection_conflict",
                "error_kind": "datarc_selection_conflict",
                "persistence": DatarcPersistenceReceipt("not_attempted", "default" if remember else "session", "selection_conflict").as_dict(),
                "hint": "The run changed source after this call was admitted. Read the current source and issue a fresh switch if needed.",
            }
        streak = ctx._switch_streak_box
        if streak_pause_at is not None and streak["n"] >= streak_pause_at - 1:
            return {
                "ok": False,
                "error": "switch_streak_paused",
                "error_kind": "switch_streak_paused",
                "switch_streak": streak["n"] + 1,
                "hint": "Read the selected workspace before switching again. Completed switches are counted within this run.",
            }
        prepared = await service.prepare_selection(identifier, name, current=state.current.selection)
        scope = state.publish(prepared)
        if streak_pause_at is not None:
            streak["n"] += 1
        project_datarc_scope(ctx, scope)
        if not scoped_call:
            ctx.datarc_execution_scope = None
        try:
            saved = await service.save_preference(prepared, remember=remember)
        except Exception:
            saved = DatarcPersistenceReceipt("unknown", "default" if remember else "session", "save_unconfirmed")
    return {
        "ok": True,
        "switched": {"id": identifier, "name": name},
        "effective": saved.scope if saved else "run",
        "runtime_scope": "owning_run",
        "selection_revision": scope.revision,
        "persistence": saved.as_dict(),
        "note": (
            "The current run uses the selected Datarc workspace. Already admitted helpers retain their source. "
            + (
                "The default is saved and this Chat's overlay is cleared."
                if saved and remember
                else "This Chat's selection is saved."
                if saved
                else "The preference was not saved; only this run changed."
                if saved.status in {"failed", "skipped", "not_attempted"}
                else "Saving is unconfirmed; no Chat-wide or default change is confirmed."
            )
        ),
    }


async def restore_datarc_selection(ctx: Any, snapshot: DatarcSelectionSnapshot) -> DatarcSelection:
    current = current_datarc_selection(ctx)
    binding = current.binding
    if snapshot.user_id != ctx.user_id or snapshot.team_id != (getattr(ctx, "team_id", "") or ""):
        raise ValueError("datarc_resume_scope_changed")
    if snapshot.state == "unavailable":
        return DatarcSelection(None, ctx.user_id, None, "", "", None)
    if binding is None or (binding.connection_id, binding.revision) != (snapshot.connection_id, snapshot.connection_revision):
        raise ValueError("datarc_resume_scope_changed")
    if snapshot.state == "selected":
        service = ctx.datarc_context_switcher
        if service is None:
            raise ValueError("datarc_resume_scope_missing")
        prepared = await service.prepare_selection(snapshot.datarc_workspace_id, snapshot.name, current=current)
        if prepared.member_uid != snapshot.member_uid:
            raise ValueError("datarc_resume_scope_changed")
        return prepared
    return replace(current, datarc_workspace_id=snapshot.datarc_workspace_id, name=snapshot.name, member_uid=snapshot.member_uid, vector_client=None)


def validate_datarc_source_evidence(
    raw: object,
    *,
    binding: DatarcConnectionBinding | None,
    team_id: str,
    user_id: str,
    datarc_workspace_id: str | None,
    member_uid: str,
    allow_workspace_change: bool = False,
) -> None:
    """Check the original source after native recovery has reauthorized identity."""
    if raw is None:
        return
    evidence = DatarcSelectionSnapshot.model_validate(raw)
    if (evidence.team_id, evidence.user_id) != (team_id, user_id):
        raise ValueError("datarc_resume_scope_changed")
    if evidence.state == "unavailable":
        if binding is not None or datarc_workspace_id or member_uid:
            raise ValueError("datarc_resume_scope_changed")
        return
    if binding is None or (evidence.connection_id, evidence.connection_revision) != (binding.connection_id, binding.revision):
        raise ValueError("datarc_resume_scope_changed")
    if evidence.state == "selected" and not allow_workspace_change:
        if (evidence.datarc_workspace_id, evidence.member_uid) != (datarc_workspace_id, member_uid):
            raise ValueError("datarc_resume_scope_changed")
    if evidence.state == "unselected" and not allow_workspace_change and (datarc_workspace_id or member_uid):
        raise ValueError("datarc_resume_scope_changed")
