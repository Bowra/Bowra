---
name: analyst-method-solvency-cashflow
description: "偿债能力/流动性/现金流质量 (Solvency & Liquidity & Cashflow Quality): 杠杆/利息覆盖/流动资产能否兑现 — 流动比率/速动比率/净负债/EBITDA/利息保障倍数/经营现金流持续性/自由现金流缺口/短债长投困境。用于 '短期偿债/借钱续命/账面资产虚/现金枯竭/杠杆爆表/流动性风险'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Solvency and cash flow quality"
title_zh: "偿债与现金流质量"
category: analysis
---

# Method · 偿债能力穿透 (Solvency & Liquidity Distress)

**何时用**: 任何判断企业能否在短期内还债、是否陷入流动性困境、杠杆是否超限的任务。financial-analyst 看到债务 / 利息 / 营运现金流数据应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 资产负债表看起来有资产，现金流表才见真章。本 method 强制 5 查 + 复合困境判断，把"账面有钱"拆穿成"能不能还债 / 有没有现金"。

## 5 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **流动性两比**: (a) 流动比率 = 流动资产 / 流动负债，≥1.5 健康，<1.0 红色警报。(b) 速动比率 = (流动资产 − 存货) / 流动负债，≥1.0 健康，<0.7 风险。
2. **偿债杠杆三指标**: (a) 资产负债率 = 总负债 / 总资产，≤60% 健康，>70% 警戒。(b) 利息保障倍数 = EBIT / 利息，>3 健康，1.5–3 黄色，<1.5 **红色** (借钱都还不起利息)。(c) 净负债 / EBITDA，<2 健康，>4 **高杠杆** (融资困难)。
3. **现金流三段分析**: (a) 经营现金流(CFO) — 必须持续为正，若为负且持续，公司借钱续命。(b) 投资现金流(CFI) — 负常态(正常企业投资)，但若 CFI 突然大幅正(抛资产自救)→ 危险信号。(c) 筹资现金流(CFF) — 若正且持续，配合 CFO 负 = 借钱维持运营。
4. **自由现金流(FCF)缺口**: FCF = CFO − CapEx。FCF < 净利润 = 利润含金量存疑、难以分红。FCF 持续负 = 无法还债/投资/分红。
5. **困境复合判断** (任意多项命中 → P0 流动性风险): (a) CFO 持续为负; (b) 利息保障倍数 < 1.5; (c) 流动比率 < 1.0; (d) 速动比率 < 0.7; (e) 净负债/EBITDA > 4; (f) FCF < 0 且持续。

## 计算走 python_sandbox(不靠心算)

拿到财务数据后, `skill_invoke("python_sandbox")` 跑 `scripts/solvency_checks.py` 模板(它读 INPUTS 里的流动资产/负债/债务/现金流等标准项, 输出所有比率 / 困境复合标志 + 显式数据缺口)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例: `{"level":"high","scenario":"利息保障倍数0.82 + CFO连续3期负, 短期无法还息","evidence":"art_xxx"}`。
- 缺债务 / 现金流序列数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 流动比率 + 速动比率 + 利息保障倍数 + CFO 正负判断 + 综合困境flag。
- **SHOULD**(complexity≥0.7 / "详细"): 补资产负债率 + 净负债/EBITDA + FCF缺口分析 + CFO/CFI/CFF 三段演绎。
- **MAY**("深度/全套"): 期后融资成功率追踪 + 债务重组/展期可能性评估 + 同行杠杆中位数对标。
