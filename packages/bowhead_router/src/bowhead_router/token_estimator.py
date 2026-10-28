"""Shared dependency-safe cl100k token estimation.

Counts are estimates, not provider usage or guaranteed bounds. Callers add
appropriate framing and uncertainty margins. Core retains compatibility exports
so input preparation and final Router fitting use the same implementation.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import tiktoken

_ENCODER_NAME = "cl100k_base"


@lru_cache(maxsize=1)
def _enc() -> tiktoken.Encoding:
    """Private cached encoder. Callers use `get_encoder()` instead."""
    return tiktoken.get_encoding(_ENCODER_NAME)


def get_encoder() -> tiktoken.Encoding:
    """Public alias for the cached encoder. For callers that need both
    `.encode()` and `.decode()` (e.g. kb splitter for truncation by token slice).
    """
    return _enc()


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a single string.

    Empty string short-circuits to 0 (skips encoder call for hot paths like
    tool_result instrumentation).
    """
    if not text:
        return 0
    # ``disallowed_special=()`` counts special tokens like ``<|endoftext|>`` as ordinary text instead of raising ValueError.
    # This is an estimator, not an encoder: pasted tokenizer documentation could crash an entire turn or permanently
    # invalidate all session history. Review (2026-09-05) caught this P1; none of the three new call sites caught the exception.
    return len(_enc().encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate `text` to at most `max_tokens` cl100k tokens.

    If already under cap, returns as-is (cheaper than encode+decode round-trip).
    """
    if max_tokens <= 0:
        return ""
    enc = _enc()
    tokens = enc.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


def count_tokens_of_obj(obj: Any) -> int:
    """Count tokens of an arbitrary object by JSON-serializing first.

    Mirrors the serialization pattern used by `subagent._preview`:
      try json.dumps(obj, ensure_ascii=False, default=str), fallback to str(obj).

    Sprint ω r3.1 §2.15 A.1.1 instrumentation: every `tool_result` event payload
    carries `result_token_count` measured BEFORE `_preview` truncates to 500
    chars. Phase B (ω-A.1.2, deferred to ω+1) reads this field to lock real
    p95/p99 caps per tool.
    """
    # P1-2 fix (Sprint ω 2026-05-12): broaden exception cover. json.dumps can
    # raise TypeError (unserializable), ValueError (NaN/Inf without allow_nan),
    # RecursionError (deeply-nested objects), OverflowError (large floats), or
    # any other Exception from `default=str` running __str__ on a hostile obj.
    # Instrumentation must never bubble — fall back to str() and last-resort
    # empty string.
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        try:
            s = str(obj)
        except Exception:
            s = ""
    return estimate_tokens(s)
