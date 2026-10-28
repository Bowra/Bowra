---
name: analyst-method-funnel-growth
description: "漏斗增长拆解 (Funnel Growth Decomposition): 营收增长来自啥 — 访问→注册→激活→首单→复购各段转化率的短板诊断、新增用户vs流失拆分、ARPU拆价格×用量×产品mix。用于 '增长驱动/转化率优化/流失分析/mix shift/营收拆解'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Growth funnel analysis"
title_zh: "漏斗增长拆解"
category: analysis
---

# Method · 漏斗增长拆解 (Funnel Growth Decomposition)

**何时用**: 看到营收或用户增速，问"增长来自哪儿"时必跑。增长分析师看到 3 月 GMV↑15% 应**默认**先用本 method 拆成"用户数↑? ARPU↑?""用户数拆新客获取 vs 流失率"。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 营收 = 用户数 × ARPU。用户数 = 累积新增 − 流失。ARPU = 价格 × 用量 × 产品mix。本 method 强制拆解各层, 找增长真正瓶颈 + mix 影响。

## 4 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **漏斗段位转化率**: 访问→注册(访问转化率) / 注册→激活 / 激活→首单 / 首单→复购。找最短板环节。量化: stage_1_count / stage_0_count。
2. **用户数归因**: 用户数增速 = ? 拆成新增用户 vs 流失用户。公式: Δ用户数 = 新增 − (前期用户 × 流失率)。若只有"用户数"无新增/流失细节, 显式标缺口。
3. **ARPU 拆解**: ARPU = 营收 / 用户数。进一步拆: ARPU = 价格 × 用量 × mix(高价品占比)。识别价格↑ vs 用量↑ vs mix shift 贡献。
4. **mix shift 信号**: 同期高价产品占比变化, 或单价均值与销量均值反向(一升一降 = mix 变)。量化 Δ mix = (高价SKU销售额 / 总销售额)当期 − 同期。

## 计算走 python_sandbox(不靠心算)

拿到漏斗 + 财务数据后, `skill_invoke("python_sandbox")` 跑 `scripts/funnel_growth.py` 模板(它读 INPUTS 里的漏斗阶段、用户数、营收序列, 输出转化率 / 用户归因 / ARPU拆解, 并按阈值打 flag)。**转化率与底层驱动交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例: `{"level":"high","scenario":"首单转化率环比下降 15pp，激活用户流失加速，新增拉新放缓","evidence":"art_xxx"}`。
- 缺新增/流失/SKU mix 数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 漏斗各段转化率 + 最短板诊断 + 用户数增速 + ARPU 均值。
- **SHOULD**(complexity≥0.7 / "详细"): 用户归因(新增 vs 流失) + ARPU 拆价格/用量 + mix 环比。
- **MAY**("深度/全套"): A/B 测试统计严谨性(样本量/功效/margin of safety) + 边际营收分析(新用户 CAC vs LTV) + 多层漏斗回溯(渠道/区域/cohort 维度)。
