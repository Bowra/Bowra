"""DatarcClient — datarc OpenAPI 的 HTTP 客户端.

职责：
- 统一 auth / URL 拼装（URL 末尾 `/`、workspaceId 路径 / memberUid body 等规则）
- 把 HTTP 响应映射为 Bowhead 标准化 dataclass
- 把 `errcode != 200` 的业务错误映射为具体 :mod:`bowhead_bi_bridge.errors`
- 不做重试（交给 Router / Tool 层）；不做缓存
- 非流式（datarc 无 streaming 接口）

Concurrency: httpx2.AsyncClient supports concurrent tasks; multiple agents may share this client.
资源释放：`async with` 或显式 `await client.aclose()`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx2

from bowhead_bi_bridge.errors import (
    BIAsyncTaskTimeout,
    BIConnectionError,
    BINotFoundError,
    BIPermissionError,
    BIQueryFailedError,
    BIValidationError,
    DatarcError,
    is_throttle_message,
)
from bowhead_bi_bridge.types import (
    AskQuestionResult,
    AsyncTaskInfo,
    AttributeResult,
    AttributeRow,
    CardResult,
    CutResult,
    DatarcResponse,
    DatarcWorkspaceMeta,
    DimensionMeta,
    DownloadDataResult,
    ExplainResult,
    ExplanationEntry,
    KeywordCandidate,
    KeywordRecall,
    MeasureMeta,
    MetricEntry,
    ModelMeta,
    QueryPlan,
)

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


def _display_names(item: dict[str, Any]) -> dict[str, str]:
    """datarc `displayName` → `{locale: label}`，只留非空字符串值。

    上游可能给 None / 缺键 / 非字符串；这里规整掉，免得下游每处都判一遍。
    """
    raw = item.get("displayName")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v).strip() for k, v in raw.items() if isinstance(v, str) and v.strip()}


def _measure_format(item: dict[str, Any], model_formats: dict[int, str]) -> str:
    """measure 的数值形状：**概念层自带的 format 与模型层的 format 合并**。

    ★ 为什么两个都要读（2026-08-27 在 116 逮到）：`format` 原先只从模型层
    `/models/` 按 measure id 关联。而**公式指标**（`利润率` = `[利润]/[营业额]`、
    `退款率` = `-1*[退款额]/[营业额]`）不属于任何物理模型——它们的 `models` 是空数组，
    模型层里根本没有这个 id，于是 join 永远落空、format 恒为空。
    偏偏**比率类指标几乎全是公式指标**，等于「这是不是百分比」这条事实对它们系统性丢失：
    116 上 `利润率` 的概念层 item 自己就写着 `"format": "percentage"`，我们却读不到，
    表格把 0.1802 原样渲出来，同一轮的 KPI 和散文都是 18.0%。

    冲突解法与 `_measure_formats` 内部一致：percentage 优先（任一处说是百分比就按
    百分比），其次 float 胜 int。
    """
    try:
        mid = int(item.get("id", 0))
    except (TypeError, ValueError):
        mid = 0
    candidates = [str(item.get("format") or "").strip(), model_formats.get(mid, "")]
    candidates = [c for c in candidates if c]
    if not candidates:
        return ""
    if "percentage" in candidates:
        return "percentage"
    if "float" in candidates:
        return "float"
    return candidates[0]


class DatarcClient:
    """datarc API client.

    Example:
        async with DatarcClient("https://demo.datarc.cn", auth_key="xxx") as c:
            ws = await c.list_workspaces()
            answer = await c.ask_question(datarc_workspace_id="1",
                                          member_uid="aaa...",
                                          text="今年 GMV")
    """

    def __init__(
        self,
        base_url: str,
        auth_key: str,
        *,
        timeout_sec: int = 60,
        default_headers: dict[str, str] | None = None,
        ssrf_guard: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._auth_key = auth_key
        self._timeout = timeout_sec
        # measure id → model-layer format ("float"/"int"/"percentage"). The schema rarely changes;
        # cache by Datarc workspace for the process lifetime: `list_measures` backs bi_schema /
        # bi_list_measures, which most data Agents call at the start of a task. Without caching,
        # each call adds an HTTP call. **Cache only successful responses**, even an empty dict:
        # "this instance has no percentage measures" is stable. Never cache failed requests, or a transient 500
        # prevents this connection from obtaining formats for the entire process lifetime (see `_measure_formats`).
        self._measure_format_cache: dict[str, dict[int, str]] = {}
        # Lowercase measure alias → actual units: {"currency": "CNY", "unit": "元", "format": "percentage"}.
        # Populated only by `list_measures` (schema prefetch); **queries read but never populate it**. ask_question
        # is the hottest path; labeling units must not add another HTTP call.
        self._measure_unit_cache: dict[str, dict[str, dict[str, Any]]] = {}
        # Lowercase dimension alias → {display_names, primary}. Follow the same rule as measures:
        # only `list_dimensions` populates it; queries only read. Dimensions have no unit/currency, so store only display names.
        self._dimension_display_cache: dict[str, dict[str, dict[str, Any]]] = {}
        _client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": timeout_sec,
            # R7.4 (ws-datarc-decouple): never follow redirects. datarc never needs it, and a
            # per-connection (admin-supplied) base_url could otherwise be redirected to an
            # internal address (SSRF). httpx2 already defaults to False — explicit for intent.
            "follow_redirects": False,
            "headers": {
                "Authorization": f"Bearer {auth_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(default_headers or {}),
            },
        }
        # R7.4 (ws-datarc-decouple [review P1-b]): for UNTRUSTED, admin-supplied base_urls (the
        # per-team connection pool sets ssrf_guard=True), pin every outbound request to a freshly
        # validated public IP — create-time validate_url alone can't stop DNS rebinding / a DNS
        # record later pointed at an internal address. The env/dev client leaves this off (it may
        # legitimately target localhost / a mock).
        #
        # The import is LAZY because core→bi_bridge is already a hard dependency (tool_registry /
        # orchestrator import DatarcClient), so bi_bridge MUST NOT hard-import core (cycle). In any
        # real deployment core is present (it cannot run without bi_bridge) and ssrf_guard=True is
        # only ever set by the gateway-side factory. FAIL CLOSED: if the guard cannot be built we
        # let the error propagate rather than silently connect UNGUARDED to an untrusted base_url
        # (review P1-b — a silent fallback would defeat the SSRF protection).
        self._http: httpx2.AsyncClient
        if ssrf_guard:
            from bowhead_core.ssrf_guard import build_guarded_async_client

            self._http = build_guarded_async_client(
                **_client_kwargs,
            )
        else:
            self._http = httpx2.AsyncClient(**_client_kwargs)

    async def __aenter__(self) -> DatarcClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─────────────────────────────────────────────────────────
    # Internal: HTTP requests + response mapping
    # ─────────────────────────────────────────────────────────
    def _ensure_trailing_slash(self, path: str) -> str:
        # datarc requires URLs to end with `/`; append the query string afterward.
        if "?" in path:
            head, _, query = path.partition("?")
            if not head.endswith("/"):
                head += "/"
            return f"{head}?{query}"
        return path if path.endswith("/") else path + "/"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> DatarcResponse:
        url = self._ensure_trailing_slash(path)
        try:
            resp = await self._http.request(
                method=method,
                url=url,
                json=json_body,
                params=params,
            )
        except httpx2.TimeoutException as e:
            raise BIConnectionError(
                f"datarc request timeout: {method} {url}",
                request_path=url,
            ) from e
        except httpx2.HTTPError as e:
            # R7.5 (ws-datarc-decouple): echo the exception *class*, not str(e) — httpx2 error
            # text can carry the resolved full URL (host included). The relative `url` (path,
            # no host/secret) stays for tracing; the original cause rides `from e` into logs.
            raise BIConnectionError(
                f"datarc network error: {method} {url}: {type(e).__name__}",
                request_path=url,
            ) from e

        # HTTP-layer errors.
        if resp.status_code in {401, 403}:
            raise BIPermissionError(
                f"datarc {resp.status_code}: access rejected",
                request_path=url,
                source="http",
            )
        if resp.status_code == 404:
            raise BINotFoundError(
                f"datarc 404: {url}（注意 URL 必须 `/` 结尾）",
                request_path=url,
            )
        if 500 <= resp.status_code < 600:
            raise BIConnectionError(
                f"datarc 5xx: {resp.status_code} at {url}",
                request_path=url,
            )

        # Decode the body only for HTTP 2xx; propagate other unhandled status codes.
        # R7.5: NEVER put the raw upstream body into the exception message — it can carry
        # cross-tenant data and reaches the API/LLM via str(e). Echo only bounded-safe shape
        # (status + content-type + byte length); the body stays server-side.
        if not (200 <= resp.status_code < 300):
            raise BIConnectionError(
                f"datarc HTTP {resp.status_code} at {url} (ct={resp.headers.get('content-type', '?')} len={len(resp.content)})",
                request_path=url,
                source="http",
                throttled=resp.status_code == 429,
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise BIConnectionError(
                f"datarc response not JSON at {url} (ct={resp.headers.get('content-type', '?')} len={len(resp.content)})",
                request_path=url,
            ) from e

        errcode = int(payload.get("errcode", 0) or 0)
        errmsg = payload.get("errmsg", "")
        data = payload.get("data")

        dr = DatarcResponse(
            errcode=errcode,
            errmsg=errmsg,
            data=data,
            request_path=url,
            request_id=str(resp.headers.get("x-request-id") or "")[:128],
        )

        # Business error mapping.
        if not dr.ok:
            self._raise_business_error(dr)

        return dr

    @staticmethod
    def _raise_business_error(dr: DatarcResponse) -> None:
        """把 datarc 业务错误映射为具体异常."""
        code = dr.errcode
        # Fallback: errmsg can be a dict (nested datarc business error bodies); coerce it to str,
        # or the .lower() / `in` checks below raise AttributeError: 'dict' has no 'lower'.
        # Observed in the attribute call for t_b556678b6d81.
        raw_msg = dr.errmsg
        if isinstance(raw_msg, dict):
            raw_msg = str(raw_msg)
        msg = raw_msg or f"datarc errcode={code}"
        # Typed facts; the exception classes and messages below are unchanged.
        throttled = is_throttle_message(msg)

        # Map errmsg text to exceptions (conservative heuristics; datarc provides no enumeration).
        if "无法搜索" in msg or "查询结果为表格" in msg or "解释" in msg:
            raise BIQueryFailedError(msg, errcode=code, errmsg=dr.errmsg, request_path=dr.request_path, source="business", throttled=throttled)
        if "权限" in msg or "forbidden" in msg.lower():
            raise BIPermissionError(msg, errcode=code, errmsg=dr.errmsg, request_path=dr.request_path, source="business", throttled=throttled)
        if "不存在" in msg or "not found" in msg.lower():
            raise BINotFoundError(msg, errcode=code, errmsg=dr.errmsg, request_path=dr.request_path, source="business", throttled=throttled)
        # Fallback.
        if code == 400:
            raise BIValidationError(msg, errcode=code, errmsg=dr.errmsg, request_path=dr.request_path, source="business", throttled=throttled)
        raise DatarcError(msg, errcode=code, errmsg=dr.errmsg, request_path=dr.request_path, source="business", throttled=throttled)

    # ─────────────────────────────────────────────────────────
    # Metadata: workspaces / models / measures / dimensions
    # ─────────────────────────────────────────────────────────
    async def get_scope(self) -> dict[str, Any]:
        """Return the current Bearer access key's declared capability scope."""
        dr = await self._request("GET", "openapi/v1/scopes/")
        if not isinstance(dr.data, dict):
            raise BIValidationError("datarc scope response must contain an object", request_path=dr.request_path)
        return dr.data

    async def list_workspaces(self, q: str | None = None) -> list[DatarcWorkspaceMeta]:
        params = {"q": q} if q else None
        dr = await self._request("GET", "openapi/v1/workspaces/", params=params)
        items = dr.data or []
        return [
            DatarcWorkspaceMeta(
                id=str(item.get("id", "")),
                name=item.get("name", ""),
                desc=item.get("desc", ""),
                adapter=item.get("adapter", 0),
                date_offset=item.get("date_offset", -1),
                raw=item,
            )
            for item in items
        ]

    async def get_workspace(self, datarc_workspace_id: str) -> DatarcWorkspaceMeta:
        dr = await self._request("GET", f"openapi/v1/workspaces/{datarc_workspace_id}/")
        item = dr.data or {}
        return DatarcWorkspaceMeta(
            id=str(item.get("id", datarc_workspace_id)),
            name=item.get("name", ""),
            desc=item.get("desc", ""),
            adapter=item.get("adapter", 0),
            date_offset=item.get("date_offset", -1),
            raw=item,
        )

    async def list_models(self, datarc_workspace_id: str) -> list[ModelMeta]:
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/models/",
        )
        items = dr.data or []
        return [
            ModelMeta(
                uid=str(item.get("uid", "")),
                name=item.get("name", ""),
                table=item.get("table", ""),
                dimensions=item.get("dimensions", []) or [],
                measures=item.get("measures", []) or [],
                raw=item,
            )
            for item in items
        ]

    async def get_model(self, datarc_workspace_id: str, model_uid: str) -> ModelMeta:
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/models/{model_uid}/",
        )
        item = dr.data or {}
        return ModelMeta(
            uid=str(item.get("uid", model_uid)),
            name=item.get("name", ""),
            table=item.get("table", ""),
            dimensions=item.get("dimensions", []) or [],
            measures=item.get("measures", []) or [],
            raw=item,
        )

    async def _measure_formats(self, datarc_workspace_id: str) -> dict[int, str]:
        """Read measure value shapes from model metadata.

        The concept endpoint does not supply format; values alone cannot distinguish
        fractional percentages from points. Cache successful results by workspace, call
        serially after measure retrieval to respect upstream concurrency limits, and
        return an empty mapping on failure without caching it. A pooled client must retry
        a transient failure on a later call. When models disagree, prefer percentage,
        then float, then int.
        """
        cached = self._measure_format_cache.get(datarc_workspace_id)
        if cached is not None:
            return cached
        out: dict[int, str] = {}
        try:
            dr = await self._request(
                "GET",
                f"openapi/v1/workspaces/{datarc_workspace_id}/models/",
            )
            for model in dr.data or []:
                if not isinstance(model, dict):
                    continue
                for m in model.get("measures") or []:
                    if not isinstance(m, dict):
                        continue
                    try:
                        mid = int(m.get("id", 0))
                    except (TypeError, ValueError):
                        continue
                    fmt = str(m.get("format") or "").strip()
                    if not mid or not fmt:
                        continue
                    prev = out.get(mid)
                    if prev is None or fmt == "percentage" or (prev == "int" and fmt == "float"):
                        out[mid] = fmt
        except Exception as e:  # noqa: BLE001 — Optional enrichment must never propagate errors.
            logger.warning(
                "datarc.measure_formats_failed ws=%s err=%s (schema 退回无 format，不缓存，下次重试)",
                datarc_workspace_id,
                e,
            )
            return {}  # ← Do not cache: transient failures must not poison the entire process; see the docstring.
        self._measure_format_cache[datarc_workspace_id] = out
        return out

    async def list_measures(self, datarc_workspace_id: str) -> list[MeasureMeta]:
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/measures/",
        )
        items = dr.data or []
        # Sequential: fetch the model layer after measures, not concurrently with the request above (see _measure_formats).
        formats = await self._measure_formats(datarc_workspace_id)
        by_name: dict[str, dict[str, Any]] = {}
        for item in items:
            names_l = [n for n in (item.get("names") or []) if isinstance(n, str) and n.strip()]
            info: dict[str, Any] = {
                "currency": str(item.get("currencyCode") or ""),
                "unit": str(item.get("unitName") or ""),
                "format": _measure_format(item, formats),
                # Display labels (by locale) + canonical name. Downstream chart column renaming needs both:
                # the replacement (display) and the original segment length (primary, to replace only the measure name).
                "display_names": _display_names(item),
                "primary": names_l[0].strip() if names_l else "",
            }
            if not any(v for k, v in info.items() if k != "primary"):
                continue
            for nm in names_l:
                by_name[nm.strip().lower()] = info
        self._measure_unit_cache[datarc_workspace_id] = by_name
        return [
            MeasureMeta(
                id=int(item.get("id", 0)),
                names=list(item.get("names", []) or []),
                description=item.get("description", ""),
                sort=int(item.get("sort", 1)),
                unit_name=str(item.get("unitName") or ""),
                currency_code=str(item.get("currencyCode") or ""),
                used=bool(item.get("used", True)),
                value_format=_measure_format(item, formats),
                display_names=_display_names(item),
                raw=item,
            )
            for item in items
        ]

    async def list_dimensions(self, datarc_workspace_id: str) -> list[DimensionMeta]:
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/dimensions/",
        )
        items = dr.data or []
        by_name: dict[str, dict[str, Any]] = {}
        for item in items:
            names_l = [n for n in (item.get("names") or []) if isinstance(n, str) and n.strip()]
            dn = _display_names(item)
            if not dn:
                continue
            info: dict[str, Any] = {"display_names": dn, "primary": names_l[0].strip() if names_l else ""}
            for nm in names_l:
                by_name[nm.strip().lower()] = info
        self._dimension_display_cache[datarc_workspace_id] = by_name
        return [
            DimensionMeta(
                id=int(item.get("id", 0)),
                names=list(item.get("names", []) or []),
                type=item.get("type", "str"),
                format=item.get("format", ""),
                rating=int(item.get("rating", 0)),
                is_keyword=bool(item.get("isKeyword", False)),
                auto_group=bool(item.get("autoGroup", False)),
                used=bool(item.get("used", False)),
                display_names=_display_names(item),
                raw=item,
            )
            for item in items
        ]

    # ─────────────────────────────────────────────────────────
    # Data queries
    # ─────────────────────────────────────────────────────────
    def measure_metadata(self, datarc_workspace_id: str, name: str | None) -> dict[str, str] | None:
        """指标名 → 该源的**原生**单位元数据；查不到 None。

        故意只吐字段、不做任何解释——「这算不算钱、要不要 ×100」是**跨数据源**的
        判断，归 `bowhead_core.units`，不该由某一个源的 client 各自决定。
        键按语义命名（不按本源字段名），换个源填同样的键即可复用下游全部逻辑。

        只读缓存，**从不为此发请求**：调用点在渲染路上，不能因为查单位多打一跳。
        缓存由 `list_measures` 填（每次取 schema 都跑），没热就返回 None，
        下游退回名称兜底。
        """
        table = self._measure_unit_cache.get(datarc_workspace_id) or {}
        info = table.get((name or "").strip().lower())
        if not info:
            return None
        return {
            "currency_code": info.get("currency", ""),
            "value_format": info.get("format", ""),
            "unit_name": info.get("unit", ""),
            "display_names": info.get("display_names") or {},
            "primary_name": info.get("primary", ""),
        }

    def dimension_metadata(self, datarc_workspace_id: str, name: str | None) -> dict[str, Any] | None:
        """维度名 → 展示元数据；查不到 None。与 `measure_metadata` 同规矩（只读缓存，不发请求）。

        维度没有单位/货币/数值形状，所以只有展示名——图表列换名两边都要用，
        只接指标那一半的话，英文轮里维度列名仍是中文（116 实测：`营业额 / 元` 换成了
        `GMV / 元`，而 `省` 纹丝不动）。
        """
        table = self._dimension_display_cache.get(datarc_workspace_id) or {}
        info = table.get((name or "").strip().lower())
        if not info:
            return None
        return {
            "currency_code": "",
            "value_format": "",
            "unit_name": "",
            "display_names": info.get("display_names") or {},
            "primary_name": info.get("primary", ""),
        }

    async def ask_question(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        text: str,
    ) -> AskQuestionResult:
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/question/",
            json_body={"memberUid": member_uid, "text": text},
        )
        return self._parse_ask_question(dr.data or {}, datarc_workspace_id)

    # ─────────────────────────────────────────────────────────
    # Sprint ar2 — /data/cut/ question tokenization (parse-only, no execution)
    # ─────────────────────────────────────────────────────────
    async def cut_question(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        question: str,
    ) -> CutResult:
        """调 datarc /data/cut/ 对 user query 做 token 切分 + prop 标注.

        spike 2026-05-16 verified prop 格式:
          - "d{id}=base64(value)" — keywords coll dim_value 命中
          - "d{id}" — 纯 dim 名
          - "m{id}" — 指标
          - "t{id}" — 时间字段
          - "$like" / "$eq" — operator
          - "dateRange_YYYYMMDDYYYYMMDD" — 时间范围 (datarc 自处理)
          - "value_xxx" — value 但未绑 dim_id (跟 $like 同 unitIdx = contains predicate; 孤立 = 半精确)
          - "unknown" — datarc 自承没分类 (ar2 resolution 关键信号)

        Native BI queries use these tokens for transparent parsing diagnostics;
        concept resolution validates unknown terms separately.
        """
        from bowhead_bi_bridge.types import CutResult, CutToken

        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/cut/",
            json_body={"memberUid": member_uid, "question": question},
        )
        data = dr.data
        # data is list[dict[word, prop, unitIdx]]; None when errcode != 200.
        if not isinstance(data, list):
            return CutResult(tokens=[])
        tokens = [
            CutToken(
                word=str(item.get("word", "")),
                prop=str(item.get("prop", "")),
                unit_idx=int(item.get("unitIdx", 0)),
            )
            for item in data
            if isinstance(item, dict)
        ]
        return CutResult(tokens=tokens)

    async def list_guide_questions(self, datarc_workspace_id: str) -> list[str]:
        """Workspace 引导问题（datarc 推荐用户问啥）. 用于 WelcomeHome 页签示例."""
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/question-guide/",
        )
        data = dr.data
        if isinstance(data, list):
            return [str(x) for x in data if x]
        return []

    async def recall_keywords(
        self,
        datarc_workspace_id: str,
        words: list[str],
        *,
        n_results: int | None = None,
    ) -> list[KeywordRecall]:
        """调 datarc /keywords/recall/：原始词 → 工作区里的维度值候选（向量召回）.

        返回与 ``words`` 按下标一一对应（datarc 契约保证顺序）。条数对不上时抛
        :class:`DatarcError`，**不按位置硬配**——错位会把 A 词的候选挂到 B 词头上。
        ``words`` 不许含空串（datarc 校验）；``n_results`` 范围 1–100，缺省 datarc 取 20。
        工作区所属客户未配置向量服务时 datarc 回 errcode=400「未配置向量服务」
        （映射为 :class:`BIValidationError`）。
        """
        if not words:
            return []
        body: dict[str, Any] = {"words": list(words)}
        if n_results is not None:
            body["n_results"] = n_results
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/keywords/recall/",
            json_body=body,
        )
        data = dr.data if isinstance(dr.data, list) else []
        if len(data) != len(words):
            raise DatarcError(
                f"datarc keywords/recall 返回 {len(data)} 条结果，请求了 {len(words)} 个词",
                errcode=dr.errcode,
                errmsg=dr.errmsg,
                request_path=dr.request_path,
            )
        out: list[KeywordRecall] = []
        for word, item in zip(words, data, strict=True):
            item = item if isinstance(item, dict) else {}
            candidates = [
                KeywordCandidate(
                    word=str(c.get("word") or ""),
                    distance=self._recall_distance(c.get("distance")),
                    field=str(c.get("field") or ""),
                    prop=str(c.get("prop") or ""),
                    tuids=str(c.get("tuids") or ""),
                )
                for c in (item.get("results") or [])
                if isinstance(c, dict)
            ]
            out.append(KeywordRecall(source=str(item.get("source") or word), candidates=candidates))
        return out

    async def check_token_permissions(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        tokens: list[dict[str, Any]],
    ) -> list[str]:
        """Call datarc ``/tokens/permissions/`` for one member; return one status per token.

        Each token is ``{"word", "prop", "tuids"?}``; ``tuids`` must be a list (a
        comma-joined string is rejected as an invalid token). Statuses are requested in
        ``en-US`` so callers can classify them: ``Normal``, ``No permission`` or an
        invalid-token message. A length mismatch raises :class:`DatarcError` rather than
        pairing statuses with the wrong tokens.
        """
        if not tokens:
            return []
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/tokens/permissions/",
            json_body={"memberUid": member_uid, "language": "en-US", "tokens": tokens},
        )
        data = dr.data if isinstance(dr.data, list) else []
        if len(data) != len(tokens):
            raise DatarcError(
                f"datarc tokens/permissions returned {len(data)} statuses for {len(tokens)} tokens",
                errcode=dr.errcode,
                errmsg=dr.errmsg,
                request_path=dr.request_path,
            )
        return [str(s) for s in data]

    @staticmethod
    def _recall_distance(raw: Any) -> float:
        # Treat missing/non-numeric distances as farthest: rank them last rather than falsely indicating an exact match (0.0).
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    def _parse_ask_question(self, data: dict[str, Any], datarc_workspace_id: str = "") -> AskQuestionResult:
        type_value = data.get("type", "unsupported")
        inner = data.get("data") or {}
        metrics_raw = inner.get("metrics", []) if isinstance(inner, dict) else []
        return AskQuestionResult(
            url=data.get("url", ""),
            type=type_value,  # type: ignore[arg-type]
            metrics=[
                MetricEntry(
                    name=m.get("name", ""),
                    content=m.get("content", ""),
                    value=m.get("value"),
                )
                for m in metrics_raw
            ],
            image_base64=inner.get("image", "") if isinstance(inner, dict) else "",
            tokens=data.get("tokens", []) or [],
            explanations=[
                ExplanationEntry(
                    sql=e.get("sql", ""),
                    params=e.get("params", {}) or {},
                    model=e.get("model", ""),
                    dimensions=e.get("dimensions", []) or [],
                    measures=e.get("measures", []) or [],
                    filters=e.get("filters", []) or [],
                    date_filters=e.get("dateFilters", []) or [],
                    keywords=e.get("keywords", []) or [],
                    order=e.get("order", []) or [],
                    limit=e.get("limit"),
                    having=e.get("having", []) or [],
                    proportion=e.get("proportion", []) or [],
                )
                for e in (data.get("explanations") or [])
            ],
            raw=data,
        )

    async def get_card(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        text: str,
        *,
        auto_login: bool = False,
    ) -> CardResult:
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/card/",
            json_body={
                "memberUid": member_uid,
                "text": text,
                "autoLogin": 1 if auto_login else 0,
            },
        )
        return self._parse_card(dr.data or {})

    async def get_cards(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        text: str,
        *,
        auto_login: bool = False,
    ) -> list[CardResult]:
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/cards/",
            json_body={
                "memberUid": member_uid,
                "text": text,
                "autoLogin": 1 if auto_login else 0,
            },
        )
        return [self._parse_card(item) for item in (dr.data or [])]

    @staticmethod
    def _parse_card(data: dict[str, Any]) -> CardResult:
        return CardResult(
            url=data.get("url", ""),
            title=data.get("title", ""),
            card_type=int(data.get("cardType", 0) or 0),
            card_tokens=data.get("cardTokens", ""),
            sentence_type=int(data.get("sentenceType", 0) or 0),
            tokens=data.get("tokens", []) or [],
            explanations=data.get("explanations", []) or [],
            raw=data,
        )

    async def attribute(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        question: str,
        comparison_type: str = "yoy",
    ) -> AttributeResult:
        if comparison_type not in ("yoy", "ring"):
            raise BIValidationError(f"comparison_type must be 'yoy' or 'ring', got {comparison_type!r}")
        dr = await self._request(
            "POST",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/attribution-dimensions/",
            json_body={
                "memberUid": member_uid,
                "question": question,
                "comparisonType": comparison_type,
            },
        )
        return self._parse_attribute(dr.data or {})

    @staticmethod
    def _parse_attribute(data: dict[str, Any]) -> AttributeResult:
        measure = data.get("measure", {}) or {}

        def _safe_float(v: Any, default: float = 0.0) -> float:
            """ξ A1: defensive float cast — datarc 偶尔在 numeric slot 返 string
            (e.g. dimension name '光明国际饮料有限公司' 串到 measureCurrent),
            导致 ValueError. 兜底 default + log skip.
            """
            if v is None:
                return default
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        def _safe_int(v: Any, default: int = 0) -> int:
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        # ξ A1: filter malformed rows + log warning (don't silently zero — would
        # produce the incorrect narrative "光明国际 = 0元"). Skip rows when measure fields
        # are non-numeric string, keep ones that cast cleanly.
        rows_in = data.get("rows") or []
        rows_out: list[AttributeRow] = []
        malformed_count = 0
        for r in rows_in:
            if not isinstance(r, dict):
                malformed_count += 1
                continue
            mp_raw = r.get("measurePrevious", 0) or 0
            mc_raw = r.get("measureCurrent", 0) or 0
            # If either measure is non-numeric string, skip row entirely
            try:
                mp = float(mp_raw)
                mc = float(mc_raw)
            except (ValueError, TypeError):
                malformed_count += 1
                continue
            rows_out.append(
                AttributeRow(
                    rank=_safe_int(r.get("rank", 0) or 0),
                    dimension=r.get("dimension", ""),
                    attr=r.get("attr", ""),
                    measure_previous=mp,
                    measure_current=mc,
                    text=r.get("text", ""),
                    mask_key=r.get("mask_key", ""),
                    units=r.get("units", []) or [],
                )
            )
        if malformed_count:
            import logging as _lg

            _lg.getLogger(__name__).warning(
                "datarc.attribute_malformed_rows skipped=%d total_in=%d — check datarc payload (string in numeric slot)",
                malformed_count,
                len(rows_in),
            )
            try:
                from bowhead_core import metrics as _m

                _m.inc(
                    "bowhead_bi_attribute_malformed_row_total",
                    labels={"reason": "non_numeric_measure"},
                )
            except Exception:  # noqa: S110
                pass

        return AttributeResult(
            title=data.get("title", ""),
            insight=data.get("insight", ""),
            description=data.get("description", ""),
            sub_title=data.get("subTitle", ""),
            measure_name=measure.get("name", ""),
            measure_delta=_safe_float(data.get("measureDelta", 0) or 0),
            measure_dimension_max_delta=_safe_float(data.get("measureDimensionMaxDelta", 0) or 0),
            rows=rows_out,
            raw=data,
        )

    async def explain(
        self,
        datarc_workspace_id: str,
        member_uid: str,
        text: str,
    ) -> ExplainResult:
        dr = await self._request(
            "GET",
            f"openapi/v1/workspaces/{datarc_workspace_id}/data/insight/",
            params={"memberUid": member_uid, "text": text},
        )
        data = dr.data or {}
        return ExplainResult(
            type=data.get("type", "text"),
            text=data.get("text", ""),
        )

    # ─────────────────────────────────────────────────────────
    # Asynchronous LLM tasks
    # ─────────────────────────────────────────────────────────
    async def call_llm_async(
        self,
        member_uid: str,
        *,
        prompt: str | None = None,
        template: dict[str, Any] | None = None,
    ) -> AsyncTaskInfo:
        if not prompt and not template:
            raise BIValidationError("provide either `prompt` or `template`")
        params: dict[str, Any] = {"memberUid": member_uid}
        if prompt is not None:
            params["prompt"] = prompt
        if template is not None:
            params["template"] = template

        dr = await self._request(
            "POST",
            "openapi/v1/async-tasks/",
            json_body={"taskType": 20, "params": params},
        )
        return self._parse_task(dr.data or {})

    async def get_llm_result(self, task_uid: str) -> AsyncTaskInfo:
        dr = await self._request(
            "GET",
            f"openapi/v1/async-tasks/{task_uid}/",
        )
        return self._parse_task(dr.data or {})

    # ─────────────────────────────────────────────────────────
    # Data download (taskType=22, v25.7.0+) — row-level CSV retrieval
    # ─────────────────────────────────────────────────────────
    async def download_data(
        self,
        datarc_workspace_id: str,  # Only for logging/tracing; taskType=22 itself does not distinguish Datarc workspaces.
        member_uid: str,
        text: str,
        *,
        timeout_sec: float = 60.0,
        poll_interval_sec: float = 1.5,
    ) -> DownloadDataResult:
        """调 datarc taskType=22 取**行级明细**.

        流程: 创建任务 → 轮询 status∈{2,3} → 下载预签名 OBS ZIP → 解压读 CSV → list[dict].
        详见 docs/datarc-api-dump/async_task_download_data.md.

        行级场景（按维度分组 / 清单 / 前 N / 否定过滤）走这个；**不要**用 ask_question
        套 "按 X" query —— 那个对 group-by 返 image，拿不到 rows.

        返:
            :class:`DownloadDataResult` 含 columns/rows/row_count/total_rows/csv_filename/download_ms

        抛:
            :class:`BIQueryFailedError` datarc 返 status=3 时；
            :class:`BIConnectionError` 轮询超时 / OBS 下载失败 / ZIP 解压失败；
            其他 :mod:`errors` 映射 —— 和其它 datarc API 一致.
        """
        import asyncio as _aio
        import csv as _csv
        import io as _io
        import time as _time
        import zipfile as _zip

        import httpx2 as _httpx2

        from bowhead_bi_bridge.types import DownloadDataResult

        t0 = _time.perf_counter()

        # 1. Create the task.
        dr = await self._request(
            "POST",
            "openapi/v1/async-tasks/",
            json_body={"taskType": 22, "params": {"memberUid": member_uid, "text": text}},
        )
        task = self._parse_task(dr.data or {})
        if not task.uid:
            raise BIQueryFailedError(
                f"download_data: create task returned no uid (data={dr.data!r})",
                errcode=dr.errcode,
                errmsg=dr.errmsg,
                request_path=dr.request_path,
            )

        # 2. Poll.
        deadline = _time.perf_counter() + timeout_sec
        final: AsyncTaskInfo | None = None
        while _time.perf_counter() < deadline:
            await _aio.sleep(poll_interval_sec)
            dr2 = await self._request(
                "GET",
                f"openapi/v1/async-tasks/{task.uid}/",
            )
            cur = self._parse_task(dr2.data or {})
            if cur.is_terminal:
                final = cur
                break
        if final is None:
            raise BIAsyncTaskTimeout(
                f"download_data: poll timeout after {timeout_sec}s (task_uid={task.uid})",
                request_path=f"openapi/v1/async-tasks/{task.uid}/",
            )

        if final.status == 3:
            msg = ""
            err_code_inner = 0
            err_type = ""
            if isinstance(final.result, dict):
                msg = str(final.result.get("message") or "")
                err_code_inner = int(final.result.get("errcode", 0) or 0)
                err_type = str(final.result.get("error_type") or "")
            # ξ A3: build an actionable error message so the LLM does not keep forcing refinement.
            # Hints like "Check the query logs" are useless to the LLM; rephrase them.
            actionable_hint = ""
            msg_lower = msg.lower()
            if "timeout" in msg_lower or "超时" in msg:
                actionable_hint = " hint: datarc 超时, query 太复杂 — 拆成多个小 query 顺序跑 + 本地 join."
            elif "not found" in msg_lower or "找不到" in msg or "不存在" in msg:
                actionable_hint = " hint: 字段名/实体名拼写错 — 先 bi_list_measures / bi_list_dimensions 看 ws 真有什么字段."
            elif "validation" in msg_lower or "校验" in msg:
                actionable_hint = " hint: query 形态 datarc 不支持 — 用 bi_fetch_rows 拿 rows 或拆 query."
            elif "permission" in msg_lower or "权限" in msg:
                actionable_hint = " hint: 当前 member 无该 ws 字段权限 — 用别的 measure 或换 ws."
            elif not msg or msg in ("(no message)", "下载数据失败"):
                actionable_hint = (
                    " hint: datarc 没给错误细节. 可能 query 太复杂或字段拼错. "
                    "建议: (1) 简化 query 单 metric/单 dim 试; (2) bi_list_measures 查真字段名; "
                    "(3) 别硬 refine 同 shape — 同一错连续 ≥2 次后必须换 tool/换查询."
                )
            raise BIQueryFailedError(
                f"download_data failed (errcode={err_code_inner} type={err_type or 'unknown'}): "
                f"{msg[:200] or '(no datarc message)'}.{actionable_hint}",
                errcode=err_code_inner,
                errmsg=msg,
                request_path="",
            )

        result = final.result if isinstance(final.result, dict) else {}
        url = str(result.get("url") or "")
        if not url:
            raise BIQueryFailedError(
                f"download_data: status=2 but no url in result (result={result!r})",
                errcode=0,
                errmsg="missing url",
                request_path="",
            )

        # 3. Download the ZIP (OBS presigned URL, no auth header).
        try:
            async with _httpx2.AsyncClient(timeout=self._timeout, follow_redirects=True) as c:
                resp = await c.get(url)
            if resp.status_code != 200:
                raise BIConnectionError(
                    f"download_data: OBS {resp.status_code} at {url[:100]}",
                    request_path=url,
                )
            zip_bytes = resp.content
        except _httpx2.HTTPError as e:
            raise BIConnectionError(
                f"download_data: OBS network error: {e}",
                request_path=url,
            ) from e

        # 4. Extract and parse the first CSV.
        try:
            z = _zip.ZipFile(_io.BytesIO(zip_bytes))
            csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise BIQueryFailedError(
                    f"download_data: zip has no csv (names={z.namelist()!r})",
                    errcode=0,
                    errmsg="no csv in zip",
                    request_path="",
                )
            csv_name = csv_names[0]
            content = z.read(csv_name).decode("utf-8-sig", errors="replace")
        except _zip.BadZipFile as e:
            raise BIConnectionError(
                f"download_data: bad zip ({e}); url={url[:100]}",
                request_path=url,
            ) from e

        reader = _csv.DictReader(_io.StringIO(content))
        rows: list[dict[str, Any]] = []
        for r in reader:
            # Try converting numeric columns to float; keep str when conversion fails.
            casted: dict[str, Any] = {}
            for k, v in r.items():
                if v is None or v == "":
                    casted[k] = v
                    continue
                try:
                    casted[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
                except (ValueError, TypeError):
                    casted[k] = v
            rows.append(casted)
        columns = list(reader.fieldnames or (rows[0].keys() if rows else []))

        # datarc reports the query semantics actually applied; new in taskType=22, alongside url/errors.
        # Older versions lack these two keys; leave them empty without errors (the upstream owns the response shape; less information is acceptable).
        explain_message = str(result.get("message") or "")
        raw_explain = result.get("message_json")
        explain_json = raw_explain if isinstance(raw_explain, dict) else {}
        # `errors` lists server-generated data items whose downloads failed (zero-based indices); the ZIP contains only successful items.
        # With multiple data items, read only `csv_names[0]`. Neither was previously exposed, so callers could not know some data was not retrieved.
        # bool subclasses int; explicitly exclude it so True does not count as index 1.
        download_errors = [int(i) for i in (result.get("errors") or []) if isinstance(i, int) and not isinstance(i, bool)]
        return DownloadDataResult(
            columns=columns,
            rows=rows,
            total_rows=len(rows),
            csv_filename=csv_name,
            download_ms=int((_time.perf_counter() - t0) * 1000),
            task_uid=task.uid,
            query_text=text,
            explain_message=explain_message,
            explain_json=explain_json,
            download_errors=download_errors,
            csv_count=len(csv_names),
            datarc_completed_at=str(final.updated_at or ""),
            request_id=dr.request_id,
        )

    # ─────────────────────────────────────────────────────────
    # DataSeek (taskType=25) — question classification / decomposition
    # ─────────────────────────────────────────────────────────
    async def plan_query(
        self,
        member_uid: str,
        text: str,
        *,
        thread_uid: str = "",
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 1.0,
    ) -> QueryPlan:
        """调 DataSeek 对 user text 做 query shape 分类 / 拆解.

        返回 QueryPlan：根据 action type 不同填 chain/texts/reports/tokens 等字段.
        失败（超时 / 任务 fail）→ 返 `type="error"` + error 字段，不抛异常（让调用方
        降级到直接 ask_question）.

        调用方预期时延：simple query ~7s；complex ~2-3s. 有时延抖动，给 30s 超时.
        """
        import asyncio as _aio
        import time as _time

        from bowhead_bi_bridge.types import QueryPlan

        t0 = _time.perf_counter()

        # 1. Create the task.
        params: dict[str, Any] = {"memberUid": member_uid, "text": text}
        if thread_uid:
            params["threadUid"] = thread_uid
        try:
            dr = await self._request(
                "POST",
                "openapi/v1/async-tasks/",
                json_body={"taskType": 25, "params": params},
            )
        except Exception as e:
            return QueryPlan(type="error", text=text, error=f"create_task_failed: {e}", elapsed_sec=_time.perf_counter() - t0)
        task = self._parse_task(dr.data or {})
        if not task.uid:
            return QueryPlan(type="error", text=text, error="no_task_uid", elapsed_sec=_time.perf_counter() - t0)

        # 2. Poll
        deadline = t0 + timeout_sec
        final_task: AsyncTaskInfo | None = None
        while _time.perf_counter() < deadline:
            await _aio.sleep(poll_interval_sec)
            try:
                dr = await self._request(
                    "GET",
                    f"openapi/v1/async-tasks/{task.uid}/",
                )
                cur = self._parse_task(dr.data or {})
            except Exception as e:
                return QueryPlan(type="error", text=text, error=f"poll_failed: {e}", elapsed_sec=_time.perf_counter() - t0)
            if cur.is_terminal:
                final_task = cur
                break

        if final_task is None:
            return QueryPlan(type="error", text=text, error=f"timeout_{timeout_sec}s", elapsed_sec=_time.perf_counter() - t0)

        elapsed = _time.perf_counter() - t0
        if final_task.status == 3:
            # failed
            msg = ""
            if isinstance(final_task.result, dict):
                msg = str(final_task.result.get("message", "")) or ""
            return QueryPlan(type="error", text=text, error=f"task_failed: {msg[:200]}", elapsed_sec=elapsed)

        # 3. Parse action
        return self._parse_dataseek_action(final_task, text, elapsed)

    @staticmethod
    def _parse_dataseek_action(
        task: AsyncTaskInfo,
        fallback_text: str,
        elapsed: float,
    ) -> QueryPlan:
        """从 DataSeek 任务结果抽第一个 action 成 QueryPlan."""
        from bowhead_bi_bridge.types import QueryPlan

        result = task.result or {}
        data = result.get("data") if isinstance(result, dict) else {}
        current = data.get("current") if isinstance(data, dict) else {}
        actions = current.get("result") if isinstance(current, dict) else []
        if not isinstance(actions, list) or not actions:
            return QueryPlan(type="unsupported", text=fallback_text, error="no_action_returned", elapsed_sec=elapsed)

        action = actions[0]
        if not isinstance(action, dict):
            return QueryPlan(type="unsupported", text=fallback_text, elapsed_sec=elapsed)

        a_type = str(action.get("type") or "unsupported")
        a_data = action.get("data") or {}

        plan = QueryPlan(type=a_type, raw_action=action, elapsed_sec=elapsed)

        if a_type == "chained":
            chain = a_data.get("chain") or []
            plan.chain = [dict(step) for step in chain if isinstance(step, dict)]
        elif a_type == "parallel":
            texts = a_data.get("texts") or []
            plan.texts = [str(x) for x in texts if x]
        elif a_type == "reports":
            reports = a_data.get("reports") or []
            plan.reports = [dict(r) for r in reports if isinstance(r, dict)]
        elif a_type in ("answer", "query"):
            # answer returns tokens + questions that can feed bi_ask_question directly.
            plan.type = "query"  # Normalize.
            plan.tokens = a_data.get("tokens") or []
            plan.questions = a_data.get("questions") or []
            plan.text = fallback_text
        elif a_type == "deepseek":
            plan.text = str(a_data.get("prompt") or fallback_text)
        else:
            plan.text = fallback_text

        return plan

    @staticmethod
    def _parse_task(data: dict[str, Any]) -> AsyncTaskInfo:
        return AsyncTaskInfo(
            uid=data.get("uid", ""),
            task_type=int(data.get("taskType", 0) or 0),
            status=int(data.get("status", 0) or 0),
            params=data.get("params", {}) or {},
            result=data.get("result", {}) or {},
            creator=data.get("creator", ""),
            created_at=data.get("createdAt", ""),
            updated_at=data.get("updatedAt", ""),
        )

    # ─────────────────────────────────────────────────────────
    # User binding
    # ─────────────────────────────────────────────────────────
    async def fetch_account(self, username: str) -> dict[str, Any] | None:
        """按 username/phone/email 反查 account；不存在返回 None，其他错抛异常。

        ⚠ 2026-09-08 116 真机订正：文档说的「否则返回空」**不成立**。datarc 对
        未注册用户回 HTTP 200 + `errcode=400 errmsg='未找到相关用户'`，`_request`
        把它变成 `BIValidationError` 抛出来 —— 于是这个方法**从来没有返回过 None**。

        后果链（真机取证，turn t_e49a3a7ae0a9）：调用方
        `connector_context_service._resolve_identity` 的注释写着「fetch_account 只在
        网络/HTTP/API 错时抛（未找到返 None），一律当瞬时」，于是把「这个人在 datarc
        没账号」标成 `transient` ⇒ `datarc_absence_reason="transient"` ⇒ 工具错误串变成
        「临时上游故障，**别说用户没权限**，建议稍后重试」⇒ 用户看到「这不是权限问题，
        稍后重试」。**而它永远不会恢复** —— 真正该说的是「请管理员给你开账号」，
        那句文案就在 `tool_registry` 的 `no_identity` 分支里，只是走不到。

        姊妹方法 `fetch_workspace_member` 一直是对的（400 + 「未找到」→ None）；
        本方法照它对齐。`lookup_user_by_email` 里那段本地 workaround 也随之作废。
        """
        try:
            dr = await self._request(
                "POST",
                "openapi/v1/accounts/fetch/",
                json_body={"username": username},
            )
        except BIValidationError as e:
            msg = (e.errmsg or "") if hasattr(e, "errmsg") else str(e)
            if "未找到" in msg or "not found" in msg.lower():
                return None
            raise
        return dr.data if dr.data else None

    async def create_account(
        self,
        *,
        uid: str,
        email: str,
        nickname: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a password-disabled Datarc account for a federated user."""
        dr = await self._request(
            "POST",
            "openapi/v1/accounts/",
            json_body={
                "uid": uid,
                "email": email,
                "nickname": nickname,
                "metadata": metadata,
            },
        )
        return dr.data or {}

    async def fetch_workspace_member(
        self,
        datarc_workspace_id: str,
        username: str,
    ) -> dict[str, Any] | None:
        """按 account identity (login/phone/email) 反查 workspace 内的 member.

        命中 → 返完整 member dict（含 ``uid`` 即 memberUid、``account.uid`` 等）；
        未命中（datarc 返 errcode=400 errmsg='未找到相关成员'）→ None；其他错抛异常.
        """
        try:
            dr = await self._request(
                "POST",
                f"openapi/v1/workspaces/{datarc_workspace_id}/members/fetch/",
                json_body={"username": username},
            )
        except BIValidationError as e:
            # Treat 400 + "未找到相关成员" as a soft failure → None; let the caller provide an error with clearer context.
            msg = (e.errmsg or "") if hasattr(e, "errmsg") else str(e)
            if "未找到" in msg or "not found" in msg.lower():
                return None
            raise
        return dr.data or None

    async def lookup_user_by_email(
        self,
        email: str,
        *,
        datarc_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Sprint α G4 high-level wrapper. Soft-fail safe across both paths.

        If datarc_workspace_id provided: queries fetch_workspace_member → returns sanitized
        dict with `uid` + `account_uid` (no raw_response per Sec P1-S-10).

        If datarc_workspace_id None: queries fetch_account (also soft-fails on "未找到").

        Returns None when datarc has no match. Returns None on any network/auth
        error (logs warning, doesn't raise — caller can continue register).

        Phase 0 spike result: fetch_workspace_member proven soft-fail; uid field
        not positively verified in dev demo (no known test user emails), so
        defensive extraction: try `uid`, fallback to `memberUid`, `member_uid`.
        """
        if datarc_workspace_id:
            try:
                r = await self.fetch_workspace_member(datarc_workspace_id, email)
            except Exception as e:
                # Network/auth failure — log + return None, don't block register.
                # Batch 1/c11 (implementation review): honor the docstring's "logs warning" promise; swallowing errors silently makes
                # callers misclassify datarc downtime as datarc_lookup_not_found (assuming the user is absent from datarc).
                logger.warning(
                    "datarc.lookup_user_by_email workspace-path failed ws=%s err=%s: %s",
                    datarc_workspace_id,
                    type(e).__name__,
                    e,
                )
                return None
            if not r:
                return None
            uid = r.get("uid") or r.get("memberUid") or r.get("member_uid")
            acc = r.get("account")
            acc_uid = acc.get("uid") if isinstance(acc, dict) else None
            return {
                "uid": str(uid) if uid is not None else None,
                "account_uid": str(acc_uid) if acc_uid is not None else None,
            }

        # Account-only path (no workspace binding). Since 2026-09-08, `fetch_account` converts
        # "未找到" to None at the source (see its docstring), making the previous local workaround
        # obsolete. Keep the matching catch only defensively; the actual decision criterion lives solely at the source.
        try:
            r = await self.fetch_account(username=email)
        except Exception as e:
            # Batch 1/c11: as above, warn on network/auth failures instead of silently treating them as not-found.
            logger.warning(
                "datarc.lookup_user_by_email account-path failed err=%s: %s",
                type(e).__name__,
                e,
            )
            return None
        if not r:
            return None
        return {
            "account_uid": str(r.get("uid")) if r.get("uid") is not None else None,
        }

    async def list_account_projects(
        self,
        account_uid: str,
        *,
        role: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"offset": offset, "limit": limit}
        if role is not None:
            body["role"] = role
        dr = await self._request(
            "GET",
            f"openapi/v1/accounts/{account_uid}/projects/",
            # datarc documents GET with a body; also send the data in the query for compatibility.
            params=body,
        )
        return dr.data or []

    async def get_sso_auth_data(
        self,
        identity: str,
        *,
        project_id: int | str | None = None,
        urlencode: bool = True,
    ) -> str:
        """P4.5.16: 调 datarc 免登 endpoint 拿 authData.

        identity: user 的 login_name / phone / email (datarc 已存在 user)
        project_id: 可选，指定 workspace 落地
        return: authData 字符串 (urlencode=True 已 url-safe), append 到
                `https://{host}/#/?authData=` 即免登链接
        """
        body: dict[str, Any] = {"identity": identity, "urlencode": urlencode}
        if project_id is not None:
            body["project_id"] = int(project_id) if isinstance(project_id, str) and project_id.isdigit() else project_id
        dr = await self._request(
            "POST",
            "openapi/v1/auth/auth-data/",
            json_body=body,
        )
        data = dr.data or {}
        auth_data = data.get("authData") if isinstance(data, dict) else None
        if not auth_data:
            raise DatarcError(
                "datarc auth-data response missing authData",
                errcode=dr.errcode,
                errmsg=dr.errmsg,
                request_path=dr.request_path,
            )
        return str(auth_data)
