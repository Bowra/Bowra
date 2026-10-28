from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import bowhead_gateway.source_access_projection as source_projection
import pytest
from bowhead_core.source_access_projection import MAX_SOURCE_IDENTITIES, SOURCE_ALIAS_PREFIX
from bowhead_gateway.source_access_projection import (
    ViewerSafeCanvasProjector,
    ViewerSourceProjectionError,
    project_event_payloads_for_viewer,
    project_live_event_payload_for_viewer,
)


class _ArtifactStore:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.requested: list[str] = []

    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        self.requested = list(artifact_ids)
        return [SimpleNamespace(id=value) for value in artifact_ids if value in self.existing]


class _FailingArtifactStore:
    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        raise RuntimeError("storage unavailable")


@pytest.mark.anyio
async def test_projector_uses_report_closure_and_existing_artifact_for_access() -> None:
    sections = [
        {
            "id": "sec_sources",
            "kind": "sources",
            "entries": [
                {"artifact_id": "art_allowed", "label": "Revenue", "query": "revenue by region"},
                {"artifact_id": "art_outside", "label": "Secret", "query": "private query"},
            ],
        },
    ]
    store = _ArtifactStore({"art_allowed", "art_outside"})
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=sections,
        source_refs=[],
        report_artifact_closure=["art_allowed"],
        artifact_store=store,
    )
    projected, _ = projector.project(sections)

    allowed, denied = projected[0]["entries"]
    assert allowed["access_state"] == "linked_accessible"
    assert allowed["safe_preview_url"].endswith("/art_allowed/preview")
    assert denied["access_state"] == "linked_unavailable"
    assert "Secret" not in repr(denied)
    assert "art_outside" not in repr(projected)
    assert store.requested == ["art_allowed"]


@pytest.mark.anyio
async def test_tool_call_source_is_accessible_only_via_its_backing_artifact() -> None:
    refs = [{"id": "tl_call_1", "artifact_id": "art_1", "query": "revenue"}]
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=[],
        source_refs=refs,
        report_artifact_closure=["art_1"],
        artifact_store=_ArtifactStore({"art_1"}),
    )
    _, projected_refs = projector.project([], refs)

    assert len(projected_refs) == 1
    assert projected_refs[0]["id"].startswith(SOURCE_ALIAS_PREFIX)
    assert projected_refs[0]["safe_query"] == "revenue"
    assert "tl_call_1" not in repr(projected_refs)
    assert "art_1" in projected_refs[0]["safe_url"]


@pytest.mark.anyio
async def test_resolution_is_capped_and_overflow_sources_are_unavailable() -> None:
    refs = [{"id": f"art_{index:05d}", "artifact_id": f"art_{index:05d}", "label": f"Source {index}"} for index in range(MAX_SOURCE_IDENTITIES + 2)]
    all_ids = {ref["id"] for ref in refs}
    store = _ArtifactStore(all_ids)
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=[],
        source_refs=refs,
        report_artifact_closure=list(all_ids),
        artifact_store=store,
    )
    _, projected_refs = projector.project([], refs)

    assert len(store.requested) == MAX_SOURCE_IDENTITIES
    assert len(projected_refs) == MAX_SOURCE_IDENTITIES
    assert projector.access_by_identity[refs[-1]["id"]] is False


@pytest.mark.anyio
async def test_missing_alias_secret_is_allowed_only_for_source_free_report() -> None:
    source_free = await ViewerSafeCanvasProjector.build(
        root_secret=None,
        workspace_id="ws_1",
        session_id="ses_1",
        sections=[],
        source_refs=[],
        report_artifact_closure=[],
        artifact_store=None,
    )
    assert source_free.project([]) == ([], [])

    with pytest.raises(ViewerSourceProjectionError):
        await ViewerSafeCanvasProjector.build(
            root_secret=None,
            workspace_id="ws_1",
            session_id="ses_1",
            sections=[
                {
                    "id": "sources",
                    "kind": "sources",
                    "entries": [{"artifact_id": "art_1", "label": "Secret"}],
                }
            ],
            source_refs=[],
            report_artifact_closure=["art_1"],
            artifact_store=None,
        )


