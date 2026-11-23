"""Admit the Skills of a Skill-only `/` send as an ordinary Lead Turn.

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2. Send checks each
selected Skill with the same resolver as reference preparation (current read
access, exact version, read-only entry and size) before any Turn, receipt or
dispatch exists, so a refusal is definitive and carries `admission: not_started`.
The worker checks the same Skills again before Lead reads them.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_resolution import (
    SOURCE_CHANGED,
    SkillReadResolutionError,
    SkillReadSourceResolver,
    repeat_once_if_source_changed,
)
from bowhead_core.sqlite_domains import domain_database_path
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Refusal codes the Composer explains with a next step (chat-send-errors).
SELECTION_UNAVAILABLE = "skill_selection_unavailable"
SKILL_UNAVAILABLE = "selected_skill_unavailable"
SKILL_CHANGED = "selected_skill_changed"
# The Skill's record or the sender's access kept changing while it was checked (not an edit of the Skill).
SKILL_CHECK_INTERRUPTED = "selected_skill_check_interrupted"
SKILL_TOO_LARGE = "selected_skill_too_large"


def _refused(code: str) -> HTTPException:
    return HTTPException(409, detail={"error": {"code": code, "admission": "not_started"}})


def _code(error: Exception) -> str:
    text = str(error)
    if text == "reference_skill_version_changed":
        return SKILL_CHANGED
    if text == SOURCE_CHANGED:
        return SKILL_CHECK_INTERRUPTED
    if text == "reference_skill_material_too_large":
        return SKILL_TOO_LARGE
    return SKILL_UNAVAILABLE


async def admit_selected_skills(
    request: Request,
    skills: list[SkillResourceRef],
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
    session_id: str | None,
) -> tuple[SkillResourceRef, ...]:
    """Return the exact selection, or refuse before admission with a specific code."""
    orchestrator = request.app.state.orchestrator
    store = orchestrator.session_store
    try:
        scope = ReferenceRequestScope(team_id=team_id, user_id=user_id, workspace_id=workspace_id, session_id=session_id)
        sources = orchestrator.reference_skill_sources_factory(scope)
        if inspect.isawaitable(sources):
            sources = await sources
        if (
            type(sources) is not SkillReadSourceResolver
            or Path(domain_database_path(sources.db_path, "core")).resolve() != Path(domain_database_path(store.db_path, "core")).resolve()
        ):
            raise ValueError("selected_skill_source_scope_changed")
    except Exception as error:
        logger.warning("selected_skills.sources_unavailable team_id=%s cause=%s", team_id, type(error).__name__)
        raise _refused(SELECTION_UNAVAILABLE) from None
    for skill in skills:
        try:
            # A concurrent access change (another session editing groups) must not read as an edited Skill.
            await repeat_once_if_source_changed(lambda skill=skill: sources.prepare(skill, scope=scope))
        except (SkillReadResolutionError, SkillReadMaterialError) as error:
            code = _code(error)
            logger.info("selected_skills.refused team_id=%s skill=%s code=%s reason=%s", team_id, skill.id, code, error)
            raise _refused(code) from None
        except Exception as error:
            logger.warning("selected_skills.prepare_failed team_id=%s skill=%s cause=%s", team_id, skill.id, type(error).__name__)
            raise _refused(SKILL_UNAVAILABLE) from None
    return tuple(skills)
