---
name: analyst-sop-output-contract
description: "Pack a bounded Chat answer or a report AnalysisPayload via subagent_return. FINAL step after any analyst-sop-* SOP."
version: 1.2.0
trigger_examples:
  - "subagent 跑完 business-review SOP，准备返 Lead 前"
  - "subagent 跑完 compute SOP，已经画完所有图"
  - "subagent 跑完 diagnosis SOP，已经分析出根因"
pipes_output_to: null
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Analysis output contract"
title_zh: "分析产出契约"
category: analysis
---

# Output Contract — Pack Analysis as Structured Payload

> 你跑完任何 `analyst-sop-*` 后的**最后一步**。先选择 Chat 短答或 Report；只有 Report 才进入 canvas composer。

---

## 0 · 何时调用

任何 subagent 跑完分析准备结束时，**最后一个 tool 调用**应该是：

```
subagent_return(payload={...AnalysisPayload schema...})
```

先确定唯一交付面：

- **交付面是你的决定，`delivery_surface` 必填**（Lead 在 Sub-task context 给了 `delivery` 就照办——那是用户原话或记住的偏好）。
- **Chat 短答**：聊天里一个答复能说清的（数字 / 结论 / 小对比）。返回 `{delivery_surface: "chat", exec_summary, kpis?, insights?, caveats?}`；
  只有 exec_summary 必填，其余是你想在聊天里说的内容；要带图就 `add_chart`（系统内联进气泡，几张你定）。
  判断依据：长文在聊天里读着费力——几百字、几个数、一两张图说得清的是 chat；要分章节、要反复翻看的就别硬塞进 chat。
- **Report**：要结构、多维解释、洞察与行动建议、用户之后要翻看/导出的。`delivery_surface: "report"`，沿用下文完整报告契约。
- 两种交付面下 sandbox 的 print、Markdown、CSV 与其他文件都由系统保留在「数据」。

**调用时机**：

- ✅ 你已经调过 `bi_query` 拿数据
- ✅ Report 模式下，你已经调过 `add_chart` 画了所有该画的图
- ✅ 复杂计算（中位数 / 分位 / 集合差）你已经调过 `python_sandbox`
- ✅ 你已经形成了对问题的**洞察**（不只是"拉到了哪些数字"）

**调完之后**：subagent loop 立即退出，不要再调任何工具，不要再写 free-form markdown.

---

## 1 · 铁律（第 2–11 条仅适用于 Report）

1. **payload 是最后一步**：调完 `subagent_return` 即结束，**不要**再调工具
2. **chart_section_id 必须真实**：你前面 `add_chart` 调用返过的 `section_id` 才能写进 evidence；不要编 ID
3. **insights MECE**：3-5 个互斥穷尽的洞察，**不要**罗列 10+ 个琐碎点
4. **每个 insight 必须有 evidence**：至少 1 个 chart/computation/table/callout 类 evidence；纯文字洞察不接受
5. **narrative 30-1200 字**：低于 30 不算解读；按论点自然长度铺开，不用为省字数牺牲解读
6. **P0 必须有 owner**：紧急事项不指派负责人就是空话；填部门 / 角色都行
7. **不要编数字**：所有 KPI / impact / narrative 里的数字都得来自 `bi.*` 工具或 `python_sandbox` 的真实返回
8. **JSON 严格语法**（!!!）：
   - 用 `null` / `true` / `false`，**不要** Python 的 `None` / `True` / `False`
   - 字符串里中文 OK，但**不要**在 payload JSON 之后再写说明文字（"以上是 payload..." 那种）
   - **不要**用单引号；必须双引号

