---
name: building-dashboards
description: "Build 3-tier monitoring dashboard: KPI + charts + rule-compliance. Use for '做看板 / build dashboard / 监控面板'. Not for simple lookups."
version: 1.0.0
title: "Dashboard builder"
title_zh: "监控看板搭建"
category: design
---

# Skill · building-dashboards

## Overview

Build a **three-tier monitoring dashboard** from data the user provides. Each tier drills deeper: overview KPIs → grouped breakdown → business-rule compliance. Output must include 2+ KPIs, 2+ charts, 1+ data tables, and 600+ char narrative tying the layers together.

## When to use / when not

- **Use when**: user says "做看板 / dashboard / 监控面板 / 分层分析 / 三层递进 / monitoring panel / compliance dashboard"，OR 上传数据附件 + 明确业务规则（"X 要同时有 Y 和 Z"）.
- **Don't use when**:
  - simple lookup（"今年 GMV"）→ 用 Lead 直答
  - 单指标趋势（"GMV 月度走势"）→ 用 `analyst-sop-period-compare`
  - 宽泛复盘（"最近经营怎么样"）→ 用 `analyst-sop-business-review`
  - 诊断（"为什么 X 下滑"）→ 用 `analyst-sop-diagnosis`
  - 找驱动（"谁带动了增长"）→ 用 `analyst-sop-growth-driver`

**本 skill 的独特价值**（vs business-review 等）：

1. **三层递进骨架** 强制结构化（总览 / 分组 / 规则专项）
2. **业务规则编码** pandas mask（其它 SOP 没有）
3. **附件优先** 数据源路由（其它 SOP 默认 datarc BI）

若用户 query 只匹配前两点（Layer 1-2），没有规则 + 没有附件 → business-review 更合适，转用.

## Prerequisites

`skill_read("analyst-framework")` — read framework first for:

- Data source routing (`§0.1`) — attachment vs datarc BI
- Schema detection (`§0.2`) — must run first if there is an attachment
- Business rule encoding (`§0.3`) — translate rules into pandas masks

## Workflow

Copy this progress tracker into your response and check items as you go:

```
Dashboard Progress:
- [ ] Step 1: Parse scope, rules, and tier hints from user query
- [ ] Step 2: Route data source (attachment OR datarc BI)
- [ ] Step 3: (if attachment) Run schema detection
- [ ] Step 4: Layer 1 — Overview KPIs (≥ 2 kpi_row cells)
- [ ] Step 5: Layer 2 — Grouped breakdown (≥ 1 rank chart)
- [ ] Step 6: Layer 3 — Business-rule compliance (≥ 1 data_table, sample rows)
- [ ] Step 7: (optional) SHOULD — drill-down / trend / cross-tab
- [ ] Step 8: Narrative tying layers + actionable recommendations
- [ ] Step 9: Self-check against output template
```

### Step 1 · Parse scope / rules / tiers (**业务 → 结构化 plan**)

用户给的通常是**业务描述 + 规则 + 报告骨架**, 不是 BI 可执行 query.
**禁止**直接写 bi_query, 必须先**结构化翻译**, 在自己的 reasoning 里
列出来:

| 项               | 意义                                                      | 来源                      |
| ---------------- | --------------------------------------------------------- | ------------------------- |
| **数据范围**     | 时间 / 状态 filter (已提交/已审批/排除草稿等)             | 用户原句 + ws.name/desc   |
| **受监控子集**   | 哪些 row 进入分析 (含/不含 / 边界条件)                    | 用户原句"聚焦 X / 剔除 Y" |
| **业务规则**     | 判定 mask: 子集 → 字段 → 阈值 (条件分支按 §0.4 多 mask)   | 用户原句"X 要 A，Y 要 B"  |
| **报告 outline** | N 段递进 / 子集对照 (按 output-contract §1.9 落 insights) | 用户原句"X-Y-Z 三层"      |
| **派生指标**     | 合规率 / 占比 / 分位 等不在 measures 里的 → compute path  | 上面规则推                |

