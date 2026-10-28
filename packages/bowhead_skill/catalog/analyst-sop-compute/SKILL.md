---
name: analyst-sop-compute
description: "Numerical compute SOP beyond datarc BI: stats/forecast/clustering/regression/correlation. Use for '显著/p-value/预测/聚类/相关性'."
required_skills: [analyst-framework]
version: 3.1.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Numerical analysis workflow"
title_zh: "数值计算 SOP"
category: analysis
---

# Analyst Compute SOP v3

**何时用**：`bi_query` / `bi_query + python_sandbox` 得不到的计算—统计检验、预测、聚类、回归、自定义聚合、集合差集、分位数阈值等.

**前置**：`skill_read("analyst-framework")` —— 里面有三档契约 + scope 协商.

## 三档产出契约（跟 analyst-framework §0.5 对齐）

本 SOP 的任务大多是"计算任务"，有明确算法输出，不太需要多层细化. 所以：

- **MUST**（任何 compute 都必出）：Step 1-5 拿数据 → sandbox 算 → canvas 展示结果
- **SHOULD**（用户说 "详细 / 深入", 或 Lead 的 Sub-task context 要求）：补稳健性检验（如 bootstrap CI）+ 多方法交叉（如 t-test + Mann-Whitney 并行）
- **MAY**（if 用户说 "深度 / 全套"）：对结果再归因（哪个维度贡献最大到显著差异？）

**canvas 产出铁律**：sandbox 跑完若产 CSV/JSON → 一定 `add_chart(data_artifact_id=<sandbox 输出 artifact>)`，别只 print 结论不画图.

## Scope 协商范例

| 用户说                        | SOP 调整                                                                  |
| ----------------------------- | ------------------------------------------------------------------------- |
| "A 和 B 显著吗"               | 只 MUST：Step 2 拿两组 + Step 3 t-test + 说结论                           |
| "A 和 B 显著吗，有多少置信度" | MUST + SHOULD：加 CI 和 effect size                                       |
| "A 和 B 显著吗全面分析"       | MUST + SHOULD + MAY：再归因差异来自哪维度                                 |
| "预测 Q4 销量"                | MUST：Step 2 拿近期 series + ARIMA；SHOULD：多模型比较（MA/LinReg）取最好 |
| "哪些门店月销量差距 >10 倍"   | MUST：bi_query → sandbox 按门店算 max/min ratio 过滤                      |

---

## 典型任务（按 intent 认）

| 关键词                            | 方法                                                          | 返回                         |
| --------------------------------- | ------------------------------------------------------------- | ---------------------------- |
| "显著吗" / "统计显著" / "p-value" | `scipy.stats.ttest_ind` / `chi2_contingency` / `mannwhitneyu` | p 值 + 显著性结论            |
| "预测" / "forecast" / "下月多少"  | `statsmodels.tsa.ARIMA` / `sklearn.LinearRegression`          | 预测点估 + 置信区间          |
| "聚类" / "分群" / "cluster"       | `sklearn.cluster.KMeans` / `DBSCAN`                           | 每簇中心 + 簇大小 + 典型成员 |
| "相关性" / "correlation"          | `df.corr(method='pearson')`                                   | 矩阵 + 说人话挑 top 3        |
| "回归" / "OLS" / "因果影响"       | `statsmodels.OLS`                                             | 系数 + p 值 + R²             |
| "置信区间" / "CI"                 | bootstrap or `scipy.stats.t.interval`                         | 95% CI                       |

## 5 步执行

### Step 1 — 读 framework（已必做）

```
skill_read(name="analyst-framework")
```

### Step 2 — 拿原始数据

用 `bi_query` 以**按月 / 按日 / 按维度**的聚合形式拿到 raw timeseries / cross-section。

**时间粒度用 datarc 的原生语法**（不要在 python 里再切，datarc 切得更快更准）：

- `每天/每周/每月/每季度/每年` —— 基础粒度
- `每3天/每2周/每N月/每N年` —— 自定义间隔
- 同义：`按天/按月` / `每日/每周`

