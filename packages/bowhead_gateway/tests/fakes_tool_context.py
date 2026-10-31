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

    async def get_datarc_workspace(self, user_id, scope, *, session_id=None):
        if session_id:
            return self.session if self.session and self.session.get("session_id") == session_id else None
        return self.base

    async def commit_datarc_workspace(self, user_id, scope, *, context_id, context_name="", remember, session_id, source=None):
        from bowhead_core.datarc_selection import DatarcPersistenceReceipt

        await self.set_tool_context(
            user_id,
            "datarc",
            context_id=context_id,
            context_name=context_name,
            source=source or ("explicit" if remember else "session"),
            session_id=None if remember else session_id,
        )
        if remember and session_id:
            await self.clear_tool_context(user_id, "datarc", session_scoped=True)
        return DatarcPersistenceReceipt("saved", "default" if remember else "session")

    async def clear_datarc_workspace(self, user_id, scope, *, session_id=None):
        await self.clear_tool_context(user_id, "datarc", session_scoped=bool(session_id))

    @property
    def cleared(self) -> bool:
        """便捷：base key 是否被清过（撤权路径断言用）."""
        return any(not c["session_scoped"] for c in self.clear_calls)
