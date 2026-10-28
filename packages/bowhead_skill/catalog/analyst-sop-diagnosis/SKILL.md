---
name: analyst-sop-diagnosis
description: "Diagnose why metric dropped — layered: confirm→main-dim attribution→time inflection→volume/price. Use for '为什么 X 跌 / why X dropped'."
required_skills: [analyst-framework]
version: 3.2.0
pipes_output_to: analyst-sop-output-contract
trigger_examples:
  - "为什么 GMV 下滑了"
  - "营业额为什么跌"
  - "上周利润为什么下降"
  - "活跃用户数出了什么问题"
  - "Why did conversion rate drop"
  - "芳草地门店营业额为啥下滑"
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Metric decline diagnosis"
title_zh: "指标下跌诊断"
category: analysis
---

# Analyst SOP · 指标诊断 v3

> 场景：用户明确指向某指标的**负向变化**，要找根因。属于 `analyst-framework` §模板 3.

## 前置

`skill_read("analyst-framework")` —— 心智模型 + scope 协商 + 三档契约.

**家底获取**：先调一次 `bi_schema()`（并发拿 measures + dimensions），同一任务调一次即可，结果留在对话里。

---

## MUST 层（任何诊断都必出）

### MUST-0 · 复杂 query 先探 schema (用户给公式 / 清单 / 派生指标)

如果用户 query 含 **公式 / 多字段加总 / 派生指标** (e.g. "周转率 = COGS/平均存货", "19 项成本明细", "ROI 增长率"):

- **第 1 步必须先 `bi_schema()`** — 不要直接 bi_query 拉数
- 用户的 derived metric 大概率 BI 没有现成列, 需要从 raw 字段 derive
- 看 list 后判: 用户公式拆出来的 raw 字段是否能 fuzzy match BI 真字段 (G3.1)
- 见 framework §0.45 G4.5 Derivation 优先

### MUST-1 · 确认下滑事实

```
bi_query(queries=["<metric> <time> 同比"])
```

- 默认 time = `今年 vs 去年`；用户给了就用用户的
- 若 |delta| < 2%（单位按百分点 / 百分比自判）→ 告诉用户"未达显著阈值"，**不要**编因果
- 若 bi_query 返回 `_no_data`、要的指标 schema 里没有现成的 → 不要立刻 bail, 先判断能否由已有字段算出, 能就切 G4.5 Derivation 路径

### MUST-2 · 主维度归因（1 条）

**首选路径** (透明 + 用户公式可对齐):

```
bi_query(queries=["<time> <metric> 按<主维度>"])  # 当期数据
bi_query(queries=["<上期time> <metric> 按<主维度>"])  # 上期数据
→ skill_invoke(python_sandbox)  # 算 delta 和 contribution_share
→ add_chart(data_artifact_id=<merge 后>, hint="rank",
                   title="<metric>同比归因 Top 10 贡献")
```

**Fallback 路径** (用户没给公式 + budget 紧):

```
bi_query(queries=["<metric> <time> 按<主维度> 同比"])   # datarc 原生归因，一条返分维度 delta
```

- 主路径优先: 用户给了**公式 / 清单 / 拆解维度**时 — 必须 bi_query 拉明细 + python (analyst-framework §0.45)
- Fallback 用 attribute: 用户没明示, 默认菜单也想拿 top-N 贡献 → attribute 黑盒一把梭
- 挑 top-1 贡献下滑的 `dim=value`（形如 "品牌=A 贡献 60% 下滑"）
- 任一路径返空 rows → 明说"无显著维度差异"，跳到 MUST-3 做时间归因

### MUST-3 · 时间拐点（1 条）

```
bi_query(queries=["<time> <主维度=top-1 属性> <metric> 每月"])
→ add_chart(hint="trend", title="<metric> 月度趋势",
                   column_types={"每月":"date"})
```

- 识别环比波动 > 15% 的月份（拐点）
- 用户 query 里时间跨度太短（< 3 个月）→ 跳过 MUST-3，注明

### 产出 MUST checklist

- [ ] 同比数值 + canvas 有 **≥ 2 chart**（归因 rank + 月度趋势）
- [ ] markdown 含：诊断结论（一句话）+ 归因明细（top-3 bullet）+ 时间拐点（拐点月份）
- [ ] 建议 **≥ 2 条**（针对 top-1 维度值）

---

## SHOULD 层（用户说 "详细 / 深入"，或 Lead 的 Sub-task context 要求）

### SHOULD-1 · 量 vs 价拆分

对 top-1 维度属性，发两条并行查询把 metric 拆量×价
（具体指标按 workspace：电商订单量/客单价；制造产量/单价；SaaS 用户数/ARPU —— 从 `bi_schema()` 挑语义对应）：