例：

```
bi_query(queries=["2025年 华东 营业额 每月"])
→ 12 条月值（N=1 时返 flat 形态；看 `sample_rows` 的时间列是否逐月、`total_rows` 是否 ≈ 12）
```

**关键**：

- **拿到能算的形态**：t-test 要两组数 → "2024 各月 / 2025 各月"；ARIMA 要连续 series → "近 36 个月 每月"
- **验证 granularity**：看返回的 `sample_rows` 时间列粒度 + `total_rows`；不一致（如你要"每月"但只返 1 行 = 全期汇总）→ 重新问
- **拉原始不做聚合**：聚合交给 python_sandbox pandas —— 减少多次 bi 调用

### 拿行级数据的**标准打法**（compute 必读）

聚类 / 回归 / 相关性 / 分布拟合 / 差集 / 分位数都要"**多行数值数据**"。**唯一入口是
`bi_query`** —— 它内部按你写的 NL text 自动路由（"按 X 分组 / 前 N / 列出哪些 / 明细"
走行级 CSV；单值走 metric；单实体 + 每月走时间序列）。不用记多个工具，只需把 text
写对。

#### `bi_query(queries=[...], limit?, columns?)` —— 行级清单唯一入口 ⭐

任何 "按 X / 各 X / 前 N / 列出哪些 / 明细" 类 query 一步到位拿 CSV 行. 底层走
datarc async（5-15s 延迟）。`queries` 是 **list**（N=1 也写成 list）；**两种返回形态**：

```
# N ≥ 2（批量）
{
  results: [                       # 与 queries 同序
    {
      ok: bool,
      query_text: str,
      _ref_artifact_id: "art_xxx",   # ★ 数据已存到 artifact —— sandbox inputs 用这个 id（不要用下标取 results[i]）
      sample_rows: list[dict],       # 预览 ≤10 行; 完整 rows 在 artifact 里
      total_rows: int,
      _parse: {tokens, unknown_words, half_match_values},   # datarc 怎么解析的（best-effort, 可能缺）; unknown_words 不空 = 本结果没按这些词筛选, 按 analyst-framework G2.7 处理（核对范围 / 解析或按常识展开 / 写明口径）
      _no_data / _no_data_hint / _preflight_hint   # 0 行 / 改写建议 / **时间区间被换掉的警告**（有则带, 见下）
    }, ...
  ]
}
# N = 1（单条, flat 形态, 向后兼容）——成功时**没有** ok / query_text 字段（只有失败才带 ok=false + error_kind/error_message）
{
  columns: list[str],
  rows: list[dict],                # inline ≤30 行预览; 完整 rows 在 artifact 里
  row_count, total_rows,
  artifact_id: "art_xxx", _ref_artifact_id: "art_xxx",   # 同一个 id（后者是镜像, 两种形态都能用 _ref_artifact_id 取）
  _parse / _no_data / ... 同上（有则带）
}
```

**关键：数据已落 artifact_id，不要 LLM 逐字抄进 code**. 做差集 / 中位 / 聚类时：

```python
# Step A: bi_query 两次拿数据（artifact_id 会记录在 tool_result）
a = bi_query(queries=["2025年4月 买示例药品乙的医院 金额"])  # _ref_artifact_id = art_AAA
b = bi_query(queries=["2025年4月 买示例药品甲的医院"])  # _ref_artifact_id = art_BBB

# Step B: skill_invoke python_sandbox，通过 inputs 载入，不要在 code 里粘 CSV
skill_invoke(
    name="python_sandbox",
    script="runner.py",
    input={
        "intent": "两家机构覆盖差集（只在 A 出现的医疗机构）",
        "code": """
import pandas as pd
df_a = pd.read_csv(INPUTS['a']['path'])   # 示例药品乙 全量 1630 行
df_b = pd.read_csv(INPUTS['b']['path'])   # 示例药品甲 全量 1968 行
only_a = set(df_a['医疗机构']) - set(df_b['医疗机构'])
sub = df_a[df_a['医疗机构'].isin(only_a)]
med = sub['金额'].median()
high_pot = sub[sub['金额'] > med].sort_values('金额', ascending=False)
print(f"只买 A 的医院数 = {len(sub)}")
print(f"金额中位数 = {med}")
print(f"高潜医院数 = {len(high_pot)}")
high_pot.to_csv('/workdir/high_potential.csv', index=False)
""",
        "inputs": {
            "a": {"artifact_id": "art_AAA"},  # 只写 artifact_id, 文件名宿主侧自动补; 文件落 inputs/a.csv
            "b": {"artifact_id": "art_BBB"},
        },
        "timeout_sec": 30,
    },
)
```

