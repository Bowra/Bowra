"""Code blocks carry viewer aliases of their outputs, identical on every delivery path.

A numbered "Run Code" citation only carries its source alias. The report links it to the code that
produced it through `code_block.output_aliases`, derived in the viewer projection from the section
alone. The canvas detail, a live SSE event and a paged replay must therefore produce the same value,
and it must equal the alias of the matching sources entry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.source_access_projection import SOURCE_ALIAS_PREFIX
from bowhead_gateway.source_access_projection import (
    ViewerSafeCanvasProjector,
    project_event_payloads_for_viewer,
    project_live_event_payload_for_viewer,
)

SOURCES = {
    "id": "sec_sources",
    "kind": "sources",
    "entries": [{"artifact_id": "art_stdout", "tool": "python_sandbox", "label": "Run Code: q", "query": "q"}],
}
CODE_BLOCK = {
    "id": "sec_code",
    "kind": "code_block",
    "call_id": "call_1",
    "turn_id": "turn_1",
    "lang": "python",
    "code": "print(1)",
    "output_artifact_ids": ["art_stdout"],
}


class _ArtifactStore:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=value, created_by_turn="turn_1") for value in artifact_ids if value in self.existing]


class _SessionStore:
    async def get_turn(self, turn_id: str) -> SimpleNamespace | None:
        return SimpleNamespace(id=turn_id, session_id="ses_1")

    async def get_session(self, session_id: str) -> SimpleNamespace | None:
        return SimpleNamespace(id=session_id, workspace_id="ws_1")


class _Section:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def to_report_section(self) -> dict[str, object]:
        return self.value


class _CanvasStore:
    def __init__(self, sections: list[dict[str, object]]) -> None:
        self.canvas = SimpleNamespace(id="cv_1")
        self.sections = [_Section(section) for section in sections]

    async def get_canvas_by_session(self, session_id: str) -> SimpleNamespace:
        return self.canvas

    async def read_canvas_projection_state(self, canvas_id: str) -> tuple[SimpleNamespace, list[_Section]]:
        return self.canvas, self.sections


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([SOURCES, CODE_BLOCK]),
                artifact_store=_ArtifactStore({"art_stdout"}),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )


async def _detail_projection() -> list[dict[str, object]]:
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=[SOURCES, CODE_BLOCK],
        source_refs=[],
        report_artifact_closure=["art_stdout"],
        artifact_store=_ArtifactStore({"art_stdout"}),
    )
    projected, _ = projector.project([SOURCES, CODE_BLOCK])
    return projected


@pytest.mark.anyio
async def test_detail_code_block_aliases_match_the_sources_entry() -> None:
    sources, code = await _detail_projection()
    entry_alias = sources["entries"][0]["source_alias"]
    assert entry_alias.startswith(SOURCE_ALIAS_PREFIX)
    assert code["output_aliases"] == [entry_alias]
    # The raw section is never mutated by the projection.
    assert "output_aliases" not in CODE_BLOCK


@pytest.mark.anyio
async def test_live_and_replay_events_carry_the_same_aliases_as_the_detail() -> None:
    _, detail_code = await _detail_projection()
    event = ("canvas.section_added", {"seq": 3, "section": dict(CODE_BLOCK)})

    live = await project_live_event_payload_for_viewer(_request(), "turn_1", event[0], event[1], event_ordinal=3)
    replay = await project_event_payloads_for_viewer(
        _request(),
        "turn_1",
        [("narrative.delta", {"delta": "earlier"}), event],
        event_ordinals=[2, 3],
    )

    assert live == replay[1]
    assert live["section"]["output_aliases"] == detail_code["output_aliases"]
    assert "art_stdout" in live["section"]["output_artifact_ids"]


@pytest.mark.anyio
async def test_old_code_blocks_without_outputs_get_no_aliases() -> None:
    legacy = {key: value for key, value in CODE_BLOCK.items() if key != "output_artifact_ids"}
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=[SOURCES, legacy],
        source_refs=[],
        report_artifact_closure=["art_stdout"],
        artifact_store=_ArtifactStore({"art_stdout"}),
    )
    projected, _ = projector.project([SOURCES, legacy])
    assert "output_aliases" not in projected[1]
