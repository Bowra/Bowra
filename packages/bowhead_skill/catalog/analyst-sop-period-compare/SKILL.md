---
name: analyst-sop-period-compare
description: "Compare metric across periods (YoY/QoQ/MoM/WoW) with time series + dim breakdown. Use for '同比/环比 / vs last year / YoY'."
required_skills: [analyst-framework]
version: 3.4.0
pipes_output_to: analyst-sop-output-contract
trigger_examples:
  - "今年 GMV 同比"
  - "本月营业额环比"
  - "和去年相比利润怎么样"
  - "相比上月用户数有多少"
  - "Revenue YoY this quarter"
  - "QoQ comparison of DAU"
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Period comparison"
title_zh: "周期对比分析"
category: analysis
---

# Analyst SOP · 周期对比 v3

## 前置

`skill_read("analyst-framework")`.

**家底获取**：先调一次 `bi_schema()`（并发拿 measures + dimensions），同一任务调一次即可，结果留在对话里。

## 选 comparison 类型（第一步必做）

- "同比 / YoY" → NL 里写「同比」（vs 去年同期）
- "环比 / QoQ / MoM / WoW" → NL 里写「环比」（vs 上一周期）
- "同环比"（两者都要）→ 2 条 query：`["<metric> <time> 同比", "<metric> <time> 环比"]`
- 用户没说 → 默认同比

## ⭐ datarc 原生时间语法（本 SOP 的地基，禁拆）

同比/环比/每月/每季度/趋势 都是 datarc NL2SQL **一条 query 原生支持**的语法
（实测全绿, docs/dataseek-50-query-eval.md）：

- ❌ 禁逐期拆：`queries=["1月 X", "2月 X", ...]`
- ❌ 禁查两期自己算 delta：`queries=["今年 X", "去年 X"]` + 手算

---

## MUST 层（任何周期对比都必出）

### MUST-1 · 基础对比值 + MUST-2 · 时间序列（1 次 batch 拿齐）

```
bi_query(queries=[
  "<metric> <time> <同比|环比>",   # MUST-1: 返当期值 + 同期值 + delta %
  "<metric> <time> 每月",          # MUST-2: 返完整月度序列（季度口径用 每季度）
])
→ add_chart(data_artifact_id=<每月那条的 _ref_artifact_id>, hint="trend",
                   title="<metric> 月度趋势", column_types={"每月":"date"})
```

- MUST-1 返 0 行 → 按 framework §6 换 workspace 实际字段名重问；`_parse.unknown_words` 不空 → 本结果没按这些词筛选，按 framework G2.7 处理（核对范围、解析或按常识展开、只用数据里有的值、写明口径）；仍失败 → 用 MUST-2 的序列走 `python_sandbox` 算 delta（这是唯一允许手算的兜底路径，caveats 里注明口径）
- MUST-2 识别连续 N 期为负 / 增速放缓 / 单月异常（环比波动 > 15%）

### 产出 MUST checklist

- [ ] canvas 有 **≥ 1 chart（trend）**
- [ ] markdown 含 kpi 行（当期值 + delta % + 单位） + 时间趋势描述（1-2 句）
- [ ] 若出现明显拐点，单独指出该月份 + 绝对值

---

## SHOULD 层（用户说 "详细"、含"按 X"，或 Lead 的 Sub-task context 要求）

### SHOULD-1 · 主维度分拆 vs 同期

**首选路径** (透明 + 用户公式可对齐, 见 framework §0.45 G4):

```
bi_query(queries=[
  "<time> <metric> 按<主维度>",        # 当期
  "<上期time> <metric> 按<主维度>",    # 上期
])
→ python_exec(code="from period_growth import compare_growth ...")  # 同期对齐 + delta + 排名
→ add_chart(data_artifact_id=<growth.csv>, hint="rank",
                   title="<metric> 同比归因 Top 10")
```

⚠ 本期没走完（年初至今 / 当月未完）时，上期**必须是同一时段**，不能拿去年全年比。两期对齐、
剔除没过完的月份这两件事交给 `compare_growth` 做（见下「增长排名」），别手写 `groupby().sum()` 再相除。

**Fallback 路径** (用户没明示 + budget 紧)——一条原生语法直接要分维度同比:

```
bi_query(queries=["<time> <metric> 按<主维度> 同比"])
```

- 按 delta 排序 → 找最亮 / 最差（top 3 贡献 + bottom 3 贡献）

### SHOULD-1b · 增长排名（「增长最快 / 下滑最多的 N 个 X」）

```python
from period_growth import compare_growth
r = compare_growth([df_本期年, df_上年], value="<metric 列>", period="<期间列>", group="<主维度列>", top_n=<N>)
print(r.summary)                          # 口径与结论事实：照它写结论
r.table.to_csv("growth.csv", index=False) # add_chart 用；「增长率」列 column_types={"增长率": "percent_fraction"}
```

- 口径由它定：没过完的期间先剔除；有上年就只用同期同比；没有上年才退回「近期 vs 前期」并写明无法排除季节性。
- **季节性强的月度数据不要用环比或趋势斜率判断「增长」**——两年逐月形状相同时，斜率量到的是季节，不是增长。
- 排名只在正增长里排；基数为 0（新增）和规模过小的组单列说明、不进排名。
- 正增长不足 N 个 → 如实说「只有 X 个在增长」，**不拿负增长凑数**；一个都没有就说没有。

### SHOULD-2 · 增长/下滑结构

用 SHOULD-1 artifact + python_sandbox：

```
skill_invoke(python_sandbox, code="
  import pandas as pd
  df = pd.read_csv(INPUTS['attr']['path'])
  # 增长贡献集中度
  pos = df[df['delta'] > 0]
  top3_share = pos.nlargest(3, 'delta')['delta'].sum() / pos['delta'].sum() if len(pos) else 0
  print(f'top3 正贡献占比 = {top3_share:.1%}')
")
```

### 产出 SHOULD checklist

- [ ] 额外 +1 chart（归因 rank）
- [ ] narrative：结构分析段（最大贡献 vs 最大拖累）
- [ ] 量化：若最差维度值修复到均值，整体 delta 改善多少

---

## MAY 层（if 用户说 "深度 / 全套"）

### MAY-1 · 次级维度交叉

对 SHOULD-1 top-1 贡献维度值再 drill：

```
bi_query(queries=["<time> <主维度=top-1 属性> <metric> 按<次级维度>"])
→ add_chart(hint="rank")
```

### MAY-2 · 多周期对比

除同比 / 环比，额外加：

- `近3年同比趋势` → 判断是否历史性波动
- `上月 vs 本月环比` → 环比的环比

### 产出 MAY checklist

- [ ] 风险 / 机会段（集中度、多周期连续性评估）

---

## Scope 协商范例

| 用户说                       | SOP 跳过 / 调整                                      |
| ---------------------------- | ---------------------------------------------------- |
| "今年 GMV 同比多少"          | 只 MUST-1（1 条 query），跳过 trend（是单值 lookup） |
| "今年 GMV 同比 + 月度走势"   | MUST-1 + MUST-2 都做（仍是 1 次 batch 2 条）         |
| "各省同比"                   | MUST-1 + SHOULD-1 按 省拆解                          |
| "同比 + 详细归因 + 次级下钻" | 全套 MUST + SHOULD + MAY                             |

---

## 禁忌

- **同比和环比不混用**（必须区分口径）
- **时间口径一致性**（"今年" = 年初至今 ≠ 完整 365 天；年初至今只能比上年同一时段）
- **没过完的期间先剔除再算**：只在结论里「标注仅供参考」不够，环比 / 斜率 / 同比照样被它拉偏
- **百分点 pp vs 百分比 %**：率值变化（如利润率 17% → 15%）差 **2 pp**，不是 2%

---

## 错误处理

- bi_query 返 0 行 + `_parse.unknown_words` 不空 → datarc 没识别该词：字段名问题换 workspace 实际字段名（`bi_schema()` 返回的 names[0]）重问；地名/名称类按 framework G2.7 解析或按常识展开，只用数据里有的值，写明口径
- 同比/环比 query 失败 → 用「每月」序列 + python_sandbox 算 delta 兜底（caveats 注明）
- 「每月」序列也挂 / 返空 → 跳时间序列，只出 MUST-1 数值
- 所有数字工具挂 → bail 信封 `{"error": {"kind": "tool_failure", "message": "<哪个工具、什么错>"}}`
