---
name: analyst-method-data-compliance
description: "数据合规穿透 (Data Compliance Audit): PIPL/GDPR 逐条核查 — 敏感信息同意覆盖 / 最小化原则 / 跨境安全评估 / 自动化决策权 / 隐私影响评估。用于 '数据合规 / 监管风险 / PIPL条文 / GDPR合规 / 隐私审计'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Data compliance review"
title_zh: "数据合规审计"
category: analysis
---

# Method · 数据合规穿透 (Data Compliance Audit)

**何时用**: 涉及数据收集、处理、跨境、自动化决策等任何PIPL/GDPR监管场景下，需判断"这个数据处理合法吗 / 有什么合规缺口"的任务。数据privacy officer / 合规审计应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 数据处理必须逐条对标法规条文。本 method 强制 7 查, 把"感觉合规"拆穿成"法规逐条无违反"。

## 7 查协议(MUST 全跑, 缺证据显式标缺口, 不得跳过装完整)

1. **敏感数据无单独同意(PIPL §29 §28)**: 收集生物识别/宗教/性取向等敏感信息是否有明确单独同意? 捆绑同意 = 违规。
2. **信息最小化原则(§6)**: 收集的数据字段 vs 使用场景。超范围收集(收电话号只为验证，却存身份证) = 违规。
3. **SDK第三方提供无单独同意(§23)**: 内嵌SDK跟数据的关系。有无告知用户+征得同意?
4. **捆绑同意(§14 §16)**: "勾选即可注册"式一揽子同意，是否限制了用户自主选择权 → 违规。
5. **自动化决策无拒绝权(§24)**: 存在算法定价/推荐/放贷决策，用户是否知情且有权拒绝和申诉?
6. **未做PIA(§55)**: 高风险处理(大规模敏感数据/算法决策)是否做过隐私影响评估报告?
7. **跨境无SCC或评估(§38)**: 数据出境到个人信息保护不足地区(非PIPL等效)，是否有SCC/SCCs或安全评估?

## 计算走 python_sandbox(合规检查表计分)

拿到 compliance evidence (收集流程/SDK清单/同意模板/跨境列表) 后, `skill_invoke("python_sandbox")` 跑 `scripts/compliance_checks.py` 模板(它读 INPUTS 里的合规条件(booleans/enum), 输出违规项 + severity + 条文 + data_gaps, 并计覆盖度)。**条文映射与风险等级判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 violation 带真实PIPL条文号 + severity(critical/high/medium/low)。
- risk_flags 例:`{"level":"critical","article":"PIPL §28 §29","scenario":"生物识别数据未单独征得同意","evidence":"upload_consent_form_xxx"}`。
- 缺证据(无同意记录/无PIA报告) → flag "证据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 敏感信息同意 + 最小化原则两查 + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补SDK同意 + 自动化决策权 + 跨境评估。
- **MAY**("深度/全套"): 完整PIA评估 + 合规整改路径 + GDPR等效对标。
