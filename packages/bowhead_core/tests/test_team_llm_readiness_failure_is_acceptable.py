"""探针失败 / 证据过期 / 从没验过 —— 都不得把团队永久锁在自己的配置外面。

2026-08-14 之前只有 `not_testable` 有「确认风险后应用」这条路；`failed` / `stale` /
`unknown` 是硬拦、**无任何 override**。116 实证这有多危险：一个纯 HTTP 导致的前端崩溃
让证据表长期 0 行 → 17 条配置全 unknown → 管理员再也存不下模型配置，而页面把原因显示成
「网络故障」。同一批里 Doubao 两条 `model_identity_mismatch` 还是 HTTP 200 —— 只因返回的
model 字符串与配置不严格相等就判失败，说明**判据本身也会错**。

所以这三种状态改为降级到既有的显式认领通道。本文件钉住两侧：
- 松的那侧：认领后必须真的能应用；
- 紧的那侧：没认领不许应用、认领 A 不得顺带放行 B、能力没声明仍然硬拦。
"""

from __future__ import annotations

import base64

import pytest
from bowhead_core.team_llm_readiness import (
    ReadinessFact,
    build_required_assignments,
    compute_team_llm_readiness,
)

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def _config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "main": {
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "secret",
                "models": [{"id": "gpt-main", "capabilities": ["chat", "tool"]}],
            }
        },
        "tiers": {
            "primary": {"provider": "main", "model": "gpt-main"},
            "fast": {"follow": "primary"},
            "judge": {"provider": "main", "model": "gpt-main"},
        },
    }


@pytest.fixture(autouse=True)
def _fernet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)


def _assignments():
    return build_required_assignments(_config(), provider_ids={"main": f"llmp_{'1' * 32}"})


def _facts(assignments, state: str):
    return {a.scope_key: ReadinessFact(state=state, evidence_id="e-1") for a in assignments}


@pytest.mark.parametrize(
    ("state", "risk_code"),
    [
        ("failed", "capability_failed"),
        ("stale", "capability_stale"),
        ("unknown", "capability_unknown"),
    ],
)
def test_failure_states_are_acceptable_instead_of_hard_blocked(state: str, risk_code: str) -> None:
    assignments = _assignments()
    facts = _facts(assignments, state)

    decision = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
    )

    # No longer blocking (that path has no override),
    assert not decision.blocking
    # but never silently allow: acknowledge each item; applying is forbidden before acknowledgment.
    assert decision.may_apply is False
    assert decision.required_acceptances
    assert all(r.risk_code == risk_code for r in decision.required_acceptances)
    assert all(row.status == "risk_acceptance_required" for row in decision.rows)

    accepted = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
        active_acceptances=frozenset(a.acceptance_key_for(risk_code) for a in assignments),
    )

    assert accepted.may_apply is True
    assert all(row.status == "risk_accepted" for row in accepted.rows)


def test_accepting_one_risk_kind_does_not_cover_another() -> None:
    """认领「测不了」不得顺带放行「测了但失败」。

    认领键此前不含 risk_code。若不补上，一次 `capability_not_testable` 的认领会在该
    scope 之后**任何**失败上继续生效——用户从没为「它失败了」这件事签过字。
    """
    assignments = _assignments()
    facts = _facts(assignments, "failed")

    stale_acceptance = frozenset(a.acceptance_key_for("capability_not_testable") for a in assignments)
    decision = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
        active_acceptances=stale_acceptance,
    )

    assert decision.may_apply is False
    assert all(row.status == "risk_acceptance_required" for row in decision.rows)


def test_undeclared_capability_is_still_hard_blocked() -> None:
    """没声明该能力仍然硬拦 —— 这不是「验证失败」，是路由必然打不通。

    负对照：本次放宽只该动验证结果那一类，摘掉它就该红。
    """
    config = _config()
    config["providers"]["main"]["models"] = [{"id": "gpt-main", "capabilities": ["chat"]}]
    assignments = build_required_assignments(config, provider_ids={"main": f"llmp_{'1' * 32}"})
    verified = {a.scope_key: ReadinessFact(state="verified", evidence_id="e-1") for a in assignments}

    decision = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=verified,
    )

    assert decision.blocking
    assert any(issue.code == "required_capability_not_declared" for issue in decision.blocking)
    assert decision.may_apply is False