9. **保留用户指定的报告骨架（数量约束）**：

   用户原 query 给出**结构化 N 段** outline 时，insights 数量与命名**必须**对应它，不要自由发挥. 触发词模式:
   - "&lt;X&gt;—&lt;Y&gt;—&lt;Z&gt; N 层 / N 段 / 递进 / 拆解"
   - "&lt;X 子集&gt; vs &lt;Y 子集&gt; 分别看"
   - "先 &lt;A&gt; 后 &lt;B&gt;"
   - "分 N 部分: &lt;A&gt; / &lt;B&gt; / &lt;C&gt;"

   规则:
   - 用户说 N 层 → insights ≥ N，且每条 title 对应用户给的层名（不要自创新名字盖过去）
   - 用户说 "&lt;A&gt; vs &lt;B&gt; 分别看" → 至少 2 条 insight，每条只看一个子集
   - 多余的洞察可以追加，但不能少于用户指定的层数
   - **绝不**: 把用户结构化的 N 段需求 collapse 成 1 条 "汇总 insight"
   - 注: 这条是**数量约束**, 跟 §1.10 的 inline 写法互补, 见下条

10. **insights 一律 inline 写进 `payload.insights[]`**：chunk 式的洞察写入工具不对 compute 开放，
    `insight_section_ids` 留空（不填）。每条 insight 的 evidence 引用你前面 `add_chart` 真返过的 `section_id`。
    一次写全、JSON 严格；3-5 条 insight × 100-400 字是正常体量，不要为了"省字"砍解读，也不要为了凑数写空壳。

11. **空壳禁令**（M137 follow-up，最关键）：

- **绝不允许**用占位符填 KPI value / impact / narrative / thesis：
  ❌ `"value": "待计算"` `"value": "暂无"` `"value": "N/A"` `"value": "TBD"`
  ❌ `"narrative": "数据加载后补充"` `"thesis": "待数据校验"`
- **没拿到真数据 = 不要 pack 报告 payload**. 工具失败 / sandbox 挂 / BI 返空 / 任意 blocker：
  → 改走 **bail 信封**：`subagent_return(payload={"error": {"kind": "<domain_mismatch|ambiguity|needs_user_input|transient|tool_failure>", "message": "<1-2 句：哪步挂了 + 为什么>"}})`
  → Lead 据 kind 决定 prose 答 / 追问用户 / 重试 / 显错，**绝不**编一个"看板设计 framework"骗用户
  （`insufficient_data:` 只是运行时在你超时/强退时**自己合成**的兜底文案，不是你该返的形状）
- 看你的 KPI: 任一数字字段值不是真数 → **直接放弃 payload 路径**
- **insight 必须基于真数据**：thesis/narrative 引用具体数（"45.6%"、"678 单据"），不能写"设计逻辑/规则对齐/维度覆盖"这种元描述当 insight

---

## 2 · Schema 速查

完整定义见 `references/payload-schema.md`. 这里是**记忆口诀**：

```
AnalysisPayload {
  delivery_surface   → Chat 短答必填 "chat"；Report 可省略或填 "report"
  // Chat: exec_summary 必填, kpis/insights/caveats 可选（是聊天里要说的内容, 不套报告校验）
  title             → 报告 H1（必填，例 "EA 单据合规监控（2026年4月）"）
  eyebrow           → H1 之上小字（可选，例 "合规监控"）
  exec_summary      → 30-200 字结论先行（必填，金字塔顶层）
  kpis[]            → 3-5 个核心数字卡（必填）
    {label, value, delta, sentiment: "good"|"bad"|"neutral"}
  insights[]        → 3-5 个 MECE 洞察（必填，1-7 范围）
    {
      title         → h2 标题
      thesis        → 1 句论点（≤50 字）
      evidence[]    → 1-5 个证据块
        {kind: "chart"|"computation"|"table"|"callout", caption, *_id}
      narrative     → 100-400 字综合解读（30-1200 范围）
      impact        → 量化业务影响（可选）
        {quantity, unit, direction, timeframe, note}
      confidence    → "high"|"med"|"low"（默认 "med"）
      source_refs[] → artifact_ids（可选）
    }
  recommendations[] → 行动建议（**可选**，按 P0/P1/P2 分级）
                      ⚠ 这里填的**不会**被渲成画布上的一节——它是给 Lead 写对话回复用的结构化证据。
                      要让建议出现在报告里，写进你的正文（末尾 `## 建议`）。
    {priority: "P0"|"P1"|"P2", action, owner, timeline, expected_impact, rationale}
  caveats[]         → 全局数据局限（可选）
                      同上，不会单独渲成一节；真正的局限写进正文。
                      （系统会自己补两类你写不出来的：数字没有工具依据的指控、
                        以及计划里某一步失败时的说明。）
  sources[]         → 数据源条目（可选）
}
```

## 3 · 选 evidence kind 的口诀

```
你拿到了什么 → 用哪个 evidence kind
─────────────────────────────────────────────────────────────────
图表（已 add_chart） → kind="chart" + chart_section_id="sec_xxx"
python 算出的【单值衍生指标】（中位/CV/HHI/集合差/相关系数等）
                       → kind="computation" + computation_artifact_id
