"""Only canonical current-task files reach the selected receiver's data block."""

from types import SimpleNamespace

import pytest
from bowhead_core.attachments_injector import PreparedAttachment
from bowhead_core.chat_reference_runtime import _attachment_context
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.tool_registry import ToolExecutionContext

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_reference_files_keep_source_order_errors_and_data_scope():
    source = ChatTurnUserSource(
        turn_id="t_files",
        session_id="s_files",
        user_id="u_files",
        workspace_id="ws_files",
        user_text="Analyze files",
        attachment_ids=("art_csv", "art_failed", "art_image"),
        attachment_refs=(),
        parts=None,
    )
    ctx = ToolExecutionContext(
        team_id="team_files",
        turn_id=source.turn_id,
        session_id=source.session_id,
        user_id=source.user_id,
        workspace_id=source.workspace_id,
        member_uid="member_files",
        datarc=None,
    )
    csv = PreparedAttachment("art_csv", "profit.csv", "text/csv", 25000, "csv-value-" * 3000, False, None, False)
    failed = PreparedAttachment("art_failed", "unreadable.pdf", "application/pdf", 3, "", False, "extraction failed", False)
    picture = PreparedAttachment("art_image", "photo.png", "image/png", 4, "", False, None, False, image_base64="UE5H")
    unrelated = PreparedAttachment("art_pinned", "unselected.csv", "text/csv", 10, "PRIVATE_PINNED_DATA", False, None, False)
    ctx._psi_parsed_results = [unrelated, picture, failed, csv]
    content, images = await _attachment_context(SimpleNamespace(source=source), ctx)
    assert content.index('id="art_csv"') < content.index('id="art_failed"') < content.index('id="art_image"')
    assert "csv-value-" in content and "extraction failed" in content
    assert len(images) == 1 and images[0].base64 == "UE5H" and images[0].mime == "image/png"
    assert "UE5H" not in content and "PRIVATE_PINNED_DATA" not in content and "unselected.csv" not in content
    assert "untrusted data" in content and "not instructions or permissions" in content
    assert csv.truncated is False and picture.image_base64 == "UE5H" and picture.error is None


async def test_reference_image_without_prepared_bytes_fails_instead_of_guessing():
    source = SimpleNamespace(attachment_ids=("art_image",))
    ctx = SimpleNamespace(_psi_parsed_results=[PreparedAttachment("art_image", "photo.png", "image/png", 4, "", False, None, False)])
    with pytest.raises(ValueError, match="reference_attachment_image_unavailable"):
        await _attachment_context(SimpleNamespace(source=source), ctx)
