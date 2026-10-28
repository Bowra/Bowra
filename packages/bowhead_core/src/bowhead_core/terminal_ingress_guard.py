"""AST/SQL guard for Terminal Protocol v1 production write ingress."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass

_LEGACY_MUTATORS = frozenset(
    {
        "complete_turn",
        "finalize_turn",
        "mark_aborted",
        "mark_approval_pending",
        "mark_cancelled",
        "mark_clarification_pending",
        "mark_completed",
        "mark_failed",
        "record_turn",
        "revive_session",
        "soft_delete_session",
    }
)
_S5_CONTROL_MUTATORS = frozenset(
    {
        "clear_steer_buffer",
        "mark_dismissed",
        "record_execution",
        "set_decision_once",
        "spawn_resume_turn",
    }
)
_EFFECT_CONTROL_WRITERS = frozenset(
    {
        "record_approval_projection_effect_settlement",
        "record_admin_probed_effect_settlement",
        "record_manual_effect_settlement",
        "record_provider_effect_settlement",
    }
)
_EFFECT_RUNTIME_WRITERS = frozenset(
    {
        "begin_cancelled_finalization_with_local_billing",
        "begin_effect_attempt",
        "claim_effect_dispatch",
        "discard_effect_intent",
        "freeze_usage_and_claim_billing_attempt",
        "mark_effect_unknown",
        "mark_effect_unknown_and_discover_recovery",
        "record_effect_settlement",
        "record_llm_effect_settlement_with_usage",
        "record_local_billing_settlement",
        "record_tool_effect_settlement",
    }
)
_CHAT_TASK_SCAN_SOURCES = frozenset(
    {
        "packages/bowhead_core/src/bowhead_core/agents/lead_orchestrator.py",
        "packages/bowhead_core/src/bowhead_core/agents/task_runner.py",
        "packages/bowhead_core/src/bowhead_core/canvas_composers/__init__.py",
        "packages/bowhead_core/src/bowhead_core/canvas_composers/streaming.py",
        "packages/bowhead_core/src/bowhead_core/delta_lifecycle.py",
        "packages/bowhead_core/src/bowhead_core/events.py",
        "packages/bowhead_core/src/bowhead_core/orchestrator.py",
        "packages/bowhead_core/src/bowhead_core/subagent_return_tool.py",
        "packages/bowhead_core/src/bowhead_core/terminal_event_writer.py",
        "packages/bowhead_core/src/bowhead_core/terminal_finalizer.py",
        "packages/bowhead_core/src/bowhead_core/terminal_llm_invocation.py",
        "packages/bowhead_core/src/bowhead_core/terminal_runtime.py",
        "packages/bowhead_core/src/bowhead_core/terminal_runtime_factory.py",
        "packages/bowhead_core/src/bowhead_core/tool_finding.py",
        "packages/bowhead_core/src/bowhead_core/tool_registry.py",
        "packages/bowhead_core/src/bowhead_core/turn_log_store.py",
        "packages/bowhead_core/src/bowhead_core/turn_runner.py",
        "packages/bowhead_gateway/src/bowhead_gateway/routes/chat.py",
        "packages/bowhead_gateway/src/bowhead_gateway/terminal_approval_runner.py",
        "packages/bowhead_gateway/src/bowhead_gateway/turn_service.py",
        "packages/bowhead_router/src/bowhead_router/invocation.py",
    }
)
_CHAT_TASK_SPAWNERS = frozenset(
    {
        "create_detached_auxiliary_task",
        "create_task",
        "ensure_future",
        "shield",
    }
)
_EFFECT_RETENTION_GATE = "aggregate_effect_retention"
# Count occurrences: second same-kind write/terminal emit in function cannot be incidentally registered by first
# (old dedup for direct_sql/root_terminal_emit/legacy_mutator hid additional state-changing UPDATEs
# within same function). Repeated manifest identifiers require explicit occurrences count.
_COUNTED_FINDING_KINDS = frozenset(
    {
        "direct_sql",
        "root_terminal_emit",
        "legacy_mutator",
        "chat_task_spawn",
        "effect_control_writer",
        "effect_retention_gate_definition",
        "effect_detail_delete",
        "effect_runtime_writer",
        "post_terminal_auxiliary_scope",
    }
)
_EFFECT_DETAIL_DELETE_PATTERNS = (
    (
        "turn_effect_attempts",
        re.compile(
            r'\bDELETE\s+FROM\s+["`\[]?turn_effect_attempts["`\]]?(?:\s|\b)',
            re.IGNORECASE,
        ),
    ),
    (
        "turn_effect_evidence_links",
        re.compile(
            r'\bDELETE\s+FROM\s+["`\[]?turn_effect_evidence_links["`\]]?(?:\s|\b)',
            re.IGNORECASE,
        ),
    ),
    (
        "turn_effect_settlements",
        re.compile(
            r'\bDELETE\s+FROM\s+["`\[]?turn_effect_settlements["`\]]?(?:\s|\b)',
            re.IGNORECASE,
        ),
    ),
)
_SQL_PATTERNS = (
    (
        "turn_status",
        re.compile(r"\bUPDATE\s+turns\b[\s\S]*?\bSET\b[\s\S]*?\bstatus\s*=", re.IGNORECASE),
    ),
    (
        "session_active",
        re.compile(r"\bUPDATE\s+sessions\s+SET\b[\s\S]*\bactive_turn_id\b", re.IGNORECASE),
    ),
    (
        "session_deleted",
        re.compile(r"\bUPDATE\s+sessions\s+SET\b[\s\S]*\bdeleted_at\b", re.IGNORECASE),
    ),
    (
        "account_delete",
        re.compile(r"\bDELETE\s+FROM\s+users\b", re.IGNORECASE),
    ),
    (
        "chat_turn_user_source",
        re.compile(
            r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+chat_turn_user_sources\b",
            re.IGNORECASE,
        ),
    ),
    (
        "clarification_decision_source",
        re.compile(r"\bUPDATE\s+clarification_requests\b[\s\S]*?\bSET\b", re.IGNORECASE),
    ),
    (
        "approval_decision_source",
        re.compile(r"\bUPDATE\s+approvals\b[\s\S]*?\bSET\b", re.IGNORECASE),
    ),
    (
        "terminal_decision",
        re.compile(r"\b(?:INSERT\s+INTO|UPDATE)\s+turn_control_decisions\b", re.IGNORECASE),
    ),
    (
        "terminal_decision_effect",
        re.compile(r"\b(?:INSERT\s+INTO|UPDATE)\s+turn_decision_effects\b", re.IGNORECASE),
    ),
)
_ROOT_TERMINAL_EVENTS = frozenset({"done", "error", "turn_failed"})
_EVENT_FACTORIES = frozenset({"OrchestratorEvent", "_event"})
_LEGACY_EXECUTION_ADAPTER = "admit_legacy_chat_execution"
_LEGACY_DIRECT_RUN_TURN = "run_turn"
_LEGACY_BUS_PRODUCERS = frozenset({"get_or_create"})
_LEGACY_BUS_CONSTRUCTOR = "TurnEventBus"
_TERMINAL_BUS_PRODUCER_EXEMPTIONS = frozenset(
    {
        (
            "packages/bowhead_core/src/bowhead_core/terminal_runtime_factory.py",
            "<module>.TerminalRuntimeFactory._attach_opened_scope_owned",
            "get_or_create",
        ),
    }
)


@dataclass(frozen=True, slots=True, order=True)
class TerminalIngressFinding:
    kind: str
    source: str
    owner: str
    detail: str

    @property
    def identity(self) -> str:
        return f"{self.kind}|{self.source}|{self.owner}|{self.detail}"


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        source_name: str,
        *,
        approval_execute_wrappers: frozenset[str],
    ) -> None:
        self.source_name = source_name
        self.approval_execute_wrappers = approval_execute_wrappers
        self.owners: list[str] = ["<module>"]
        self.qualified_owners: list[str] = ["<module>"]
        self.findings: list[TerminalIngressFinding] = []
        self.aliases: list[dict[str, str]] = [{}]
        self.ingress_aliases: list[dict[str, str]] = [{}]
        self.event_values: list[dict[str, str]] = [{}]
        self.approval_task_values: list[set[str]] = [set()]
        self.terminal_feature_guard_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name == _LEGACY_EXECUTION_ADAPTER:
            self._add_ingress(
                "legacy_execution_adapter_definition",
                node.name,
                owner=".".join((*self.qualified_owners, node.name)),
            )
        if node.name == _EFFECT_RETENTION_GATE:
            self._add_ingress(
                "effect_retention_gate_definition",
                node.name,
                owner=".".join((*self.qualified_owners, node.name)),
            )
        self.owners.append(node.name)
        self.qualified_owners.append(node.name)
        self.aliases.append({})
        self.ingress_aliases.append({})
        self.event_values.append({})
        self.approval_task_values.append(set())
        self.generic_visit(node)
        self.approval_task_values.pop()
        self.event_values.pop()
        self.ingress_aliases.pop()
        self.aliases.pop()
        self.qualified_owners.pop()
        self.owners.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.qualified_owners.append(node.name)
        self.ingress_aliases.append({})
        self.generic_visit(node)
        self.ingress_aliases.pop()
        self.qualified_owners.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in {_LEGACY_EXECUTION_ADAPTER, _LEGACY_BUS_CONSTRUCTOR}:
                self.ingress_aliases[-1][alias.asname or alias.name] = alias.name
            if alias.name in (
                _EFFECT_CONTROL_WRITERS | _EFFECT_RUNTIME_WRITERS | _CHAT_TASK_SPAWNERS | _EVENT_FACTORIES | {"detached_auxiliary_scope"}
            ):
                self.aliases[-1][alias.asname or alias.name] = alias.name

    def visit_Assign(self, node: ast.Assign) -> None:
        self._track_assignment(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._track_assignment((node.target,), node.value)
        self.generic_visit(node)

    def _track_assignment(
        self,
        targets: tuple[ast.expr, ...] | list[ast.expr],
        value: ast.expr,
    ) -> None:
        alias = self._guarded_mutator_reference(value)
        ingress_alias = self._ingress_reference_name(value)
        event_type = self._event_expression_type(value)
        approval_task = self._is_approval_task_expression(value)
        for target in targets:
            for name in _assigned_names(target):
                if alias is not None:
                    self.aliases[-1][name] = alias
                else:
                    self.aliases[-1].pop(name, None)
                if ingress_alias in _LEGACY_BUS_PRODUCERS | {
                    _LEGACY_EXECUTION_ADAPTER,
                    _LEGACY_DIRECT_RUN_TURN,
                    _LEGACY_BUS_CONSTRUCTOR,
                }:
                    self.ingress_aliases[-1][name] = ingress_alias
                else:
                    self.ingress_aliases[-1].pop(name, None)
                if event_type is not None:
                    self.event_values[-1][name] = event_type
                else:
                    self.event_values[-1].pop(name, None)
                if approval_task:
                    self.approval_task_values[-1].add(name)
                else:
                    self.approval_task_values[-1].discard(name)

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._resolved_call_name(node)
        if callee in _LEGACY_MUTATORS:
            self._add("legacy_mutator", callee)
        if callee in _S5_CONTROL_MUTATORS:
            self._add("s5_control_mutator", callee)
        if callee in _EFFECT_CONTROL_WRITERS:
            self._add_ingress("effect_control_writer", callee)
        if callee in _EFFECT_RUNTIME_WRITERS:
            self._add_ingress("effect_runtime_writer", callee)
        if (
            self.source_name in _CHAT_TASK_SCAN_SOURCES
            and callee in _CHAT_TASK_SPAWNERS
            and (callee != "shield" or bool(node.args and isinstance(node.args[0], ast.Call)))
        ):
            self._add_ingress(
                "chat_task_spawn",
                _task_spawn_detail(node, callee),
            )
        if self.source_name == "packages/bowhead_core/src/bowhead_core/canvas_store.py" and callee == "detached_auxiliary_scope":
            self._add_ingress("post_terminal_auxiliary_scope", callee)
        if self.terminal_feature_guard_depth and callee in (_LEGACY_MUTATORS | _S5_CONTROL_MUTATORS):
            self._add("feature_flag_control_fallback", callee)
        if callee == "create_task" and any(
            self._is_approval_task_expression(argument)
            for argument in (
                *node.args,
                *(keyword.value for keyword in node.keywords),
            )
        ):
            self._add("approval_bare_task", "execute")
        terminal_type = self._terminal_event_type(node)
        if terminal_type is not None:
            self._add("root_terminal_emit", terminal_type)
        ingress_callee = self._ingress_reference_name(node.func)
        if ingress_callee == _LEGACY_EXECUTION_ADAPTER:
            self._add_ingress("legacy_execution_adapter_call", ingress_callee)
        elif ingress_callee == _LEGACY_DIRECT_RUN_TURN:
            self._add_ingress("legacy_direct_run_turn_call", ingress_callee)
        elif ingress_callee == _LEGACY_BUS_CONSTRUCTOR:
            self._add_ingress("legacy_event_bus_constructor", ingress_callee)
        elif ingress_callee in _LEGACY_BUS_PRODUCERS and self._is_legacy_bus_producer_call(node, ingress_callee):
            exemption = (self.source_name, self._qualified_owner, ingress_callee)
            if exemption not in _TERMINAL_BUS_PRODUCER_EXEMPTIONS:
                self._add_ingress("legacy_event_bus_producer", ingress_callee)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        guarded = _is_terminal_feature_expression(node.test)
        if guarded:
            self.terminal_feature_guard_depth += 1
        self.generic_visit(node)
        if guarded:
            self.terminal_feature_guard_depth -= 1

    def visit_Try(self, node: ast.Try) -> None:
        emits = [
            child
            for statement in node.body
            for child in ast.walk(statement)
            if isinstance(child, ast.Call) and self._resolved_call_name(child) in {"emit", "emit_idempotent"}
        ]
        if emits:
            for handler in node.handlers:
                caught = _caught_exception_names(handler.type)
                catches_fail_stop = handler.type is None or "BaseException" in caught or "TerminalDurabilityAbort" in caught
                reraises = any(isinstance(child, ast.Raise) for statement in handler.body for child in ast.walk(statement))
                if catches_fail_stop and not reraises:
                    self._add(
                        "durability_abort_swallowed",
                        f"emit_try_line_{node.lineno}",
                    )
        self.generic_visit(node)

    def _resolved_call_name(self, node: ast.Call) -> str | None:
        name = _call_name(node)
        if isinstance(node.func, ast.Name):
            for aliases in reversed(self.aliases):
                if node.func.id in aliases:
                    return aliases[node.func.id]
        return self._ingress_reference_name(node.func) or name

    def _guarded_mutator_reference(self, node: ast.expr) -> str | None:
        guarded = (
            _LEGACY_MUTATORS
            | _S5_CONTROL_MUTATORS
            | _EFFECT_CONTROL_WRITERS
            | _EFFECT_RUNTIME_WRITERS
            | _CHAT_TASK_SPAWNERS
            | _EVENT_FACTORIES
            | {
                "detached_auxiliary_scope",
                "emit",
                "emit_idempotent",
                "execute",
            }
        )
        if isinstance(node, ast.Attribute) and node.attr in guarded:
            return node.attr
        if isinstance(node, ast.Name):
            for aliases in reversed(self.aliases):
                if node.id in aliases:
                    return aliases[node.id]
        reference = self._ingress_reference_name(node)
        if reference in guarded:
            return reference
        return None

    def _ingress_reference_name(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            for aliases in reversed(self.ingress_aliases):
                if node.id in aliases:
                    return aliases[node.id]
            return node.id
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            return node.args[1].value
        return None

    @staticmethod
    def _is_legacy_bus_producer_call(node: ast.Call, callee: str) -> bool:
        return not any(
            keyword.arg == "producer" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False for keyword in node.keywords
        )

    def _event_expression_type(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.event_values[-1].get(node.id)
        if not isinstance(node, ast.Call):
            return None
        callee = self._resolved_call_name(node)
        if callee == "OrchestratorEvent":
            event_type = _literal_event_type(node)
        elif callee == "_event":
            event_type = _literal_helper_event_type(node)
        else:
            return None
        return event_type if event_type in _ROOT_TERMINAL_EVENTS else None

    def _terminal_event_type(self, node: ast.Call) -> str | None:
        if (
            self._resolved_call_name(node)
            not in {
                "emit",
                "emit_idempotent",
            }
            or not node.args
        ):
            return None
        event = node.args[0]
        event_type = self._event_expression_type(event)
        if event_type is not None:
            return event_type
        # A forwarding edge cannot prove that an opaque value is non-terminal.
        # Keep it in the reviewed manifest instead of silently accepting a new
        # root terminal write path.
        if isinstance(event, (ast.Name, ast.Call)):
            return "unresolved"
        return None

    def _is_approval_task_expression(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.approval_task_values[-1]
        if not isinstance(node, ast.Call):
            return False
        callee = self._resolved_call_name(node)
        if _is_approval_execute_call(node, aliases=self.aliases[-1]) or callee in self.approval_execute_wrappers:
            return True
        return any(
            isinstance(child, ast.Call)
            and (
                _is_approval_execute_call(
                    child,
                    aliases=self.aliases[-1],
                )
                or self._resolved_call_name(child) in self.approval_execute_wrappers
            )
            for child in ast.walk(node)
        )

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._scan_sql(node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        literal = "".join(value.value for value in node.values if isinstance(value, ast.Constant) and isinstance(value.value, str))
        self._scan_sql(literal)
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        literal = _literal_string_concat(node)
        if literal is None:
            self.generic_visit(node)
            return
        self._scan_sql(literal)

    def _scan_sql(self, text: str) -> None:
        for table, pattern in _EFFECT_DETAIL_DELETE_PATTERNS:
            if pattern.search(text):
                self._add_ingress("effect_detail_delete", table)
        for category, pattern in _SQL_PATTERNS:
            if pattern.search(text):
                self._add("direct_sql", category)

    def _add(self, kind: str, detail: str) -> None:
        self.findings.append(
            TerminalIngressFinding(
                kind=kind,
                source=self.source_name,
                owner=".".join(self.owners),
                detail=detail,
            )
        )

    @property
    def _qualified_owner(self) -> str:
        return ".".join(self.qualified_owners)

    def _add_ingress(self, kind: str, detail: str, *, owner: str | None = None) -> None:
        self.findings.append(
            TerminalIngressFinding(
                kind=kind,
                source=self.source_name,
                owner=owner or self._qualified_owner,
                detail=detail,
            )
        )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _literal_string_concat(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
        return None
    left = _literal_string_concat(node.left)
    right = _literal_string_concat(node.right)
    if left is None or right is None:
        return None
    return left + right


def _task_spawn_detail(node: ast.Call, callee: str) -> str:
    if callee == "create_detached_auxiliary_task":
        reason = next(
            (
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)
            ),
            "unresolved",
        )
        return f"{callee}:{reason}"
    if node.args and isinstance(node.args[0], ast.Call):
        target = _call_name(node.args[0]) or "unresolved"
    elif node.args and isinstance(node.args[0], ast.Name):
        target = node.args[0].id
    else:
        target = "unresolved"
    return f"{callee}:{target}"


def _literal_event_type(node: ast.Call) -> str | None:
    for keyword in node.keywords:
        if keyword.arg == "event_type" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value if isinstance(keyword.value.value, str) else None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _literal_helper_event_type(node: ast.Call) -> str | None:
    for keyword in node.keywords:
        if keyword.arg == "event_type" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value if isinstance(keyword.value.value, str) else None
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        return node.args[1].value
    return None


def _assigned_names(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for item in node.elts for name in _assigned_names(item))
    return ()


def _caught_exception_names(node: ast.expr | None) -> frozenset[str]:
    if node is None:
        return frozenset()
    if isinstance(node, (ast.Tuple, ast.List)):
        return frozenset(name for item in node.elts for name in _caught_exception_names(item))
    if isinstance(node, ast.Name):
        return frozenset({node.id})
    if isinstance(node, ast.Attribute):
        return frozenset({node.attr})
    return frozenset()


def _is_terminal_feature_expression(node: ast.expr) -> bool:
    identifiers = {
        child.id.lower() if isinstance(child, ast.Name) else child.attr.lower()
        for child in ast.walk(node)
        if isinstance(child, (ast.Name, ast.Attribute))
    }
    return any("terminal" in value and ("feature" in value or "enabled" in value) for value in identifiers)


def scan_terminal_ingress(sources: Mapping[str, str]) -> tuple[TerminalIngressFinding, ...]:
    findings: list[TerminalIngressFinding] = []
    for source_name, text in sorted(sources.items()):
        tree = ast.parse(text, filename=source_name)
        visitor = _Visitor(
            source_name,
            approval_execute_wrappers=_approval_execute_wrapper_names(tree),
        )
        visitor.visit(tree)
        findings.extend(visitor.findings)
    counted = [finding for finding in findings if finding.kind in _COUNTED_FINDING_KINDS]
    deduplicated = {finding for finding in findings if finding.kind not in _COUNTED_FINDING_KINDS}
    return tuple(sorted((*deduplicated, *counted)))


def _approval_execute_wrapper_names(tree: ast.AST) -> frozenset[str]:
    wrappers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(child, ast.Call) and _is_approval_execute_call(child, aliases={}) for child in ast.walk(node) if child is not node):
            wrappers.add(node.name)
    return frozenset(wrappers)


def _is_approval_execute_call(
    node: ast.Call,
    *,
    aliases: Mapping[str, str],
) -> bool:
    if isinstance(node.func, ast.Name):
        return aliases.get(node.func.id) == "execute"
    return bool(isinstance(node.func, ast.Attribute) and node.func.attr == "execute")


def unexpected_terminal_ingress(
    sources: Mapping[str, str],
    *,
    allowlist: frozenset[str],
) -> tuple[TerminalIngressFinding, ...]:
    return tuple(finding for finding in scan_terminal_ingress(sources) if finding.identity not in allowlist)


__all__ = [
    "TerminalIngressFinding",
    "scan_terminal_ingress",
    "unexpected_terminal_ingress",
]
