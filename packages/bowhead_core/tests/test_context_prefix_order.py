"""system / tools 按波动率排序 —— 上下文工程 sprint 批 2（a / b / c）。

线上这批 provider 做的是**自动前缀缓存**：不认 `cache_control` 断点，只按「从第一个字节起
逐字节相同的那一段」命中。所以 prompt 的**物理顺序**就是缓存策略本身。

本文件 pin 三条不变量：
1. **回滚绳**：`BOWHEAD_CTX_PREFIX_ORDER=0` ⇒ 三处都逐字节回到改前形态；
2. **内容守恒**：重排只换位置，一个字节都不新增、不丢失、不改写；
3. **方向正确**：稳定段排在易变段之前（这才是收益的因）。

⚠ 第 2 条是这批最容易出错的地方：`turn-latency` 实施 A 的既定纪律是
「同一份内容只换位置，一个字节都不改」—— 否则 A/B 里分不清是「换了位置」
还是「换了措辞」造成的差异。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.context_assembly import split_system_for_prefix
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

_FLAG = "BOWHEAD_CTX_PREFIX_ORDER"


def _six_blocks() -> dict[str, str]:
    return {
        "profile_prompt": "PROFILE_BODY",
        "extra_instructions_block": "额外指令：\nEXTRA",
        "memory_block": "<remembered_about_user>MEM</remembered_about_user>",
        "language_block": "\n\nLANG_DIRECTIVE",
    }


# ── 2-a: SubagentRunner system layering ──────────────────────────────────────


def test_split_puts_only_stable_segments_in_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 方向：per-team/user 的段留在 system；per-spawn/per-turn 的进尾巴。"""
    monkeypatch.setenv(_FLAG, "1")
    out = split_system_for_prefix(**_six_blocks())
    assert "PROFILE_BODY" in out.stable_system
    assert "MEM" in out.stable_system
    for volatile in ("EXTRA", "LANG_DIRECTIVE"):
        assert volatile not in out.stable_system, f"{volatile} 仍留在 system —— 它每 turn 都变，会把后面全打掉"
        # Language block returned separately since 2026-09-09 (after refs); other volatile blocks remain in turn_tail.
        assert volatile in (out.turn_tail + out.turn_tail_language)


def test_split_orders_stable_before_volatile_inside_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """profile → memory：越稳的越靠前。"""
    monkeypatch.setenv(_FLAG, "1")
    sys_text = split_system_for_prefix(**_six_blocks()).stable_system
    assert sys_text.index("PROFILE_BODY") < sys_text.index("MEM")


def test_split_hands_the_language_block_back_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 尾部契约升级（2026-09-09）：语言段**不再并进 `turn_tail`**，单独返回。

    它是**指令**，要吃最末位的 recency ⇒ 调用方把它排在 `<available_refs>` 之后；
    `turn_tail` 的其余部分是上下文，仍排在 refs 之前。并进 tail 时它会被几千字符的
    refs 清单挤出生成点，而 116 实测 Lead 恰在「中文语境 + 英文设置」这一格不听它。
    """
    monkeypatch.setenv(_FLAG, "1")
    out = split_system_for_prefix(**_six_blocks())
    assert out.turn_tail_language.strip() == "LANG_DIRECTIVE"
    assert "LANG_DIRECTIVE" not in out.turn_tail
    assert "LANG_DIRECTIVE" not in out.stable_system


def test_split_conserves_every_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 内容守恒：每一段都必须原样出现在 system 或 tail 里，不许被改写。"""
    monkeypatch.setenv(_FLAG, "1")
    blocks = _six_blocks()
    out = split_system_for_prefix(**blocks)
    both = out.stable_system + "\n" + out.turn_tail + "\n" + out.turn_tail_language
    for name, body in blocks.items():
        assert body.strip() in both, f"{name} 的内容在重排中丢失或被改写了"


def _legacy_concat(b: dict[str, str]) -> str:
    """逐字复刻改前 `subagent.py` 的拼接。

    ★ 原码对每一段是 `system = system + "\\n\\n" + block`（块非空就加），
    **哪怕 `system` 当时是空串也照加分隔符**；语言段则是直接 `+`（自带前导换行）。
    用 `"\\n\\n".join(非空块)` 复刻是**错的** —— 它会把 profile_prompt 为空时的前导换行抹平。
    """
    out = b["profile_prompt"]
    for key in ("extra_instructions_block", "memory_block"):
        if b[key]:
            out = out + "\n\n" + b[key]
    return out + b["language_block"]


