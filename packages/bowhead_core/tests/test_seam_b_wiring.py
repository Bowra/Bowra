"""said-not-done Seam B —— **穿过闸本身**的接线测试。

`test_deliverable_ledger.py` / `test_overclaim_guard.py` 测的是两个模块的纯逻辑。
它们全绿**并不能证明这个闸在真 turn 里会跑** —— 接线错了（flag 名写错、块放在
early-return 之后、拿错 store）单测一条都不会红。
[[feedback_verify_at_the_gate_not_around_it]]：闸上游/下游验证都不算数。

所以本文件直接调 `_compose_turn_output`（收尾定稿的真实入口，新块就挂在它末尾），
断言**它返回的文案**变了或没变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _compose_turn_output

# Original production answer at 08-04T10:14 (that turn had zero pptx artifacts).
PROD_FALSE_CLAIM = "2026年7月营业额约9.80亿。完整报告已生成PPT演示稿，可直接下载。"


@dataclass
class _FakeBus:
    events: list[Any] = field(default_factory=list)

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


class _FakeArtifactStore:
    """★ 必须带 `filename`：真 `ArtifactStore.list_deliverables_for_turn` 返回的是完整
    `Artifact` 记录（`artifact_store.py:1507`）。此前这个 fake 只给 `id`，比真实现宽松 ——
    那样 `file_exts` 恒为空、`count_for_ext()` 恒返 -1 恒放行，
    **按格式对账的新逻辑会全部"绿"而实际从未被执行**（fake 比真协作者宽松 = 假绿）。
    """

    def __init__(self, n: int, ext: str = ".pptx") -> None:
        self._n = n
        self._ext = ext

    async def list_deliverables_for_turn(self, turn_id: str) -> list[Any]:
        return [SimpleNamespace(id=f"art_{i}", filename=f"out_{i}{self._ext}") for i in range(self._n)]


class _FakeCanvasStore:
    def __init__(self, sections: int) -> None:
        self._s = sections

    async def count_sections_for_turn(self, turn_id: str) -> int:
        return self._s


def _mk_runtime(*, files: int, sections: int, ext: str = ".pptx", lang: str | None = "zh-CN") -> Any:
    from bowhead_core.language_context import LanguageContext

    # The fixture turn is Chinese; `lang=None` models a turn without any language context.
    language_context = LanguageContext(ui_language=lang, output_language=lang) if lang else None
    ctx = SimpleNamespace(
        canvas_store=_FakeCanvasStore(sections),
        artifact_store=_FakeArtifactStore(files, ext),
        memory_notes_store=None,
        member_uid="",
        language_context=language_context,
        _m137_subagent_payloads=[],
    )
    return SimpleNamespace(parent_bus=_FakeBus(), parent_ctx=ctx, router=None)


def _mk_sctx() -> Any:
    return SimpleNamespace(
        turn_id="t_seamb",
        session_id="s_seamb",
        workspace_id="w",
        user_id="u",
        prior_turn_status=None,
    )


def _mk_result(text: str) -> Any:
    return SimpleNamespace(final_answer=text, payload=None, intent=None, tool_invocations=[])


async def _run(*, files: int, sections: int, text: str = PROD_FALSE_CLAIM, ext: str = ".pptx", lang: str | None = "zh-CN") -> str:
    _, out = await _compose_turn_output(
        runtime=_mk_runtime(files=files, sections=sections, ext=ext, lang=lang),
        sctx=_mk_sctx(),
        result=_mk_result(text),
        aggregated_payload={},
        user_text="做成一份可下载的PPT演示稿",
        peer_payloads=[],
    )
    return out


# ── Intervention ON: false claims must receive an honest correction ──────────────


@pytest.mark.asyncio
async def test_overclaim_gets_caveat_when_intervention_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心：声明「已生成PPT，可直接下载」+ 账单 files=0 → 追加诚实说明。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=0, sections=11)
    assert "没有成功生成可下载文件" in out, "闸没接上：谎报原样发出去了"


@pytest.mark.asyncio
async def test_overclaim_caveat_uses_english_without_a_language_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=0, sections=11, lang=None)
    assert "No downloadable file was successfully produced this turn" in out


@pytest.mark.asyncio
async def test_no_caveat_when_file_really_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 反向：真出了文件的轮次绝不能被扣帽子（误判比漏判更伤）。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=1, sections=11)
    assert "没有成功生成可下载文件" not in out


@pytest.mark.asyncio
async def test_no_caveat_when_answer_makes_no_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯分析答案（静默漏掉那一类）——闸不管，交给 ledger_empty 指标去看。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=0, sections=11, text="分析完成。7 月营业额约 9.8 亿。")
    assert "没有成功生成" not in out


# ── Default intervention + explicit rollback ─────────────────────────────────────


@pytest.mark.asyncio
async def test_intervention_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """DGF-04：定向语料通过后默认必须收敛成诚实终态。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.delenv("BOWHEAD_OVERCLAIM_GUARD_V2", raising=False)
    out = await _run(files=0, sections=11)
    assert "没有成功生成可下载文件" in out
    assert "已生成PPT演示稿" not in out


