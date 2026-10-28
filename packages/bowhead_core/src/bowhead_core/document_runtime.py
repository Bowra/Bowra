"""Fixed packaged document contract selection; never a deployment switch."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal


@lru_cache(maxsize=1)
def read_document_profile() -> Literal["legacy", "bowra-v1"]:
    """Reject incomplete or unknown build composition instead of changing behavior."""
    try:
        data = json.loads(Path(__file__).with_name("document-runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Invalid packaged document runtime profile") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
        or not isinstance(data.get("profile"), str)
        or data.get("profile") not in {"legacy", "bowra-v1"}
    ):
        raise RuntimeError("Invalid packaged document runtime profile")
    return data["profile"]


def community_deck_guidance(*, template_present: bool) -> str:
    """Describe the fresh-deck contract before creating files or staging templates."""
    if template_present:
        return (
            "## Presentation template limitation\n"
            "An uploaded PowerPoint is in scope. Bowra cannot preserve or update its template, masters, "
            "layout, charts, images or other complex objects. Unless the user has explicitly accepted a "
            "new independent basic deck, do not generate any file; return "
            'subagent_return(payload={"error": {"kind": "needs_user_input", "message": "This tool cannot preserve your PowerPoint template.", "clarification_payload": {"questions": [{"question": "Would a new basic text/table deck be acceptable?", "type": "free_text", "options": []}]}}}). Translate the message and question into the user\'s '
            'answer language. After explicit acceptance, read skill_read(name="bowra-pptx") and use '
            "its run.py create contract for the fresh deck. Never claim template fidelity, silently "
            "discard the template, or try "
            "an unavailable refresh helper. The uploaded file must remain unchanged.\n"
        )
    return (
        "## Fresh editable presentation\n"
        'Read skill_read(name="bowra-pptx") and its schema before invoking '
        'skill_invoke(name="bowra-pptx",script="run.py",input={"operation":"create",'
        '"title":"Report title","slides":[{"title":"Evidence","bullets":["Verified finding"]},'
        '{"title":"Results","columns":["Metric","Value"],"rows":[["Requests","240"]]}]}). '
        "The example values are placeholders, never evidence. Use only authorized facts from this task; "
        "retrieve missing evidence through permitted tools or request it. Each slide has title plus "
        "bullets OR columns/rows. Read the schema's size limits. No template, artifact_id, sections, "
        "image or chart input is supported. Never pass a host path or invent a renderer. "
        "This creates a new basic 16:9 text/table deck; there is no slide preview or layout-fidelity guarantee. "
        'Return subagent_return(payload={"title":"Report title","delivery_surface":"chat",'
        '"exec_summary":"A concise, truthful creation summary and format limitations"}). '
        "Omit deliverables and file paths: "
        "the backend registers only successful current-turn outputs as downloadable Artifacts.\n"
    )
