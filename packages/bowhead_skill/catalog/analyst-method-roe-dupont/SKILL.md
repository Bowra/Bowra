---
name: analyst-method-roe-dupont
description: "ROE 杜邦归因 + 价值创造判断：ROE=净利率×资产周转率×权益乘数 拆三步, 净利率再拆五步(经营vs杠杆), ROIC vs WACC铁律识别虚高ROE毁灭价值。用于 'ROE虚高/杠杆堆砌/价值创造/真实回报率/去杠杆冲击'。"
required_skills: [analyst-framework]
version: 1.1.0
read_only_entry:
  schema_version: 1
  entry_file: references/readonly-method.md
  required_files: []
  parameter_schema:
    type: object
    properties:
      company:
        type: string
        title: Company
        maxLength: 160
      period:
        type: string
        title: Reporting period
        maxLength: 160
    additionalProperties: false
  required_skill_refs: []
  requires_script: false
  requires_new_tools: false
  requires_new_connections: false
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "ROE and DuPont analysis"
title_zh: "ROE 杜邦归因"
category: analysis
---

# Method · ROE 杜邦归因与价值创造判断

**何时用**: 任何看到ROE数据且要判断"这ROE是真的吗 / 来自经营还是杠杆 / 能否持续"的任务。financial-analyst 看到股东回报率应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: ROE高不等于价值创造强。本 method 强制 4 层拆解, 把"账面ROE漂亮"识别为"杠杆/税务游戏"还是"真实经营能力"。

## 4层拆解协议(MUST 全跑, 缺数据显式标缺口, 不得装完整)

1. **三步杜邦 — ROE = 净利率 × 资产周转率 × 权益乘数**: `(NI/Rev) × (Rev/TA) × (TA/E)` 层级定位 (净利能力 / 资产效率 / 杠杆倍数)。验证恒等式: `ROE ≈ ROA × 权益乘数`。
2. **五步杜邦 — 净利率深度拆**: `(EBIT/Rev) × (税负担) × (利息负担) = EBT/EBIT × NI/EBT × EBIT/Rev`。**分离经营(EBIT率)vs杠杆(利息)/税(递延优惠不可持续)**。
3. **价值创造铁律 — ROIC vs WACC**: `NOPAT=EBIT×(1-税率)`, `ROIC=NOPAT/投入资本`, `EVA=NOPAT−投入资本×WACC`。**ROIC<WACC + 高权益乘数 = 毁灭价值的虚高ROE 必须点破**。**应计利息低/递延税高 = 杠杆成本虚低**。
4. **杠杆敏感性 — 利率/税率冲击**: 若WACC上升100bp或税率取消优惠, 权益乘数拔毛, ROE和EVA如何断尾。是否"脆性高ROE"。

## 计算走 python_sandbox(不靠心算)

拿到财务数据后, `skill_invoke("python_sandbox")` 跑 `scripts/roe_dupont.py` 模板(它读 INPUTS 里的财务标量, 输出三步+五步拆解 / ROIC / EVA / 价值创造verdict, 并按阈值打 flag)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例:`{"level":"high","scenario":"ROIC 8.2% << WACC 9.5% 毁灭价值, 权益乘数3.8x堆杠杆掩盖","evidence":"art_xxx"}`。
- 缺EBIT / 税率 / 权益资本 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 三步杜邦拆解 + 权益乘数>2.5警告 + ROE vs ROIC对标 + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补五步拆解(分离经营vs杠杆) + EVA计算 + 杠杆敏感性(±100bp WACC冲击)。
- **MAY**("深度/全套"): 递延税/应计利息细节追踪 + 多年ROE稳定性(杠杆/税优可持续性) + 同业对标ROIC。