@pytest.mark.anyio
async def test_artifact_resolver_failure_degrades_sources_without_failing_report() -> None:
    sections = [
        {
            "id": "sources",
            "kind": "sources",
            "entries": [{"artifact_id": "art_1", "label": "Secret"}],
        },
    ]
    projector = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_1",
        session_id="ses_1",
        sections=sections,
        source_refs=[],
        report_artifact_closure=["art_1"],
        artifact_store=_FailingArtifactStore(),
    )
    projected, _ = projector.project(sections)
    assert projected[0]["entries"][0]["access_state"] == "linked_unavailable"
    assert "Secret" not in repr(projected)


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


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["available", "changed", "missing", "foreign"])
async def test_inline_chart_uses_same_current_evidence_projection_as_canvas(tmp_path: Path, state: str) -> None:
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.chart.presentation_plan import artifact_text_representation, compile_notes_plan
    from bowhead_core.chart.types import ChartColumn

    store = ArtifactStore(tmp_path / "artifacts.db", LocalBackend(tmp_path / "blobs"))
    await store.initialize()
    raw = b"value\n12\n"
    artifact = await store.put_bytes(content=raw, filename="values.csv", mime="text/csv", created_by="u", created_by_turn="other_turn")
    note = await store.put_bytes(
        content=b"Revenue includes returns.", filename="note.txt", mime="text/plain", created_by="u", created_by_turn="other_turn"
    )
    representation = await artifact_text_representation(note, store)
    assert representation is not None
    spec = {
        "kind": "table",
        "title": "Revenue",
        "data_artifact_id": artifact.id,
        "columns": [{"key": "value", "header": "Value", "type": "numeric"}],
        "rows": [{"value": 12}],
    }
    spec["presentation_plan"] = compile_notes_plan(
        spec,
        [
            {
                "id": "definition",
                "measure_keys": ["value"],
                "excerpt": representation[0],
                "source_ref": {"id": note.id, "artifact_id": note.id, "tool": note.source, "label": note.filename},
                "verified_content_digest": representation[1],
            }
        ],
        artifact_id=artifact.id,
        raw=raw,
        columns=[ChartColumn(key="value", header="Value", type="numeric")],
    )
    section = {"id": "sec_chart", "kind": "chart", "spec": spec}
    if state == "changed":
        Path(artifact.path).write_bytes(b"value\n99\n")
    elif state == "missing":
        Path(artifact.path).unlink()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([] if state == "foreign" else [section]),
                artifact_store=store,
                artifact_fetch_secret="root-secret",
            )
        )
    )
    payload = {"inline_artifacts": [{"kind": "file", "id": "art_file"}, section]}
    live = await project_live_event_payload_for_viewer(request, "turn_1", "lead.final.payload", payload)
    replay = (await project_event_payloads_for_viewer(request, "turn_1", [("lead.final.payload", payload)]))[0]
    canvas = (await project_event_payloads_for_viewer(request, "turn_1", [("canvas.section_added", {"section": section})]))[0]["section"]
    assert live == replay
    assert live["inline_artifacts"][1]["spec"] == canvas["spec"]
    assert live["inline_artifacts"][1]["spec"]["presentation_plan"]["dataset"]["availability"] == (
        "available" if state == "available" else "unavailable"
    )
    assert live["inline_artifacts"][0] == payload["inline_artifacts"][0]
    assert "availability" not in spec["presentation_plan"]["dataset"]


