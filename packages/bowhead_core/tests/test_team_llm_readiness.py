from __future__ import annotations

import base64

from bowhead_core.team_llm_readiness import (
    ReadinessFact,
    build_required_assignments,
    compute_team_llm_readiness,
    external_readiness_rows,
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
                "models": [
                    {
                        "id": "gpt-main",
                        "capabilities": ["chat", "tool"],
                    },
                    {
                        "id": "rerank-1",
                        "capabilities": ["rerank"],
                    },
                ],
            }
        },
        "tiers": {
            "primary": {"provider": "main", "model": "gpt-main"},
            "fast": {"follow": "primary"},
            "judge": {"provider": "main", "model": "gpt-main"},
        },
        "kb": {
            "models": {
                "row": {
                    "provider": "main",
                    "real_id": "gpt-main",
                    "capability": "row_extractor",
                    "protocol": "openai_compat",
                },
                "rerank": {
                    "provider": "main",
                    "real_id": "rerank-1",
                    "capability": "rerank",
                    "protocol": "openai_compat",
                },
            },
            "active": {
                "row_extractor": "row",
                "rerank": "rerank",
            },
        },
        "media": {"models": {}, "active": {}},
        "search": {"models": {}, "active": {}},
    }


def test_required_assignments_preserve_fast_and_judge_chat_only(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    assignments = build_required_assignments(
        _config(),
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )

    by_role: dict[str, list[str]] = {}
    for assignment in assignments:
        by_role.setdefault(assignment.role_key, []).append(assignment.capability)
    assert by_role["tier.primary"] == ["chat", "tool"]
    assert by_role["tier.fast"] == ["chat"]
    assert by_role["tier.judge"] == ["chat"]
    assert by_role["kb.row_extractor"] == ["chat", "tool"]
    assert by_role["kb.rerank"] == ["rerank"]


def test_readiness_blocks_undeclared_and_only_accepts_nonrouting_risk(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    config = _config()
    assignments = build_required_assignments(
        config,
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )
    facts = {
        assignment.scope_key: ReadinessFact(
            state=("not_testable" if assignment.capability == "rerank" else "verified"),
            evidence_id=f"evidence-{index}",
        )
        for index, assignment in enumerate(assignments)
    }
    first = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
    )
    assert not first.blocking
    assert len(first.required_acceptances) == 1
    assert first.required_acceptances[0].assignment.role_key == "kb.rerank"
    assert first.may_apply is False

    accepted = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
        active_acceptances=frozenset(
            {
                first.required_acceptances[0].assignment.acceptance_key_for(
                    first.required_acceptances[0].risk_code,
                ),
            },
        ),
    )
    assert accepted.may_apply is True
    assert any(row.status == "risk_accepted" for row in accepted.rows)


def test_aged_only_survives_unchanged_scope_and_grandfather_is_exact(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    assignments = build_required_assignments(
        _config(),
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )
    aged = {
        assignment.scope_key: ReadinessFact(
            state="aged",
            evidence_id="old-evidence",
        )
        for assignment in assignments
    }
    changed = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=aged,
    )
    assert changed.blocking

    unchanged = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=aged,
        previous_scopes=frozenset(assignment.scope_key for assignment in assignments),
    )
    assert not unchanged.blocking
    assert unchanged.may_apply is True
    assert all(row.status == "warning" for row in unchanged.rows)

    unknown = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts={},
        grandfather_scopes=frozenset(assignment.scope_key for assignment in assignments),
    )
    assert unknown.may_apply is True
    assert all(row.status == "grandfathered" for row in unknown.rows)

    invalidated = compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts={},
        grandfather_scopes=frozenset(assignment.scope_key for assignment in assignments),
        baseline_will_invalidate=True,
    )
    # 2026-08-14 contract change: invalidated baselines no longer hard-block these scopes; instead require explicit
    # acceptance of each (`capability_unknown`). The protected behavior is unchanged: baseline invalidation must
    # not silently allow application. Replace `blocking` with `may_apply is False` plus required acceptance items.
    assert invalidated.may_apply is False
    assert invalidated.required_acceptances
    assert all(request.risk_code == "capability_unknown" for request in invalidated.required_acceptances)
    assert all(row.status == "risk_acceptance_required" for row in invalidated.rows)


