"""Bounded page reads preserve exact selections and actionable continuation."""

import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.attachments_injector import MAX_INLINE_CHARS, PreparedAttachment, _extract_one
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_core.pptx_reading import (
    AttachmentReadQueryError,
    bind_pptx_continuation,
    validate_attachment_read_arguments,
    validate_attachment_read_result,
)
from pptx import Presentation
from pptx.util import Inches

pytestmark = pytest.mark.unit
SOURCE_HASH = "sha256:" + "a" * 64


def deck(texts: list[str]) -> bytes:
    presentation = Presentation()
    for text in texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        if text:
            slide.shapes.add_textbox(0, 0, Inches(8), Inches(3)).text = text
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def artifact(content: bytes) -> SimpleNamespace:
    return SimpleNamespace(id="art_source", mime=OFFICE_MIMES[".pptx"], filename="template.pptx", size=len(content))


def read(content: bytes, **query: object) -> PreparedAttachment:
    parsed = _extract_one(artifact(content), content, pptx_query=query)
    assert parsed.error is None, parsed.error
    parsed.read_info = bind_pptx_continuation(
        {**parsed.read_info, "full_input_available": False},
        tool="attachment_read",
        arguments={"artifact_id": "art_source", "expected_content_hash": SOURCE_HASH},
        maximum=30,
    )
    return parsed


def test_later_pages_remain_reachable_without_character_truncation() -> None:
    content = deck([f"Unique page {page}" for page in range(1, 62)])
    first = read(content)
    assert not first.truncated and first.read_info["view"] == "partial"
    assert first.read_info["coverage"]["complete_pages"] == list(range(1, 31))
    following = first.read_info["continuation"]["next"]["arguments"]
    assert following == {"artifact_id": "art_source", "expected_content_hash": SOURCE_HASH, "detail": "content", "slides": list(range(31, 61))}
    second = read(content, slides=following["slides"], detail=following["detail"])
    assert "Unique page 31" in second.extracted_text and "Unique page 60" in second.extracted_text
    assert second.read_info["continuation"]["next"]["arguments"]["slides"] == [61]
    last = read(content, slides=[61], detail="content")
    assert last.read_info["view"] == "complete"
    assert last.read_info["coverage"]["scope"] == "selection"
    assert last.read_info["continuation"]["reason"] == "selection_complete"


@pytest.mark.parametrize("pages", [[3, 10], [3, 2, 1]])
def test_explicit_order_does_not_invent_a_global_cursor(pages: list[int]) -> None:
    result = read(deck([f"Page {page:02}" for page in range(1, 12)]), slides=pages)
    assert result.read_info["coverage"]["returned_pages"] == pages
    assert result.read_info["continuation"] == {"scope": "selection", "available": False, "next": None, "reason": "selection_complete"}
    positions = [result.extracted_text.index(f"Page {page:02}") for page in pages]
    assert positions == sorted(positions)


def test_cut_page_is_read_again_alone_before_advancing() -> None:
    content = deck(["" if page not in (3, 10) else "A" * 2500 if page == 3 else "B" * 3000 for page in range(1, 11)])
    result = read(content, slides=[3, 10])
    assert result.truncated
    assert result.read_info["coverage"]["returned_pages"] == [3, 10]
    assert result.read_info["coverage"]["complete_pages"] == [3]
    assert result.read_info["limit"]["maximum"] == MAX_INLINE_CHARS
    assert result.read_info["continuation"]["next"]["arguments"]["slides"] == [10]
    single = read(content, slides=[10])
    assert not single.truncated and single.read_info["coverage"]["complete_pages"] == [10]


def test_oversized_single_page_does_not_offer_an_identical_retry() -> None:
    result = read(deck(["A" * 6000]), slides=[1])
    assert result.read_info["coverage"]["complete_pages"] == []
    assert result.read_info["continuation"] == {"scope": "selection", "available": False, "next": None, "reason": "page_content_limit"}
    assert result.read_info["recovery"] == {"action": "none", "retry_same_input": False}
    first_page = read(deck(["A" * 6000, "Later page"]))
    assert first_page.read_info["continuation"]["reason"] == "page_content_limit"


def test_index_preview_is_not_full_body_and_blank_pages_have_coverage() -> None:
    content = deck(["A" * 3000, ""])
    index = read(content, detail="index", slides=[1, 2])
    assert "A" * 121 not in index.extracted_text
    assert index.read_info["view"] == "complete" and index.read_info["coverage"]["content_kind"] == "index"
    assert index.read_info["coverage"]["complete_pages"] == [1, 2]
    body = read(content, slides=[1])
    assert "A" * 3000 in body.extracted_text and body.read_info["coverage"]["content_kind"] == "text_and_tables"


@pytest.mark.parametrize(
    "arguments",
    [
        {"slides": []},
        {"slides": [True]},
        {"slides": [1, 1]},
        {"slides": list(range(1, 32))},
        {"slides": "1-3"},
        {"detail": "objects"},
        {"expected_content_hash": "a" * 64},
        {"offset": 1},
    ],
)
def test_unsupported_query_is_rejected(arguments: dict) -> None:
    with pytest.raises(AttachmentReadQueryError):
        validate_attachment_read_arguments({"artifact_id": "art_source", **arguments})


