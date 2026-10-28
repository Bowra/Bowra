# Attachment Schema Detection

**When**: user uploaded a data file (csv/xlsx/json/tsv). Before doing **any** analysis, run schema detection so you know which column is which.

## Standard detection snippet

Use this exact code (adapt path from INPUTS):

```python
import pandas as pd

# 1. 读取（自适应 encoding）
path = INPUTS["data"]["path"]
try:
    df = pd.read_csv(path, encoding="utf-8-sig")
except UnicodeDecodeError:
    df = pd.read_csv(path, encoding="gbk")
# xlsx/json 分别用 pd.read_excel / pd.read_json

# 2. 打印 schema（stdout 回注 context）
print("# Schema")
print(df.dtypes.to_string())
print("\n# Shape")
print(f"rows={len(df)}, cols={len(df.columns)}")
print("\n# Head")
print(df.head(3).to_string(max_cols=20))
print("\n# Numeric stats")
print(df.describe(include="number").to_string())
print("\n# Unique cats (前 10 列)")
for c in df.select_dtypes(include="object").columns[:10]:
    vals = df[c].dropna().unique()
    print(f"- {c}: n_unique={len(vals)} sample={list(vals[:3])}")
```

Output goes to stdout → you (subagent) see it in the next iteration. From there:

- 找出关键字段（业务实体、规则判定字段、时间字段）
- 按字段名挑 `<主分组维度>` / `<主指标>` 等 SOP 占位符

## Common column-name patterns

| 关键词                  | 可能是                                   |
| ----------------------- | ---------------------------------------- |
| 部门 / 体系 / 机构      | 组织维度（cat 轴）                       |
| ID / 编码 / 工号        | 主键或外键（**不是**度量，别当 numeric） |
| 金额 / 数量 / 次数 / 率 | 度量（numeric）                          |
| 日期 / 时间 / 月份      | 时间维度                                 |
| 是否 / 状态 / 标记      | 布尔/枚举（可作分组）                    |

## Gotchas

- **编码问题**：中文 csv 多半 utf-8-sig；失败了 fallback gbk；**不要** 默认 utf-8 卡死
- **stdout 太长**：controlling 输出量用 head(3) + describe + select_dtypes；不要直接 `print(df)` 把全表 dump 进 context
- **空列 / 单值列**：describe 出 std=0 的数值列可能是定值字段（类型编码），不适合画图
- **ID 列被当数值**：工号 / 部门编码是数字但本质是类别，绘图时要先 `df['col'].astype(str)` 或用列名黑名单过滤

## After detection

把"字段 → 语义"的映射写进你的下一个 response 供后续步骤用：

```
字段识别结果：
- 组织维度：体系 / 部门名称
- 主指标：单号数（count）、合规率（比率）
- 规则判定字段：请购ID明细、立项项目ID编码、支出分类
- 时间字段：（本数据无显式日期列）
```
