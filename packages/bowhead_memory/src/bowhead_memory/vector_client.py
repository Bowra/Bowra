"""Datarc Embedding Service 客户端.

封装 https://<host>/ 的 REST API（ChromaDB 背书）：
    - collection 增删改查
    - 文档 add / fetch / query / delete / count
    - 语义查询 /query-documents

production 约束：
    - Async HTTPX2 client; base_url can change at runtime through Gateway lifespan injection.
    - 默认 30s timeout
    - 非 2xx 抛 VectorDBError；保留 body 便于排查
    - access_token 可选（header `Authorization: Bearer <t>`）；
      dev 环境可能靠 URL 本身作秘钥

两种 transport 模式：
    - **直连**（默认）：base_url = embedding service 根（URL 即秘钥），每个操作一个 path
      （`POST /query-documents` / `GET /list-collections` …）。
    - **datarc 代理**（``datarc_proxy=True``，embed-via-openapi）：base_url = datarc OpenAPI 根
      （如 ``https://demo.datarc.cn``），access_token = team 连接密钥（必须 Bearer）。所有操作
      经 ``/openapi/v1/embed/`` 透传：POST 信封 ``{"uri": <op>, "data": {...}}``；
      health / list-collections / get-collection 上游只收 GET → 走 ``GET ?uri=<op>&k=v``
      （代理透传 HTTP method，实测 POST 这三个会被上游 405）。响应包 ``{errcode, errmsg, data}``
      壳，解壳后返回 data（与直连同 shape，下游零改动）。代理模式**只读**：concept-resolve
      只需 query/fetch/list/get/count/health；create/delete/rename 等写操作直接抛
      VectorDBError，使 team 连接密钥永远无法变更向量库（recreate-db 类风险物理隔离）。
      连接密钥未开 embed 权限时 datarc 返 HTTP 403 ``{"detail": ...}``（FastAPI 风格非
      errcode 壳）→ 映射为带权限提示的 VectorDBError(status=403)。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

logger = logging.getLogger(__name__)

# Datarc proxy endpoint (Datarc requires URLs to end with `/`).
_PROXY_PATH = "/openapi/v1/embed/"

# Operations allowed in proxy mode (reads and health). Anything outside this set is a write operation and raises an error.
# Upstream accepts only GET for health/list-collections/get-collection and POST for the rest, matching
# the existing method chosen by each client method; the proxy branch can forward the requested method unchanged.
_PROXY_READONLY_OPS = frozenset(
    {
        "health",
        "version",
        "list-collections",
        "get-collection",
        "count-documents",
        "query-documents",
        "fetch-documents",
    }
)


# ─── Datarc collection naming conventions ──────────────────────────────────────────
# All collections maintained automatically by Datarc are named `v1_<tenant>_p<workspace_id>`;
# dimension-value collections append `_keywords` (Bowhead does not read them directly; dimension values use Datarc `/keywords/recall/`).
# The tenant segment is the Datarc **client** (`core_client.username`),
# which differs for each on-premises instance (official example: `v1_client1_p1`); it is not a constant and must not be hardcoded.
# Cross-module callers must use this helper instead of assembling strings manually, preventing typos and keeping version changes in one place.
def schema_collection(tenant: str, workspace_id: str | int) -> str:
    """指标 / 维度 / 时间字段的 schema 向量 collection."""
    return f"v1_{tenant}_p{workspace_id}"


# `v1_{tenant}_p{project}`: tenant may contain underscores; project does not (Datarc uses a numeric ID).
# Match only the schema collection itself; subcollections such as `_keywords` / `_sop` do not participate in tenant resolution
# (the same tenant/project repeats, providing no additional evidence).
_SCHEMA_COLLECTION_RE = re.compile(r"^v1_(?P<tenant>.+)_p(?P<project>[^_]+)$")


def parse_schema_collection(name: str) -> tuple[str, str] | None:
    """`v1_{tenant}_p{project}` → (tenant, project)；不是 schema coll 本体则 None."""
    m = _SCHEMA_COLLECTION_RE.match(name or "")
    if m is None:
        return None
    return m.group("tenant"), m.group("project")


def collection_names(raw: Iterable[Any]) -> list[str]:
    """把 ``list-collections`` 的返回归一成名字列表.

    实测 datarc 代理返 ``[{"name": ..., "metadata": {...}}, ...]``，但直连老接口
    历史上返过裸字符串数组 —— 两种都吃，避免形状漂移把发现逻辑静默打成空。
    """
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if isinstance(name, str) and name:
            out.append(name)
    return out


def resolve_tenant_from_collections(
    names: Iterable[str],
    *,
    project_ids: Sequence[str],
) -> str | None:
    """从 collection 名反查本连接的 tenant 段；判不准返 None（绝不猜）.

    **为什么不能只按 project 名匹配**：一个向量服务可以同时托管多个 datarc client
    （2026-08-17 实测某私有化实例上 ``v1_dev_p1`` 与 ``v1_rc_p1`` 并存）。project id
    在 client 之间会撞号，猜错 = 拿另一个客户的语义词典当本客户的答案，是**串户读取**
    不只是查不到。所以判据用连接自己可见的 workspace 集合（密钥只能看到本 client 的
    工作区）：覆盖得最多的那个 tenant 才是它；并列或零覆盖一律返 None，由调用方降级成
    「embed 不可用」，宁可不出答案也不出错答案。
    """
    wanted = {str(p) for p in project_ids if str(p or "")}
    if not wanted:
        return None
    by_tenant: dict[str, set[str]] = {}
    for name in names:
        parsed = parse_schema_collection(name)
        if parsed is None:
            continue
        tenant, project = parsed
        by_tenant.setdefault(tenant, set()).add(project)
    scored = sorted(
        ((len(projects & wanted), tenant) for tenant, projects in by_tenant.items()),
        reverse=True,
    )
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # A tie makes resolution ambiguous.
    return scored[0][1]


class VectorDBError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class VectorClient:
    base_url: str
    access_token: str | None = None
    timeout_sec: int = 30
    # embed-via-openapi: True uses the Datarc `/openapi/v1/embed/` proxy (see the module docstring).
    # In this mode, base_url is the Datarc OpenAPI root and access_token is the required team connection key.
    datarc_proxy: bool = False
    # embed-via-openapi: True means an admin-supplied (untrusted) base_url; revalidate the public IP on every outbound request
    # (DNS rebinding defense, aligned with DatarcClient R7.4/review P1-b). FAIL CLOSED:
    # raise if guard construction fails; never silently fall back to an unguarded connection.
    ssrf_guard: bool = False
    # Datarc connections in Team settings may use intranet HTTP in local deployments.
    # These two opt-ins must match the BI-side ``DatarcClientFactory``: both use the **same connection
    # configured by the admin**. Divergent policies allow BI through but reject embedding at the transport layer, leaving concept
    # resolution silently empty (the second customer verified on 2026-08-18 exhibited this pattern).
    # The collection tenant segment for this connection (`v1_{tenant}_p{project}`), populated by
    # ``EmbedProxyClientFactory`` after probing the collection list; None means unresolved.
    # Callers must degrade to "embedding unavailable" instead of constructing names from defaults (a wrong name reads another tenant's data).
    datarc_tenant: str | None = None

    def __post_init__(self) -> None:
        if self.datarc_proxy and not self.access_token:
            raise VectorDBError("datarc_proxy mode requires access_token (team connection key)")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        _client_kwargs: dict[str, Any] = {
            "base_url": self.base_url.rstrip("/"),
            "headers": headers,
            "timeout": self.timeout_sec,
            # Neither proxy nor direct mode needs redirects; redirecting an admin-supplied base_url to an internal network is SSRF.
            "follow_redirects": False,
        }
        # Lazy import: memory cannot depend directly on core (the dependency direction is core→memory). Real deployments include core
        # (only the gateway sets ssrf_guard=True). Raise on failure, following the same FAIL CLOSED rationale as bi_bridge.
        if self.ssrf_guard:
            from bowhead_core.ssrf_guard import build_guarded_async_client

            self._http = build_guarded_async_client(
                **_client_kwargs,
            )
        else:
            self._http = httpx2.AsyncClient(**_client_kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> VectorClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ─── internal ──────────────────────────────────────────
    async def _req(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.datarc_proxy:
            return await self._req_via_proxy(method, path, json=json, params=params)
        try:
            r = await self._http.request(method, path, json=json, params=params)
        except httpx2.HTTPError as e:
            raise VectorDBError(f"{method} {path} network error: {e}") from e
        if r.status_code // 100 != 2:
            raise VectorDBError(
                f"{method} {path} returned {r.status_code}",
                status=r.status_code,
                body=r.text[:500],
            )
        try:
            return r.json()
        except ValueError as e:
            raise VectorDBError(
                f"{method} {path} invalid JSON",
                status=r.status_code,
                body=r.text[:500],
            ) from e

    async def _req_via_proxy(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """embed-via-openapi：把直连 (method, /path) 翻译成 datarc 代理调用并解壳.

        method 原样转发（代理透传 HTTP method 给上游）：GET 类 → `?uri=<op>&k=v`；
        POST 类 → 信封 `{"uri": <op>, "data": <json>}`。响应 `{errcode, errmsg, data}`
        解壳后返 data —— 与直连响应同 shape，所有上层方法零改动复用。
        """
        op = path.strip("/")
        if op not in _PROXY_READONLY_OPS:
            raise VectorDBError(
                f"vector op '{op}' blocked: datarc-proxy client is read-only (create/delete/rename/add go via the direct embed connection only)",
            )
        try:
            if method.upper() == "GET":
                r = await self._http.request(
                    "GET",
                    _PROXY_PATH,
                    params={"uri": op, **(params or {})},
                )
            else:
                r = await self._http.request(
                    "POST",
                    _PROXY_PATH,
                    json={"uri": op, "data": json or {}},
                )
        except httpx2.HTTPError as e:
            # Align with R7.5: report only the exception class for network errors (httpx2 error text may include the full URL).
            raise VectorDBError(
                f"{method} {op} (datarc embed proxy) network error: {type(e).__name__}",
            ) from e
        if r.status_code == 403:
            # FastAPI-style {"detail": "无当前接口权限"} (without an errcode envelope) means the connection key lacks embed permission.
            raise VectorDBError(
                f"datarc embed proxy 403 for '{op}': connection key lacks embed permission (grant it on the datarc side)",
                status=403,
                body=r.text[:200],
            )
        if r.status_code // 100 != 2:
            raise VectorDBError(
                f"datarc embed proxy returned {r.status_code} for '{op}'",
                status=r.status_code,
                body=r.text[:500],
            )
        try:
            payload = r.json()
        except ValueError as e:
            raise VectorDBError(
                f"datarc embed proxy invalid JSON for '{op}'",
                status=r.status_code,
                body=r.text[:500],
            ) from e
        errcode = int(payload.get("errcode", 0) or 0)
        if errcode != 200:
            # errmsg may be a dict (nested Datarc error bodies, the same pitfall as bi_bridge); coerce it to str.
            errmsg = str(payload.get("errmsg") or "")
            raise VectorDBError(
                f"datarc embed proxy errcode={errcode} for '{op}': {errmsg[:300]}",
                status=r.status_code,
                body=errmsg[:500],
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ─── health ────────────────────────────────────────────
    async def health(self) -> dict[str, Any]:
        return await self._req("GET", "/health")

    # ─── collections ───────────────────────────────────────
    async def list_collections(self, *, q: str | None = None) -> list[dict[str, Any]]:
        data = await self._req("GET", "/list-collections", params={"q": q} if q else None)
        return data.get("collections") or []

    async def get_collection(self, name: str) -> dict[str, Any] | None:
        data = await self._req("GET", "/get-collection", params={"name": name})
        return data.get("collection")

    async def create_collection(
        self,
        name: str,
        *,
        force_recreate: bool = False,
    ) -> dict[str, Any]:
        body = {"name": name, "force-recreate": force_recreate}
        data = await self._req("POST", "/create-collection", json=body)
        return data.get("collection") or {}

    async def ensure_collection(self, name: str) -> dict[str, Any]:
        """幂等：已有则返回，无则创建."""
        existing = await self.get_collection(name)
        if existing is not None:
            return existing
        return await self.create_collection(name)

    async def delete_collection(self, name: str) -> None:
        await self._req("POST", "/delete-collection", json={"name": name})

    async def rename_collection(self, old: str, new: str) -> dict[str, Any]:
        data = await self._req(
            "POST",
            "/rename-collection",
            json={"old_name": old, "new_name": new},
        )
        return data.get("collection") or {}

    # ─── documents ─────────────────────────────────────────
    async def add_documents(
        self,
        *,
        collection: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> int:
        if not (len(documents) == len(metadatas) == len(ids)):
            raise VectorDBError("documents / metadatas / ids length mismatch")
        if not documents:
            return 0
        data = await self._req(
            "POST",
            "/add-documents",
            json={
                "collection": collection,
                "documents": documents,
                "metadatas": metadatas,
                "ids": ids,
            },
        )
        return int(data.get("count", 0))

    async def upsert_document(
        self,
        *,
        collection: str,
        doc_id: str,
        document: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """幂等 upsert：先按 id 删除，再 add。add-documents 要求 id 唯一，否则 409."""
        try:  # noqa: SIM105
            await self.delete_documents(collection=collection, ids=[doc_id])
        except VectorDBError:
            pass  # Allow absence.
        await self.add_documents(
            collection=collection,
            documents=[document],
            metadatas=[metadata or {}],
            ids=[doc_id],
        )

    async def query(
        self,
        *,
        collection: str,
        query_texts: list[str],
        n_results: int = 5,
        where: dict[str, Any] | None = None,
        where_document: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        """返回原始 {ids, distances, documents, metadatas} 四元组。"""
        body: dict[str, Any] = {
            "collection": collection,
            "query_texts": query_texts,
            "n_results": n_results,
        }
        if where:
            body["where"] = where
        if where_document:
            body["where_document"] = where_document
        if include:
            body["include"] = include
        data = await self._req("POST", "/query-documents", json=body)
        return data.get("results") or {}

    async def fetch_documents(
        self,
        *,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"collection": collection}
        if ids:
            body["ids"] = ids
        if where:
            body["where"] = where
        if limit is not None:
            body["limit"] = limit
        if offset is not None:
            body["offset"] = offset
        data = await self._req("POST", "/fetch-documents", json=body)
        return data.get("results") or {}

    async def delete_documents(
        self,
        *,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        where_document: dict[str, Any] | None = None,
    ) -> None:
        if not (ids or where or where_document):
            raise VectorDBError("delete_documents requires at least one of ids/where/where_document")
        body: dict[str, Any] = {"collection": collection}
        if ids:
            body["ids"] = ids
        if where:
            body["where"] = where
        if where_document:
            body["where_document"] = where_document
        await self._req("POST", "/delete-documents", json=body)


# ─────────────────────────────────────────────────────────────
# embed-via-openapi: per-connection proxy client pool and embed capability probing
# ─────────────────────────────────────────────────────────────
class EmbedProxyClientFactory:
    """datarc-proxy VectorClient 的进程级池（一条 team 连接一个实例）.

    镜像 ``bowhead_bi_bridge.DatarcClientFactory``：key = sha256(base_url ⊕ auth_key)
    （raw secret 永不作 dict key / 不可逆推）。额外带 **capability 探测**：datarc 的
    embed 代理要求连接密钥单独开 embed 权限（未开 = 403），所以 ``get_verified_client``
    首次对每条连接 probe ``health`` 一次——

    - 探测成功 → 正缓存**永久**（进程内），直接复用
    - 探测失败 → 负缓存 **TTL 300s**（admin 在 datarc 侧补开权限后最多 5min 自愈，
      不需要重启 gateway），期间返 None（调用方回退全局 embed client）
    """

    def __init__(self, *, timeout_sec: int = 30, probe_neg_ttl_sec: float = 300.0) -> None:
        self._timeout = timeout_sec
        self._neg_ttl = probe_neg_ttl_sec
        self._pool: dict[str, VectorClient] = {}
        self._probe_ok: set[str] = set()
        self._probe_failed_at: dict[str, float] = {}
        # key -> {project set used for the probe: failure time} (see _ensure_tenant cooldown semantics).
        self._tenant_probe_failed_at: dict[str, dict[frozenset[str], float]] = {}

    @staticmethod
    def _key(base_url: str, auth_key: str) -> str:
        return hashlib.sha256(f"{base_url}\x00{auth_key}".encode()).hexdigest()

    def get_client(self, base_url: str, auth_key: str) -> VectorClient:
        """未经探测的缓存 client（/validate dry-run 等想看原始错误时用）."""
        key = self._key(base_url, auth_key)
        client = self._pool.get(key)
        if client is None:
            # ssrf_guard=True: base_url is admin-supplied (untrusted); revalidate the IP on every outbound request.
            client = VectorClient(
                base_url=base_url,
                access_token=auth_key,
                timeout_sec=self._timeout,
                datarc_proxy=True,
                ssrf_guard=True,
                # Match DatarcClientFactory exactly: team settings deliberately allow HTTP and internal on-premises
                # targets only through this "connection configured by the team admin" path. Blocked ports, metadata/link-local/
                # reserved addresses remain prohibited. Without these two opt-ins, BI connects but embedding fails (see field comments).
            )
            self._pool[key] = client
        return client

    async def get_verified_client(
        self,
        base_url: str,
        auth_key: str,
        *,
        project_ids: Sequence[str] = (),
    ) -> VectorClient | None:
        """probe 过 embed capability 的 client；权限/版本/网络不行 → None（带负缓存）.

        ``project_ids`` = 本连接密钥可见的 datarc 工作区 id（调用方从已解析的可及集合
        传入）。用来判 collection 的 tenant 段——见
        ``resolve_tenant_from_collections``。判不出来不阻断 client 返回：BI 侧照常可用，
        只是 embed 侧会以「tenant 未判定」降级，日志有明确一行。
        """
        key = self._key(base_url, auth_key)
        if key in self._probe_ok:
            client = self._pool[key]
            await self._ensure_tenant(key, client, project_ids)
            return client
        failed_at = self._probe_failed_at.get(key)
        if failed_at is not None and (time.monotonic() - failed_at) < self._neg_ttl:
            return None
        client = self.get_client(base_url, auth_key)
        try:
            await client.health()
        except Exception as e:
            self._probe_failed_at[key] = time.monotonic()
            # VectorDBError messages are constructed locally (safe); report only the class for other errors to keep URLs/secrets out of logs.
            detail = str(e)[:200] if isinstance(e, VectorDBError) else type(e).__name__
            logger.warning("embed_proxy.capability_probe_failed err=%s", detail)
            return None
        self._probe_ok.add(key)
        self._probe_failed_at.pop(key, None)
        await self._ensure_tenant(key, client, project_ids)
        return client

    async def _ensure_tenant(
        self,
        key: str,
        client: VectorClient,
        project_ids: Sequence[str],
    ) -> None:
        """判一次本连接的 collection tenant 段并缓存到 client 上（失败带负缓存冷却）.

        成功后**永久**留在 client 上（tenant 是实例属性，不会变）；失败/判不准按
        ``_neg_ttl`` 冷却，避免每个 turn 都去拉一次全量 collection 列表。

        冷却键**带上这次用的 project 集合**：pin 通道（scheduled/审批）只带一个 project，
        撞号时判不出来；随后 web turn 带着完整可及集合过来时信息更多、本该能判出，不能被
        上一次的冷却挡在门外。所以只有「同一批（或更小的）project 刚失败过」才跳过。
        """
        if client.datarc_tenant or not project_ids:
            return
        probed = frozenset(str(p) for p in project_ids)
        now = time.monotonic()
        for tried, failed_at in list(self._tenant_probe_failed_at.get(key, {}).items()):
            if (now - failed_at) >= self._neg_ttl:
                continue
            if probed <= tried:  # No new information means the conclusion cannot change; do not fetch the list again.
                return
        try:
            raw = await client.list_collections()
        except Exception as e:
            self._tenant_probe_failed_at.setdefault(key, {})[probed] = time.monotonic()
            detail = str(e)[:200] if isinstance(e, VectorDBError) else type(e).__name__
            logger.warning("embed_proxy.tenant_probe_failed err=%s", detail)
            return
        names = collection_names(raw)
        tenant = resolve_tenant_from_collections(names, project_ids=project_ids)
        if tenant is None:
            self._tenant_probe_failed_at.setdefault(key, {})[probed] = time.monotonic()
            # Names contain no keys, so candidates can be logged safely, helping ops distinguish "no index" from "colliding IDs" immediately.
            candidates = sorted({t for t, _ in filter(None, (parse_schema_collection(n) for n in names))})
            logger.warning(
                "embed_proxy.tenant_unresolved projects=%s tenants_seen=%s",
                sorted({str(p) for p in project_ids}),
                candidates[:10],
            )
            return
        client.datarc_tenant = tenant
        self._tenant_probe_failed_at.pop(key, None)
        logger.info("embed_proxy.tenant_resolved tenant=%s", tenant)

    def size(self) -> int:
        return len(self._pool)

    async def aclose(self) -> None:
        for client in self._pool.values():
            await client.aclose()
        self._pool.clear()
        self._probe_ok.clear()
        self._probe_failed_at.clear()
