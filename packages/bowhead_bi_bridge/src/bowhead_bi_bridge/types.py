"""datarc 响应的标准化数据结构.

上层 Agent / Tool 只依赖这些 dataclass；DatarcClient 负责从 raw JSON 解析。
字段命名保留 datarc 风格（camelCase → snake_case 已转换），但 Bowhead 内部 ID 统一 str。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────
# Common envelope
# ─────────────────────────────────────────────────────────────
@dataclass
class DatarcResponse:
    """datarc 响应通用信封（业务状态码 + message + data）."""

    errcode: int
    errmsg: str
    data: Any = None
    request_path: str | None = None  # Filled by Bowhead for tracing.
    # Datarc's request id when the response carries one (X-Request-Id); "" otherwise.
    request_id: str = ""

    @property
    def ok(self) -> bool:
        return self.errcode == 200


# ─────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────
@dataclass
class DatarcWorkspaceMeta:
    id: str  # datarc returns int; Bowhead converts it to str.
    name: str
    desc: str = ""
    adapter: int = 0
    date_offset: int = -1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelMeta:
    uid: str
    name: str
    table: str = ""
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeasureMeta:
    id: int
    names: list[str]  # The first is the primary keyword.
    description: str = ""
    sort: int = 1  # 1=negative / -1=positive.
    # datarc `/measures/` already includes these; previously all were discarded (only id/names/description/sort remained).
    # unit_name: "元" / "人次"; currency_code: "CNY".
    unit_name: str = ""
    currency_code: str = ""
    used: bool = True
    # Numeric format from **model-layer** `/models/` measures[].format ("float" / "int" /
    # "percentage"); concept-layer `/measures/` lacks this field. percentage matters for attainment rates
    # / growth rates stored as 0-1 ratios: downstream uses it to decide whether to multiply by 100; values alone cannot tell
    # (attainment rate 1.02 = 102%, market share 2.45 = 2.45%). Empty string when absent; downstream uses the old fallback.
    value_format: str = ""
    # datarc `displayName`: `{"zh-CN": "营业额", "en-US": "GMV"}`.
    # ⚠ **Different from `names`**; never interchange them. `names` are **query identifiers** for the datarc NL engine
    # (used to build bi_query text); `display_names` are only **human-readable labels** (column names / answer wording).
    # Querying by a display name may fail to parse; en-US is not guaranteed to contain English either (demo ws=1,
    # measure 4 has `{"en-US": "销售量", "zh-CN": "销售量"}`).
    display_names: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_name(self) -> str:
        return self.names[0] if self.names else ""


@dataclass
class DimensionMeta:
    id: int
    names: list[str]
    type: str = "str"
    format: str = ""
    rating: int = 0
    is_keyword: bool = False
    auto_group: bool = False
    used: bool = False
    display_names: dict[str, str] = field(default_factory=dict)  # See MeasureMeta.display_names.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_name(self) -> str:
        return self.names[0] if self.names else ""


# ─────────────────────────────────────────────────────────────
# Data queries
# ─────────────────────────────────────────────────────────────
AskQuestionType = Literal["metric", "image", "unsupported"]


@dataclass
class MetricEntry:
    name: str
    content: str  # Formatted display value (e.g. "29.3亿"); the LLM should not use it directly because scaling is error-prone.
    value: str | float | None = None  # Actual numeric value (raw float; the LLM should prefer this).
    # ⚠ There is **no** unit_type here: the repository audit on 2026-08-25 found no consumers of the computed field
    #   (neither tool results nor core/gateway used it). It was removed with the keyword table used to infer it.
    #   `docs/i18n-number-placeholders-design.md` proposes passing unit_type in tool results so
    #   the LLM can write `[num:V:U]`, but this was never wired up. If implemented, metadata now flows through
    #   `DatarcClient.measure_metadata()` + `bowhead_core.units.resolve()`，
    #   Do not create another keyword table in the data-source layer.


@dataclass
class ExplanationEntry:
    """datarc 内部对问题的解析（dimensions/measures/filters + SQL）."""

    sql: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    dimensions: list[Any] = field(default_factory=list)
    measures: list[Any] = field(default_factory=list)
    filters: list[Any] = field(default_factory=list)
    date_filters: list[Any] = field(default_factory=list)
    keywords: list[Any] = field(default_factory=list)
    order: list[Any] = field(default_factory=list)
    limit: float | None = None
    having: list[Any] = field(default_factory=list)
    proportion: list[Any] = field(default_factory=list)


@dataclass
class AskQuestionResult:
    url: str
    type: AskQuestionType
    metrics: list[MetricEntry] = field(default_factory=list)  # When type=metric.
    image_base64: str = ""  # When type=image.
    tokens: list[Any] = field(default_factory=list)
    explanations: list[ExplanationEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CardResult:
    url: str
    title: str = ""
    card_type: int = 0
    card_tokens: str = ""
    sentence_type: int = 0
    tokens: list[Any] = field(default_factory=list)
    explanations: list[Any] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttributeRow:
    rank: int
    dimension: str
    attr: str
    measure_previous: float
    measure_current: float
    text: str = ""
    mask_key: str = ""
    units: list[Any] = field(default_factory=list)


@dataclass
class AttributeResult:
    title: str
    insight: str
    description: str
    sub_title: str
    measure_name: str
    measure_delta: float
    measure_dimension_max_delta: float
    rows: list[AttributeRow] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExplainResult:
    type: Literal["text", "llm"]
    text: str


@dataclass
class DownloadDataResult:
    """taskType=22 数据下载异步任务的结果（ZIP → CSV → list[dict]）.

    行级明细取数场景（group by / 清单 / 前 N / 否定过滤）用；详见
    docs/datarc-api-dump/async_task_download_data.md + docs/datarc-capability-boundaries.md.
    """

    columns: list[str]  # CSV headers.
    rows: list[dict[str, Any]]  # All rows.
    total_rows: int  # len(rows), without a limit.
    csv_filename: str = ""  # datarc-generated CSV filename (for debugging).
    download_ms: int = 0  # End-to-end duration.
    task_uid: str = ""  # datarc task uid.
    query_text: str = ""  # Original text from the caller (for debugging).
    # datarc reports the query semantics actually applied (new in taskType=22; see async_task/download_data in the OpenAPI docs).
    # Natural-language questions are parsed into models/measures/dimensions/time ranges/filters before execution; these fields report
    # what datarc actually ran. Previously they were discarded, leaving the model to infer query semantics from rows alone.
    # ⚠ `message_json` values are **human-readable display strings** that vary with the member account language (Chinese/English). datarc states:
    # "Do not rely on the text format inside array elements for queries." Pass them through for the model to read; do not parse them in code.
    explain_message: str = ""  # One-sentence explanation in plain language.
    explain_json: dict[str, Any] = field(default_factory=dict)  # Fixed English keys; all values are string[].
    # One question can produce multiple data items (multiple CSVs); only the first is read, and the rest were **silently discarded**.
    # These fields let callers say some data was not retrieved, instead of implying that the user received everything.
    download_errors: list[int] = field(default_factory=list)  # result.errors: indices of data items whose downloads failed.
    csv_count: int = 0  # Number of CSVs in the ZIP (>1 = only the first was read).
    # Datarc's own finish time of the export task (AsyncTaskInfo.updated_at); "" when not reported.
    datarc_completed_at: str = ""
    # Datarc's request id of the task creation, when the response carries one; "" otherwise.
    request_id: str = ""


# ─────────────────────────────────────────────────────────────
# Asynchronous LLM tasks
# ─────────────────────────────────────────────────────────────
@dataclass
class AsyncTaskInfo:
    uid: str
    task_type: int
    status: int  # 0=waiting / 1=running / 2=success / 3=failed
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    creator: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (2, 3)


# ─────────────────────────────────────────────────────────────
# DataSeek (taskType=25) — question classification / decomposition
# ─────────────────────────────────────────────────────────────
@dataclass
class QueryPlan:
    """DataSeek 返回的问题分类结果. 一个 task 可能返多个 action，我们取第一个主 action.

    type 枚举（来自 DataSeek action.type）:
      - "query"    / "answer"    —— 单次查询，有 tokens 和 questions 可直接 bi_ask_question
      - "chained"                —— 两步递进，chain=[{text, dimension}, {text}]
      - "parallel"               —— 多个并列，texts=[...]
      - "reports"                —— 匹配到已有报告，reports=[{uid, name, ...}]
      - "deepseek"               —— 无上下文退化成 LLM 对话，prompt=...
      - "unsupported" / "error"  —— 任务失败或无法识别，Lead 按 text fallback 走 ask_question
    """

    type: str
    text: str = ""  # query / answer / deepseek: executable questions after completion.
    chain: list[dict[str, Any]] = field(default_factory=list)  # chained: [{text, dimension?}, ...]
    texts: list[str] = field(default_factory=list)  # parallel: multiple items in parallel.
    reports: list[dict[str, Any]] = field(default_factory=list)  # reports: [{uid, name, desc}]
    tokens: list[dict[str, Any]] = field(default_factory=list)  # answer: datarc token parsing of the input.
    questions: list[list[dict[str, Any]]] = field(default_factory=list)  # answer: complete list of parsed query tokens.
    raw_action: dict[str, Any] = field(default_factory=dict)  # Original action dict, for future use.
    elapsed_sec: float = 0.0  # Duration (debugging / observability).
    error: str = ""  # Error details when the task fails.


# ─────────────────────────────────────────────────────────────
# Sprint ar2 — datarc /data/cut/ tokenization results
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CutToken:
    """datarc /data/cut/ 单个分词结果.

    prop 格式 (spike 2026-05-16 verified):
      - "d{id}=base64(value)"   — keywords coll 命中 dim_value, e.g. "d50=5LiK5rW3" (上海)
      - "d{id}"                  — 纯 dim 名 (用户提到维度名本身), e.g. "d54" (品牌)
      - "m{id}"                  — 指标, e.g. "m1" (销售)
      - "t{id}"                  — 时间字段, e.g. "t1" (日期)
      - "$like" / "$eq" 等       — operator
      - "dateRange_YYYYMMDDYYYYMMDD" — 时间范围 (datarc 自处理)
      - "value_base64"           — value 但未绑 dim_id (e.g. 跟 $like 同 unitIdx 的 value)
      - "unknown"                — datarc 自承没分类 (ar2 关键信号)
    """

    word: str
    prop: str
    unit_idx: int


@dataclass
class CutResult:
    """datarc /data/cut/ response (errcode 200 + data: list[CutToken])."""

    tokens: list[CutToken] = field(default_factory=list)

    def unknown_tokens(self) -> list[CutToken]:
        """prop == 'unknown' 的 token list (ar2 resolution 目标)."""
        return [t for t in self.tokens if t.prop == "unknown"]

    def value_without_dim(self) -> list[CutToken]:
        """prop 是 'value_xxx' 但没跟同 unit_idx 的 $like/d{id} 组合 (半精确 token).

        ar2 spike 例: '品牌包含阿迪的销售' → '阿迪的' prop=value_xxx 但同 unitIdx
        有 '$like' + 'd54' → 组合视为 contains predicate 整体放行;
        孤立 value_xxx 无 dim 上下文 → 真歧义, 走 Phase 2.
        """
        result: list[CutToken] = []
        for t in self.tokens:
            if not t.prop.startswith("value_"):
                continue
            # The same unit_idx has $like / d{id} → this value is already bound by a contains predicate; skip it.
            has_context = any((o.prop in ("$like", "$eq") or o.prop.startswith("d")) and o.unit_idx == t.unit_idx and o is not t for o in self.tokens)
            if not has_context:
                result.append(t)
        return result

    def is_clean(self) -> bool:
        """无 unknown, 无孤立 value_无dim → 视为 cut/ 已 resolved 干净."""
        return not self.unknown_tokens() and not self.value_without_dim()


# ─────────────────────────────────────────────────────────────
# datarc /keywords/recall/ keyword recall for dimension values
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KeywordCandidate:
    """一条召回到的维度值.

    ``field`` 是维度 id（``d21``），``word`` 是维度值原文。``prop`` 实测形如 ``d21=5YyX5Lqs``
    （``=`` 后段是编码值，**不是** ``word``，别解码取值），保留原样仅作排查用。
    ``tuids`` 是逗号分隔的模型 uid。同一个值挂在多个维度下会各出一条。
    """

    word: str
    distance: float
    field: str
    prop: str = ""
    tuids: str = ""


@dataclass
class KeywordRecall:
    """一个原始词的召回结果；datarc 保证 ``data`` 顺序与请求 ``words`` 一致."""

    source: str
    candidates: list[KeywordCandidate] = field(default_factory=list)
