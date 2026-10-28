---
name: analyst-framework
description: "核心分析方法论 / Core analyst methodology: 4-step (摸底→拆解→诊断→定量) + ABC 任务分类 + data-source routing + reality-check / derivation / business-rule. Load before 归因/多维拆解/复合分析/公式推导/衍生指标 subagent task. Skip for trivial bi_query/lookup."
description_long: true
version: 2.1.0
title: "Analysis framework"
title_zh: "分析方法论骨架"
category: analysis
---

# Bowhead Analyst · 核心方法论

> 借鉴自内部成熟的 dataseek-analysis skill。所有复合/宽泛分析必读。

---

## 0. 数据源 + 规则编码（先于任何分析）

### 0.1 数据源路由

| 条件                              | 数据源    | 入口工具                                                               |
| --------------------------------- | --------- | ---------------------------------------------------------------------- |
| 有 data 附件（csv/xlsx/json/tsv） | **附件**  | `skill_invoke(python_sandbox)` + `pd.read_csv(INPUTS['data']['path'])` |
| 无附件 + workspace 有对应 measure | datarc BI | `bi_query`（唯一 BI 入口，按 text 自动路由单值/明细/序列）             |

**判断"有无附件"的依据（铁律）**：

- ✅ 看 system prompt 里的 `<workspace_attachments>` 段 / `INPUTS['data']` 是否真存在
- ❌ **不要**看用户 query 文字"我有数据"/"数据的内容是..."就脑补 — 用户可能在描述 workspace 数据
- 没有 `<workspace_attachments>` 段 + 没有 `INPUTS['data']` ⟹ **无附件，必走 BI 路径**
- query 含数据描述但 ctx 没附件 = 用户在描述 workspace 表结构 → 用 `bi_schema()` 摸家底
  | 两者都无 | 无 | bail 信封 `{"error": {"kind": "domain_mismatch", "message": "没有附件, workspace 也没有相关 measure"}}` |

**铁律**：有附件就用附件，**不要**跨源去查 datarc BI（那是工作区数据库 ≠ 用户上传的表）.

**先调一次 `bi_schema()` 拿 workspace 的 schema**（measures 含 sort/别名/描述 + 可查维度；`data_source.context_name` 是当前北极九章工作区名）。同一任务调一次即可，结果留在对话里。

### 0.2 有附件时第一步：schema 探测

用户上传数据 → 你**必须先**运行 schema 探测脚本才知道字段语义。见
[references/schema_detection.md](references/schema_detection.md).

### 0.3 业务规则 → pandas mask

用户说"X 要同时有 Y 和 Z" / "X ≥ N 算 Y" / "X 但不 Y" 这类规则时，**必须**
把规则翻译成 pandas boolean mask；**禁止**假设 df 里已有现成结果字段（如"是否合规"）.

6 种常见 pattern（必需字段 / 多字段 AND / 阈值聚合 / 集合差集 / 分位阈值 / ratio）
见 [references/business_rule_patterns.md](references/business_rule_patterns.md).

### 0.4 业务规则**条件分支**（同一目标，不同子集判定不同）

用户原 query 含 "对 &lt;子集 A&gt; 来说判定为 X，对 &lt;子集 B&gt; 来说判定为 Y" 的结构 ——
即**同一指标按子集走不同判定逻辑**. 触发词模式：

- "&lt;A&gt; 要 …，&lt;B&gt; 只要 …"
- "&lt;A&gt; 类 …，&lt;B&gt; 类 …"
- "对 &lt;A&gt; 而言… 对 &lt;B&gt; 而言…"
- "如果 X 则 A，否则 B"

这类规则**不能 collapse 成单一 mask**. 必须:

1. 先抽**规则表**（人话）: `子集 → 判定字段 → 判定条件`
2. **每个子集独立 mask 算合规率**
3. **按子集分别在报告里展示**, 再按其他维度交叉拆解

**绝不**: 把所有子集合并算一个统一指标当结论 (用户会发现规则被忽略).

落地步骤：

- 用 `bi_schema()` 摸清判定字段对应 workspace 哪个 dim
- 子集分类词不在 `bi_schema()` 里 → 先 `bi_concept_resolve` 查词表；查不到就按常识解释成成员值再回查核对，拿不准用 clarify 问用户
- **绝不**靠"看起来像"猜 dim (相近名字 ≠ 同语义)

### 0.5 "剔除/排除" 类条件 → 必含显式 filter

用户 query 含 "剔除 X" / "不含 Y" / "排除 Z" / "刨除 …" 类否定限定 →
`bi_query` 的 NL `text` **必须**显式带否定 filter（如
`<X 字段> != <X 值>` 或正向等价 `<X 字段> in [非 X 取值列表]`）。

否则数字含本应排除的子集，**报告结论是错的**。
跑数前 self-check: NL query 里是否体现了"排除 X"？

---

## 0. 核心理念：分析 ≠ 罗列数字

拉几个指标摆在一起不叫分析，叫**数据搬运**。

> 下面**仅为风格示例**，具体指标/维度/维度值需用 workspace 的 `bi_schema()` 返回决定。本框架不假定任何行业/业务背景。

