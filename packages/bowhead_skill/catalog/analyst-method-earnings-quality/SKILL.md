---
name: analyst-method-earnings-quality
description: "盈利质量穿透 (Quality of Earnings): 净利润是真的吗 — 净现比/应计背离/营运资本吞噬/非经常性剔除/毛利税率异动/关联交易资本化。用于 '利润含金量/纸面富贵/增收不增现/盈利质量/经常性利润/应收暴增'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Earnings quality"
title_zh: "盈利质量穿透"
category: analysis
---

# Method · 盈利质量穿透 (Quality of Earnings)

**何时用**: 任何看到净利润且要判断"这利润是真的吗 / 含金量 / 可持续吗"的任务。financial-analyst 看到利润数据应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 利润是会计观点, 现金是经济事实。本 method 强制 5 查, 把"账面好看"拆穿成"现金能不能兑现"。

## 5 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **应计/现金背离 — 净现比**: `OCF / 净利润`。≥1 健康; <1 含金量存疑; **<0.5 红色警报**(近半利润停在表上未变现)。
2. **营运资本吞噬**: 应收/存货**增速** vs 营收增速。应收增速 >> 营收增速 → 放宽信用 / 渠道压货 / 激进确认收入 三选一, 埋坏账雷。量化"新增营运资本占用 = Δ应收 + Δ存货 − Δ应付", 与净利润、OCF 对比。
3. **非经常性剔除**: 净利润里多少来自一次性(资产处置 / 政府补助 / 公允价值变动 / 汇兑)? 算 **经常性净利润** 判真实盈利能力。
4. **毛利率 / 税率异动**: 毛利率突升 → 成本资本化 / 收入提前确认嫌疑; 实际税率 << 名义 → 递延 / 优惠不可持续。
5. **关联交易 / 研发资本化**: 大额关联收入(非关联化美化)/ 研发费用资本化(同行费用化)= 调节信号。

## 计算走 python_sandbox(不靠心算)

拿到 BI 数据后, `skill_invoke("python_sandbox")` 跑 `scripts/qoe_checks.py` 模板(它读 INPUTS 里的财务序列, 输出净现比 / 应计比率 / 营运资本占用 / 经常性利润, 并按阈值打 flag)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例:`{"level":"high","scenario":"净现比0.42 + 应收↑78%远超营收↑14%, 利润未兑现为现金","evidence":"art_xxx"}`。
- 缺现金流 / 账龄数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 净现比 + 营运资本背离两查 + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补非经常性剔除 + 毛利税率异动。
- **MAY**("深度/全套"): 关联交易/资本化排查 + 期后回款率追踪建议。
