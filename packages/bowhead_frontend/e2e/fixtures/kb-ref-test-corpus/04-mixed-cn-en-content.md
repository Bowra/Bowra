# Multi-language Mixed Content Test

## Section 1 — 中英混排

This document tests handling of 中英文 mixed content in KB chunks. Common patterns include:

- Tech terms: API、SDK、SaaS 等 acronyms 直接用英文
- Product names: ChatGPT、Slack、Salesforce 中文段落里直引
- 量词混用: 5 GB / 10 万次 / Q1-2026

## Section 2 — 长 chunk 测试 (>500 字)

这是一段比较长的内容，用于测试 KB chunk splitter 在遇到长段落时的切分逻辑。
通常 KB ingestion 会把超过 chunk_size (e.g. 512 token) 的段落拆成多个 chunk，
但保持 section_title 关联以便检索时恢复上下文。

长 chunk 场景常见于:

- PDF 导出文档（章节没有显式分段）
- 法律文本（条款长句）
- 技术规格书（参数表 + 描述）
- 客户合同正文

测试要点：

1. chunk_id 在 split 后是否保持 stable (content-addressable hash 输入含 chunk_index)
2. 跨 chunk 引用时 [ref:chunk_xxx] 是否能定位到正确句子
3. 中英混排时 sentence boundary detection 是否正确

## Section 3 — 繁简共存

繁体: 庫存週轉率、營業額、客戶
简体: 库存周转率、营业额、客户

理论上 vector search rerank 应该把语义相同的繁简内容召回同一类，
但在 chunk_id 层面它们是不同的 chunks (内容不同 → hash 不同)。

## Section 4 — 数字与单位

测试各种数字格式:

- 中文金额: 13.87 亿元 / 1234 万元 / 5万元
- 百分比: 0.09% / 13.2% / 100%
- 比率: 1.618 倍
- 数量: 1,234 件 / 5000 单 / 32 个 SKU
- 英文金额: $1.5M / $20K / £500
