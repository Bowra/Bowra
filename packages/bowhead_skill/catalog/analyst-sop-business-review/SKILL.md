---
name: analyst-sop-business-review
description: "Broad business review: 3-5 KPIs + monthly trend + primary-dim breakdown. Use for '最近经营 / how's business / 整体复盘 / overall status'."
required_skills: [analyst-framework]
version: 3.2.0
pipes_output_to: analyst-sop-output-contract
trigger_examples:
  - "最近经营如何？"
  - "业绩怎么样"
  - "给我看看最近的数据"
  - "整体复盘一下"
  - "How's business going"
  - "帮我看看 Q1 整体情况"
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Business performance review"
title_zh: "业务综合复盘"
category: analysis
---

# Analyst SOP · 业务复盘 v3

> 类型 C 宽泛问题 SOP. **容易做砸的坑**：拉几个数字摆上去 = 数据搬运.
> 本 SOP 按 MUST / SHOULD / MAY 三档给动作和产出，Lead / subagent 按
> 用户显式 scope + Lead 的 Sub-task context 决定执行到哪档（没有数值分档信号会下传给你）.

## 前置

`skill_read("analyst-framework")` —— 建立心智模型 + 看 scope 协商 + 三档规则.

---

## MUST 层（任何复盘都必出）

### 动作 MUST-1 · 摸家底（最多 1 秒）

```
bi_schema()   → 一次拿 workspace 的全部指标（含 sort）与全部维度
```

### 动作 MUST-2 · 核心指标 × 同比（5-8 条并发）

从 measures 里按以下规则挑 **5-8 个**核心指标（目标报告丰富度）：

1. `sort` 字段升序（越小越核心），取前 5-8
2. `sort` 缺失或相等 → 至少覆盖 **3 大主题**，每主题 2-3 个：
   - **规模类**（金额/数量总量）：营业额 / GMV / 客流 / 订单 / 利润 ≥ 2 个
   - **效率类**（率/比例）：利润率 / 客单价 / 转化率 / ARPU ≥ 2 个
   - **风险类**（退款/差评/流失）：退款率 / 差评率 / 流失率 ≥ 1 个
3. 用户显式点名的指标**必须**加入（不挤占 5-8 的槽位）
4. ≤ 5 个指标 = 报告薄；≥ 8 更全但要注意 token 预算

对每个指标并发发：

```
bi_query(queries=["<measure.names[0]> <时间窗> 同比"])
```

**时间窗规则**：

- "最近 / 近期" → `近30天`
- "本月" → `本月`（+ 环比）
- "今年 / YTD" → `今年`（+ 同比）
- "去年" → `<last_year>年`（runtime_context 取具体年份）
- "Q1/Q2" → `<year>年Q1`

**返回预期**：每个 measure 返 `type=metric` + metrics 含同比值 + 变化率 %.

### 动作 MUST-3 · 主指标月度趋势（1 条）

主指标 = `measures[0]`（通常是规模类 top 指标）或用户显式点名.

```
bi_query(queries=["<主指标> <时间窗> 每月"])
→ 返 artifact_id
add_chart(data_artifact_id=<...>, hint="trend",
                 title="<主指标> <时间窗> 月度趋势", column_types={"每月":"date"})
```

### 动作 MUST-4 · 主维度拆解（1 条）

主维度 = `dimensions` 的 sort 最高者 或用户显式指定（如"按品牌"）.

```
bi_query(queries=["<主指标> <时间窗> 按 <主维度.names[0]>"])
→ 返 artifact_id (全量按降序)
add_chart(data_artifact_id=<...>, hint="rank",
                 title="<主指标> 按<主维度> top 10", limit=10,
                 sort_by="<主指标.names[0]>")
```

### 产出 MUST checklist（subagent 返 Lead 前自检）

