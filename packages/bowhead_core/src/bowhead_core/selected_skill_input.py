"""The Skills a user selected with `/`, as readable messages in Lead's conversation.

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2 (slice S3). A
Skill-only `/` send admitted as an ordinary Lead Turn persists the exact
`SkillResourceRef` values with its canonical source. Before Lead runs, each one
is prepared again with the current user's access (exact version, read-only
entry, size) and rendered as `skill-read-text.v1` text. The messages follow the
user's request and are published with the conversation, so later Turns keep
them; a version already present in the history is not repeated.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from bowhead_router.types import Message

from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.skill_package_material import SkillPackageMaterialError
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from bowhead_core.skill_text import render_skill_text

SKILL_CONTENT_MESSAGE_NAME = "skill_content"
logger = logging.getLogger(__name__)


class SelectedSkillUnavailable(Exception):
    """A selected Skill can no longer be read by this user at its admitted version."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    resource: SkillResourceRef
    name: str
    text: str


def _guidance(name: str) -> str:
    return (
        f'The user selected the Skill "{name}" for the request above. Follow its method for that request. '
        f'When you dispatch the work, pass it with subagent_spawn skills=["{name}"] together with the user\'s own words; '
        "the dispatched Agent then receives the same method. Do not restate, narrow or widen the request. "
        "Split the work across Agents only when the method itself calls for independent parts."
    )


def _render(name: str, content: bytes, package_files: Sequence[tuple[str, int]] = ()) -> str:
    material = json.loads(content)
    entry = material["read_only_entry"]["entry_file"]
    files = [(item["path"], item["text"]) for item in material["files"]]
    body = next((text for path, text in files if path == entry), "")
    return render_skill_text(name=name, body=body, files=[(path, text) for path, text in files if path != entry], package_files=package_files)


async def load_selected_skills(ctx, *, turn_id: str) -> tuple[SelectedSkill, ...]:
    """Prepare the Turn's persisted selection again; refuse if any Skill is no longer readable."""
    # Every ordinary Lead run asks; most Turns carry no selection. A context
    # without a store, user or Space has none, and a failed read of the source
    # must not fail a Turn that admission already accepted: it runs without it.
    reader = getattr(getattr(ctx, "session_store", None), "get_chat_turn_user_source", None)
    user_id, workspace_id = getattr(ctx, "user_id", None), getattr(ctx, "workspace_id", None)
    if reader is None or not user_id or not workspace_id:
        return ()
    try:
        source = await reader(turn_id=turn_id, user_id=user_id, workspace_id=workspace_id)
    except Exception as error:
        logger.warning("selected_skills.source_unavailable turn=%s cause=%s", turn_id, type(error).__name__)
        return ()
    if source is None or not source.selected_skills:
        return ()
    from bowhead_core.ordinary_lead_material import _scope, _sources

    selected = []
    for resource in source.selected_skills:
        # A built-in Skill's identity is its catalog name; others resolve their name from the store.
        catalog_name = resource.id.removeprefix("builtin:") if resource.source_kind == "builtin" else ""
        try:
            sources = _sources(ctx, resource, catalog_name)
            resolved = await sources.prepare(resource, scope=_scope(ctx))
            # The selection is the whole package at this version: list every file after the body.
            package = await sources.prepare_runtime_package(resource, scope=_scope(ctx))
        except (SkillReadResolutionError, SkillReadMaterialError, SkillPackageMaterialError, OrdinaryContinuationUnavailable) as error:
            raise SelectedSkillUnavailable(str(error) or "selected_skill_unavailable") from None
        listing = sorted((path, len(data)) for path, data in package.material.files().items())
        selected.append(
            SelectedSkill(resource=resource, name=resolved.proof.name, text=_render(resolved.proof.name, resolved.material.content, listing))
        )
    return tuple(selected)


def selected_skill_messages(selected: Sequence[SelectedSkill], *, history: Sequence[Message]) -> list[Message]:
    """One message per Skill, or a short note when the same text is already in the history."""
    loaded = [message.content for message in history if message.role == "user" and message.name == SKILL_CONTENT_MESSAGE_NAME]
    messages = []
    for skill in selected:
        if any(isinstance(content, str) and skill.text in content for content in loaded):
            content = (
                f'The Skill "{skill.name}" selected for the request above is unchanged from the copy loaded earlier in this conversation. '
                + (_guidance(skill.name).split(". ", 1)[1])
            )
        else:
            content = _guidance(skill.name) + "\n\n" + skill.text
        messages.append(Message(role="user", content=content, name=SKILL_CONTENT_MESSAGE_NAME))
    return messages


DISPATCH_SKILLS_DESCRIPTION = (
    "Names of Skills the user selected with / in this conversation (in this request, or earlier and still shown above). "
    "The dispatched Agent receives each named method verbatim, so pass the user's own words as the task instead of restating the method."
)
_SKILL_COPY = re.compile(r'<skill_content name="([^"]*)">.*?</skill_content>', re.S)


def history_skill_texts(history: Sequence[Message]) -> dict[str, str]:
    """The newest copy of each Skill selected earlier and still in Lead's history."""
    texts: dict[str, str] = {}
    for message in history:
        if message.role == "user" and message.name == SKILL_CONTENT_MESSAGE_NAME and isinstance(message.content, str):
            for match in _SKILL_COPY.finditer(message.content):
                texts[html.unescape(match.group(1))] = match.group(0)
    return texts


def dispatch_skill_input(ctx, requested: object):
    """Return (`RequiredSkillModelInput`, None) for the named Skills, or (None, refusal).

    A Skill selected in this request uses its freshly prepared text. One
    selected earlier uses the copy Lead itself still reads in its history, so
    passing it on grants nothing Lead does not already have.
    """
    import hashlib

    from bowhead_core.skill_read_model_input import RequiredSkillModelInput

    def refused(correction: str) -> dict:
        return {
            "ok": False,
            "error_kind": "invalid_args",
            "reason_code": "dispatch_argument_invalid",
            "path": "skills",
            "error": f"No helper ran. Invalid skills. {correction}",
        }

    available = dict(getattr(ctx, "_history_skill_texts", None) or {})
    available.update({skill.name: skill.text for skill in getattr(ctx, "_selected_skills", None) or ()})
    if not available:
        return None, refused("No Skill selected with / is in this conversation; omit skills.")
    if not isinstance(requested, list) or not requested or not all(isinstance(name, str) and name.strip() for name in requested):
        return None, refused("Provide a list of selected Skill names.")
    names = list(dict.fromkeys(name.strip() for name in requested))
    unknown = [name for name in names if name not in available]
    if unknown:
        return None, refused(f"Use only Skills selected with / in this conversation: {', '.join(sorted(available))}.")
    content = "The user selected the method below for this task. Follow it.\n\n" + "\n\n".join(available[name] for name in names)
    return RequiredSkillModelInput(content, "sha256:" + hashlib.sha256(content.encode()).hexdigest()), None