**简单 top-k / 可以直接读 rows 的不用 sandbox**：rows 就在 tool_result 里，直接
在你的总结里引用"top 1 徐州铜山 3,366,360 / top 2 驻马店 833,258 / ..." 即可.

**用法约束**：

- `text` 的 measure / dimension 用 `bi_schema()`的
  `names[0]`（主关键词）对齐，别乱造.
- **"前 N" datarc 不截**：给 `limit=N` 让本工具本地切；不传 limit 会返全量.
- 做集合 / 分位计算时 **一律走 artifact 路径**，别抄数字进 code（见示例）.
- 否定词"没 / 未 / 不 / 除..."不支持，分两次 `bi_query` 拿 A / B artifact 再 python 本地差集.
- **(π K) sandbox 写 pandas 前必先 print INPUTS schema**：`skill_invoke(python_sandbox)`
  首次返回的 `INPUTS["<input_name>"]` 已带 `columns` / `sample_first_5` / `dtypes` /
  `row_count`。**写代码前先 print schema**，列名直接抄 schema，别凭 `bi_query` 时的列
  名记忆（LLM 容易 hallucinate）。`<input_name>` 是你在 `inputs: {<input_name>: {...}}`
  自定的 key，别假定固定叫 `rows` 或 `sales`。

**🚨 datarc NL 范式（基于 200 条 ws=1 实测，2026-04-27，95.5% 通过率）**

**支持** (实测 ✓，放心写)：

| 类别              | 范式                               | 例                                                                           |
| ----------------- | ---------------------------------- | ---------------------------------------------------------------------------- |
| 时间              | 相对/绝对/范围/同期/X前            | `今年` / `2024年Q1` / `2024-01-01至2024-03-31` / `去年和今年同期` / `近30天` |
| Where 单值        | 空格连接                           | `品牌 金辉福 营业额`                                                         |
| Where 多值同 dim  | 空格连接（默认 OR）                | `品牌 金辉福 阿迪达斯 营业额`                                                |
| Where 多 dim      | 空格连接（datarc 自动 AND）        | `品牌 金辉福 省 江苏 市 南京市 营业额`                                       |
| 否定              | `不是` / `!=`                      | `品牌 不是 金辉福` / `品牌!=金辉福`                                          |
| 模糊              | `包含/像/开头是/结尾是`            | `品牌包含金` / `品牌结尾是福`                                                |
| 比较 (dim 数值)   | `>` / `<` / `介于X到Y` / `在X以上` | `门店面积>10000` / `门店面积介于5000到10000`                                 |
| Having (指标过滤) | 同上                               | `营业额大于1000万 品牌` / `营业额介于500万到1000万 品牌`                     |
| 排序              | `最高/最低` / `前N` / `top/bottom` | `营业额最高的5个品牌` / `门店营业额top10`                                    |
| 分析              | 趋势/同比/环比/占比/对比           | `各品牌营业额 同比` / `金辉福 在 各品牌营业额 占比`                          |
| 子查询            | 带或不带引号都接                   | `"去年营业额最高的5个品牌" 今年营业额`                                       |

**不支持 / 拆开** (实测 fail，必避)：

