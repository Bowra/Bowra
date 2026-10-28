"""Bowhead BI Bridge — datarc adapter + 工具实现.

公共 API：
    - :class:`DatarcClient` — datarc HTTP client（含统一响应映射）
    - :class:`DatarcError` — datarc 业务错误
    - :mod:`tools`          — Bowhead tool 实现（`bi.*`）

未来扩展（接别的 BI）：新增 `packages/bowhead_bi_bridge/src/bowhead_bi_bridge/<vendor>_client.py`
并按 bi-api-contract 规范实现相同接口。
"""

from bowhead_bi_bridge.datarc_client import DatarcClient
from bowhead_bi_bridge.datarc_client_factory import DatarcClientFactory
from bowhead_bi_bridge.errors import (
    BIArtifactPersistFailed,
    BIAsyncTaskTimeout,
    BIConnectionError,
    BINotFoundError,
    BIPermissionError,
    BIQueryFailedError,
    BIValidationError,
    DatarcError,
)
from bowhead_bi_bridge.types import (
    AskQuestionResult,
    AsyncTaskInfo,
    AttributeResult,
    CardResult,
    DatarcResponse,
    DatarcWorkspaceMeta,
    DimensionMeta,
    DownloadDataResult,
    ExplainResult,
    KeywordCandidate,
    KeywordRecall,
    MeasureMeta,
    ModelMeta,
    QueryPlan,
)

__all__ = [
    "AskQuestionResult",
    "AsyncTaskInfo",
    "AttributeResult",
    "BIArtifactPersistFailed",
    "BIAsyncTaskTimeout",
    "BIConnectionError",
    "BINotFoundError",
    "BIPermissionError",
    "BIQueryFailedError",
    "BIValidationError",
    "CardResult",
    "DatarcClient",
    "DatarcClientFactory",
    "DatarcError",
    "DatarcResponse",
    "DatarcWorkspaceMeta",
    "DimensionMeta",
    "DownloadDataResult",
    "ExplainResult",
    "KeywordCandidate",
    "KeywordRecall",
    "MeasureMeta",
    "ModelMeta",
    "QueryPlan",
]

__version__ = "0.1.0"
