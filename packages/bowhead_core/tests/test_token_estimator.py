"""UT for Sprint ω Phase 1 `token_estimator.py`."""

from __future__ import annotations

import re
from pathlib import Path

import tiktoken
from bowhead_core.token_estimator import (
    _enc,
    count_tokens_of_obj,
    estimate_tokens,
    get_encoder,
    truncate_to_tokens,
)


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_ascii() -> None:
    # "Hello, world!" — 4 cl100k tokens per OpenAI docs.
    n = estimate_tokens("Hello, world!")
    assert 1 <= n <= 8, f"unexpected token count: {n}"


def test_estimate_tokens_chinese() -> None:
    # Chinese under-counts in cl100k vs DeepSeek tokenizer — accepted bias.
    n = estimate_tokens("数据分析")
    assert n >= 1


def test_estimate_tokens_is_deterministic() -> None:
    s = "the quick brown fox jumps over the lazy dog"
    assert estimate_tokens(s) == estimate_tokens(s)


def test_get_encoder_returns_tiktoken_encoding() -> None:
    enc = get_encoder()
    assert isinstance(enc, tiktoken.Encoding)
    # cl100k_base encoder name
    assert enc.name == "cl100k_base"


def test_get_encoder_is_cached() -> None:
    """lru_cache(maxsize=1) → same instance across calls."""
    a = get_encoder()
    b = get_encoder()
    assert a is b


def test_truncate_to_tokens_under_cap_unchanged() -> None:
    s = "short text"
    out = truncate_to_tokens(s, 1000)
    assert out == s


def test_truncate_to_tokens_above_cap_chops() -> None:
    # Build a string with at least 100 tokens, ask for 5.
    s = " ".join(["alpha"] * 200)  # ~200 tokens
    out = truncate_to_tokens(s, 5)
    assert estimate_tokens(out) <= 5
    # Must be a real prefix (decode roundtrip preserves leftmost tokens)
    assert s.startswith(out[:5])


def test_truncate_to_tokens_zero() -> None:
    assert truncate_to_tokens("anything", 0) == ""


def test_truncate_to_tokens_negative() -> None:
    """Edge case — defensive."""
    assert truncate_to_tokens("anything", -1) == ""


def test_module_has_zero_internal_bowhead_imports() -> None:
    """Foundation module invariant: no `from bowhead_core...` imports.

    Phase 1 contract — prevents circular import. Sibling to Phase 0
    `test_db_helper_has_no_internal_bowhead_imports`.
    """
    module_path = Path(__file__).resolve().parents[1] / "src/bowhead_core/token_estimator.py"
    src = module_path.read_text(encoding="utf-8")
    bowhead_imports = [line.strip() for line in src.split("\n") if re.match(r"^\s*from\s+bowhead_core\.", line)]
    assert bowhead_imports == [], f"token_estimator.py must have zero internal bowhead_core imports. Found: {bowhead_imports}"


def test_private_underscore_enc_not_in_public_api() -> None:
    """`_enc` is a private cache helper — callers must use `get_encoder()`.
    Test asserts the public path works (smoke); style enforcement is by
    convention (Python doesn't block underscore imports).
    """
    # Smoke: both _enc() and get_encoder() return the same object via cache.
    assert _enc() is get_encoder()


def test_count_tokens_of_obj_dict() -> None:
    """Pre-truncation token count of a dict (JSON-serialized)."""
    n = count_tokens_of_obj({"key": "value", "num": 42})
    assert n >= 1


def test_count_tokens_of_obj_list() -> None:
    n = count_tokens_of_obj([1, 2, 3, "four"])
    assert n >= 1


def test_count_tokens_of_obj_unserializable_falls_back_to_str() -> None:
    """Non-JSON-serializable types: e.g. a class instance — fallback to str(obj),
    don't raise. Mirrors `_preview`'s default=str behavior.
    """

    class Custom:
        def __repr__(self) -> str:
            return "<Custom>"

    n = count_tokens_of_obj(Custom())
    # str(Custom()) → "<Custom>" → encode → ≥1 token
    assert n >= 1


def test_count_tokens_of_obj_empty_dict() -> None:
    n = count_tokens_of_obj({})
    # "{}" → 1 cl100k token
    assert n >= 1


def test_count_tokens_of_obj_none() -> None:
    """None serializes to 'null' → 1 token."""
    n = count_tokens_of_obj(None)
    assert n >= 1


def test_count_tokens_of_obj_string() -> None:
    """A bare string serializes to "\"...\"" (quoted JSON). Verify it works
    and is roughly equivalent to estimate_tokens on the raw string.
    """
    s = "hello world"
    n = count_tokens_of_obj(s)
    # Quoted JSON adds 2-3 tokens for the quotes; ≥ estimate_tokens(s)
    assert n >= estimate_tokens(s)


# ─── Phase 1 P1-2 broadened exception cover (Sprint ω 2026-05-12) ──────────


def test_count_tokens_of_obj_nan_inf_does_not_raise() -> None:
    """ValueError path: json.dumps with NaN/Inf raises ValueError without
    allow_nan=False semantics. Broader except clause catches it; fallback to
    str(obj) emits something countable.
    """
    n = count_tokens_of_obj(float("nan"))
    assert n >= 1


def test_count_tokens_of_obj_circular_reference_does_not_raise() -> None:
    """json.dumps raises ValueError on circular reference. Fallback to str(obj)
    handles it (str renders as `[..., [...]]` truncated by repr safety)."""
    a: list = []
    a.append(a)
    n = count_tokens_of_obj(a)
    assert n >= 1


def test_count_tokens_of_obj_recursive_struct_does_not_raise() -> None:
    """Deeply nested non-cyclic structure (recursion-depth class). Should NOT
    crash with RecursionError per P1-2 fix — broader except clause catches it.
    """
    # Construct a list nested 2000 deep — exceeds default recursion limit.
    deep: list = []
    cur = deep
    for _ in range(2000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    n = count_tokens_of_obj(deep)
    # Either json.dumps succeeds (≥1 token), or fallback to str() (≥1 token).
    # Critical: no exception propagation.
    assert n >= 1


def test_count_tokens_of_obj_hostile_str_raises_handled() -> None:
    """default=str path runs __str__ on hostile object. If __str__ itself
    raises, the outer Exception clause must catch (P1-2 last-resort fallback).
    """

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("hostile __str__")

    n = count_tokens_of_obj(Hostile())
    # Last-resort fallback to "" → 0 tokens. Critical: no exception.
    assert n >= 0