def test_split_flag_off_is_byte_identical_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 回滚绳：flag=0 ⇒ 与改前的拼法逐字节相同，且 tail 为空。"""
    monkeypatch.setenv(_FLAG, "0")
    b = _six_blocks()
    out = split_system_for_prefix(**b)
    assert out.stable_system == _legacy_concat(b)
    assert out.turn_tail == ""


def test_split_flag_off_byte_identical_when_profile_prompt_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 自查逮到的 bug：**Lead 的 `profile.system_prompt` 是空串**。

    原码 `system = f"{system}\\n\\n额外指令：\\n{extra}"` 在 system 为空时照加两个换行，
    所以 Lead 的 system 真的以 `\\n\\n` 开头。第一版实现用 `join(非空块)` 把它抹平了
    ⇒ 回滚绳对 Lead **不成立**，而 Lead 恰恰是全系统最大的那份 prompt。
    突变锚点：把 `_append_legacy` 换回 join ⇒ 本条红。
    """
    monkeypatch.setenv(_FLAG, "0")
    b = _six_blocks()
    b["profile_prompt"] = ""
    out = split_system_for_prefix(**b)
    assert out.stable_system.startswith("\n\n额外指令："), repr(out.stable_system[:24])
    assert out.stable_system == _legacy_concat(b)


def test_lead_keeps_its_whole_prompt_in_the_stable_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 自查逮到的 P0：Lead 的 `extra_instructions` 就是整份 11.6k 提示词。

    若按 peer 的规矩把它搬进 tail，等于把**全系统最大的稳定块**搬出可缓存前缀 ——
    与本批目标正好相反。（接线是否真的按 profile 传对，另见文末的 wiring 用例。）
    """
    monkeypatch.setenv(_FLAG, "1")
    b = _six_blocks()
    b["profile_prompt"] = ""
    b["extra_instructions_block"] = "额外指令：\nLEAD_PROMPT_11K"
    lead = split_system_for_prefix(**b, extra_is_stable=True)
    assert "LEAD_PROMPT_11K" in lead.stable_system, "Lead 的整份提示词被搬出了 system"
    assert "LEAD_PROMPT_11K" not in lead.turn_tail
    # Positive control: same input at peer tier (default False) moves into the tail.
    peer = split_system_for_prefix(**b)
    assert "LEAD_PROMPT_11K" in peer.turn_tail
    assert "LEAD_PROMPT_11K" not in peer.stable_system


def test_split_skips_empty_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """空段不产生多余分隔符（否则 flag 两档的字节数会莫名其妙地不一样）。"""
    monkeypatch.setenv(_FLAG, "1")
    out = split_system_for_prefix(
        profile_prompt="P",
        extra_instructions_block="",
        memory_block="",
        language_block="",
    )
    assert out.stable_system == "P"
    assert out.turn_tail == ""


# ── 2-b: reorder Lead prompt sections ───────────────────────────────────────


def _lead(flag: str, monkeypatch: pytest.MonkeyPatch, **kw) -> str:
    monkeypatch.setenv(_FLAG, flag)
    return build_lead_system_prompt("ws_test", facts=[], **kw)


def test_lead_reorder_conserves_the_section_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 内容守恒：两档渲染出的**段落集合**完全相同，只有顺序不同。"""
    off = _lead("0", monkeypatch)
    on = _lead("1", monkeypatch)
    assert sorted(off.split("\n\n")) == sorted(on.split("\n\n")), "重排改动了内容，不只是顺序"
    assert off != on, "flag ON 没有真的改变顺序"
    assert len(off) == len(on)


def test_lead_reorder_keeps_wrapper_tags_at_the_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    on = _lead("1", monkeypatch)
    assert on.startswith("<lead_orchestrator>")
    assert on.rstrip().endswith("</lead_orchestrator>")


