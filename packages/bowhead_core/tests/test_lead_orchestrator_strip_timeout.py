"""chat-truth L-BE tests: [data:art_*] strip + hard-timeout terminal ordering.

Covers tech-design §L:
- L-T1..L-T3: `_strip_data_markers` helper (data:art_* family + num:V:* family)
- L-T4: timeout path emits turn_failed before done. Since chat turn feedback D3 the
  failure card states the timeout; the branch writes no answer stub.
"""

from __future__ import annotations

from bowhead_core.agents.lead_orchestrator import _strip_data_markers

# ─────────────────────────────────────────────────────────────────
# L-T1..L-T3 · marker strip helper
# ─────────────────────────────────────────────────────────────────


def test_l_t1_strip_data_art_basic():
    """L-T1: basic [data:art_*] strip preserves surrounding text."""
    text = "foo [data:art_abc] bar"
    out = _strip_data_markers(text)
    assert "[data:art_abc]" not in out
    assert "foo" in out
    assert "bar" in out


def test_l_t2_strip_data_art_prod_shape():
    """L-T2: handles prod shape [data:art_0b81e38bf610] (12-hex-char id)."""
    text = (
        "客单贡献了增长的绝大部分，客单小幅提升起到辅助作用。"
        "`[data:art_6d9364aae2aa][data:art_b87550fa9788][data:art_1c18fbbf4ef6]"
        "[data:art_24b4f6c855f7]`"
    )
    out = _strip_data_markers(text)
    assert "[data:art_" not in out
    assert "客单贡献" in out


def test_l_t3_strip_num_v_family_too():
    """L-T3: also strips [num:V:1.5] family per ι-15 lesson
    (LLM emits internal-syntax tokens unreliably; backend post-process mandatory).
    """
    text = "GMV 同比 [num:V:1.5] 增长 [num:V:0.09]%"
    out = _strip_data_markers(text)
    assert "[num:V:" not in out
    assert "GMV" in out
    assert "增长" in out


def test_l_t3b_strip_idempotent_and_safe_on_empty():
    """L-T3b: helper is idempotent + safe on empty / None-ish."""
    assert _strip_data_markers("") == ""
    assert _strip_data_markers("plain text") == "plain text"
    # Idempotent
    text = "x [data:art_abc] y [num:V:1] z"
    once = _strip_data_markers(text)
    twice = _strip_data_markers(once)
    assert once == twice


# ─────────────────────────────────────────────────────────────────
# L-T4 · timeout path emits final_answer stub BEFORE turn_failed
# ─────────────────────────────────────────────────────────────────


def test_l_t4_timeout_path_emits_turn_failed_before_done() -> None:
    """L-T4: the hard-timeout branch closes the turn with turn_failed, then done.

    The full run_turn harness is too heavy, so this pins source order in the branch.
    """
    import inspect

    from bowhead_core.agents import lead_orchestrator as lo

    src = inspect.getsource(lo)
    idx_timeout = src.find("except TimeoutError")
    assert idx_timeout >= 0, "expected TimeoutError handler in lead_orchestrator"
    block = src[idx_timeout : idx_timeout + 4000]
    pos_failed = block.find('"turn_failed"')
    pos_done = block.find('"done"')
    assert pos_failed >= 0, "turn_failed emit missing in timeout branch"
    assert pos_done >= 0, "done emit missing in timeout branch"
    assert pos_failed < pos_done, "turn_failed MUST emit before done"
