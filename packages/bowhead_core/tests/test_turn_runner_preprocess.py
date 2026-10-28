"""Sprint chat-redesign-2026-05-21 Phase 1.1 — preprocess_attachments helpers.

Covers `_preprocess_attachments_with_events` event emit shape + failed metadata
aggregation. Does NOT exercise the full TurnRunner.run path (covered by
existing turn_runner_test); focuses on the new helper in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.attachments_injector import PreparedAttachment
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.turn_runner import (
    _classify_prep_error,
    _prep_to_step_done_payload,
    _preprocess_attachments_with_events,
)


def test_classify_prep_error_taxonomy() -> None:
    """Each error string maps to the expected StepError.type + transient flag."""

    def p(err):
        return PreparedAttachment(
            artifact_id="a",
            filename="f",
            mime="m",
            size=0,
            extracted_text="",
            truncated=False,
            summary_only=False,
            error=err,
        )

    assert _classify_prep_error(p("unsupported mime application/x-foo")) == (
        "unsupported_mime",
        False,
    )
    assert _classify_prep_error(p("attachment not found (missing or deleted)")) == (
        "corrupted",
        False,
    )
    assert _classify_prep_error(p("read failed: I/O error")) == ("corrupted", True)
    assert _classify_prep_error(p("vision OCR timeout")) == ("vision_failed", True)
    assert _classify_prep_error(p("timeout while parsing")) == ("timeout", True)
    assert _classify_prep_error(p("something weird")) == ("tool_exception", True)


def test_prep_to_step_done_payload_success_with_text() -> None:
    prep = PreparedAttachment(
        artifact_id="a",
        filename="f.xlsx",
        mime="application/x-xlsx",
        size=12345,
        extracted_text="row1\nrow2\nrow3",
        truncated=False,
        summary_only=False,
        error=None,
    )
    payload = _prep_to_step_done_payload(
        prep,
        card_id="t_1",
        step_id="parse_a",
        duration_ms=42,
    )
    assert payload["card_id"] == "t_1"
    assert payload["step_id"] == "parse_a"
    assert payload["step_type"] == "parse"
    assert payload["state"] == "success"
    assert payload["duration_ms"] == 42
    assert "已解析" in payload["output_preview"]
    assert "12345" in payload["output_preview"]
    assert "error" not in payload


def test_prep_to_step_done_payload_truncated_preview() -> None:
    prep = PreparedAttachment(
        artifact_id="a",
        filename="big.csv",
        mime="text/csv",
        size=99999999,
        extracted_text="x" * 8000,
        truncated=True,
        summary_only=False,
        error=None,
    )
    payload = _prep_to_step_done_payload(
        prep,
        card_id="t",
        step_id="s",
        duration_ms=10,
    )
    assert payload["state"] == "success"
    assert "截断" in payload["output_preview"]


def test_prep_to_step_done_payload_failure_with_error() -> None:
    prep = PreparedAttachment(
        artifact_id="a",
        filename="bad.pdf",
        mime="application/pdf",
        size=10,
        extracted_text="",
        truncated=False,
        summary_only=False,
        error="read failed: corrupted PDF",
    )
    payload = _prep_to_step_done_payload(
        prep,
        card_id="t",
        step_id="s",
        duration_ms=5,
    )
    assert payload["state"] == "failure"
    assert payload["error"]["type"] == "corrupted"
    assert payload["error"]["is_transient"] is True
    assert "corrupted PDF" in payload["error"]["message_zh"]
    assert "output_preview" not in payload


def test_render_block_inlines_only_full_inject() -> None:
    """M1 RD-6 Gate B: render_attachments_block inlines <extracted_text> only
    for inject=='full'; 'list'/'stub' assets render a metadata-only stub hint,
    never their body — defense-in-depth behind the turn_runner skip-parse gate.
    """
    from bowhead_core.attachments_injector import render_attachments_block

    # Use a non-Office body: DOCX/PPTX bodies are
    # deferred to metadata_only (documents/package_io.defer_office_content) and never inlined here.
    full = PreparedAttachment(
        artifact_id="a_full",
        filename="t.txt",
        mime="text/plain",
        size=1,
        extracted_text="DECK_BODY_TEXT",
        truncated=False,
        summary_only=False,
        error=None,
        inject="full",
    )
    listed = PreparedAttachment(
        artifact_id="a_list",
        filename="d.csv",
        mime="text/csv",
        size=1,
        extracted_text="CSV_BODY_TEXT",
        truncated=False,
        summary_only=False,
        error=None,
        inject="list",
    )
    out = render_attachments_block([full, listed])
    assert "<extracted_text>" in out
    assert "DECK_BODY_TEXT" in out  # full asset body IS inlined
    assert "CSV_BODY_TEXT" not in out  # list asset body NEVER inlined
    assert "未注入正文" in out  # list asset gets the stub hint


@pytest.mark.asyncio
async def test_preprocess_short_circuits_on_empty_snapshot() -> None:
    bus = TurnEventBus()
    out = await _preprocess_attachments_with_events(
        snapshot=[],
        bus=bus,
        turn_id="t_1",
        artifact_store=object(),
    )
    assert out == ([], None)


@pytest.mark.asyncio
async def test_preprocess_short_circuits_on_missing_store() -> None:
    bus = TurnEventBus()
    out = await _preprocess_attachments_with_events(
        snapshot=[{"artifact_id": "a"}],
        bus=bus,
        turn_id="t_1",
        artifact_store=None,
    )
    assert out == ([], None)


@pytest.mark.asyncio
async def test_preprocess_emits_card_created_and_per_attachment_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: 2 attachments → 1 card_created + 2 step_started + 2 step_done."""

    # Stub `prepare_attachments` (the module-level import inside turn_runner)
    # to return one success + one failure without touching real parsers.
    async def _fake_prepare(*, store: Any, artifact_ids: list[str]) -> list[PreparedAttachment]:
        aid = artifact_ids[0]
        if aid == "a_ok":
            return [
                PreparedAttachment(
                    artifact_id="a_ok",
                    filename="ok.csv",
                    mime="text/csv",
                    size=100,
                    extracted_text="col1,col2\n1,2",
                    truncated=False,
                    summary_only=False,
                    error=None,
                )
            ]
        return [
            PreparedAttachment(
                artifact_id=aid,
                filename="bad.pdf",
                mime="application/pdf",
                size=200,
                extracted_text="",
                truncated=False,
                summary_only=False,
                error="read failed: bad header",
            )
        ]

    monkeypatch.setattr(
        "bowhead_core.turn_runner.prepare_attachments",
        _fake_prepare,
    )

    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _collect() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    import asyncio

    consumer = asyncio.create_task(_collect())

    snapshot = [
        {"artifact_id": "a_ok", "filename": "ok.csv", "mime": "text/csv", "size": 100},
        {"artifact_id": "a_bad", "filename": "bad.pdf", "mime": "application/pdf", "size": 200},
    ]
    parsed, failed_meta = await _preprocess_attachments_with_events(
        snapshot=snapshot,
        bus=bus,
        turn_id="t_xyz",
        artifact_store=object(),  # store is unused by the stub
    )

    # close bus so consumer exits
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_xyz", payload={}))
    await consumer

    # Result shape
    assert len(parsed) == 2
    assert {p.artifact_id for p in parsed} == {"a_ok", "a_bad"}
    assert failed_meta is not None
    assert "bad.pdf" in failed_meta
    assert "bad header" in failed_meta

    # Event shape — note: step_list_card_created is now emitted by
    # turn_runner.run() unconditionally at turn start (universal card shell),
    # not inside preprocess. So preprocess only emits the per-attachment steps.
    types = [e.event_type for e in captured]
    assert types.count("step_list_card_created") == 0  # moved upstream
    assert types.count("step_started") == 2
    assert types.count("step_done") == 2
    assert types.count("done") == 1

    # step_started per attachment carries card_id, header_zh, step_id
    started = [e for e in captured if e.event_type == "step_started"]
    for ev in started:
        p = ev.payload
        assert p["card_id"] == "t_xyz"
        assert p["step_id"].startswith("parse_")
        assert p["step_type"] == "parse"
        assert p["header_zh"].startswith("解析 ")
        assert "started_at" in p

    # step_done per attachment carries state + duration_ms
    done = [e for e in captured if e.event_type == "step_done"]
    by_step_id = {e.payload["step_id"]: e for e in done}
    assert by_step_id["parse_a_ok"].payload["state"] == "success"
    assert by_step_id["parse_a_bad"].payload["state"] == "failure"
    assert by_step_id["parse_a_bad"].payload["error"]["type"] == "corrupted"
    assert all("duration_ms" in e.payload for e in done)


@pytest.mark.asyncio
async def test_preprocess_no_failed_metadata_when_all_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_prepare(*, store: Any, artifact_ids: list[str]) -> list[PreparedAttachment]:
        aid = artifact_ids[0]
        return [
            PreparedAttachment(
                artifact_id=aid,
                filename="ok.csv",
                mime="text/csv",
                size=10,
                extracted_text="x",
                truncated=False,
                summary_only=False,
                error=None,
            )
        ]

    monkeypatch.setattr(
        "bowhead_core.turn_runner.prepare_attachments",
        _fake_prepare,
    )

    parsed, failed_meta = await _preprocess_attachments_with_events(
        snapshot=[{"artifact_id": "a1", "filename": "ok.csv", "mime": "text/csv", "size": 10}],
        bus=None,  # no bus also exercised
        turn_id="t_2",
        artifact_store=object(),
    )
    assert len(parsed) == 1
    assert failed_meta is None
