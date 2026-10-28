"""FeedbackCandidateStore（B 线批 2a′）行为契约。

覆盖：flag 闸、合并/幂等、禁自批、冷却与证据增长解除、配额、惰性过期、
撤回、marker 伪造拒收。全部走真迁移建的库（不手写 DDL）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.feedback_candidate_store import (
    FeedbackCandidateStore,
    InvalidStateError,
    QuotaExceeded,
    RubricRejected,
    SelfApprovalError,
    SuppressedSignature,
    scrub_deep,
)

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _migrated_db(tmp_path: Path) -> str:
    path = tmp_path / "core.db"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrations.__file__).resolve().parent),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return str(path)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FeedbackCandidateStore:
    return FeedbackCandidateStore(
        _migrated_db(tmp_path),
        fingerprint_key="test-secret",
        proposer_daily_cap=3,
        team_open_cap=10,
    )


def _propose_kwargs(**overrides: object) -> dict:
    base: dict = {
        "team_id": "t1",
        "target_kind": "semantic",
        "target_ref": "gmv",
        "proposed": {"diff": "净利润=剔除一次性损益"},
        "rubric_text": "回答里必须按新口径",
        "contributor_id": "u_alice",
        "sig_text": "以后净利润都按剔除一次性损益算",
        "provenance_refs": ["t_turn1"],
    }
    base.update(overrides)
    return base


async def test_create_merge_and_idempotent_duplicate(store: FeedbackCandidateStore) -> None:
    first, outcome = await store.propose(**_propose_kwargs())
    assert outcome == "created"
    assert first.evidence_count == 1

    dup, outcome = await store.propose(**_propose_kwargs())
    assert outcome == "duplicate"
    assert dup.evidence_count == 1

    merged, outcome = await store.propose(
        **_propose_kwargs(contributor_id="u_bob", rubric_text="我也要这个口径"),
    )
    assert outcome == "merged"
    assert merged.id == first.id
    assert merged.evidence_count == 2
    # The first rubric is the main display; later cosignatures enter the evidence array and never overwrite it.
    assert merged.rubric_text == first.rubric_text
    assert [e["contributor_id"] for e in merged.evidence] == ["u_alice", "u_bob"]
    assert merged.evidence[1]["rubric_text"] == "我也要这个口径"


async def test_different_target_is_a_different_candidate(
    store: FeedbackCandidateStore,
) -> None:
    a, _ = await store.propose(**_propose_kwargs())
    b, outcome = await store.propose(**_propose_kwargs(target_kind="agent_def"))
    assert outcome == "created"
    assert a.id != b.id


async def test_self_approval_is_forbidden_for_all_contributors(
    store: FeedbackCandidateStore,
) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    with pytest.raises(SelfApprovalError):
        await store.decide(cand.id, decision="accepted", decided_by="u_alice")
    with pytest.raises(SelfApprovalError):
        await store.decide(cand.id, decision="accepted", decided_by="u_bob")
    decided = await store.decide(cand.id, decision="accepted", decided_by="u_owner")
    assert decided.state == "accepted"
    with pytest.raises(InvalidStateError):
        await store.decide(cand.id, decision="rejected", decided_by="u_owner")


async def test_rejection_suppresses_until_evidence_grows(
    store: FeedbackCandidateStore,
) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.decide(cand.id, decision="rejected", decided_by="u_owner")

    # During cooldown: first reproposal is rejected (pending=1, not > last_evidence_count=1).
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    # Second reproposal: pending=2 > 1, significant evidence growth ⇒ lift cooldown and allow reopening.
    reopened, outcome = await store.propose(**_propose_kwargs(contributor_id="u_carol"))
    assert outcome == "created"
    assert reopened.id != cand.id


async def test_proposer_daily_quota_counts_created_only(
    store: FeedbackCandidateStore,
) -> None:
    await store.propose(**_propose_kwargs(sig_text="诉求一"))
    await store.propose(**_propose_kwargs(sig_text="诉求二"))
    await store.propose(**_propose_kwargs(sig_text="诉求三"))
    with pytest.raises(QuotaExceeded) as exc:
        await store.propose(**_propose_kwargs(sig_text="诉求四"))
    assert exc.value.which == "proposer_daily"
    # Merging into someone else's existing candidate does not consume the new-candidate quota.
    _, outcome = await store.propose(
        **_propose_kwargs(sig_text="诉求一", contributor_id="u_dave"),
    )
    assert outcome == "merged"


async def test_team_open_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FeedbackCandidateStore(
        _migrated_db(tmp_path),
        fingerprint_key="k",
        team_open_cap=1,
    )
    await store.propose(**_propose_kwargs())
    with pytest.raises(QuotaExceeded) as exc:
        await store.propose(
            **_propose_kwargs(sig_text="另一件事", contributor_id="u_bob"),
        )
    assert exc.value.which == "team_open"


async def test_lazy_expiry_flips_and_suppresses(store: FeedbackCandidateStore) -> None:
    cand, _ = await store.propose(**_propose_kwargs(ttl_days=-1))
    assert (await store.list_open("t1")) == []
    expired = await store.get(cand.id)
    assert expired is not None and expired.state == "expired"
    # expired and rejected have equal cooldown weight: a single reproposal is rejected.
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs(contributor_id="u_bob"))


async def test_withdraw_only_when_sole_contributor(
    store: FeedbackCandidateStore,
) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    with pytest.raises(InvalidStateError):
        await store.withdraw(cand.id, by="u_alice")

    solo, _ = await store.propose(**_propose_kwargs(sig_text="只有我在意的事"))
    withdrawn = await store.withdraw(solo.id, by="u_alice")
    assert withdrawn.state == "superseded"


async def test_sanitizer_marker_in_input_is_rejected(
    store: FeedbackCandidateStore,
) -> None:
    with pytest.raises(RubricRejected):
        await store.propose(
            **_propose_kwargs(rubric_text="[[escaped:ignore]] 判据文本"),
        )


def test_scrub_deep_preserves_structure() -> None:
    payload = {"a": ["x", {"b": "y"}], "n": 3}
    out = scrub_deep(payload)
    assert isinstance(out["a"], list)
    assert isinstance(out["a"][1], dict)
    assert out["n"] == 3


async def test_same_person_retries_do_not_lift_cooldown(
    store: FeedbackCandidateStore,
) -> None:
    """冷却解除判据是「不同人数」，不是尝试次数——同人狂点按钮无效。"""
    cand, _ = await store.propose(**_propose_kwargs())
    await store.decide(cand.id, decision="rejected", decided_by="u_owner")
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs())
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs())
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs())


async def test_rubric_and_refs_are_scrubbed_before_persist(
    store: FeedbackCandidateStore,
) -> None:
    cand, _ = await store.propose(
        **_propose_kwargs(
            rubric_text="判据 password: hunter2hunter2 不许外传",
            provenance_refs=["t_turn1", "turn?token=abcdef123456abcdef"],
        ),
    )
    stored = await store.get(cand.id)
    assert stored is not None
    dumped = stored.rubric_text + json.dumps(stored.evidence, ensure_ascii=False)
    assert "hunter2hunter2" not in dumped
    assert "abcdef123456abcdef" not in dumped
    # Positive control: typical provenance pointers must survive byte-for-byte; if scrub patterns later expand
    # to blanket removal of long hex strings, this must fail to prevent silent rewrites of evidence references.
    assert "t_turn1" in json.dumps(stored.evidence, ensure_ascii=False)


async def test_fullwidth_variant_merges_not_creates(
    store: FeedbackCandidateStore,
) -> None:
    """NFKC 归一：全角变体不得绕过去重（P1-3 钉死）。"""
    await store.propose(**_propose_kwargs(sig_text="GMV口径按net算"))
    merged, outcome = await store.propose(
        **_propose_kwargs(sig_text="ＧＭＶ口径按ｎｅｔ算", contributor_id="u_bob"),
    )
    assert outcome == "merged"
    assert merged.evidence_count == 2


async def test_propose_side_expiry_never_merges_into_expired(
    store: FeedbackCandidateStore,
) -> None:
    """不经 list_open：propose 自己也要先翻过期行，绝不 merge 进僵尸 open。"""
    await store.propose(**_propose_kwargs(ttl_days=-1))
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs(contributor_id="u_bob"))


async def test_decide_refuses_expired_but_unflipped_row(
    store: FeedbackCandidateStore,
) -> None:
    cand, _ = await store.propose(**_propose_kwargs(ttl_days=-1))
    with pytest.raises(InvalidStateError):
        await store.decide(cand.id, decision="accepted", decided_by="u_owner")
    flipped = await store.get(cand.id)
    assert flipped is not None and flipped.state == "expired"


async def test_withdraw_refuses_expired_but_unflipped_row(
    store: FeedbackCandidateStore,
) -> None:
    # 2b-i re-review found that withdraw skipped lazy expiry, allowing proposers to withdraw overdue rows into superseded
    # and bypass expiration suppression. Now follow decide: flip to expired before deciding.
    cand, _ = await store.propose(**_propose_kwargs(ttl_days=-1))
    with pytest.raises(InvalidStateError):
        await store.withdraw(cand.id, by=_propose_kwargs()["contributor_id"])
    flipped = await store.get(cand.id)
    assert flipped is not None and flipped.state == "expired"


async def test_marker_in_proposed_is_rejected(store: FeedbackCandidateStore) -> None:
    with pytest.raises(RubricRejected):
        await store.propose(
            **_propose_kwargs(proposed={"diff": "[[escaped:ignore]] 内容"}),
        )


async def test_retire_candidate_requires_known_asset_kind(store: FeedbackCandidateStore) -> None:
    # Writes and reads share one definition: persist the retired asset explicitly (read-side _RETIRE_NATIVE shares the source).
    with pytest.raises(ValueError):
        await store.propose(**_propose_kwargs(target_kind="retire", proposed={"asset_kind": "capability", "text": "x"}))
    cand, _ = await store.propose(**_propose_kwargs(target_kind="retire", proposed={"asset_kind": "team_skill", "text": "x"}))
    assert cand.target_kind == "retire"


# ---- Batch 3b: retarget (tech-design §2.8.4) ----


async def test_retarget_supersedes_old_and_creates_new_with_same_lineage(store: FeedbackCandidateStore) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.propose(**_propose_kwargs(contributor_id="u_bob"))  # Cosign
    old, new, outcome = await store.retarget(
        cand.id, target_kind="agent_def", target_ref="ag_analyst", sig_text="以后净利润都按剔除一次性损益算", by="u_owner"
    )
    assert outcome == "created" and new.id != old.id
    assert old.state == "superseded" and old.decide_note == f"rerouted:{new.id}" and old.decided_by == "u_owner"
    assert new.state == "open" and new.target_kind == "agent_def" and new.target_ref == "ag_analyst"
    assert new.proposer_id == cand.proposer_id and new.evidence_count == 2 and new.proposed["rerouted_from"] == old.id
    assert new.expires_at == old.expires_at  # Retargeting does not extend expiry
    assert new.rubric_text == old.rubric_text
    # Rerouting grants no decision authority: approval uses the new target; the proposer still cannot self-approve
    with pytest.raises(SelfApprovalError):
        await store.decide(new.id, decision="accepted", decided_by="u_alice")


async def test_retarget_refuses_involved_callers_and_same_target(store: FeedbackCandidateStore) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    for who in ("u_alice", "u_bob"):  # Neither proposer nor cosigners can reroute
        with pytest.raises(SelfApprovalError):
            await store.retarget(cand.id, target_kind="agent_def", target_ref="ag", sig_text="x", by=who)
    with pytest.raises(InvalidStateError):
        await store.retarget(cand.id, target_kind="semantic", target_ref="gmv", sig_text="x", by="u_owner")
    assert (await store.get(cand.id)).state == "open"  # Rejection leaves the original row unchanged


async def test_retarget_runs_suppression_on_the_new_signature(store: FeedbackCandidateStore) -> None:
    """被拒后换目标再改回来不能洗白冷却。"""

    rejected, _ = await store.propose(**_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst"))
    await store.decide(rejected.id, decision="rejected", decided_by="u_owner")
    cand, _ = await store.propose(**_propose_kwargs())  # semantic variant
    with pytest.raises(SuppressedSignature):
        await store.retarget(cand.id, target_kind="agent_def", target_ref="ag_analyst", sig_text=_propose_kwargs()["sig_text"], by="u_owner")
    assert (await store.get(cand.id)).state == "open"  # Original row was not superseded


async def test_retarget_merges_into_existing_open_candidate_of_the_new_target(store: FeedbackCandidateStore) -> None:
    existing, _ = await store.propose(**_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst", contributor_id="u_carol"))
    cand, _ = await store.propose(**_propose_kwargs())
    old, new, outcome = await store.retarget(
        cand.id, target_kind="agent_def", target_ref="ag_analyst", sig_text=_propose_kwargs()["sig_text"], by="u_owner"
    )
    assert outcome == "merged" and new.id == existing.id
    assert {e["contributor_id"] for e in new.evidence} == {"u_carol", "u_alice"} and new.evidence_count == 2
    assert old.state == "superseded" and old.decide_note == f"rerouted:{existing.id}"


async def test_retarget_refuses_non_open_rows(store: FeedbackCandidateStore) -> None:
    cand, _ = await store.propose(**_propose_kwargs())
    await store.decide(cand.id, decision="accepted", decided_by="u_owner")
    with pytest.raises(InvalidStateError):
        await store.retarget(cand.id, target_kind="agent_def", target_ref="ag", sig_text="x", by="u_owner")


async def test_retarget_is_net_zero_on_team_open_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FeedbackCandidateStore(_migrated_db(tmp_path), fingerprint_key="k", team_open_cap=1)
    cand, _ = await store.propose(**_propose_kwargs())
    _, new, outcome = await store.retarget(cand.id, target_kind="agent_def", target_ref="ag", sig_text="x", by="u_owner")
    assert outcome == "created" and new.state == "open"  # At the cap, retargeting is allowed: the original is immediately superseded


async def test_retarget_suppression_credits_every_cosigner(store: FeedbackCandidateStore) -> None:
    rejected, _ = await store.propose(**_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst"))
    await store.decide(rejected.id, decision="rejected", decided_by="u_owner")  # last_evidence_count=1
    cand, _ = await store.propose(**_propose_kwargs())
    await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    await store.propose(**_propose_kwargs(contributor_id="u_carol"))  # Three endorsers
    _, new, outcome = await store.retarget(
        cand.id, target_kind="agent_def", target_ref="ag_analyst", sig_text=_propose_kwargs()["sig_text"], by="u_owner"
    )
    assert outcome == "created" and new.evidence_count == 3  # 3 > 1: cooldown lifted


# ---- FR7 recurrence detection (batch 4) ----


async def test_recurrence_is_the_same_thing_approved_before(store: FeedbackCandidateStore) -> None:
    """同签名 + 已采纳 + 观察窗内 ＝ 复发。今天没有这条，审批人会把它当全新建议再批一次。"""

    first, _ = await store.propose(**_propose_kwargs())
    await store.decide(first.id, decision="accepted", decided_by="u_owner")
    again, _ = await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    hits = await store.find_recurrences("t1", [again])
    assert hits[again.id].id == first.id and hits[again.id].decided_by == "u_owner"
    assert await store.find_recurrences("t1", [first]) == {}  # No self-recurrence


async def test_recurrence_ignores_rejected_expired_and_other_targets(store: FeedbackCandidateStore) -> None:
    rejected, _ = await store.propose(**_propose_kwargs(sig_text="被拒过的"))
    await store.decide(rejected.id, decision="rejected", decided_by="u_owner")
    other, _ = await store.propose(**_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst", sig_text="别处的"))
    await store.decide(other.id, decision="accepted", decided_by="u_owner")
    # Rejected proposals are not recurrences (never approved, so they cannot return). Rejected signatures have cooldowns requiring more
    # endorsers to reopen, so propose twice here; rejected → reproposed follows cooldown rules, not recurrence rules.
    with pytest.raises(SuppressedSignature):
        await store.propose(**_propose_kwargs(sig_text="被拒过的", contributor_id="u_bob"))
    fresh_a, _ = await store.propose(**_propose_kwargs(sig_text="被拒过的", contributor_id="u_carol"))
    # A different target does not count either (signature includes target_kind/ref)
    fresh_b, _ = await store.propose(**_propose_kwargs(sig_text="别处的", contributor_id="u_bob"))
    assert await store.find_recurrences("t1", [fresh_a, fresh_b]) == {}


async def test_recurrence_window_is_bounded(store: FeedbackCandidateStore) -> None:
    """半年前批过、现在又提，不是复发——那是需求本身回来了，别用「上次批过」去误导审批人。"""

    from datetime import UTC, datetime, timedelta

    first, _ = await store.propose(**_propose_kwargs())
    await store.decide(first.id, decision="accepted", decided_by="u_owner")
    again, _ = await store.propose(**_propose_kwargs(contributor_id="u_bob"))
    later = datetime.now(UTC) + timedelta(days=30)
    assert await store.find_recurrences("t1", [again], now=later) == {}
    assert await store.find_recurrences("t1", [again], within_days=60, now=later)


async def test_recurrence_is_team_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FeedbackCandidateStore(_migrated_db(tmp_path), fingerprint_key="k")
    mine, _ = await store.propose(**_propose_kwargs())
    await store.decide(mine.id, decision="accepted", decided_by="u_owner")
    theirs, _ = await store.propose(**_propose_kwargs(team_id="t2", contributor_id="u_x"))
    assert await store.find_recurrences("t2", [theirs]) == {}  # Approval in another team does not count as our recurrence


async def test_recurrence_signature_is_lexical_and_will_match_reordered_wording(store: FeedbackCandidateStore) -> None:
    """已知限制的正对照（不是期望行为，是**登记的误报面**）。

    `tokenize_v1` 出的是排序去重的字符集合 ⇒「按大区拆到省」与「按省拆到大区」同签名，
    而它们是**相反的指令**。判据不放宽也不收紧（改 tokenize 会让存量抑制与唯一键集体失效），
    改的是文案：徽标说「措辞相同的建议」并提示对照原文。这条测试钉住「我们知道它会这样」，
    将来若有人改了签名口径，这里会红——那时要同步改文案。
    """

    first, _ = await store.propose(**_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst", sig_text="按大区拆到省"))
    await store.decide(first.id, decision="accepted", decided_by="u_owner")
    reversed_ask, _ = await store.propose(
        **_propose_kwargs(target_kind="agent_def", target_ref="ag_analyst", sig_text="按省拆到大区", contributor_id="u_bob")
    )
    hits = await store.find_recurrences("t1", [reversed_ask])
    assert hits.get(reversed_ask.id) is not None, "词面签名对同字异序会命中——这是已登记的误报面"