@pytest.mark.anyio
async def test_event_projection_preserves_transport_shape_and_redacts_source_carriers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy (FEATURE_SOURCE_ATTRIBUTION_V2=0) projection; v2 cases are covered below.
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    persisted = [
        {
            "id": "sec_1",
            "kind": "sources",
            "entries": [{"artifact_id": "art_1", "label": "Revenue"}],
        },
    ]
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore(persisted),
                artifact_store=_ArtifactStore({"art_1"}),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )
    events = [
        (
            "canvas.section_added",
            {
                "seq": 7,
                "section": {
                    "id": "sec_1",
                    "kind": "sources",
                    "entries": [
                        {"artifact_id": "art_1", "label": "Revenue"},
                        {"artifact_id": "art_missing", "label": "Payroll"},
                    ],
                },
            },
        ),
        (
            "turn.citation_registered",
            {"citation_seq": 1, "artifact_id": "art_1", "label": "Revenue"},
        ),
    ]
    projected = await project_event_payloads_for_viewer(request, "turn_1", events)

    assert projected[0]["seq"] == 7
    assert projected[0]["section"]["entries"][0]["safe_label"] == "Revenue"
    assert projected[0]["section"]["entries"][1]["access_state"] == "linked_unavailable"
    assert projected[1]["artifact_id"].startswith(SOURCE_ALIAS_PREFIX)
    assert projected[1]["label"] == "Revenue"
    assert "art_missing" not in repr(projected)
    assert events[0][1]["section"]["entries"][0]["artifact_id"] == "art_1"


@pytest.mark.anyio
async def test_old_narrative_replay_hides_chunked_inline_reasoning() -> None:
    events = [
        ("narrative.delta", {"card_id": "turn_1", "delta": "<thi"}),
        ("narrative.delta", {"card_id": "turn_1", "delta": "nk>private reasoning</think>答案"}),
        ("done", {"status": "completed"}),
    ]

    projected = await project_event_payloads_for_viewer(object(), "turn_1", events)

    assert "".join(row["delta"] for row in projected[:2]) == "答案"
    assert "private" not in repr(projected)
    assert (
        "".join(
            row["delta"]
            for row in (await project_event_payloads_for_viewer(object(), "turn_1", list(zip((kind for kind, _ in events), projected, strict=True))))[
                :2
            ]
        )
        == "答案"
    )
    assert events[0][1]["delta"] == "<thi"


@pytest.mark.anyio
async def test_old_reference_dispatch_errors_are_safe_and_not_retryable_on_replay() -> None:
    events = [
        ("tool_result", {"tool_name": "subagent_spawn", "error": "tool_exception: ValueError: reference_primary_already_dispatched"}),
        (
            "step_done",
            {
                "tool_name": "subagent_spawn",
                "output_preview": "tool_exception: ValueError: reference_primary_already_dispatched",
                "error": {
                    "type": "tool_exception",
                    "message_zh": "tool_exception: ValueError: reference_primary_already_dispatched",
                    "is_transient": True,
                },
            },
        ),
    ]

    projected = await project_event_payloads_for_viewer(object(), "turn_1", events)

    assert "reference_primary_already_dispatched" not in repr(projected)
    assert projected[1]["error"]["is_transient"] is False
    assert "不能重复" in projected[1]["error"]["message_zh"]


@pytest.mark.anyio
async def test_live_citation_uses_persisted_session_source_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def source_refs(_request: object, _session_id: str) -> list[dict[str, str]]:
        return [{"id": "call_1", "artifact_id": "art_live", "label": "Live result"}]

    monkeypatch.setattr(source_projection, "_source_refs_for_session", source_refs)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([]),
                artifact_store=_ArtifactStore({"art_live"}),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )
    events = [
        (
            "turn.citation_registered",
            {"citation_seq": 1, "artifact_id": "art_live", "label": "Live result"},
        ),
    ]

    projected = await project_event_payloads_for_viewer(request, "turn_1", events)

    assert projected[0]["artifact_id"].startswith(SOURCE_ALIAS_PREFIX)
    assert projected[0]["label"] == "Live result"
    assert "art_live" not in repr(projected)


