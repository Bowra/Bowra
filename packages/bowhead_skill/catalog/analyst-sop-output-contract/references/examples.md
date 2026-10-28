# AnalysisPayload — 完整 Example

> 3 个真实场景 payload. 来源 = M137 spike + 历史 production turn.
> 看 1-2 个完整例子比读抽象 schema 更快上手.

---

## Example 0 · 简单计算（Chat 短答）

**用户原 query**：`19 × 29 是多少？`

```json
{
  "delivery_surface": "chat",
  "exec_summary": "19 × 29 = 551",
  "kpis": [{ "label": "19 × 29", "value": "551", "sentiment": "neutral" }],
  "ground_facts": [{ "key": "product", "label": "19 × 29", "value": 551, "unit": "", "formula": "19*29", "source": "python_sandbox" }]
}
```

不写 title/insights/recommendations，不调用 `canvas_add_*`。Python stdout 与文件由系统留在「数据」。

---

## Example 1 · `EA-monitoring`（report 类）

**Source**：M137 spike `run_01.json`（Qwen 真实输出，已通过 schema validation）.

**用户原 query**：

> "数据是公司结算阶段清单明细，要监控 EA 单据是否都关联请购和立项流程，做一个三层递进式的合规分析看板"

**预期 payload**（节选 — 完整版本是 spike 实际输出）：

```json
{
  "title": "EA 结算单据流程合规监控看板（2026年1月1日至今）",
  "eyebrow": "合规监控",
  "exec_summary": "整体合规率仅11.39%，远低于合理阈值。部门支出（01类）合规率低至1.85%，是最大短板；92.5%不合规单因'无请购原因'字段为空，暴露系统强制缺失与执行断点；高风险集中于江西现场交付组、国内东区组等5个部门，占不合规单量超40%。",
  "kpis": [
    { "label": "整体合规率", "value": "11.39%", "delta": "vs 合理基线 -70+pp", "sentiment": "bad" },
    { "label": "部门支出合规率", "value": "1.85%", "delta": "vs 项目支出 10.31%", "sentiment": "bad" },
    { "label": "不合规主因：空值", "value": "92.5%", "delta": "(66,224单)", "sentiment": "bad" },
    { "label": "高风险TOP5部门单量占比", "value": "42.3%", "sentiment": "bad" }
  ],
  "insights": [
    {
      "title": "部门支出构成结构性瓶颈，合规率不足项目支出的1/5",
      "thesis": "部门支出(01)合规率1.85% vs 项目支出(02)10.31%，且占总单量约90%，是杠杆核心",
      "evidence": [
        {
          "kind": "chart",
          "chart_section_id": "sec_7614fafe336b",
          "caption": "按支出分类合规率对比：部门支出(01)仅1.85%，项目支出(02)为10.31%，其他类别均100%"
        }
      ],
      "narrative": "支出分类维度揭示根本失衡：部门支出虽为结算主力（占单量绝对多数），但合规率仅为项目支出的17.9%，且长期稳定在低位。该分类下差旅报销、关联方能源费用等高频场景缺乏前置流程绑定机制，导致系统无法校验请购关联性，形成合规洼地。",
      "impact": { "quantity": 8.46, "unit": "pp", "direction": "+", "timeframe": "若部门支出提至10.31%", "note": "可拉动整体合规率升至约20%" },
      "confidence": "high"
    },
    {
      "title": "不合规主因是系统性字段缺失：92.5%单据'无请购原因'为空",
      "thesis": "66,224单'无请购原因'字段为空，属流程执行黑洞，非业务逻辑问题",
      "evidence": [
        {
          "kind": "chart",
          "chart_section_id": "sec_dc3b05cc329d",
          "caption": "不合规原因Pareto分布：空值占比92.5%，前5类已知原因合计仅4.3%"
        },
        {
          "kind": "callout",
          "callout_text": "字段为空 ≠ 无原因，而是系统未设必填+无默认枚举，导致数据不可归因",
          "caption": "空值本质是治理缺口"
        }
      ],
      "narrative": "'无请购原因'字段空值占据不合规单绝对主导（92.5%），远超所有已知业务原因总和。该现象在近30天持续稳定，表明非偶发操作失误，而是OA流程设计缺陷——未将该字段设为必填、未提供标准下拉选项（如'差旅''社保''上线前合同'），致使审批流通过后仍无法追溯真实动因。",
      "impact": { "quantity": 66224, "unit": "单", "direction": "-", "note": "92.5%数据不可分析，阻断根因定位" },
      "confidence": "high"
    },
    {
      "title": "高风险部门高度集中：TOP5部门贡献42.3%不合规单量",
      "thesis": "江西现场交付组、国内东区组等5个部门单量级千级以上，且合规率普遍<3%",
      "evidence": [
        {
          "kind": "chart",
          "chart_section_id": "sec_37efacdea1d4",
          "caption": "高风险部门TOP5：江西现场交付组（680单）、国内东区组（2016单合计）、设施管理部等"
        }
      ],
      "narrative": "申请人部门维度呈现显著长尾分布：TOP5部门不合规单量达1.8万单，占全部不合规单的42.3%。其中'江西现场交付组'与'国内东区组'单号数最高，且多涉及现场差旅、临时采购等边缘场景，现有立项/请购流程覆盖不足，亟需适配轻量级替代路径。",
      "impact": { "quantity": 42.3, "unit": "%", "direction": "=", "note": "TOP5部门集中度高，干预ROI明确" },
      "confidence": "med"
    }
  ],
  "recommendations": [
    {
      "priority": "P0",
      "action": "OA系统立即强制'无请购原因'字段为必填，并预置标准枚举值（差旅/社保/上线前合同/关联方能源/其他）",
      "owner": "IT系统组",
      "timeline": "48小时内上线",
      "expected_impact": "解决66,224单空值，可识别真正不合规原因",
      "rationale": "字段空值92.5%是数据治理黑洞"
    },
    {
      "priority": "P0",
      "action": "对'部门支出(01)'类单据启用拦截规则：无请购ID明细则禁止提交审批",
      "owner": "财务流程中心",
      "timeline": "3个工作日内"
    },
    {
      "priority": "P1",
      "action": "为江西现场交付组、国内东区组试点'简易请购备案制'：邮件报备+设施BP签字+财务备案，替代完整请购流程",
      "owner": "流程治理办公室",
      "timeline": "2周内完成方案并试点"
    }
  ],
  "caveats": [
    "数据覆盖期为2026年1月1日至今，尚未包含完整季度趋势",
    "'无请购原因'字段空值无法二次归类，影响深度根因分析",
    "部门名称存在同义不同名现象（如'东区组'与'国内东区组'），可能低估实际集中度"
  ]
}
```

