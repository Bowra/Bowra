"""Plural Canvas export V2 HTTP contract and error-envelope tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chart_style import PRESET_RELEASES, PRODUCT_DEFAULT_PRESET_ID, chart_style_policy_etag
from bowhead_core.chart_style_export import build_chart_style_export_theme_v2
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    actual_surface_projection,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_v2 import (
    built_in_style_projection,
    chart_style_policy_etag_v2,
)
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    TRUST_COPY_CONTRACT,
    output_producer_for,
)
from bowhead_core.export_snapshot import ExportSnapshotStore
from bowhead_core.export_store import ExportStore
from bowhead_core.export_visual_identity import (
    compute_screen_visual_input_sha256_v2,
    compute_visual_input_sha256,
)
from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner
from bowhead_core.print_theme import PRINT_PAPER, build_print_theme_snapshot, current_print_theme_snapshot
from bowhead_core.session_store import SessionStore
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_compiler import compile_theme_scheme
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import canvas_export as legacy_export

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_core.canvas_store import Canvas, Section
    from fastapi import FastAPI


@dataclass(frozen=True)
class V2RouteEnvironment:
    app: FastAPI
    db_path: Path
    canvases: CanvasStore
    canvas: Canvas
    section: Section


@dataclass
class FakeExportRunner:
    enqueued: list[str]
    canceled: list[str]

    def is_available_for(self, fmt: str, profile: str) -> bool:
        return (fmt, profile) in {
            ("pdf", "full_report"),
            ("pptx", "full_report"),
            ("pptx", "executive_summary"),
            ("xlsx", "data_workbook"),
        }

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)

    async def cancel(self, job_id: str) -> None:
        self.canceled.append(job_id)


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("canvas-export-v2-template") / "bowhead.db"
    upgrade(f"sqlite:///{path}", target="core")
    return path


@pytest.fixture
async def env(tmp_path: Path, migrated_template: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[V2RouteEnvironment]:
    db_path = tmp_path / "canvas_export_v2.db"
    with sqlite3.connect(migrated_template) as source, sqlite3.connect(db_path) as destination:
        source.backup(destination)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    identity = await initialize_installation(str(db_path), "community", admitted_fresh=True)
    owner = await setup_community_owner(
        str(db_path),
        setup_token=community_setup_token(identity),
        team_name="Export Contract Tests",
        email="export-owner@example.test",
        password="SyntheticExportOwner7!",
    )
    monkeypatch.setenv("BOWHEAD_DEV_DEFAULT_USER", owner.id)
    users = UserStore(str(db_path))
    workspace_id = await users.create_local_workspace(team_id=owner.team_id, name="Export Contract Space", creator_user_id=owner.id)
    sessions = SessionStore(db_path)
    canvases = CanvasStore(db_path)
    snapshots = ExportSnapshotStore(db_path)
    team_settings = TeamSettingsStore(db_path)
    user_settings = UserSettingsStore(db_path)
    artifacts = ArtifactStore(
        db_path=str(db_path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await sessions.initialize()
    await canvases.initialize()
    await artifacts.initialize()
    await snapshots.initialize()
    await team_settings.init()
    await user_settings.init()
    old_db_path = legacy_export._db_path
    await legacy_export.init_export_jobs(str(db_path))
    user_id = Settings().dev_default_user
    await sessions.ensure_session(
        session_id="s_export_v2",
        workspace_id=workspace_id,
        user_id=user_id,
    )
    canvas = await canvases.ensure_canvas(
        session_id="s_export_v2",
        workspace_id=workspace_id,
        user_id=user_id,
        title="V2 export",
    )
    section = await canvases.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "Durable export"},
    )
    app = create_app()
    if app.state.edition == "business":
        await initialize_installation(str(db_path), "business")
    app.state.user_store = users
    app.state.settings = Settings()
    app.state.canvas_store = canvases
    app.state.artifact_store = artifacts
    app.state.export_snapshot_store = snapshots
    export_store = ExportStore(db_path, snapshots)
    await export_store.initialize()
    app.state.export_store = export_store
    app.state.export_runner = FakeExportRunner(enqueued=[], canceled=[])
    app.state.team_settings_store = team_settings
    app.state.user_settings_store = user_settings
    try:
        yield V2RouteEnvironment(
            app=app,
            db_path=db_path,
            canvases=canvases,
            canvas=canvas,
            section=section,
        )
    finally:
        legacy_export._jobs.clear()
        legacy_export._db_path = old_db_path


async def _prepared_body(client: httpx2.AsyncClient, canvas_id: str) -> dict[str, object]:
    response = await client.get(f"/api/v1/canvas/{canvas_id}")
    assert response.status_code == 200
    detail = response.json()
    theme = _chart_style_theme()
    section_by_id = {section["id"]: section for section in detail["sections"]}
    return {
        "format": "pdf",
        "profile": "full_report",
        "locale": "zh-CN",
        "timezone": "Asia/Singapore",
        "theme": theme,
        "consistency": "require_projection",
        "expected_projection_token": detail["projectionToken"],
        "expected_surface_token": detail["surfaceToken"],
        "client_surface_receipt_sha256": f"sha256:{'a' * 64}",
        "client_visual_receipts": [
            {
                "section_id": entry["sectionId"],
                "section_rev": entry["sectionRev"],
                "visual_input_sha256": compute_visual_input_sha256(
                    section_kind=entry["effectiveKind"],
                    payload={key: value for key, value in section_by_id[entry["sectionId"]].items() if key not in {"id", "kind", "rev"}},
                    locale="zh-CN",
                    theme=theme,
                ),
                "render_disposition": "rendered",
            }
            for entry in detail["surfaceProjection"]["entries"]
            if entry["parityRequirement"] == "exact_visual"
        ],
        "options": {"include_appendix": True},
    }


def _chart_style_theme() -> dict[str, object]:
    repo_root = Path(__file__).parents[3]
    preset = json.loads(
        (repo_root / f"config/chart_style/presets/{PRODUCT_DEFAULT_PRESET_ID}.json").read_text(
            encoding="utf-8",
        )
    )
    return {
        "kind": "chart-style",
        "schemaVersion": 1,
        "mode": "light",
        "preset": preset,
        "presetChecksum": PRESET_RELEASES[PRODUCT_DEFAULT_PRESET_ID][1],
        "selectionSource": "product",
        "accessibility": {"contrast": "standard", "colorVision": "standard"},
        "teamPolicyEtag": chart_style_policy_etag(None),
        "resolverPolicyVersion": "1",
        "contextRecipeVersion": "1",
    }


def _effective_chart_style_v2(
    *,
    light_surface: str = "#FFFFFF",
    dark_surface: str = DARK_STANDARD_SURFACE,
    color_scheme_checksum: str = "structural-standard-surfaces",
) -> dict[str, object]:
    style = built_in_style_projection(PRODUCT_DEFAULT_PRESET_ID)
    snapshot = style["resolvedPresetSnapshot"]
    return {
        "schemaVersion": 2,
        "registryVersion": 0,
        "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        "style": style,
        "selection": {
            "source": "product",
            "savedStyleId": None,
            "unavailableReason": None,
        },
        "accessibility": {
            "contrast": "standard",
            "colorVision": "standard",
            "source": {
                "contrast": "product",
                "colorVision": "product",
            },
        },
        "modeColors": {
            "light": actual_surface_projection(
                snapshot,
                mode="light",
                surface=light_surface,
                color_scheme_checksum=color_scheme_checksum,
            ),
            "dark": actual_surface_projection(
                snapshot,
                mode="dark",
                surface=dark_surface,
                color_scheme_checksum=color_scheme_checksum,
            ),
        },
        "teamPolicy": {"etag": chart_style_policy_etag_v2(None)},
        "resolutionAdjustments": [],
    }


async def _prepared_body_v2(
    client: httpx2.AsyncClient,
    canvas_id: str,
    effective: dict[str, object],
) -> dict[str, object]:
    response = await client.get(f"/api/v1/canvas/{canvas_id}")
    assert response.status_code == 200
    detail = response.json()
    style = effective["style"]
    policy = effective["teamPolicy"]
    assert isinstance(style, dict) and isinstance(policy, dict)
    section_by_id = {section["id"]: section for section in detail["sections"]}
    return {
        "format": "pdf",
        "profile": "full_report",
        "locale": "zh-CN",
        "timezone": "Asia/Singapore",
        "theme": {
            "kind": "chart-style-freshness",
            "schemaVersion": 2,
            "styleId": style["id"],
            "revision": style["revision"],
            "snapshotChecksum": style["snapshotChecksum"],
            "teamPolicyEtag": policy["etag"],
            "registryVersion": effective["registryVersion"],
            "builtInRegistryChecksum": effective["builtInRegistryChecksum"],
        },
        "consistency": "require_projection",
        "expected_projection_token": detail["projectionToken"],
        "expected_surface_token": detail["surfaceToken"],
        "client_surface_receipt_sha256": f"sha256:{'a' * 64}",
        "client_visual_receipts": [
            {
                "section_id": entry["sectionId"],
                "section_rev": entry["sectionRev"],
                "surface_token": detail["surfaceToken"],
                "screen_visual_input_sha256": (
                    compute_screen_visual_input_sha256_v2(
                        section_kind=entry["effectiveKind"],
                        payload={key: value for key, value in section_by_id[entry["sectionId"]].items() if key not in {"id", "kind", "rev"}},
                        locale="zh-CN",
                        surface_token=detail["surfaceToken"],
                        effective_chart_style=effective,
                    )
                ),
                "render_disposition": "rendered",
            }
            for entry in detail["surfaceProjection"]["entries"]
            if entry["parityRequirement"] == "exact_visual"
        ],
        "options": {"include_appendix": True},
    }


@pytest.mark.integration
async def test_canvas_detail_exposes_only_runtime_ready_v2_profiles(
    env: V2RouteEnvironment,
) -> None:
    """能力清单只由 runner 的实际可用性决定：没装配出 runner 的部署一条都不报。"""
    transport = httpx2.ASGITransport(app=env.app)
    runner = env.app.state.export_runner
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        with_runner = await client.get(f"/api/v1/canvas/{env.canvas.id}")
        env.app.state.export_runner = None
        without_runner = await client.get(f"/api/v1/canvas/{env.canvas.id}")
    env.app.state.export_runner = runner

    assert with_runner.status_code == 200
    assert with_runner.json()["exportV2Capabilities"] == [
        {"format": "pdf", "profile": "full_report"},
        {"format": "pptx", "profile": "full_report"},
        {"format": "pptx", "profile": "executive_summary"},
        {"format": "xlsx", "profile": "data_workbook"},
    ]
    assert without_runner.status_code == 200
    assert without_runner.json()["exportV2Capabilities"] == []


@pytest.mark.integration
async def test_create_get_list_cancel_and_idempotency(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        created = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "route-create"},
            json=body,
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["contract_version"] == 2
        assert job["format"] == "pdf"
        assert job["profile"] == "full_report"
        assert job["parity_contract"] == "full_content_v1"
        assert job["status"] == "queued"
        assert job["snapshot_id"].startswith("exs_")
        assert job["download_url"] is None
        assert env.app.state.export_runner.enqueued == [job["job_id"]]

        duplicate = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "route-create"},
            json=body,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job["job_id"]
        assert env.app.state.export_runner.enqueued == [job["job_id"]]

        different_body = dict(body)
        different_body["options"] = {"include_appendix": False}
        idempotency_conflict = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "route-create"},
            json=different_body,
        )
        assert idempotency_conflict.status_code == 409
        assert idempotency_conflict.json()["detail"]["code"] == "idempotency_conflict"

        history = await client.get(f"/api/v1/canvas/{env.canvas.id}/exports")
        assert history.status_code == 200
        assert [item["job_id"] for item in history.json()["items"]] == [job["job_id"]]

        fetched = await client.get(f"/api/v1/canvas/{env.canvas.id}/exports/{job['job_id']}")
        assert fetched.status_code == 200
        assert fetched.json() == job

        canceled = await client.post(f"/api/v1/canvas/{env.canvas.id}/exports/{job['job_id']}/cancel")
        assert canceled.status_code == 202
        assert canceled.json()["status"] == "canceled"
        assert canceled.json()["phase"] == "terminal"
        assert env.app.state.export_runner.canceled == [job["job_id"]]

        conflict = await client.post(f"/api/v1/canvas/{env.canvas.id}/exports/{job['job_id']}/cancel")
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "job_not_cancelable"


@pytest.mark.integration
async def test_v2_export_locale_skips_missing_or_invalid_body_candidate(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class _UserStore:
        db_path = str(env.db_path)

        async def get_user(self, _user_id: str) -> SimpleNamespace:
            return SimpleNamespace(team_id="team_export", locale="en_GB")

    env.app.state.user_store = _UserStore()
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        missing_body = await _prepared_body(client, env.canvas.id)
        missing_body.pop("locale")
        missing = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "locale-missing"},
            json=missing_body,
        )
        invalid_body = dict(missing_body)
        invalid_body["locale"] = "ja-JP"
        invalid = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "locale-invalid"},
            json=invalid_body,
        )
        explicit_body = dict(missing_body)
        explicit_body["locale"] = "zh-Hant-TW"
        explicit = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "locale-explicit"},
            json=explicit_body,
        )

    assert (missing.status_code, invalid.status_code, explicit.status_code) == (202, 202, 202)
    snapshots = env.app.state.export_snapshot_store
    missing_snapshot = await snapshots.get_snapshot(missing.json()["snapshot_id"])
    invalid_snapshot = await snapshots.get_snapshot(invalid.json()["snapshot_id"])
    explicit_snapshot = await snapshots.get_snapshot(explicit.json()["snapshot_id"])
    assert missing_snapshot is not None and missing_snapshot.locale == "en-US"
    assert invalid_snapshot is not None and invalid_snapshot.locale == "en-US"
    assert explicit_snapshot is not None and explicit_snapshot.locale == "zh-CN"


@pytest.mark.parametrize(
    ("explicit", "user_locale", "expected"),
    [
        ("fr-FR", None, "fr-FR"),
        (None, "fr-FR", "fr-FR"),
        ("de-DE", None, "de-DE"),
        ("zh-CN", "fr-FR", "zh-CN"),
    ],
)
async def test_export_requester_locale_keeps_german_and_french(
    explicit: str | None,
    user_locale: str | None,
    expected: str,
) -> None:
    """导出视觉渲染支持四种界面语言：德语、法语（显式或账号语言）按原语言导出，不落中文。"""
    from bowhead_gateway.routes.canvas import _resolve_export_requester_locale

    class _UserStore:
        async def get_user(self, _user_id: str) -> SimpleNamespace:
            return SimpleNamespace(locale=user_locale)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=_UserStore())))
    assert await _resolve_export_requester_locale(request, "u_1", explicit) == expected


@pytest.mark.integration
async def test_done_export_projects_contract_three_only_from_complete_evidence(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        created = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "current-copy"},
            json=body,
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        artifacts = env.app.state.artifact_store
        manifest = await artifacts.put_bytes(
            content=json.dumps(
                {
                    "manifest_version": 2,
                    "trust_copy_contract": TRUST_COPY_CONTRACT,
                    "job_id": job_id,
                    "format": "pdf",
                }
            ).encode(),
            filename="report.manifest.json",
            mime="application/json",
            source="canvas_export",
            canvas_id=env.canvas.id,
            created_by=Settings().dev_default_user,
            producer_kind=CURRENT_MANIFEST_PRODUCER,
        )
        output = await artifacts.put_bytes(
            content=b"%PDF-1.7\n",
            filename="report.pdf",
            mime="application/pdf",
            source="canvas_export",
            canvas_id=env.canvas.id,
            created_by=Settings().dev_default_user,
            producer_kind=output_producer_for(engine="durable", fmt="pdf"),
            manifest_artifact_id=manifest.id,
        )
        async with open_shared_ctx(str(env.db_path)) as db:
            await db.execute(
                "UPDATE canvas_export_jobs SET status='done', phase='terminal', "
                "quality_status='clean', artifact_id=?, manifest_artifact_id=? WHERE id=?",
                (output.id, manifest.id, job_id),
            )
            await db.commit()

        fetched = await client.get(f"/api/v1/canvas/{env.canvas.id}/exports/{job_id}")
        history = await client.get(f"/api/v1/canvas/{env.canvas.id}/exports")
        replayed = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "current-copy"},
            json=body,
        )

    assert fetched.status_code == 200 and fetched.json()["contract_version"] == 3
    matching = [item for item in history.json()["items"] if item["job_id"] == job_id]
    assert matching[0]["contract_version"] == 3
    assert replayed.status_code == 202 and replayed.json()["contract_version"] == 3


@pytest.mark.integration
async def test_idempotent_replay_precedes_runner_and_projection_preflight(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        body = await _prepared_body(client, env.canvas.id)
        created = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "lost-response"},
            json=body,
        )
        assert created.status_code == 202
        await env.canvases.append_section(
            canvas_id=env.canvas.id,
            kind="paragraph",
            payload={"text": "Changed after the accepted request"},
        )
        env.app.state.export_runner = None

        replayed = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "lost-response"},
            json=body,
        )

    assert replayed.status_code == 202
    assert replayed.json()["job_id"] == created.json()["job_id"]


@pytest.mark.integration
async def test_create_rejects_visual_receipt_that_does_not_match_snapshot(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "bar",
                "title": "Revenue",
                "columns": [
                    {
                        "key": "revenue",
                        "header": "Revenue",
                        "type": "numeric",
                    }
                ],
                "rows": [{"revenue": 42}],
            }
        },
    )
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        body = await _prepared_body(client, env.canvas.id)
        assert len(body["client_visual_receipts"]) == 1
        forged = dict(body)
        forged["client_visual_receipts"] = [
            {
                **body["client_visual_receipts"][0],
                "visual_input_sha256": f"sha256:{'f' * 64}",
            }
        ]
        rejected = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "visual-forged"},
            json=forged,
        )
        accepted = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "visual-matched"},
            json=body,
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "surface_projection_conflict"
    assert accepted.status_code == 202
    assert await _job_count(env.db_path) == 1
    assert await _snapshot_count(env.db_path) == 1


@pytest.mark.integration
async def test_chart_style_v2_freezes_server_theme_and_distinct_screen_receipt(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await env.canvases.append_section(
        canvas_id=env.canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "line",
                "title": "Revenue",
                "row_count": 2,
                "columns": [
                    {"key": "month", "header": "Month", "type": "date"},
                    {
                        "key": "revenue",
                        "header": "Revenue",
                        "type": "numeric",
                    },
                ],
                "rows": [
                    {"month": "Jan", "revenue": 40},
                    {"month": "Feb", "revenue": 42},
                ],
            }
        },
    )
    effective = _effective_chart_style_v2(
        light_surface="#F5E8DC",
        dark_surface="#242A2E",
        color_scheme_checksum="actual-team-scheme",
    )

    class _ChartStyleService:
        async def effective(self, **kwargs: object) -> tuple[dict, str]:
            assert kwargs["surfaces"] == {
                "light": "#F5E8DC",
                "dark": "#242A2E",
            }
            assert kwargs["color_scheme_checksum"] == "actual-team-scheme"
            return effective, "cache-identity"

    resolved_user_configs: list[object] = []
    team_scheme = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#0F766E",
            "neutralSeed": "#64748B",
            "backgroundStyle": "custom",
            "overrides": {"light": {"surface": "#F5E8DC"}, "dark": {"surface": "#242A2E"}},
        }
    )

    class _ActualSurfaceResolver:
        # Stands in for whichever resolver the fixed edition composition installed; its base
        # theme (empty user configuration) is what the export freezes as its print theme.
        async def resolve(self, **kwargs: object) -> SimpleNamespace:
            resolved_user_configs.append(kwargs["user_config"])
            return SimpleNamespace(
                payload={
                    "effective": {
                        "colorScheme": {
                            "light": team_scheme["light"],
                            "dark": team_scheme["dark"],
                            "checksum": "actual-team-scheme",
                        }
                    }
                },
                revision_id="theme-rev-1",
                revision_checksum="theme-checksum-1",
            )

    class _UserStore:
        db_path = str(env.db_path)

        async def get_user(self, _user_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                id=_user_id,
                team_id="team_export",
                locale="zh-CN",
            )

    env.app.state.chart_style_read_service = _ChartStyleService()
    env.app.state.effective_theme_resolver = _ActualSurfaceResolver()
    env.app.state.user_store = _UserStore()
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        body = await _prepared_body_v2(client, env.canvas.id, effective)
        screen_hash = body["client_visual_receipts"][0]["screen_visual_input_sha256"]
        accepted = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "chart-style-v2"},
            json=body,
        )

        resolved_body = json.loads(json.dumps(body))
        resolved_body["theme"] = build_chart_style_export_theme_v2(
            effective,
            print_theme=current_print_theme_snapshot(),
        )
        rejected_resolved = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "client-resolved-v2"},
            json=resolved_body,
        )

        stale_body = json.loads(json.dumps(body))
        stale_body["theme"]["registryVersion"] = 1
        rejected_stale = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "stale-v2"},
            json=stale_body,
        )

    assert accepted.status_code == 202, accepted.text
    assert rejected_resolved.status_code == 422
    assert rejected_resolved.json()["detail"]["code"] == ("template_profile_not_supported")
    assert rejected_stale.status_code == 409
    assert rejected_stale.json()["detail"]["code"] == ("chart_style_projection_conflict")

    job = await env.app.state.export_store.get_job(accepted.json()["job_id"])
    snapshot = await env.app.state.export_snapshot_store.get_snapshot(accepted.json()["snapshot_id"])
    assert job is not None and job.renderer_contract_version == 2
    assert snapshot is not None
    frozen_theme = snapshot.payload["request"]["theme"]
    assert frozen_theme["schemaVersion"] == 2
    assert frozen_theme["kind"] == "chart-style"
    assert "resolvedPresetSnapshot" in frozen_theme["style"]
    # The export projection is computed on the white paper, never on the team's tinted screen
    # surface (#F5E8DC), so the PNG background always matches the PDF/PPTX paper.
    assert snapshot.payload["request"]["print_theme"]["tokens"]["surface"] == "#F5E8DC"
    assert (
        frozen_theme["modeColors"]["light"]["finalPaletteQA"]["surfaceChecksum"]
        != effective["modeColors"]["light"]["finalPaletteQA"]["surfaceChecksum"]
    )
    assert (
        frozen_theme["modeColors"]["light"]["finalPaletteQA"]["surfaceChecksum"]
        == actual_surface_projection(
            effective["style"]["resolvedPresetSnapshot"],
            mode="light",
            surface=PRINT_PAPER,
            color_scheme_checksum="unused-for-surface-checksum",
        )["finalPaletteQA"]["surfaceChecksum"]
    )
    assert snapshot.payload["request"]["client_visual_receipts"][0]["screen_visual_input_sha256"] == screen_hash
    # The team's base theme (never personal appearance) is frozen as the print theme and bound
    # to the chart-style theme.
    team_print_theme = build_print_theme_snapshot(team_scheme["light"])
    assert {"version": 1} in resolved_user_configs
    assert snapshot.payload["request"]["print_theme"] == team_print_theme
    assert team_print_theme["checksum"] != current_print_theme_snapshot()["checksum"]
    assert frozen_theme["structuralThemeChecksum"] == team_print_theme["checksum"]
    chart = next(section for section in snapshot.payload["sections"] if section["kind"] == "chart")
    assert chart["frozen_chart_presentation"]["renderContext"]["bounds"] == {
        "widthPx": 1456,
        "heightPx": 772,
        "devicePixelRatio": 1,
    }


@pytest.mark.integration
async def test_create_requires_live_format_processor(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.app.state.export_runner = None
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        response = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "runner-missing"},
            json=body,
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "export_runner_unavailable"
    assert await _job_count(env.db_path) == 0


@pytest.mark.integration
async def test_create_capacity_returns_retry_after_without_partial_snapshot(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        for index in range(3):
            response = await client.post(
                f"/api/v1/canvas/{env.canvas.id}/exports",
                headers={"Idempotency-Key": f"capacity-{index}"},
                json=body,
            )
            assert response.status_code == 202
        rejected = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "capacity-rejected"},
            json=body,
        )

    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "5"
    assert rejected.json()["detail"] == {
        "code": "export_capacity_exceeded",
        "message": "当前导出队列已满，请稍后重试",
        "retryable": True,
        "context": {"scope": "canvas", "limit": 3},
    }
    assert await _job_count(env.db_path) == 3
    assert await _snapshot_count(env.db_path) == 3


@pytest.mark.integration
async def test_projection_conflict_and_request_errors_are_coded(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        patched = await env.canvases.patch_section(
            section_id=env.section.id,
            patch={"text": "Changed after prepare"},
            expected_rev=env.section.rev,
        )
        assert patched is not None
        stale = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "stale"},
            json=body,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "projection_conflict"

        body = await _prepared_body(client, env.canvas.id)
        body["options"] = {"include_history": True}
        invalid = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "invalid-option"},
            json=body,
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_options"

        body = await _prepared_body(client, env.canvas.id)
        body["theme"] = {"kind": "uploaded", "artifact_id": "art_secret"}
        unsupported = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "uploaded-theme"},
            json=body,
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["detail"]["code"] == "template_profile_not_supported"

        body = await _prepared_body(client, env.canvas.id)
        modified_package = json.loads(json.dumps(body["theme"]))
        modified_package["preset"]["tokens"]["light"]["palette"][0] = "#000000"
        body["theme"] = modified_package
        forged_package = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "forged-chart-style-package"},
            json=body,
        )
        assert forged_package.status_code == 422
        assert forged_package.json()["detail"]["code"] == "template_profile_not_supported"

        body = await _prepared_body(client, env.canvas.id)
        body["theme"]["teamPolicyEtag"] = f'W/"chart-style-policy:{"f" * 64}"'
        stale_style = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={"Idempotency-Key": "stale-chart-style"},
            json=body,
        )
        assert stale_style.status_code == 409
        assert stale_style.json()["detail"]["code"] == "surface_projection_conflict"

        # Directory names and non-zone files in the tz database are a coded 400, never a 500.
        for zone in ("America", "Etc", "posixrules", "Not/AZone"):
            body = await _prepared_body(client, env.canvas.id)
            body["timezone"] = zone
            bad_zone = await client.post(
                f"/api/v1/canvas/{env.canvas.id}/exports",
                headers={"Idempotency-Key": f"bad-zone-{zone.replace('/', '-')}"},
                json=body,
            )
            assert bad_zone.status_code == 400, (zone, bad_zone.text)
            assert bad_zone.json()["detail"]["code"] == "invalid_locale"

    assert await _job_count(env.db_path) == 0


@pytest.mark.integration
async def test_owner_only_does_not_create_snapshot(
    env: V2RouteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        response = await client.post(
            f"/api/v1/canvas/{env.canvas.id}/exports",
            headers={
                "Idempotency-Key": "foreign-owner",
                "X-Bowhead-User-Id": "u_other",
            },
            json=body,
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "canvas_not_owned"
    assert await _job_count(env.db_path) == 0


async def _job_count(db_path: Path) -> int:
    async with open_shared_ctx(str(db_path)) as db, db.execute("SELECT COUNT(*) FROM canvas_export_jobs WHERE contract_version=2") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _snapshot_count(db_path: Path) -> int:
    async with open_shared_ctx(str(db_path)) as db, db.execute("SELECT COUNT(*) FROM canvas_export_snapshots") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


@pytest.mark.parametrize("stale", [False, True])
async def test_native_evidence_notes_use_request_projection_before_export_freeze(env: V2RouteEnvironment, stale: bool) -> None:
    from bowhead_core.chart.presentation_plan import artifact_text_representation, compile_notes_plan
    from bowhead_core.chart.types import ChartColumn

    env.app.state.artifact_fetch_secret = "test-artifact-fetch-secret"
    artifacts = env.app.state.artifact_store
    raw = b"value\n12\n"
    data = await artifacts.put_bytes(
        content=raw, filename="values.csv", mime="text/csv", created_by=env.canvas.user_id, session_id=env.canvas.session_id
    )
    note = await artifacts.put_bytes(
        content=b"Revenue includes returns.",
        filename="definition.txt",
        mime="text/plain",
        created_by=env.canvas.user_id,
        session_id=env.canvas.session_id,
    )
    representation = await artifact_text_representation(note, artifacts)
    assert representation is not None
    spec = {
        "kind": "table",
        "title": "Revenue",
        "row_count": 1,
        "data_artifact_id": data.id,
        "columns": [{"key": "value", "header": "Value", "type": "numeric"}],
        "rows": [{"value": 12}],
    }
    verified = [
        {
            "id": "definition",
            "measure_keys": ["value"],
            "excerpt": "Revenue includes returns.",
            "source_ref": {"id": note.id, "artifact_id": note.id, "tool": note.source, "label": note.filename},
            "verified_content_digest": representation[1],
        }
    ]
    spec["presentation_plan"] = compile_notes_plan(
        spec, verified, artifact_id=data.id, raw=raw, columns=[ChartColumn(key="value", header="Value", type="numeric")]
    )
    await env.canvases.append_section(canvas_id=env.canvas.id, kind="chart", payload={"spec": spec})
    if stale:
        Path(note.path).write_bytes(b"Changed definition")
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body(client, env.canvas.id)
        response = await client.post(f"/api/v1/canvas/{env.canvas.id}/exports", json=body, headers={"Idempotency-Key": "native-evidence"})
    assert response.status_code == 202, response.text
    snapshot = await env.app.state.export_snapshot_store.get_snapshot(response.json()["snapshot_id"])
    assert snapshot is not None
    serialized = json.dumps(snapshot.payload)
    assert note.id not in serialized
    assert ("Revenue includes returns." in serialized) is not stale
    assert "source-ordinal:" in serialized


@pytest.mark.integration
@pytest.mark.parametrize("presentation", ["plain", "trends", "coverage", "frames"])
async def test_composed_chart_receipt_compares_screen_before_export_freeze(env: V2RouteEnvironment, presentation: str) -> None:
    from bowhead_core._ref_allowlist import RefAllowlist
    from bowhead_core.canvas_tools import _add_chart, _parse_csv_with_types
    from bowhead_core.chart.binding_catalog import build_binding_catalog
    from bowhead_core.tool_registry import ToolExecutionContext

    effective = _effective_chart_style_v2()

    class _ChartStyleService:
        async def effective(self, **kwargs):
            return effective, "cache-identity"

    env.app.state.chart_style_read_service = _ChartStyleService()
    env.app.state.artifact_fetch_secret = "test-artifact-fetch-secret"
    artifacts = env.app.state.artifact_store
    raw = b"month,revenue,cost\n2026-01,12,5\n2026-02,21,7\n"
    if presentation in {"trends", "frames"}:
        raw = b"month,region,revenue,cost\n2026-01,A,12,5\n2026-02,A,21,7\n2026-01,B,9,3\n2026-02,B,15,6\n"
    data = await artifacts.put_bytes(
        content=raw,
        filename="revenue.csv",
        mime="text/csv",
        created_by=env.canvas.user_id,
        session_id=env.canvas.session_id,
    )
    ctx = ToolExecutionContext(
        workspace_id=env.canvas.workspace_id,
        member_uid="",
        user_id=env.canvas.user_id,
        turn_id="t_composed",
        datarc=None,
        session_id=env.canvas.session_id,
        artifact_store=artifacts,
        canvas_store=env.canvases,
    )
    ctx._ν15_allowlist = RefAllowlist()
    ctx._ν15_allowlist.add("data", data.id)
    args = {
        "data_artifact_id": data.id,
        "title": "Monthly revenue",
        "views": [
            {"id": "trend", "title": "Revenue", "hint": "trend", "force_kind": "line", "series": ["revenue"]},
            {"id": "detail", "title": "Observations", "hint": "raw", "force_kind": "table", "series": ["revenue", "cost"]},
        ],
        "layout": "comparison_grid",
    }
    rows, columns = _parse_csv_with_types(raw)
    candidates = build_binding_catalog(columns, rows, raw=raw)["candidates"]
    if presentation in {"trends", "frames"}:
        binding = next(item["id"] for item in candidates if item["kind"] == "frame")
        if presentation == "trends":
            args["presentation_plan"] = {"version": 2, "table_trends": [{"view_id": "detail", "binding_id": binding, "measure_key": "revenue"}]}
        else:
            args["views"][0].update(force_kind="scatter", series=["revenue", "cost"])
            args["presentation_plan"] = {
                "version": 1,
                "interactions": [{"preset": "frame_player", "view_ids": ["trend", "detail"], "binding_id": binding}],
            }
    elif presentation == "coverage":
        statement = json.dumps(
            {
                "role": "coverage",
                "time_key": "month",
                "measure_keys": ["revenue"],
                "text": "Partial reporting period",
                "lower": "2026-01",
                "upper": "2026-02",
            }
        )
        note = await artifacts.put_bytes(
            content=statement.encode(),
            filename="scope.json",
            mime="application/json",
            created_by=env.canvas.user_id,
            session_id=env.canvas.session_id,
        )
        ref = ctx._ν15_allowlist.add("data", note.id)
        binding = next(item["id"] for item in candidates if item["kind"] == "time")
        args["presentation_plan"] = {
            "version": 2,
            "evidence_notes": [{"id": "scope", "measure_keys": ["revenue"], "ref_id": str(ref), "excerpt": statement}],
            "annotations": [
                {
                    "id": "period",
                    "view_id": "trend",
                    "binding_id": binding,
                    "kind": "interval",
                    "measure_key": "revenue",
                    "lower": "2026-01",
                    "upper": "2026-02",
                    "role": "coverage",
                    "note_ids": ["scope"],
                    "label": "Partial reporting period",
                }
            ],
        }
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    transport = httpx2.ASGITransport(app=env.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        body = await _prepared_body_v2(client, env.canvas.id, effective)
        response = await client.post(f"/api/v1/canvas/{env.canvas.id}/exports", json=body, headers={"Idempotency-Key": "composed-screen"})
        assert response.status_code == 202, response.text
        snapshot = await env.app.state.export_snapshot_store.get_snapshot(response.json()["snapshot_id"])
        section = next(row for row in snapshot.payload["sections"] if row["id"] == result["section_id"])
        assert section["payload"]["spec"]["views"][0]["frozen_chart_presentation"]
        body["client_visual_receipts"][0]["screen_visual_input_sha256"] = "sha256:" + "0" * 64
        rejected = await client.post(f"/api/v1/canvas/{env.canvas.id}/exports", json=body, headers={"Idempotency-Key": "forged-composed-screen"})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "surface_projection_conflict"