@pytest.mark.anyio
async def test_lead_final_payload_sources_use_same_viewer_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source_refs(_request: object, _session_id: str) -> list[dict[str, str]]:
        return [{"id": "call_1", "artifact_id": "art_live", "label": "Canonical result"}]

    monkeypatch.setattr(source_projection, "_source_refs_for_session", source_refs)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([]),
                artifact_store=_ArtifactStore({"art_live"}),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )
    original_payload = {
        "title": "Answer",
        "exec_summary": "Revenue increased.",
        "sources": [
            {
                "artifact_id": "art_live",
                "label": "Revenue",
                "tool": "bi_query",
                "query": "select secret_column from revenue",
            },
            {
                "artifact_id": "art_outside",
                "label": "Payroll",
                "tool": "bi_query",
                "query": "select * from payroll",
            },
        ],
        "inline_artifacts": [{"kind": "file", "id": "art_output"}],
    }

    projected = await project_event_payloads_for_viewer(
        request,
        "turn_1",
        [("lead.final.payload", original_payload)],
    )

    assert projected[0]["title"] == "Answer"
    assert projected[0]["exec_summary"] == "Revenue increased."
    assert projected[0]["inline_artifacts"] == [{"kind": "file", "id": "art_output"}]
    assert projected[0]["sources"][0]["access_state"] == "linked_accessible"
    assert projected[0]["sources"][0]["safe_label"] == "Revenue"
    assert projected[0]["sources"][0]["safe_query"] == "select secret_column from revenue"
    assert "artifact_id" not in projected[0]["sources"][0]
    assert projected[0]["sources"][0]["safe_url"].endswith("/art_live")
    assert projected[0]["sources"][1]["access_state"] == "linked_unavailable"
    assert "artifact_id" not in projected[0]["sources"][1]
    assert "art_outside" not in repr(projected)
    assert original_payload["sources"][0]["artifact_id"] == "art_live"


@pytest.mark.anyio
async def test_invalid_lead_source_alias_is_stable_across_live_and_paged_replay() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([]),
                artifact_store=_ArtifactStore(set()),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )
    payload = {"sources": [{"label": "No identity", "tool": "web_search"}]}

    live = await project_event_payloads_for_viewer(
        request,
        "turn_1",
        [("lead.final.payload", payload)],
    )
    replay = await project_event_payloads_for_viewer(
        request,
        "turn_1",
        [("narrative.delta", {"delta": "x"}), ("lead.final.payload", payload)],
    )

    assert live[0]["sources"][0]["source_alias"] == replay[1]["sources"][0]["source_alias"]


@pytest.mark.anyio
async def test_v1_event_ordinal_keeps_live_and_replay_projection_byte_equivalent() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([]),
                artifact_store=_ArtifactStore(set()),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )
    payload = {"artifact_id": "missing-source", "label": "Private"}

    live = await project_live_event_payload_for_viewer(
        request,
        "turn_1",
        "turn.citation_registered",
        payload,
        event_ordinal=7,
    )
    replay = await project_event_payloads_for_viewer(
        request,
        "turn_1",
        [
            ("narrative.delta", {"delta": "earlier"}),
            ("turn.citation_registered", payload),
        ],
        event_ordinals=[6, 7],
    )

    assert live == replay[1]


@pytest.mark.anyio
async def test_source_free_stream_events_do_not_build_authorization_context() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    events = [
        ("narrative.delta", {"delta": "ordinary answer"}),
        ("canvas.section_chunk", {"section_id": "sec_1", "delta": "Revenue increased [1]."}),
        (
            "canvas.section_added",
            {"section": {"id": "sec_1", "kind": "narrative", "markdown": "Revenue increased [1]."}},
        ),
        ("lead.final.payload", {"exec_summary": "Done", "sources": []}),
    ]

    projected = await project_event_payloads_for_viewer(request, "turn_1", events)

    assert projected == [payload for _, payload in events]
    assert projected[1] is not events[1][1]


