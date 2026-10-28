"""Planning Mode V2 flag (planning-mode-audit 结构修 sprint).

`BOWHEAD_PLANNING_V2` 只 gate **plan 执行路径**的新行为(P1 确定性闸 + P2 plan 侧
正门接线); P2-a 的 compose 块抽取改的是普通路, 不受本 flag 管(回滚=revert 该
commit)。planning 总闸仍是 `BOWHEAD_PLANNING_MODE`(gateway routes/planning_flags.py)。
默认 ON: V2 行为严格更诚实, 默认 OFF 只会延长假成功事故面; ship gate = 真栈
e2e dogfood(tech-design §2/Acceptance)。
"""

from __future__ import annotations

import os


def planning_v2_enabled() -> bool:
    return os.environ.get("BOWHEAD_PLANNING_V2", "1") == "1"


def planning_verify_enabled() -> bool:
    """graph-mode 批1 总闸: criteria 第二遍生成 + verify hook + 卡面渲染。

    默认 ON(V2 先例);OFF = planner prompt **字节级**不变(criteria 是独立第二遍
    调用, 出图 prompt 不含 criteria 语汇, 有字节 pin 测试守着)+ 行为回现状。
    ⚠ 非全链路字节级: _step_view/proposal 卡恒带 additive 空字段(success_criteria
    []/verify null), FE 容忍(实现 review B8 口径)。
    """
    return os.environ.get("BOWHEAD_PLANNING_VERIFY", "1") == "1"


def planning_midrun_enabled() -> bool:
    """graph-mode 批2: 执行中 HITL 即时化(出卡 hook + revive 拾取 + 收敛两态)。

    默认 ON;OFF = 无卡可产生 → resolve 的 plan_midrun 分支死路自然不触发,
    driver round 内核不在本 flag 内(行为等价重构, 独立回归钉死)。"""
    return os.environ.get("BOWHEAD_PLANNING_MIDRUN_HITL", "1") == "1"


def planning_replan_enabled() -> bool:
    """graph-mode 批3: replan 版本协议(revise/replan 两条 route + input_manifest
    写入与消费)。默认 ON;OFF = route 404 + manifest 不写不读。driver 别名与
    SUPERSEDED 语义不 gate——数据不存在时天然 no-op, gate 反而制造 off 态
    孤儿数据的行为分叉(batch3-replan-design D-H)。"""
    return os.environ.get("BOWHEAD_PLANNING_REPLAN", "1") == "1"


def planning_replan_max() -> int:
    """soft 护栏(不 409, 只出 warning): per-plan revise/replan 次数建议上限。"""
    try:
        return int(os.environ.get("BOWHEAD_PLANNING_REPLAN_MAX", "2"))
    except ValueError:
        return 2


def planning_provenance_enabled() -> bool:
    """graph-mode 批4: provenance 台账(溯源导出 route + source_refs 种子 hook)。

    默认 ON;OFF = route 404 + 草稿槽不种 refs。derive 传参/owner 编码/fallback
    sources 修复不 gate(additive 数据, off 态无行为分叉, batch4 D-E)。"""
    return os.environ.get("BOWHEAD_PLANNING_PROVENANCE", "1") == "1"
