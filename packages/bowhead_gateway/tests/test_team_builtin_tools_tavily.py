"""Tavily 密钥验证：与运行时共用 tavily_client，按错误类型映射设置页错误码。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bowhead_core.tavily_client import TavilyError
from bowhead_gateway.routes.team_builtin_tools import _verify_tavily
from fastapi import HTTPException


async def _verify_with(error: TavilyError | None) -> None:
    post = AsyncMock(return_value=({"results": []} if error is None else None, error))
    with patch("bowhead_core.tavily_client.tavily_post", post):
        await _verify_tavily("tvly-key")
    assert post.await_args.args[:2] == ("tvly-key", "/search")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        None,
        TavilyError("tavily_plan_quota_exhausted", 432),
        TavilyError("tavily_spend_limit_reached", 433),
        TavilyError("tavily_rate_limited", 429),
    ],
)
async def test_valid_key_is_accepted_even_when_out_of_credits(error: TavilyError | None) -> None:
    await _verify_with(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (TavilyError("tavily_key_invalid", 401), 422, "tavily_api_key_invalid"),
        (TavilyError("tavily_url_not_supported", 403), 422, "tavily_api_key_invalid"),
        (TavilyError("tavily_unavailable", None, "ConnectError"), 502, "tavily_verification_unreachable"),
        (TavilyError("tavily_unavailable", 500), 502, "tavily_verification_failed"),
    ],
)
async def test_verification_failures_map_to_settings_error_codes(error: TavilyError, status: int, code: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _verify_with(error)
    assert exc_info.value.status_code == status
    assert exc_info.value.detail == code
