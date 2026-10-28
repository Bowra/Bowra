from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_gateway.team_onboarding import TeamOnboardingReadinessService


def _model_config(*, with_secret: bool = True) -> dict:
    provider = {
        "type": "openai_compatible",
        "base_url": "https://models.example.com/v1",
        "models": [
            {
                "id": "agent-main",
                "capabilities": ["chat", "tool", "vision", "think"],
            },
        ],
    }
    if with_secret:
        provider["api_key"] = "secret-that-must-not-leak"
    return {
        "schema_version": 2,
        "providers": {"primary": provider},
        "tiers": {
            "primary": {"provider": "primary", "model": "agent-main"},
            "fast": {"follow": "primary"},
            "judge": {"auto": True},
        },
        "kb": {
            "models": {
                "embedding": {
                    "provider": "primary",
                    "capability": "embed",
                    "real_id": "embed-real-id",
                    "protocol": "openai_compat",
                    "dim": 1536,
                },
            },
            "active": {"embed": "embedding"},
            "index_version": "idx_1",
        },
    }


class _UserStore:
    def __init__(
        self,
        config: dict,
        *,
        active_seat_failure: bool = False,
    ) -> None:
        self.config = config
        self.active_seat_failure = active_seat_failure

    async def get_team(self, _team_id: str):
        return SimpleNamespace(name="Acme")

    async def get_team_llm_config_with_version(self, _team_id: str):
        return self.config, 1

    async def count_active_workspaces(self, _team_id: str) -> int:
        return 1

    async def list_workspaces_for_user(self, _user_id: str, *, team_id: str):
        return [{"workspace_id": "ws_active", "team_id": team_id}]

    async def list_active_workspace_ids(self, _team_id: str) -> list[str]:
        return ["ws_active"]

    async def count_active_seats(self, _team_id: str) -> int:
        if self.active_seat_failure:
            raise RuntimeError("seat store unavailable with secret=value")
        return 1

    async def list_team_users_by_roles(
        self,
        _team_id: str,
        _roles: set[str],
        *,
        include_creator: bool,
    ) -> list[str]:
        assert include_creator is True
        return ["u_owner"]


class _Entitlements:
    async def get(self, _team_id: str):
        return SimpleNamespace(
            health="active",
            max_seats=5,
            expires_at="2027-01-01T00:00:00Z",
            grace_until="2027-01-08T00:00:00Z",
        )

    @staticmethod
    def is_feature_licensed_from_snapshot(_snapshot, feature: str) -> bool:
        return feature == "kb"


class _PublicEntries:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def list_for_team(self, _team_id: str):
        if self.fail:
            raise RuntimeError("store unavailable with secret=value")
        return [
            SimpleNamespace(
                id="entry_1",
                status="active",
                app_root="https://bowhead.example/",
            ),
        ]


