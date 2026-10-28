---
name: canvas-docx
description: "Render Bowhead Canvas (ReportSection[] JSON) as an editable Word doc via python-docx. Use for '导出 Word / export docx / 把报告下载成 Word'. Output: output.docx."
version: 1.0.0
allowed-tools: Bash(python3)
title: "Export report to Word"
title_zh: "报告导出 Word"
category: documents
---

# Canvas → Word (.docx)

This skill converts Bowhead's `ReportSection[]` schema to a `.docx` via `python-docx`.

**When to use it**: the user wants a **可编辑**的交付物 —— Word 适合需要对方在上面改字、
批注、走审批流的场景。要**版式固定、拿去就发**的用 `canvas-pdf`；要**演示**用 `canvas-pptx`；
要**数据再加工**用 `canvas-xlsx`。

## Invocation

The current canvas's sections are injected automatically server-side — do **not**
retype the full `ReportSection[]` into `input` (a large canvas will truncate mid-stream
and silently fail to produce a file). Just pass the small optional title override:

```json
{
  "name": "canvas-docx",
  "script": "render_from_canvas.py",
  "input": { "title": "Optional document title" }
}
```

(`sections` is still honored if explicitly passed and no active canvas is found —
e.g. test/eval harnesses — but never rely on this in a real turn.)

Output file: `workdir/output.docx`. Returns `result.json` with `{"path": "output.docx", "sections": N}`.

## Style rules

- **字体**：正文与 `w:eastAsia` 都设成 `Microsoft YaHei`。docx 只写 OOXML、字形由
  Word/WPS 端解析，所以**不会**出现 canvas-pdf 那种"中文变黑方块"（那是 reportlab
  自己画字形才有的问题，见飞书 bug 追#18）；但不显式设 `w:eastAsia` 的话，部分 Word
  版本会用西文默认字体去套中文，字距很难看。
- **chart 降级成数据表**，不画图。app 容器里没有 matplotlib（canvas-pdf 的
  `_render_chart_png` 在 prod 实际一直走 None 分支），而且在一份可编辑文档里，
  一张能继续加工的表比一张位图更有用。
- **未知 kind 安静降级**（导出契约③）：有 `markdown`/`text` 就渲内容，纯 chrome kind
  渲一行灰注记，**禁止**打 `[unsupported kind]` 之类占位字样。
- 下划线强调按 CommonMark 规则只认**词间**：否则 `province_str` 这类 snake_case
  会被当成斜体、下划线被吃掉。
