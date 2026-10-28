"""LLM reason code 的前端注册表必须覆盖后端白名单。

上下文工程 sprint 批 4-c review 逮到：`llm_context_overflow` 后端四处都齐了，前端三个注册表一个没加，
`normalizeReasonCode` 把它归成 `lead_internal_error`、连 diagnostics 一起丢——比改前的 `llm_request_rejected` 还退化。
这条测试让「后端加了 reason code、前端没跟」在 Python CI 里就红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bowhead_core.llm_failure import LLM_REASON_CODES

_FE = Path(__file__).resolve().parents[3] / "packages" / "bowhead_frontend"


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_frontend_failure_reasons_cover_backend_llm_reason_codes(locale: str) -> None:
    messages = json.loads((_FE / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
    keys = set(messages["error_messages"]["reasons"].keys())
    missing = LLM_REASON_CODES - keys
    assert not missing, f"{locale}.json error_messages.reasons is missing: {sorted(missing)}"


@pytest.mark.unit
def test_frontend_error_messages_reason_list_covers_backend_llm_reason_codes() -> None:
    src = (_FE / "lib" / "error-messages.ts").read_text(encoding="utf-8")
    match = re.search(r"REASON_CODE_LIST\s*=\s*\[(.*?)\]\s*as const", src, re.DOTALL)
    assert match is not None, "error-messages.ts 找不到 REASON_CODE_LIST"
    codes = set(re.findall(r'^\s*"([a-z_]+)",', match.group(1), flags=re.MULTILINE))
    # Failed parsing with no matches must not pass vacuously: the list must actually contain llm_* reason codes.
    assert len({code for code in codes if code.startswith("llm_")}) >= len(LLM_REASON_CODES)
    missing = LLM_REASON_CODES - codes
    assert not missing, f"error-messages.ts REASON_CODE_LIST 缺: {sorted(missing)}"