def _request(
    config: dict,
    *,
    public_entry_failure: bool = False,
    active_seat_failure: bool = False,
):
    state = SimpleNamespace(
        user_store=_UserStore(
            config,
            active_seat_failure=active_seat_failure,
        ),
        entitlements=_Entitlements(),
        public_entry_store=_PublicEntries(fail=public_entry_failure),
        session_store=SimpleNamespace(
            first_turn_readiness_facts_for_team=_successful_turn,
        ),
        datarc_connection_store=SimpleNamespace(list_connections=_no_connections),
        kb_files_store=SimpleNamespace(readiness_stats_for_team=_kb_stats),
        kb_doc_type_store=SimpleNamespace(
            has_many_auto_extract_for_workspaces=_no_many_extract,
        ),
        auth_policy=SimpleNamespace(
            account_creation=SimpleNamespace(value="invite_only"),
            auth_methods=[],
        ),
        routing_config_loaded=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _successful_turn(_team_id: str):
    return SimpleNamespace(
        has_any_normal_turn=True,
        has_completed_turn=True,
        has_successful_turn=True,
    )


async def _no_connections(_team_id: str):
    return []


async def _kb_stats(_team_id: str):
    return SimpleNamespace(
        active_file_count=0,
        image_file_count=0,
        scanned_pdf_evidence_count=0,
    )


async def _no_many_extract(_workspace_ids: list[str]):
    return False


@pytest.fixture(autouse=True)
def _local_resolvers(monkeypatch):
    monkeypatch.setattr(
        "bowhead_gateway.team_onboarding.current_public_access_context",
        lambda: SimpleNamespace(
            team_id="team_a",
            entry_id="entry_1",
        ),
    )
    monkeypatch.setattr(
        "bowhead_gateway.team_onboarding.resolve_smtp_config",
        _no_email,
    )
    monkeypatch.setattr(
        "bowhead_gateway.team_onboarding.team_email_config_enabled",
        lambda: True,
    )


async def _no_email(*_args, **_kwargs):
    return None


async def test_core_ready_does_not_require_datarc_or_recommendations() -> None:
    result = await TeamOnboardingReadinessService(
        _request(_model_config()),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    assert result.core.satisfied == 5
    assert result.milestone == "core_ready"
    by_id = {item.id: item for item in result.items}
    assert by_id["datarc_connection_configured"].state == "missing"
    assert by_id["datarc_connection_configured"].blocking is False
    assert by_id["knowledge_base_ready"].state == "ready"
    assert by_id["knowledge_base_ready"].action
    assert by_id["knowledge_base_ready"].action.href == "/knowledge?from=setup"
    assert by_id["llm_fallback_configured"].state == "missing"
    payload = result.model_dump_json()
    assert len(payload.encode()) < 30_000
    assert "secret-that-must-not-leak" not in payload
    assert "embed-real-id" not in payload


async def test_legacy_duplicate_fallbacks_do_not_block_core_readiness() -> None:
    config = _model_config()
    primary = {"provider": "primary", "model": "agent-main"}
    config["tiers"]["primary_fallback"] = [primary, primary]

    result = await TeamOnboardingReadinessService(
        _request(config),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    by_id = {item.id: item for item in result.items}
    assert by_id["llm_primary_configured"].satisfied is True
    assert by_id["llm_primary_configured"].reason_code != "team_model_config_invalid"
    assert by_id["llm_fallback_configured"].state == "missing"
    assert result.core.satisfied == 5
    assert result.milestone == "core_ready"


async def test_runtime_signal_reports_refusal_counter_scoped_to_team() -> None:
    """R8b（PRD C1）：context.runtime 区分「未配置」与「配置了但运行时不可达」——
    refusal counter 按 reason 聚合下发，且**按团队过滤**：本团队 + unattributed
    （含缺 team 键的旧形状行）计入，他队的 refusal 不串进本队信号（§19 follow-up）。"""
    from bowhead_core import metrics

    registry = metrics.get_registry()
    registry.reset()
    try:
        result = await TeamOnboardingReadinessService(
            _request(_model_config()),
            user_id="u_owner",
            team_id="team_a",
            role="owner",
        ).collect()
        assert result.context.runtime.llm_refusals_total == 0
        assert result.context.runtime.llm_refusals_by_reason == {}
        assert result.context.runtime.observed_since is not None

        metrics.inc("bowhead_llm_refusal_total", labels={"reason": "dreaming", "team": "team_a"})
        metrics.inc("bowhead_llm_refusal_total", labels={"reason": "dreaming", "team": "team_a"})
        metrics.inc("bowhead_llm_refusal_total", labels={"reason": "chat", "team": "unattributed"})
        metrics.inc("bowhead_llm_refusal_total", labels={"reason": "chat"})  # Old shape=unattributed fallback.
        metrics.inc("bowhead_llm_refusal_total", labels={"reason": "turn", "team": "team_OTHER"})
        result = await TeamOnboardingReadinessService(
            _request(_model_config()),
            user_id="u_owner",
            team_id="team_a",
            role="owner",
        ).collect()
        assert result.context.runtime.llm_refusals_total == 4  # team_OTHER excluded.
        assert result.context.runtime.llm_refusals_by_reason == {"dreaming": 2, "chat": 2}
    finally:
        registry.reset()


async def test_missing_provider_secret_blocks_primary_model() -> None:
    result = await TeamOnboardingReadinessService(
        _request(_model_config(with_secret=False)),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    model = next(item for item in result.items if item.id == "llm_primary_configured")
    assert model.state == "blocked"
    assert model.satisfied is False
    assert model.reason_code in {
        "primary_provider_credential_missing",
        "team_model_config_invalid",
    }
    assert result.core.satisfied == 4
    assert result.milestone == "core_configuring"


async def test_invalid_kb_config_does_not_block_primary_model_core() -> None:
    config = _model_config()
    del config["kb"]["models"]["embedding"]["real_id"]
    result = await TeamOnboardingReadinessService(
        _request(config),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    by_id = {item.id: item for item in result.items}
    assert by_id["llm_primary_configured"].satisfied is True
    assert by_id["knowledge_base_ready"].state == "blocked"
    assert by_id["knowledge_base_ready"].satisfied is False
    assert result.core.satisfied == 5


async def test_initial_embedding_reindex_is_not_reported_ready() -> None:
    config = _model_config()
    config["kb"].pop("index_version")
    config["kb"]["active"] = {}
    config["kb"]["pending_embed"] = {
        "model": "embedding",
        "target_index_version": "idx_next",
        "task_id": "kb_reindex_1",
    }
    result = await TeamOnboardingReadinessService(
        _request(config),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    kb = next(item for item in result.items if item.id == "knowledge_base_ready")
    assert kb.state == "warning"
    assert kb.satisfied is False
    assert kb.reason_code == "kb_initial_reindex_pending"
    assert any(check.id == "kb_embedding_index_ready" and check.satisfied is False for check in kb.checks)


async def test_failed_reindex_keeps_old_index_satisfied_with_warning() -> None:
    config = _model_config()
    config["kb"]["pending_embed"] = {
        "model": "embedding",
        "target_index_version": "idx_next",
        "task_id": "kb_reindex_2",
        "state": "failed",
        "error": "provider secret that must not leak",
    }
    result = await TeamOnboardingReadinessService(
        _request(config),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    kb = next(item for item in result.items if item.id == "knowledge_base_ready")
    assert kb.state == "warning"
    assert kb.satisfied is True
    assert kb.reason_code == "kb_reindex_failed"
    assert "provider secret" not in result.model_dump_json()


async def test_adapter_failure_returns_partial_unknown_without_raw_error() -> None:
    result = await TeamOnboardingReadinessService(
        _request(_model_config(), public_entry_failure=True),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    entry = next(item for item in result.items if item.id == "public_entry_ready")
    assert result.partial is True
    assert entry.state == "unknown"
    assert entry.reason_code == "public_entry_unavailable"
    assert "secret=value" not in result.model_dump_json()


async def test_optional_context_failure_keeps_readiness_response() -> None:
    result = await TeamOnboardingReadinessService(
        _request(_model_config(), active_seat_failure=True),
        user_id="u_owner",
        team_id="team_a",
        role="owner",
    ).collect()

    assert result.partial is True
    assert result.core.satisfied == 5
    assert result.context.active_seats is None
    assert "secret=value" not in result.model_dump_json()


async def test_community_readiness_keeps_setup_and_secondary_admin_actions():
    request = _request(_model_config())
    request.app.state.edition = "community"
    service = TeamOnboardingReadinessService(request, user_id="u_owner", team_id="team_a", role="owner")
    snapshot = SimpleNamespace(health="active", max_seats=None)
    availability = service._license_item(snapshot)
    assert availability.satisfied
    assert availability.action.href == "/setup/"
    secondary = await service._secondary_admin(snapshot)
    assert secondary.state != "not_applicable"
