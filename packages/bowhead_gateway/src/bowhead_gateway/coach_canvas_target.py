"""Authorize original resource editor baselines before paid Chat preparation."""

from __future__ import annotations

from bowhead_core.coach_canvas_target import AdmittedCoachCanvasTarget, CoachCanvasSendTarget
from fastapi import HTTPException, Request

from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes.auth import _current_user


async def admit_coach_canvas_target(
    request: Request, target: CoachCanvasSendTarget, *, user_id: str, team_id: str, workspace_id: str, session_id: str
) -> AdmittedCoachCanvasTarget:
    async def recheck() -> dict:
        try:
            user = await _current_user(request)
            actual = (user_id, team_id, workspace_id, session_id)
            original = (target.user_id, target.team_id, target.workspace_id, target.session_id)
            if actual != original or (user.id, user.team_id) != actual[:2] or target.dirty or target.pending_write:
                raise ValueError("editor scope or saved baseline unavailable")
            resources = AgentCoachResources(request, user, workspace_id, session_id)
            await resources._access()
            selected = target.target
            if selected.kind == "agent":
                from bowhead_core.agent_release_read_store import AgentReleaseReadStore

                value = await AgentReleaseReadStore(resources.store.db_path).working_copy(
                    team_id=team_id, agent_id=selected.id, actor_user_id=user_id
                )
                working = value["working_copy"]
                baseline = (
                    {
                        "id": working["id"],
                        "version": working["row_version"],
                        "base_release_id": None if working["base_release_id"] in {None, "", "None"} else working["base_release_id"],
                    }
                    if working
                    else None
                )
                if (
                    value["aggregate_row_version"] != selected.version
                    or value["live"]["release_id"] != selected.live_release_id
                    or baseline != (selected.working_copy.model_dump() if selected.working_copy else None)
                ):
                    raise ValueError("Agent editor baseline changed")
                projection = {
                    "target": selected.model_dump(),
                    "authoring_target": {
                        "kind": "agent_working_copy",
                        "agent_id": selected.id,
                        "working_copy_id": baseline["id"],
                        "version": baseline["version"],
                        "base_release_id": baseline["base_release_id"],
                    }
                    if baseline
                    else None,
                }
            else:
                reference = (
                    selected.model_dump()
                    if selected.kind == "skill"
                    else {"kind": "capability", "id": selected.id, "revision": str(selected.revision)}
                )
                value = await resources.editors.read(reference, exact=True)
                if selected.kind == "plugin":
                    plugin = value["target"]["plugin"]
                    if plugin["row_version"] != selected.version or plugin["content_hash"] != selected.content_hash:
                        raise ValueError("Plugin editor baseline changed")
                    authoring_target = {**selected.model_dump(), "kind": "team_plugin"}
                else:
                    skill = value["target"]["skill"]
                    authoring_target = (
                        {"kind": "personal_skill", "name": skill["name"], "id": selected.id, "revision": selected.revision}
                        if selected.source_kind == "user"
                        else None
                    )
                projection = {"target": selected.model_dump(), "authoring_target": authoring_target, "read_only": value["read_only"]}
            await resources._access()
            return projection
        except Exception as exc:
            raise HTTPException(409, detail={"error": {"code": "coach_canvas_target_unavailable", "admission": "not_started"}}) from exc

    await recheck()
    return AdmittedCoachCanvasTarget(target, recheck)
