"""safe_detail 的长度预算有**两份实现**，一份在服务端一份在浏览器，必须绑住。

服务端：`published_diagnostics.DIAGNOSTIC_POLICIES[*].max_length`（按 diagnostic_code
可配，`chat_llm_failed` 因为多带 upstream_type/code/param 三段是 360）。
浏览器：`lib/error-presentation.ts` 的 `MAX_SAFE_DETAIL_LENGTH`（一个全局常量）。

**单边改的后果不是「少显示一段」**：前端 `optionalValue` 把非法值变成 null，
`normalizeErrorDiagnostics` 见 null 就整体返回 undefined —— 整块诊断从页面消失，
用户连 phase / http_status 都看不到，比不发那几段更糟。

2026-08-17 就差点这么炸：服务端策略提到 360，前端还是 240，超过 240 的 detail 会在
浏览器侧被整条丢掉。这条闸让「服务端能发的最长 detail」永远 ≤「浏览器肯收的上限」。
"""

from __future__ import annotations

import pathlib
import re

import pytest
from bowhead_core.published_diagnostics import DIAGNOSTIC_POLICIES

_FRONTEND = pathlib.Path(__file__).resolve().parents[3] / "packages" / "bowhead_frontend"
_PRESENTATION = _FRONTEND / "lib" / "error-presentation.ts"


def _browser_limit() -> int:
    source = _PRESENTATION.read_text(encoding="utf-8")
    match = re.search(r"const MAX_SAFE_DETAIL_LENGTH\s*=\s*(\d+)\s*;", source)
    assert match, "没在 error-presentation.ts 里找到 MAX_SAFE_DETAIL_LENGTH —— 常量被改名/挪走了，这条闸会假绿"
    return int(match.group(1))


@pytest.mark.unit
def test_browser_accepts_every_detail_the_server_may_publish() -> None:
    server_max = max(policy.max_length for policy in DIAGNOSTIC_POLICIES.values())
    offenders = sorted(code for code, policy in DIAGNOSTIC_POLICIES.items() if policy.max_length > _browser_limit())

    assert not offenders, (
        f"以下 diagnostic_code 的 max_length 超过浏览器上限 {_browser_limit()}："
        f"{offenders}（服务端最大 {server_max}）。超限的 detail 不是被截断，而是让"
        "整块诊断在前端消失 —— 要么调低策略，要么同步抬高 MAX_SAFE_DETAIL_LENGTH。"
    )


@pytest.mark.unit
def test_the_parity_gate_actually_reads_the_frontend_constant() -> None:
    """positive control：确认上面那条不是在跟一个恒真值对拍。

    读不到常量就该炸（`_browser_limit` 里已断言），这里再钉住量级 —— 常量若被改成
    一个荒谬的大数（等于取消前端校验），也该有人来看一眼。
    """
    limit = _browser_limit()

    assert 120 <= limit <= 2048, f"MAX_SAFE_DETAIL_LENGTH={limit} 不在合理量级"
