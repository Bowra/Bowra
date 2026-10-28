# AnalysisPayload — 完整 JSON Schema 参考

> 这是 `subagent_return(payload=...)` 的字段定义. 跟 `bowhead_core.analysis_payload.AnalysisPayload` 一对一.

## 顶层：两种互斥模式

### Chat 短答

仅用于单值 lookup、3-5 个查数或最多 6 个 KPI 的小对比，且用户没有明确要图/报告：

```json
{
  "delivery_surface": "chat",
  "exec_summary": "19 × 29 = 551",
  "kpis": [{ "label": "19 × 29", "value": "551", "sentiment": "neutral" }]
}
```

约束：`exec_summary` 必填且 ≤600 字，`kpis` 可省略且 ≤6；必须省略
`title`、`eyebrow`、`insights`、`insight_section_ids`、`recommendations`、`narrative`、`findings` 与
`multi_source_compare`。不得带 `error` 或 deliverable，也不得先调用 `canvas_add_*`。

### Report

| 字段               | 类型             | 必填 | 说明                                            |
| ------------------ | ---------------- | ---- | ----------------------------------------------- |
| `delivery_surface` | `"report"`       | —    | 可省略；显式写时只能为 `"report"`               |
| `title`            | string           | ✅   | 报告 H1 标题. 例 "EA 单据合规监控（2026年4月）" |
| `eyebrow`          | string \| null   | —    | H1 之上的小字. 例 "合规监控" / "经营复盘"       |
| `exec_summary`     | string           | ✅   | 30-200 字结论先行. 含核心数字 + 主因 + 行动方向 |
| `kpis`             | Kpi[]            | ✅   | 顶部 3-5 个数字卡                               |
| `insights`         | Insight[]        | ✅   | 3-5 个 MECE 洞察（schema 限制 1-7）             |
| `recommendations`  | Recommendation[] | ✅   | 行动建议列表                                    |
| `caveats`          | string[]         | —    | 全局数据局限. 强烈建议非空                      |
| `sources`          | Source[]         | —    | 数据源条目                                      |

## Kpi

| 字段        | 类型                         | 必填 | 说明                                       |
| ----------- | ---------------------------- | ---- | ------------------------------------------ |
| `label`     | string                       | ✅   | 指标名. 例 "整体合规率"                    |
| `value`     | string                       | ✅   | 已格式化的字符串. 例 "11.39%" / "84,037"   |
| `delta`     | string \| null               | —    | 对比说明. 例 "vs 行业 -73.6pp" / "MoM +2%" |
| `sentiment` | "good" \| "bad" \| "neutral" | —    | 着色. 默认 "neutral"                       |

## Insight

| 字段          | 类型                     | 必填 | 说明                                                 |
| ------------- | ------------------------ | ---- | ---------------------------------------------------- |
| `title`       | string                   | ✅   | h2 标题. 例 "部门支出是合规率结构性拖累"             |
| `thesis`      | string                   | ✅   | 1 句论点（≤50 字）. 例 "占 90% 单量但合规率仅 1.85%" |
| `evidence`    | EvidenceBlock[]          | ✅   | 1-5 个证据块（schema 严格）                          |
| `narrative`   | string                   | ✅   | 30-1200 字综合解读. 把 evidence 串成业务洞察         |
| `impact`      | Impact \| null           | —    | 量化业务影响                                         |
| `confidence`  | "high" \| "med" \| "low" | —    | 默认 "med"                                           |
| `source_refs` | string[]                 | —    | artifact_ids（可选）                                 |

## EvidenceBlock

