"""高置信个人域零点击直写路（B 线批 1a）。

五道闸每道一条用例 —— 它们**理由各不相同**，不是同一件事的五个说法，
所以不能合并成一条「happy path + 一条 refuse」。
"""

from __future__ import annotations

import pytest
from bowhead_core.memory_auto_capture import (
    ACTION_NEEDS_CONFIRMATION,
    ACTION_REFUSED,
    ACTION_SETTING_SUGGESTED,
    ACTION_SKIPPED,
    ACTION_WRITTEN,
    CAPTURE_SOURCE,
    SETTING_OUTPUT_LANGUAGE,
    _CaptureRateLimiter,
    capture_modification_request,
    reason_user_message,
)

from bowhead_core import UserMemoryStore

_INSTRUCTION = "记一下：以后排名类图表都用横向条形图"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")
    # ★ The rate limiter is a process-local singleton: without replacement, one test's consumed quota leaks into the next,
    # making tests depend on execution order (four existing tests failed immediately after adding limits).
    # Supply an effectively unlimited instance here; two dedicated tests cover rate limiting itself.
    monkeypatch.setattr(
        "bowhead_core.memory_auto_capture._DEFAULT_RATE_LIMITER",
        _CaptureRateLimiter(min_interval_seconds=0.0, daily_cap=10**6),
    )


async def _store(tmp_path):
    s = UserMemoryStore(tmp_path / "m.db")
    await s.initialize()
    return s


def _drafter(text: str):
    async def _d(_inp: str) -> str:
        return text

    return _d


