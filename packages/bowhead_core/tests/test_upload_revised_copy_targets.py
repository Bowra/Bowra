"""Revising this person's upload yields a new file named like the deck editor's copy, linked to the upload.

pptx dogfood 2026-11-25 (docs/sprints/pptx-upload-edit-dogfood-2026-11-23/spike.md): the helper was told to
keep the upload's file name, so the user got a second file with that name and no link to the upload.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.artifact_presentation import PresentationUnavailable
from bowhead_core.artifact_store import Artifact
from bowhead_core.delivered_files import FileTarget, file_target_context_line, resolve_revise_files, revised_files_facts, upload_copy_name
from bowhead_core.presentation_edit import edited_copy_filename


def _file(artifact_id: str, name: str, *, kind: str, created_by: str = "u") -> Artifact:
    return Artifact(
        id=artifact_id,
        filename=name,
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=10,
        source="upload" if kind == "user_upload" else "python_sandbox",
        path=f"/x/{artifact_id}",
        canvas_id=None,
        created_by=created_by,
        created_at="2026-11-25T01:00:00+00:00",
        kind=kind,
        created_by_turn=None if kind == "user_upload" else "t1",
    )


@pytest.mark.parametrize(
    ("filename", "language", "expected"),
    [
        ("区域销售.pptx", "zh-CN", "区域销售（已修改）.pptx"),
        ("Q3 report.xlsx", "en-US", "Q3 report (edited).xlsx"),
        ("notes", "fr-FR", "notes (modifié)"),
        ("bericht.docx", "xx-XX", "bericht (edited).docx"),
    ],
)
def test_the_copy_name_uses_the_editor_marker_and_keeps_the_type(filename: str, language: str, expected: str) -> None:
    assert edited_copy_filename(filename, language) == expected


def test_only_this_persons_own_upload_gets_a_copy_name() -> None:
    assert upload_copy_name(_file("a", "deck.pptx", kind="user_upload"), user_id="u", ui_language="zh-CN") == "deck（已修改）.pptx"
    assert upload_copy_name(_file("a", "deck.pptx", kind="user_upload", created_by="other"), user_id="u", ui_language="zh-CN") == ""
    assert upload_copy_name(_file("a", "deck.pptx", kind="deliverable"), user_id="u", ui_language="zh-CN") == ""


class _Files:
    def __init__(self, *artifacts: Artifact) -> None:
        self.members = {artifact.id: artifact for artifact in artifacts}

    async def list_for_session(self, *, session_id, user_id, artifact_id=None, limit=100, **_):
        found = self.members.get(artifact_id)
        return [found] if found else []


@pytest.mark.asyncio
async def test_a_revision_target_upload_carries_its_copy_name(monkeypatch: pytest.MonkeyPatch) -> None:
    async def available(ctx, artifact_id, *, produced=False):
        artifact = ctx.artifact_store.members.get(artifact_id)
        if artifact is None:
            raise PresentationUnavailable("artifact_presentation_reference_unavailable")
        return artifact

    monkeypatch.setattr("bowhead_core.artifact_presentation.available_artifact", available)
    store = _Files(_file("art_up", "deck.pptx", kind="user_upload"), _file("art_team", "team.pptx", kind="user_upload", created_by="other"))
    ctx = SimpleNamespace(artifact_store=store, session_id="s1", user_id="u", turn_id="t2", language_context=SimpleNamespace(ui_language="en-US"))
    targets, refused = await resolve_revise_files(ctx, ["art_up", "art_team"])
    assert refused is None
    assert [(target.artifact_id, target.versionable, target.copy_name) for target in targets] == [
        ("art_up", False, "deck (edited).pptx"),
        ("art_team", False, ""),
    ]


def test_the_helper_is_told_what_saving_the_uploads_name_does() -> None:
    line = file_target_context_line(
        [
            FileTarget(artifact_id="art_up", name="deck.pptx", type="pptx", copy_name="deck（已修改）.pptx"),
            FileTarget(artifact_id="art_team", name="team.pptx", type="pptx"),
        ]
    )
    assert (
        "《deck.pptx》 was uploaded and stays unchanged: a deliverable saved with its filename becomes a new file named "
        "《deck（已修改）.pptx》 that records the upload as its source." in line
    )
    assert "《team.pptx》 cannot be versioned (it was uploaded or attached)" in line
    assert "《deck.pptx》 cannot be versioned" not in line


def test_lead_is_told_the_copy_name_its_source_and_only_the_newest_save() -> None:
    target = FileTarget(artifact_id="art_up", name="deck.pptx", type="pptx", copy_name="deck（已修改）.pptx")
    declared = {"purpose": "deliverable", "deliverable_outputs": ["deck.pptx"]}
    first = SimpleNamespace(
        error=None,
        arguments=declared,
        result={"output_artifacts": [{"artifact_id": "art_c1", "name": "deck.pptx", "saved_as": "deck（已修改）.pptx", "derived_from": "art_up"}]},
        nested=[],
    )
    again = SimpleNamespace(
        error=None,
        arguments=declared,
        result={
            "output_artifacts": [{"artifact_id": "art_c2", "name": "deck.pptx", "saved_as": "deck（已修改）.pptx"}],
            "recorded_as_new_version": [{"filename": "deck.pptx", "artifact_id": "art_c2", "previous_version": "art_c1"}],
        },
        nested=[],
    )
    assert revised_files_facts([target], [first])["new_files"] == [
        {"artifact_id": "art_c1", "name": "deck（已修改）.pptx", "based_on": "art_up", "recorded_as_version": False}
    ]
    assert revised_files_facts([target], [first, again])["new_files"] == [
        {"artifact_id": "art_c2", "name": "deck（已修改）.pptx", "recorded_as_version": False}
    ]