1. ❌ `贡献度` — 不识别. 改用 `维度分析` 或 sandbox 自算
2. ❌ `预测` / `异常检测` — datarc 不算 ML, 走 compute profile + python_sandbox
3. ❌ **同比/环比 + having 组合**: `营业额同比增长大于10% 的品牌` 返 0 行. 拆: step1 `各品牌营业额 同比` → step2 sandbox 过滤
4. ❌ 时间分隔用 `/`: `2024/01/01至2024/03/31` API 报错. 用 `-`: `2024-01-01至2024-03-31`
5. ❌ 复合 having 子查询 + 对比: `客流量大于10000的门店 各品牌 营业额对比` 返 0 行. 拆: step1 拿门店 list → step2 用 list 查 各品牌
6. ⚠ 连接词 `且 / 和 / 或` **被静默忽略** (实测 OR/AND 由 datarc 自动按 dim 边界推): 不写, 用空格分

**Chain query (`chained_query` / `result_based_query`) 必拆**:

```python
# ❌ 不可控: 让 datarc 自己 chain (带引号返 sum, 不带返双列)
bi_query(queries=['"去年营业额最高的5个品牌" 今年营业额'])

# ✅ 拆开调
top5 = bi_query(queries=["去年营业额最高的5个品牌"])
this_year = bi_query(queries=["金辉福 哥弟 阿迪达斯 春宇 苹果 今年营业额"])
# python_sandbox join 按 brand
```

**信号 → 拆查询**: 单次 bi_query 返 `row_count=0` + `duration>60s` + 你列了 ≥ 5 dim 列, handler 会返 `_preflight_hint` 提示拆.

### ⛔ `_preflight_hint` 里出现 🚨 / ⚠ 时间区间警告 → 不许当没看见

datarc 会在**不报错、返回形状完全正常**的情况下换掉你要的时间段（两个客户环境实测过）。
handler 认出来时会把警告放进 `_preflight_hint`：

- **🚨「datarc 实际查询的是 X 年，而你要的是 Y 年」** —— 这份数**不是你要的口径**（问未来月份时
  它会悄悄给你去年同月，值看着完全正常）。**不要拿它算数、不要叙述、不要写进 payload**；
  如实说该时间段没有数据。换个措辞重问没用，它每次都这么回。
- **⚠「实际返回了 N 个月（缺 …）」** —— 区间被截到"有数据的月份"了。只能按**实际返回的月份**
  叙述，并明确说明缺哪几个月。**绝对不要**拿它算全年汇总、全年达成率、年度目标完成度——
  那正是这条警告存在的原因（真实事故：拿 1-8 月的数当全年，两轮答案相反且都错）。

没有这条警告**不等于**时间段一定对：只说年份（`2026年 营业额`）或只说半年/季度的标量查询
datarc 不给任何信号。要在结论里用"全年/整个 H2"这类口径时，先用 `每月` 拉出时间序列看月份齐不齐。

### 多 metric 同维同时间 → 1 次 batch query

N ≥ 2 个 metric 在同一时间窗 → **1 次 `bi_query(queries=["2026年 A 同比", "2026年 B 同比", ...])`（一条 query 一个 metric, 单次 ≤20 条, 后端并行）不要 N 次循环调用**.

反例: 22 次独立的 `bi_query(queries=["2026年 X 同比"])` 调用, 浪费 ~50s.
正例: 1 次 `bi_query(queries=["2026年 A 同比", "2026年 B 同比", ..., "2026年 V 同比"])`.

实现要点 (compute peer prompt soft rule, 不是硬性 cap):

- 同 dim 同 time window 才 batch; 跨 dim / 跨 period 拆.
- 5 个 metric 是经验阈值, ≤4 个循环也接受 (overhead 微).
- 若 batch 返 row 形态混乱 (不同 metric 单位差大), 再退回拆查.

#### 多指标矩阵（同维多 metric）—— 一条 query 塞多个 metric

要一个维度上一次拿多指标（营业额 + 客流量 + 客单价），直接把它们写进同一条 NL：