@pytest.mark.asyncio
async def test_the_happy_path_writes_one_entry_at_low_confidence(tmp_path):
    """★ `feedback_confirmed` 拿 **low**，这是与 tech-design §1.2（写着 high）的**有意偏离**。

    §1.2 给 high 的理由是「避免抛 MemoryConfidenceConflictError 整事务回滚」——
    2026-09-08 收口 review 查实那个理由对这条路**不成立**（该异常只在 governance 抛，
    `commit_memory_write` 是返回 `blocked`）。而 high 的真实效果是反的，见下一条用例。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_WRITTEN, out
    [e] = [x for x in await store.list_for_user("u1") if x.key == "ranking_chart_type"]
    assert e.confidence == "low"
    assert e.source == CAPTURE_SOURCE
    assert e.created_source_turn_id == "t1", "provenance 必须落在 entry 侧"


@pytest.mark.asyncio
async def test_the_llm_cannot_authorize_itself(tmp_path):
    """★ 闸①：没有第一层确定性命中，判型再自信也不许免确认写。

    LLM 的产出不能自己给自己发通行证 —— 同一条纪律见 `commit_memory_write`
    「置信度由写入身份决定，调用方无法传入」。

    ★ 突变验证：删掉 `classify_user_message` 那道判断 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text="帮我看下本月各省的营业额",  # Ordinary question; keyword filter does not match.
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_SKIPPED
    assert out.reason == "trigger_none"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_tainted_input_is_never_written_without_confirmation(tmp_path):
    """★ 闸②：输入含工具轨迹/第三方文本 ⇒ **一律降级为确认卡**，服务端拒绝免确认路径。

    ★ 突变验证：删掉 taint 判断 ⇒ 本用例红（会变成 written）。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=True,
        user_paused=False,
    )
    assert out.action == ACTION_NEEDS_CONFIRMATION
    assert await store.list_for_user("u1") == [], "taint 输入一条都不许落库"


@pytest.mark.asyncio
async def test_a_draft_echoing_our_own_sanitizer_marker_is_refused(tmp_path):
    """★ 闸③：起草产物里出现**我们自己吐出去的**转义记号 = 模型把展示替换当成了原文。

    转义策略（而不是拦截）敢用的前提就是有这道回收闸；没有它，
    `[[escaped:...]]` 会被写进用户的记忆。

    ★ 突变验证：删掉 `echoes_escape_marker` 那道判断 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名图用横向条形图 [[escaped:ignore previous]]"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_REFUSED
    assert out.reason == "draft_echoed_sanitizer_marker"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_automatic_writes_still_go_through_the_write_gate(tmp_path):
    """★ 闸④：自动写入**不是绕闸的许可证**。四条判据一条不减。

    这里用密钥那条做正例（`*_key=<值>` 是判据 ④ 认的凭据形状）。

    ★ 突变验证：删掉 `gate_check` 那段 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="openai_api_key",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("sk-live-abcdefghijklmnopqrstuvwxyz123456"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_REFUSED
    assert out.reason == "contains_secret"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_saying_the_same_thing_twice_does_not_grow_a_second_row(tmp_path):
    """★ 闸⑤：判重与裁决表照走 —— 换个措辞再说一遍只 bump `last_confirmed_at`。

    这是 2026-09-04 在 116 生产上验到的那个形状（同 key 覆盖走同一行、entries 不涨），
    自动写入必须继承它，否则「零点击」会变成「零点击地长垃圾」。
    """
    store = await _store(tmp_path)
    kw = {
        "suggested_key": "ranking_chart_type",
        "user_id": "u1",
        "turn_id": "t1",
        "workspace_id": None,
        "user_memory_store": store,
        "input_is_tainted": False,
        "user_paused": False,
    }
    a = await capture_modification_request(user_text=_INSTRUCTION, drafter=_drafter("排名类图表默认用横向条形图"), **kw)
    b = await capture_modification_request(user_text=_INSTRUCTION, drafter=_drafter("排名类图表默认用横向条形图"), **kw)
    assert a.action == ACTION_WRITTEN and b.action == ACTION_WRITTEN
    rows = [x for x in await store.list_for_user("u1") if x.key == "ranking_chart_type"]
    assert len(rows) == 1, "同一条说两遍不该长出第二行"
    assert b.reason == "confirmed", "第二次应当走判重的 confirmed 分支"


@pytest.mark.asyncio
async def test_the_flag_off_path_does_nothing_at_all(tmp_path, monkeypatch):
    """★ 判据 6：闸关时一步都不走 —— 连起草（一次 LLM 调用）都不许发生。"""
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "0")
    store = await _store(tmp_path)
    called = {"n": 0}

    async def _counting(_inp: str) -> str:
        called["n"] += 1
        return "排名类图表默认用横向条形图"

    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_counting,
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_SKIPPED and out.reason == "flag_off"
    assert called["n"] == 0, "闸关时不该起草——那是一次真金白银的 LLM 调用"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_an_automatic_write_can_never_overwrite_what_the_user_wrote_by_hand(tmp_path):
    """★★ 这是把 `feedback_confirmed` 从 high 退回 low 的**全部理由**。

    自动写入是 LLM 起草、**LLM 起 key** 的。key 猜错撞上用户在记忆页手写的条目时：
    - 拿 high ⇒ `can_overwrite(high, high)` 为真 ⇒ 用户亲手写的话被替换掉，
      只在 `previous_value` 里留一版；
    - 拿 low ⇒ 被裁决表挡下 —— 那正是这张表存在的理由。

    ★ 突变验证：把 `feedback_confirmed` 加回 `_HIGH_CONFIDENCE_SOURCES` ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    # User first writes an entry manually on the memory page (user_explicit ⇒ high).
    await store.commit_memory_write(
        user_id="u1",
        kind="preference",
        key="ranking_chart_type",
        value="排名图我要竖向柱状图，这是我自己写的",
        source="user_explicit",
    )
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",  # Collision
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_REFUSED, "撞上用户手写的条目必须被挡下"
    assert out.reason == "blocked_by_stronger_entry"
    [e] = [x for x in await store.list_for_user("u1") if x.key == "ranking_chart_type"]
    assert "我自己写的" in e.value, "用户手写的内容一个字都不许被自动写入覆盖"