- [ ] canvas 有 **≥ 2 个 chart section**（至少一个 trend + 一个 rank）
- [ ] 返 Lead 的 markdown 有 **5-8 个 KPI 行**（每行带 当期值 + 同比值 + % 变化；覆盖 3 大主题：规模 / 效率 / 风险）
- [ ] narrative 含 **overview 一段**（"营收规模 / 盈利能力 / 效率 / 风险" 四维速评；每维 ≥ 1 数字支撑）
- [ ] 建议部分 **≥ 3 条**（具体可执行，不是"建议关注"；每条含量化预期影响）
- [ ] **数据厚度**：narrative 总字数 ≥ 600 字；若 ≤ 400 字 = 报告太瘦必补

不齐 → **补跑步骤直到齐**，不要返 Lead.

**返 Lead 格式**（markdown）：

```markdown
## 经营概览

- <measure_1.name>：<当期值>（YoY <pct>）
- <measure_2.name>：<当期值>（YoY <pct>）
- ...

## 核心发现（2-3 条）

- 关键发现 1 + 数字支撑
- 关键发现 2 + 数字支撑

## 行动建议（≥ 2 条）

- 建议 1：针对 <X>，<具体动作>
- 建议 2：针对 <Y>，<具体动作>
```

---

## SHOULD 层（用户说"详细 / 深入 / 完整"，或 Lead 的 Sub-task context 要求）

### 动作 SHOULD-1 · 归因主指标同比

**首选路径** (透明 + 用户公式可对齐, 见 framework §0.45 G4):

```
bi_query(queries=["<时间窗> <主指标> 按<主维度>"])        # 当期
bi_query(queries=["<上期time> <主指标> 按<主维度>"])     # 同期
→ skill_invoke(python_sandbox)  # merge + 算 delta + contribution_share
→ add_chart(data_artifact_id=<merge>, hint="rank",
                   title="<主指标>同比归因 Top 10 贡献维度")
```

**Fallback 路径** (用户没明示 + budget 紧):

```
bi_query(queries=["<主指标> 按<主维度> 同比"])   # datarc 原生归因，一条返分维度 delta
```

若原生归因直接返 rows 有数据，可 inline 成 `force_kind="table"` 的 chart.

### 动作 SHOULD-2 · 次维度拆解

再挑 1 个次重要维度（不同于 MUST-4 的主维度）：

```
bi_query(queries=["<主指标> <时间窗> 按 <次维度>"])
add_chart(..., hint="rank", limit=10, sort_by="<主指标>")
```

### 动作 SHOULD-3 · 拐点识别

拿 MUST-3 的月度 artifact：

```
skill_invoke(python_sandbox, code="
  import pandas as pd
  df = pd.read_csv(INPUTS['trend']['path'])
  # 找 MoM > ±20% 的拐点月份
  df['MoM'] = df['<主指标>'].pct_change()
  spikes = df[abs(df['MoM']) > 0.2]
  print(spikes.to_string())
", inputs={"trend": {"artifact_id": "<MUST-3 的 artifact>", ...}})
```

对每个拐点月份追加 drill query：

```
bi_query(queries=["<拐点月份> <主指标> 按 <主维度>"])
add_chart(..., hint="rank", title="<拐点月> 结构异常")
```

### 产出 SHOULD checklist

- [ ] canvas 额外 +2 chart（归因 + 次维度 或 拐点分析）
- [ ] narrative 扩充 **问题诊断段**：量化损失（"若 X 修正到均值可提升 Y"）
- [ ] markdown 加 **具体拐点月份 + 同比 %**

---

## MAY 层（if 用户明说 "深度 / 全套 / 最细" 或 intent=report 且 score ≥ 0.9）

### 动作 MAY-1 · 集中度 / 分布统计

