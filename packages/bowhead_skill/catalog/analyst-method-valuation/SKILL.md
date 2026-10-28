---
name: analyst-method-valuation
description: "估值法选择 + DCF假设纪律 (Valuation Method Selection & DCF Assumption Discipline): 按生意模式选估值法(P/E vs EV/EBITDA vs PS vs PB); 周期股低P/E陷阱(用normalized盈利); DCF敏感性(g<WACC必须满足); 交叉验证(相对vs绝对背离说明来源)。用于'估值法选择/倍数给不出来源/周期顶部P/E便宜陷阱/DCF永续增长假设/终值占比异常'。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Valuation and DCF"
title_zh: "估值方法与 DCF"
category: analysis
---

# Method · 估值法选择 + DCF 假设纪律

**何时用**: 任何看到"这家公司值多少钱"且要判断"用哪个估值法 / 倍数是否合理 / DCF假设是否自洽"的任务。financial-analyst 面对估值任务应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 估值有多种法门, 没有"正确"的估值法只有"适配生意模式"的估值法。本 method 强制 4 查 + 敏感性验证, 把"给个估值数字"拆穿成"你选的估值法为什么适配这家公司 / 假设的敏感性在哪 / 相对vs绝对数字为什么一致或背离"。

## 4 查协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

1. **估值法选择与匹配验证**: 按生意模式和生命周期阶段选法。盈利稳定(利润可信) → P/E; 重资产/周期 → EV/EBITDA 或 PB(剔资本结构+折旧政策); 亏损/高增长 → PS/PEG; 金融/地产 → PB/P/NAV。**给倍数必配可比公司或历史分位**, 不得单点无源。
2. **周期股陷阱检测**: 周期顶部 P/E 看起来便宜但是盈利顶部, 误导。量化"当前P/E vs 3-5年中值P/E"与"当前盈利 vs 周期中值盈利"背离, 用 normalized/mid-cycle 重新定价。
3. **DCF 假设纪律**: (a) **终值增长 g 必须 < WACC, 否则数学无效**(infinty); (b) WACC 范围 5-20%, 超出需解释; (c) 终值占 EV 比例 50-70%, 过高过低需解释; (d) 给敏感性区间不给单点(WACC 区间 ± 200bps, g 区间 ± 0.5-1.0%), 看极值与基准偏离幅度。**假设比结论重要**。
4. **交叉验证**: 相对估值(可比倍数) vs 绝对估值(DCF) 背离说明来源。若P/E给价100元但DCF给价80元, 不是数学错误是"市场给了溢价还是DCF假设保守了"。显式标出背离与调和逻辑。

## 计算走 python_sandbox(不靠心算)

拿到输入(WACC/终值增长/EV/terminal_value/business_type/可比倍数...)后, `skill_invoke("python_sandbox")` 跑 `scripts/validate_valuation.py` 模板(它读 INPUTS 里的参数, 输出倍数选择逻辑 / terminal_growth<wacc 验证 / 敏感性范围 / 相对vs绝对背离分析, 按阈值打 flag)。**比率与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带数字 + `evidence_source_ids`。
- risk_flags 例: `{"level":"high","scenario":"周期顶部: 当前P/E 8倍vs历史11倍, 但盈利同比+45%→normalized应用15倍, 溢价4倍","evidence":"art_xxx"}` 或 `{"level":"medium","scenario":"终值占EV 78%>70%上限 + g=3.2%接近WACC下界5.2% → 敏感性极高","evidence":"art_xxx"}`。
- 缺WACC/可比倍数历史分位数据 → flag "数据缺口", 相关结论标"无法验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: 估值法选择与理由 + DCF g<WACC 检查 + 敏感性上下限 + 结论。
- **SHOULD**(complexity≥0.7 / "详细"): 补周期股陷阱检测(当前vs中值P/E对比) + 相对vs绝对背离分析。
- **MAY**("深度/全套"): 多法交叉(P/E+EV/EBITDA+PS同时建模) + 历史分位分析 + 情景分析(bear/base/bull下的estimate倍数)。
