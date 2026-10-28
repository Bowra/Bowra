# Dashboard Layer Templates — 代码片段

Use these snippets in `skill_invoke(python_sandbox)`. Replace column names with what you detected in Schema Detection.

## Layer 1 · Overview KPIs

```python
import pandas as pd

df = pd.read_csv(INPUTS["data"]["path"], encoding="utf-8-sig")

# 核心 KPI（调整列名）
total = len(df)
# 合规率（基于业务规则重算 —— 见 business_rule_patterns.md）
is_compliant = _compute_compliance(df)  # 替换为你的规则 mask
compliance_rate = is_compliant.mean()

# 其它常用：重点管控占比 / 主要状态分布
key_managed = df["对公重点管控程度"].str.contains("重点管控", na=False).mean()
approved = (df["审批状态"] == "END_APPROVAL").mean()

# stdout 方便 Lead 引用
print(f"总单数: {total}")
print(f"整体合规率: {compliance_rate:.1%}")
print(f"重点管控占比: {key_managed:.1%}")
print(f"审批完成比例: {approved:.1%}")
```

然后**前端 kpi_row 的 cell 由 narrative 直接写**（数字来自 stdout），不用单独 add_chart.

## Layer 2 · Grouped breakdown

```python
# 主分组维度（体系 / 部门 / 品类 / 门店 ...）
dim = "申请人体系归属"  # 替换

grouped = (
    df.groupby(dim)
    .agg(
        n=("单据编号", "count"),
        compliant_n=("_compliant", "sum"),  # 假设 step 1 里把 mask 存成 column
        rate=("_compliant", "mean"),
    )
    .reset_index()
)
grouped = grouped.sort_values("rate", ascending=True)  # 最差在前

# 裁两列保证 add_chart 能画出柱图（防降级 table）
chart_df = grouped[[dim, "rate"]].rename(columns={"rate": "合规率"})
chart_df.to_csv("group_rate.csv", index=False)

print("## 分组合规率（升序）")
print(grouped[[dim, "n", "compliant_n", "rate"]].to_string(index=False))
```

然后：

```
add_chart(
    data_artifact_id="<group_rate.csv artifact_id>",
    hint="rank",
    title="<dim> 合规率排名",
    sort_by="合规率",
    limit=20,
)
```

## Layer 3 · Business rule compliance + violation samples

参考 [../../analyst-framework/references/business_rule_patterns.md](../../analyst-framework/references/business_rule_patterns.md) 选合适 pattern.

```python
# 例：Pattern B 多字段 AND
is_proj = df["支出分类"].str.contains("项目支出", na=False)
has_purchase = df["请购ID明细"].notna() & df["请购ID明细"].astype(str).str.strip().ne("")
has_proj_id = df["立项项目ID编码"].notna() & df["立项项目ID编码"].astype(str).str.strip().ne("")
proj_compliant = is_proj & has_purchase & has_proj_id

# 合规率
proj_rate = proj_compliant[is_proj].mean() if is_proj.sum() else 0.0
print(f"项目支出合规率: {proj_rate:.1%} (N={is_proj.sum()})")

# 违规样本
violations = df[is_proj & ~(has_purchase & has_proj_id)]
print(f"项目支出违规: {len(violations)} 条")

# 按"无请购原因"聚合 top 违规模式
if len(violations) and "无请购原因明细" in violations.columns:
    top_reasons = violations["无请购原因明细"].value_counts().head(5)
    print("Top 5 违规原因：")
    print(top_reasons.to_string())

# 样本 CSV 供 data_table 展示
violations[["单据编号", "申请人姓名", "申请人体系归属", "支出分类", "请购ID明细", "立项项目ID编码"]].head(20).to_csv(
    "violations_sample.csv", index=False
)
```

然后：

```
add_chart(
    data_artifact_id="<violations_sample.csv artifact_id>",
    hint="raw",
    force_kind="table",
    title="违规样本（前 20）",
)
```

## SHOULD Layer 1 · Drill-down worst group

```python
worst_group = grouped.iloc[0][dim]
sub = df[df[dim] == worst_group]
sub_by = (
    sub.groupby("申请人部门名称")
    .agg(
        rate=("_compliant", "mean"),
        n=("单据编号", "count"),
    )
    .sort_values("rate")
    .head(10)
    .reset_index()
)
sub_by[["申请人部门名称", "rate"]].to_csv("worst_group_drill.csv", index=False)
```

`add_chart(hint="rank", title=f"{worst_group} 内部部门合规率")`

## SHOULD Layer 2 · Monthly trend

```python
if "日期" in df.columns:
    df["月份"] = pd.to_datetime(df["日期"], errors="coerce").dt.to_period("M").astype(str)
    trend = df.groupby("月份").agg(rate=("_compliant", "mean")).reset_index()
    trend.to_csv("monthly_trend.csv", index=False)
```

`add_chart(hint="trend", column_types={"月份": "date"}, title="月度合规率趋势")`

## SHOULD Layer 3 · Cross-tab heatmap

```python
cross = pd.crosstab(df["申请人体系归属"], df["无请购原因明细"])
# 转宽表为长表供 heatmap
cross_long = cross.stack().reset_index()
cross_long.columns = ["体系", "原因", "计数"]
cross_long.to_csv("cross_tab.csv", index=False)
```

`add_chart(hint="composition", title="体系 × 违规原因 分布")`

## Gotchas

- 数据里 `_compliant` 列要你先算出来存进 df；Layer 2/3 引用这个 column
- Layer 2 裁列很重要：不裁的话 35 列数据传进去会被判 table
- 违规样本超过 20 行要 head(20) + 加 subCaption "已截断，全量在 artifact"
- `groupby().rate` 若 na_ratio 高（某组全空）会出 NaN → 用 `.fillna(0)` 避免前端 crash