**必先 `bi_schema()` 摸底**（一次拿 measures + dimensions）, 再:

- 业务概念 ↔ workspace 真字段 一一对应
- 概念找不到对应字段 → 先 `bi_concept_resolve` 查词表, 查不到按常识解释后回查核对, 拿不准用 clarify 问用户; **绝不**强行用相近名 dim 凑
- 每个要分析的维度都要列出来作 group-by 维度

把以上 5 项**写进 reasoning** 再开始拉数据. 这步省了 → 后面 query 写垃圾, 必返工.

### Step 1.5 · NL query 写法铁律 (bi_query 共用)

**只用真 dim/measure 名作 filter / group-by 关键字**. 写 query 前必有：

1. `bi_schema()` 探底 (Step 1 已做)
2. query 里**每个非时间词**都要能在 `bi_schema` 的 measures.names 或 dimensions.names 里找到对应

**不能当 filter 的词** (会让 datarc 0 行):

- workspace.name / workspace.description 里出现的主题描述词 (整个工作区已经是这个领域)
- 用户原 query 里的派生概念 / 合规判定词 (这些是你 query rewrite 后**自己算**, 不是 dim_value)
- 任何**没在 bi_schema dimensions 返回里出现**的词

**0 行 retry 协议**:

- 第一次 0 行 → 读返回里的 `_parse`, 看哪个词没绑上字段
- 没绑上的主题词 / 派生概念 / schema 里没有的词 **从 query 里去掉**重发
- **不要**换个同义词再加 filter (同概念加 filter 还是 0 行)
- 连续 2 次 0 行 → 改用纯聚合 query (无主题 filter, 全维度 group-by), 在 python 里 mask 计算合规

### Step 2 · Route data source

See `analyst-framework §0.1` data source decision table. Pick **one** source and stick to it — do not mix attachment + datarc.

### Step 3 · Schema detection (attachment only)

If the user uploaded a file, run the standard detection snippet from
[../analyst-framework/references/schema_detection.md](../analyst-framework/references/schema_detection.md)
first. Stdout comes back in your next iteration so you know which column is
what. Do not proceed to Layer 1 without detecting.

### Step 4 · Layer 1 — Overview KPIs（**目标厚度**：≥ 6 个 KPI，分主题展示）

Produce **2 `kpi_row` sections**，各 3-5 cells，按主题分组：

**Row A · 规模 / 整体量**

- Total count（总数 / 总额）
- Overall health rate（合规率 / 活跃率 / 有效率）
- Key volume metric（如"受监控范围 N 单"）

**Row B · 效率 + 风险**

- 最严格规则的合规率（e.g. 项目支出合规率）
- 主风险指标（重点管控占比 / 违规率 / 流失率）
- 趋势速读（e.g. 环比变化 + 拐点月份）
- 分布离散度（最好组 - 最差组的 pp 差距）

**写法**：

- 每个 cell **必带单位**（%, pp, 元, 条）
- ratio 值同时展示**基数**（例 "合规率 78% (6234/8000)"）
- 风险类 cell 标红（subCaption: tone="neg"）

Example code template lives in
[references/layer_templates.md](references/layer_templates.md) § Layer 1.

不出 chart（KPI 够概览）；narrative Overview 段用这些数字点评四维（规模/效率/风险/趋势）.

### Step 5 · Layer 2 — Grouped breakdown

Pick the **primary grouping dimension** (体系 / 部门 / 品类 / 渠道 / 门店) and compute per-group KPIs:

```python
# See references/layer_templates.md § Layer 2 for full code
grouped = df.groupby("<dim>").agg(
    n=("<key>", "count"),
    compliant=("<rule_result>", "sum"),
    rate=("<rule_result>", "mean"),
)
grouped.to_csv("group_stats.csv")
```

