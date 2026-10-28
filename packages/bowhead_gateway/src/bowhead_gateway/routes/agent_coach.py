"""Authenticated read access to original Coach operation records."""

from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
from bowhead_core.agent_coach import ResourceOpenInput
from bowhead_core.agent_eval_store import AgentEvalStoreError
from bowhead_core.agent_revision_store import AgentRevisionError
from bowhead_core.coach_ci_delegation import CoachCIError
from bowhead_core.coach_operation_reference import OperationReadInput
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field

from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes.auth import _current_user

router = APIRouter()


class OperationReadBody(OperationReadInput):
    workspace_id: str = Field(pattern=r"^ws_[A-Za-z0-9_-]+$", max_length=160)
    session_id: str | None = Field(default=None, pattern=r"^s_[A-Za-z0-9_-]+$", max_length=160)


@router.post("/operations/read")
async def read_operation(body: OperationReadBody, request: Request, response: Response, user: User = Depends(_current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store, private"
    resource = AgentCoachResources(request, user, body.workspace_id, body.session_id or "")
    try:
        return await resource.operations.get(body.reference.model_dump())
    except (ValueError, AgentChangeProposalError, AgentEvalStoreError, AgentRevisionError, CoachCIError, HTTPException) as exc:
        raise HTTPException(404, detail={"error": {"code": "coach_operation_unavailable"}}, headers={"Cache-Control": "no-store, private"}) from exc
    except Exception as exc:
        raise HTTPException(503, detail={"error": {"code": "coach_operation_read_failed"}}, headers={"Cache-Control": "no-store, private"}) from exc


class ResourceEditorBody(ResourceOpenInput):
    workspace_id: str = Field(pattern=r"^ws_[A-Za-z0-9_-]+$", max_length=160)
    session_id: str | None = Field(default=None, pattern=r"^s_[A-Za-z0-9_-]+$", max_length=160)


@router.post("/resources/editor")
async def read_resource_editor(body: ResourceEditorBody, request: Request, response: Response, user: User = Depends(_current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store, private"
    resource = AgentCoachResources(request, user, body.workspace_id, body.session_id or "")
    try:
        return await resource.editors.read(body.reference.model_dump(), body.parent.model_dump() if body.parent else None)
    except (ValueError, HTTPException, AgentRevisionError) as exc:
        raise HTTPException(
            404, detail={"error": {"code": "coach_resource_editor_unavailable"}}, headers={"Cache-Control": "no-store, private"}
        ) from exc
    except Exception as exc:
        raise HTTPException(
            503, detail={"error": {"code": "coach_resource_editor_read_failed"}}, headers={"Cache-Control": "no-store, private"}
        ) from exc