@pytest.mark.asyncio
async def test_blocked_is_never_reported_as_written(tmp_path):
    """★ 裁决表说 `blocked` 时**什么都没写**。报成 written 会让 1c 的 disclose
    对用户说「已按你的偏好…（可撤销）」，而那条记忆根本不存在、撤销也没有落点。

    ★ 突变验证：把 `if action == "blocked"` 那段删掉 ⇒ 本用例红
    （会拿到 ACTION_WRITTEN + entry_id=None —— 一个既说写了又指不出写在哪的回执）。
    """
    store = await _store(tmp_path)
    await store.commit_memory_write(user_id="u1", kind="preference", key="k1", value="用户手写", source="user_explicit")
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="k1",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("模型起草的另一条"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action != ACTION_WRITTEN
    assert out.stored_text is None, "没写成功就不该给出 stored_text"


@pytest.mark.asyncio
async def test_a_second_request_within_the_cooldown_does_not_reach_the_drafter(tmp_path):
    """★★ 起草档的成本护栏。turn 收尾是**全通道共享**路径（定时 routine、飞书的
    completed turn 同样触发），每个命中的 turn 就是一次 LLM 起草调用。
    旁边那条蒸馏路早有 60s 冷却 + 日上限 40，起草档一条都没接
    —— 翻开 flag 就是没有闸门地放水。

    ★ 断言「起草器没被调到」而不是「没写库」：钱花在起草那一步，
    只断言库里没多一行的话，闸放在起草**之后**照样绿。

    ★ 突变验证：把限流判断挪到 `drafter(...)` 之后 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    calls = {"n": 0}

    async def _counting(_inp: str) -> str:
        calls["n"] += 1
        return "排名类图表默认用横向条形图"

    kw = {
        "user_text": _INSTRUCTION,
        "suggested_key": "ranking_chart_type",
        "user_id": "u1",
        "turn_id": "t1",
        "workspace_id": None,
        "user_memory_store": store,
        "drafter": _counting,
        "input_is_tainted": False,
        "user_paused": False,
        "rate_limiter": _CaptureRateLimiter(min_interval_seconds=60.0, daily_cap=40),
    }
    first = await capture_modification_request(**kw)
    second = await capture_modification_request(**kw)

    assert first.action == ACTION_WRITTEN
    assert second.action == ACTION_SKIPPED and second.reason == "rate_limited"
    assert calls["n"] == 1, "第二次不该走到起草——那是一次真金白银的 LLM 调用"


@pytest.mark.asyncio
async def test_a_tainted_turn_now_costs_a_draft_and_therefore_a_slot(tmp_path):
    """★ taint 现在**会起草**（要给确认卡一句正文），所以它**该**占配额。

    ⚠ 这条用例原来断言的是反面（「taint 不占额度」）—— 那是**旧设计**：
    那时 taint 就地 return，不调 LLM。2026-09-09 用户拍板把 taint 从「拦下」
    改成「降级入候选池」之后，它要花一次起草调用，成本护栏就必须罩住它。
    旧断言不是变松了，是**前提没了**。
    """
    store = await _store(tmp_path)
    limiter = _CaptureRateLimiter(min_interval_seconds=60.0, daily_cap=40)
    kw = {
        "suggested_key": "ranking_chart_type",
        "user_id": "u1",
        "turn_id": "t1",
        "workspace_id": None,
        "user_memory_store": store,
        "drafter": _drafter("排名类图表默认用横向条形图"),
        "rate_limiter": limiter,
        "user_paused": False,
    }
    first = await capture_modification_request(user_text=_INSTRUCTION, input_is_tainted=True, **kw)
    second = await capture_modification_request(user_text=_INSTRUCTION, input_is_tainted=False, **kw)
    assert first.action == ACTION_NEEDS_CONFIRMATION
    assert second.action == ACTION_SKIPPED and second.reason == "rate_limited"


@pytest.mark.asyncio
async def test_a_tainted_turn_lands_in_the_candidate_pool_instead_of_evaporating(tmp_path):
    """★★★ taint 的处置是**降级**，不是丢掉。

    原来这里就地 `return ACTION_NEEDS_CONFIRMATION` —— 那只是个**标签**：
    没有任何东西去建那张卡，用户的诉求照样蒸发。而 taint 的判据是
    「本轮动过任何工具」，116 实测 **395/400 = 98.8%** 的轮次都满足
    ⇒ 这条路可达上限只有 1.2%，等于功能被关掉。

    现在入候选池：第三方文本永远到不了免确认路（原安全边界一字未松），
    用户真说过的话不会消失。

    ★ 突变验证：把 `candidate_store.enqueue_if_new_with_outcome(...)` 那段删掉 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    seen: list[dict] = []

    class _FakeCandidates:
        async def enqueue_if_new_with_outcome(self, **kw):
            seen.append(kw)
            return type("C", (), {"id": "cand_x"})(), "created"

    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=True,
        user_paused=False,
        candidate_store=_FakeCandidates(),
    )
    assert out.action == ACTION_NEEDS_CONFIRMATION
    assert out.entry_id == "cand_x", "得把候选 id 带回去，否则调用方没法指向那张卡"
    assert len(seen) == 1
    # ★ Must enqueue in the drafting-tier pool: approval routing, triage allowlist, and suppression isolation all depend on origin.
    assert seen[0]["origin"] == "feedback_draft"
    assert "横向条形图" in str(seen[0]["value"]), "候选里要有收窄过的正文，不是用户原话"
    # ⚠ No entry may persist directly; downgrade means asking first.
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_without_a_candidate_store_it_says_so_instead_of_silently_dropping(tmp_path):
    """★ 没有候选池时退回老行为，但**记 warning**：这是能力缺失不是正常路径。
    静默的话又会变成「判对了却不接」——本 sprint 已经因为它修过两次。"""
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("排名类图表默认用横向条形图"),
        input_is_tainted=True,
        user_paused=False,
    )
    assert out.action == ACTION_NEEDS_CONFIRMATION and out.entry_id is None