class _OwnedArtifactStore:
    """按 `created_by_turn` 建模归属的 artifact store 替身。

    ⚠ 真实 `ArtifactStore.get_many` 是 `SELECT * FROM artifacts WHERE id IN (...)`，
    **不带 workspace/session 条件** —— 隔离完全靠 closure。所以这里必须能表达
    「产物存在但属于别人」，否则安全那一半根本测不出来。
    """

    def __init__(self, owned_by: dict[str, str]) -> None:
        self.owned_by = owned_by

    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=value, created_by_turn=self.owned_by[value]) for value in artifact_ids if value in self.owned_by]


class _NoBatchArtifactStore:
    """老实现 / 替身只有 get()，没有批量接口 —— 拿不到归属证据。"""

    async def get(self, artifact_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=artifact_id)


def _event_request(artifact_store: object) -> SimpleNamespace:
    # No canvas sources section⇒closure only from events, exactly the chat-turn shape.
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore([]),
                artifact_store=artifact_store,
                artifact_fetch_secret="root-secret",
            ),
        ),
    )


_CHAT_EVENTS = [
    ("turn.citation_registered", {"citation_seq": 1, "artifact_id": "art_own", "label": "bi_query: 各城市 营业额"}),
]


@pytest.mark.anyio
async def test_chat_citation_backed_by_this_turns_artifact_is_accessible() -> None:
    """An event-only citation remains accessible when its artifact belongs to this turn."""
    projected = await project_event_payloads_for_viewer(
        _event_request(_OwnedArtifactStore({"art_own": "turn_1"})),
        "turn_1",
        _CHAT_EVENTS,
    )
    assert projected[0]["access_state"] == "linked_accessible"
    assert projected[0]["label"] == "bi_query: 各城市 营业额"
    assert projected[0]["artifact_id"].startswith(SOURCE_ALIAS_PREFIX)
    # Real ID never appears in exposed payload (alias contract unchanged).
    assert "art_own" not in repr(projected)


@pytest.mark.anyio
async def test_foreign_artifact_named_in_events_stays_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """★安全对照：产物**存在**但归属别的轮 → 必须照旧全剥。

    这是本次放宽的爆炸半径所在：`get_many` 不做租户过滤，closure 就是隔离闸。
    若归属校验被摘掉，模型只要在正文里吐出别人的 `art_`，这里就会变成
    `linked_accessible` 并铸出可访问链接 —— 跨租户泄漏。
    """
    # Legacy (FEATURE_SOURCE_ATTRIBUTION_V2=0) projection; v2 is covered by test_v2_foreign_artifact_restricted_strips_everything.
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    projected = await project_event_payloads_for_viewer(
        _event_request(_OwnedArtifactStore({"art_own": "turn_OTHER"})),
        "turn_1",
        _CHAT_EVENTS,
    )
    assert projected[0]["access_state"] == "linked_unavailable"
    assert "label" not in projected[0]
    assert "art_own" not in repr(projected)


@pytest.mark.anyio
async def test_artifact_absent_from_store_stays_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """产物在库里根本不存在（陈旧引用）→ 仍然不可访问。"""
    # Legacy (FEATURE_SOURCE_ATTRIBUTION_V2=0) projection; v2 is covered by test_v2_missing_artifact_is_linked_missing.
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    projected = await project_event_payloads_for_viewer(
        _event_request(_OwnedArtifactStore({})),
        "turn_1",
        _CHAT_EVENTS,
    )
    assert projected[0]["access_state"] == "linked_unavailable"
    assert "label" not in projected[0]


