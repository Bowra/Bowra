"""Custom Subagent REST API (#90 Step 1).

Project-level CRUD. SubagentRunner integration + Lead prompt wiring 在 Step 2.
"""

from __future__ import annotations

from typing import Any

from bowhead_core.custom_subagent_store import (
    MAX_PROMPT_LEN,
    CustomSubagentStore,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter()


def _store(request: Request) -> CustomSubagentStore:
    s: CustomSubagentStore | None = getattr(
        request.app.state,
        "custom_subagent_store",
        None,
    )
    if s is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "custom_subagent_store not configured",
        )
    return s


def _user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


class CreateDTO(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(max_length=MAX_PROMPT_LEN)
    allowed_tools: list[str] = Field(default_factory=list)
    auto_read_skills: list[str] = Field(default_factory=list)


class PatchDTO(BaseModel):
    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    auto_read_skills: list[str] | None = None
    status: str | None = None  # active | archived


class CustomSubagentDTO(BaseModel):
    id: str
    user_id: str
    name: str
    display_name: str
    description: str
    system_prompt: str
    allowed_tools: list[str]
    auto_read_skills: list[str]
    max_iterations: int
    status: str
    created_at: str
    updated_at: str


class CustomSubagentListDTO(BaseModel):
    items: list[CustomSubagentDTO]


def _to_dto(a: Any) -> CustomSubagentDTO:
    return CustomSubagentDTO(
        id=a.id,
        user_id=a.user_id,
        name=a.name,
        display_name=a.display_name,
        description=a.description,
        system_prompt=a.system_prompt,
        allowed_tools=a.allowed_tools,
        auto_read_skills=a.auto_read_skills,
        max_iterations=a.max_iterations,
        status=a.status,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.post(
    "",
    response_model=CustomSubagentDTO,
    status_code=status.HTTP_201_CREATED,
)
async def create(
    body: CreateDTO,
    request: Request,
    store: CustomSubagentStore = Depends(_store),
) -> CustomSubagentDTO:
    try:
        agent = await store.create(
            user_id=_user_id(request),
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            system_prompt=body.system_prompt,
            allowed_tools=body.allowed_tools,
            auto_read_skills=body.auto_read_skills,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return _to_dto(agent)


@router.get("", response_model=CustomSubagentListDTO)
async def list_agents(
    request: Request,
    store: CustomSubagentStore = Depends(_store),
) -> CustomSubagentListDTO:
    items = await store.list_for_user(_user_id(request))
    return CustomSubagentListDTO(items=[_to_dto(a) for a in items])


@router.get("/{agent_id}", response_model=CustomSubagentDTO)
async def get_agent(
    agent_id: str,
    request: Request,
    store: CustomSubagentStore = Depends(_store),
) -> CustomSubagentDTO:
    a = await store.get(agent_id)
    if a is None or a.user_id != _user_id(request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _to_dto(a)


@router.patch("/{agent_id}", response_model=CustomSubagentDTO)
async def patch_agent(
    agent_id: str,
    body: PatchDTO,
    request: Request,
    store: CustomSubagentStore = Depends(_store),
) -> CustomSubagentDTO:
    current = await store.get(agent_id)
    if current is None or current.user_id != _user_id(request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        updated = await store.patch(agent_id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    assert updated is not None
    return _to_dto(updated)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    request: Request,
    store: CustomSubagentStore = Depends(_store),
) -> None:
    a = await store.get(agent_id)
    if a is None or a.user_id != _user_id(request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    await store.delete(agent_id)
