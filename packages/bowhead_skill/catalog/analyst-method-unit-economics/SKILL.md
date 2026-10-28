---
name: analyst-method-unit-economics
description: "单位经济穿透 (Unit Economics): LTV/CAC 健康度 —— 客户生命周期价值、获客成本、回本周期、毛利调整。用于 '健康线/渠道对比/获客质量/毛利口径/边际成本递增'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Unit economics analysis"
title_zh: "单位经济分析"
category: analysis
---

# Method · 单位经济穿透 (Unit Economics)

**何时用**: 任何看到获客成本(CAC)、客户价值(ARPU/LTV)要判断"生意赚不赚、健康吗、怎么配置渠道"的任务。business-analyst 看到营销/渠道数据应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 不是 GMV 大就算赚钱。本 method 强制 4 核算，把"营销热度"拆穿成"每块钱的真实回报"。

## 4 核心协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **LTV 计算 — 两条路**: (a) **简式** `LTV = 月ARPU × 平均生命周期月数`; (b) **精式** `LTV = 月ARPU × 购买频率 × 贡献周期`。选数据最全的路。
2. **CAC 与健康线**: `CAC = 营销投入 / 新用户数`。核心判定 `LTV / CAC >= 3` 为健康线。<1.5 为危险。
3. **回本周期 — 现金流约束**: `回本月数 = CAC / 月ARPU`。≤12 月良好; >24 月严重拖累现金流。
4. **毛利口径铁律 — 最常踩雷**: 若 ARPU 是营收口径, **必须按毛利率折算**。真实 `LTV(毛利口径) = LTV(营收) × 毛利率`。营收口径 LTV/CAC ≥3 ≈ 毛利口径 ≥1.5。**不调整就得出假结论**。

## 分渠道计算 + 推荐分配

按 channels: [{name, cac, arpu, lifetime(月), retention/cohort_衰减_rate, gross_margin}] 跑各渠道 LTV/CAC、回本、毛利调整，输出健康度排序 + 预算配置建议。

## 计算走 python_sandbox(不靠心算)

拿到渠道数据后, `skill_invoke("python_sandbox")` 跑 `scripts/unit_economics.py` 模板。脚本读 INPUTS 里的渠道列表, 输出每渠道 LTV / CAC / 回本月 / 毛利调整后 LTV / 健康度 flag, 并按 LTV/CAC 排序推荐预算分配。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条渠道 finding 带数字 + 健康度级别。
- risk_flags 例: `{"level":"high","scenario":"渠道 A: LTV/CAC=1.2, 毛利调整后 0.8, 实际亏损","evidence":"ch_aaa"}`.
- 缺生命周期/毛利数据 → flag "数据缺口", 结论标"无法精确验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: LTV + CAC + 毛利调整 + 健康度判定(≥3 vs <1.5) + 渠道排序。
- **SHOULD**(complexity≥0.7): 补回本周期分析 + edge cases(负毛利渠道/试错渠道)。
- **MAY**("深度"): cohort 衰减曲线追踪建议 + 边际 CAC 递增检查 + 预算敏感性(LTV/CAC 目标倒推可容错空间)。
