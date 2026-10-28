"""把正文开头内联的推理块（`<think>…</think>`）挪出 content。

`LLMResponse.thinking` 本来就是推理轨迹的专用字段：`reasoning_content`（DashScope / o1 类）
与 Anthropic 的 thinking block 都已分流到那里。漏掉的是**把推理直接写进正文**的提供方——
正文以 `<think>` 开头，推理完才给答案。纯文本的小任务会把它原样当结果用：116 上会话标题
（`max_output_tokens=40`，推理还没写完就被截断）直接变成了「<think> 用户输入的问题是…」。

规则刻意收窄：
- 只处理**开头**的块；正文中间出现的 `<think>` 可能是合法内容（比如在讲 HTML 标签），不动。
- 没有闭合标签 = 输出在推理中途被截断：整段都是推理，正文为空，由调用方走自己的兜底。
- 纯字符串操作，不用正则——模型输出是不可信输入，不给回溯留空间。
"""

from __future__ import annotations

_TAGS = ("think", "thinking")


def split_inline_reasoning(content: str) -> tuple[str, str]:
    """返回 (正文, 推理)。开头没有推理块时原样返回 (content, "")。"""

    remaining = content
    reasoning_parts: list[str] = []
    while True:
        stripped = remaining.lstrip()
        lowered = stripped.lower()
        tag = next((t for t in _TAGS if lowered.startswith(f"<{t}>")), None)
        if tag is None:
            break
        body = stripped[len(tag) + 2 :]
        close = f"</{tag}>"
        end = body.lower().find(close)
        if end < 0:
            reasoning_parts.append(body.strip())
            remaining = ""
            break
        reasoning_parts.append(body[:end].strip())
        remaining = body[end + len(close) :]
    if not reasoning_parts:
        return content, ""
    return remaining.lstrip(), "\n".join(part for part in reasoning_parts if part)


class InlineReasoningStream:
    """Hold leading inline reasoning until its closing tag, across chunk boundaries."""

    def __init__(self) -> None:
        self._buffer = ""
        self._mode = "leading"
        self._tag = ""
        self._saw_reasoning = False

    def feed(self, delta: str) -> tuple[str, str]:
        if self._mode == "content":
            return delta, ""
        self._buffer += delta
        reasoning: list[str] = []
        while True:
            if self._mode == "inside":
                close = f"</{self._tag}>"
                end = self._buffer.lower().find(close)
                if end < 0:
                    return "", "\n".join(reasoning)
                body = self._buffer[:end].strip()
                if body:
                    reasoning.append(body)
                self._buffer = self._buffer[end + len(close) :].lstrip()
                self._mode = "leading"
                self._saw_reasoning = True
                continue

            stripped = self._buffer.lstrip()
            lowered = stripped.lower()
            tag = next((candidate for candidate in _TAGS if lowered.startswith(f"<{candidate}>")), None)
            if tag is not None:
                self._tag = tag
                self._buffer = stripped[len(tag) + 2 :]
                self._mode = "inside"
                continue
            if not lowered or any(f"<{candidate}>".startswith(lowered) for candidate in _TAGS):
                return "", "\n".join(reasoning)
            self._mode = "content"
            content = stripped if self._saw_reasoning else self._buffer
            self._buffer = ""
            return content, "\n".join(reasoning)

    def finish(self) -> tuple[str, str]:
        if self._mode == "inside":
            reasoning = self._buffer.strip()
            self._buffer = ""
            self._mode = "content"
            return "", reasoning
        if self._mode == "leading":
            content = self._buffer.lstrip() if self._saw_reasoning else self._buffer
            self._buffer = ""
            self._mode = "content"
            return content, ""
        return "", ""