```
# 用 MUST-4 的 artifact
skill_invoke(python_sandbox, code="
  import pandas as pd, numpy as np
  df = pd.read_csv(INPUTS['rank']['path'])
  total = df['<主指标>'].sum()
  shares = df['<主指标>'] / total
  hhi = (shares ** 2).sum() * 10000  # Herfindahl-Hirschman
  cv = df['<主指标>'].std() / df['<主指标>'].mean()
  print(f'HHI={hhi:.0f}  CV={cv:.2f}')
  print('Top3 占比:', shares.head(3).sum())
", inputs={"rank": {"artifact_id": "<MUST-4 artifact>", ...}})
```

### 动作 MAY-2 · 第二归因 / 环比 / 多维度串行下钻

对 SHOULD-1 归因 top 1 做第二层下钻：

```
bi_query(queries=["<top 贡献维度值> <主指标> 每月"])
add_chart(..., hint="trend", title="<维度值>月度表现")
```

### 产出 MAY checklist

- [ ] narrative 加 **风险与机会段**（集中度 / 健康度评估）
- [ ] 可能再加 1-2 chart（二层下钻）

---

## Scope 协商范例（用户显式意图 override）

| 用户说                       | SOP 跳过 / 调整                                                   |
| ---------------------------- | ----------------------------------------------------------------- |
| "最近经营怎么样，只看营业额" | MUST-2 只做 1 个指标（营业额），MUST-4 跳过或改成按主维度拆营业额 |
| "只按品牌看就行，别拉地区"   | MUST-4 主维度固定 品牌；次维度 SHOULD-2 跳过                      |
| "2025 Q1 详细复盘 完整分析"  | 触发 MUST + SHOULD + MAY 全套                                     |
| "整体看看就行，简要"         | 只 MUST 层；narrative 每段压到 1-2 句                             |
| "给我看营业额趋势图"         | 只做 MUST-3，其他全跳过；相当于 lookup/trend                      |

---

## 自检清单（接 analyst-framework §4）

返 Lead 前对照框架的 8 条 + 本 SOP 的产出 checklist. 缺 3 项以上 → 不返，补跑.

---

## 反例（绝对不要这样做）

> 风格示例，具体指标/维度需用 workspace 的 `bi_schema()` 返回决定.

**❌ 数据搬运**（老 SOP 版本容易出的结果）：

> 近 30 天 &lt;规模&gt; X 亿，&lt;利润&gt; Y 亿，&lt;退款&gt; Z 亿，&lt;数量&gt; N 万.

**✓ 数据分析**（按本 SOP MUST+SHOULD 产出的样）：

> 近 30 天 &lt;规模&gt; X 亿（同比 +12%），**但 &lt;风险率&gt; 同比 +3pp 是警号**. 按 &lt;主维度&gt;
> 拆解，&lt;值 A&gt; 贡献 60% 增量但 &lt;效率&gt; 下滑；&lt;值 B&gt; &lt;效率&gt; +20% 却贡献不足 5%.
> 6 月出现拐点（MoM -15%），主因 &lt;值 C&gt; 突发下滑（归因贡献 40%）.
> **若 &lt;值 B&gt; 转化追平 &lt;值 A&gt;（1.5% vs 0.8%），月均可多 1.2 亿**.
> 建议：① &lt;值 A&gt; 稳复购；② 6 月 &lt;值 C&gt; 深度排查；③ &lt;值 B&gt; 优化转化链路.

（配套 canvas：月度趋势折线 + 主维度 top10 柱 + 归因贡献柱 + 拐点月分析）

---

## 错误处理

- 某指标查询失败（BIValidationError 等）→ 跳过该指标，markdown 注 "暂无数据"，继续其他指标
- 所有核心指标都失败 → bail 信封 `{"error": {"kind": "domain_mismatch", "message": "核心指标在 workspace 里都查不到: <列名字>"}}`
- `bi_query` 返 0 行（workspace 维度配置问题）→ 跳过该拆解，注明
- python_sandbox 连挂 3 次 → bail 信封 `{"error": {"kind": "tool_failure", "message": "sandbox 连续失败: <最后一条错误>"}}`
- 缺一两个数字不要编，宁可返 "暂无"
