"""Existing Send admission validates selected chart scope against actual stores."""

import copy
from types import SimpleNamespace

import pytest
from bowhead_gateway.routes.chat import ChatAttachmentRef, _validate_chat_attachment_refs
from fastapi import HTTPException
from test_chart_selected_context import migrated_db as migrated_db
from test_chart_selected_context import prepared as prepared
from test_chart_selected_context import selected as selected


async def test_existing_admission_retains_typed_scope_and_rejects_stale_revision(selected):
    artifacts, _, _, ref, ctx = selected
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=artifacts)))
    kwargs = {
        "request": request,
        "runtime_ids": [ref["fileId"]],
        "user_id": ctx.user_id,
        "workspace_id": ctx.workspace_id,
        "session_id": ctx.session_id,
    }
    assert await _validate_chat_attachment_refs(refs=[ChatAttachmentRef.model_validate(ref)], **kwargs) == [ref]
    stale = copy.deepcopy(ref)
    stale["selectedContext"]["sectionRev"] += 1
    with pytest.raises(HTTPException) as failure:
        await _validate_chat_attachment_refs(refs=[ChatAttachmentRef.model_validate(stale)], **kwargs)
    assert failure.value.status_code == 409
    assert failure.value.detail == {"error": {"code": "chart_selection_unavailable"}}