def test_nonexistent_page_is_a_query_refusal() -> None:
    content = deck(["One page"])
    parsed = _extract_one(artifact(content), content, pptx_query={"slides": [2]})
    assert parsed.error == "attachment_read_query_invalid" and "1-page" in parsed.note


def test_page_receipt_survives_strict_round_trip_without_changing_generic_receipts() -> None:
    from bowhead_core.read_receipts import PptxReadCoverage, ReadInfo, read_info

    original = read(deck(["One", "Two"]), slides=[1]).read_info
    restored = ReadInfo.model_validate(original)
    assert isinstance(restored.coverage, PptxReadCoverage)
    assert restored.model_dump(mode="json") == original
    generic = read_info(view="none", unit="characters")
    assert generic["limit"] is None and generic["full_input_available"] is None and generic["coverage"]["total"] is None
    assert "continuation" not in generic
    for field, value in (("unexpected", "value"), ("detail", "objects"), ("complete_pages", [2])):
        with pytest.raises(ValueError):
            ReadInfo.model_validate({**original, "coverage": {**original["coverage"], field: value}})
    for key in ("detail", "slides", "expected_content_hash"):
        arguments = {field: value for field, value in original["continuation"]["next"]["arguments"].items() if field != key}
        with pytest.raises(ValueError):
            ReadInfo.model_validate(
                {**original, "continuation": {**original["continuation"], "next": {"tool": "attachment_read", "arguments": arguments}}}
            )


def test_table_row_cut_is_not_a_complete_page() -> None:
    presentation = Presentation(io.BytesIO(deck([""])))
    table = presentation.slides[0].shapes.add_table(2, 2, 0, 0, Inches(4), Inches(2)).table
    table.cell(0, 0).text = "Header"
    table.cell(1, 0).text = "A" * 6000
    output = io.BytesIO()
    presentation.save(output)
    parsed = read(output.getvalue(), slides=[1])
    assert parsed.truncated and parsed.read_info["coverage"]["complete_pages"] == []
    assert "table: 2 rows x 2 columns" in parsed.extracted_text


def test_retained_page_and_version_must_match_the_actual_receipt() -> None:
    result = read(deck(["One", "Two"]), slides=[2])
    arguments = {"artifact_id": "art_source", "slides": [2], "detail": "content", "expected_content_hash": SOURCE_HASH}
    receipt = {"ok": True, "artifact_id": "art_source", "content_hash": SOURCE_HASH, "read_info": result.read_info}
    validate_attachment_read_result(arguments, receipt)
    for change in ({"slides": [1]}, {"detail": "index"}, {"expected_content_hash": "sha256:" + "b" * 64}):
        with pytest.raises(ValueError):
            validate_attachment_read_result({**arguments, **change}, receipt)
    validate_attachment_read_result({"artifact_id": "art_source"}, {"ok": True, "artifact_id": "art_source", "content_hash": SOURCE_HASH})


@pytest.mark.asyncio
async def test_changed_source_wins_over_size_without_opening_the_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core import attachment_read

    @asynccontextmanager
    async def connection(_path: str):
        yield object()

    source = SimpleNamespace(session_id="s_test", attachment_ids=["art_source"])
    stored = SimpleNamespace(id="art_source", mime=OFFICE_MIMES[".pptx"], size=attachment_read.MAX_READ_BYTES + 1)
    backend = SimpleNamespace(read_bounded=AsyncMock())
    store = SimpleNamespace(get=AsyncMock(return_value=stored), backend=backend)
    ctx = SimpleNamespace(
        session_store=SimpleNamespace(db_path="unused"),
        artifact_store=store,
        turn_id="t_test",
        session_id="s_test",
        user_id="usr_test",
        workspace_id="ws_test",
        reference_skill_required=False,
    )
    monkeypatch.setattr(attachment_read, "open_shared_ctx", connection)
    monkeypatch.setattr(attachment_read, "_read_chat_turn_user_source_in_tx", AsyncMock(return_value=source))
    monkeypatch.setattr(attachment_read, "confirmed_content_hash", AsyncMock(return_value=SOURCE_HASH))
    changed = await attachment_read.read_attachment({"artifact_id": "art_source", "expected_content_hash": "sha256:" + "b" * 64}, ctx)
    assert changed["error"] == "attachment_read_changed" and "read_info" not in changed
    oversized = await attachment_read.read_attachment({"artifact_id": "art_source"}, ctx)
    assert oversized["error"] == "attachment_read_limit"
    assert oversized["read_info"]["limit"]["actual"] == stored.size
    backend.read_bounded.assert_not_called()
    store.get.return_value = None
    unavailable = await attachment_read.read_attachment({"artifact_id": "art_source"}, ctx)
    assert unavailable == {"ok": False, "error": "attachment_read_unavailable"}
