"""V2.8 A2: metrics_ingest route 单测."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from bowhead_core import metrics as core_metrics


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    from bowhead_gateway.routes.auth import _current_user, _store
    from bowhead_gateway.routes.metrics_ingest import router

    class _RateStore:
        allowed = True

        async def rate_limit_check(self, *_args, **_kwargs) -> bool:
            return self.allowed

    class _TelemetryStore:
        def __init__(self) -> None:
            self.seen: set[tuple[str, str]] = set()

        async def record_composer_telemetry_event(
            self,
            *,
            user_id: str,
            event_id: str,
            occurred_at,
        ) -> bool:
            key = (user_id, event_id)
            if key in self.seen:
                return False
            self.seen.add(key)
            return True

    rate_store = _RateStore()
    a.state.rate_store = rate_store
    a.state.session_store = _TelemetryStore()
    a.dependency_overrides[_current_user] = lambda: SimpleNamespace(
        id="u_test",
        team_id="team_test",
    )
    a.dependency_overrides[_store] = lambda: rate_store
    a.include_router(router, prefix="/api/v1/metrics")
    return a


@pytest.mark.asyncio
async def test_allowed_metric_inc(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_trace_strip_expanded_total",
                "labels": {"intent": "trend"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True


@pytest.mark.asyncio
async def test_disallowed_metric_silently_ignored(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/event",
            json={"name": "bowhead_random_evil_metric", "labels": {}},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": False, "reason": "not_allowed"}


@pytest.mark.asyncio
async def test_unknown_label_dropped(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_sql_chip_clicked_total",
                "labels": {"tool_hash": "ab12cd", "evil_label": "x" * 1000},
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_long_label_value_clamped(app: FastAPI) -> None:
    """label value > 64 字符截断, 防 cardinality."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_plan_step_mismatch_total",
                "labels": {"intent": "x" * 200, "tool_name": "y" * 200},
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_canvas_report_metrics_allowed(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        for payload in (
            {
                "name": "bowhead_canvas_citation_total",
                "labels": {
                    "action": "open_panel",
                    "kind": "numbered",
                    "quality": "artifact_only",
                },
            },
            {
                "name": "bowhead_canvas_ai_rewrite_total",
                "labels": {
                    "action": "done",
                    "preset": "grammar",
                    "mode": "backend",
                },
            },
            {
                "name": "bowhead_canvas_data_focus_total",
                "labels": {
                    "action": "request",
                    "artifact": "art_high_cardinality",
                },
            },
            {
                "name": "bowhead_canvas_report_perf_total",
                "labels": {
                    "action": "set_content",
                    "sections": "31-60",
                    "doc_size": "150-500kb",
                    "duration": "50-150ms",
                },
            },
            {
                "name": "bowhead_chart_fallback_table_total",
                "labels": {
                    "reason": "config_error",
                    "kind": "bar",
                },
            },
            {
                "name": "bowhead_report_library_total",
                "labels": {
                    "action": "load_error",
                },
            },
            {
                "name": "bowhead_canvas_export_total",
                "labels": {
                    "action": "done",
                    "fmt": "pptx",
                },
            },
            {
                "name": "bowhead_composer_layout_mode_total",
                "labels": {
                    "surface": "workbench",
                    "mode": "compact",
                },
            },
            {
                "name": "bowhead_design_flatten_ui_total",
                "labels": {
                    "action": "start",
                },
            },
            {
                "name": "bowhead_report_link_total",
                "labels": {
                    "action": "route_404",
                    "surface": "reports",
                },
            },
            {
                "name": "bowhead_home_proposal_ui_total",
                "labels": {
                    "action": "start",
                    "strategy": "llm",
                    "kind": "create",
                    "accepted": "true",
                    "generation_id": "hp_high_cardinality_dropped",
                },
            },
        ):
            r = await c.post("/api/v1/metrics/event", json=payload)
            assert r.status_code == 200
            assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_results_navigation_metric_keeps_bounded_dimensions(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def capture(
        name: str,
        *,
        labels: dict[str, str],
        **options: str,
    ) -> None:
        assert set(options) <= {"help"}
        calls.append((name, labels))

    monkeypatch.setattr(core_metrics, "inc", capture)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as c:
        response = await c.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_canvas_data_focus_total",
                "labels": {
                    "action": "request",
                    "entry_point": "source_panel",
                    "result_type": "code",
                    "artifact": "art_high_cardinality",
                },
            },
        )

    assert response.json() == {"ok": True}
    assert calls == [
        (
            "bowhead_canvas_data_focus_total",
            {
                "action": "request",
                "entry_point": "source_panel",
                "result_type": "code",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_results_workspace_metric_requires_a_complete_finite_tuple(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def capture(name: str, *, labels: dict[str, str], **_options: str) -> None:
        calls.append((name, labels))

    monkeypatch.setattr(core_metrics, "inc", capture)
    valid_labels = {
        "action": "result_selected",
        "result_type": "artifact",
        "group": "evidence",
        "entry_point": "navigator",
        "mode": "preview",
        "error_class": "none",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        accepted = await client.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_results_workspace_total",
                "labels": valid_labels,
            },
        )
        feedback_labels = {
            **valid_labels,
            "action": "unsupported_feedback",
            "mode": "details",
            "error_class": "unsupported",
        }
        feedback = await client.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_results_workspace_total",
                "labels": feedback_labels,
            },
        )
        sensitive = await client.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_results_workspace_total",
                "labels": {**valid_labels, "artifact_id": "art_secret"},
            },
        )
        free_text = await client.post(
            "/api/v1/metrics/event",
            json={
                "name": "bowhead_results_workspace_total",
                "labels": {**valid_labels, "entry_point": "customer-input.csv"},
            },
        )

    assert accepted.json() == {"ok": True}
    assert feedback.json() == {"ok": True}
    assert sensitive.json() == {"ok": False, "reason": "invalid_labels"}
    assert free_text.json() == {"ok": False, "reason": "invalid_labels"}
    assert calls == [
        ("bowhead_results_workspace_total", valid_labels),
        ("bowhead_results_workspace_total", feedback_labels),
    ]


