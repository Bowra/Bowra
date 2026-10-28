"""Fallback compute profile 终态守卫（原 test_zeta_compute_profile.py）.

历史：ζ sprint 在硬编码 compute profile 上做过动态 Layer-1 skill metadata 注入，
但该 profile 启动即被 compute.yaml 覆盖、manifest 路径从不注入——机制已随
2026-07-03 清算删除（docs/skills-tools-ssot.md §5-G3）。本文件守终态不变式：

  1. fallback prompt 不再含任何动态 skill 目录块（"## Available Skills"）；
     skill 感知 = 起手段的文本 + skill_list 工具。
  2. 起手段 / 终止条件 / body 关键约束保留（批 2 起 fallback 逐字就是 compute.md）。
     ⚠ 批 3 分层重写把这些约束换了标题（`## 起手指引` → `## ② 起手…`、
     `## 🔴 终止条件` → `① 五条不可违反` 第 1 条）。断言因此**钉规则原话不钉标题**——
     标题是版面，改版面不该让守卫变红；规则消失才该红。
  3. 对 BOWHEAD_SKILL_PROGRESSIVE_LOAD 环境变量彻底不敏感（flag 已删）。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from bowhead_core.subagent import (
    PROFILES,
    _build_compute_system_prompt,
    get_compute_profile,
)


def test_fallback_prompt_has_no_dynamic_skill_catalog() -> None:
    """终态：动态 Layer-1 已删——prompt 里不许再出现机制产物的标题块."""
    get_compute_profile.cache_clear()
    prompt = _build_compute_system_prompt()
    assert "## Available Skills" not in prompt
    assert "## Skills usage" not in prompt
    # Skill awareness now lives in the opening text (assert the choice-as-needed semantics, not heading text).
    assert "起手" in prompt
    assert "按需自决" in prompt, "起手段被改成硬 mandate 了——chat-truth 明确软化过，别回潮"
    assert "skill_read" in prompt
    assert "skill_list" in prompt


def test_fallback_prompt_flag_insensitive() -> None:
    """BOWHEAD_SKILL_PROGRESSIVE_LOAD 已删除——设任何值都不改变 prompt."""
    get_compute_profile.cache_clear()
    baseline = _build_compute_system_prompt()
    for raw in ("true", "false", "1", "0"):
        with mock.patch.dict(
            os.environ,
            {"BOWHEAD_SKILL_PROGRESSIVE_LOAD": raw},
        ):
            assert _build_compute_system_prompt() == baseline
    os.environ.pop("BOWHEAD_SKILL_PROGRESSIVE_LOAD", None)


def test_fallback_prompt_keeps_contract_and_body() -> None:
    """终止条件 + body 关键约束保留（chat-truth 重写后的不变式）."""
    get_compute_profile.cache_clear()
    prompt = _build_compute_system_prompt()
    assert "最后一步必须" in prompt, "终止条件（最后一步必须 subagent_return）不见了"
    assert "subagent_return" in prompt
    assert "bi_query" in prompt
    assert "交付面你定" in prompt
    assert "INPUTS[key]['path']" in prompt, "python_sandbox 载入段（inputs 唯一取法）不见了"
    assert "AnalysisPayload" in prompt
    assert "G2 Reality Check" in prompt
    assert "G4.5 Derivation" in prompt
    # The old hard BOOT mandate removed by chat-truth must not return.
    assert "## 🔴 BOOT 铁律 (BEFORE ANY ANALYSIS, 不准跳)" not in prompt
    assert "第 1 个 tool call 必须是" not in prompt


def test_compute_preserves_source_selection_and_optional_tool_boundaries() -> None:
    prompt = _build_compute_system_prompt()
    assert "Use the source the user requested" in prompt
    assert "verified supplied source material" in prompt
    assert "absence of attachments does not select Datarc" in prompt
    assert "This section applies only to Datarc queries" in prompt
    assert "report the gap instead of guessing fields or inventing a call" in prompt
    assert "`python_sandbox` Skill is authorized" in prompt
    assert "use another authorized computation Tool or report the missing capability" in prompt
    assert "subagent_return(payload=...)" in prompt


def test_live_and_fallback_prompts_share_short_chat_contract() -> None:
    """Manifest live prompt 与 fallback 都必须教会模型短答不写 Canvas。"""
    live_prompt = (Path(__file__).parents[1] / "src" / "bowhead_core" / "agents" / "prompts" / "system" / "compute.md").read_text(encoding="utf-8")

    for prompt in (live_prompt, _build_compute_system_prompt()):
        # Delivery-surface choice (batch 5ⓐ): both prompts must teach that the agent decides the required delivery surface.
        assert "交付面" in prompt
        assert "delivery_surface" in prompt
        assert "你的决定" in prompt
        assert "Markdown" in prompt and "数据" in prompt


def test_fallback_whitelist_matches_manifest_allow() -> None:
    """review B2-R1：兜底 prompt 现在**逐字**是 compute.md，工具面就必须跟得上它。

    compute.md 把 `write_report` 定为报告轮的必经一步；批 2 之前兜底 whitelist 只有 8 个工具
    （没有 write_report/read_report/add_image/web_search/web_fetch），于是「prompt 教的
    动作在兜底路上无声失效」。whitelist 是过滤器不是注册器，缺集才是问题，多集不是。
    """
    from bowhead_core.agents.registry import AgentRegistry

    allow = set(AgentRegistry.bootstrap().get("compute").tools.allow)
    fallback = set(get_compute_profile().tool_whitelist)
    missing = sorted(allow - fallback - {"bi_fetch_rows"})  # bi_fetch_rows is hidden and belongs in neither set.
    assert not missing, f"兜底 profile 少了 manifest 允许的工具，prompt 教的动作会静默失效: {missing}"


def test_fallback_survives_unreadable_compute_md(monkeypatch) -> None:
    """review B2-R4：文件损坏（UnicodeDecodeError 属 ValueError）必须降级而不是崩。

    本函数在 **import 期**跑（模块级 PROFILES），漏兜就是 gateway/celery 起不来。
    """
    import bowhead_core.subagent as _sub

    class _BadPath:
        def read_text(self, encoding: str = "utf-8") -> str:  # noqa: ARG002
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(_sub, "_COMPUTE_MD_PATH", _BadPath())
    prompt = _sub._build_compute_system_prompt()
    assert "Required completion" in prompt and "subagent_return" in prompt
    assert "delivery_surface" in prompt
    assert "available Tools or verified supplied source material" in prompt


def test_fallback_prompt_carries_manifest_append_block() -> None:
    """★ review2-R6：manifest 的 `system.append`（叶子纪律 + PDF/Excel/Word 导出指令）不在
    compute.md 里——兜底路少了它，用户要导出时 peer 根本不知道有这条路。"""
    from bowhead_core.agents.registry import AgentRegistry

    manifest = AgentRegistry.bootstrap().get("compute")
    assert manifest.system is not None and manifest.system.append
    get_compute_profile.cache_clear()
    prompt = _build_compute_system_prompt()
    assert "canvas-xlsx" in prompt and "canvas-docx" in prompt, "导出指令没进兜底 prompt"
    assert "DO NOT call subagent.spawn" in prompt, "叶子纪律没进兜底 prompt"


def test_fallback_prompt_starts_with_identity_not_rules() -> None:
    """review B2-R5：身份先于规则——provenance 块不该出现在 prompt 开头。"""
    get_compute_profile.cache_clear()
    prompt = _build_compute_system_prompt()
    assert prompt.lstrip().startswith("# compute peer"), prompt[:80]


def test_removed_fallback_profiles_stay_absent() -> None:
    """已删除的 modeler 与已退役的 debate 都不能回到 fallback profiles."""

    assert "modeler" not in PROFILES
    assert "debate" not in PROFILES
