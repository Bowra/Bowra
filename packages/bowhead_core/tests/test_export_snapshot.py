"""ExportSnapshot v1 persistence and transaction contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.export_snapshot import (
    ExportSnapshotRequest,
    ExportSnapshotStore,
    SnapshotError,
)

if TYPE_CHECKING:
    from pathlib import Path


def _request() -> ExportSnapshotRequest:
    return ExportSnapshotRequest(
        requested_by="u_owner",
        team_id="team_1",
        fmt="pptx",
        profile="full_report",
        parity_contract="full_content_v1",
        locale="zh-CN",
        timezone="Asia/Singapore",
        client_surface_receipt_sha256="sha256:client-receipt",
        theme={"kind": "default"},
        options={"include_appendix": True},
    )


@pytest.fixture
async def stores(tmp_path: Path) -> tuple[CanvasStore, ExportSnapshotStore]:
    path = tmp_path / "snapshot.db"
    canvas_store = CanvasStore(path)
    snapshot_store = ExportSnapshotStore(path)
    await canvas_store.initialize()
    await snapshot_store.initialize()
    return canvas_store, snapshot_store


async def test_snapshot_is_canonical_sanitized_and_insert_only(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_snapshot",
        workspace_id="ws_datarc",
        user_id="u_owner",
        title="Frozen report",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={
            "text": "Revenue",
            "_local_path": "/tmp/attacker-controlled",
            "not_in_schema": "must not cross the boundary",
        },
        by_turn_id="t_1",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="data_table",
        payload={
            "caption": "By region",
            "columns": [{"key": "amount", "label": "Amount"}],
            "rows": [
                {
                    "amount": 42,
                    "_total": 42,
                    "_local_path": "/tmp/nested-transport-field",
                }
            ],
            "source_refs": ["tl_source"],
        },
        by_turn_id="t_1",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={
            "markdown": "营收增长 [1]",
            "unverified_numbers": ["毛估 X"],
            # ★ Trust metric's **denominator**. Omitting it from the allowlist makes exported percentages fall back
            #   to truncated list length, silently inflating perceived trustworthiness (allowlists silently drop fields).
            "unverified_total": 30,
            "not_in_schema": "must not cross the boundary",
        },
        by_turn_id="t_1",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="sources",
        payload={
            "label": "Sources",
            "entries": [
                {
                    "artifact_id": "art_private_source",
                    "label": "Board revenue.xlsx",
                    "query": "select confidential_margin",
                    "preview_rows": [{"margin": "secret"}],
                },
            ],
        },
        by_turn_id="t_1",
    )

    candidate = await snapshot_store.build_candidate(
        canvas_id=canvas.id,
        request=_request(),
        snapshot_id="exs_deterministic",
        created_at="2026-07-16T10:00:00+00:00",
    )
    repeated = await snapshot_store.build_candidate(
        canvas_id=canvas.id,
        request=_request(),
        snapshot_id="exs_deterministic",
        created_at="2026-07-16T10:00:00+00:00",
    )

    assert repeated.payload_json == candidate.payload_json
    assert repeated.payload_sha256 == candidate.payload_sha256
    assert candidate.payload_sha256 == f"sha256:{hashlib.sha256(candidate.payload_json.encode()).hexdigest()}"
    assert candidate.size_bytes == len(candidate.payload_json.encode("utf-8"))
    assert "_local_path" not in candidate.payload_json
    assert "not_in_schema" not in candidate.payload_json
    payload = json.loads(candidate.payload_json)
    assert payload["canvas"] == {
        "id": canvas.id,
        "team_id": "team_1",
        "owner_user_id": "u_owner",
        "session_id": "s_snapshot",
        "title": "Frozen report",
        "workspace_id": "ws_datarc",
    }
    table = next(section for section in payload["sections"] if section["kind"] == "data_table")
    assert table["payload"]["rows"] == [{"_total": 42, "amount": 42}]
    assert table["source_refs"] == ["source-ordinal:2"]
    assert "tl_source" not in candidate.payload_json
    narrative = next(section for section in payload["sections"] if section["kind"] == "narrative")
    assert narrative["payload"]["unverified_total"] == 30, "可信度分母必须穿过白名单"
    assert narrative["payload"]["unverified_numbers"] == ["毛估 X"]
    assert "not_in_schema" not in narrative["payload"], "白名单仍然要挡住无关键"
    sources = next(section for section in payload["sections"] if section["kind"] == "sources")
    assert sources["payload"]["entries"] == [
        {
            "source_ref": "source-ordinal:1",
            "generic_type": "generated_artifact",
            "ordinal": 1,
        },
    ]
    for secret in (
        "art_private_source",
        "Board revenue.xlsx",
        "confidential_margin",
        "secret",
    ):
        assert secret not in candidate.payload_json

    saved = await snapshot_store.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    loaded = await snapshot_store.get_snapshot(saved.id)
    assert loaded is not None
    assert loaded.payload == payload
    assert loaded.payload_sha256 == candidate.payload_sha256
    assert loaded.section_count == 4  # h1 + data_table + narrative + sources

    with pytest.raises(SnapshotError) as collision:
        await snapshot_store.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert collision.value.code == "snapshot_id_conflict"
    assert (await snapshot_store.get_snapshot(saved.id)) == loaded

    async with open_shared_ctx(str(snapshot_store.db_path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="export snapshot is immutable"):
            await db.execute(
                "UPDATE canvas_export_snapshots SET title='mutated' WHERE id=?",
                (saved.id,),
            )
        await db.rollback()


async def test_snapshot_commit_rejects_content_drift_without_partial_write(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_drift",
        workspace_id="ws_1",
        user_id="u_owner",
        title="Before",
    )
    section = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Before"},
    )
    candidate = await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    await canvas_store.patch_section(
        section_id=section.id,
        patch={"markdown": "After"},
        expected_rev=section.rev,
    )

    with pytest.raises(SnapshotError) as conflict:
        await snapshot_store.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert conflict.value.code == "projection_conflict"
    assert await snapshot_store.get_snapshot(candidate.id) is None


async def test_snapshot_commit_rejects_title_drift_without_partial_write(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_title_drift",
        workspace_id="ws_1",
        user_id="u_owner",
        title="Before",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "Stable section"},
    )
    candidate = await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    await canvas_store.set_title(canvas.id, "After")

    with pytest.raises(SnapshotError) as conflict:
        await snapshot_store.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert conflict.value.code == "projection_conflict"
    assert await snapshot_store.get_snapshot(candidate.id) is None


async def test_snapshot_commit_checks_both_expected_tokens(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_expected",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "Expected"},
    )
    candidate = await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())

    with pytest.raises(SnapshotError) as content_conflict:
        await snapshot_store.commit_candidate(
            candidate,
            expected_projection_token="sha256:stale-content",
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert content_conflict.value.code == "projection_conflict"

    with pytest.raises(SnapshotError) as surface_conflict:
        await snapshot_store.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token="sha256:stale-surface",
        )
    assert surface_conflict.value.code == "surface_projection_conflict"
    assert await snapshot_store.get_snapshot(candidate.id) is None


async def test_snapshot_records_lifecycle_exclusions_but_freezes_only_surface_entries(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_lifecycle",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    current = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Current"},
        by_turn_id="t_current",
    )
    history = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "History"},
        by_turn_id="t_history",
    )
    hidden = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="code_block",
        payload={"call_id": "tl_hidden", "lang": "python", "code": "print(1)"},
        by_turn_id="t_hidden",
    )
    await canvas_store.mark_sections_superseded(
        section_ids=[history.id],
        superseded_by_turn="t_current",
    )
    await canvas_store.mark_turn_hidden_for_chat_mode("t_hidden", "chat-only")

    candidate = await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    payload = json.loads(candidate.payload_json)

    assert [section["id"] for section in payload["sections"]] == [current.id]
    assert payload["projection"]["excluded"] == [
        {"reason": "superseded_history", "section_id": history.id},
        {"reason": "hidden_for_chat_mode", "section_id": hidden.id},
    ]
    assert payload["surface_projection"]["exclusions"] == payload["projection"]["excluded"]


@pytest.mark.parametrize(
    ("kind", "payload", "expected_code"),
    [
        (None, None, "snapshot_empty"),
        ("future_kind", {"value": 1}, "snapshot_unsupported_section_kind"),
    ],
)
async def test_snapshot_rejects_empty_and_unknown_kind(
    stores: tuple[CanvasStore, ExportSnapshotStore],
    kind: str | None,
    payload: dict[str, object] | None,
    expected_code: str,
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id=f"s_{expected_code}",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    if kind is not None and payload is not None:
        await canvas_store.append_section(canvas_id=canvas.id, kind=kind, payload=payload)

    with pytest.raises(SnapshotError) as error:
        await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    assert error.value.code == expected_code


async def test_snapshot_rejects_current_quarantined_section(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_quarantined",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    section = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "Unsafe"},
    )
    await canvas_store.set_quarantine(
        section_id=section.id,
        quarantined=True,
        reason="legacy numeric mismatch",
    )

    with pytest.raises(SnapshotError) as error:
        await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    assert error.value.code == "snapshot_quarantined"


async def test_snapshot_counts_nested_sections_for_hard_limit(
    stores: tuple[CanvasStore, ExportSnapshotStore],
) -> None:
    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_nested_limit",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    nested = [{"id": f"nested_{index}", "kind": "eyebrow", "text": str(index)} for index in range(500)]
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="section",
        payload={"label": "Nested", "h2": "Limit", "body": nested},
    )

    with pytest.raises(SnapshotError) as error:
        await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    assert error.value.code == "snapshot_section_limit"


async def test_snapshot_enforces_canonical_payload_byte_limit(
    stores: tuple[CanvasStore, ExportSnapshotStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core import export_snapshot

    canvas_store, snapshot_store = stores
    canvas = await canvas_store.ensure_canvas(
        session_id="s_byte_limit",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "A" * 256},
    )
    monkeypatch.setattr(export_snapshot, "MAX_SNAPSHOT_PAYLOAD_BYTES", 128)

    with pytest.raises(SnapshotError) as error:
        await snapshot_store.build_candidate(canvas_id=canvas.id, request=_request())
    assert error.value.code == "snapshot_too_large"