@pytest.mark.anyio
async def test_store_without_batch_lookup_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到归属证据时不放行 —— 宁可继续剥 label，也不扩大 closure。"""
    # Legacy (FEATURE_SOURCE_ATTRIBUTION_V2=0) projection.
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    projected = await project_event_payloads_for_viewer(
        _event_request(_NoBatchArtifactStore()),
        "turn_1",
        _CHAT_EVENTS,
    )
    assert projected[0]["access_state"] == "linked_unavailable"
    assert "label" not in projected[0]


# ─────────────────────────────────────────────────────────────────────────────
# Batch 2: event citations adopt batch 1 attribution checks (FEATURE_SOURCE_ATTRIBUTION_V2=1).
# Six-state contract = tech-design §4.1; sole state-mapping point = citation_presentation (same shape as c1).
# ─────────────────────────────────────────────────────────────────────────────

from bowhead_gateway.source_access_projection import (  # noqa: E402
    LiveEventProjectionCache,
)


class _V2SessionStore(_SessionStore):
    async def list_turns(self, session_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="turn_1"), SimpleNamespace(id="turn_prev")]


class _V2CanvasStore(_CanvasStore):
    def __init__(self, sections: list[dict[str, object]], section_canvas: dict[str, str] | None = None) -> None:
        super().__init__(sections)
        self._section_canvas = section_canvas or {}

    async def get_section_canvas_id(self, section_id: str) -> str | None:
        return self._section_canvas.get(section_id)


class _V2ArtifactStore:
    """id → (created_by_turn, canvas_id)。get_many 同时服务旧路 existing 校验。"""

    def __init__(self, arts: dict[str, tuple[str | None, str | None]]) -> None:
        self._arts = arts

    async def get_many(self, ids: list[str]) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=i, created_by_turn=self._arts[i][0], canvas_id=self._arts[i][1]) for i in ids if i in self._arts]


def _v2_request(
    persisted: list[dict[str, object]],
    arts: dict[str, tuple[str | None, str | None]] | None = None,
    section_canvas: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_V2SessionStore(),
                canvas_store=_V2CanvasStore(persisted, section_canvas),
                artifact_store=_V2ArtifactStore(arts or {}),
                artifact_fetch_secret="root-secret",
            ),
        ),
    )


def _cite_event(artifact_id: str, kind: str, label: str | None = "标签") -> tuple[str, dict[str, object]]:
    return (
        "turn.citation_registered",
        {"citation_seq": 1, "artifact_id": artifact_id, "label": label, "kind": kind},
    )


@pytest.mark.anyio
async def test_v2_chart_section_citation_is_labeled_with_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """§9-2-②：chart_section 从 linked_unavailable → linked_labeled + label + 锚点。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], section_canvas={"sec_c": "cv_1"})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("sec_c", "chart_section", "月度走势")])
    row = projected[0]
    assert row["access_state"] == "linked_labeled"
    assert row["label"] == "月度走势"
    assert row["safe_label"] == "月度走势"
    assert row["safe_anchor_id"] == "sec_c"
    assert row["artifact_id"].startswith(SOURCE_ALIAS_PREFIX)


@pytest.mark.anyio
async def test_v2_legacy_source_ref_artifact_stays_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7.2-c 保护：created_by_turn=NULL、canvas_id=NULL 的 legacy data_ref 只靠
    source_refs 臂闭合——事件面 scope 不喂它就回归 restricted（批 1 对拍 6 条实锤）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")

    async def source_refs(_request: object, _session_id: str) -> list[dict[str, str]]:
        return [{"artifact_id": "art_legacy", "label": "旧取数"}]

    import bowhead_gateway.source_access_projection as sap

    monkeypatch.setattr(sap, "_source_refs_for_session", source_refs)
    request = _v2_request([], arts={"art_legacy": (None, None)})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("art_legacy", "data_ref", "旧取数")])
    row = projected[0]
    # source_refs branch⇒OWNED; merge source_refs into rendered⇒accessible (same source as outputs).
    assert row["access_state"] == "linked_accessible"
    assert row["label"] == "旧取数"


@pytest.mark.anyio
async def test_v2_session_turn_artifact_without_exit_is_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], arts={"art_own": ("turn_prev", None)})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("art_own", "data_ref", "本会话产物")])
    row = projected[0]
    assert row["access_state"] == "linked_labeled"
    assert row["label"] == "本会话产物"
    assert "safe_anchor_id" not in row


