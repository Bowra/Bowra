"""bi-hitl-cleanup P2.1 / B2.1 tests — peer prompt rewrite verification.

After B2 deleted ar2 internal preflight from bi_query, the old peer prompt
教学 block in `subagent.py:299-327` teaching how to react to
`error_kind="needs_clarification"` became dead doc — bi_query never produces
that shape anymore.

This test verifies the rewrite (per tech-design §P2.1):
- Old "bi_query 返 needs_clarification" header is gone
- bi_concept_resolve is the disambiguation entry point
- Peer prompt teaches `needs_user_input` bail kind (B7.1 enum) AND
  `domain_mismatch` (distinct from needs_user_input)
- Silent-drop anti-example preserved (still relevant)

⚠ 批 2（compute-agent-optimize，2026-08-20）逮到的事：这些断言原本 grep 的是
`subagent.py` **源码**，而那份 prompt 副本从 2026-07-03 起就不是 live（manifest 启动
即同名覆盖）——P2.1 的几条教学（`kind_hint=` 的调用范式、"silent-drop fake answer"
反例、"判断原则" 这个小标题）**只存在于死副本里，模型从没读到过**，测试却一直全绿：
典型的「为错误的理由绿」（feedback_gate_credibility）。副本随批 2 退役后，断言对象改成
**装配好的 live prompt**，措辞按 compute.md 的实况重锚——教学意图一条不少，但现在钉的是
模型真读得到的串。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SUBAGENT_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "subagent.py"


def _peer_prompt_source() -> str:
    """批 2（2026-08-20）起 compute 的 prompt 只有一份（`prompts/system/compute.md`），
    `subagent.py` 的手抄副本已退役。本文件断言的是「peer prompt 教了什么」——对象因此改成
    **装配好的 prompt**（live manifest 与 fallback 同源读它），比 grep .py 源码更贴近模型真读到的。
    """
    from bowhead_core.agents.prompts.simple_lookup_locale import render_lookup_template
    from bowhead_core.subagent import _build_compute_system_prompt

    return render_lookup_template(_build_compute_system_prompt())


def test_p21_old_needs_clarification_header_removed() -> None:
    """The old `## bi_query 返 needs_clarification — ar2 中间层...` header is
    gone. (Was `subagent.py:299` pre-B2.1.)"""
    src = _peer_prompt_source()
    assert "## bi_query 返 needs_clarification" not in src, "old needs_clarification teaching header still present — P2.1 incomplete"


def test_p21_no_more_dict_shape_reaction_teaching() -> None:
    """Old block taught reaction to `{"ok": false, "error_kind": "needs_clarification",
    "clarification": {...}}` shape. Since B2 砍 ar2, this shape never occurs.
    Peer prompt should NOT teach how to react to it (dead doc).
    """
    src = _peer_prompt_source()
    # The specific old teaching pattern about the literal dict shape
    assert '`{"ok": false, "error_kind": "needs_clarification", "clarification"' not in src


def test_p21_new_workflow_references_bi_concept_resolve() -> None:
    """Available dictionary resolution verifies ambiguous terms without guessing."""
    src = _peer_prompt_source()
    block = src.split("- **Ambiguous Datarc concepts**", 1)[1].split("\n---", 1)[0]
    instruction = block.splitlines()[0]
    assert "when available, call `bi_concept_resolve(tokens=[...])`" in instruction
    assert "with all ambiguous terms together" in instruction
    assert "looks up the dictionary without guessing" in instruction
    assert "If required terms cannot be verified with the available Tools or supplied evidence, report the gap" in instruction
    assert "next_action_hint" in block


def test_p21_teaches_needs_user_input_bail_kind() -> None:
    """两个 bail kind 必须在 **bail envelope 段**里同时教到并且区分开
    （`needs_user_input`＝ws 有数据但写法歧义，`domain_mismatch`＝ws 根本没这个字段）。
    作用域限定到该段——`domain_mismatch` 在别处也出现，全文 grep 会假绿。"""
    src = _peer_prompt_source()
    header = "## bail envelope"
    block_start = src.find(header)
    assert block_start >= 0, "bail envelope 段不见了"
    next_h2 = src.find("\n## ", block_start + len(header))
    block = src[block_start:next_h2] if next_h2 > 0 else src[block_start:]
    assert "needs_user_input" in block, "bail 段缺 needs_user_input（B7.1）"
    assert "domain_mismatch" in block, "bail 段缺 domain_mismatch"
    assert "clarification_payload" in block, "bail 段缺 clarification_payload forward 教学"


def test_p21_teaches_advisory_hint_not_directive() -> None:
    """`next_action_hint` 是建议不是指令（review CTO-P1-1）。

    ⚠ 批 2 逮到：原断言是 `"advisory" in src or "建议" in src`——`建议` 在 prompt 里到处都是
    （「建议第 1 步 skill_read」「行动建议」），把整段 hint 教学删掉它照样绿（正对照实测）。
    现在钉「hint 那句话本身」。
    """
    src = _peer_prompt_source()
    line = next((ln for ln in src.splitlines() if "next_action_hint" in ln and "建议不是指令" in ln), "")
    assert line, "prompt 没教「next_action_hint 是建议不是指令」——这条不变式又只活在别处了"


def test_p21_silent_drop_anti_example_preserved() -> None:
    """Unknown terms require scope inspection instead of accepting fallback rows."""
    src = _peer_prompt_source()
    assert "If a requested item cannot be delivered, name it and the reason" in src, "Missing requested items must be explained."
    block = src.split("- **🔴 每条 result 的 `_parse` 必看**", 1)[1].split("\n- `half_match_values`", 1)[0]
    assert "`unknown_words` 非空" in block
    assert "返回未过滤的数据" in block
    assert "按字面进了查询却一行都匹配不到" in block
    assert "先核对返回的维度值是不是用户问的范围" in block
    assert "只用数据里有的值重查，结论里写明口径" in block
    assert "把没按该词筛选的兜底数据当成结果" in block


def test_p21_general_instruction_over_case_list() -> None:
    """Teach ambiguity categories and verification of expanded members and metrics."""
    src = _peer_prompt_source()
    block = src.split("- **Ambiguous Datarc concepts**", 1)[1].split("\n---", 1)[0]
    assert "brands, shortened site names or aliases" in block.splitlines()[0]
    expansion = next(line for line in block.splitlines() if "词表里没有的概念" in line)
    assert "展开成成员值" in expansion
    assert "拆成数据里已有的指标" in expansion
    assert "传进 `bi_concept_resolve` 核对哪些真在数据里" in expansion
    assert "**只用核对过的去查**" in expansion
    assert "在结论里写明口径" in expansion
    assert "一个都没有就如实说本数据没有" in expansion


def test_p21_clarification_payload_shape_taught() -> None:
    """peer prompt 必须教 `clarification_payload` 的 3 字段形状（B7.2 路）。

    ⚠ 批 2 逮到：原断言 `"question"/"options"/"type" in src` 是全文 grep——把整段形状教学删掉
    仍绿（这三个词在 prompt 别处都有）。现在限定在 `clarification_payload` 那个 JSON 例子里。
    """
    src = _peer_prompt_source()
    anchor = src.find("clarification_payload")
    assert anchor >= 0, "prompt 没教 clarification_payload"
    block = src[anchor : anchor + 600]
    for field in ('"question"', '"options"', '"type"'):
        assert field in block, f"clarification_payload 形状缺 {field}（B5 三字段）"