```python
bi_query(queries=["2025年 各门店 营业额 客流量 客单价 前 30"], limit=30)
# 一条返各门店的多指标合成行矩阵；聚类 / 回归的特征列一步拿齐.
```

已有实体清单时先 `bi_query` 取清单，再把清单里的实体名拼进第二条 `bi_query`
（见下方 chain query）。

#### 时间序列（单实体）—— `bi_query` + `每月 / 每日`

单实体连续 time series 用 `bi_query` + 粒度词，返逐期一行（`sample_rows` 预览 + 完整序列在 artifact）：

```
bi_query(queries=["2025年5月-2026年4月 耐克 销售额 每月"])
# 返 12 条 metric rows. ARIMA / linear fit 都能跑.
```

粒度词 datarc 支持 `每天/每周/每月/每季/每年` 或 `每3天/每2周` 等. `total_rows` ≈ 期数说明对了；
返 `_no_data` 说明 datarc 没理解，换问法；`_parse.unknown_words` 不空说明本结果没按这些词筛选，按 analyst-framework G2.7 处理（核对范围、解析或按常识展开、写明口径）.

---

### 绝对**不允许**的行为

| 错误行为                                       | 你应该做的                                                                          |
| ---------------------------------------------- | ----------------------------------------------------------------------------------- |
| 看 insight 文字提了几个省名就当真 / 编实体名   | 用 `bi_query` 拿真实体清单                                                          |
| 把 "A 没买 B" 写一条 query                     | 分两次 `bi_query` 取 A / B 各自清单，sandbox 差集                                   |
| 看到 1 次 0 行 / `_no_data` 就断言"拿不到数据" | 看 `_no_data_hint` / `_parse`, 改写 text（"各 X / 前 N"）再拿 rows                  |
| 看到 `ok=false` / `error_kind` 就放弃          | 看 `error_message`; 限流 (`datarc_rate_limited`) 等一下重发, 其它换措辞或拆单实体查 |
| 直接请用户"提供 CSV"                           | 上述打法都真试过失败才请求用户                                                      |
| 编造数据跑 sklearn                             | 宁可返 "暂无数据" 也别编                                                            |
| 分组 >10 就说"太多循环不动"                    | bi_query 一次全拿，别循环                                                           |

### 任务自检清单（回 Lead 前过一遍）

- [ ] "按 X / 各 X / 前 N / 列出 / 明细" 这类 query 走的是 `bi_query`（写对 text 即可）？
- [ ] 否定词 / 集合运算拆成了两次 `bi_query` 再 sandbox 本地算？
- [ ] 单实体时间序列用的是 `bi_query + 每月`？
- [ ] 归因问题（"为什么 X 下滑"）用的是 `bi_query（按主维度）+ python_sandbox`？
- [ ] "前 N" 用 `limit` 本地截而不是靠 datarc？
- [ ] DataFrame 至少有 N ≥ 3 行（否则 KMeans 没意义）？
- [ ] 数据真是 tool 拿的，不是我自己生成 / 示例 / 硬编的？

**未过清单 → 不许返 "请你提供 CSV"**

### Step 3 — 调 python_sandbox 算

```
skill_invoke(
  name="python_sandbox",
  script="runner.py",
  input={
    "code": "<python>",
    "intent": "<一句话：这次在算什么，会作为「数据来源」文案给用户看>",
    "timeout_sec": 30
  }
)
```

**数据注入只有一种方式——artifact 走 `inputs`**（不要把数字抄进 code，哪怕只有 12 个点；
抄数 = 让 LLM 当复印机，真机里抄错/抄漏反复出现）：

```json
"inputs": {"ts": {"artifact_id": "<bi_query 返的 _ref_artifact_id 或 sandbox 上次的 output_artifacts[].artifact_id>"}}
```

只写 `artifact_id`，文件名宿主侧自动补；文件落在 `inputs/<key><ext>`，code 里
`df = pd.read_csv(INPUTS['ts']['path'])`（`INPUTS` 是已注入的变量，**别加引号写成字符串**）。

### Step 4 — 读 stdout 拿数