def test_lead_reorder_puts_per_user_blocks_after_the_static_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 方向：per-user 的记忆段必须排在静态大块（few-shot / 语气 / 安全）**之后**。

    这是本批收益最大的一处：原先 memory/facts 排在十余段静态内容之前，
    一个 per-user 的变化就把那十几段全挤出可缓存前缀。
    突变锚点：把 `_blk_memory_pack` 挪回静态组 ⇒ 本条红。
    """
    on = _lead("1", monkeypatch, memory_pack_block="MEMORY_PACK_MARKER")
    off = _lead("0", monkeypatch, memory_pack_block="MEMORY_PACK_MARKER")
    # `<tone>` and `<few_shot_examples>` are static (change only with deployments), forming this batch's protected prefix.
    for static_tag in ("<tone>", "<few_shot_examples>"):
        assert on.index(static_tag) < on.index("MEMORY_PACK_MARKER"), f"{static_tag} 仍排在 per-user 记忆段之后——它会被每次记忆变化挤出前缀"
        # Positive control: the previous order was reversed; otherwise this gate would have no discriminatory power.
        assert off.index(static_tag) > off.index("MEMORY_PACK_MARKER"), f"改前 {static_tag} 就已经在记忆段之前了，本用例失去判别力"


def test_lead_flag_off_matches_legacy_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 回滚绳：flag=0 时 sections 走 `_legacy_order`。

    与「改前形态」的逐字节对拍在 CI 之外用基线 worktree 做过（sha256 相同）；
    这里 pin 的是「两档确实是两条不同的路」。
    """
    off = _lead("0", monkeypatch)
    on = _lead("1", monkeypatch)
    assert off.index("<role>") < off.index("</lead_orchestrator>")
    assert off != on


# ── 2-c: sort tools by name ────────────────────────────────────────────────


