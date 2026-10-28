"""datarc 错误类型层次.

对应 `bi-api-contract.md` §1.5 错误码表。业务错误由 DatarcClient 统一抛出；
上层 Agent catch 基类 :class:`DatarcError` 并按需降级。
"""

from __future__ import annotations

from typing import Literal

# Datarc's rate-limit wording. Datarc has no throttle code, so the text is the only signal; the
# client classifies it once and callers read the typed ``throttled`` flag or this predicate.
DATARC_THROTTLE_MARKERS = (
    "查询过于频繁",
    "请求过于频繁",
    "too many requests",
    "rate limit",
)

# Where a failure came from: the HTTP layer (the connection itself was refused, e.g. 401/403) or a
# Datarc business response (e.g. the member lacks permission). None when it was not set.
DatarcErrorSource = Literal["http", "business"]


def is_throttle_message(message: str | None) -> bool:
    """Whether a Datarc error text is a rate limit; a rate limit can be retried, other errors can't."""

    low = (message or "").lower()
    return any(marker in low for marker in DATARC_THROTTLE_MARKERS)


class DatarcError(Exception):
    """所有 datarc 相关异常的基类."""

    def __init__(
        self,
        message: str,
        errcode: int | None = None,
        errmsg: str | None = None,
        request_path: str | None = None,
        *,
        source: DatarcErrorSource | None = None,
        throttled: bool = False,
    ) -> None:
        super().__init__(message)
        self.errcode = errcode
        self.errmsg = errmsg
        self.request_path = request_path
        # Typed facts for callers that must not parse message text (Agent evaluation's call ledger).
        self.source: DatarcErrorSource | None = source
        self.throttled = throttled


class BIConnectionError(DatarcError):
    """网络层错误（DNS / TLS / 超时 / 5xx / 未知 HTTP 错误）."""


class BIAsyncTaskTimeout(BIConnectionError):
    """An accepted row-export task did not reach a terminal state before polling expired.

    This typed failure lets callers avoid repeated full polling windows during an
    asynchronous-task outage without matching changeable exception messages.
    """


class BIPermissionError(DatarcError):
    """401 / 403 或 datarc 业务层返回的权限错误."""


class BINotFoundError(DatarcError):
    """404 或 datarc 业务层返回的"资源不存在"."""


class BIValidationError(DatarcError):
    """400 参数校验 / body 格式错误."""


class BIQueryFailedError(DatarcError):
    """datarc 处理失败：无法搜索有效结果 / 结果为表格 / 解释失败等.

    errmsg 示例：
        - "用户问题无法搜索出有效结果"
        - "数据查询结果为表格"
        - "获取数据解释超时"
    """


class BIArtifactPersistFailed(DatarcError):
    """Sprint omicron Phase 0: bi_fetch_rows succeeded in fetching rows but
    artifact_store.put_bytes failed (disk full / DB lock / FK constraint).

    The current runtime uses soft failure: it logs the error, increments
    bowhead_bi_artifact_fail_total, and returns rows without artifact_id.

    Background: T3 prod audit 2026-05-11 found /api/v1/sessions/{sid}/artifacts
    returned empty across 15 dogfood turns — silent except-swallow had masked
    the failure mode. omicron-0 exposes it as metric first, then optional hard
    fail after 1-week baseline.
    """