```
bi_query(queries=["<time> <dim>=<attr> <量指标> 同比"])
bi_query(queries=["<time> <dim>=<attr> <价指标> 同比"])
```

判定：

- 量 ↓ 为主 → 获客/渠道问题
- 价 ↓ 为主 → 促销/定价问题
- 两者都 ↓ → 结构性衰退

### SHOULD-2 · 次级维度交叉

找 top-1 维度属性内部还有啥次级分化：

```
bi_query(queries=["<time> <主维度=top-1 属性> <metric> 按 <次级维度>"])
→ add_chart(hint="rank", limit=10, sort_by="<metric>")
```

### 产出 SHOULD checklist

- [ ] 额外 +1 chart（量/价 对比 或 次级维度 rank）
- [ ] narrative 扩充：**根因分析段**（量 vs 价 判定 + 时间拐点与外部事件的对应）
- [ ] 量化影响段：**"若把 top-1 属性 &lt;量指标&gt; 修到持平，整体下滑从 -12% 收窄到 -7%"**

---

## MAY 层（if 用户说 "深度 / 全套" 或 intent=report 且 score ≥ 0.9）

### MAY-1 · 双层归因

对 MUST-2 top-1 再做一次归因（固定这个属性作为 filter）：

```
bi_query(queries=["<time> <dim>=<top-1>内 <metric> 按<次级维度> 同比"])
```

### MAY-2 · 集中度指标

用 MUST-2 artifact + python_sandbox 算：

```
skill_invoke(python_sandbox, code="
  import pandas as pd
  df = pd.read_csv(INPUTS['attr']['path'])
  # 下滑集中度：top-3 贡献占总下滑
  df_down = df[df['delta'] < 0]
  share_top3 = df_down.nlargest(3, columns='abs_delta')['abs_delta'].sum() / df_down['abs_delta'].sum()
  print(f'下滑集中度 top-3 share = {share_top3:.1%}')
")
```

### 产出 MAY checklist

- [ ] 报告有 **风险与机会段**（集中度评估 + 外部事件核验）
- [ ] 双层归因展示下钻路径（eg "品牌 A → 品类 X → 门店 Y"）

---

## Scope 协商范例

| 用户说                        | SOP 跳过 / 调整                                                          |
| ----------------------------- | ------------------------------------------------------------------------ |
| "GMV 下滑，简要说明"          | 只 MUST 层；每段 1-2 句                                                  |
| "为什么 GMV 下滑，重点看品牌" | MUST-2 主维度固定 品牌；次级维度 SHOULD-2 跳过                           |
| "下滑根因全面分析"            | MUST + SHOULD + MAY 全套                                                 |
| "只看 A 品牌下滑原因"         | MUST-2 filter 固定 `品牌=A`，skip attribute top-N；直接到 SHOULD-1 量/价 |

---

## 自检清单（接 analyst-framework §4）

对照框架 8 条 + 本 SOP MUST/SHOULD 产出 checklist. 缺 3+ → 补跑.

---

## 反例（绝对不要）

> 风格示例，具体指标/维度需用 workspace 的 `bi.*` 返回决定.

**❌ 数字 + 套话**：

> &lt;指标&gt; 下滑 12%，主要原因是整体市场不景气，建议加强运营。

**✓ MUST+SHOULD 层层下钻**：

> 今年 &lt;指标&gt; 同比 -12%，**&lt;主维度&gt; 的 &lt;维度值 A&gt; 贡献 60% 下滑**（净降 3.6 亿）.
> 拆解后 &lt;维度值 A&gt; &lt;量指标&gt; -8%、&lt;价指标&gt; -4% —— **量降为主**.
> 按月看 **Q3 7 月起环比暴跌 -22%**，与外部事件（竞品大促）时间重合.
> **若把 &lt;维度值 A&gt; &lt;量指标&gt; 修到持平，整体下滑从 -12% 收窄到 -7%**.
> 建议：① 评估 7-9 月 &lt;维度值 A&gt; 流量恢复；② &lt;价指标&gt; 下滑单独归因 &lt;次级维度&gt;.

（配套 canvas：归因 top10 柱 + 月度趋势折线 + 量/价对比 + 次级维度 rank）

---

## 错误处理

- `bi_query` 返空 rows → 跳 MUST-2；改从 MUST-3 时间归因入手
- 某指标查询挂（BIValidationError 等）→ 跳过注 "暂无数据"，继续其他步
- 核心指标都失败 → bail 信封 `{"error": {"kind": "domain_mismatch", "message": "核心指标在 workspace 里查不到: <列名字>"}}`
- python_sandbox 挂 3 次 → bail 信封 `{"error": {"kind": "tool_failure", "message": "sandbox 连续失败: <最后一条错误>"}}`
- 缺数字不要编，宁可返 "暂无"
