from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.team_llm_evidence import (
    AttemptRecord,
    EvidenceRecord,
    compute_config_signature,
    legacy_provider_id,
    project_latest_evidence,
)
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

from bowhead_router import ProviderConfig, build_provider_transport_plan

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
PROVIDER_ID = "llmp_00000000000000000000000000000000"
NOW = datetime(2026, 7, 30, 8, tzinfo=UTC)


def _transport(
    *,
    base_url: str = "https://API.Example.com/v1",
    api_key: str = "secret-a",
    timeout_sec: int = 60,
):
    return build_provider_transport_plan(
        ProviderConfig(
            name="display-only",
            type="openai_compatible",
            base_url=base_url,
            api_key=api_key,
            timeout_sec=timeout_sec,
        ),
        purpose="probe_tool",
    )


def _signature(
    *,
    transport=None,
    capability="tool",
    model_id="gpt-4.1",
    testability="openai-tool-testability-v1",
    probe_contract="tool-call-v1",
    execution_policy="probe-policy-v1",
) -> str:
    return compute_config_signature(
        transport or _transport(),
        provider_id=PROVIDER_ID,
        model_id=model_id,
        capability=capability,
        testability_contract_version=testability,
        probe_contract_version=probe_contract,
        probe_execution_policy_version=execution_policy,
        root_key=ROOT_KEY,
    )


@pytest.mark.unit
def test_signature_is_canonical_and_domain_separated() -> None:
    first = _signature(
        transport=_transport(base_url="https://API.Example.com:443/v1"),
    )
    second = _signature(
        transport=_transport(base_url="https://api.example.com/v1"),
    )
    assert first == second
    assert legacy_provider_id(
        "team_a",
        "main",
        root_key=ROOT_KEY,
    ) == legacy_provider_id("team_a", "main", root_key=ROOT_KEY)
    assert legacy_provider_id(
        "team_a",
        "main",
        root_key=ROOT_KEY,
    ) != legacy_provider_id("team_b", "main", root_key=ROOT_KEY)


@pytest.mark.unit
def test_signature_changes_only_for_request_semantics() -> None:
    baseline = _signature()
    assert baseline != _signature(
        transport=_transport(api_key="secret-b"),
    )
    assert baseline != _signature(
        transport=_transport(base_url="https://api.example.com/v2"),
    )
    assert baseline != _signature(model_id="gpt-4.1-mini")
    assert baseline != _signature(capability="chat")
    assert baseline != _signature(execution_policy="probe-policy-v2")
    assert baseline != _signature(transport=_transport(timeout_sec=30))
    assert "secret-a" not in baseline


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_and_probe_share_transport_target_and_auth() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com/v1?region=cn&region=global",
        api_key="secret-a",
        provider_name="runtime",
    )
    probe = _transport(
        base_url="https://api.example.com/v1?region=cn&region=global",
        api_key="secret-a",
    )
    try:
        assert adapter._transport_plan.request_url == probe.request_url
        assert adapter._transport_plan.safe_header_semantics == probe.safe_header_semantics
        assert [(item.kind, item.name, item.semantic) for item in adapter._transport_plan.secret_components] == [
            (item.kind, item.name, item.semantic) for item in probe.secret_components
        ]
    finally:
        await adapter.close()


@pytest.mark.unit
def test_projection_keeps_fact_and_attempt_separate() -> None:
    signature = "sig-current"
    verified = EvidenceRecord(
        evidence_id="lve_verified",
        config_signature=signature,
        outcome="verified",
        safe_code="ok",
        observed_at=NOW - timedelta(hours=1),
        fresh_until=NOW + timedelta(hours=23),
    )
    projection = project_latest_evidence(
        [verified],
        current_signature=signature,
        now=NOW,
        attempt=AttemptRecord(
            state="cancelled",
            safe_code="cancelled_by_user",
            completed_at=NOW,
        ),
    )
    assert projection.fact_state == "verified"
    assert projection.attempt_state == "cancelled"

    aged = project_latest_evidence(
        [verified],
        current_signature=signature,
        now=NOW + timedelta(days=2),
    )
    assert aged.fact_state == "aged"

    stale = project_latest_evidence(
        [verified],
        current_signature="sig-new",
        now=NOW,
    )
    assert stale.fact_state == "stale"