def test_external_rows_are_informational() -> None:
    rows = external_readiness_rows(_config())
    assert [row.status for row in rows] == [
        "unavailable",
        "unavailable",
    ]


# Readiness hashes describe conclusions, not evidence-row bookkeeping IDs.
# Fresh probes can persist new evidence IDs with identical conclusions; including
# those IDs in the semantic hash would cause a stale-review loop on Apply.


def _verified_facts(assignments, *, evidence_prefix: str = "evidence") -> dict:
    return {
        assignment.scope_key: ReadinessFact(
            state="verified",
            evidence_id=f"{evidence_prefix}-{index}",
        )
        for index, assignment in enumerate(assignments)
    }


def _decide(assignments, facts, **kwargs):
    return compute_team_llm_readiness(
        expected_version=7,
        assignments=assignments,
        facts=facts,
        **kwargs,
    )


def test_semantic_hash_ignores_a_fresh_probe_that_reconfirms_the_same_state(
    monkeypatch,
) -> None:
    """★ 同样的结论、不同的证据行 → 哈希必须相同，否则跑一次探针就把 review 作废。"""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    assignments = build_required_assignments(
        _config(),
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )

    reviewed = _decide(assignments, _verified_facts(assignments, evidence_prefix="lve_old"))
    reprobed = _decide(assignments, _verified_facts(assignments, evidence_prefix="lve_new"))

    assert reviewed.draft_semantic_hash == reprobed.draft_semantic_hash


def test_semantic_hash_still_changes_when_the_conclusion_changes(monkeypatch) -> None:
    """反向对照：真的结论变化仍然必须拦住，别把修复做成一刀切放行。

    `verified -> aged` 会把 status 变成 blocked/warning，那种 409 是对的。
    """
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    assignments = build_required_assignments(
        _config(),
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )
    verified = _verified_facts(assignments)
    aged = dict(verified)
    first_key = next(iter(aged))
    aged[first_key] = ReadinessFact(state="aged", evidence_id=verified[first_key].evidence_id)

    assert _decide(assignments, verified).draft_semantic_hash != _decide(assignments, aged).draft_semantic_hash


def test_semantic_hash_still_changes_when_the_assignment_changes(monkeypatch) -> None:
    """反向对照：换了模型/配置签名仍然必须拦住。"""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    base_config = _config()
    assignments = build_required_assignments(
        base_config,
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )
    swapped_config = _config()
    swapped_config["tiers"]["judge"] = {"provider": "main", "model": "rerank-1"}
    swapped = build_required_assignments(
        swapped_config,
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )

    assert _decide(assignments, _verified_facts(assignments)).draft_semantic_hash != _decide(swapped, _verified_facts(swapped)).draft_semantic_hash


def test_semantic_hash_does_not_carry_evidence_ids(monkeypatch) -> None:
    """★ 直接钉住载荷本身：evidence_id 不得再进哈希。

    只比两次哈希相等的话，将来有人把 evidence_id 换成别的等价簿记字段（观测时间、
    batch id…）照样会重现这个 409；这条盯的是「簿记字段一律不进结论哈希」。
    """
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    assignments = build_required_assignments(
        _config(),
        provider_ids={"main": f"llmp_{'1' * 32}"},
    )
    facts = _verified_facts(assignments)
    only_evidence_differs = {key: ReadinessFact(state=fact.state, evidence_id="totally-different-id") for key, fact in facts.items()}

    assert _decide(assignments, facts).draft_semantic_hash == _decide(assignments, only_evidence_differs).draft_semantic_hash
