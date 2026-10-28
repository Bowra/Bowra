"""Canvas composer 子包。

**这里曾经住着 `compose_answer`（Option C-flex 三阶段管道：outline LLM → 代码
mapper → narrator LLM）。2026-09-10 整条删除**——它的唯一入口
`Orchestrator._compose_canvas` 全仓零调用方，`canvas_composer.compose_canvas`
本身也只被那一个零调用方调用。生产走的是 `compose_canvas_from_payload`
（结构化 payload 路）和 `report_writer_loop`（代笔路）。

一并删掉的还有 `outline.py`（生成 section kind 列表）、`narrator.py`
（并发填叙事段）以及 `answer_decomposer.decompose_answer` 的降级兜底分支。
取证见 `docs/sprints/chart-config-ux-2026-08-28/evidence-decompose-fallback-2026-09-10.md`。

仍然活着的子模块（**不要**因为本文件变空就以为整包是死的）：

- ``realtime_stream`` —— turn 内逐工具流式 emit code_block
- ``mappers`` — ``MapperInput`` and ``_map_code_block`` for ``realtime_stream``
- ``answer_decomposer`` —— 一批被 citation_marks / canvas_tools /
  canvas_composer / report_writer_loop 直接 import 的辅助函数
- ``auto_chart`` —— ι 自动出图兜底
- ``streaming`` —— 流式 section 装配
"""

from __future__ import annotations