@pytest.mark.asyncio
async def test_unified_sidebar_metrics_allowed(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        for payload in (
            {
                "name": "bowhead_sidebar_state_changed_total",
                "labels": {
                    "from": "compact",
                    "to": "overlay",
                    "viewport": "narrow_desktop",
                    "trigger": "control",
                },
            },
            {
                "name": "bowhead_nav_destination_opened_total",
                "labels": {
                    "primary": "library",
                    "leaf": "knowledge",
                    "surface": "drawer",
                    "role": "member",
                },
            },
            {
                "name": "bowhead_sidebar_recent_opened_total",
                "labels": {"source": "search", "surface": "flyout"},
            },
            {
                "name": "bowhead_sidebar_search_total",
                "labels": {
                    "action": "open",
                    "kind": "knowledge",
                    "surface": "drawer",
                },
            },
            {
                "name": "bowhead_sidebar_session_action_total",
                "labels": {
                    "action": "archive",
                    "outcome": "blocked",
                    "surface": "drawer",
                },
            },
            {
                "name": "bowhead_settings_opened_total",
                "labels": {"surface": "user_menu", "scope": "personal"},
            },
            {
                "name": "bowhead_admin_destination_opened_total",
                "labels": {"destination": "approvals"},
            },
            {
                "name": "bowhead_admin_destination_opened_total",
                "labels": {"destination": "audit"},
            },
        ):
            r = await c.post("/api/v1/metrics/event", json=payload)
            assert r.status_code == 200
            assert r.json()["ok"] is True


def _composer_event(**overrides) -> dict:
    payload = {
        "event_id": "018f0cc3-e4a2-7b21-a109-001122334455",
        "event_name": "composer_tools_opened",
        "occurred_at": datetime.now(tz=UTC).isoformat(),
        "surface": "workbench",
        "composer_mode": "compact",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_composer_event_is_durably_deduplicated(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        first = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(),
        )
        replay = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(),
        )

    assert first.status_code == 200
    assert first.json() == {"ok": True, "duplicate": False}
    assert replay.json() == {"ok": True, "duplicate": True}


@pytest.mark.asyncio
async def test_composer_event_rejects_cross_origin_and_sensitive_unknown_fields(
    app: FastAPI,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        cross_origin = await client.post(
            "/api/v1/metrics/composer-event",
            headers={"Origin": "https://evil.example"},
            json=_composer_event(),
        )
        unknown = await client.post(
            "/api/v1/metrics/composer-event",
            headers={"Origin": "http://t"},
            json=_composer_event(model_id="secret-runtime-id"),
        )

    assert cross_origin.status_code == 403
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_composer_event_contract_rejects_invalid_event_specific_values(
    app: FastAPI,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        tools_in_comfortable = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(composer_mode="comfortable"),
        )
        bad_planning_entry = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(
                event_name="composer_planning_toggled",
                enabled=True,
                entry="keyboard",
            ),
        )

    assert tools_in_comfortable.status_code == 422
    assert bad_planning_entry.status_code == 422


@pytest.mark.asyncio
async def test_composer_event_rate_limit_is_fail_closed(app: FastAPI) -> None:
    app.state.rate_store.allowed = False
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        response = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(),
        )

    assert response.status_code == 429


