---
name: analyst-method-customer-structure
description: "客户结构风险穿透 (Customer Structure): 客户集中度 / RFM 分层 / 盈利倒挂。用于'客户依赖/大客户贡献/长尾贡献/毛利倒挂/沉睡客户/客户流失/集中度风险'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Customer concentration risk"
title_zh: "客户结构风险"
category: analysis
---

# Method · 客户结构风险穿透 (Customer Structure)

**何时用**: 任何看到客户维度数据且要判断"这些收入靠几个客户/大客户利润健不健康/长尾活不活跃"的任务。financial-analyst 看到营收分布应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 营收集中 = 业绩脆弱。本 method 强制 4 查, 把"客户多" 拆穿成"是不是小数几个客户撑全局营收 + 大客户利润被折扣吃掉了"。

## 4 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **客户集中度 — Top-N 营收占比**: Top1 / Top5 / Top20 分别占多少? B2B 行业 Top5 > 50% = 高风险单点; 消费品 Top20 > 70% 也预警。量化 HHI(赫芬达尔指数, 若数据允许)。
2. **RFM 分层 — 8 象限客户分类**: 近期性(R: Recency≤3月 vs >3月) × 频率(F: 频次≥2次 vs <2次) × 金额(M: 贡献>中位数 vs ≤中位数) → 8 类(重要保持/重要唤醒/潜力开发/沉睡/衰退…)。输出各象限客户数 + 营收占比。
3. **盈利倒挂检测 — 大客户毛利 vs 长尾毛利**: 大客户(Top-N %)用毛利率 vs 非大客户长尾毛利率。大客户毛利 < 长尾毛利 = 倒挂(折扣陷阱, 规模掩盖不了低利率)。量化倒挂幅度。
4. **集中度趋势 + 分群 P&L**: (SHOULD) Top-N 占比环比/同比; (MAY) 重要客户群/长尾群各 ROE / 销管成本贡献率。

## 计算走 python_sandbox(不靠心算)

拿到客户维度数据后, `skill_invoke("python_sandbox")` 跑 `scripts/customer_checks.py` 模板(它读 INPUTS 里的客户序列[{revenue, margin, recency_days, frequency}], 输出集中度比率 / RFM 分布 / 倒挂检测, 并按阈值打 flag)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例: `{"level":"high","scenario":"Top5 占 72% + 大客户毛利率 8% vs 长尾 24%, 集中度+倒挂双重风险","evidence":"art_xxx"}`。
- 缺客户明细 / 毛利数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 集中度(Top1/Top5/Top20) + RFM 分层 + 倒挂检测 + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补集中度趋势(环比/同比) + 重要客户群 P&L。
- **MAY**("深度/全套"): 客户流失率追踪 + 新客获得成本 ROI + 客户生命周期价值(LTV)建议。
