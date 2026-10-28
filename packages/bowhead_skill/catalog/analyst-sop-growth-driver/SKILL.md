---
name: analyst-sop-growth-driver
description: "Top growth drivers — delta-ranked attribution + cross-dim validation. Use for '增长来自哪 / 谁带动 / which product drove X'."
required_skills: [analyst-framework]
version: 3.2.0
pipes_output_to: analyst-sop-output-contract
trigger_examples:
  - "GMV 增长来自哪里"
  - "谁带动了营业额增长"
  - "利润增长的主要原因是什么"
  - "Which product drove Q2 revenue"
  - "哪个品类贡献了最多销售增长"
  - "芳草地门店增长来自哪个品类"
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Growth driver analysis"
title_zh: "增长驱动归因"
category: analysis
---

# Analyst SOP · 增长归因 v3

## 前置

`skill_read("analyst-framework")`.

> 本 SOP 与 workspace 无关 —— 所有 `<metric>` / `<dim>` / `<value>` 占位符必须用
> 运行时 `bi_schema()`/ `bi_query` 返回替换.

**家底获取**：先调一次 `bi_schema()`（并发拿 measures + dimensions），同一任务调一次即可，结果留在对话里。

---

## MUST 层

### MUST-1 · 确认增长指标 + 时间窗（1 条）

```
bi_query(queries=["<metric> <time> 同比"])
```

- 用户点名 metric → 用；未点名 → 从 `bi_schema()` 挑 sort 最低的规模类
- 默认 time = `近30天 同比`；用户明示 → 用具体
- 若 YoY < 2% → 告知"增长未达显著阈值"，**不要**编 driver

### MUST-2 · 主维度归因（1 条）

**首选路径** (透明 + 用户公式可对齐, 见 framework §0.45 G4):

```
bi_query(queries=["<time> <metric> 按<主维度>"])          # 当期
bi_query(queries=["<上期time> <metric> 按<主维度>"])      # 上期
→ skill_invoke(python_sandbox)  # merge + 算 delta + contribution_share
→ add_chart(data_artifact_id=<merge>, hint="rank",
                   title="<metric>同比归因 Top 10")
```

**Fallback 路径** (用户没给公式 / 清单 / 拆维度时, budget 紧):

```
bi_query(queries=["<metric> <time> 按<主维度> 同比"])   # datarc 原生归因，一条返分维度 delta
```

- 主路径优先: 用户给了**公式 / 清单 / 拆解维度**时 — 必须 bi_query 拉明细 + python
- Fallback 用 datarc 原生同比归因: 用户没明示 + 默认菜单也想拿 top-N
- 按 delta 排序取正向 top-3（正贡献），负向 top-3（负贡献，拖累增长）
- 主维度 = `bi_schema()` sort 最前那条，或用户点名

### 产出 MUST checklist

- [ ] canvas 有 **≥ 1 chart**（归因 rank）
- [ ] narrative：**top driver 一句话结论**（"品牌=A 贡献 60% 增量，价格=X 拖累 20%"） + 量化（增量绝对值）
- [ ] **不假设维度/属性存在**（只用 bi.* 返的真实值）
- [ ] 建议 **≥ 2 条**（"复制 top driver 玩法" + "检查拖累项")

---

## SHOULD 层（用户说 "详细 / 交叉"，或 Lead 的 Sub-task context 要求）

### SHOULD-1 · 次级维度交叉

对 MUST-2 的 top-1 属性，拉次级维度拆解：

```
bi_query(queries=["<time> <主维度=top-1 属性> <metric> 按<次级维度>"])
→ add_chart(hint="rank", limit=10, sort_by="<metric>")
```

### SHOULD-2 · 组合验证

若次级维度 top-1 与主维度 top-1 重合 → 组合查询确认：

```
bi_query(queries=["<metric> <time> <dim_A>=<value_A> <dim_B>=<value_B> 同比"])
```

### 产出 SHOULD checklist

- [ ] 额外 +1 chart（次级维度分布）
- [ ] narrative：**交叉结论**（"品牌 A × 品类 X 组合贡献 35%"）
- [ ] 量化：若 top driver 模式推广到其他维度值，整体增量可 +Y%

---

## MAY 层（if 用户说 "深度 / 全套"）

### MAY-1 · 多维度归因

对另外 2-3 个维度各做一次 bi_query（原生同比归因），并列展示：

```
bi_query(queries=["<metric> <time> 按 <dim_X> 同比"])
```

集合 top-1 各维度，判 driver 共识（同一业务能多维度命中）.

### MAY-2 · 增量集中度

用 MUST-2 artifact + python_sandbox 算：

- top-3 贡献 / 总增量（集中度高 = 风险）
- Herfindahl-Hirschman 指数（HHI < 1000 分散 / >2500 高集中）

### 产出 MAY checklist

- [ ] 风险段：top driver 集中度 + 可复制性分析
- [ ] 多维度共识 driver（同时在 N 个视角 top-3）

---

## Scope 协商范例

| 用户说               | 动作                                          |
| -------------------- | --------------------------------------------- |
| "增长来自哪"         | 只 MUST（1-2 次调用）                         |
| "增长来自哪，按品牌" | MUST-2 主维度固定 品牌；SHOULD 跳             |
| "增长全面归因"       | MUST + SHOULD + MAY                           |
| "A 品牌的增长来自哪" | filter 固定 `品牌=A`，SHOULD-1 走次级维度拆解 |

---

## 禁忌

- 单维度归因不直接结论 "因为 X"，要交叉验证（SHOULD-1）
- attribution 的数值必须引用，不改写
- **不要**假设维度/属性（没"渠道"、"品类"、"城市" 这些默认）—— 永远以 `bi_schema()` 为准
- 正负贡献分清楚：top-3 正贡献 ≠ top-3 负贡献取反；两者都要展示

---

## 错误处理

- bi_query 返空 rows → 换更窄时间窗试；仍空则告"无显著维度差异"
- `bi_schema` dimensions 为空（rare）→ bail 信封 `{"error": {"kind": "domain_mismatch", "message": "workspace 没有可拆的维度"}}`
- python_sandbox 挂 → 只出 MUST+SHOULD 层，略过集中度计算
