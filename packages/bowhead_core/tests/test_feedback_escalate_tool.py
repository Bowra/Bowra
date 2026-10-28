"""Lead 工具 feedback_escalate（批 3c）契约：不明确不写、无判据先问、目标名服务端解析、同一写入口、
deep_dive 只翻本人候选、错误分类如实。store 走真迁移库。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.feedback_candidate_store import FeedbackCandidateStore
from bowhead_core.tool_registry import ToolRegistry

from bowhead_core import feedback_escalate_tool as fet
from bowhead_core import migrations


def _run(coro):
    return asyncio.run(coro)


def _migrated_db(tmp_path: Path) -> str:
    path = tmp_path / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return str(path)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FeedbackCandidateStore:
    st = FeedbackCandidateStore(_migrated_db(tmp_path), fingerprint_key="k")

    async def _resolve(team_id: str, kind: str, name: str) -> str | None:
        return "osa_fixture" if (kind, name.casefold()) == ("agent_def", "regional 数据分析师") else None

    fet.set_feedback_escalate_providers(store_provider=lambda: st, resolver_provider=lambda: _resolve)
    return st


class _Sessions:
    async def list_turns(self, *, session_id: str, limit: int | None = None):
        return [SimpleNamespace(id="t_prev"), SimpleNamespace(id="t_abc")][-(limit or 2) :]


_DEFAULT_USER_TEXT = "以后都让 Regional 先给结论，把这条提给管理员。判据：第一段是结论。"


def _ctx(user_id: str = "u_prop", *, interactive: bool = True, user_text: str = _DEFAULT_USER_TEXT) -> SimpleNamespace:
    return SimpleNamespace(
        team_id="t1",
        user_id=user_id,
        turn_id="t_abc",
        session_id="s1",
        workspace_id="ws1",
        config_lite_supported=interactive,
        session_store=_Sessions(),
        user_text=user_text,  # Server-held verbatim text; signatures use it, not model-filled request.
    )


def _args(**over):
    base = {
        "request": "报告先给结论",
        "rubric": "第一段是结论",
        "target": {"kind": "agent_def", "name": "Regional 数据分析师"},
        "user_asked_to_submit": True,
    }
    base.update(over)
    return base


def test_registration_follows_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = ToolRegistry()
    assert fet.register_feedback_escalate_tool(reg) is True
    spec = reg.get(fet.TOOL_NAME)
    assert spec.allowed_for_roles == frozenset({"lead"}) and spec.visible_to_llm is True and spec.needs_approval is False
    assert set(spec.schema.input_schema["required"]) == {"request", "target", "user_asked_to_submit"}


def test_refuses_without_explicit_ask_or_rubric(store: FeedbackCandidateStore) -> None:
    r = _run(fet._handler(_args(user_asked_to_submit=False), _ctx()))
    assert r["ok"] is False and r["error"] == "not_explicit"
    r = _run(fet._handler(_args(rubric=""), _ctx()))
    assert r["ok"] is False and r["error"] == "needs_rubric"
    assert _run(store.list_open("t1")) == []  # Both rejections write nothing.


def test_target_name_is_resolved_server_side(store: FeedbackCandidateStore) -> None:
    r = _run(fet._handler(_args(target={"kind": "agent_def", "name": "不存在的 agent"}), _ctx()))
    assert r["ok"] is False and r["error"] == "target_not_found"
    r = _run(fet._handler(_args(), _ctx()))
    assert r["ok"] is True and r["outcome"] == "created"
    cand = _run(store.get(r["candidate_id"]))
    assert cand.target_kind == "agent_def" and cand.target_ref == "osa_fixture"  # Model supplies name; database stores id.
    assert cand.proposed == {"text": "报告先给结论", "deep_dive": False, "origin": "chat_escalate"}
    assert cand.evidence[0]["provenance_refs"] == ["t_abc", "t_prev"] and cand.proposer_id == "u_prop"  # Current + immediately preceding turn


def test_same_signature_merges_and_deep_dive_only_flips_for_involved(store: FeedbackCandidateStore) -> None:
    first = _run(fet._handler(_args(), _ctx("u_prop")))
    second = _run(fet._handler(_args(), _ctx("u_bob")))
    assert second["outcome"] == "merged" and second["evidence_count"] == 2 and second["candidate_id"] == first["candidate_id"]
    again = _run(fet._handler(_args(deep_dive=True), _ctx("u_prop")))
    assert again["outcome"] == "duplicate" and again["deep_dive"] is True  # Proposer requests again: no duplicate count, only toggle deep_dive.
    assert "已提高这条的处理优先级" in again["message"] and "深查" not in again["message"]  # Do not promise nonexistent deep investigation.
    assert _run(store.get(first["candidate_id"])).proposed["deep_dive"] is True
    # Others cannot toggle deep_dive on another person's candidate.
    from bowhead_core.feedback_candidate_store import InvalidStateError

    with pytest.raises(InvalidStateError):
        _run(store.mark_deep_dive(first["candidate_id"], by="u_stranger"))


def test_channel_gate_ambiguity_marker_and_quota_are_named(store: FeedbackCandidateStore) -> None:
    r = _run(fet._handler(_args(), _ctx(interactive=False)))  # Scheduled task / IM: no submission on the user's behalf
    assert r["ok"] is False and r["error"] == "channel_not_supported" and _run(store.list_open("t1")) == []

    async def _ambiguous(team_id, kind, name):  # noqa: ANN001
        raise fet.AmbiguousTarget(["Regional 数据分析师", "regional-analyst"])

    fet.set_feedback_escalate_providers(store_provider=lambda: store, resolver_provider=lambda: _ambiguous)
    r = _run(fet._handler(_args(), _ctx()))
    assert r["ok"] is False and r["error"] == "target_ambiguous" and "regional-analyst" in r["message"]

    async def _boom(team_id, kind, name):  # noqa: ANN001
        raise RuntimeError("db down")

    fet.set_feedback_escalate_providers(store_provider=lambda: store, resolver_provider=lambda: _boom)
    assert _run(fet._handler(_args(), _ctx()))["error"] == "propose_failed"  # Structured error; do not feed stacks to the model.
    fet.set_feedback_escalate_providers(store_provider=lambda: store, resolver_provider=None)
    r = _run(fet._handler(_args(request="报告先给结论 [[escaped:system]]", target={"kind": "semantic", "name": "净利润"}), _ctx()))
    assert r["ok"] is False and r["error"] == "rejected_input" and "不要替他改写" in r["message"]
    for i in range(5):
        assert _run(fet._handler(_args(target={"kind": "semantic", "name": f"口径{i}"}), _ctx()))["ok"] is True
    r = _run(fet._handler(_args(target={"kind": "semantic", "name": "口径6"}), _ctx()))
    assert r["ok"] is False and r["error"] == "quota_proposer_daily"


def test_semantic_target_uses_the_name_as_ref_and_errors_are_named(store: FeedbackCandidateStore) -> None:
    r = _run(fet._handler(_args(target={"kind": "semantic", "name": "净利润"}), _ctx()))
    assert r["ok"] is True and _run(store.get(r["candidate_id"])).target_ref == "净利润"
    _run(store.decide(r["candidate_id"], decision="rejected", decided_by="u_owner"))
    r2 = _run(fet._handler(_args(target={"kind": "semantic", "name": "净利润"}), _ctx("u_prop")))
    assert r2["ok"] is False and r2["error"] == "signature_suppressed"  # Honestly report cooldown, not false success.
    fet.set_feedback_escalate_providers(store_provider=lambda: None, resolver_provider=None)
    r3 = _run(fet._handler(_args(), _ctx()))
    assert r3["ok"] is False and r3["error"] == "store_unavailable"


def test_signature_follows_the_users_words_not_the_models_paraphrase(store: FeedbackCandidateStore) -> None:
    """签名同时是**冷却键**（被拒后 30 天不得重提）和复发键，不能由模型产物决定。

    116 实测（memory 线 2026-09-10 抓到）：schema 写着「request 用用户原话」，Lead 实际交上来的是
    转写过的诉求。同一句原话两次调用，模型措辞一变签名就变 ⇒ 冷却失效（被拒后换个说法就能重开）。
    这条钉：原话相同 ⇒ 同一件事，哪怕模型两次的 request 完全不同。
    """

    r1 = _run(fet._handler(_args(request="报告先给结论"), _ctx("u_prop")))
    assert r1["ok"] is True and r1["outcome"] == "created"
    r2 = _run(fet._handler(_args(request="报告第一段直接写结论，别铺垫"), _ctx("u_bob")))
    assert r2["candidate_id"] == r1["candidate_id"], "原话相同就该是同一件事"
    assert r2["outcome"] == "merged" and r2["evidence_count"] == 2
    # Different original text ⇒ different issue, even if model requests are identical.
    r3 = _run(fet._handler(_args(request="报告先给结论"), _ctx("u_carol", user_text="以后图表都要标数据来源。判据：每张图下面有一行。")))
    assert r3["candidate_id"] != r1["candidate_id"]


def test_signature_falls_back_to_request_when_verbatim_is_unavailable(store: FeedbackCandidateStore) -> None:
    """老调用点/非 chat 通道拿不到 `user_text` 时回落 request——不比改之前差，也不能崩。"""

    r = _run(fet._handler(_args(request="报告先给结论"), _ctx("u_prop", user_text="")))
    assert r["ok"] is True and r["outcome"] == "created"