@pytest.mark.anyio
async def test_v2_foreign_artifact_restricted_strips_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸 B 成对：FOREIGN 一个安全字段都不释放（与上一条互为 positive control）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], arts={"art_theirs": ("turn_other_session", "cv_other")})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("art_theirs", "data_ref", "别人的")])
    row = projected[0]
    assert row["access_state"] == "linked_restricted"
    assert "label" not in row
    assert "safe_label" not in row
    assert "safe_anchor_id" not in row
    assert "别人的" not in repr(projected)


@pytest.mark.anyio
async def test_v2_missing_artifact_is_linked_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], arts={})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("art_gone", "data_ref")])
    assert projected[0]["access_state"] == "linked_missing"
    assert "label" not in projected[0]


@pytest.mark.anyio
async def test_v2_unknown_kind_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([])
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("xyz_1", "totally_new_kind")])
    assert projected[0]["access_state"] == "unsupported"


@pytest.mark.anyio
async def test_v2_inflight_section_citation_is_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.4：在途（未落库）段落的引用按服务端铸的 id 判 OWNED，出口恒无。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], section_canvas={})  # Not found in store → use in-flight.
    events = [
        (
            "canvas.section_added",
            {
                "seq": 1,
                "section": {
                    "id": "sec_live",
                    "kind": "chart",
                    "spec": {"source_meta": {"artifact_id": "art_x", "label": "q"}},
                },
            },
        ),
        _cite_event("sec_live", "chart_section", "在途图"),
    ]
    projected = await project_event_payloads_for_viewer(request, "turn_1", events)
    row = projected[1]
    assert row["access_state"] == "linked_labeled"
    assert row["safe_anchor_id"] == "sec_live"


@pytest.mark.anyio
async def test_v2_flag_off_keeps_old_citation_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag=0 走旧路 bit-for-bit：chart_section 仍是 linked_unavailable、无新字段。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    request = _v2_request([], section_canvas={"sec_c": "cv_1"})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("sec_c", "chart_section", "月度走势")])
    row = projected[0]
    assert row["access_state"] == "linked_unavailable"
    assert "safe_anchor_id" not in row
    assert "safe_label" not in row


@pytest.mark.anyio
async def test_v2_live_fast_path_matches_slow_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.4-3 缓存快路与慢路 byte-equivalent（正确性以慢路为准）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    persisted: list[dict[str, object]] = []
    section_canvas = {"sec_c": "cv_1"}
    arts = {"art_own": ("turn_prev", None)}

    cite_sec = _cite_event("sec_c", "chart_section", "月度走势")
    cite_art = _cite_event("art_own", "data_ref", "本会话产物")
    warm = (
        "canvas.section_added",
        {
            "seq": 1,
            "section": {
                "id": "sec_warm",
                "kind": "chart",
                "spec": {"source_meta": {"artifact_id": "art_own", "label": "q"}},
            },
        },
    )

    # Slow reference path (independent per-event projection, no cache).
    slow_request = _v2_request(persisted, arts, section_canvas)
    slow_sec = await project_live_event_payload_for_viewer(slow_request, "turn_1", *cite_sec, event_ordinal=3)
    slow_art = await project_live_event_payload_for_viewer(slow_request, "turn_1", *cite_art, event_ordinal=4)

    # Fast path: source-bearing event fills cache, then two consecutive citations.
    fast_request = _v2_request(persisted, arts, section_canvas)
    cache = LiveEventProjectionCache()
    await project_live_event_payload_for_viewer(fast_request, "turn_1", *warm, event_ordinal=2, cache=cache)
    assert cache.projector is not None and cache.attribution is not None  # Cache filled.
    fast_sec = await project_live_event_payload_for_viewer(fast_request, "turn_1", *cite_sec, event_ordinal=3, cache=cache)
    fast_art = await project_live_event_payload_for_viewer(fast_request, "turn_1", *cite_art, event_ordinal=4, cache=cache)

    assert fast_sec == slow_sec
    assert fast_art == slow_art