**数据搬运（❌）**：

> &lt;指标 A&gt; 165 亿，&lt;指标 B&gt; 28 亿，&lt;指标 C&gt; 28 亿，&lt;率 D&gt; 17.4%，&lt;数量指标 E&gt; 40 家。

**数据分析（✓）**：

> &lt;指标 A&gt; 165 亿，但 &lt;风险指标&gt; 28.8 亿几乎吞掉全部 &lt;利润指标&gt; 28.8 亿 ——
> **&lt;风险率&gt; 高达 17.4%，意味着每赚 1 块钱就退掉近 1 块钱**。
> 进一步拆解发现，&lt;风险指标&gt; 集中在 Q3（占全年 42%），主要由 &lt;主维度&gt;=&lt;维度值 X&gt;
> 驱动（该值 &lt;风险率&gt; 31%，远超均值）。如果将 X 的 &lt;风险率&gt; 压到行业均值 15%，
> 全年 &lt;利润指标&gt; 可提升约 5.2 亿（+18%）。

差异在于：**对比** + **归因** + **量化影响** + **建议**。

---

## 0.45 用户授权优先（User Authority — SOP 是参考不是法律）

**最高原则**: 用户给的 framework / 公式 / 清单 / stated facts 永远 **override** SOP 默认 path.

SOP 是**默认菜单**, 是用户没明说时的兜底. 用户说了具体要什么, **照做**, 不要拿 SOP 当借口偷工.

### 必须完全遵从用户的 4 类输入

| 用户给了                                                        | 你必须做                                                                                                                        |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| **公式 / 计算逻辑** （e.g. "周转率 = COGS / 平均存货"）         | 用 `python_sandbox` 跑**确切**公式; 不准用 datarc 派生指标近似代替                                                              |
| **明细清单 / 分类列表** （e.g. "看 19 项成本: 原料/人工/...")   | **覆盖率 ≥ 80%**, 凑不齐就 bail 信封 `domain_mismatch`（message 写 "覆盖 M/N, 缺: …"）让用户缩范围; 不准只查头几项就交工        |
| **拆解维度** （e.g. "按品类 / 按工厂 / by province × hospital") | 强制 dimension-style query (`bi_query` "按 X 拆 Y") + python 算每 cell 贡献度; 不准只出单值                                     |
| **stated facts** （e.g. "4 月同比下降原因 / 我们是 X 厂家")     | **第一句话 reality check**: 数据真相 vs 用户假设差 > 2pp 就先纠正 ("实际数据显示 4 月同比上升 5.7%, 跟你假设相反") 再分析为什么 |

> **谁是权威**：G 系列的**硬约束一行版**住在 compute 每轮常驻的 system prompt 里（那才是每次都被读到的那份）。
> 本文件是**长版**：给推导、反例和执行顺序。两边讲同一条规则时，**阈值与必做动作以常驻版为准**——
> 改了这里也要回去看那一份，别让两处各自漂移。

### Reality Check Gate (G2) — 硬规则

每次调 `bi_query` 拿到核心数字后, 立即 cross-check 用户 stated facts:

```
if 用户 query 含方向词 (下降/上升/增长/萎缩/亏损/持平) 或因果词 (因为X / 主要靠X):
    cross_check = 实际数据方向 / 真实驱动
    if cross_check ≠ 用户假设 and (方向相反 OR abs(量级差) > 2pp OR 因果不成立):
        ⚠ payload.exec_summary **第 1 句必须**: "实际数据显示 <真相>, 跟你提到的 <用户假设> 不一致" 然后再写其他内容
        ⚠ 不准只在中段 narrative 软提及, 必须 exec_summary 首句
```

不准刺穿差 < 2pp 的微小偏差 (可能是统计口径差).

### Anti-Hallucination Gate (G2.5) — 数字必须来自数据

`exec_summary` / `kpis` / `narrative` 里**每个数字**都必须能 trace 到某一次 `bi_query` / `python_sandbox` 的真 result.

- ❌ 不准: 自创 KPI 数字 (e.g. "总营业额 234,567,890") 因为模型脑补
- ❌ 不准: 在 bi_query 真返了 1.39亿 时 narrative 写 "2.34亿"
- ✓ 必须: 数字 ⊇ {bi_query 真返列, python_sandbox stdout/result}

如果想算汇总数字 (sum / mean / 加权) — **必须** 用 `python_sandbox` 算一次, 不准心算.

### BI Fallback Detection (G2.7) — 兜底数据不能当真值

**Signal 1 · 显式信号（bi_query result 上有则看；`_parse` 是 best-effort, 缺了按「未核验」处理——拿 `bi_concept_resolve` 验关键词, 别当「全认出」）**:

```
result._parse.unknown_words 不空      → datarc 没认出这些词, 本结果没按它筛选(兜底数据) → 先核对返回范围; 地名/名称/指标先 bi_concept_resolve, 解析不出可按常识展开、只用数据里有的值重查并写明口径; 操作词(汇总/合计)忽略; 展开后全都不在数据里才 bail (domain_mismatch)
result._no_data == true              → 成功但 0 行; 看 _no_data_hint 与 _parse 再决定: 词没对上 → bi_concept_resolve 核对;
                                       要的指标 schema 里没有但能由已有字段算 → 切 G4.5 derivation path (自己拆公式, 拉 raw 字段, sandbox 算);
                                       算不出 → workspace 没这个概念, 如实 bail, 不编
```

（旧文档里的 `_auto_fallback_from` 字段**早已不存在**——tool 不会返它，别等它。）

**Signal 2 · 隐式 fallback (多次相同 column)**:

```
连续 ≥ 2 次 bi_query 查不同 metric (e.g. COGS / 平均存货 / 原料),
但都返同一个 column (e.g. 都是 "营业额 / 元"):
  → BI 在持续兜底, 你查的字段 BI 都没有
  → 必须: 立即停止 try, 总结"BI 仅有 X 字段, 用户清单的 Y/Z 都没有", bail
  → 不准: 用同一列数据当作不同 metric 算公式
```

**例外** — 合法 derived metric 计算 (e.g. F14 毛利率):

- 如果 AI 主动 fetch 多个 raw 字段 (营业额 + 直接成本) 然后 python 算公式 → 不触发 G2.7 (返的列各自不同, 没 fallback flag)
- 如果 AI 想拿 derived metric "毛利率" 但 BI 不存, fallback 到 "营业额" → 触发 G2.7

### G3.2 数据缺失时不要 propose "创建指标方案"

如果 `bi_schema` 反馈用户清单 **0 项存在** (全部 mismatch, 包括 fuzzy match 后) → 必须返 bail 信封 `{"error": {"kind": "domain_mismatch", "message": "workspace 不存在 X / Y / Z 字段（已 fuzzy 试过）"}}`.

**禁止** 改写任务为:

- ❌ "成本指标创建准备方案" — 那是 BI admin 的 job, 不是 analyst 的
- ❌ "建议先新增 X / Y 字段后重新发起" 作为整个 deliverable — 这是 caveat 不是分析报告
- ❌ propose 一份"指标定义建议书"代替分析

**正确**: 一句话 caveat ("workspace 不存在 X / Y / Z 字段, 已与 admin 反馈") + bail. 不要扩成多段方案.

### List Coverage Gate (G3)

用户给清单 N 项, 当前查询覆盖 M 项, **覆盖率 = M / N**:

- ≥ 80%: 继续 (允许漏个别项, **必须 explicit 列在 caveats** — 见下面强制规则)
- < 80%: **强制 bail 信封** `{"error": {"kind": "domain_mismatch", "message": "用户清单 N 项, 仅覆盖 M 项 (M/N=X%), 缺: [具体名字]; 请缩范围或换口径"}}`; **不准**自作主张交"伪完整"答卷

**漏项 caveat 强制规则** (M/N ≥ 80% 时):

- payload.insights 或 caveats 中**必须**含一条专门列举 missing 项的 entry
- 格式: "用户清单 N 项, 已覆盖 M 项: [list]; **未覆盖 (N-M) 项: [具体名字 list]** — BI workspace 不存对应字段 / 已 fuzzy 试过未匹配"
- ❌ 禁: "已查 4 项, NPS 暂未配置" (太简略, 没 explicit 列 missing list)
- ✓ 必: "用户给 5 项 (营业额, 利润, 客流量, 退款率, NPS 评分), 已覆盖 4 项 [营业额/利润/客流量/退款率]; **未覆盖 1 项: [NPS 评分]** — workspace 无此字段, 已 fuzzy 试 NPS/Net Promoter Score 均未匹配"
- F22 prod incident 教训: 4/5 查了但 NPS 缺 caveat 没显式列名, 用户不知道 AI 怎么处理 NPS

#### G3.1 同义词 fuzzy match (bail 前必试)

`bi_schema()`返的字段名跟用户清单**字面不重合时不要立刻 bail**, 先试 fuzzy match:

- 子字符串包含: 用户"原料" / BI "原材料" → 匹配 ✓
- 同义替换: 用户"电费" / BI "能源-电" → 匹配 ✓ (跨连字符 / 跨前后缀)
- 2-gram 重合: 用户"包装" / BI "包装材料" → 匹配 ✓

匹配成功 → caveat 显式说"已映射: 原料 → 原材料". 真匹配不上才 bail.

### Micro-Delta Caveat (G2.6) — 微差必标"未达阈值"

**触发条件**: 比较类指标 (同比 / 环比 / vs baseline) 的 |delta_pct| < 2% 时:

- 注意: **这是百分比比例**, 不是 absolute 差额! e.g. delta_pct = -0.29% 即使 absolute 差 123万 也算微差
- 含极小值: -0.0X% / -0.X% / 0.X% / 0.0X% / 接近 0 的 ±数字 都触发
- 不含: |delta| > 2% (那是真显著变化)

**强制**: payload.exec_summary **必须含**"未达显著阈值" 类 phrase, 例如:

- ✓ "GMV同比下降 0.29% (-123万), 幅度极小, **未达显著下滑阈值**, 不必过度归因"
- ✓ "Q1营收同比微增 0.03%, **基本持平**, 在统计噪音内"
- ✓ "退款率同比变动 0.8pp, **未达显著差异**, 主因可能是口径波动"

**禁止**: 把 0.1% / 0.29% / 0.5% 这类 delta 当"显著拖累"做完整归因 — 数据极小变动归因 = 编因果, 误导用户.

**判定优先级**: 先检查 |delta_pct|, 不是 |delta_absolute|. 4亿 GMV 跌 123万 看似多, 但 0.29% 在 noise floor, 不该归因.

### 归因路径优先级 (G4)

| 优先级       | 路径                                                   | 何时用                                               |
| ------------ | ------------------------------------------------------ | ---------------------------------------------------- |
| 1 (首选)     | `bi_query` 拉明细 + `python_sandbox` 算贡献度          | 用户给公式 / 给清单 / 要求"按 X 拆"时 (90% 归因场景) |
| 2 (fallback) | `bi_query("<metric> 按<主维度> 同比")` datarc 原生归因 | 用户没给公式 + 不在乎口径细节 + budget 紧            |

首选路径 (bi_query 明细 + python) 计算透明、可验证、能跟用户公式对齐; datarc 原生同比归因 (一条 query 带 delta) 是黑盒近似, 只在 budget 紧时退而求其次.

### G4.5 Derivation 优先 (核心架构原则)

**全局事实**: 用户问的指标大多是 **derived** (公式组合 / 多字段加总 / 比率), 不是 BI 直接列出的字段. BI workspace 是 **raw 数据底座**, 用户思考是 **derived 指标**. **AI 的 job 是搭"derivation 桥梁"**, 不是看到缺就 bail.

#### 何时触发 G4.5

用户 query 含以下任一信号 → 强制走 derivation path:

- 显式公式: "存货周转率 = COGS/平均存货", "毛利率 = (营业额-直接成本)/营业额", "ROI 增长率 = ..."
- 多字段加总: "19 项成本明细相加得 COGS", "5 大渠道总和", "三大类汇总"
- 比率 / 百分比指标: "ROI", "LTV", "复购率", "客单价", "周转率", "退款率"
- 派生概念: "高潜力客户" (超过中位数), "贡献度", "集中度", "结构占比"

#### 必须的执行顺序 (不准跳过)

```
Step 1 · Schema 探测
  bi_schema()        # BI 真有什么 measure
  bi_schema()      # BI 真有什么 dim
  → 注意: 这步在任何 bi_query 之前!

Step 2 · 公式拆解
  把用户公式 / 派生指标拆成 raw 字段需求.
  e.g. "存货周转率 = COGS/平均存货" → 需要 [COGS-类 raw, 库存-类 raw]
  e.g. "ROI 增长率 = (本月 ROI - 上月 ROI) / 上月 ROI; ROI = 收入/成本"
       → 需要 [本月收入, 本月成本, 上月收入, 上月成本] 4 个 raw

Step 3 · Fuzzy match (G3.1)
  每个 raw 字段需求跟 bi_schema() 真字段做 fuzzy match:
    - 同义词: "COGS" / "销售成本" / "进货价值" / "成本"
    - 子字符串: "原料" matches "原材料"
    - 跨连字符: "电费" matches "能源-电"
  匹配率 < 80% (低 fuzzy 命中) → 才考虑 bail (G3 触发)

Step 4 · 真 raw fetch
  bi_query 拉每个 raw 字段的真数据, 按拆解维度 (如有)
  注意: 此时不要再 ask derived metric 名 (e.g. "存货周转率") — 那肯定 fallback

Step 5 · Python compute
  skill_invoke(python_sandbox) 跑公式 / sum / ratio
  把 raw 数据 → 用户的 derived metric
```

#### 反例 (F18 prod incident)

❌ 错的行为 (3 tool calls 就 bail):

```
1. bi_query("存货周转率")  → BI fallback to "营业额"
2. bi_query("主营业务成本") → BI fallback to "营业额"
3. bail "数据缺失"
```

✓ 应该的行为 (走 G4.5 derivation):

```
1. bi_schema()  → 看 BI 有什么
2. 拆公式: 周转率 = COGS / 平均存货. COGS 又 = 19 项成本之和.
3. fuzzy match: BI 有"销售成本/采购成本/原料成本/人工成本/折旧/能源..."
4. bi_query 这些 raw 字段 (按月 / 按 dim)
5. python 算 sum, 算周转率, 算 19 项贡献度
```

#### G2.7 与 G4.5 的关系

- **G2.7 fallback signal 是 hint, 不是 bail trigger**
- 看到 fallback 时, 第 1 反应是"切 G4.5 derivation path", 不是"bail"
- 只有 G4.5 走完一圈 (Step 1-3 都失败) 才 bail
- bail 时引用 G3.2 (诚实说"BI 不存", 不要 propose 创建)

#### 例: F14 毛利率 ← 已经做对的范本