@pytest.mark.unit
def test_newer_factual_evidence_wins() -> None:
    records = [
        EvidenceRecord(
            evidence_id="lve_old",
            config_signature="sig",
            outcome="verified",
            safe_code="ok",
            observed_at=NOW - timedelta(hours=2),
            fresh_until=NOW + timedelta(hours=22),
        ),
        EvidenceRecord(
            evidence_id="lve_new",
            config_signature="sig",
            outcome="failed",
            safe_code="model_not_found",
            observed_at=NOW,
            fresh_until=NOW,
        ),
    ]

    projection = project_latest_evidence(
        records,
        current_signature="sig",
        now=NOW,
    )

    assert projection.fact_state == "failed"
    assert projection.evidence_id == "lve_new"


def _probe_signature(provider: dict, *, capability: str = "chat", model_id: str = "m1") -> str:
    """按探针 plan 算签名 —— 与 admin_llm_config_v2 / team_llm_readiness 两个调用点同源。"""
    from bowhead_router import CompatibleProbeRegistry

    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id=PROVIDER_ID,
        provider_display_name="P",
        model_id=model_id,
        capability=capability,  # type: ignore[arg-type]
    )
    return compute_config_signature(
        plan.transport,
        provider_id=PROVIDER_ID,
        model_id=model_id,
        capability=capability,  # type: ignore[arg-type]
        testability_contract_version=plan.testability_contract_version,
        probe_contract_version=plan.probe_contract_version,
        probe_execution_policy_version=plan.probe_execution_policy_version,
        root_key=ROOT_KEY,
    )


def _openai_provider(model: dict) -> dict:
    return {
        "type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "models": [model],
    }


@pytest.mark.unit
def test_model_body_overrides_invalidate_evidence() -> None:
    """★ 改了模型级覆盖 ⇒ 旧证据必须 stale。

    此前 `request_semantics_sha256` **只对 custom_http** 计算，于是 openai / anthropic 面
    改 `request_body_overrides` 签名一字不变 —— 与权威契约「Body、模型覆盖…变化会使旧
    Evidence stale」不符。探针开始消费这一层之后，它会直接变成「证据说验过、而我们发的
    是另一个形状」的谎报。
    """
    plain = _openai_provider({"id": "m1", "capabilities": ["chat"]})
    overridden = _openai_provider(
        {"id": "m1", "capabilities": ["chat"], "request_body_overrides": {"chat": {"temperature": 1}}},
    )

    assert _probe_signature(plain) != _probe_signature(overridden)


@pytest.mark.unit
def test_outbound_params_declaration_invalidates_evidence() -> None:
    """出站声明同理：声明变了，探针发的形状就变了。"""
    plain = _openai_provider({"id": "m1", "capabilities": ["chat"]})
    declared = _openai_provider(
        {"id": "m1", "capabilities": ["chat"], "outbound_params": {"temperature": {"mode": "omit"}}},
    )

    assert _probe_signature(plain) != _probe_signature(declared)


@pytest.mark.unit
def test_signature_is_unchanged_when_nothing_is_declared() -> None:
    """★ 负对照：没配这些字段的部署，签名必须一字不变。

    否则本能力一上线就会把**所有**团队的证据全判 stale —— 那是无谓的全量重验，
    也会让「stale」这个信号本身失去意义。`follow` 视同没声明，同样不得改签名。
    """
    plain = _openai_provider({"id": "m1", "capabilities": ["chat"]})
    follow_only = _openai_provider(
        {"id": "m1", "capabilities": ["chat"], "outbound_params": {"temperature": {"mode": "follow"}}},
    )

    assert _probe_signature(plain) == _probe_signature(follow_only)
