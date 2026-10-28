"""共用授权判定：五处调用点（执行闸、工具列表过滤、团队工具视图、最后一道资源闸、技能判定）都经过这里。

钉住的是判定本身，而不是各调用点怎么处理拒绝：
- 作者豁免只对团队技能、只对「不在允许名单」、只对作者本人；显式拒绝对作者照样生效；
- 依赖当前轮次的占位目标按上下文解析，解析不出或 id 为空的跳过；
- 需要改用团队键时，作者从那次查找里得到；
- 判定本身不写审计。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.grant_evaluation import grant_verdicts, iter_grant_verdicts


class _Store:
    """Records every question and answers from a fixed table; any audit write fails the test."""

    def __init__(self, answers: dict[tuple[str, str, str], dict]):
        self.answers = answers
        self.asked: list[tuple[str, str, str]] = []

    async def explain_decision(self, access_ctx, kind, resource_id, permission):
        self.asked.append((kind, resource_id, permission))
        return self.answers.get((kind, resource_id, permission), {"allowed": True, "reason": "allowed_default_no_allowlist"})

    async def record_runtime_denied(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("grant evaluation must not write audit rows")


NOT_LISTED = {"allowed": False, "reason": "not_in_allowlist"}
DENIED = {"allowed": False, "reason": "denied_by_grant"}
CTX = SimpleNamespace(user_id="u_author", workspace_id="ws_1", team_id="team_1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "kind", "created_by", "exempt"),
    [
        (NOT_LISTED, "team_skill", "u_author", True),
        (DENIED, "team_skill", "u_author", False),  # explicit deny still applies to the author
        (NOT_LISTED, "team_skill", "u_someone_else", False),
        (NOT_LISTED, "team_skill", None, False),
        (NOT_LISTED, "builtin_capability", "u_author", False),  # only the skill target itself
        (NOT_LISTED, "tool", "u_author", False),
    ],
)
async def test_author_exemption_is_narrow(answer, kind, created_by, exempt) -> None:
    store = _Store({(kind, "r1", "read"): answer})
    (verdict,) = await grant_verdicts(CTX, store, {"user_id": "u_author"}, [(kind, "r1", "read")], team_skill_created_by=created_by)
    assert verdict.allowed is False
    assert verdict.owner_exempt is exempt
    assert verdict.refused is (not exempt)
    assert verdict.reason == answer["reason"]


@pytest.mark.asyncio
async def test_placeholder_targets_resolve_against_the_turn_and_empty_ids_are_skipped() -> None:
    store = _Store({})
    verdicts = await grant_verdicts(
        CTX,
        store,
        {},
        [("__kb_current_workspace__", "", "read"), ("__kb_current_team__", "", "read"), ("tool", "", "use"), ("tool", "bi_query", "use")],
    )
    assert store.asked == [("kb_scope", "workspace:ws_1", "read"), ("kb_scope", "team:team_1", "read"), ("tool", "bi_query", "use")]
    assert [v.allowed for v in verdicts] == [True, True, True]

    no_space = SimpleNamespace(user_id="u", workspace_id="", team_id="")
    assert await grant_verdicts(no_space, _Store({}), {}, [("__kb_current_workspace__", "", "read"), ("__kb_current_team__", "", "read")]) == []


@pytest.mark.asyncio
async def test_rekeying_learns_the_author_from_the_lookup(monkeypatch) -> None:
    async def fake_resolve(ctx, name, *, permission):
        return ("team_skill", f"tsk_{name}", permission), "u_author"

    from bowhead_core import resource_access

    monkeypatch.setattr(resource_access, "resolve_skill_grant_target", fake_resolve)
    store = _Store({("team_skill", "tsk_pricing", "read"): NOT_LISTED})
    (verdict,) = await grant_verdicts(CTX, store, {}, [("skill", "pricing", "read")], rekey_skill_targets=True)
    assert (verdict.resource_kind, verdict.resource_id) == ("team_skill", "tsk_pricing")
    assert verdict.owner_exempt is True

    # Without re-keying the name target is asked as-is and nobody is exempt.
    store = _Store({("skill", "pricing", "read"): NOT_LISTED})
    (verdict,) = await grant_verdicts(CTX, store, {}, [("skill", "pricing", "read")])
    assert (verdict.resource_kind, verdict.owner_exempt) == ("skill", False)


@pytest.mark.asyncio
async def test_iteration_stops_asking_once_the_caller_stops() -> None:
    store = _Store({("tool", "a", "use"): DENIED})
    async for verdict in iter_grant_verdicts(CTX, store, {}, [("tool", "a", "use"), ("tool", "b", "use")]):
        assert not verdict.allowed
        break
    assert store.asked == [("tool", "a", "use")]