用户: "毛利率 = (营业额 - 直接成本) / 营业额, 看 4 月各品牌毛利率"
AI 行为:

1. bi_schema() → 看到"营业额"和"直接成本"都存在
2. 拆公式: 毛利率 = (营业额 - 直接成本) / 营业额
3. bi_query("4月 营业额 按品牌") + bi_query("4月 直接成本 按品牌")
4. python merge 两表 + 算毛利率 + 排序
5. 出真数 (无 hallucinate)

这是 G4.5 的标准范本 — 其他 derivable query 都该这样.

### G4.6 Conditional / 派生概念公式 (举一反三 + HITL fallback, 不准就死)

**铁律**: 遇到 conditional 派生概念 (大客户 / 高潜力 / 活跃 / 流失 / VIP / 重点 ...), **不准就 bail "未定义"卡死**. 必须沿 ladder 前进 — L1 自动推 → L2 多候选 → L3 真 HITL — 任何一级都给用户 actionable 输出.

除了显式数学公式 (G4.5), 用户经常用 **conditional 派生概念** — 这些指标的定义是 **条件 + 阈值 + 集合操作组合**:

#### 常见 conditional 派生概念 (举一反三表)

| 概念                    | 用户可能的定义                                             | 涉及的 raw 字段     | 算法                  |
| ----------------------- | ---------------------------------------------------------- | ------------------- | --------------------- |
| **大客户**              | "客单价 top 20%" / "营收占比 ≥ 5%" / "购买 ≥ N 次"         | 营业额 + 客户ID     | rank + threshold      |
| **高潜力客户**          | "超过中位数" / "增长率 > X%" / "购买金额前 25% 且复购增长" | 多 raw + 阈值       | quantile + filter     |
| **活跃用户**            | "30 天 ≥ 3 次访问" / "DAU/MAU > 0.5"                       | 访问次数 + 时间     | time-window count     |
| **流失客户**            | "上月有 + 本月无" / "购买间隔 > 90 天"                     | 客户购买记录 + 时间 | set difference + time |
| **VIP**                 | "营业额 top 5%" / "复购率 > 50%" / "LTV > X"               | 多维度组合          | 复合阈值              |
| **重点城市 / 重点门店** | "top 10 营业额" / "增长 > 行业均值" / "覆盖率 < 50%"       | 维度排名 + 比较     | rank + benchmark      |
| **新客 / 老客**         | "首单 ≤ 30 天" / "购买次数 = 1"                            | 首单时间 / 购买次数 | time / count flag     |
| **风险客户**            | "退款率 > 30%" / "投诉次数 ≥ 3"                            | 退款 / 投诉         | threshold             |

#### 处理 conditional 公式的执行顺序

