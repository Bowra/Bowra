---
title: 边界 metadata 测试
author: ν1 fixture
date: 2026-05-10
tags: [edge-case, junk, parser-test]
---

# 主标题（含 metadata 边界）

上面的 YAML frontmatter 是 KB ingestion 常见的 junk metadata pattern。
splitter 应该:

- 选项 A: 把 frontmatter 当作 metadata 不进 chunk content
- 选项 B: 把 frontmatter 当成 plain text 进 chunk

不同 splitter 处理不同。本 fixture 用于测试。

## 异常字符 edge case

下面是一些可能让 parser 报错的字符:

- 反引号: ` ` `
- 双反引号: ` ` ``
- 三反引号: ` ` ```
- HTML-like: <script>alert("xss")</script> （应该不解释）
- Markdown 链接: [literal-text](http://example.com)
- 表格分隔: | 列1 | 列2 | 列3 |
- 未闭合粗体: **未闭合
- 未闭合斜体: *斜体未闭合

## Heading 但没内容

##

(上面是空 heading)

## 重复 heading

## 重复 heading

(两个相同 heading)

## 含特殊字符的 heading:

(heading 文本含 #)

## 段落极短

短.

是.

够短.

## 段落极长 (单段 600+ 字)

这是一段非常长的内容，目的是测试 KB chunk splitter 对超长段落的处理逻辑。
当一个段落超过 chunk_size 阈值 (通常 512 tokens) 时，splitter 需要决定如何
切分而不破坏语义。常见策略包括 sentence boundary 切分、固定 token window
切分、greedy 切分。每种策略各有优劣。Sentence boundary 最自然但需要 NLP
sentence detection；固定 window 最简单但可能切到句子中间；greedy 在 sentence
boundary 上找接近但不超 chunk_size 的边界。Bowhead KB 当前用 sentence-aware
splitter (kb/splitter/section_aware.py)，先按 markdown heading 分章节，章节
内按 sentence 分，超 chunk_size 的 sentence 再按 token window split (与
section_title 关联保上下文)。这条段落的目的就是触发这个超长 sentence 路径，
让测试覆盖到 fallback 切分逻辑。同时测试 chunk_id content-addressable hash
是否对 split 后的多个 chunks 保持 stable (hash 输入含 chunk_index)。