没法画图但要展示 50 行明细表 → kind="table" + table_artifact_id
单独的小结论或杠杆点测算（没图也没原始数据） → kind="callout" + callout_text="..."
```

⚠️ **Report 模式铁律 (prod-incident-driven, 2026-05)**: **多行聚合数据** (per-category /
per-period / per-entity 的对比、占比、排名、趋势) **必须**先调 `add_chart`
渲染成图, 再以 `kind="chart"` 写 evidence. 不要图省事直接 `kind="computation"`
喂个 artifact_id 让用户自己点开 Data tab 看数据 — 那不是报告, 那是甩锅.

判定捷径:

- Report 模式数据是 **N 行 × M 列**, N ≥ 2 → **必图** (add_chart → kind="chart")
- 数据是 **1 行 × 1 列** (单值, 比如"HHI = 0.42") → kind="computation"
- 数据是 **N 行 × M 列**, 但 N > 30 且不是趋势/排名/占比 → kind="table"

**反例** (本 SOP 修订前的常见 bug — 已修):

```json
// ❌ 各省份 GMV 同比贡献度 是多行聚合, 不是单值衍生
{"kind": "computation", "computation_artifact_id": "art_xxx",
 "caption": "各省份2025Q3 GMV同比贡献度"}

// ✅ 应先 add_chart(art_xxx, hint="rank") → 拿到 sec_yyy
{"kind": "chart", "chart_section_id": "sec_yyy",
 "caption": "各省份2025Q3 GMV同比贡献度"}
