---
name: analyst-method-contract-risk
description: "合同/并购协议风险评分 (Contract Risk Assessment): 对赌主体错配/竞业限制要素缺失/或有负债承担方缺失/陈述与保证缺口/赔偿责任/MAC条款/交割条件. 用于 '并购尽调/合同审查/对赌风险/交割条件/或有负债/竞业限制'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Contract risk scoring"
title_zh: "合同风险评分"
category: analysis
---

# Method · 合同/并购协议风险穿透 (Contract Risk Assessment)

**何时用**: 任何涉及并购协议、对赌条款、竞业限制、或有负债或赔偿责任条款的风险评估任务。M&A analyst 收到合同文本应**默认**先跑这一关来穿透结构化风险。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 合同条款是否结构完整、权责分明、法律可执行。本 method 强制 8 项风险分类检查，把定性问题量化为缺口覆盖评分 + 严重性排序。

## 8 项风险分类 (MUST 全覆盖, 缺条款显式标缺口, 逐项判严重性)

1. **对赌主体与资本维持** (PE/创始人 buyback 风险)
   - 对赌条款对象是否清晰(定向赔偿对象/金额结算主体是谁)
   - 是否包含"现金补偿"而非"股权调整"(九民纪要：现金补偿受资本维持原则制约)
   - 补偿金是否有缴存、担保或第三方托管(Escrow)
   - **机制**: 资本维持原则限制补偿来源(净利润、未分配利润仅限, 不得触及股本/资本公积/盈余公积)。主体错配(赔偿人恒不能覆盖承诺)导致实现困难
   - **缓解**: 规范对赌条款的主体、金额、期限、担保机制

2. **竞业限制完整性** (员工/股东/管理层流失风险)
   - 地域范围是否明确(境内/特定城市/全球)
   - 限制期限(通常 1-3 年)
   - 竞争对手定义(同业企业、产品覆盖范围)
   - 对价/补偿措施(停薪留职、竞业补偿)
   - **机制**: 要素缺失 → 法院可判合同无效或缩小范围。主要损失是关键人才跳槽到竞争对手
   - **缓解**: 补完地域/期限/竞争对手定义 + 配备补偿

3. **或有负债/离表负债承担方未约定** (交割后债务风险)
   - 诉讼/仲裁/罚款在交割日前后如何分配
   - 环保/税务/劳动负债(未清缴的社保/欠税)谁承担
   - 是否有独立的 Escrow 账户/时间设置(通常 12-36 个月)
   - **机制**: 买方交割时接手隐形负债，卖方不再追偿。或Escrow不足导致无人承担
   - **缓解**: 明确分界线(交割日),Escrow机制,后续责任追偿期

4. **陈述与保证 (Representations & Warranties)** (合同基础信息虚假风险)
   - 财务报表准确性(资产/负债/收入的真实性)
   - 法律诉讼/合规(无重大隐瞒诉讼、环保罚款、知识产权侵权)
   - 主要客户/供应商维持
   - 员工与社保(无重大劳动纠纷)
   - **机制**: R&W缺失或不完整 → 交割后发现问题卖方可推卸责任
   - **缓解**: 穷尽式R&W清单，含时间范围和金钱责任限额

5. **赔偿责任(Indemnification)** (后续追偿困难)
   - 赔偿金上限(通常 10-30% of transaction value)
   - 赔偿时效(R&W breach的追偿期，交割后 12-24 个月常见)
   - 免赔额(Deductible, 如 500K 以下自我吸收)
   - 累计触发门槛(Basket, 如累计达 1M 才可赔偿)
   - **机制**: 缺失或不合理上限 → 买方损失无人负责
   - **缓解**: 明确赔偿上限、时效、免赔额、触发机制

6. **交割先决条件缺失** (Deal uncertainty)
   - 前置审批(反垄断/外资/行业)
   - 关键方许可/同意(大客户/供应商/监管)
   - 资产/许可证/合同转移(IP转移、经营许可是否可转)
   - **机制**: 条件未满足 → Deal无法closing或closing后失去关键资产
   - **缓解**: 穷尽列示并购前置条件，评估可实现概率

7. **MAC 条款** (Market/business condition failure)
   - 定义是否清晰(What constitutes "Material Adverse Change")
   - 是否有常见豁免(economic/industry downturn除外)
   - 触发金额阈值(如 >10% EBITDA损失)
   - **机制**: 条款不清晰 → 交割前生意变差时双方争议谁可退出
   - **缓解**: 明确MAC定义, 排除项, 数字阈值

8. **交割条件与补救机制** (Post-closing recourse)
   - 是否规定了post-closing调整(Working capital true-up, 常见)
   - 是否有质押/留置权机制保障卖方
   - 违反承诺时的补救(specific performance vs 赔偿金)
   - **机制**: 缺失 → 交割后问题无法追溯纠正
   - **缓解**: 约定post-closing true-up、质押、违约救济

## 计算走 python_sandbox (不靠心算)

拿到合同条款或交易协议后, `skill_invoke("python_sandbox")` 跑 `scripts/contract_risk_checks.py` 模板。输入: 8 个风险分类各自的"是否覆盖"(含二级子项), 脚本输出:

- 覆盖率(每个风险类 coverage %)
- 缺口列表(Gaps in category X, Y, Z)
- 每项风险的 severity(高/中/低)排序
- 综合风险评分 + verdict

**比率与严重性判定交给脚本, 确定且可审计。**

## 产出 (进 payload 的 risk_flags + insights)

- 每条 finding 带风险分类 + severity + 缺口描述 + 缓解建议。
- risk_flags 例: `{"level":"high","category":"对赌主体","mechanism":"现金补偿未约定担保机制","gap":"缺Escrow账户","mitigation":"补充Escrow条款,设立 12-24 月托管期"}`。
- 缺合同全文/关键条款 → flag "数据缺口", 相关结论标"无法完整评估"。
- **措辞**: 建议律师确认(不给法律定性), 分析师仅作风险穿透。

## 三档 (对齐 analyst-framework §0.5)

- **MUST**: 对赌主体/资本维持 + 竞业限制要素 + 或有负债承担 + 陈述保证存在性 四项关键检查 + 缺口显式列出 + verdict。
- **SHOULD** (complexity≥0.7 / "详细"): 补赔偿责任条款审查 + MAC条款清晰度评估 + 交割条件穷尽度 (三项)。
- **MAY** ("深度/全套"): 对标标准 M&A 文本模板(SAFE/YC models)的偏离度分析 + PMI内控与尽调脱节检查(卖方宣传 vs 合同R&W有无矛盾)。
