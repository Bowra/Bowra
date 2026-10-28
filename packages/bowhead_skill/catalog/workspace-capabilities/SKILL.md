---
name: workspace-capabilities
description: "Lead answers meta queries ('你能干什么 / 看到什么数据 / 能分析什么'). Loads workspace measures + dimensions + SOP list, renders capability summary + 5 sample queries."
trigger_examples:
  - "你能做什么"
  - "我能问你什么"
  - "你看到什么数据"
  - "能分析什么"
  - "what can you do"
  - "what data do you have"
provides: workspace_capability_summary
version: 1.0.0
title: "Space capabilities"
title_zh: "空间能力自述"
category: platform
---

# Workspace Capabilities — Lead 答 "能干什么" 的标准 SOP

## 何时调

用户问以下任一变体（或类似）:

- "你能做什么"
- "我能问什么"
- "你看到什么数据"
- "能分析什么"
- "你都会什么"
- "what can you do" / "what data do you have"

**不要**用于具体 query（"今年 GMV"、"为什么 X 下滑"等）—— 那些走 bi.* 正常路径.

## Step 1: 拉 workspace 元数据

并行调:

- `bi_schema()` → 拿所有 measure 的 `names[0]`（主关键词）+ description
- `bi_schema()` → 拿所有 dimension 的 `names[0]` + 是否 used

## Step 2: 渲 markdown 答案

按下面 4 段输出:

### 段 1: 能力总览（≤ 3 句）

```
我可以在你的 workspace「<workspace 名 / id>」上做：
- 取数: <N 个指标> × <M 个维度> 的任意组合
- 分析: 同比环比 / 归因 / 趋势 / 排名 / 复盘 / 显著性检验
- 出件: 自动 canvas 报告 + 图表 / PDF/PPT/XLSX 导出
```

### 段 2: 可分析的指标（动态从 bi_schema()）

```
**指标** ({count} 个):
- {names[0]}: {description}
- ...
- (列前 12 个；> 12 显示 "另有 N 个")
```

### 段 3: 可分组的维度（动态从 bi_schema(), used=true）

```
**维度** ({count} 个 used):
- {names[0]} (alias: {names[1:3]})
- ...
```

### 段 4: 5 个 sample queries（按 intent 分类）

```
**试试这些问法**:
1. 📊 简单查询: "今年 {第 1 个 measure} 是多少"
2. 📈 趋势分析: "近 6 个月 {第 1 个 measure} 每月趋势"
3. 🔍 归因: "本月 {第 1 个 measure} 为什么下滑"
4. 📊 对比: "2025 vs 2024 {第 1 个 measure} 同比"
5. 📑 复盘: "最近经营情况"（自动多指标 dashboard）
```

## 铁律

- **必须**真调 `bi_schema()`，**不要**编字段
- 列表展示前 12 个就够；avoid token 爆炸
- 答案末尾不要追加 "还想知道什么" 之类引导（信息已足）
- 这个 SOP 不调 `subagent_spawn`（capability summary 不需要分析子任务）

## SOP 列表（capability section 4 后可附）

可用的分析 SOP（subagent profile=compute 触发）:

- `analyst-sop-diagnosis` — "为什么 X 下滑"
- `analyst-sop-period-compare` — 同比 / 环比对比
- `analyst-sop-growth-driver` — "谁带动了增长"
- `analyst-sop-business-review` — "最近经营 / 整体复盘"
- `analyst-sop-compute` — 显著性检验 / 预测 / 回归 / 聚类 / 集合运算
