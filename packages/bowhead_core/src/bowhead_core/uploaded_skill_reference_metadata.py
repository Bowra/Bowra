"""Background metadata for personal/team packages; discovery never opens files.

The cache contains labels, exact whole-package hashes and reference parameter
schemas only. Current scope, visibility, import/approval and use checks belong
to each catalog query. Cached metadata is never execution authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Literal

import aiosqlite
import anyio
import yaml
from bowhead_skill.loader import split_skill_markdown

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_package_material import _manifest_name
from bowhead_core.skill_read_material import _bundle_hash, _capture, prepare_skill_read_files

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS = 15
MAX_METADATA_AGE_SECONDS = 30
UploadedSourceKind = Literal["user", "team"]

_PERSONAL_COLUMNS = "id,user_id,name,source_type,source_path,version,description,enabled,updated_at,import_status"
_TEAM_COLUMNS = (
    "id,team_id,name,display_name,description,source_path,status,approval,approved_sha256,visibility,created_by,approved_by,approved_at,updated_at"
)


def uploaded_skill_metadata_stamp(kind: UploadedSourceKind, row: dict) -> str:
    """Bind metadata to the live source record, never the pending update slot."""
    fields = (_PERSONAL_COLUMNS if kind == "user" else _TEAM_COLUMNS).split(",")
    state = {key: row[key] for key in fields}
    # An approved hash already pins live bytes. A proposal changes updated_at
    # without replacing that live package and must not invalidate its metadata.
    if kind == "team" and state["approved_sha256"]:
        state.pop("updated_at")
    return hashlib.sha256(json.dumps([kind, state], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def uploaded_skill_metadata_rows(
    db: aiosqlite.Connection, *, user_id: str | None = None, team_id: str | None = None
) -> list[tuple[UploadedSourceKind, dict]]:
    """Finite metadata columns on the caller transaction; no package bodies."""
    if not db.in_transaction:
        raise ValueError("reference_metadata_transaction_required")
    result = []
    queries = (
        (
            "user",
            f"SELECT {_PERSONAL_COLUMNS} FROM skill_instances WHERE source_type IN ('user_upload','user_generated')"
            + (" AND user_id=?" if user_id is not None else ""),
            (user_id,) if user_id is not None else (),
        ),
        (
            "team",
            f"SELECT {_TEAM_COLUMNS} FROM team_skill" + (" WHERE team_id=?" if team_id is not None else ""),
            (team_id,) if team_id is not None else (),
        ),
    )
    for kind, sql, parameters in queries:
        async with db.execute(sql, parameters) as cursor:
            cursor.row_factory = aiosqlite.Row
            result.extend((kind, dict(row)) for row in await cursor.fetchall())
    return result


@dataclass(frozen=True, slots=True)
class UploadedSkillMetadata:
    source_kind: UploadedSourceKind
    id: str
    owner_scope_id: str
    source_stamp: str
    name: str
    label: str
    summary: str
    revision: str
    parameter_schema_json: str | None
    read_only_entry_json: str | None = None

    def parameter_schema(self) -> dict | None:
        return json.loads(self.parameter_schema_json) if self.parameter_schema_json is not None else None


@dataclass(frozen=True, slots=True)
class UploadedSkillMetadataIndex:
    items: tuple[UploadedSkillMetadata, ...]
    _lookup: Mapping[tuple[str, str, str], UploadedSkillMetadata] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        lookup = {(item.source_kind, item.id, item.owner_scope_id): item for item in self.items}
        if len(lookup) != len(self.items):
            raise ValueError("reference_metadata_identity_ambiguous")
        object.__setattr__(self, "_lookup", MappingProxyType(lookup))

    def find(self, kind: UploadedSourceKind, row: dict) -> UploadedSkillMetadata | None:
        item = self._lookup.get((kind, row["id"], row["user_id" if kind == "user" else "team_id"]))
        return item if item is not None and item.source_stamp == uploaded_skill_metadata_stamp(kind, row) else None


def _build_metadata(kind: UploadedSourceKind, row: dict) -> UploadedSkillMetadata:
    files = _capture(Path(row["source_path"]))
    _manifest_name(files, row["name"])
    revision = _bundle_hash(files)
    approved = row.get("approved_sha256")
    if approved and revision != (approved if approved.startswith("sha256:") else "sha256:" + approved):
        raise ValueError("reference_skill_version_changed")
    header, _ = split_skill_markdown(files["SKILL.md"].decode("utf-8"))
    metadata = yaml.safe_load(header)
    resource = SkillResourceRef(kind="skill", source_kind=kind, id=row["id"], version=revision)
    method = prepare_skill_read_files(files, resource=resource, expected_name=row["name"])
    entry = method.document()["read_only_entry"]
    schema = json.dumps(entry["parameter_schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    entry_json = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if "read_only_entry" in metadata else None
    label = str(row.get("display_name") or metadata.get("title") or row["name"])[:256]
    summary = str(row.get("description") if row.get("description") is not None else metadata.get("description") or "")[:1024]
    label.encode("utf-8")
    summary.encode("utf-8")
    return UploadedSkillMetadata(
        kind,
        row["id"],
        row["user_id" if kind == "user" else "team_id"],
        uploaded_skill_metadata_stamp(kind, row),
        row["name"],
        label,
        summary,
        revision,
        schema,
        entry_json,
    )


async def build_uploaded_skill_metadata(db_path: str | Path) -> UploadedSkillMetadataIndex:
    async def rows():
        async with open_shared_ctx(db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                return await uploaded_skill_metadata_rows(db)
            finally:
                await db.rollback()

    before = await rows()
    items = []
    for kind, row in before:
        active = (
            (row["enabled"] and row["import_status"] != "pending")
            if kind == "user"
            else (row["status"] == "enabled" and row["approval"] == "approved")
        )
        if not active or not row["source_path"]:
            continue
        try:
            items.append(await anyio.to_thread.run_sync(_build_metadata, kind, row))
        except Exception as exc:
            # A bad/missing package removes only its own metadata. No raw source
            # path or private manifest is logged or used as a fallback.
            logger.debug("reference_uploaded_metadata_unavailable source_kind=%s error=%s", kind, type(exc).__name__)
    current = {
        (kind, row["id"], row["user_id" if kind == "user" else "team_id"]): uploaded_skill_metadata_stamp(kind, row) for kind, row in await rows()
    }
    return UploadedSkillMetadataIndex(
        tuple(item for item in items if current.get((item.source_kind, item.id, item.owner_scope_id)) == item.source_stamp)
    )


class UploadedSkillMetadataCache:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._index: UploadedSkillMetadataIndex | None = None
        self._expires_at = 0.0
        self._lock = anyio.Lock()

    def current(self) -> UploadedSkillMetadataIndex | None:
        return self._index if monotonic() < self._expires_at else None

    async def refresh(self) -> None:
        async with self._lock:
            # Start age at capture, so a slow filesystem cannot extend freshness.
            expires_at = monotonic() + MAX_METADATA_AGE_SECONDS
            index = await build_uploaded_skill_metadata(self.db_path)
            self._index, self._expires_at = index, expires_at

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                logger.warning("reference_uploaded_metadata_refresh_failed error=%s", type(exc).__name__)
            await anyio.sleep(REFRESH_INTERVAL_SECONDS)