def test_the_draft_origin_constant_matches_the_storage_package():
    """★ `bowhead_core` 不依赖 `bowhead_memory`（反向依赖），所以来源标识
    在两个包里各写了一份字面量。这条守卫钉住两边同值——
    改一处不改另一处，候选会落错池子，而 approve 分流/triage 白名单/抑制隔离
    全是按 origin 判的。
    """
    from bowhead_core.memory_auto_capture import ORIGIN_FEEDBACK_DRAFT as CORE
    from bowhead_memory.candidates import ORIGIN_FEEDBACK_DRAFT as STORAGE

    assert CORE == STORAGE


def test_every_reason_the_module_can_emit_is_registered():
    """★★ 守卫：AST 扫描本模块里每一个 `CaptureOutcome(reason=...)`，
    漏登记当场红。

    拒因原本是**散在函数里的裸字符串**，谁都可以随手再加一个，
    而 UI 直接把码显示出来就是又一次「toast 里蹦错误码」——本 sprint 踩过两次，
    116 真机抓到过。靠「记得去四处注册」这种纪律守不住，所以让它成为一条测试。

    ★ 顺带钉住：标了「要给用户看」的每一条都必须有中英文案（缺了是 KeyError）。
    前端文案跟 1c 一起落，这条测试保证到时候不会漏。
    """
    import ast
    import inspect

    from bowhead_core import memory_auto_capture as mod

    tree = ast.parse(inspect.getsource(mod))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CaptureOutcome"):
            continue
        for kw in node.keywords:
            if kw.arg != "reason":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                emitted.add(kw.value.value)
            elif isinstance(kw.value, ast.JoinedStr):
                # f"trigger_{decision}": expand to all values in the trigger family.
                from bowhead_core.memory_capture_trigger import (
                    DECISION_LANGUAGE_SETTING,
                    DECISION_NONE,
                )

                emitted.update({f"trigger_{DECISION_NONE}", f"trigger_{DECISION_LANGUAGE_SETTING}"})

    assert emitted, "一个 reason 都没扫到——扫描器坏了，不是模块干净了"
    unregistered = emitted - set(mod.REASON_USER_FACING)
    assert not unregistered, f"这些拒因没登记：{sorted(unregistered)}"

    for reason, user_facing in mod.REASON_USER_FACING.items():
        for lang in ("zh-CN", "en-US"):
            msg = mod.reason_user_message(reason, lang=lang)
            if user_facing:
                assert msg, f"{reason} 标了要给用户看，但 {lang} 没文案"
            else:
                assert msg is None, f"{reason} 是内部拒因，不该有用户文案"


@pytest.mark.asyncio
async def test_a_language_request_is_routed_to_the_setting_instead_of_evaporating(tmp_path):
    """★★ 「以后都用英文回答」有确切落点——设置页的「回答语言」，只是不在记忆这边。

    此前这条走 `ACTION_SKIPPED` + 内部拒因，于是**用户说了一句话，什么都没发生**
    （2026-09-08 空跑报告 §六「语言族目前是死路」）。判对了却不接，比判错还难查：
    日志里它和「压根没命中」长得一模一样。

    ★ 三条断言各管一段：动作要能被调用方分辨（不能混进 skip）、
    要指出**是哪个设置**、要有话对用户说。

    ★ 突变验证：把语言分支删掉（回落到 `decision != DECISION_PERSISTENT` 那条）
    ⇒ 本用例红（拿回 ACTION_SKIPPED）。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text="以后都用英文回答我",
        suggested_key="answer_language",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("用英文回答"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_SETTING_SUGGESTED, "不能混进 skip——调用方就是靠这个分辨要不要引导"
    assert out.suggested_setting == SETTING_OUTPUT_LANGUAGE
    msg = reason_user_message(out.reason)
    # ★ The requirement to explain to users remains. Describe only memory-side facts; neither claim is allowed:
    # remembering it will not take effect — false: saying this in conversation makes Lead change the setting directly through config_propose;
    # (observed on 116 on 2026-09-14, zh-CN→en-US). Change it in Settings is also false: that page has no such control.
    assert msg, "语言诉求必须有话对用户说"
    assert not any(b in msg for b in ["不会生效", "照旧", "等于没办到", "would not take effect", "would not change anything"]), msg
    assert not any(b in msg for b in ["到设置里改", "去设置里改", "in Settings"]), msg
    assert await store.list_for_user("u1") == [], "改道不是写入——一条都不许落库"


def test_the_language_route_does_not_guess_which_language_the_user_wants():
    """★ 只给**设置项名**，不给目标值。

    从「以后都用英文」里抽出 `en-US` 是另一个判别器；猜错了就是**替用户改了
    他没要求的设置**，而用户根本不知道有人动过。UI 把人送到那个表单，值他自己选。

    ★ 突变验证：给 `CaptureOutcome` 加一个被填上的 `suggested_value` ⇒ 本用例红。
    """
    from bowhead_core.memory_auto_capture import CaptureOutcome

    assert not hasattr(CaptureOutcome(ACTION_SETTING_SUGGESTED), "suggested_value")


@pytest.mark.asyncio
async def test_a_user_who_turned_automatic_learning_off_is_never_written_to(tmp_path):
    """★★★ 用户关掉了自动学习，这条路就**一步都不走**。

    ⚠ 这个闸原本不存在（2026-09-09 review 逮到）：`PauseStateCache.is_paused`
    只有蒸馏那条路查，零点击直写路**完全没查** —— 用户明确关掉之后照样被自动写入，
    而且连问都不问（建议路至少还弹一张卡）。两个开关各管各的。

    ★ 断言「起草器没被调到」而不是只断「没写库」：暂停的语义是「你压根不该来」，
    连一次 LLM 调用都不该发生。只断库的话，闸放在起草之后照样绿。

    ★ 突变验证：把 `if user_paused` 那段删掉 ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    called = {"n": 0}

    async def _counting(_inp: str) -> str:
        called["n"] += 1
        return "排名类图表默认用横向条形图"

    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_counting,
        input_is_tainted=False,
        user_paused=True,
    )
    assert out.action == ACTION_SKIPPED and out.reason == "learning_paused"
    assert called["n"] == 0, "暂停时连起草都不该发生"
    assert await store.list_for_user("u1") == []