@pytest.mark.asyncio
async def test_transport_result_is_server_classified_and_deduplicated(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core import metrics

    observed: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        metrics,
        "inc",
        lambda name, **kwargs: observed.append((name, kwargs["labels"])),
    )
    event = _composer_event(
        event_name="composer_send_result",
        result="transport_error",
        model_control_batch="a",
        has_attachments=True,
        surface="home",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        first = await client.post("/api/v1/metrics/composer-event", json=event)
        replay = await client.post("/api/v1/metrics/composer-event", json=event)

    assert first.json() == {"ok": True, "duplicate": False}
    assert replay.json() == {"ok": True, "duplicate": True}
    assert [name for name, _labels in observed] == [
        "bowhead_composer_product_event_total",
        "composer_transport_error_total",
        "composer_attachment_transport_error_total",
    ]
    assert observed[1][1]["result"] == "transport_error"
    assert observed[1][1]["surface"] == "home"


@pytest.mark.asyncio
async def test_attachment_action_uses_only_bounded_content_free_detail(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core import metrics

    observed: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        metrics,
        "inc",
        lambda name, **kwargs: observed.append((name, kwargs["labels"])),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        response = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(
                event_name="composer_attachment_action",
                action="upload",
                result="failed",
                kind="bundle",
                size_band="large",
            ),
        )

    assert response.status_code == 200
    assert observed[0] == (
        "bowhead_composer_product_event_total",
        {
            "event_name": "composer_attachment_action",
            "surface": "workbench",
            "composer_mode": "compact",
            "detail": "upload:failed:bundle:large",
        },
    )
    assert observed[1][0] == "composer_attachment_lifecycle_total"
    assert observed[1][1] == {
        "action": "upload",
        "result": "failed",
        "kind": "bundle",
        "size_band": "large",
        "surface": "workbench",
    }


@pytest.mark.asyncio
async def test_browser_model_batch_does_not_change_transport_metric(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core import metrics

    observed: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        metrics,
        "inc",
        lambda name, **kwargs: observed.append((name, kwargs["labels"])),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        response = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(
                event_name="composer_send_result",
                result="transport_error",
                model_control_batch="c",
                has_attachments=False,
            ),
        )

    assert response.status_code == 200
    transport_metric = next(labels for name, labels in observed if name == "composer_transport_error_total")
    assert transport_metric == {
        "result": "transport_error",
        "surface": "workbench",
    }


@pytest.mark.asyncio
async def test_client_cannot_spoof_server_owned_send_results(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Origin": "http://t"},
    ) as client:
        response = await client.post(
            "/api/v1/metrics/composer-event",
            json=_composer_event(
                event_name="composer_send_result",
                result="success",
                model_control_batch="a",
                has_attachments=False,
            ),
        )

    assert response.status_code == 422


# ── Batch entry (2026-08-08) ────────────────────────────────────────────────
#
# Why added: frontend `track()` sent a separate `fetch(keepalive)` per metric. Hydrating a 30-section report
# sends over a dozen `bowhead_canvas_report_perf_total` events → one connection each → browser
# `ERR_INSUFFICIENT_RESOURCES`, and burns `_require_ingest_access`'s 120/60s budget
# per event rather than per submission. Batch entry uses one connection + one quota unit per submission.


@pytest.mark.asyncio
async def test_batch_records_every_event(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ counter 语义一条不少——批量不是去重。"""
    observed: list[tuple[str, dict[str, str]]] = []

    def _capture(name: str, labels: dict[str, str] | None = None, **_kw: object) -> None:
        observed.append((name, dict(labels or {})))

    monkeypatch.setattr(core_metrics, "inc", _capture)
    same = {
        "name": "bowhead_canvas_report_perf_total",
        "labels": {
            "action": "set_content",
            "sections": "16-30",
            "doc_size": "50-150kb",
            "duration": "150ms+",
        },
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post("/api/v1/metrics/events", json={"events": [same] * 14})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "accepted": 14, "rejected": {}}
    # 14 setContent calls must record 14 events; transport deduplication erases the very activity being measured.
    assert len(observed) == 14


@pytest.mark.asyncio
async def test_batch_shares_the_single_event_allowlist(app: FastAPI) -> None:
    """批量入口不得成为绕过 allowlist / label 校验的旁路。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/events",
            json={
                "events": [
                    {"name": "bowhead_trace_strip_expanded_total", "labels": {"intent": "trend"}},
                    {"name": "bowhead_random_evil_metric", "labels": {}},
                    # Allowlisted but incomplete label tuple → reject entire event (same rule as /event).
                    {"name": "bowhead_results_workspace_total", "labels": {"action": "pane_opened"}},
                ]
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == {"not_allowed": 1, "invalid_labels": 1}


@pytest.mark.asyncio
async def test_batch_counts_as_one_rate_limit_unit(app: FastAPI) -> None:
    """一批 = 一个限流额度（这正是它要解决的问题）。"""
    calls: list[str] = []
    rate_store = app.state.rate_store

    async def _check(key: str, *, limit: int, window_s: int) -> bool:
        calls.append(key)
        return True

    rate_store.rate_limit_check = _check
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/events",
            json={"events": [{"name": "bowhead_cmd_k_total", "labels": {}}] * 20},
        )

    assert r.status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_batch_rejects_oversized_and_empty(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        too_many = await c.post(
            "/api/v1/metrics/events",
            json={"events": [{"name": "bowhead_cmd_k_total", "labels": {}}] * 51},
        )
        empty = await c.post("/api/v1/metrics/events", json={"events": []})

    assert too_many.status_code == 422
    assert empty.status_code == 422


@pytest.mark.asyncio
async def test_batch_honours_rate_limit(app: FastAPI) -> None:
    app.state.rate_store.allowed = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as c:
        r = await c.post(
            "/api/v1/metrics/events",
            json={"events": [{"name": "bowhead_cmd_k_total", "labels": {}}]},
        )
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_batch_requires_same_origin(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://evil"}) as c:
        r = await c.post(
            "/api/v1/metrics/events",
            json={"events": [{"name": "bowhead_cmd_k_total", "labels": {}}]},
        )
    assert r.status_code == 403