```

**Note**: 一个 evidence 块**只填一个** ref 字段（chart_section_id ××× / computation_artifact_id ××× / table_artifact_id ××× / callout_text ×××），不要同时填两个.

---

## 4 · Common Pitfalls

### 4.1 ❌ 编造 chart_section_id

```json
{ "kind": "chart", "chart_section_id": "sec_made_up" }
```

✅ 必须用你前面 `add_chart` 真返过的 ID（看 tool_result）.

### 4.2 ❌ insight 没有 evidence

```json
{
  "title": "市场份额下滑",
  "thesis": "连续三月下降",
  "evidence": [],  // 空 → schema 拒收
  ...
}
```

✅ 至少给 1 个 chart 或 callout：

```json
"evidence": [{"kind": "callout", "callout_text": "MoM -3pp / -5pp / -7pp", "caption": "下滑趋势"}]
```

### 4.3 ❌ narrative 太短

```json
"narrative": "下滑了"   // 长度 3，schema 要求 ≥30
```

✅ 综合 evidence 写解读（50-200 字甜蜜区）：

```json
"narrative": "三月度连续下滑、MoM 加速恶化（-3pp→-5pp→-7pp）。从拆解看核心拉力是华南市场（贡献 60% 跌幅），但东北区出现反向增长机会（MoM +2pp）—— 应优先稳华南."
```

### 4.4 ❌ P0 没填 owner

```json
{ "priority": "P0", "action": "立即修复" } // 缺 owner
```

✅ P0 必须能落地：

```json
{ "priority": "P0", "action": "立即修复 X 字段空值", "owner": "IT 系统组", "timeline": "48小时" }
```

### 4.5 ❌ 同一 evidence 填多个 ref

```json
{ "kind": "chart", "chart_section_id": "sec_a", "table_artifact_id": "art_b" }
```

✅ 一个 evidence 一个性质，按 kind 选对应字段：

```json
{ "kind": "chart", "chart_section_id": "sec_a" }
```

要展示明细表 → 单独再加一个 evidence：

```json
{ "kind": "table", "table_artifact_id": "art_b" }
```

### 4.6 ❌ caveats 不填或编

```json
"caveats": []   // 数据真的零局限？很少有
```

✅ 老实填. 例如：

```json
"caveats": [
  "样本仅 4 月单月数据，趋势结论需多月验证",
  "维度 X 颗粒度 400+，TOP5 不代表全分布"
]
```

---

## 5 · Recovery —— validation 失败怎么办

`subagent_return` 调完返 `{"ok": false, "error": "...", "validation_errors": [...]}` 时：

1. 看 `validation_errors` 列表 —— 每条说哪个字段错（"insights[0].evidence count=0"、"recommendations[1] is P0 but owner missing"）
2. 改 payload 修一下，**再调一次** `subagent_return`
3. 最多重试 2 次（共 3 次），第 3 次还失败就改返 bail 信封：`{"error": {"kind": "tool_failure", "message": "payload 三次校验未过: <最后一条 validation_error>"}}`，不要再硬凑

---

## 6 · 完整 Example

3 个真实场景的完整 payload 在子文档 `examples.md` 里. 怎么读：

```
skill_read(name="analyst-sop-output-contract", ref="examples")
```

包含：

- `EA-monitoring`：合规监控看板（report 类）
- `GMV-causal`：为什么 GMV 下滑（causal 类）
- `median-cohort`：超过中位数的医院（compute 类）

详细字段语义同样可读 `ref="payload-schema"` 拿完整 schema 表格. 速查表见上面 §2 已够用，
仅在 schema validation 反复失败时再深读完整 schema.

---

## 7 · Self-check（pack 前过一遍）

Chat 短答只检查：

- [ ] `delivery_surface == "chat"` 写了；这答复在聊天里读得下去（长了就该选报告）
- [ ] 想让用户在聊天里看到的图都 `add_chart` 了
- [ ] 数字都来自真实 tool 返回；sandbox 产物留在「数据」

Report 再检查以下全部项目：

- [ ] insights 数量 ∈ [3, 5]
- [ ] 每个 insight 至少 1 个 evidence
- [ ] 每个 chart evidence 的 chart_section_id 真实存在（你前面 add_chart 返过的）
- [ ] narrative 至少 30 字
- [ ] 有可执行的行动才写 recommendations；写了就每条带 action，P0 带 owner
- [ ] 该说的数据局限写进了正文（不是只塞进 caveats）
- [ ] exec_summary 含数字 + 主因 + 行动方向（不是泛泛"业绩有压力"）
- [ ] 所有数字都来自真实 tool 返回，没编
- [ ] ⭐ **用户结构化要求都对得上**: query 里说的"X-Y-Z 三层"/"X vs Y 分别看"
      在 insights 里有对应 section
- [ ] ⭐ **业务规则条件分支不被 collapse**: 用户给"X 要 A，Y 要 B"类规则,
      合规率 / 命中率 必须**按子集分别算**, 报告里有显式分组
- [ ] **数字写法**：所有文本字段写纯散文数字（"GMV 5.2 亿元" / "12.3%"），**不写** `[num:...]` 标记（已退役）；
      数字都能在 tool 真返里找到，溯源编号由后端确定性绑定。

不全打勾就不要 return.

---

## 8 · 数字写法规范

`[num:VALUE:UNIT_TYPE]` 标记语法**已退役**（citation-audit M1-0）：写了也会被后端 bake 掉，不要再用。

- ✅ `"exec_summary": "Q3 营收 34.71 万元，同比下降 5.0%。"` / `"value": "5.37 次"`
- ❌ `"exec_summary": "Q3 营收 [num:347100:CNY]"`
- 数字必须来自 tool 真返（`bi_query` 的 `sample_rows` / `metrics`，或 `python_sandbox` 的 stdout）；后端按 tool 真值把数字确定性绑定到溯源编号 `[N]`，你不用写任何标记。
- 派生量（占比/同比/户均）先在 sandbox 算出来并登记进 `ground_facts`，再照账写（见 compute prompt G2.5.1）。