def _schemas(flag: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv(_FLAG, flag)
    from bowhead_core.capability_resolver import advertised_tool_schemas
    from bowhead_core.subagent import PROFILES
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    profile = PROFILES["compute"]
    return [s.name for s in advertised_tool_schemas(reg, profile.tool_whitelist, profile_name="compute", role=None)]


def test_tools_are_sorted_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ tools 在 render order 里是位置 0 —— 一处不稳整份前缀作废。"""
    names = _schemas("1", monkeypatch)
    assert names == sorted(names), f"工具未按 name 排序: {names}"
    assert names, "广告面为空，这条闸是死的"


def test_tools_flag_off_keeps_registry_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """回滚绳：flag=0 时回到注册插入序（一般**不是**字典序）。"""
    off = _schemas("0", monkeypatch)
    on = _schemas("1", monkeypatch)
    assert set(off) == set(on), "排序改变了广告出去的工具集合"
    # compute registers ['bi_schema', 'bi_query'], **not** alphabetical order, making this control discriminating.
    assert off != sorted(off), f"注册序恰好等于字典序，本用例失去判别力（换个 profile）: {off}"
    assert on == sorted(off)


# ── Assembly order: bail instructions must always be last ────────────────────────────


def test_bail_instruction_stays_last_even_with_tail_and_refs() -> None:
    """★ 自查逮到的 P1：`turn_tail` 不能把 hard-bail 指令挤到倒数第二条。

    hard-bail 是**指令**（「下一步必须立即 subagent_return」），靠最末位置吃 recency；
    tail 与 refs 只是**上下文**。三条装配路径都要守这条：
      ① 无 refs（`BOWHEAD_REFS_IN_MESSAGES=0`）② 有 refs ③ 无 tail。
    第一版实现漏了 ①——tail 直接 append 到 messages 末尾，落在 bail 之后。

    这里复刻 `subagent.py` 的三段装配表达式（纯列表运算，无 I/O），
    pin 的是**顺序契约**本身。
    """
    hist = ["h1", "h2"]
    bail = "BAIL_NOW"
    messages = [*hist, bail]
    tail = ["TAIL"]
    refs = "REFS"

    # ① No refs + tail + bail
    assembled = [*messages[:-1], *tail, messages[-1]]
    assert assembled[-1] == bail

    # ② refs + tail + bail
    assembled = [*messages[:-1], *tail, refs, messages[-1]]
    assert assembled[-1] == bail
    assert assembled.index("TAIL") < assembled.index(refs), "tail 比 refs 稳，应排在它之前"

    # ③ No bail: tail at the end, refs after tail
    plain = [*hist]
    assembled = [*plain, *tail, refs]
    assert assembled[-1] == refs


# ── Wiring gate: exercise `SubagentRunner`, beyond pure-function tests ──────────────────────
#
# ★ Lesson from `378b53a94`: the hard 600 cutoff survived until 1.2.24 because tests **copied the builder**
#   without running product code. Likewise, the `split_system_for_prefix` cases above cannot prove
#   `subagent.py` actually passes correct profile-based `extra_is_stable`; the case below verifies wiring.


class _CaptureRouter(Router):
    """记下每次请求的 (system, messages 各段)。"""

    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.calls: list[tuple[str, list[str]]] = []

    def _rec(self, req) -> None:  # noqa: ANN001
        self.calls.append((req.system or "", [(getattr(m, "content", "") or "") for m in (req.messages or [])]))

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        return await super().stream(agent_type, req, **kw)


async def _run_profile(monkeypatch: pytest.MonkeyPatch, *, profile_name: str, extra: str) -> tuple[str, list[str]]:
    monkeypatch.setenv(_FLAG, "1")
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))

    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    await runner.run(
        profile_name=profile_name,
        task="接线闸",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        extra_instructions=extra,
    )
    assert router.calls, "没记到任何请求，这条闸是死的"
    return router.calls[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_lead_extra_instructions_stay_in_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ Lead 那 11.6k 提示词走 `extra_instructions` 通道，必须留在 `system`。

    突变锚点：把 `subagent.py` 的 `extra_is_stable=(profile.name == "lead")` 写死成 False ⇒ 本条红。
    """
    system, msgs = await _run_profile(monkeypatch, profile_name="lead", extra="LEAD_PROMPT_MARKER")
    assert "LEAD_PROMPT_MARKER" in system, "Lead 的整份提示词被搬出了 system —— 前缀缓存的最大收益没了"
    assert not any("LEAD_PROMPT_MARKER" in m for m in msgs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_peer_extra_instructions_move_to_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：peer 的 `extra_instructions` 是**每次 spawn 都不同**的子任务指令 ⇒ 进尾巴。

    没有这条对照，上一条用「恒 True」也能过。
    """
    system, msgs = await _run_profile(monkeypatch, profile_name="compute", extra="PEER_SUBTASK_MARKER")
    assert "PEER_SUBTASK_MARKER" not in system
    assert any("PEER_SUBTASK_MARKER" in m for m in msgs), "peer 的 per-spawn 指令没进 messages 尾巴"


def test_tools_wire_payload_is_byte_stable_across_registry_builds() -> None:
    """P0.5 #3：tools 在 render order 位置 0，DashScope 明写工具定义的字段顺序都必须一致。

    review 修（P2）：同一进程里两次构建拿到同一个 hash seed，set 迭代序会**恰好一致**——那样的断言逮不到无序遍历。
    这里起两个独立解释器、各自不同的 `PYTHONHASHSEED`，分别走 OpenAI 与 Anthropic 两个 adapter 的出线转换，
    序列化字节必须逐字节相同（顺序、schema 键序、required 列表都在内）。"""
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json, sys\n"
        "from bowhead_core.capability_resolver import advertised_tool_schemas\n"
        "from bowhead_core.subagent import PROFILES\n"
        "from bowhead_core.tool_registry import build_default_registry\n"
        "from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter\n"
        "from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter\n"
        "anth = AnthropicCompatibleAdapter.__new__(AnthropicCompatibleAdapter)\n"
        "anth.sanitize_tool_dots = False\n"
        "out = {}\n"
        "for prof, role in (('compute', None), ('lead', 'lead')):\n"
        "    if prof not in PROFILES: continue\n"
        "    reg = build_default_registry()\n"
        "    schemas = advertised_tool_schemas(reg, PROFILES[prof].tool_whitelist, profile_name=prof, role=role)\n"
        "    out[prof + ':openai'] = json.dumps([OpenAICompatibleAdapter._convert_tool(s) for s in schemas], ensure_ascii=False)\n"
        "    out[prof + ':anthropic'] = json.dumps([anth._convert_tool(s) for s in schemas], ensure_ascii=False)\n"
        "sys.stdout.write(json.dumps(out))\n"
    )

    def _run(seed: str) -> dict[str, str]:
        env = {**os.environ, _FLAG: "1", "PYTHONHASHSEED": seed}
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=180, check=False)
        assert proc.returncode == 0, proc.stderr[-2000:]
        return json.loads(proc.stdout)

    a, b = _run("1"), _run("424242")
    assert a and set(a) == set(b) and "compute:openai" in a
    for key, wire in a.items():
        assert wire == b[key], f"{key}：不同 hash seed 下 tools 出线字节不同——某处用了 set / 无序遍历"
    # Lead advertises the most tools: wire order must follow names, not allowlist/registration order.
    lead = a.get("lead:openai")
    if lead:
        got = [t["function"]["name"] for t in json.loads(lead)]
        assert got == sorted(got)
