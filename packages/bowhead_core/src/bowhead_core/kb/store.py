"""KbStore — chromadb wrap.

Tech design v3.6 §3.4. 关键设计:
- P0-N1 (round 2): per-ws + per-team collection (不 per-user, 防 1000+ collection 爆 chromadb)
- P0-O1 (round 3): 全 method signature 统一接受 KbFileDTO 单参数
- P0-O3 (round 3): chromadb where filter 用 is_active=True 替 deleted_at=None
- P0-P2 (round 4): update_chunks_scope 8 个 routing key 显式传 (跨 collection 物理迁移)
- P1-P4 (round 4): collection name sanitize (chromadb 限 alphanumeric / _ / - / . , 3-63 char)
- P1-Q3 (round 5): search 3s timeout 防大 coll 卡
- Round 5 spike 实测: chromadb 1.5.8 嵌套 $and/$or where filter work
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import chromadb
from chromadb.config import Settings

from bowhead_core.kb.errors import ScopeChangeMigrationError
from bowhead_core.kb.knowledge_base_access import KnowledgeLevels
from bowhead_core.kb.knowledge_base_store import default_for_scope
from bowhead_core.kb.source_location import SourceChunk, chunk_metadata_page
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.document import KbChunk
    from bowhead_core.kb.health import IndexObservation
    from bowhead_core.kb.permission import ScopeContext
    from bowhead_core.kb.schemas import KbFileDTO


logger = logging.getLogger(__name__)


# [P1-Q3] search 3s timeout
SEARCH_TIMEOUT_S = 3.0
# Default top_k matches retriever Step 2 (candidate pool before rerank)
DEFAULT_SEARCH_TOP_K = 20


def _without_knowledge_bases(where: dict[str, Any], excluded: tuple[str, ...]) -> dict[str, Any]:
    """Add ``knowledge_base_id $nin excluded`` to a top-level ``$and`` filter; unchanged when nothing is excluded."""
    if not excluded:
        return where
    return {"$and": [*where["$and"], {"knowledge_base_id": {"$nin": list(excluded)}}]}


def _only_knowledge_base(where: dict[str, Any], knowledge_base_id: str | None) -> dict[str, Any]:
    """Add ``knowledge_base_id == id`` to a top-level ``$and`` filter; unchanged for None."""
    if not knowledge_base_id:
        return where
    return {"$and": [*where["$and"], {"knowledge_base_id": knowledge_base_id}]}


def _selected_conditions(scope_conditions: list[dict[str, Any]], home: Any) -> list[dict[str, Any]]:
    """A home's scope conditions plus its knowledge base selection clause (none when the whole home is selected)."""
    if home is None:
        return scope_conditions
    if home.default:
        return [*scope_conditions, {"knowledge_base_id": {"$nin": list(home.excluded)}}] if home.excluded else scope_conditions
    return [*scope_conditions, {"knowledge_base_id": {"$in": list(home.only)}}]


def _all_of(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def _sanitize_id(raw: str) -> str:
    """[P1-P4] chromadb collection name 限 alphanumeric / _ / - / . , 3-63 char.

    防 user-input workspace_id 含特殊字符. chromadb 1.5+ 强制 valid range.
    0.40.3 Bug 7: chromadb 1.5.8 strictness — name MUST start AND end with
    [a-zA-Z0-9]. Pre-fix pad was `f"_{s}_"` which gave `_1_` for ws "1" →
    silent search/upload failure for any user with default workspace_id="1"
    (every non-datarc user!). New: pad with alphanumeric "0" so result like
    "100" / "1a0" is valid AND keeps the raw id readable.
    0.40.4 self-review: chromadb 1.5.8 also rejects consecutive periods (..)
    in collection names — collapse multi-dots to single underscore so
    path-traversal-shaped ws_ids ("../../foo") don't produce noisy validation
    errors. Net effect: "../../foo" → "_foo" → "_foo00" / wrapped → "x_foo".
    """
    s = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw)
    s = re.sub(r"\.{2,}", "_", s)  # collapse `..+` to `_` (chromadb rejects)
    s = s[:50]  # cap 50 chars (leave room for prefix 'kb_ws_' / 'kb_team_')
    while len(s) < 3:
        s = s + "0"  # pad to ≥ 3 char with alphanum (not `_`)
    # Boundary: if first/last char is non-alnum (`_` / `-` / `.`), wrap with `x`.
    if s and not s[0].isalnum():
        s = "x" + s
    if s and not s[-1].isalnum():
        s = s + "x"
    return s


