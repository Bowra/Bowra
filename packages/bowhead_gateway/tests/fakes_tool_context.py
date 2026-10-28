"""tool-context framework 共享测试假件（reuse review 2026-07-13）.

唯一命名模块（不放 conftest——裸 `from conftest import` 在与 core tests 混跑时
会命中先进 sys.path 的另一个 conftest）。真 store 签名变更只改这里。
"""

from __future__ import annotations


class FakeUserMemoryToolContext:
    def __init__(self, base: dict | None = None, session: dict | None = None) -> None:
        self.base = base
        self.session = session
        self.set_calls: list[dict] = []
        self.clear_calls: list[dict] = []

    async def get_tool_context(self, user_id, tool, slot="workspace", *, session_scoped=False):
        return self.session if session_scoped else self.base

    async def set_tool_context(self, user_id, tool, *, context_id, context_name="", source="explicit", slot="workspace", session_id=None):
        rec = {"tool": tool, "id": context_id, "name": context_name, "source": source, "session_id": session_id}
        self.set_calls.append(rec)
        if source == "session":
            self.session = {"id": context_id, "name": context_name, "source": source, "session_id": session_id}
        else:
            self.base = {"id": context_id, "name": context_name, "source": source}

    async def clear_tool_context(self, user_id, tool, slot="workspace", *, session_scoped=False):
        self.clear_calls.append({"tool": tool, "session_scoped": session_scoped})
        if session_scoped:
            self.session = None
        else:
            self.base = None

    @property
    def cleared(self) -> bool:
        """便捷：base key 是否被清过（撤权路径断言用）."""
        return any(not c["session_scoped"] for c in self.clear_calls)
