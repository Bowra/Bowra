from __future__ import annotations

import asyncio
import json

import pytest
from bowhead_core.canvas_rewrite import (
    _build_messages,
    _detect_citation_drift,
    _detect_value_drift,
    _resolve_prompt,
    stream_rewrite,
    validate_rewrite_request,
)


def test_resolve_prompt_supports_frontend_safe_presets() -> None:
    assert "仅修正语法" in _resolve_prompt("grammar", None)
    assert "相同事实范围" in _resolve_prompt("different_angle", None)


def test_validate_rewrite_request_rejects_retired_preset_without_custom() -> None:
    with pytest.raises(ValueError, match="unsupported rewrite preset"):
        validate_rewrite_request("with_data", None)


def test_custom_prompt_is_wrapped_with_citation_safety_contract() -> None:
    prompt = _resolve_prompt("with_data", "请补充更多数据和引用")
    assert "请补充更多数据和引用" in prompt
    assert "不得新增、删除或替换事实" in prompt
    assert "输出不得新增任何引用角标" in prompt  # M3-5: gen2 token wording →gen3 badges.


def test_build_messages_forbids_new_numbers_and_citations() -> None:
    messages = _build_messages(
        "同比增长 10% [1]",
        _resolve_prompt("concise", None),
        [{"id": "s_1", "title": "Revenue table"}],
    )
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "不能创造新数字或新 citation" in system
    assert "出现次数与原文一致" in system  # M3-5 wording change.
    assert "不得据此新增 citation" in user


def test_detect_citation_drift_flags_added_and_removed_tokens() -> None:
    """引用增删必须报（原契约）。M3-5 形态：只认 gen3 `[N]`——退役族 `[ref:`/`[num:`
    已无数据可匹配（M1 停写 + M2 洗净），继续参与比对只是噪声。"""
    assert _detect_citation_drift("收入提升 [1]", "收入提升 [1] [2]") == ["[2]"]  # Added.
    assert _detect_citation_drift("收入提升 [1] [2]", "收入提升 [1]") == ["[2]"]  # Removed.
    assert _detect_citation_drift("收入提升 [1] [2]", "收入提升 [2] [1]") == []  # Reordering is no drift.


def test_detect_citation_drift_allows_preserved_tokens() -> None:
    assert (
        _detect_citation_drift(
            "收入提升 10% [1]",
            "表达更顺一些, 收入提升 10% [1]",
        )
        == []
    )


# ── _detect_value_drift (WP3 review #2: rewritten verified-number values) ──


def test_detect_value_drift_flags_changed_digit_under_same_anchor() -> None:
    # Token count unchanged (still [1]), but 962.7 →999.9 must be detected.
    drift = _detect_value_drift("营收 962.7亿元 [1]", "营收 999.9亿元 [1]")
    assert "962.7亿元 [1]" in drift  # Original anchored value vanished.
    assert "999.9亿元 [1]" in drift  # New anchored value appeared without source.


def test_detect_value_drift_empty_when_value_preserved() -> None:
    assert (
        _detect_value_drift(
            "营收 962.7亿元 [1]，同比 +18% [1]",
            "表达更顺, 营收 962.7亿元 [1]，同比 +18% [1]",
        )
        == []
    )


def test_detect_value_drift_ignores_bare_year() -> None:
    # Bare year is claim-cite, not verified number; changes are no value drift.
    assert _detect_value_drift("2024年 [1] 表现", "2025年 [1] 表现") == []


# ── stream_rewrite → router.stream (WP3 2026-06-13 migration) ────