```python
result = skill_invoke(...)
# result.stdout 形如 "p_value=0.0023\nt_stat=3.14"
# parse → 结构化数据
```

**必须用 print()**：数字要能从 stdout 取回。代码里直接 print 最终结论，别只写文件。

### Step 5 — 结构化回 Lead

**两行说结论**：

```markdown
**结论**：华东 2024 vs 2025 GMV 同比增长 **显著**（p=0.0023 < 0.05，t=3.14）。
**建议**：把增长动能归到当前 6 个月的 sustained 趋势；下一步可按门店再拆看.
```

**绝不**：

- ❌ 心算或估算 p 值（LLM 的 p-value 直觉错到离谱）
- ❌ 没调 sandbox 就给出数字
- ❌ 代码里硬编数字"假装是 bi 返的"

## 增长 / 排名计算的语义规则（ranking 类 query 必看）

用户问 "增幅最大的 top N"、"涨得最多的"、"下滑最明显的"、"top X 里谁增长最快" 这类时，python
代码**必须**遵守：

### 1. "增幅" = Δ > 0；负数不属"增幅"

把**下滑的医院/门店也排进"增幅最大 top 5"**是业务错答. 先 filter `Δ > 0`：

```python
growth = [(h, q1, q2, q2 - q1) for h in universe if ...]
positive = [r for r in growth if r[3] > 0]  # 只保留正增长
positive.sort(key=lambda x: -x[3])  # 按 Δ 降序
top5 = positive[:5]
```

### 2. 新进入者 (Q1=0 → Q2>0) 必须包含

"0 基数"在业务上是**新进入者**，增量一般是 `Δ = Q2` ，应排进榜；**不能**因为
`growth_rate = (Q2-Q1)/Q1 = inf` 就 drop 掉：

```python
# ❌ 错：这条会把新进入者（Q1=0）抛出
if q1 > 0:
    rate = (q2 - q1) / q1
else:
    continue  # ← 业务错，这家医院 Q2 买了 10 万但没进榜

# ✅ 对：Q1=0 时用 Δ 排，并标记 "新进入"
rate = (q2 - q1) / q1 if q1 > 0 else float("inf")
delta = q2 - q1
new_entry = q1 == 0 and q2 > 0
```

### 3. Δ 和 rate 都算，按 **Δ 绝对值排序**为默认

Δ 比 rate 更抗"基数陷阱"（Q1=1 → Q2=100 是 100 倍但只有 99 元增量）.
用户说"增幅最大"默认按 **Δ 降序**：

```python
# 返表两列都给：按 Δ 排，% 作辅助信息
rows = sorted([...], key=lambda r: -r.delta)
for r in rows[:5]:
    rate_str = f"{r.rate * 100:+.1f}%" if r.q1 > 0 else "new_entry"
    print(f"{r.name}: Q1={r.q1:,}  Q2={r.q2:,}  Δ={r.delta:+,}  rate={rate_str}")
```

**用户特别要 "% 最高"** 才换成按 rate 排 + 小 base 告警（"仅 22,002 基数，参考价值有限"）.

### 4. "下滑" / "下降最多" 同理反过来

Δ < 0 才算下滑；按 |Δ| 降序（或 Δ 升序）. **别把涨的跟跌的混在一张 "变化最大" 表里**.

## 反例

### ❌ 错法 1：偷懒说"我这里没法计算"

```
用户：2025 年 vs 2024 年 GMV 同比是否显著？
Agent：抱歉我没法做统计检验。
```

→ **有 python_sandbox 没用，扣分**

### ❌ 错法 2：心算编数字

```
Agent：p ≈ 0.03 显著的。
```

→ **没调 sandbox 数字是编的，生产事故**

### ❌ 错法 3：数据硬编

```python
# 代码里写死
gmv_24 = [100, 105, 110]
gmv_25 = [120, 125, 130]
```

→ **数字不是真的，分析结论白扯**

### ✅ 对法

