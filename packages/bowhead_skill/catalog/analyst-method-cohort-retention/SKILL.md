---
name: analyst-method-cohort-retention
description: "留存曲线与 cohort LTV 穿透 (Cohort Retention Analysis): 新用户为什么流失、高粘性是假象还是真实、SaaS 收入留存如何衰减。用于 '次月留存率/N 月留存/留存衰减/churn/流失原因/NDR/GRR/粘性质量/快进快出'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Retention and cohort LTV"
title_zh: "留存与 Cohort LTV"
category: analysis
---

# Method · 留存曲线与 Cohort LTV 穿透 (Cohort Retention Analysis)

**何时用**: 任何看到用户数 / 留存数 / 且要判断"这群用户粘不粘 / 留存衰减形态 / 收入真实留存"的任务。product-analyst 或 SaaS 业务团队看到 cohort 数据应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 留存率是用户粘性最真实的表现，衰减曲线形态决定了增长可持续性。本 method 强制 5 查，把"表面高粘性"拆穿成"快进快出 vs 高粘性"、"收入留存 vs 用户留存"、"多 cohort 对比"。

## 5 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **次月留存率 (Day 30 / M1 Retention)**: `新用户中 30 天后仍活跃 / 新用户总数`。≥40% 健康; 20-40% 中等; **<20% 红色警报**(快进快出)。
2. **N 月留存衰减曲线**: M1 / M2 / M3 / M6 / M12 逐月留存率。线性衰减 (负压) vs 指数衰减 vs 平台化 (高粘性)。量化衰减速率 `(M1-M12)/M1` 判断用户粘性形态。
3. **Cohort LTV (基于留存累加)**: 按月 ARPU (Average Revenue Per User) × 累计留存用户数，不取简单平均生命周期。LTV = Σ(ARPU_m × retained_pct_m)，明确区分"未来假设 vs 已实现留存"。
4. **NDR / GRR (仅 SaaS 收入留存)**: Net Dollar Retention = (起始 MRR + 升级 − 流失) / 起始 MRR。≥100% 表示收入留存良好; <100% 收入在萎缩。GRR (Gross) 不扣升级。
5. **多 Cohort 对比 + 留存差异归因**: 不同获取月份 cohort 的 M1 / M3 是否存在显著差异 → 获取质量恶化 / 季节性 / 产品版本迭代信号。

## 计算走 python_sandbox(不靠心算)

拿到 cohort 数据后, `skill_invoke("python_sandbox")` 跑 `scripts/cohort_retention.py` 模板(它读 INPUTS 里的 cohort 序列或行为事件, 输出次月留存 / N 月衰减曲线 / cohort LTV / NDR, 并按阈值打 flag)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例:`{"level":"high","scenario":"M1 留存 18% 快进快出, 同时 3 个 cohort 的 M1 都下降 10pp，产品版本恶化信号","evidence":"art_xxx"}`。
- 缺后续月份 / 用户归类数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 次月留存率 + 衰减形态判断(线性 vs 指数) + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补多 cohort 对比 + 留存差异解释。
- **MAY**("深度/全套"): SaaS NDR 回推 + 升级率、预测 LTV 与最低 unit economics。