def test_the_two_server_computed_flags_have_no_defaults():
    """★ `input_is_tainted` 与 `user_paused` **都不给默认值**。

    给了默认值，新调用点在没想过这两件事的情况下会静默拿到
    「不 taint、没暂停」—— 而这正是本次 review 逮到的那个 bug 的成因
    （暂停这件事压根没人想过，所以也就没人传）。
    """
    import inspect

    sig = inspect.signature(capture_modification_request)
    for name in ("input_is_tainted", "user_paused"):
        assert sig.parameters[name].default is inspect.Parameter.empty, name


@pytest.mark.asyncio
async def test_the_drafter_can_say_this_is_a_one_off_and_the_user_hears_about_it(tmp_path):
    """★★ 第二层（语义）判据：关键词粗筛认得出「记住…」，认不出
    「…且不得改动其他章节内容」这种一次性句式 —— 116 队列里正堆着这类。

    ★ 两条断言各管一半：不许写库，**而且要有话对用户说**。
    判出来了却静默丢掉的话，用户明说了「记住」却什么都没发生，还不知道为什么。

    ★ 突变验证：把 `draft_says_not_lasting` 那道判断删掉 ⇒ 本用例红
    （SKIP 会被当成正文写进记忆——那正是「记忆奇奇怪怪」的一种）。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="rec_section_format",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("SKIP"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_SKIPPED and out.reason == "draft_says_not_lasting"
    assert await store.list_for_user("u1") == []
    assert reason_user_message(out.reason), "判了不记，就得说一句为什么"


@pytest.mark.asyncio
async def test_a_preference_that_merely_starts_with_skip_is_not_swallowed(tmp_path):
    """★★ 「Skip the executive summary for weekly reports」是一条**正当偏好**。

    旧判据是 `out.upper().startswith("SKIP")` ⇒ 它整条被静默吞掉，
    而生产语料里确实有英文条目（116 实测）。协议记号必须**自成一条**，
    不能是正文的一部分。

    ★ 突变验证：把全等判定换回 `startswith` ⇒ 本用例红。
    """
    store = await _store(tmp_path)
    out = await capture_modification_request(
        user_text=_INSTRUCTION,
        suggested_key="summary_section",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=store,
        drafter=_drafter("Skip the executive summary for weekly reports"),
        input_is_tainted=False,
        user_paused=False,
    )
    assert out.action == ACTION_WRITTEN, out
    [e] = [x for x in await store.list_for_user("u1") if x.key == "summary_section"]
    # value is a quoted JSON string; assert with in instead of startswith.
    assert "Skip the executive summary" in e.value


def test_the_turn_path_actually_passes_a_candidate_store():
    """★★ 接线守卫。能力加在 `capture_modification_request` 上、调用点不传，
    等于没加 —— 而它的失败形态是**静默的**（退回老行为，只报不写）。

    「测了函数没测接线」今天已经咬过一次（`count_memory_receipts`：
    把接线换成 `memories = 0`，纯函数用例照样绿）。这条用 AST 扫真实调用点。

    ★ 突变验证：把 `turn_runner` 里那个 `candidate_store=` 实参删掉 ⇒ 本用例红。
    """
    import ast
    import inspect

    from bowhead_core import turn_runner

    tree = ast.parse(inspect.getsource(turn_runner))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "capture_modification_request"]
    assert calls, "扫不到调用点——扫描器坏了，不是接线没了"
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        assert "candidate_store" in names, "turn 收尾必须传候选池，否则 taint 降级只是一句空话"
        # Also guard the two other required server-computed inputs; omitting them likewise silently takes the permissive path.
        assert "input_is_tainted" in names
        assert "user_paused" in names


# ─── Checklist ④: distinguish the three None outcomes when taint downgrade enqueues ─────────────────────────

#: ⚠ Must match the lasting-preference keyword family; otherwise capture_modification_request returns
#: trigger_none in layer one and never reaches the taint branch. The initial tests failed for exactly this reason:
#: 「以后」 and 「都」 are not adjacent in 「以后排名图都…」, so the keyword family does not recognize it.
_PERSISTENT_TEXT = "以后都用横向条形图画排名图"


def _assert_reaches_the_taint_branch(user_text: str) -> None:
    """前置条件：用例的输入必须能过第一层，否则断言测的是粗筛不是本分支。"""
    from bowhead_core.memory_capture_trigger import DECISION_PERSISTENT, classify_user_message

    decision, _hits = classify_user_message(user_text)
    assert decision == DECISION_PERSISTENT, f"用例输入到不了 taint 分支：{user_text!r} → {decision}"


async def _real_candidates(tmp_path):
    from bowhead_memory.candidates import MemoryCandidateStore

    c = MemoryCandidateStore(db_path=str(tmp_path / "cand.db"))
    await c.initialize()
    return c


async def _count_candidates(tmp_path) -> int:
    import aiosqlite

    async with (
        aiosqlite.connect(str(tmp_path / "cand.db")) as db,
        db.execute("SELECT COUNT(*) FROM memory_candidates WHERE status='pending'") as cur,
    ):
        return (await cur.fetchone())[0]


async def test_a_tainted_draft_the_write_gate_refuses_does_not_promise_a_card(tmp_path, monkeypatch):
    """★★ 清单 ④。`enqueue_if_new` 有三条 `return None`：判重命中、被抑制、写入闸拒。
    此前 taint 分支把三者都报成 `tainted_input`（= 「卡在那儿等你确认」）——
    写入闸拒的那一种**根本没有卡**。

    ★ 用**真**候选池，不用替身：替身会返回我想要的值，证明不了真实现真的这么回。
    ★ 突变验证：把分支里「其余一律 tainted_not_enqueued」改回返回 tainted_input ⇒ 本用例红。
    """
    from bowhead_core.memory_auto_capture import ACTION_SKIPPED, _CaptureRateLimiter
    from bowhead_memory.candidates import _write_gate_allows

    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")
    _assert_reaches_the_taint_branch("以后都不要给我出图")
    draft = "这一次先不要图"
    # Positive control: prove this draft actually fails the write gate in the current environment first;
    # otherwise failure to enqueue below could have another cause (gate off / changed criterion) while the test remains green.
    assert _write_gate_allows(kind="preference", key="chart_off", value=draft, turn_id="t1") is False

    out = await capture_modification_request(
        user_text="以后都不要给我出图",
        suggested_key="chart_off",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=await _store(tmp_path),
        drafter=_drafter(draft),
        input_is_tainted=True,
        user_paused=False,
        rate_limiter=_CaptureRateLimiter(),
        candidate_store=await _real_candidates(tmp_path),
    )
    assert out.reason == "tainted_not_enqueued", out
    assert out.action == ACTION_SKIPPED
    assert out.entry_id is None
    assert await _count_candidates(tmp_path) == 0


async def test_the_same_tainted_request_twice_points_at_the_card_that_already_exists(tmp_path, monkeypatch):
    """判重命中**仍然是** `tainted_input`：同一条内容已经在池里等确认，卡是真实存在的。
    负对照——没有它，上一条在「一律报 tainted_not_enqueued」的实现下照样绿。

    ⚠ 两次调用各给一个新限流器：默认限流器是进程级的，第二次会先死在 `rate_limited`，
    根本走不到判重。
    """
    from bowhead_core.memory_auto_capture import _CaptureRateLimiter
    from bowhead_memory.candidates import _write_gate_allows

    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")
    _assert_reaches_the_taint_branch(_PERSISTENT_TEXT)
    draft = "排名图用横向条形图"
    assert _write_gate_allows(kind="preference", key="ranking_chart_type", value=draft, turn_id="t1") is True
    store = await _store(tmp_path)
    cands = await _real_candidates(tmp_path)

    async def _once(turn_id):
        return await capture_modification_request(
            user_text=_PERSISTENT_TEXT,
            suggested_key="ranking_chart_type",
            user_id="u1",
            turn_id=turn_id,
            workspace_id=None,
            user_memory_store=store,
            drafter=_drafter(draft),
            input_is_tainted=True,
            user_paused=False,
            rate_limiter=_CaptureRateLimiter(),
            candidate_store=cands,
        )

    first = await _once("t1")
    second = await _once("t2")
    assert first.reason == "tainted_input" and first.entry_id
    assert second.reason == "tainted_input" and second.entry_id is None
    assert await _count_candidates(tmp_path) == 1


async def test_a_failed_enqueue_does_not_claim_a_card_either(tmp_path, monkeypatch):
    """入队本身炸了（库锁住 / 超限）⇒ 没有卡 ⇒ 不许报 tainted_input。"""
    from bowhead_core.memory_auto_capture import ACTION_SKIPPED, _CaptureRateLimiter

    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")
    _assert_reaches_the_taint_branch(_PERSISTENT_TEXT)

    class _Exploding:
        async def enqueue_if_new_with_outcome(self, **_kw):
            raise RuntimeError("database is locked")

    out = await capture_modification_request(
        user_text=_PERSISTENT_TEXT,
        suggested_key="ranking_chart_type",
        user_id="u1",
        turn_id="t1",
        workspace_id=None,
        user_memory_store=await _store(tmp_path),
        drafter=_drafter("排名图用横向条形图"),
        input_is_tainted=True,
        user_paused=False,
        rate_limiter=_CaptureRateLimiter(),
        candidate_store=_Exploding(),
    )
    assert out.action == ACTION_SKIPPED and out.reason == "write_failed"


def test_the_enqueue_outcome_literals_match_the_storage_package():
    """同 `ORIGIN_FEEDBACK_DRAFT` 那条：两个包各写一份字面量，改一处不改另一处，
    taint 分支会把「已入队」全部判成「没入队」（或反过来），而且一个测试都不红。"""
    from bowhead_core import memory_auto_capture as core
    from bowhead_memory import candidates as storage

    assert core.ENQUEUE_CREATED == storage.ENQUEUE_CREATED
    assert core.ENQUEUE_DUPLICATE_PENDING == storage.ENQUEUE_DUPLICATE_PENDING


# ─── Checklist ③: collision-matrix criteria and wiring ───────────────────────────────────────────


def test_an_outcome_that_left_a_card_counts_as_the_utterance_having_a_home():
    """★ 此前只有 WRITTEN 算「有着落」，taint 降级出的卡不算 ⇒ NL 意图会再发一张。
    ★ 突变验证：把判据改回 `== ACTION_WRITTEN` ⇒ 本用例红。"""
    from bowhead_core.memory_auto_capture import (
        ACTION_REFUSED,
        ACTION_SETTING_SUGGESTED,
        ACTION_SKIPPED,
        ACTION_WRITTEN,
        CaptureOutcome,
        outcome_gives_the_utterance_a_home,
    )

    assert outcome_gives_the_utterance_a_home(CaptureOutcome(ACTION_WRITTEN))
    assert outcome_gives_the_utterance_a_home(CaptureOutcome(ACTION_NEEDS_CONFIRMATION, reason="tainted_input"))
    # Negative controls: no memory and no card do not count.
    assert not outcome_gives_the_utterance_a_home(CaptureOutcome(ACTION_SKIPPED, reason="tainted_not_enqueued"))
    assert not outcome_gives_the_utterance_a_home(CaptureOutcome(ACTION_REFUSED, reason="rate_limited"))
    assert not outcome_gives_the_utterance_a_home(CaptureOutcome(ACTION_SETTING_SUGGESTED, reason="trigger_language_setting"))


def test_escalate_counts_only_when_a_team_candidate_actually_landed():
    """★ 「调了 feedback_escalate」≠「落库了」：目标不存在 / 同名歧义 / 配额都会被拒，
    那时用户的话没有着落，个人这条路不该让路。
    ★ 突变验证：把回执检查整个去掉（只认 tool_name）⇒ 「被拒的回执」那条红。
    ⚠ 单独去掉 `ok is True` 或单独去掉 `candidate_id` 都是**等价突变**：真实回执里二者同进同退
    （`_fail` 从不带 candidate_id），所以两个条件互为冗余，钉住的是「调了 ≠ 落库」这一整件事。"""
    import json

    from bowhead_core.memory_auto_capture import escalate_persisted
    from bowhead_core.types import ToolInvocation

    landed = {"ok": True, "candidate_id": "fbc_1", "outcome": "created"}
    assert escalate_persisted([ToolInvocation(tool_name="feedback_escalate", arguments={}, result=landed)])
    assert escalate_persisted([ToolInvocation(tool_name="feedback_escalate", arguments={}, result=json.dumps(landed))])
    merged = {"ok": True, "candidate_id": "fbc_1", "outcome": "merged"}
    assert escalate_persisted([ToolInvocation(tool_name="feedback_escalate", arguments={}, result=merged)])
    # Recognize it when wrapped in a child invocation too.
    parent = ToolInvocation(tool_name="subagent_spawn", arguments={})
    parent.nested.append(ToolInvocation(tool_name="feedback_escalate", arguments={}, result=landed))
    assert escalate_persisted([parent])
    # Negative control
    refused = {"ok": False, "error": "target_not_found", "message": "..."}
    assert not escalate_persisted([ToolInvocation(tool_name="feedback_escalate", arguments={}, result=refused)])
    assert not escalate_persisted([ToolInvocation(tool_name="remember", arguments={}, result=landed)])
    assert not escalate_persisted([ToolInvocation(tool_name="feedback_escalate", arguments={}, result=None)])
    assert not escalate_persisted([])


def test_the_turn_path_wires_the_collision_guards_for_real():
    """★★ 接线守卫。三件事都长在 turn_runner 的闭包 / 调用点上，纯函数用例看不见：
    ① 留住 `on_post_turn` 返回的 δ 任务并传给 NL 意图（否则 NL 意图照旧不等就读 ctx）；
    ② 本轮算出 `escalate_fired`，并且自动捕获回调真的读它；
    ③ `captured` 用判据函数赋值，而不是闭包里一行会被随手改回去的比较。

    ★ 突变验证：删掉 `delta_task=_delta_task` 实参 ⇒ 红；删掉回调里 escalate 的 if ⇒ 红。
    """
    import ast
    import inspect

    from bowhead_core import turn_runner

    tree = ast.parse(inspect.getsource(turn_runner))

    nl_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "maybe_capture_nl_intent_post_turn"]
    assert nl_calls, "扫不到 NL 意图调用点——扫描器坏了，不是接线没了"
    assert all("delta_task" in {kw.arg for kw in c.keywords} for c in nl_calls), "NL 意图必须拿到 δ 任务"

    kept = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_delta_task" for t in n.targets)
        and isinstance(n.value, ast.Await)
        and getattr(getattr(n.value.value, "func", None), "attr", "") == "on_post_turn"
    ]
    assert kept, "on_post_turn 返回的任务必须留住"

    def _subscript_key(node):
        return node.slice.value if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) else None

    assigned = {_subscript_key(t) for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets}
    assert "escalate_fired" in assigned
    read_in_if = {_subscript_key(sub) for n in ast.walk(tree) if isinstance(n, ast.If) for sub in ast.walk(n.test)}
    assert "escalate_fired" in read_in_if, "算出来了却没有任何 if 读它 = 判对了却不接"

    captured_from_helper = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(_subscript_key(t) == "captured" for t in n.targets)
        and getattr(getattr(n.value, "func", None), "id", "") == "outcome_gives_the_utterance_a_home"
    ]
    assert captured_from_helper, "captured 必须用判据函数赋值"


@pytest.mark.parametrize(
    ("reason", "german", "french"),
    [
        ("trigger_language_setting", "Antwortsprache", "langue des réponses"),
        ("draft_says_not_lasting", "dauerhaften Präferenz", "préférence durable"),
        ("rate_limited", "Wiederholen Sie", "Répétez-le"),
        ("tainted_input", "externe Inhalte", "contenu externe"),
        ("draft_too_long", "zu lang", "trop long"),
        ("kind_not_allowed", "persönlichen Gedächtnis", "mémoire personnelle"),
        ("draft_echoed_sanitizer_marker", "Aufbereiten", "préparation"),
        ("key_too_long", "Themenname", "nom du sujet"),
        ("blocked_by_stronger_entry", "Bearbeiten Sie", "Modifiez ce souvenir"),
    ],
)
def test_capture_receipts_use_native_german_and_french(reason, german, french):
    assert german in reason_user_message(reason, lang="de-DE")
    assert french in reason_user_message(reason, lang="fr-FR")
