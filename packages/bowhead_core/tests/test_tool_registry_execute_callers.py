"""Only the guarded entry points may call ``ToolRegistry.execute``.

Agent Tool calls go through ``SubagentRunner._execute_tool``, which runs the
per-call guards and emits the step events before dispatching. A new direct
caller would skip those guards (the forced Compute dispatch and the auto-chart
self-call used to). See docs/design/tool-call-policy-chain-tech-design.md.

Receivers are matched by name (``effective``, ``registry``, ``view``,
``*tool_registry``, ``*tool_view`` and ``super()``), the names the codebase
uses for Tool registries. Other ``execute`` methods (database cursors, the
LLM verification probe registry) use different receivers.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIVER_SUFFIXES = ("tool_registry", "tool_view")
RECEIVER_NAMES = frozenset({"effective", "registry", "view", "super()"})

# (path relative to the repository root, enclosing qualified name)
ALLOWED_CALLERS = frozenset(
    {
        # The guarded entry point for every Agent Tool call.
        ("packages/bowhead_core/src/bowhead_core/subagent.py", "SubagentRunner._execute_tool"),
        # Executes an already approved call outside any Agent loop.
        ("packages/bowhead_core/src/bowhead_core/approval_executor.py", "ApprovalExecutor.execute"),
        ("packages/bowhead_core/src/bowhead_core/approval_executor.py", "ApprovalExecutor.execute_effect"),
        # Agent Eval's registry subclass delegating to the base implementation.
        ("packages/bowhead_core/src/bowhead_core/agent_eval_effects.py", "EvalToolRegistryView.execute"),
    }
)


class _ExecuteCallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.found: set[tuple[str, str]] = set()

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self.scope.append(node.name)
            super().generic_visit(node)
            self.scope.pop()
        else:
            super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            receiver = ast.unparse(func.value)
            if receiver in RECEIVER_NAMES or receiver.endswith(RECEIVER_SUFFIXES):
                self.found.add((self.relative_path, ".".join(self.scope)))
        self.generic_visit(node)


def _registry_execute_callers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for base in [*REPO_ROOT.glob("packages/*/src"), REPO_ROOT / "enterprise"]:
        for path in base.rglob("*.py"):
            visitor = _ExecuteCallVisitor(path.relative_to(REPO_ROOT).as_posix())
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            found |= visitor.found
    return found


def test_registry_execute_is_called_only_from_guarded_entry_points() -> None:
    callers = _registry_execute_callers()

    unexpected = callers - ALLOWED_CALLERS
    assert not unexpected, f"Route these Tool calls through SubagentRunner._execute_tool: {sorted(unexpected)}"
    # Keep the allowlist exact so a removed caller does not leave a stale exemption.
    assert callers == ALLOWED_CALLERS