| 字段                      | 类型                                             | 必填                      | 说明                                    |
| ------------------------- | ------------------------------------------------ | ------------------------- | --------------------------------------- |
| `kind`                    | "chart" \| "computation" \| "table" \| "callout" | ✅                        | 证据类型                                |
| `caption`                 | string                                           | ✅                        | 1 句"这块在说什么"                      |
| `chart_section_id`        | string \| null                                   | 当 kind=chart 时 ✅       | `add_chart` 返的 section_id             |
| `computation_artifact_id` | string \| null                                   | 当 kind=computation 时 ✅ | `python_sandbox` 返的 artifact_id       |
| `table_artifact_id`       | string \| null                                   | 当 kind=table 时 ✅       | `bi_query` 返的 artifact_id             |
| `callout_text`            | string \| null                                   | 当 kind=callout 时 ✅     | 文字结论. 例 "若 X 提至 Y → 整体可达 Z" |
| `subcaption`              | string \| null                                   | —                         | 进一步细节                              |
| `display_hint`            | "full_width" \| "half" \| "third" \| "collapsed" | —                         | 默认 "full_width". 同行 2 个图用 "half" |

**关键约束**：

- 同一 evidence 只能填一个 ref 字段（`chart_section_id` ××× `table_artifact_id`）
- `kind` 决定要填哪个字段；填错或漏填都会 schema 失败

## Impact

| 字段        | 类型                      | 必填 | 说明                                     |
| ----------- | ------------------------- | ---- | ---------------------------------------- |
| `quantity`  | number \| null            | —    | 数值大小. 例 16.5 / 66224                |
| `unit`      | string \| null            | —    | 单位. 例 "pp" / "单" / "元" / "%" / "倍" |
| `direction` | "+" \| "-" \| "=" \| null | —    | 方向                                     |
| `timeframe` | string \| null            | —    | 例 "若部门提至 30%" / "1 个月内"         |
| `note`      | string \| null            | —    | 说明. 例 "杠杆点测算"                    |

**全部 optional** —— 但建议至少填 quantity + unit + direction，否则只是空话.

## Recommendation

| 字段              | 类型                 | 必填    | 说明                                    |
| ----------------- | -------------------- | ------- | --------------------------------------- |
| `priority`        | "P0" \| "P1" \| "P2" | ✅      | 优先级                                  |
| `action`          | string               | ✅      | 1 句话动作. 例 "OA 系统强制 X 字段必填" |
| `owner`           | string \| null       | P0 必填 | 负责人 / 部门. 例 "IT 系统组"           |
| `timeline`        | string \| null       | —       | 例 "48 小时" / "Q3 完成"                |
| `expected_impact` | string \| null       | —       | 预期效果                                |
| `rationale`       | string \| null       | —       | 为什么这样做                            |

## Source

| 字段          | 类型           | 必填 | 说明                                        |
| ------------- | -------------- | ---- | ------------------------------------------- |
| `label`       | string         | ✅   | 数据条目名                                  |
| `tool`        | string         | ✅   | 来源 tool. 例 "bi_query" / "python_sandbox" |
| `artifact_id` | string \| null | —    | 关联 artifact_id                            |
| `query`       | string \| null | —    | 原始 query 文字                             |

## Validation 严格度

| 规则                                    | 严格度      |
| --------------------------------------- | ----------- |
| 顶层 5 个 required 缺一                 | ❌ 必失败   |
| `len(insights)` ∉ [1, 7]                | ❌ 必失败   |
| `len(insight.evidence)` ∉ [1, 5]        | ❌ 必失败   |
| `len(insight.narrative)` ∉ [30, 1200]   | ❌ 必失败   |
| `evidence` 同时填两个 ref 字段          | ❌ 必失败   |
| `kind=chart` 但 `chart_section_id` 空   | ❌ 必失败   |
| `recommendation.priority` 不在 P0/P1/P2 | ❌ 必失败   |
| P0 但 `owner` 空                        | ❌ 必失败   |
| optional 字段缺失                       | ✅ 自动默认 |

## 错误返回示例

```json
{
  "ok": false,
  "error": "payload schema invalid:\n  - insights[0].evidence count=0 not in [1,5]\n  - recommendations[1] is P0 but owner missing",
  "validation_errors": ["insights[0].evidence count=0 not in [1,5]", "recommendations[1] is P0 but owner missing"]
}
```

收到此错误 → 看 `validation_errors`、改 payload、**再调一次** subagent_return（最多 3 次）.