**关键看点**：

- 3 个 insight MECE：结构性失衡 / 治理缺口 / 集中度问题，三个维度互不重叠
- 每个 insight 至少 1 chart evidence + 真实 chart_section_id
- narrative 都 100-200 字，把 evidence 串成业务洞察（而不是"图显示 X 是 Y"）
- recommendations 含 2 个 P0 都有 owner + timeline
- caveats 老实承认数据局限

---

## Example 2 · `GMV-causal`（causal 类）

**用户原 query**：

> "本月 GMV 为什么下滑"

**预期 payload**：

```json
{
  "title": "本月 GMV 同比下滑 -8.2% 归因分析",
  "eyebrow": "归因分析",
  "exec_summary": "本月 GMV 同比 -8.2%，主因华南区订单量下滑（贡献 60% 跌幅，受 4 月暴雨影响）+ 新客转化率下降（贡献 25%）；老客复购率反而 +3pp 是反向支撑信号。建议聚焦华南天气恢复后的促销窗口 + 新客 onboarding 优化。",
  "kpis": [
    { "label": "本月 GMV", "value": "12.4亿", "delta": "YoY -8.2%", "sentiment": "bad" },
    { "label": "华南区贡献跌幅", "value": "60%", "delta": "跌幅最大区域", "sentiment": "bad" },
    { "label": "新客转化率", "value": "5.8%", "delta": "MoM -1.4pp", "sentiment": "bad" },
    { "label": "老客复购率", "value": "32%", "delta": "MoM +3pp", "sentiment": "good" }
  ],
  "insights": [
    {
      "title": "华南区订单量下滑是绝对主因（贡献跌幅 60%）",
      "thesis": "华南订单 -15.4%，对应 GMV 跌幅占总跌幅 60%；同期非华南区域稳定",
      "evidence": [
        {
          "kind": "chart",
          "chart_section_id": "sec_attr_001",
          "caption": "GMV YoY 按地域归因 — 华南 -1.5亿，其他区合计 -1.0亿"
        },
        {
          "kind": "callout",
          "callout_text": "4 月华南连续 12 天暴雨，物流配送中断 30%，与跌幅时间窗高度重合",
          "caption": "外因关联"
        }
      ],
      "narrative": "华南是全国 GMV 第一大区（占比 28%），本月订单量同比 -15.4% 直接拉低全国 -4.3pp。归因模型显示该跌幅 80% 集中在 4 月 8-19 日，恰好对应华南连续暴雨周；非华南区域同期订单稳定甚至轻微正向，证明非系统性问题。建议恢复促销窗在天气稳定后第一周展开。",
      "impact": { "quantity": -1.5, "unit": "亿", "direction": "-", "note": "可恢复 70% 当促销启动" },
      "confidence": "high"
    },
    {
      "title": "新客转化率下降（次要原因，贡献 25%）",
      "thesis": "新客转化率 MoM -1.4pp，独立于华南问题",
      "evidence": [
        { "kind": "chart", "chart_section_id": "sec_funnel_002", "caption": "新客漏斗：浏览→加购→下单 三步转化" },
        { "kind": "computation", "computation_artifact_id": "art_calc_001", "caption": "新客 LTV 测算：转化率 1pp ≈ 全月 GMV 0.8% 影响" }
      ],
      "narrative": "新客转化率从 7.2% 降至 5.8%，跌幅 1.4pp 折合全月 GMV 影响约 -0.4 亿。漏斗分析显示问题集中在'加购→下单'环节（-2.1pp），疑似支付流程或库存承诺信息影响。建议 A/B 测试结算页改版。",
      "impact": { "quantity": -0.4, "unit": "亿", "direction": "-", "timeframe": "本月" },
      "confidence": "med"
    },
    {
      "title": "老客复购率反向上升 +3pp（正向信号）",
      "thesis": "老客复购率 32% 创历史新高，说明品牌资产健康",
      "evidence": [
        { "kind": "chart", "chart_section_id": "sec_repurchase_003", "caption": "老客 6 月复购率趋势" },
        { "kind": "callout", "callout_text": "5 月会员日营销留存效应延续到 6 月", "caption": "营销贡献" }
      ],
      "narrative": "在大盘下滑背景下，老客复购率 +3pp 至 32%，部分对冲新客流失影响。归因看是 5 月 13 日会员日大促的留存效应延续。建议巩固该机制，加大会员级别差异化运营。",
      "impact": { "quantity": 3, "unit": "pp", "direction": "+" },
      "confidence": "high"
    }
  ],
  "recommendations": [
    {
      "priority": "P0",
      "action": "华南天气稳定后第一周启动恢复性促销（满减 + 包邮）",
      "owner": "华南区运营",
      "timeline": "天气预报稳定后 3 天内",
      "expected_impact": "预计可挽回 1 亿 GMV"
    },
    {
      "priority": "P1",
      "action": "新客结算页 A/B 测试：精简流程 + 实时库存承诺",
      "owner": "前端 + 增长团队",
      "timeline": "2 周内上线 A/B"
    },
    {
      "priority": "P2",
      "action": "巩固 6 月会员日，扩展到月度会员价格联动",
      "owner": "会员运营组",
      "timeline": "下季度"
    }
  ],
  "caveats": ["归因模型基于线性贡献度分解，未考虑跨维度交互项", "暴雨与 GMV 的因果性是关联推断，未做严格反事实分析"]
}
```

