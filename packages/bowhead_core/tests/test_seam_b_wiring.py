"""said-not-done Seam B: the Turn ledger read at the end of ``_compose_turn_output``.

``test_deliverable_ledger.py`` covers the ledger itself. These cases go through the real finishing entry, so a
wrong flag name, a block placed after an early return or the wrong store shows up here. The ledger only feeds the
worked-but-produced-nothing signal; the answer text is returned as written.
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
    """Returns full records with ``filename``, like ``ArtifactStore.list_deliverables_for_turn``."""

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


def _mk_result(text: str, *, dispatched: bool = False) -> Any:
    invocations = [SimpleNamespace(tool_name="subagent_spawn", result={"ok": True}, nested=[])] if dispatched else []
    return SimpleNamespace(final_answer=text, payload=None, intent=None, tool_invocations=invocations)


async def _run(*, files: int, sections: int, text: str = PROD_FALSE_CLAIM, dispatched: bool = False) -> str:
    _, out = await _compose_turn_output(
        runtime=_mk_runtime(files=files, sections=sections),
        sctx=_mk_sctx(),
        result=_mk_result(text, dispatched=dispatched),
        aggregated_payload={},
        user_text="做成一份可下载的PPT演示稿",
        peer_payloads=[],
    )
    return out


# ── The ledger's signal ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatched_work_that_left_nothing_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")
    with caplog.at_level("WARNING"):
        out = await _run(files=0, sections=0, dispatched=True)
    assert any("lead.worked_but_produced_nothing" in r.getMessage() for r in caplog.records)
    assert PROD_FALSE_CLAIM.split("。")[0] in out


@pytest.mark.asyncio
async def test_ledger_flag_off_reads_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "0")
    with caplog.at_level("WARNING"):
        await _run(files=0, sections=0, dispatched=True)
    assert not any("lead.worked_but_produced_nothing" in r.getMessage() for r in caplog.records)


# ── Best effort: store failures must not break finalization ───────────────────────


@pytest.mark.asyncio
async def test_store_failure_does_not_break_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DELIVERABLE_LEDGER", "1")

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
    assert PROD_FALSE_CLAIM.split("。")[0] in out


@pytest.mark.asyncio
async def test_ledger_failure_is_logged_loudly_not_silently(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Production debug logging is off, so a failing ledger read must leave a warning (an observed execution."""
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
    assert out
    assert any("deliverable_ledger_guard_failed" in r.getMessage() for r in caplog.records)