class _FakeRouter:
    """最小 Router 替身: stream() 按给定 deltas 产 LLMChunk, 记录被调 agent/req."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas
        self.calls: list[tuple[str, object]] = []

    async def stream(self, agent_type, req, profile=None, user_pref=None):
        from bowhead_router import LLMChunk

        self.calls.append((agent_type, req))
        for d in self._deltas:
            yield LLMChunk(delta=d)


def _collect_sse(gen) -> str:
    async def _run() -> str:
        out: list[str] = []
        async for b in gen:
            out.append(b.decode("utf-8"))
        return "".join(out)

    return asyncio.run(_run())


def test_stream_rewrite_routes_via_narrative_binding_and_emits_sse() -> None:
    router = _FakeRouter(["收入", "提升 ", "[1]"])
    sse = _collect_sse(stream_rewrite(router=router, selection_text="收入提升 [1]", preset="concise"))
    # Streaming chunks + terminal done.
    assert "event: chunk" in sse
    assert '"delta": "收入"' in sse
    assert "event: done" in sse
    assert "收入提升 [1]" in sse  # Reassembled final_text.
    # narrative binding; model blank lets configured Router decide, no hardcoding.
    assert router.calls and router.calls[0][0] == "narrative"
    assert router.calls[0][1].model == ""


def test_stream_rewrite_without_router_emits_error() -> None:
    # A0-1: no-router error frames normalize to code + readable Chinese copy (defensive; normally
    # gateway rejects before StreamingResponse).
    sse = _collect_sse(stream_rewrite(router=None, selection_text="x", preset="concise"))
    assert "event: error" in sse
    assert "team_model_unconfigured" in sse
    assert "未配置模型" in sse


def test_long_rewrite_uses_automatic_output_allowance() -> None:
    router = _FakeRouter(["A complete rewrite."])
    sse = _collect_sse(stream_rewrite(router=router, selection_text="Original evidence. " * 5000, preset="professional"))
    assert router.calls[0][1].max_output_tokens is None
    assert "event: done" in sse


@pytest.mark.parametrize("diagnostic_only", [False, True])
def test_exhausted_rewrite_never_emits_success(diagnostic_only) -> None:
    from bowhead_router.types import LLMResponseDiagnostics

    from bowhead_router import LLMChunk

    class Router:
        async def stream(self, agent_type, req):
            yield LLMChunk(delta="Incomplete rewrite")
            yield LLMChunk(
                finish_reason="stop" if diagnostic_only else "length",
                diagnostics=LLMResponseDiagnostics(transport_mode="stream", incomplete_reason="max_output_tokens") if diagnostic_only else None,
            )

    sse = _collect_sse(stream_rewrite(router=Router(), selection_text="Original evidence.", preset="professional"))
    assert "event: chunk" in sse
    assert "rewrite_output_exhausted" in sse
    assert "shorter passage" in sse
    assert "event: done" not in sse


class _ExplodingRouter:
    """stream() 直接抛异常的替身 — 验证运行期错误帧不泄漏异常类名/内部细节."""

    async def stream(self, agent_type, req, profile=None, user_pref=None):
        raise RuntimeError("secret-internal-detail sk-abc123")
        yield  # pragma: no cover - make this an async generator.


def test_stream_rewrite_runtime_error_frame_is_normalized() -> None:
    # A0-1 (report-edit-audit P0-3): raw `KeyError: "No binding ..."` previously
    # appeared in user error bar; now frames contain normalized code + readable copy only.
    sse = _collect_sse(stream_rewrite(router=_ExplodingRouter(), selection_text="x", preset="concise"))
    assert "event: error" in sse
    assert "rewrite_upstream_failed" in sse
    assert "RuntimeError" not in sse
    assert "secret-internal-detail" not in sse
    assert "sk-abc123" not in sse


def test_stream_rewrite_done_flags_citation_drift() -> None:
    # Model loses original [1] →done carries citation_drift / unverified_numbers.
    router = _FakeRouter(["收入提升"])
    sse = _collect_sse(stream_rewrite(router=router, selection_text="收入提升 [1]", preset="concise"))
    assert "event: done" in sse
    assert "citation_drift" in sse
    assert "[1]" in sse


@pytest.mark.parametrize(
    ("original", "rewritten"),
    [
        ("Revenue was 90000 yuan.", "Revenue was 900000 yuan."),
        ("Revenue was 90,000.25 EUR.", "Revenue was 900,000.25 EUR."),
        ("Umsatz: 90.000,25 EUR.", "Umsatz: 900.000,25 EUR."),
        ("Revenu : 90\u202f000,25 EUR.", "Revenu : 900\u202f000,25 EUR."),
        ("Revenue rose 9%.", "Revenue fell -9%."),
        ("Revenue in 2024.", "Revenue in 2025."),
        ("Revenue was stable [cite:1].", "Revenue was stable [cite:2]."),
        ("Revenue was stable.", "Revenue grew 10%."),
    ],
)
def test_rewrite_warns_about_changed_uncited_numbers_and_chart_references(original: str, rewritten: str) -> None:
    sse = _collect_sse(stream_rewrite(router=_FakeRouter([rewritten]), selection_text=original, custom_prompt="Expand the explanation"))
    done = json.loads(sse.split("event: done\ndata: ", 1)[1])
    assert done["unverified_numbers"], "Changed numbers or chart references must not look like a clean rewrite"


def test_citation_drift_preserves_turn_scoped_reference_identity() -> None:
    assert set(_detect_citation_drift("See [cite:1].", "See [cite:2].")) == {"[cite:1]", "[cite:2]"}


@pytest.mark.parametrize("number", ["90,000.25", "90.000,25", "90\u202f000,25", "90 000,25", "1.2e-4", "9万", "-9%"])
def test_rewrite_preserves_numeric_spelling_without_locale_guessing(number: str) -> None:
    original = f"Revenue: {number} [1]. See [cite:2]."
    rewritten = f"The reported revenue is {number} [1]. See [cite:2]."
    sse = _collect_sse(stream_rewrite(router=_FakeRouter([rewritten]), selection_text=original, preset="professional"))
    done = json.loads(sse.split("event: done\ndata: ", 1)[1])
    assert done["unverified_numbers"] == []


def test_rewrite_checks_scale_changes_and_repeated_citation_counts() -> None:
    from bowhead_core.canvas_rewrite import _detect_numeric_drift

    assert _detect_numeric_drift("9万元", "9亿元")
    assert _detect_citation_drift("[cite:1] [cite:1]", "[cite:1]") == ["[cite:1]"]
    assert _detect_value_drift("10% [1], 10% [1], 20% [1]", "10% [1], 20% [1], 20% [1]")


def test_cited_values_follow_frontend_cjk_and_scientific_notation_boundaries() -> None:
    assert "962.7亿元 [1]" in _detect_value_drift("营收962.7亿元 [1]", "营收999.9亿元 [1]")
    assert "1.2e-4 千万美元 [1]" in _detect_value_drift("1.2e-4 千万美元 [1]", "1.3e-4 千万美元 [1]")