**关键看点**：

- 3 个 insight 跨"主因/次因/反向信号"维度，符合归因分析框架
- 多 evidence 组合：chart + callout + computation 都用上了
- 量化影响真带数字（-1.5亿、-0.4亿）

---

## Example 3 · `median-cohort`（compute 类）

**用户原 query**：

> "买了示例药品乙没买示例药品甲的医院里超过中位数的"

**预期 payload**（精简）：

```json
{
  "title": "示例药品乙用户中示例药品甲潜在医院（超过中位数）",
  "eyebrow": "潜在客户分析",
  "exec_summary": "在 412 家买示例药品乙但未买示例药品甲的医院中，示例药品乙采购量超过全样本中位数（145 单）的有 87 家；这 87 家中三甲医院占 62%，是示例药品甲扩展的高潜力名单。",
  "kpis": [
    { "label": "示例药品乙用户医院", "value": "412", "sentiment": "neutral" },
    { "label": "未买示例药品甲医院", "value": "412", "sentiment": "neutral" },
    { "label": "中位数潜力客户", "value": "87", "delta": "占 21%", "sentiment": "good" },
    { "label": "三甲占比", "value": "62%", "sentiment": "good" }
  ],
  "insights": [
    {
      "title": "高潜力客户高度集中三甲医院（54 家 / 62%）",
      "thesis": "三甲医院在高潜力名单中显著超平均，是优先公关对象",
      "evidence": [
        { "kind": "chart", "chart_section_id": "sec_tier_001", "caption": "高潜力医院按等级分布" },
        { "kind": "computation", "computation_artifact_id": "art_median_calc", "caption": "Python 中位数 + 集合差计算" },
        { "kind": "table", "table_artifact_id": "art_top_list", "caption": "Top 87 高潜力医院明细" }
      ],
      "narrative": "在 87 家高潜力医院中，三甲医院 54 家占 62%，远超全样本三甲占比（41%），说明示例药品乙高购买量医院本身集中在高级别医院体系。这给示例药品甲扩展提供了清晰的三甲优先打开口子，节省广撒网成本。",
      "impact": { "quantity": 54, "unit": "家", "note": "P0 公关清单" },
      "confidence": "high"
    },
    {
      "title": "地域分布华东 + 华南占 75%（区域聚焦）",
      "thesis": "高潜力医院 75% 在华东华南，跟现有渠道重合度高",
      "evidence": [{ "kind": "chart", "chart_section_id": "sec_geo_001", "caption": "高潜力医院按省份分布（TOP 10）" }],
      "narrative": "华东（江浙沪）+ 华南（广东）两区合计 65 家，占高潜力名单 75%。这两区现有示例药品乙渠道经理可直接覆盖；建议给现有团队加 KPI（示例药品甲交叉销售）而非建新团队。",
      "impact": { "quantity": 65, "unit": "家", "direction": "+" },
      "confidence": "high"
    }
  ],
  "recommendations": [
    {
      "priority": "P0",
      "action": "锁定 54 家三甲高潜力医院，2 周内一对一上门",
      "owner": "销售总监",
      "timeline": "2 周内全覆盖",
      "expected_impact": "若 30% 转化 → 16 家新客，按平均单价测算 = 2400 万年增量"
    },
    {
      "priority": "P1",
      "action": "华东华南渠道经理增加示例药品甲交叉销售 KPI",
      "owner": "区域销售总监",
      "timeline": "下月起"
    }
  ],
  "caveats": ["中位数阈值（145 单）按本月样本计算；下月样本变化可能改变名单", "未买示例药品甲的判定基于过去 12 月，不排除 12 月前买过又流失"]
}
```

**关键看点**：

- compute 类典型：用了 chart + computation + table 三种 evidence
- kpis 突出"过滤后的潜力客户数"
- recommendations 量化预期收入（2400 万）
- caveats 承认数据时间窗的局限

---

## 写 payload 的心智 sequence

```
1. 先想好 3 个 insight 的"标题 + thesis"（金字塔顶层）
2. 每个 insight 挑配套 evidence（已有 chart 优先用 chart_section_id）
3. 写 narrative 把 evidence 串成业务洞察（不是描述图）
4. 估算 impact（如果有数字支持）
5. exec_summary 浓缩三个 insight 主线 + 行动方向
6. recommendations 跟 insight 对应起来（每个洞察至少出 1 条建议）
7. caveats 老实写
8. self-check（SKILL.md §7）→ pack
```