def _sanitize_index_version(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw)
    s = re.sub(r"\.{2,}", "_", s)
    s = s[:24]
    while len(s) < 3:
        s = s + "0"
    if s and not s[0].isalnum():
        s = "x" + s
    if s and not s[-1].isalnum():
        s = s + "x"
    return s


def _sanitize_id_capped(raw: str, max_len: int) -> str:
    s = _sanitize_id(raw)[:max_len]
    while len(s) < 3:
        s = s + "0"
    if s and not s[0].isalnum():
        s = "x" + s[1:]
    if s and not s[-1].isalnum():
        s = s[:-1] + "x"
    return s


class KbSearchCandidates(list[dict[str, Any]]):
    """``KbStore.search`` 的候选列表；``skipped_collections`` = 因超时/出错没检索到的集合数。

    >0 表示结果不完整（某个范围没查到），caller 不能据此断言知识库里没有相关内容。
    """

    skipped_collections: int = 0

    @property
    def partial(self) -> bool:
        return self.skipped_collections > 0


class KbStore:
    """chromadb wrap. per-ws (含 personal+workspace) + per-team collection.

    Caller 一律传 KbFileDTO 单参数 (P0-O1) 或 ScopeContext (search).
    """

    def __init__(self, persist_dir: str, index_version: str | None = None):
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._index_version = _sanitize_index_version(index_version) if index_version else None

    @property
    def index_version(self) -> str:
        return self._index_version or ""

    @staticmethod
    def _current_chunks_where(file: KbFileDTO) -> dict[str, Any]:
        terms: list[dict[str, Any]] = [{"file_id": file.file_id}, {"is_active": True}]
        if file.active_generation_id:
            terms.append({"generation_id": file.active_generation_id})
        return {"$and": terms}

    async def verify_generation(self, file: KbFileDTO, generation_id: str, expected: int) -> bool:
        coll = await self._file_collection(file)
        if coll is None or expected < 1:
            return False
        where = {"$and": [{"file_id": file.file_id}, {"generation_id": generation_id}, {"is_active": True}]}
        got = await asyncio.to_thread(coll.get, where=where, include=[], limit=expected + 1)
        ids = got.get("ids") or []
        if len(ids) != expected:
            return False
        sample = await asyncio.to_thread(coll.get, ids=[ids[0]], where=where, include=["documents", "embeddings"])
        documents = sample.get("documents") or []
        embeddings = sample.get("embeddings")
        return bool(
            documents
            and isinstance(documents[0], str)
            and documents[0].strip()
            and embeddings is not None
            and len(embeddings) == 1
            and len(embeddings[0]) > 0
        )

    async def delete_generation(self, file: KbFileDTO, generation_id: str) -> None:
        coll = await self._file_collection(file)
        if coll is not None:
            await asyncio.to_thread(coll.delete, where={"$and": [{"file_id": file.file_id}, {"generation_id": generation_id}]})

    # ─── collection routing ───────────────────────────────
    # chromadb calls block synchronously (first collection open loads HNSW index), stalling the entire gateway on the event
    # loop; all async entry points below use to_thread. Reads fetch only existing collections, never create empty ones via search/count.

    def _ws_collection_name(self, workspace_id: str) -> str:
        if self._index_version:
            return f"kb_{self._index_version}_ws_{_sanitize_id_capped(workspace_id, 30)}"
        return f"kb_ws_{_sanitize_id(workspace_id)}"

    def _team_collection_name(self, team_id: str) -> str:
        if self._index_version:
            return f"kb_{self._index_version}_team_{_sanitize_id_capped(team_id, 30)}"
        return f"kb_team_{_sanitize_id(team_id)}"

    def _collection_name_for_routing(self, scope: str, workspace_id: str, team_id: str) -> str:
        """[P0-P2] 显式 3 个 routing key 选 collection. 不依赖 file 字段 (refreshed file 是新值)."""
        if scope == "team":
            return self._team_collection_name(team_id)
        return self._ws_collection_name(workspace_id)

    def _open_collection(self, name: str, *, create: bool) -> Any | None:
        """同步：create=True 取或建；create=False 仅在已存在时返回，否则 None。"""
        if create:
            return self._client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
        try:
            return self._client.get_collection(name=name)
        except (chromadb.errors.NotFoundError, ValueError):
            return None

    def _coll_for_routing(self, scope: str, workspace_id: str, team_id: str) -> Any:
        """同步取或建（写路径；async caller 须 to_thread）。"""
        return self._open_collection(self._collection_name_for_routing(scope, workspace_id, team_id), create=True)

    def _coll_for_file(self, file: KbFileDTO) -> Any:
        """文件根据 scope 走 ws collection (personal+workspace) 或 team collection (team)."""
        return self._coll_for_routing(file.scope, file.workspace_id, file.team_id)

    async def _routing_collection(self, scope: str, workspace_id: str, team_id: str, *, create: bool) -> Any | None:
        name = self._collection_name_for_routing(scope, workspace_id, team_id)
        return await asyncio.to_thread(self._open_collection, name, create=create)

    async def _file_collection(self, file: KbFileDTO, *, create: bool = False) -> Any | None:
        return await self._routing_collection(file.scope, file.workspace_id, file.team_id, create=create)

    # ─── chunk operations ─────────────────────────────────

    async def add_chunks(self, file: KbFileDTO, chunks: list[KbChunk]) -> None:
        """[P0-O1] caller 传 KbFileDTO, 内部从 file.scope 路由 collection."""
        if not chunks:
            return
        coll = await self._file_collection(file, create=True)
        ids = [c.chunk_id for c in chunks]

        # P1-5: embeddings must be all None or all non-None (mixed → chromadb len mismatch error)
        embeddings_present = [c.embedding is not None for c in chunks]
        if any(embeddings_present) and not all(embeddings_present):
            raise ValueError(
                "add_chunks: embedding mixed None/non-None — must all be set or all be None",
            )
        embeddings_arg = [c.embedding for c in chunks] if all(embeddings_present) else None

        # P0-A: reparse same chunk_id; upsert avoids duplicate id error
        await asyncio.to_thread(
            coll.upsert,
            ids=ids,
            documents=[c.content for c in chunks],
            embeddings=embeddings_arg,
            metadatas=[c.metadata.to_dict() for c in chunks],
        )

    async def search(
        self,
        ctx: ScopeContext,
        query_embedding: list[float],
        top_k: int = DEFAULT_SEARCH_TOP_K,
        scope_filter: list[str] | None = None,
        allowed_file_ids: tuple[str, ...] | None = None,
    ) -> KbSearchCandidates:
        """[P0-O1] caller 传 ScopeContext.

        P0-N1: 跨 2 collection (ws + team) 查, metadata filter scope 权限.
        P0-O3: 自动加 is_active=True filter (chromadb 不接受 None filter).
        P1-Q3: 3s timeout 防大 coll HNSW load 慢致 user 卡. 超时或出错的集合被跳过，
        计入返回值的 ``skipped_collections``（结果不完整，caller 须如实告知）；
        不存在的集合（从没上传过）不算跳过。
        """
        all_results = KbSearchCandidates()
        if allowed_file_ids == ():
            return all_results
        allowed_scopes = (
            {"personal", "space", "team"}
            if scope_filter is None
            else {("space" if s == "workspace" else s) for s in scope_filter if s in {"personal", "space", "workspace", "team"}}  # B3/c1: both→space
        )
        if not allowed_scopes:
            return []

        # P0-O3 + spike 2: nested $and/$or works in chromadb 1.5.8
        ws_filter: dict[str, Any] = {
            "$and": [
                {"is_active": True},
                {
                    "$or": [
                        {"scope": {"$in": ["space", "workspace"]}},  # Batch3/c1 accept both during migration
                        {
                            "$and": [
                                {"scope": "personal"},
                                {"scope_owner": ctx.user_id},
                            ]
                        },
                    ]
                },
            ]
        }
        workspace_only_filter: dict[str, Any] = {
            "$and": [{"is_active": True}, {"scope": {"$in": ["space", "workspace"]}}]  # Batch3/c1
        }
        personal_only_filter: dict[str, Any] = {
            "$and": [
                {"is_active": True},
                {"scope": "personal"},
                {"scope_owner": ctx.user_id},
            ]
        }
        team_filter: dict[str, Any] = {"$and": [{"is_active": True}, {"scope": "team"}]}
        # Knowledge bases (phase 1 §2.2): drop chunks of the named Space/Team knowledge bases the
        # caller cannot use before the vector window is filled, or a members-only knowledge base
        # would crowd out readable results. Chunks without the key (written before it existed)
        # match `$nin`, and every one of them is in a default knowledge base. With nothing to
        # exclude the clause is left out, so the query is exactly today's. Personal chunks need
        # no clause: their filter already keeps the caller's own.
        levels = getattr(ctx, "knowledge_levels", None)
        known = isinstance(levels, KnowledgeLevels)
        space_excluded = levels.excluded_in("space", ctx.workspace_id) if known and ctx.workspace_id else ()
        team_excluded = levels.excluded_in("team", ctx.team_id) if known and ctx.team_id else ()
        ws_filter = _without_knowledge_bases(ws_filter, space_excluded)
        workspace_only_filter = _without_knowledge_bases(workspace_only_filter, space_excluded)
        team_filter = _without_knowledge_bases(team_filter, team_excluded)
        # One knowledge base's own deep search: its home's scope only, then its chunk key (a named
        # one) or every key but the home's named ones (a default, whose old chunks carry no key).
        selected = getattr(ctx, "knowledge_base_search", None)
        if selected is not None:
            home_scope = "space" if selected.scope in ("space", "workspace") else selected.scope
            allowed_scopes = allowed_scopes & {home_scope}
            if not allowed_scopes:
                return all_results
            workspace_only_filter = _only_knowledge_base(_without_knowledge_bases(workspace_only_filter, selected.excluded), selected.only)
            personal_only_filter = _only_knowledge_base(_without_knowledge_bases(personal_only_filter, selected.excluded), selected.only)
            team_filter = _only_knowledge_base(_without_knowledge_bases(team_filter, selected.excluded), selected.only)
        # An Agent's selected knowledge bases: per home, its selected named knowledge bases (`$in`),
        # or, with the home's default selected, every chunk but those of the home's unselected named
        # ones (`$nin`), so the default's old chunks without the key still match. Homes with nothing
        # selected are left out. The SQL post-filter (the file's current knowledge base) stays the authority.
        selection = getattr(ctx, "knowledge_base_selection", None)
        if selection is not None:
            allowed_scopes = allowed_scopes & {home for home in ("personal", "space", "team") if selection.home(home) is not None}
            if not allowed_scopes:
                return all_results
            space_part = _selected_conditions([{"scope": {"$in": ["space", "workspace"]}}], selection.space)
            personal_part = _selected_conditions([{"scope": "personal"}, {"scope_owner": ctx.user_id}], selection.personal)
            team_part = _selected_conditions([{"scope": "team"}], selection.team)
            workspace_only_filter = {"$and": [{"is_active": True}, *space_part]}
            personal_only_filter = {"$and": [{"is_active": True}, *personal_part]}
            ws_filter = {"$and": [{"is_active": True}, {"$or": [_all_of(space_part), _all_of(personal_part)]}]}
            team_filter = {"$and": [{"is_active": True}, *team_part]}

        # 0.40.4 Bug 8 step a: ALSO target ws collections where the user
        # has personal-scope chunks (uploaded when active ws was different,
        # since datarc ws ≠ Bowhead KB scope). Personal-only filter (workspace
        # chunks from OTHER ws stay isolated). De-dup against active ws.
        targets: list[Any] = []
        if "space" in allowed_scopes and "personal" in allowed_scopes:
            targets.append((self._ws_collection_name(ctx.workspace_id), ws_filter, "ws"))
        elif "space" in allowed_scopes:
            targets.append(
                (
                    self._ws_collection_name(ctx.workspace_id),
                    workspace_only_filter,
                    "ws",
                )
            )
        elif "personal" in allowed_scopes:
            targets.append(
                (
                    self._ws_collection_name(ctx.workspace_id),
                    personal_only_filter,
                    "ws_personal",
                )
            )
        if "team" in allowed_scopes:
            targets.append((self._team_collection_name(ctx.team_id), team_filter, "team"))
        seen_ws = {ctx.workspace_id}
        for extra_ws in ctx.extra_personal_workspaces or ():
            if "personal" not in allowed_scopes:
                break
            # Allow empty-string ws (legacy upload path; chunks live in
            # `kb_ws_{sanitize('')}` collection). Only skip exact dups.
            if extra_ws in seen_ws:
                continue
            seen_ws.add(extra_ws)
            targets.append(
                (self._ws_collection_name(extra_ws), personal_only_filter, "ws_personal_extra"),
            )
        for coll_name, coll_filter, coll_kind in targets:
            if allowed_file_ids is not None:
                coll_filter = {"$and": [coll_filter, {"file_id": {"$in": list(allowed_file_ids)}}]}
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._query_if_exists,
                        coll_name,
                        query_embeddings=[query_embedding],
                        n_results=top_k,
                        where=coll_filter,
                    ),
                    timeout=SEARCH_TIMEOUT_S,
                )
                ids_batch = results.get("ids") or [[]]
                docs_batch = results.get("documents") or [[]]
                metas_batch = results.get("metadatas") or [[]]
                dists_batch = results.get("distances") or [[]]
                for i, chunk_id in enumerate(ids_batch[0]):
                    all_results.append(
                        {
                            "chunk_id": chunk_id,
                            "content": docs_batch[0][i] if docs_batch[0] else "",
                            "metadata": metas_batch[0][i] if metas_batch[0] else {},
                            "distance": dists_batch[0][i] if dists_batch[0] else 0.0,
                        }
                    )
            except chromadb.errors.NotFoundError:
                # 0.40.2 Bug 4: chromadb 1.5.8 removed `InvalidCollectionException`
                # — collection-missing now surfaces as NotFoundError exclusively.
                # Phase 4 dogfood surfaced this when Lead spawned 6 kb_search
                # calls in parallel: 3 hit the collection (OK), 3 raised
                # AttributeError at the except clause (since the legacy name
                # doesn't exist anymore in the imported errors module).
                continue
            except TimeoutError:
                emit_audit(
                    "kb.search_timeout",
                    {
                        "workspace_id": ctx.workspace_id,
                        "coll_kind": coll_kind,
                        "timeout_s": SEARCH_TIMEOUT_S,
                    },
                )
                all_results.skipped_collections += 1
                continue
            except Exception as exc:
                logger.warning("kb store.search %s coll error: %s", coll_kind, exc)
                all_results.skipped_collections += 1
                continue

        # merge: ascending distance, take top_k
        all_results.sort(key=lambda r: r["distance"])
        del all_results[top_k:]
        return all_results

    def _query_if_exists(self, name: str, **query: Any) -> dict[str, Any]:
        """同步：集合存在才查询（检索不建空集合）；不存在返回空结果。"""
        coll = self._open_collection(name, create=False)
        if coll is None:
            return {}
        return coll.query(**query)

    # ─── soft delete (P0-B + P0-O3) ───────────────────────

    async def soft_delete_file(self, file: KbFileDTO) -> int:
        """P0-B + P0-O3: is_active=False + deleted_at_iso 设 ISO timestamp.

        chunk content 仍在 chromadb (cite popover 仍可 fetch). 90 day 后 cron hard delete.
        """
        coll = await self._file_collection(file)
        if coll is None:
            return 0
        chunks = await asyncio.to_thread(coll.get, where={"file_id": file.file_id})
        ids = chunks.get("ids") or []
        if not ids:
            return 0
        now_iso = datetime.now(UTC).isoformat()
        old_metas = chunks.get("metadatas") or []
        new_metas = [{**m, "is_active": False, "deleted_at_iso": now_iso} for m in old_metas]
        await asyncio.to_thread(coll.update, ids=ids, metadatas=new_metas)
        return len(ids)

    async def hard_delete_file(self, file: KbFileDTO) -> int:
        """P0-B: 真 chromadb delete. 90 day grace cron 或 GDPR 走. 不可恢复."""
        coll = await self._file_collection(file)
        if coll is None:
            return 0
        # Count first
        chunks = await asyncio.to_thread(coll.get, where={"file_id": file.file_id})
        n = len(chunks.get("ids") or [])
        await asyncio.to_thread(coll.delete, where={"file_id": file.file_id})
        return n

    async def set_chunks_knowledge_base(self, file: KbFileDTO, knowledge_base_id: str) -> int:
        """Write the knowledge base key into every chunk of a file that lacks it or carries another; returns the count.

        A partial metadata update merges the key, so the rest of each chunk's metadata, its text and
        its embedding stay untouched. It never creates a collection.
        """
        coll = await self._file_collection(file)
        if coll is None:
            return 0
        got = await asyncio.to_thread(coll.get, where={"file_id": file.file_id}, include=["metadatas"])
        metadatas = got.get("metadatas") or []
        stale = [
            chunk_id
            for index, chunk_id in enumerate(got.get("ids") or [])
            if not isinstance(metadatas[index] if index < len(metadatas) else None, dict)
            or metadatas[index].get("knowledge_base_id") != knowledge_base_id
        ]
        if stale:
            await asyncio.to_thread(coll.update, ids=stale, metadatas=[{"knowledge_base_id": knowledge_base_id} for _ in stale])
        return len(stale)

    async def count_chunks(self, file: KbFileDTO) -> int:
        """P0-D: parse_done 前 sanity check.

        P0-O3: filter is_active=True 替 deleted_at=None.
        """
        coll = await self._file_collection(file)
        if coll is None:
            return 0
        chunks = await asyncio.to_thread(
            coll.get,
            where=self._current_chunks_where(file),
        )
        return len(chunks.get("ids") or [])

    async def inspect_active_index(self, file: KbFileDTO, *, limit: int = 1000) -> IndexObservation:
        """Bounded diagnostics: never create collections or assert legacy byte binding."""
        from bowhead_core.kb.health import IndexObservation

        name = self._collection_name_for_routing(file.scope, file.workspace_id, file.team_id)
        try:
            coll = await asyncio.to_thread(self._client.get_collection, name=name)
        except chromadb.errors.NotFoundError:
            return IndexObservation(state="missing_or_corrupt", reason="collection_missing", observed_chunks=0)
        where = self._current_chunks_where(file)
        got = await asyncio.to_thread(coll.get, where=where, include=[], limit=limit + 1)
        ids = got.get("ids") or []
        if len(ids) > limit:
            return IndexObservation(reason="inspection_limit", observed_chunks=len(ids), count_limited=True)
        if len(ids) != file.chunks_count:
            return IndexObservation(state="missing_or_corrupt", reason="chunk_count_mismatch", observed_chunks=len(ids))
        if not ids:
            return IndexObservation(reason="empty_index", observed_chunks=0)
        sample = await asyncio.to_thread(coll.get, ids=[ids[0]], where=where, include=["documents"])
        documents = sample.get("documents") or []
        if not documents or not isinstance(documents[0], str) or not documents[0].strip():
            return IndexObservation(state="missing_or_corrupt", reason="sample_unreadable", observed_chunks=len(ids))
        return IndexObservation(reason="legacy_chunks_readable", observed_chunks=len(ids))

    async def get_chunks_for_file(self, file: KbFileDTO) -> list[tuple[str, str]]:
        return [(cid, content) for cid, content, _ in await self.list_active_chunks(file)]

    async def preview_current_chunks(self, file: KbFileDTO) -> list[tuple[str, str, dict[str, Any]]]:
        """Read only the first three published chunks for bounded source navigation."""
        if not file.active_generation_id:
            return []
        coll = await self._file_collection(file)
        if coll is None:
            return []
        where = {"$and": [self._current_chunks_where(file), {"chunk_index": {"$gte": 0}}, {"chunk_index": {"$lte": 3}}]}
        got = await asyncio.to_thread(coll.get, where=where, include=["documents", "metadatas"], limit=3)
        docs, metas = got.get("documents") or [], got.get("metadatas") or []
        rows = [
            (cid, docs[i], metas[i])
            for i, cid in enumerate(got.get("ids") or [])
            if i < len(docs)
            and i < len(metas)
            and isinstance(metas[i], dict)
            and metas[i].get("generation_id") == file.active_generation_id
            and isinstance(docs[i], str)
        ]
        return sorted(rows, key=lambda row: int(row[2]["chunk_index"]))

    async def sample_current_chunks(self, file: KbFileDTO) -> list[tuple[str, str, dict[str, Any]]]:
        """Read at most twelve distributed published chunks without scanning a file."""
        if not file.active_generation_id or file.chunks_count < 1:
            return []
        coll = await self._file_collection(file)
        if coll is None:
            return []
        count = min(12, file.chunks_count)
        positions = sorted({1 + round(index * (file.chunks_count - 1) / max(1, count - 1)) for index in range(count)})
        where = {"$and": [self._current_chunks_where(file), {"chunk_index": {"$in": positions}}]}
        got = await asyncio.to_thread(coll.get, where=where, include=["documents", "metadatas"], limit=12)
        rows = [
            (identity, text, metadata)
            for identity, text, metadata in zip(got.get("ids") or [], got.get("documents") or [], got.get("metadatas") or [], strict=True)
            if isinstance(text, str) and isinstance(metadata, dict) and metadata.get("generation_id") == file.active_generation_id
        ]
        return sorted(rows, key=lambda row: row[2].get("chunk_index", 0))

    async def get_ordered_chunks_for_file(self, file: KbFileDTO) -> list[tuple[str, str, int]]:
        return [(c.chunk_id, c.content, c.chunk_index) for c in await self.get_ordered_source_chunks(file)]

    async def get_ordered_source_chunks(self, file: KbFileDTO) -> list[SourceChunk]:
        """A file's current chunks in ``chunk_index`` order, with the page the loader recorded for each."""
        rows = await self.list_active_chunks(file)
        ordered = []
        for i, (cid, content, metadata) in enumerate(rows):
            try:
                index = int(metadata.get("chunk_index", i + 1))
            except (TypeError, ValueError):
                index = i + 1
            ordered.append(SourceChunk(cid, content, index, chunk_metadata_page(metadata.get("page"))))
        return sorted(ordered, key=lambda chunk: chunk.chunk_index)

    async def list_active_chunks(self, file: KbFileDTO, *, chunk_id: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
        """``[(chunk_id, content, metadata)]`` of a file's active chunks (storage order).

        ``chunk_id`` 给定时只取这一个分块（且必须属于该文件、仍 active），供单条引用卡片使用，
        不必下载整份文件的全部分块。
        """
        coll = await self._file_collection(file)
        if coll is None:
            return []
        # An explicit old chunk ID is a history read, never silently remapped.
        where = {"$and": [{"file_id": file.file_id}, {"is_active": True}]} if chunk_id else self._current_chunks_where(file)
        if chunk_id is not None:
            got = await asyncio.to_thread(coll.get, ids=[chunk_id], where=where, include=["documents", "metadatas"])
        else:
            got = await asyncio.to_thread(coll.get, where=where, include=["documents", "metadatas"])
        ids = got.get("ids") or []
        docs = got.get("documents") or []
        metas = got.get("metadatas") or []
        return [
            (
                cid,
                docs[i] if i < len(docs) else "",
                metas[i] if i < len(metas) and isinstance(metas[i], dict) else {},
            )
            for i, cid in enumerate(ids)
            if chunk_id is not None or (((metas[i] if i < len(metas) else {}) or {}).get("generation_id") or None) == file.active_generation_id
        ]

    # ─── scope change cross-collection migration (P0-O2 + P0-P2) ──

    async def update_chunks_scope(
        self,
        file_id: str,
        old_scope: str,
        old_scope_id: str,
        old_workspace_id: str,
        old_team_id: str,
        new_scope: str,
        new_scope_id: str,
        new_workspace_id: str,
        new_team_id: str,
        new_knowledge_base_id: str | None = None,
    ) -> int:
        """[P0-O2 + P0-P2] scope 改变跨 collection 物理迁移.

        Chunks get ``new_knowledge_base_id`` as their knowledge base key, or the target scope's
        default when it is None (today's scope change). Embeddings are copied, never recomputed.

        - 同 logical 域 (e.g. ws 内 personal ↔ workspace): in-place metadata update
        - 跨 logical 域 (workspace ↔ team / 跨 ws): fetch chunks (含 embeddings) → 加新 coll → 删旧 coll
        - 任一步失败都抛 ``ScopeChangeMigrationError``，并尽力把分块恢复到迁移前的样子：
          新 coll 里已写入的删掉，旧 coll 里已删掉的按原 metadata 写回（数据都在内存里）。
          caller 据此回滚 SQL 范围，文件整体回到旧范围，不留半迁移状态。
        """

        def _fail(stage: str, exc: BaseException) -> ScopeChangeMigrationError:
            emit_audit("kb.scope_change_migration_failed", {"file_id": file_id, "stage": stage, "error": str(exc)})
            return ScopeChangeMigrationError(file_id, stage, str(exc))

        try:
            old_coll = await self._routing_collection(old_scope, old_workspace_id, old_team_id, create=False)
            if old_coll is None:
                return 0
            new_coll = await self._routing_collection(new_scope, new_workspace_id, new_team_id, create=True)
            chunks = await asyncio.to_thread(
                old_coll.get,
                where={"file_id": file_id},
                include=["embeddings", "documents", "metadatas"],
            )
        except Exception as exc:
            raise _fail("read_old_coll", exc) from exc
        ids = chunks.get("ids") or []
        if not ids:
            return 0

        old_metas = chunks.get("metadatas") or []
        # Today's scope change moves the file to the target scope's default knowledge base.
        try:
            knowledge_base = {"knowledge_base_id": new_knowledge_base_id or default_for_scope(new_scope, new_scope_id, new_team_id)}
        except ValueError:
            knowledge_base = {}
        new_metas = [
            {
                **m,
                "scope": new_scope,
                "scope_owner": new_scope_id,
                "workspace_id": new_workspace_id,
                "team_id": new_team_id,
                **knowledge_base,
            }
            for m in old_metas
        ]

        if old_coll.name == new_coll.name:
            # Same collection: in-place metadata update (restore original metadata on failure)
            try:
                await asyncio.to_thread(old_coll.update, ids=ids, metadatas=new_metas)
            except Exception as exc:
                await self._restore_after_failed_migration(file_id, old_coll, None, ids, chunks)
                raise _fail("update_in_place", exc) from exc
            return len(ids)

        # Cross-collection: physical migration. Add new → delete old (avoid deleting old before failed add)
        try:
            await asyncio.to_thread(
                new_coll.upsert,
                ids=ids,
                embeddings=chunks.get("embeddings"),
                documents=chunks.get("documents"),
                metadatas=new_metas,
            )
        except Exception as exc:
            await self._restore_after_failed_migration(file_id, None, new_coll, ids, chunks)
            raise _fail("add_to_new_coll", exc) from exc

        try:
            await asyncio.to_thread(old_coll.delete, ids=ids)
        except Exception as exc:
            # Old collection may be partially deleted: restore original chunks, then delete new collection copies
            await self._restore_after_failed_migration(file_id, old_coll, new_coll, ids, chunks)
            raise _fail("delete_from_old_coll", exc) from exc

        emit_audit(
            "kb.chunks_migrated_cross_collection",
            {
                "file_id": file_id,
                "old": f"{old_scope}:{old_scope_id}",
                "new": f"{new_scope}:{new_scope_id}",
                "count": len(ids),
            },
        )
        return len(ids)

    async def _restore_after_failed_migration(
        self,
        file_id: str,
        old_coll: Any | None,
        new_coll: Any | None,
        ids: list[str],
        original: dict[str, Any],
    ) -> None:
        """迁移失败后的补偿：旧 coll 按原样写回、新 coll 删掉副本。best-effort，失败只记审计。"""
        try:
            if old_coll is not None:
                await asyncio.to_thread(
                    old_coll.upsert,
                    ids=ids,
                    embeddings=original.get("embeddings"),
                    documents=original.get("documents"),
                    metadatas=original.get("metadatas"),
                )
            if new_coll is not None:
                await asyncio.to_thread(new_coll.delete, ids=ids)
        except Exception as exc:
            logger.warning("kb scope change restore failed file_id=%s err=%s", file_id, exc)
            emit_audit("kb.scope_change_migration_partial", {"file_id": file_id, "stage": "restore", "error": str(exc)})
