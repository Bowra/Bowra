"""Bounded source discovery using original caller and origin authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.kb.source_navigation_store import SourceNavigationStore

if TYPE_CHECKING:
    import aiosqlite


def _text(value: str, limit: int) -> str:
    return " ".join("".join(ch for ch in value if unicodedata.category(ch) not in {"Cc", "Cf"} or ch.isspace()).split())[:limit]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cursor_cipher() -> Fernet:
    try:
        root = base64.b64decode(os.environ.get("BOWHEAD_FERNET_KEY", "").encode("ascii"), altchars=b"-_", validate=True)
        if len(root) != 32:
            raise ValueError("Invalid instance key")
        key = hmac.new(root, b"bowhead/kb-source-catalog/v1", hashlib.sha256).digest()
        return Fernet(base64.urlsafe_b64encode(key))
    except (ValueError, UnicodeError) as exc:
        raise KnowledgeAccessError("knowledge_catalog_continuation_unavailable") from exc


class NavigationDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    file_id: str = Field(min_length=1, max_length=128)
    generation_id: str | None
    membership_revision: int = Field(ge=1)
    view_token: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass
class SourcePage:
    entries: list[dict[str, Any]] = field(default_factory=list)
    dependencies: dict[str, dict[str, Any]] = field(default_factory=dict)
    categories: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, Any] = field(default_factory=dict, repr=False)
    next_cursor: str | None = None
    partial: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "sources": self.entries,
            "categories": self.categories,
            "next_cursor": self.next_cursor,
            "partial": self.partial,
            "coverage": "returned_sources",
            "hint": "Navigation is not evidence. Use original KB Tools for facts; missing matches do not prove absence.",
        }


async def _allowed(ctx: Any, files: list[Any]) -> dict[str, bool]:
    authorize = getattr(ctx, "kb_file_authorizer", None)
    if authorize is None:
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    batch = getattr(authorize, "batch", None)
    if callable(batch):
        result = await batch(files, ctx.user_id, ctx.team_id, ctx.workspace_id)
        if not isinstance(result, dict):
            raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
        return result
    return {file.file_id: bool(await authorize(file, ctx.user_id, ctx.team_id, ctx.workspace_id)) for file in files}


def _identity(file: Any) -> tuple:
    return (
        file.file_id,
        file.team_id,
        file.scope,
        file.scope_id,
        file.workspace_id,
        file.artifact_id,
        file.version,
        file.membership_revision,
        file.active_generation_id,
        file.soft_deleted_at,
        file.source_absent,
        file.parse_status,
    )


def _scope(ctx: Any) -> Any:
    if not all(getattr(ctx, name, "") for name in ("user_id", "team_id")):
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    if not getattr(getattr(ctx, "kb_files_store", None), "_db_path", None) or getattr(ctx, "kb_file_authorizer", None) is None:
        raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    scope = getattr(ctx, "knowledge_scope", None)
    if scope is not None:
        scope.require_context(ctx)
    return scope


async def visible_categories(ctx: Any, store: SourceNavigationStore, definitions: list[dict] | None = None) -> list[dict]:
    rows = definitions if definitions is not None else await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id)
    check = getattr(ctx.kb_file_authorizer, "categories", None)
    if callable(check):
        rows = await check(rows, ctx.user_id, ctx.team_id, ctx.workspace_id)
        if rows is None:
            raise KnowledgeAccessError("knowledge_source_authorization_unavailable")
    return rows


async def _project(
    ctx: Any,
    files: list[Any],
    categories: list[dict[str, Any]],
    store: SourceNavigationStore,
    *,
    include_automatic: bool = True,
    connection: aiosqlite.Connection | None = None,
) -> SourcePage:
    scope = _scope(ctx)
    categories = await visible_categories(ctx, store, categories)
    decisions = await _allowed(ctx, files)
    files = [
        file
        for file in files
        if decisions.get(file.file_id)
        and not file.soft_deleted_at
        and not file.source_absent
        and file.parse_status == "done"
        and file.team_id == ctx.team_id
        and (scope is None or scope.permits(file.file_id))
    ]
    page = SourcePage()
    if not files:
        return page
    published = await store.published_generations(files)
    metadata = await store.metadata(files, ctx.workspace_id)
    if not include_automatic:
        for state in metadata.values():
            state["descriptions"] = [row for row in state["descriptions"] if row["method"] == "manual"]
    missing = {identity for state in metadata.values() for identity in state["labels"]} - {row["id"] for row in categories}
    if missing:
        categories += await visible_categories(
            ctx, store, await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id, identities=list(missing))
        )
    origins = [row for state in metadata.values() for row in state["descriptions"] if row["method"] == "matrix" and row["generation_id"] in published]
    page.partial = (
        len(origins) > 128
        or sum(len(state["descriptions"]) for state in metadata.values()) >= 256
        or any(len(state["descriptions"]) >= 16 for state in metadata.values())
    )
    origins = origins[:128]
    origin_check = getattr(ctx.kb_file_authorizer, "navigation", None)
    allowed_origins = await origin_check(origins, ctx.user_id, ctx.team_id, ctx.workspace_id) if origins and callable(origin_check) else set()
    bodies = await store.matrix_bodies([row["id"] for row in origins if row["id"] in allowed_origins])
    read_options = {"connection": connection} if connection is not None else {}
    fresh = {file.file_id: file for file in await ctx.kb_files_store.get_many([file.file_id for file in files], **read_options)}
    final = await _allowed(ctx, list(fresh.values()))
    if allowed_origins:
        allowed_origins &= await origin_check(origins, ctx.user_id, ctx.team_id, ctx.workspace_id)
    definitions = {category["id"]: category for category in categories}
    for file in files:
        current = fresh.get(file.file_id)
        if current is None or not final.get(file.file_id) or _identity(current) != _identity(file):
            page.partial = True
            continue
        state = metadata[file.file_id]
        candidates = []
        for row in state["descriptions"]:
            if row["method"] != "manual" and row["generation_id"] not in published:
                continue
            if row["method"] == "matrix":
                body = bodies.get(row["id"])
                if row["id"] not in allowed_origins or not body or body["revision"] != row["revision"] or row["source_version"] != file.version:
                    continue
                row = {**row, **body}
            candidates.append(row)
        selected = next((row for row in candidates if row["text"]), None)
        automatic = next((row for row in candidates if row["method"] != "manual" and row["topics"]), None)
        topics = [_text(value, 80) for value in (automatic["topics"] if automatic else []) if isinstance(value, str)][:8]
        label_ids = set(state["labels"]) & definitions.keys()
        if not state["labels_override"]:
            for category in categories:
                vocabulary = {category["name"].casefold(), *(alias.casefold() for alias in category["aliases"])}
                if any(topic.casefold() in vocabulary for topic in topics):
                    label_ids.add(category["id"])
        labels = [{key: definitions[identity][key] for key in ("id", "name", "parent_id", "revision")} for identity in sorted(label_ids)][:8]
        entry = {
            "file_id": file.file_id,
            "name": _text(file.filename, 120),
            "generation_id": file.active_generation_id,
            "source_version": file.version,
            "description": selected["text"] if selected else "",
            "description_kind": selected["method"] if selected else "name",
            "coverage": selected["coverage"] if selected else "metadata_only",
            "topics": topics,
            "categories": labels,
            "navigation_revision": state["revision"],
        }
        contributors = {row["id"]: row["revision"] for row in (selected, automatic) if row is not None}
        proof = NavigationDependency(
            file_id=file.file_id,
            generation_id=file.active_generation_id,
            membership_revision=file.membership_revision,
            view_token=_digest({"entry": entry, "contributors": contributors, "version": file.version}),
        )
        page.entries.append(entry)
        page.dependencies[file.file_id] = proof.model_dump()
        page.files[file.file_id] = file
    return page


async def list_sources(
    ctx: Any, *, query: str = "", category_id: str | None = None, cursor: str | None = None, limit: int = 20, initial: bool = False
) -> SourcePage:
    scope = _scope(ctx)
    if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(query, str) or len(query) > 200:
        raise ValueError("knowledge_catalog_input_invalid")
    store = SourceNavigationStore(ctx.kb_files_store._db_path)
    if category_id is not None and (not isinstance(category_id, str) or len(category_id) > 128):
        raise ValueError("knowledge_catalog_input_invalid")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
        raise ValueError("knowledge_catalog_cursor_invalid")
    binding = _digest([ctx.user_id, ctx.team_id, ctx.workspace_id, scope.fingerprint if scope else None, query, category_id])
    after = None
    if cursor:
        try:
            payload = json.loads(_cursor_cipher().decrypt(cursor.encode()))
            if set(payload) != {"after", "binding"} or payload["binding"] != binding or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["after"]):
                raise ValueError("Invalid cursor")
            after = payload["after"]
        except (ValueError, TypeError, KeyError, UnicodeError, InvalidToken) as exc:
            raise ValueError("knowledge_catalog_cursor_invalid") from exc
    budget = 100 if initial else 128
    files = await ctx.kb_files_store.list_for_scopes_filtered(
        team_id=ctx.team_id,
        personal_owner_id=ctx.user_id,
        space_id=ctx.workspace_id,
        team_scope_id=ctx.team_id,
        statuses=["done"],
        candidate_limit=budget,
        allowed_file_ids=scope.file_ids if scope else None,
        order_by="uploaded_at" if initial else "file_id",
        order_dir="desc" if initial else "asc",
        after_file_id=after,
    )
    categories = await visible_categories(ctx, store)
    if category_id and category_id != "unclassified" and category_id not in {row["id"] for row in categories}:
        categories += await visible_categories(
            ctx, store, await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id, identities=[category_id])
        )
    page = await _project(ctx, files, categories, store)
    if initial:
        # Rank only authorized projections; stable ties retain recent-source order.
        methods = {"manual": 3, "standalone": 2, "matrix": 1, "name": 0}
        page.entries.sort(key=lambda entry: (methods[entry["description_kind"]], bool(entry["categories"]), bool(entry["topics"])), reverse=True)
    matched = []
    for entry in page.entries:
        category_ids = {category["id"] for category in entry["categories"]}
        if category_id == "unclassified" and category_ids:
            continue
        if category_id and category_id != "unclassified" and category_id not in category_ids:
            continue
        haystack = " ".join([entry["name"], entry["description"], *entry["topics"], *(category["name"] for category in entry["categories"])])
        if not query or query.casefold() in haystack.casefold():
            matched.append(entry)
        if len(matched) == limit:
            break
    scanned_to = matched[-1]["file_id"] if len(matched) == limit else files[-1].file_id if files else after
    more = bool(files and (len(files) == budget or scanned_to != files[-1].file_id))
    page.entries = matched
    page.dependencies = {entry["file_id"]: page.dependencies[entry["file_id"]] for entry in matched}
    page.files = {identity: page.files[identity] for identity in page.dependencies}
    page.partial = page.partial or more
    if more and not initial:
        page.next_cursor = _cursor_cipher().encrypt(json.dumps({"after": scanned_to, "binding": binding}).encode()).decode()
    facets = {}
    for entry in matched:
        for category in entry["categories"]:
            item = facets.setdefault(category["id"], {**category, "count": 0})
            item["count"] += 1
    page.categories = list(facets.values())
    return page


def record_navigation_uses(ctx: Any, dependencies: dict[str, dict[str, Any]]) -> None:
    maps = [ctx.knowledge_navigation_uses, *ctx.knowledge_navigation_ancestors]
    for retained in maps:
        for identity, proof in dependencies.items():
            if identity in retained and retained[identity] != proof:
                raise KnowledgeAccessError("knowledge_source_changed")
        retained.update(dependencies)


async def validate_navigation_dependencies(
    ctx: Any, dependencies: dict[str, dict[str, Any]], *, connection: aiosqlite.Connection | None = None
) -> None:
    if not dependencies:
        return
    _scope(ctx)
    if connection is not None:
        await _validate_navigation_in_connection(ctx, dependencies, connection)
    else:
        async with open_pooled_read_ctx(ctx.kb_files_store._db_path) as connection:
            await _validate_navigation_in_connection(ctx, dependencies, connection)


async def _validate_navigation_in_connection(ctx: Any, dependencies: dict[str, dict[str, Any]], connection: aiosqlite.Connection) -> None:
    """Keep fresh autocommit reads while reusing this database's physical handle."""
    store = SourceNavigationStore(ctx.kb_files_store._db_path, connection=connection)
    # Projection applies the current category authorization once per batch.
    categories = await store.categories(ctx.team_id, ctx.user_id, ctx.workspace_id)
    for offset in range(0, len(dependencies), 256):
        ids = list(dependencies)[offset : offset + 256]
        expected = {identity: NavigationDependency.model_validate(dependencies[identity]).model_dump() for identity in ids}
        current = await _project(
            ctx,
            await ctx.kb_files_store.get_many(ids, connection=connection),
            categories,
            store,
            connection=connection,
        )
        changed = [identity for identity in ids if current.dependencies.get(identity) != expected[identity]]
        if changed:
            # A newly available automatic description does not invalidate a still-current
            # basic snapshot. Existing text/category edits and revoked Matrix origins do.
            basic = await _project(
                ctx,
                await ctx.kb_files_store.get_many(changed, connection=connection),
                categories,
                store,
                include_automatic=False,
                connection=connection,
            )
            if any(basic.dependencies.get(identity) != expected[identity] for identity in changed):
                raise KnowledgeAccessError("knowledge_source_changed")