@pytest.mark.anyio
async def test_v2_live_cache_inflight_survives_to_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """live：section_added 记进 in-flight 索引后，后续 citation 判 OWNED（在途）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], section_canvas={})
    cache = LiveEventProjectionCache()
    warm = (
        "canvas.section_added",
        {
            "seq": 1,
            "section": {
                "id": "sec_live",
                "kind": "chart",
                "spec": {"source_meta": {"artifact_id": "art_x", "label": "q"}},
            },
        },
    )
    await project_live_event_payload_for_viewer(request, "turn_1", *warm, event_ordinal=1, cache=cache)
    row = await project_live_event_payload_for_viewer(
        request, "turn_1", *_cite_event("sec_live", "chart_section", "在途图"), event_ordinal=2, cache=cache
    )
    assert row["access_state"] == "linked_labeled"
    assert row["safe_anchor_id"] == "sec_live"


@pytest.mark.anyio
async def test_v2_turn_owned_event_artifact_keeps_byte_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn-owned event artifacts retain byte access without a canvas or source reference."""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], arts={"art_sbx": ("turn_1", None)})
    projected = await project_event_payloads_for_viewer(request, "turn_1", [_cite_event("art_sbx", "tool_output", "沙箱结论")])
    row = projected[0]
    assert row["access_state"] == "linked_accessible"
    assert row["label"] == "沙箱结论"


@pytest.mark.anyio
async def test_v2_live_fast_path_current_turn_artifact_stays_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """review F1：快路的缓存 rendered 冻在 warm 时刻——本 turn 新产物必须补一次
    与慢路同判据的归属查证，否则 live=labeled / replay=accessible（live≠replay）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    arts = {"art_now": ("turn_1", None)}
    warm = (
        "canvas.section_added",
        {
            "seq": 1,
            "section": {
                "id": "sec_warm",
                "kind": "chart",
                "spec": {"source_meta": {"artifact_id": "art_other", "label": "q"}},
            },
        },
    )
    cite = _cite_event("art_now", "tool_output", "本轮沙箱结论")

    slow_request = _v2_request([], arts)
    slow = await project_live_event_payload_for_viewer(slow_request, "turn_1", *cite, event_ordinal=3)

    fast_request = _v2_request([], arts)
    cache = LiveEventProjectionCache()
    await project_live_event_payload_for_viewer(fast_request, "turn_1", *warm, event_ordinal=2, cache=cache)
    assert cache.projector is not None
    fast = await project_live_event_payload_for_viewer(fast_request, "turn_1", *cite, event_ordinal=3, cache=cache)

    assert slow["access_state"] == "linked_accessible"
    assert fast == slow


@pytest.mark.anyio
async def test_v2_replay_batch_warms_inflight_for_live_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """review F6：重连 replay 批次里的在途区块 id 必须进 turn-scoped 索引——
    否则重连客户端上后续 live citation 对未落库区块判 missing（三态打架）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "1")
    request = _v2_request([], section_canvas={})
    cache = LiveEventProjectionCache()
    replay_events = [
        (
            "canvas.section_added",
            {
                "seq": 1,
                "section": {
                    "id": "sec_replay",
                    "kind": "chart",
                    "spec": {"source_meta": {"artifact_id": "art_x", "label": "q"}},
                },
            },
        ),
    ]
    await project_event_payloads_for_viewer(request, "turn_1", replay_events, _live_cache=cache)
    assert "sec_replay" in cache.in_flight_section_ids
    row = await project_live_event_payload_for_viewer(
        request, "turn_1", *_cite_event("sec_replay", "chart_section", "在途图"), event_ordinal=5, cache=cache
    )
    assert row["access_state"] == "linked_labeled"
    assert row["safe_anchor_id"] == "sec_replay"