Then `add_chart(data_artifact_id=<csv artifact>, hint="rank", sort_by="rate")` to render a bar chart.

Narrative should call out **top** and **bottom** groups with the delta between them (in pp for rates, % for counts).

### Step 6 · Layer 3 — Business-rule compliance

This is the **distinguishing layer** vs other SOPs. Encode the user's rules into pandas masks (see
[../analyst-framework/references/business_rule_patterns.md](../analyst-framework/references/business_rule_patterns.md)) and produce:

- Per-rule compliance rate (`print(f'rule X compliance: {mask[domain].mean():.1%}')`)
- Violation sample CSV (≤ 20 rows from the worst-violating group)
- `add_chart(data_artifact_id=<violations.csv>, hint="raw", force_kind="table", limit=20)` to show the samples

Do **not** rely on a pre-existing "是否合规" column — always re-compute from raw fields. Pre-computed columns may be stale, computed under a different rule, or absent.

### Step 7 · SHOULD layer (if complexity_score ≥ 0.85 or user says "深度/详细/全套")

Pick 1+ of:

- **Drill-down**: for the worst group in Layer 2, do a second-level groupby (`df[df['dim']==worst].groupby('次级维度')`).
- **Trend**: if schema has a date column, compute monthly rate and `add_chart(hint="trend")`.
- **Cross-tab**: `pd.crosstab(df['体系'], df['违规原因'])` → heatmap or stacked bar.
- **Branded theme (optional)**: if user configured brand / theme → `skill_invoke(name="theme-factory", input={...})` to generate brand-consistent palette for the exported charts.

### Step 7.5 · MAY layer · 可下载 HTML dashboard（user says "导出 HTML / 给我个可下载看板 / interactive dashboard"）

When user wants an exportable interactive artifact (not just the in-app canvas):

```
skill_invoke(name="web-artifacts-builder", input={
    "title": "<dashboard title>",
    "sections": [
        {"kind": "kpi", "cells": [...]},
        {"kind": "rank_chart", "data": "<csv artifact id>"},
        {"kind": "violations_table", "data": "<csv artifact id>"},
        ...
    ],
    "theme": "<optional theme from theme-factory>",
})
```

产出的 React/JavaScript HTML artifact 只作为可下载的交互文件，不得用 iframe 或
`canvas_add_html_artifact` 嵌入 Report。若用户同时需要 Report 内阅读，另产一份已物化的单文件静态
HTML（正文、数字、表格、图形都不依赖 JavaScript/fetch/外部资源），以正确 HTML MIME 持久化后调用
`canvas_add_html_artifact`。
**当且仅当** 用户明确要 "导出 / 下载 / 离线可打开 / interactive dashboard" 才触发，日常 canvas 够用.

### Step 8 · Narrative

Use the output template below. Do not produce under 600 characters.

### Step 9 · Self-check

Before returning, validate:

- [ ] ≥ 2 kpi_row cells rendered
- [ ] ≥ 2 charts (Layer 2 rank + at least one SHOULD chart OR Layer 3 violations table)
- [ ] ≥ 1 data_table with violation/detail samples
- [ ] Narrative ≥ 600 chars, ≥ 3 sections (overview / grouped / rules), each with ≥ 1 specific number
- [ ] ≥ 2 actionable recommendations

If any unchecked, go back and add that layer.

**厚度硬指标**（若不达标自动补跑）：

- 总 KPI cell 数 **≥ 6**（通常 2 个 kpi_row × 3-4 cells）
- canvas chart 数 **≥ 2**（Layer 2 rank + 至少 1 个 SHOULD）
- data_table 行数 **≥ 10**（违规样本或分组明细）
- narrative 总字数 **≥ 800**（dashboard 类要比 business-review 更厚）
- 行动建议 **≥ 3 条**（每条含"具体动作 + 预期量化影响"）

## Output template (ALWAYS use this exact structure)

