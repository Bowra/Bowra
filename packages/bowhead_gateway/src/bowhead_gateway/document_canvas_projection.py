"""Apply original document read authority to every browser event surface."""

import logging
from collections.abc import Mapping
from typing import Any

from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.canvas_open import DocumentDraftOpen, validate_document_draft_open_in_tx
from bowhead_core.documents.request import DocumentRequestError
from fastapi import HTTPException
from pydantic import ValidationError

logger = logging.getLogger(__name__)


async def project_document_draft_open(request: Any, turn_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    from bowhead_gateway.routes.documents import _scope, _service

    try:
        pointer = DocumentDraftOpen.model_validate({key: value for key, value in payload.items() if key not in {"seq", "evt_seq", "parent_agent_id"}})
        if pointer.turn_id != turn_id:
            return {}
        scope = await _scope(request, session_id=pointer.session_id, job_id=pointer.job_id)
        if (scope.user_id, scope.team_id, scope.workspace_id) != (pointer.user_id, pointer.team_id, pointer.workspace_id):
            return {}
        async with open_shared_ctx(_service(request).delivery.path) as connection:
            await connection.execute("BEGIN")
            await validate_document_draft_open_in_tx(connection, pointer=pointer)
        return pointer.model_dump()
    except (ValidationError, HTTPException, DocumentRequestError):
        return {}
    except Exception as exc:
        # Optional opening cannot expose stored payloads when its reader is unavailable.
        logger.warning("document_canvas.projection_unavailable error_type=%s", type(exc).__name__)
        return {}