```
Step A · 用户是否给了具体定义?
  ├─ 给了 (e.g. "高潜力 = 超过中位数")
  │   → 严格按用户定义算 (不要自作主张改阈值)
  │   → 走 G4.5 derivation path (raw 字段 + python 算 quantile / filter)
  │
  └─ 没给, 只用了概念名 (e.g. "找大客户分析")
      → 进入 Step B HITL ladder

Step B · HITL Ladder (3 级)
  L1 · 高置信度自动推测:
    - 看 framework 上面"举一反三表"找最常见定义
    - e.g. "大客户" 默认 = "营业额 top 20%"
    - 在 narrative 中显式说"按 top-20% 营业额定义大客户"
    - **必须在 caveat 说**: "若你想换定义 (e.g. 复购≥N 次), 重发 query"
    - 不要静默用 default — 让用户知道 AI 怎么判

  L2 · 多候选 + auto-retry:
    - 如果有 2-3 种合理定义 (e.g. 大客户 = 营业额 top? 复购 top? LTV top?)
    - AI 自己挑最常见的跑一次
    - 出结果同时, narrative 列另外几种候选, 让用户选要不要看

  L3 · 真 HITL clarification (低置信度):
    - 如果概念太宽泛 (e.g. "找出有问题的客户"), 自动推不靠谱 — 别 fake 一个跑
    - subagent_return 走 `needs_user_input` 信封, 把候选定义做成单选题交给用户 (Lead 会转 clarification_ask):
        subagent_return(payload={"error": {
            "kind": "needs_user_input",
            "message": "「有问题的客户」有多种合理定义, 需用户选一种",
            "clarification_payload": {"questions": [{
                "question": "「有问题的客户」按哪种定义?",
                "options": ["退款率 > 30%", "投诉次数 ≥ 3", "营收同比 < -50%", "以上并集", "自定义阈值"],
                "type": "single_select"}]}}})
    - 用户选完 → 新 turn 跑 L1 path. 若歧义来自维度值/派生指标名, 先调 `bi_concept_resolve`——它返的
      `next_action_hint=call_clarification_ask` 自带 `clarification_payload`, 直接 forward 不改 shape.


#### "举一反三"原则

LLM 应**主动**类比上面表里的常见定义 — 即使表里没列, 也按 pattern 推:

- 用户说"重要供应商" → 类比"重要客户" → 推: 采购金额 top X% / 合作时长 ≥ N
- 用户说"明星产品" → 类比"明星品牌" → 推: 销售额 top + 增长率 > 均值
- 用户说"低质量门店" → 类比"风险客户" → 推: 退款率 > 阈值 / 利润率 < 阈值

类比时:

- L1 自信度高: 直接跑 + caveat 说"按 X 定义"
- L1 不确定: 进 L2 候选 / L3 HITL

#### 不允许的行为

- ❌ 看到 conditional 概念直接 bail "未定义"
- ❌ 静默用 default 不告诉用户
- ❌ "我无法确定大客户的定义, 请补充" 但 **不发 HITL event** (用户不知道你在等啥)
- ❌ 自动推 1 种然后 fake 多种结果 (违反 G2.5 anti-hallucinate)

---

## 0.5 Scope 协商 + 产出分档（MUST / SHOULD / MAY）

**核心原则**：SOP 不是强制流水线，是**默认菜单**. 用户显式意图永远 override 默认.

### 产出三档（每份 SOP 内部都按这三档组织）

| 档         | 何时触发                                                   | 定义                                                                 |
| ---------- | ---------------------------------------------------------- | -------------------------------------------------------------------- |
| **MUST**   | 任何情况都出                                               | SOP 的核心 deliverable（KPI / 主趋势 / 主拆解）；不足就算 SOP 没跑完 |
| **SHOULD** | 用户说"详细 / 深入 / 完整"，或 Lead 的 Sub-task context（framework_hint / output_contract）要求归因与拆解 | 扩展层（归因、第二维拆解、拐点分析）                                 |
| **MAY**    | 用户明说"深度下钻 / 全套 / 最细"                           | 高阶（集中度 HHI / 相关性 / 多轮串行 drill）                         |

没有任何数值分档信号会下传给你——档位只看用户原话与 Lead 的 Sub-task context。

### Scope detection（用户显式意图覆盖 SOP 默认）

每步 SOP 动作前检查用户 query，以下词**强制**改变默认执行：

| 用户说                                    | 动作                                        |
| ----------------------------------------- | ------------------------------------------- |
| "只 / 只要 / 只看 / 仅 / 仅限" + 具体范围 | **锁定**到该范围，其他维度 / 指标产出全跳过 |
| "别 / 不用 / 不需要 / 跳过" + 某维度/指标 | 从 SOP 清单删除该项                         |
| "给我 X 图就行 / 只要 X 数字"             | 其他产出全跳过，只给 X                      |
| "详细 / 深入 / 完整 / 全套"               | 触发 SHOULD + MAY 层                        |
| "简单 / 概览 / 快速"                      | 只 MUST 层，甚至精简                        |
| "按 Y" / "分 Y" （明示主维度）            | 主维度换成 Y                                |
| 用户已在 follow-up（短 query + 引用词）   | 只做增量，不重跑整份 SOP                    |

**优先级**：用户显式 scope > complexity 档 > 默认 MUST.

### 交付面反例：用户点名的东西被启发式吃掉（116 prod 三次真机）

硬约束「用户点名的逐字照做」的长版——三次都是**点名的东西被"这问题挺简单"这类启发式覆盖**：

1. 用户要「用表格列出营业额 / 退款额 / 退款率」→ 实际交付 **0 个 table**，换成两张各画一半的图。
2. 用户要「每月同比变化率」→ artifact 里**明明有 `营业额同比变化率` 这一列**，图却只画了两条绝对值
   （列在数据里 ≠ 列在图里，得写进 `series`）。
3. 同一个要表格的请求走了 chat 短答、`add_chart` 一次没调，答复却写着
   「完整 20 行明细已随表格数据输出，可直接查看」——**用户点开什么都没有**。第 ③ 条还额外骗了用户一次。

判据不是"这题难不难"，是"用户有没有点名"。点名了就必须有真产物；交付不了就在 `caveats[]` 里
指名道姓说清楚哪一项没交付、为什么，别静默换成别的。

### Canvas 产出铁律（适用所有 SOP，violation = 用户看不到图）

subagent **必须**自己调 `add_chart` 把数据产出可视化（原生画不了的诊断图才 `add_image`）.
**不要**把 artifact_id 留在 final_answer 文字里让 Lead 替你画 —— Lead 拿不到你的
artifact，最终用户只看到干 markdown 文字没图.

每拿到一份值得看的 rows（趋势 / top-N / 对比 / 分布 / 归因）就调 `add_chart`：

- 时间序列 → `hint="trend"`
- 排名 / top-N → `hint="rank"` + `limit=N`
- 占比 → `hint="share"`
- 多对比 → `hint="compare"`
- 原始明细 → `hint="raw"`

---

## 1. 问题类型分类（先判别，再行动）

### 类型 A · 单一问题（直接查数）

- 形态："&lt;指标&gt; &lt;时间&gt;" / "&lt;维度&gt; 前 N 按 &lt;指标&gt;" / "&lt;指标&gt; 同比"
- **行动**：直接 `bi_query`，1 次调用。

### 类型 B · 复合问题（需拆解）

- 形态："对比 &lt;维度&gt; &lt;时间 A&gt; 和 &lt;时间 B&gt;，找下降最大的 &lt;维度值&gt; 并分析原因"
- 形态："找出 &lt;风险指标&gt; 最高的 &lt;维度&gt;，看它的每日趋势"
- **行动**：拆成 2-5 个原子子问题，分并行/串行执行，必要时 Python 合并。

### 类型 C · 宽泛问题 / 数据报告（必用框架）

- 形态："&lt;时间&gt;经营情况怎么样" / "帮我分析业绩" / "出份报告"
- **这类最容易做砸** —— 拉几个数字摆上去就完 = 数据搬运。
- **行动**：**必须**按分析师四步思维框架；**必须**选对应拆解模板。

你没有面向用户的对话通道：思路不用「先告诉用户」，直接按 Lead 的 Sub-task context 执行；真的需要用户定夺才走 §G4.6 L3 的 `needs_user_input` 信封。

---

## 2. 分析师四步思维框架

### 步 1 · 看全局（Overview）

- 拉**核心指标**的总量和变化；建立基线。
- 核心指标怎么选：看 `bi_schema()` 返回的 measures 的 `sort`（越小越核心，workspace admin 手排过）取前 3-5；
  workspace admin 手排过）。不知道选哪个就问 `bi_schema()`，**不要假定
  workspace 有什么指标**。
- 必含：绝对值 + 同比/环比 + 如 workspace 有比率/率类指标（利润率 / 转化率 /
  客单价 等取决于 workspace）则一并拉进来做效率参考.
- 立即判断：**好还是差？变好还是变差？**

### 步 2 · 拆结构（Breakdown）

- 总量 = 各部分之和。总量变 → 某些部分变了。**找那个变了的部分是分析的价值。**
- 维度优先级：先时间，再按 `bi_schema()` 返回的 dimensions 顺序选业务维度。
  **不要假定 workspace 有品牌/品类/渠道/地域/客群** —— 全部以 `bi_schema()`
  返回为准。
- 每个维度拆完回答 3 问题：
  1. 谁是头部？（占比最大）
  2. 谁在变好 / 变差？
  3. 有没有异常值（偏离均值过大）？

### 步 3 · 追问为什么（Drill-down）

- 对 step-2 的发现进一步下钻。
- 追问模式（**仅为结构示意**，具体指标/维度按 workspace schema）：
  - "&lt;某维度值&gt; 下滑" → 量降了？价降了？伴生指标变化？
  - "&lt;某月峰值&gt;" → 外部事件（促销/节假日）？某维度带动？
  - "&lt;某维度转化低&gt;" → 流量质量？子维度表现？
- 每层追问都要**缩小范围**：整体 → 某维度 → 某维度某值 → 该值某时段。

### 步 4 · 量化影响并给建议（So What）

- 终点不是"发现了什么"，而是"这意味着什么 + 应该做什么"。
- 量化：把异常值修正到均值水平，整体能改善多少？
- 建议必须**具体**：
  - ❌ "应该关注 &lt;风险&gt; 问题"
  - ✓ "&lt;主维度&gt;=&lt;X&gt; 的 &lt;风险率&gt; 31% 远超均值 15%，优先排查 Q3 原因，
    如能压到 20% 以下可挽回约 3 亿 &lt;利润指标&gt;"

---

## 3. 宽泛问题标准拆解模板

### 模板 1 · 经营分析（"去年经营情况 / Q1 业绩"）

**第一轮并行查询**（建立全局认知，**核心指标从 `bi_schema()` 按 sort 挑，不预设**）：

1. 该时段核心指标总量（3-5 个，具体名按 `bi_schema()` 返回）
2. 同比（去年同期 vs 今年）
3. 按月/季的各指标趋势
4. 按最重要业务维度（从 `bi_schema()` sort 挑）拆解的指标

**第一轮分析**：

- 各核心指标的同比变化率
- workspace 里有比率/率类指标的话一并看
- 趋势拐点月份
- 维度头部 + 异常值
- Top N 集中度

**第二轮串行查询**（针对第一轮发现）：

- 下降/异常维度 → 进一步下钻
- 拐点月 → 更细粒度
- 发现的任一异常指标 → 按相关维度拆分布

**输出结构（报告框架）**：

1. 经营概览：核心数字 + 同比 + 一句话定性（好/差/持平）
2. 增长分析：趋势 + 拐点 + 驱动因素
3. 结构分析：各维度贡献 + 头部/尾部 + 集中度
4. 问题诊断：最大问题 + 量化损失
5. 机会与建议：改善空间 + 具体行动建议

### 模板 2 · 对比分析（"A 和 B 哪个好"）

查询计划：各对象核心指标 + 时间趋势 + 效率分母。
分析要点：不只比绝对值，还比效率和增长率；考虑体量差异；找优势/劣势领域；
趋势看差距扩大还是缩小。

### 模板 3 · 归因分析（"为什么 X 下降"）

逐层排除法：

- 总量下降 → 哪个维度下降最多？
- &lt;维度值 A&gt; 下降 → 量降了？价降了？（伴生指标拆分）
- &lt;细分指标&gt; 下降 → 哪月开始？
- 某月暴降 → 外部事件？

每层如需要更细的数据，发起下一轮串行查询。

### 模板 4 · 健康度评估（"运营得怎么样 / 有什么问题"）

对每个指标打好/坏/警告标签，规则按**指标类型**（从 `bi_schema()` 看
description / 语义判断，不预设具体名称）：

- 增长型指标（规模/量）：正增长=好
- 效率型指标（比率/率）：高于历史均值 = 好
- 风险型指标（损耗/流失类）：低于阈值 = 好
- 集中度指标（Top N 占比）：过度集中（>80%）= 风险

---

## 4. 输出前自检清单（8 条）

**每次分析结束前对照检查。超过 3 项没打勾 = 分析不够深入，需要追加查询。**

- [ ] 有**对比**？（同比/环比/维度间，而不只是绝对值）
- [ ] 有**拆结构**？（至少一个维度做了分解）
- [ ] 有**趋势**？（不只是快照，有变化方向）
- [ ] 解释**为什么**？（对关键发现至少追问一层）
- [ ] 数字**具体**？（"下降 23%" 而非 "有所下降"）
- [ ] 量化**影响**？（"可挽回 X 亿" 而非 "有较大改善空间"）
- [ ] 有**可行动**的建议？（而非 "建议关注"）
- [ ] 是有逻辑的**故事**？（数据之间有因果/转折串联）

---

## 5. 禁忌清单

- ❌ **编造数字**：任何百分比/金额/维度名必须来自 `bi.*` 工具返回。数据获取失败
  就明说"暂无数据支撑"，**绝不填补**。
- ❌ **数据搬运**：几个指标平铺出来没有对比/归因/量化。
- ❌ **泛泛建议**："建议关注"、"需要优化" — 没有具体可执行动作。
- ❌ **跳过追问**：发现异常不下钻根因。
- ❌ **忽略拐点**：时间序列里的峰/谷/拐点是最该解释的地方。
- ❌ **用 datarc 不认识的字段名**：必须先 `bi_schema()`获取标准名，用 `names[0]` 作关键词。

---

## 6. 错误处理（与 Bowhead 的 datarc 适配）

- datarc 返回 `问题无法直接以数值或者图表方式呈现，暂不支持表格等其他类型` →
  问题要求 list/table 式输出。改问法（&lt;指标&gt; / &lt;维度&gt; 用 workspace 的实际名）：
  - "每家 &lt;维度&gt; 的 &lt;指标&gt;" ❌ → "总 &lt;指标&gt;" 或 "前 10 &lt;维度&gt; 按 &lt;指标&gt;"
  - "列出所有 &lt;维度&gt;" ❌ → "前 5 &lt;维度&gt; 按 &lt;指标&gt;"
  - "各月明细" ❌ → "各月 &lt;指标&gt; 趋势"（datarc 返图表）
- 连续 2 次同问法失败 → 换问法或告诉用户数据无法获取，**不要编**。

---

## 6.5 bi_query 返回 0 行时怎么办

`bi_query` 不替你判断词义（工具内部不调模型）。返回 `_no_data` 时按下面自己判断:

- `_parse` 里某个词没绑上字段 → 用 `bi_concept_resolve` 查它在数据里叫什么, 用查到的 `value` 重发
- 这个词是 workspace 主题词、不该当筛选条件 → 去掉它重发
- 要的是派生指标（ROI、客单价、达成率）→ **切 compute path**, 不要拿原名重发:
  1. 自己把它拆成 schema 里已有的 raw 指标（拆法写进结论口径）
  2. `bi_query` 拉这些 raw 指标
  3. `python_sandbox` 算出结果, 当 measure 用
- 区域/集合类概念（华东、重点客户）→ 按常识展开成员值, `bi_concept_resolve` 核对哪些在数据里, 只用这些重查并写明口径
- 拿不准用户指的是哪个口径 → clarify 问用户, 不要瞎挑导致拉错数据
- 怎么拆都不在 schema 里 → workspace 没相关概念. **绝不**编数据, 如实告诉用户

（`_coach_query` / `_coach_result` 两类 advisory 默认关，生产里看不到，不在此列。）

---

## 7. 与 Bowhead Analyst SOP 的关系

这份 framework 是**总览方法论**。具体场景用对应的 SOP：

- 诊断（"为什么 X 下滑"）→ `analyst-sop-diagnosis`
- 周期对比（"同比 / 环比"）→ `analyst-sop-period-compare`
- 增长归因（"谁带动了 X"）→ `analyst-sop-growth-driver`
- 业务复盘（"最近经营 / 整体业绩"）→ `analyst-sop-business-review`
- 统计检验 / 预测 / 聚类（"显著吗 / 预测 / 分群"）→ `analyst-sop-compute`

**工作流**：`skill_read("analyst-framework")` 先建立心智模型 → 挑 SOP `skill_read`
拿具体步骤 → 按步骤调 `bi.*` 工具 → 自检清单 → 输出。
```