@pytest.mark.asyncio
async def test_intervention_can_be_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "0")
    out = await _run(files=0, sections=11)
    assert "没有成功生成可下载文件" not in out
    assert PROD_FALSE_CLAIM.split("。")[0] in out


@pytest.mark.asyncio
async def test_ledger_flag_off_is_full_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill-switch：整块关掉 = 完全恢复现状。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "0")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=0, sections=11)
    assert "没有成功生成可下载文件" not in out


# ── Best effort: store failures must not break finalization ───────────────────────


@pytest.mark.asyncio
async def test_store_failure_does_not_break_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")

    class _Boom:
        async def list_deliverables_for_turn(self, turn_id: str) -> list[Any]:
            raise RuntimeError("store down")

    rt = _mk_runtime(files=0, sections=11)
    rt.parent_ctx.artifact_store = _Boom()
    _, out = await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=_mk_result(PROD_FALSE_CLAIM),
        aggregated_payload={},
        user_text="q",
        peer_payloads=[],
    )
    assert out
    assert "已生成PPT演示稿" not in out
    assert "暂时无法核验" in out


@pytest.mark.asyncio
async def test_wrong_format_gets_caveat_when_intervention_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ deliverable-format-fidelity W1 端到端：用户要 PPT，本轮产出的是 PDF。

    总数口径 `files == 1` 看着"有产出"，旧闸整条放行（真机 t_1c11f59a7ba3 的形状）；
    按格式核之后必须补上诚实说明。走 `_compose_turn_output` 是为了穿过闸本身，
    不是只测纯函数。
    """
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "1")
    out = await _run(files=1, sections=11, ext=".pdf")
    assert "已成功生成：out_0.pdf" in out
    assert "PPTX 未生成" in out, "格式不符没被抓到：兜底产物把双条件挡掉了"


@pytest.mark.asyncio
async def test_wrong_format_can_use_explicit_measurement_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 _V2=0 保留只打点、不改用户可见文案的回滚路径。"""
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    monkeypatch.setenv("BOWHEAD_OVERCLAIM_GUARD_V2", "0")
    out = await _run(files=1, sections=11, ext=".pdf")
    assert "没有成功生成可下载文件" not in out


@pytest.mark.asyncio
async def test_guard_failure_is_logged_loudly_not_silently(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """★ 守卫抛异常必须留 warning —— prod 不开 debug，debug 级等于完全静默。

    真机 116 t_72011a27fce9 出现过「复算该报却零日志零修订」，当时无从判断是没跑到
    还是抛了异常，正因为这条路径没留痕。

    ⚠ 不能用"让 artifact_store 抛"来造这个态：`build_deliverable_ledger` 是
    **按项 best-effort** 的，store 炸了只让 files=0/-1，走的是 `ledger_unverified`
    分支，根本到不了守卫的 except。要覆盖 except 必须让取数入口本身炸。
    """
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")

    import bowhead_core.deliverable_ledger as _dl

    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("ledger build exploded")

    monkeypatch.setattr(_dl, "build_deliverable_ledger", _boom)

    with caplog.at_level("WARNING"):
        _, out = await _compose_turn_output(
            runtime=_mk_runtime(files=0, sections=11),
            sctx=_mk_sctx(),
            result=_mk_result(PROD_FALSE_CLAIM),
            aggregated_payload={},
            user_text="q",
            peer_payloads=[],
        )
    assert out, "守卫挂掉不能把收尾带崩"
    assert any("deliverable_ledger_guard_failed" in r.getMessage() for r in caplog.records), "守卫抛异常却没留 warning —— prod 上等于静默失效"