```markdown
## <Dashboard title>

### 总览

- 总量 / 核心合规率 / 重点管控占比（≥ 3 句，每句含 1 数字 + 单位）

### 分组层

- 最差组 = <name> (rate X%), 最好组 = <name> (rate Y%), 差距 Z pp
- Top 3 分组按 rate 排序：…
- Bottom 3 分组：…

### 规则合规

- 规则 A: 合规率 X% (N 条违规), 主要违规模式：…
- 规则 B: 合规率 X% (N 条违规), 主要违规模式：…
- 违规样本见 data_table

### 下钻（若做了 SHOULD layer）

…

### 行动建议

- 建议 1：针对 <最差组>，<具体动作>，预期整体率从 X% → Y%
- 建议 2：针对 <top 违规原因>，<具体动作>
- 建议 3：建立 <次级维度> 日常监控（频次 / 阈值 / 通知渠道）
```

## Examples

**Example — user has attachment with 结算单 + 合规规则**

Input query:

> "基于附件里的 EA 单据做合规监控看板，三层递进：总体→按体系看→项目支出要同时有请购+立项的违规专项"

Flow:

1. Parse scope=EA单据, rules=项目支出→请购+立项, tiers=总体/按体系/违规专项
2. Data source = attachment（用户明说"附件里的"）
3. 跑 schema 探测 → 拿到 dtypes + head
4. Layer 1: total=N, 总合规率=X%, 项目/部门支出比例
5. Layer 2: groupby('体系') → rank chart
6. Layer 3: pandas mask (`is_proj & has_purchase & has_proj_id`) → violations.csv
7. SHOULD 跳过（用户没要求）
8. Narrative 按模板 + 3 条建议

Output: kpi_row (4 cells) + rank chart + violations table + narrative ~800 chars.

## Gotchas

- **G1 跑错数据源**：用户说"附件"你去查 datarc = critical bug. 永远先走 framework §0.1 判路由.
- **G2 chart 降级成 table**：数据有很多列（如 EA 单据 35 列）传 hint=rank 会退化. Layer 2 代码先 `df[['grp', 'rate']]` 裁两列再保存 artifact.
- **G3 用现成"是否合规"字段**：pre-computed column 可能不匹配用户的新规则. 永远重算.
- **G4 每层只写 1 句**：narrative 要 ≥ 600 字；每层 ≥ 3 句 + 1 个数字支撑.
- **G5 凭印象写 query**：先 `bi_schema()` 再写，schema 里没有的词不进 query.
- **G6 三层合成一坨**：必须明确分段 h3（"### 总览"/"### 分组层"/"### 规则合规"），不要粘在一起.
- **G7 sandbox stdout 太长**：`print(df)` 把全表吐出会撑爆 context. 用 `df.head(3).to_string()` + `df.describe()`.

## Error handling

- No attachment + workspace 无对应 measure → return `insufficient_data: no data source for dashboard`.
- Schema detection python_sandbox 挂 → retry 1 次；再挂返 `insufficient_data: schema detection failed`.
- 用户规则表述不清（"重点管控怎么定义"） → 在 narrative 里明说"规则需澄清"，产出 Layer 1-2 但跳过 Layer 3，提示用户补规则.
- Layer 2 分组数 > 50 → 只展示 top 20 + 累加"其他".

## References

- [layer_templates.md](references/layer_templates.md) — 三层每层的代码模板
- [../analyst-framework/references/schema_detection.md](../analyst-framework/references/schema_detection.md)
- [../analyst-framework/references/business_rule_patterns.md](../analyst-framework/references/business_rule_patterns.md)

## Related skills

- `analyst-framework` — 方法论前置依赖（必读）
- `web-artifacts-builder`（Anthropic） — MAY 层导出 HTML dashboard
- `theme-factory`（Anthropic） — SHOULD 层品牌主题（可选）
- `canvas-pdf / canvas-pptx / canvas-xlsx` — 末尾导出静态报告