```
1. bi_query(queries=["2024年 华东 营业额 每月", "2025年 华东 营业额 每月"])
   → results[0]._ref_artifact_id = art_A (12 行), results[1]._ref_artifact_id = art_B (12 行)
2. skill_invoke(name="python_sandbox", script="runner.py", input={
     "intent": "华东月度营业额 2024 vs 2025 的 t 检验",
     "inputs": {"a": {"artifact_id": "art_A"}, "b": {"artifact_id": "art_B"}},
     "code": """
import pandas as pd, scipy.stats as st
print(INPUTS['a']['columns'], INPUTS['b']['columns'])      # 先看列名, 别猜
a = pd.read_csv(INPUTS['a']['path']); b = pd.read_csv(INPUTS['b']['path'])
col = [c for c in a.columns if '营业额' in c][0]
t, p = st.ttest_ind(a[col], b[col])
print(f't={t:.3f}  p={p:.4f}  mean_before={a[col].mean():.1f}  mean_after={b[col].mean():.1f}')
"""})
3. 读 stdout 里的 t, p, means → 登记进 ground_facts
4. 返 Lead："华东 24→25 同比显著（p=0.0003），均值从 X 涨到 Y"

```

## 铁律

1. **不要心算**：p-value / 回归系数 / 聚类中心必须 sandbox 算
2. **数据从 bi 来**：硬编数字 = 编假的
3. **结构化结果落文件**：聚类 labels / 回归系数表 / 分布统计等**行级数据**用
   `df.to_csv('/workdir/xxx.csv', index=False)` 或 `df.to_json('/workdir/xxx.json', orient="records")`
   保存；前端 Data tab 会自动展成可排序表格、可下载原文件. **不要只 print 表格到 stdout** ——
   agent 看到的 stdout 会被截断 8KB，用户也看不到结构化表
4. **文本结论才 print**：用 print 输出**人读的文字结论**（"p=0.003 显著, 聚为 3 类, 最大类 60% 占比"）
5. **一次一算**：别 100 行组合拳，拆多个 skill_invoke 更好 debug
6. **画图 = 出数据，不是出图**：把要画的数据 `df.to_csv('/workdir/x.csv', index=False)` →
   `add_chart(data_artifact_id=<该 csv 的 art_id>)` 渲染**原生交互图表**（用户可切换视角、
   数字可溯源）。`plt.savefig('/workdir/x.png')` **仅限原生画不了的**（地图 / 箱线图·QQ plot 等
   统计诊断图 / 多子图矩阵 / treemap·雷达·甘特·网络图）→ 那时用 `add_image` 挂
   ⚠ **散点例外**：用户要「每个点标上名字」时仍走 `add_chart`——原生散点会自动挑互不重叠的一批点常驻标注（导出/触屏必标），其余 hover 可见。
7. **失败不重试 3 次以上**：sandbox 反复失败 → 告诉 Lead "compute 失败: <error>"，让 Lead 决定降级

## 输出规范（前端如何消费）

| 你做啥                                                | 前端展示                                                         |
| ----------------------------------------------------- | ---------------------------------------------------------------- |
| `print("p=0.003 显著")`                               | Lead 引用进 narrative / callout                                  |
| `df.to_csv('/workdir/x.csv')` → `add_chart`           | **原生交互图表**（自动选型 / 用户可切换视角 / 数字可溯源）← 默认 |
| `plt.savefig('/workdir/x.png')` → `add_image`         | 静态图片区块（**仅**原生画不了的图，见铁律 6）                   |
| `df.to_csv('/workdir/clusters.csv')`                  | **Data tab 可排序表 + 徽章"来自 Python Sandbox"**                |
| `df.to_json('/workdir/coefs.json', orient='records')` | 同上                                                             |

关键：**让用户可引用**，光 print 文字的话用户没法点"看看这个数据"。**凡是行级 / 可排序数据，落成 CSV**.

## 预装包

pandas · numpy · scipy · statsmodels · scikit-learn · matplotlib · seaborn
没 prophet / torch —— 要用这些告诉 Lead 让运维补 image。
