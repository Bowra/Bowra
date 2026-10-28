# Business Rule Patterns — 用户规则 → pandas mask

**When**: user says "X 要同时有 Y 和 Z" / "X ≥ N 算 Y" / "X 但不 Y". **Must** translate to pandas boolean mask; **never** rely on pre-computed columns in the data.

Why: pre-computed fields may be stale / wrong / absent. Regenerate from raw fields you control.

---

## Pattern A · Required field non-null

> "部门支出要关联请购"

```python
is_dept = df["支出分类"].str.contains("部门支出", na=False)
has_purchase = df["请购ID明细"].notna() & df["请购ID明细"].astype(str).str.strip().ne("")
dept_compliant = is_dept & has_purchase
print(f"部门支出 合规数 = {dept_compliant.sum()} / {is_dept.sum()}")
print(f"合规率 = {dept_compliant[is_dept].mean():.1%}")
```

**Gotcha**: `df['col'] != ''` 漏掉 NaN；用 `.notna() & ...ne('')` 组合更稳.

---

## Pattern B · Multi-field AND

> "项目支出要同时有请购 + 立项"

```python
is_proj = df["支出分类"].str.contains("项目支出", na=False)
has_purchase = df["请购ID明细"].notna() & df["请购ID明细"].astype(str).str.strip().ne("")
has_proj_id = df["立项项目ID编码"].notna() & df["立项项目ID编码"].astype(str).str.strip().ne("")
proj_compliant = is_proj & has_purchase & has_proj_id
```

输出违规样本供核验：

```python
violations = df[is_proj & ~(has_purchase & has_proj_id)]
violations[["单据编号", "申请人", "支出分类", "请购ID明细", "立项项目ID编码"]].head(20).to_csv("proj_violations.csv", index=False)
```

---

## Pattern C · Threshold + aggregation

> "近 30 天购买 ≥ 3 次算活跃；≤ 1 次算流失风险"

```python
import pandas as pd

cutoff = pd.Timestamp.now() - pd.Timedelta(days=30)
recent = df[pd.to_datetime(df["日期"]) >= cutoff]
counts = recent.groupby("用户ID").size()
active_users = counts[counts >= 3].index
churn_risk = counts[counts <= 1].index
print(f"活跃用户: {len(active_users)}")
print(f"流失风险: {len(churn_risk)}")
```

---

## Pattern D · Set difference (no X, yes Y)

> "买了 A 没买 B"

datarc NL 对否定词支持差 → **分开查 A / B 两次，python 本地差集**.

```python
bought_A = set(df[df["产品"] == "A"]["用户ID"].unique())
bought_B = set(df[df["产品"] == "B"]["用户ID"].unique())
only_A = bought_A - bought_B
print(f"只买 A 不买 B: {len(only_A)}")
# 取样本供输出
sample = df[(df["产品"] == "A") & df["用户ID"].isin(only_A)].head(20)
sample.to_csv("only_A_users.csv", index=False)
```

---

## Pattern E · 中位数 / 分位阈值

> "购买量超过中位数的"

```python
median = df["购买量"].median()
above_median = df[df["购买量"] > median]
print(f"中位数 = {median}，超出者 N = {len(above_median)}")
```

---

## Pattern F · ratio / 倍数差距

> "max/min 差距 > 10 倍"

```python
stats = df.groupby("门店")["销量"].agg(["max", "min"])
stats["ratio"] = stats["max"] / stats["min"]
filtered = stats[stats["ratio"] > 10].sort_values("ratio", ascending=False)
filtered.to_csv("high_variance_stores.csv")
```

---

## 输出要求

无论哪个 Pattern，一定要：

1. **print 数值结果**（合规率 / N / ratio），供 narrative 引用
2. **save artifact**（违规清单 / 活跃用户 / 异常行）到 CSV 让 add_chart 能引
3. **不要** 把 mask 本身当"结论"交给 Lead；要 count / sum / head(20) 浓缩后再输出

## Gotchas

- `str.contains` 默认 case-sensitive + NaN 返 NaN → 总加 `na=False`
- `df.query(...)` 语法对中文列名不友好 → 用 bracket indexing `df['中文名']`
- 混合 int / str 判等 (`df['ID'] == '123'`) 可能全 False → `df['ID'].astype(str) == '123'`
- 日期比较前必 `pd.to_datetime(col, errors='coerce')` 统一类型
