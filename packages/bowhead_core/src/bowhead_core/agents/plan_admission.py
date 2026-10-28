"""计划步的 agent 准入表（agent-capability-routing R1/R2）。

**「这个调用者能不能把这一步派给这个 agent」—— 计划侧的单一判定点。**

### 为什么需要它

在此之前，计划侧三道闸全部只查 `registry.get`（system yaml manifest）：

1. `plan_reify.validate_agents` —— 审批前；
2. `plan_driver._precondition` —— 执行前；
3. `agents/task_runner._run_one` —— 真 spawn 前取 manifest。

而团队自建 agent 存在 `team_subagent` 表里，**不在 registry**。于是聊天能用的 13 个 peer，
到了出计划只剩 8 个内置 —— 团队专门建的「医药竞品调研」「BI 异常分析师」在计划模式里
一个都点不到（spike-planner-roster.md §2）。

### 为什么是「预解析成表」而不是「换掉 manifest_lookup 的契约」

`manifest_lookup: Callable[[str], AgentManifest]` 是**同步**的，而团队解析要读库；
Consumers include task_runner, dispatcher, dag_dispatcher and celery_runner.
Changing that synchronous interface requires coordinating all dispatch callers.

⇒ 这里走窄接缝：**计划的 agent 名字集合是已知的**，开跑前一次性异步解析成表，
之后同步查表。既拿到了 caller-scoped 的正确性，又对既有契约零改动。

### ★ 安全不变量（本模块存在的主要理由）

`register_manifest_as_profile()` 写的是**进程级全局** `PROFILES` dict，而
`SubagentRunner._resolve_profile` 查 `PROFILES` **早于**团队分支。所以一旦为团队 agent
合成一份 manifest 并注册进去：

- 它会绕过团队分支那整套安全归一化（prompt 注入 lint / `filter_destructive` /
  非 analyst 工具降权 / 强制补 `subagent_return` / 模型 clamp）；
- 更糟：`PROFILES` 是进程全局的 ⇒ 一个成员的计划把私有 agent 注册进去之后，
  **同进程内所有人**都解得出它 —— `creator_only` 当场变成假承诺。

⇒ `is_team_sourced()` 就是给 `make_legacy_agent_runner_fn` 用来**拒绝注册**的。
团队 agent 不需要注册：`SubagentRunner.run(profile_name=...)` 内部会经
`resolve_for_caller` 走团队分支解析出**已归一化**的 profile。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from bowhead_core.agents.manifest import AgentManifest
    from bowhead_core.effective_authorization import LayeredDecision

__all__ = [
    "PlanAgentAdmission",
    "build_plan_agent_admission",
    "description_routing_hijack",
    "list_visible_team_subagents",
    "normalize_agent_routing_description",
    "team_agent_runtime_allowed",
    "team_agent_runtime_decision",
    "team_agent_runtime_decisions",
]

# Team agents lack manifests, but `task_runner._run_one` needs an object to estimate timeout.
# This synthetic manifest is **only for timeout estimates and names**, never PROFILES (see module docstring).
_SYNTH_MAX_OUTPUT_TOKENS = 8000
logger = logging.getLogger(__name__)

_DESCRIPTION_SEGMENT_SPLIT = re.compile(r"[.!?;。！？；\n]+")
_EN_FORCE_MODAL_TEXT = r"(?:must)"
_EN_ROUTE_MODAL_TEXT = rf"(?:{_EN_FORCE_MODAL_TEXT}|should|shall|will|would|can|could|may|might)"
_EN_COPULA_TEXT = r"(?:am|is|are|was|were)"
_EN_TO_BE_AUXILIARY_TEXT = r"(?:has|have|had|need|needs)"
_EN_PASSIVE_AUXILIARY_TEXT = rf"(?:(?:{_EN_ROUTE_MODAL_TEXT})\s+be|{_EN_COPULA_TEXT}(?:\s+to\s+be)?|{_EN_TO_BE_AUXILIARY_TEXT}\s+to\s+be)"
_EN_GLOBAL_ROUTE_ADVERB_TEXT = r"(?:always)"
_EN_ROUTE_ADVERB_TEXT = rf"(?:{_EN_GLOBAL_ROUTE_ADVERB_TEXT}|normally|typically|generally|automatically|immediately|directly|ultimately|exclusively)"
_EN_TASK_NOUN_TEXT = r"(?:task|request)"
_EN_TASK_METADATA_HEAD_TEXT = r"(?:ids?|identifiers?|metadata)"
_EN_TASK_METADATA_CHAIN_TEXT = rf"{_EN_TASK_NOUN_TEXT}(?:\s+and\s+{_EN_TASK_NOUN_TEXT})*\s+{_EN_TASK_METADATA_HEAD_TEXT}"
_EN_COMPOUND_TASK_NOUN = re.compile(
    rf"\b(?:every|all|any)\s+{_EN_TASK_METADATA_CHAIN_TEXT}\b",
    re.IGNORECASE,
)
# A scope noun must end here.  Without the follower lookahead, `every request`
# incorrectly matches the prefix of compound nouns such as `every request ID`
# or `all request metadata`, turning benign data-handling copy into a routing
# directive.  The allowlist contains grammatical continuations of a complete
# task/request noun phrase, not arbitrary following nouns.
_EN_SCOPE_FOLLOWER = (
    rf"(?:{_EN_ROUTE_MODAL_TEXT}|{_EN_COPULA_TEXT}|{_EN_TO_BE_AUXILIARY_TEXT}|be|been|"
    r"go|goes|default|defaults|by|to|for|against|with|without|as|that|which|who|and|or|"
    rf"in|within|from|{_EN_ROUTE_ADVERB_TEXT})"
)
_EN_TASK_SCOPE_TEXT = rf"(?:every|all|any)\s+(?:tasks?|requests?)(?=\s*(?:$|[,.!?;:]|{_EN_SCOPE_FOLLOWER}\b))"
_MAX_ROUTING_DESCRIPTION_CHARS = 500
_EN_TASK_SCOPE_PHRASE_TEXT = _EN_TASK_SCOPE_TEXT
_EN_ROUTABLE_TASK_SCOPE_PHRASE_TEXT = rf"{_EN_TASK_SCOPE_PHRASE_TEXT}(?:\s+and\s+{_EN_TASK_METADATA_CHAIN_TEXT})?"
_EN_TASK_SCOPE = re.compile(rf"\b{_EN_TASK_SCOPE_TEXT}\b", re.IGNORECASE)
_EN_SCOPE_FRAGMENT = re.compile(r"^(?:for\s+)?(?:every|all|any)\s+(?:tasks?|requests?)$", re.IGNORECASE)
_EN_SCAN_TOKEN = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*|[(),–—]")
_EN_SCOPE_QUANTIFIERS = frozenset({"every", "all", "any"})
_EN_SCOPE_NOUNS = frozenset({"task", "tasks", "request", "requests"})
_EN_SINGULAR_SCOPE_NOUNS = frozenset({"task", "request"})
_EN_TASK_METADATA_HEADS = frozenset({"id", "ids", "identifier", "identifiers", "metadata"})
_EN_SCOPE_PREPOSITIONS = frozenset({"in", "within", "from"})
_EN_CLAUSE_BOUNDARIES = frozenset({",", "–", "—"})
_EN_PAIRED_INSERTION_CLOSE = {",": ",", "–": "–", "—": "—", "(": ")"}
_EN_SYMMETRIC_INSERTION_DELIMITERS = frozenset({",", "–", "—"})
_EN_CLAUSE_CONNECTORS = frozenset({"and", "but", "while", "whereas", "although", "however"})
_EN_SCOPE_PRONOUNS = frozenset({"it", "they", "them", "these", "those"})
_EN_ROUTE_MODALS = frozenset({"must", "should", "shall", "will", "would", "can", "could", "may", "might"})
_EN_COPULAS = frozenset({"am", "is", "are", "was", "were"})
_EN_TO_BE_AUXILIARIES = frozenset({"has", "have", "had", "need", "needs"})
_EN_ROUTE_ADVERBS = frozenset(
    {"always", "normally", "typically", "generally", "automatically", "immediately", "directly", "ultimately", "exclusively"},
)
_EN_SELF_OBJECT = r"(?:me|this\s+(?:agent|assistant))"
_EN_SELF_SUBJECT = r"(?:i|this\s+(?:agent|assistant))"
_EN_CHOOSE_SELF = re.compile(
    rf"\b(?:choose|select|prefer|use)\s+{_EN_SELF_OBJECT}\b",
    re.IGNORECASE,
)
_EN_ASSIGN_TO_SELF = re.compile(
    rf"\b(?:assign|route|delegate)(?:\s+\w+){{0,12}}\s+(?:to\s+)?{_EN_SELF_OBJECT}\b",
    re.IGNORECASE,
)
_EN_ACTION_FRAGMENT = re.compile(
    rf"^(?:please\s+)?(?:(?:choose|select|prefer|use)\s+{_EN_SELF_OBJECT}|"
    rf"(?:assign|route|delegate)\s+(?:to\s+)?{_EN_SELF_OBJECT})$",
    re.IGNORECASE,
)
_EN_PASSIVE_SELF_SELECTION = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+{_EN_PASSIVE_AUXILIARY_TEXT}\s+"
    rf"(?:{_EN_ROUTE_ADVERB_TEXT}\s+)?(?:chosen|selected|preferred|routed)\b",
    re.IGNORECASE,
)
_EN_SELF_ASSIGNED_TASK_SCOPE = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+{_EN_PASSIVE_AUXILIARY_TEXT}\s+"
    rf"(?:{_EN_ROUTE_ADVERB_TEXT}\s+)?assigned\s+(?:to\s+)?{_EN_ROUTABLE_TASK_SCOPE_PHRASE_TEXT}\b",
    re.IGNORECASE,
)
_EN_SELF_USED_FOR_TASK_SCOPE = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+{_EN_PASSIVE_AUXILIARY_TEXT}\s+"
    rf"(?:{_EN_ROUTE_ADVERB_TEXT}\s+)?used\s+for\s+{_EN_ROUTABLE_TASK_SCOPE_PHRASE_TEXT}\b",
    re.IGNORECASE,
)
_EN_SELF_DEFAULT_AGENT = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+{_EN_COPULA_TEXT}\s+(?:the\s+)?(?:default|go-to)\s+(?:agent|assistant)\b",
    re.IGNORECASE,
)
_EN_SELF_HANDLES_SCOPE = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+(?:{_EN_ROUTE_MODAL_TEXT}\s+)?(?:{_EN_ROUTE_ADVERB_TEXT}\s+)?"
    rf"(?:handle|handles|own|owns)\s+{_EN_ROUTABLE_TASK_SCOPE_PHRASE_TEXT}\b",
    re.IGNORECASE,
)
_EN_SELF_RESPONSIBLE_FOR_SCOPE = re.compile(
    rf"\b{_EN_SELF_SUBJECT}\s+{_EN_COPULA_TEXT}\s+(?:{_EN_ROUTE_ADVERB_TEXT}\s+)?"
    rf"responsible\s+for\s+{_EN_ROUTABLE_TASK_SCOPE_PHRASE_TEXT}\b",
    re.IGNORECASE,
)
_EN_ABSOLUTE_DIRECTIVE = re.compile(
    rf"\b(?:{_EN_GLOBAL_ROUTE_ADVERB_TEXT}|{_EN_FORCE_MODAL_TEXT}|by\s+default|default)\b",
    re.IGNORECASE,
)
_EN_SELF_PRIORITY = re.compile(
    r"\b(?:i\s+am|this\s+(?:agent|assistant)\s+is|this\s+is)\s+(?:the\s+)?"
    r"(?:highest|top|first)\s+priority\s+(?:agent|assistant)\b",
    re.IGNORECASE,
)
_ZH_TASK_SCOPE_TEXT = r"(?:所有|全部|任何|每个)(?:的)?(?:任务|请求)"
_ZH_TASK_SCOPE = re.compile(_ZH_TASK_SCOPE_TEXT)
_ZH_SCOPE_FRAGMENT = re.compile(r"^(?:所有|全部|任何|每个)(?:的)?(?:任务|请求)(?:默认)?$")
_ZH_SELF_REFERENCE_TEXT = (
    r"(?:我(?!的)|本助手|本智能体|本代理|该\s*Agent|这个\s*Agent|该智能体|这个智能体|"
    r"该代理|这个代理|该助手|这个助手)"
)
_ZH_CHOOSE_SELF = re.compile(
    rf"(?:选择|选用|首选|使用|选|用)(?:了|为|成)?\s*{_ZH_SELF_REFERENCE_TEXT}",
    re.IGNORECASE,
)
_ZH_ASSIGN_TO_SELF = re.compile(
    rf"(?:交给|派给|分配给|路由给)(?:了)?\s*{_ZH_SELF_REFERENCE_TEXT}",
    re.IGNORECASE,
)
_ZH_ROUTE_CONNECTOR_TEXT = r"(?:默认|一律|优先|都|均|应当|应该|可以|必须|应|可|需)"
_ZH_ACTION_FRAGMENT = re.compile(
    rf"^(?:{_ZH_ROUTE_CONNECTOR_TEXT}\s*)?"
    rf"(?:(?:选择|选用|首选|使用|选|用|交给|派给|分配给|路由给|归|找)\s*{_ZH_SELF_REFERENCE_TEXT})$",
    re.IGNORECASE,
)
_ZH_SCOPE_OWNED_BY_SELF = re.compile(
    rf"(?:由|让)?\s*{_ZH_SELF_REFERENCE_TEXT}(?:来)?\s*(?:{_ZH_ROUTE_CONNECTOR_TEXT})?\s*"
    r"(?:负责|处理|承接|执行)(?!\s*的)",
    re.IGNORECASE,
)
_ZH_SCOPE_TO_SELF = re.compile(
    rf"{_ZH_TASK_SCOPE_TEXT}(?:{_ZH_ROUTE_CONNECTOR_TEXT}\s*){{0,5}}(?:归|找)\s*{_ZH_SELF_REFERENCE_TEXT}",
    re.IGNORECASE,
)
_ZH_SELF_MUST_HANDLE = re.compile(
    rf"{_ZH_SELF_REFERENCE_TEXT}(?:必须|始终|总是|默认)(?:负责|处理|承接|执行)",
    re.IGNORECASE,
)
_ZH_ABSOLUTE_DIRECTIVE = re.compile(r"(?:永远|始终|总是|必须|默认)")
_ZH_SELF_PRIORITY = re.compile(
    r"(?:我|本助手|本智能体|本代理|该\s*Agent|这个\s*Agent|该智能体|这个智能体|"
    r"该代理|这个代理|该助手|这个助手)(?:是|为).{0,4}"
    r"(?:最高优先级|第一优先级|最优先)(?:的)?(?:智能体|代理|助手|Agent)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlanAgentAdmission:
    """一次性解析出来的准入表；之后同步查，零 IO。"""

    manifests: dict[str, AgentManifest]
    team_sourced: frozenset[str]

    def lookup(self, name: str) -> AgentManifest:
        """与既有 `manifest_lookup` 契约一致：未准入 → `KeyError`。

        三道闸共用它，所以「审批前判过的」与「执行时判的」必然是同一个答案 ——
        此前两侧各查一次 registry，行为一致纯属它们查的是同一张表。
        """
        try:
            return self.manifests[name]
        except KeyError:
            raise KeyError(name) from None

    def is_team_sourced(self, name: str) -> bool:
        """★ 这个名字是团队/自建来源 ⇒ **不许注册进全局 PROFILES**（见模块 docstring）。"""
        return name in self.team_sourced

    @property
    def admitted(self) -> frozenset[str]:
        return frozenset(self.manifests)


def _self_object_end(tokens: tuple[str, ...], index: int) -> int | None:
    if index < len(tokens) and tokens[index] == "me":
        return index + 1
    if index + 1 < len(tokens) and tokens[index] == "this" and tokens[index + 1] in {"agent", "assistant"}:
        return index + 2
    return None


def _metadata_chain_end(tokens: tuple[str, ...], index: int) -> int | None:
    """Return the end of `request/task (+ and ...)+ ID/metadata`."""
    if index >= len(tokens) or tokens[index] not in _EN_SINGULAR_SCOPE_NOUNS:
        return None
    cursor = index + 1
    while cursor + 1 < len(tokens) and tokens[cursor] == "and" and tokens[cursor + 1] in _EN_SINGULAR_SCOPE_NOUNS:
        cursor += 2
    return cursor + 1 if cursor < len(tokens) and tokens[cursor] in _EN_TASK_METADATA_HEADS else None


def _execution_scope_end(tokens: tuple[str, ...], index: int) -> int | None:
    """Parse one executable all/every/any request/task scope.

    Singular task nouns followed by ID/identifier/metadata are data labels, not
    executable scopes. A plural scope may still coordinate such a label, as in
    `all requests and task IDs`; the plural scope remains routable.
    """
    if index + 1 >= len(tokens) or tokens[index] not in _EN_SCOPE_QUANTIFIERS or tokens[index + 1] not in _EN_SCOPE_NOUNS:
        return None
    noun = tokens[index + 1]
    cursor = index + 2
    metadata_end = _metadata_chain_end(tokens, index + 1)
    if noun in _EN_SINGULAR_SCOPE_NOUNS and metadata_end is not None:
        return None
    if noun not in _EN_SINGULAR_SCOPE_NOUNS and cursor < len(tokens) and tokens[cursor] == "and":
        metadata_end = _metadata_chain_end(tokens, cursor + 1)
        if metadata_end is not None:
            cursor = metadata_end
    return cursor


def _skip_paired_insertion(
    tokens: tuple[str, ...],
    index: int,
    *,
    insertion_run_end: tuple[int, ...],
) -> int:
    """Skip consecutive comma/dash/parenthesis insertions in O(1)."""
    if index >= len(tokens) or tokens[index] not in _EN_PAIRED_INSERTION_CLOSE:
        return index
    return insertion_run_end[index] if insertion_run_end[index] > index else index


def _scope_route_predicate_to_self(
    tokens: tuple[str, ...],
    index: int,
    *,
    insertion_run_end: tuple[int, ...],
) -> bool:
    """Match one complete, directed route predicate at ``index``.

    This deliberately does not treat an isolated modal/adverb/default token as
    a boundary: names such as `Always On Support`, `May intake`, `Will Call`,
    and `default customer` remain ordinary scope-modifier text.
    """
    cursor = index
    passive = False
    if cursor < len(tokens) and tokens[cursor] in _EN_ROUTE_MODALS:
        cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
        if cursor < len(tokens) and tokens[cursor] == "be":
            passive = True
            cursor += 1
    elif cursor < len(tokens) and tokens[cursor] in _EN_COPULAS:
        passive = True
        cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
        if cursor < len(tokens) and tokens[cursor] == "to":
            cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
            if cursor >= len(tokens) or tokens[cursor] != "be":
                return False
            cursor += 1
    elif cursor < len(tokens) and tokens[cursor] in _EN_TO_BE_AUXILIARIES:
        cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
        if cursor >= len(tokens) or tokens[cursor] != "to":
            return False
        cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
        if cursor >= len(tokens) or tokens[cursor] != "be":
            return False
        passive = True
        cursor += 1

    cursor = _skip_paired_insertion(tokens, cursor, insertion_run_end=insertion_run_end)

    if cursor < len(tokens) and tokens[cursor] in _EN_ROUTE_ADVERBS:
        cursor += 1

    if passive:
        if cursor >= len(tokens):
            return False
        verb = tokens[cursor]
        cursor += 1
        if verb in {"assigned", "routed", "delegated"}:
            return cursor < len(tokens) and tokens[cursor] == "to" and _self_object_end(tokens, cursor + 1) is not None
        if verb in {"chosen", "selected", "preferred", "handled"}:
            return cursor < len(tokens) and tokens[cursor] == "by" and _self_object_end(tokens, cursor + 1) is not None
        return False

    if cursor < len(tokens) and tokens[cursor] in {"go", "goes", "belong", "belongs", "default", "defaults"}:
        cursor += 1
        return cursor < len(tokens) and tokens[cursor] == "to" and _self_object_end(tokens, cursor + 1) is not None
    return False


def _selection_action_to_self(tokens: tuple[str, ...], index: int) -> bool:
    return index < len(tokens) and tokens[index] in {"choose", "select", "prefer", "use"} and _self_object_end(tokens, index + 1) is not None


def _to_self(tokens: tuple[str, ...], index: int) -> bool:
    return index < len(tokens) and tokens[index] == "to" and _self_object_end(tokens, index + 1) is not None


def _directed_route_result_to_self(tokens: tuple[str, ...], index: int) -> bool:
    if index >= len(tokens):
        return False
    verb = tokens[index]
    if verb in {"assigned", "routed", "delegated"}:
        return _to_self(tokens, index + 1)
    if verb in {"chosen", "selected", "preferred", "handled"}:
        return index + 1 < len(tokens) and tokens[index + 1] == "by" and _self_object_end(tokens, index + 2) is not None
    if verb in {"go", "goes", "belong", "belongs", "default", "defaults"}:
        return _to_self(tokens, index + 1)
    return False


def _completed_non_route_predicate(
    tokens: tuple[str, ...],
    index: int,
    *,
    titlecase_tokens: frozenset[int],
    insertion_run_end: tuple[int, ...],
) -> bool:
    """Recognize only finite predicate heads needed to fence a prior scope."""
    if index >= len(tokens) or _scope_route_predicate_to_self(
        tokens,
        index,
        insertion_run_end=insertion_run_end,
    ):
        return False
    token = tokens[index]
    if token not in _EN_COPULAS | _EN_TO_BE_AUXILIARIES | _EN_ROUTE_MODALS or index in titlecase_tokens:
        return False
    # `Are`, `Has`, `May intake`, and `Will Call` can occur in proper names. A title-cased auxiliary
    # inside a modifier is not evidence that a non-routing predicate finished.
    cursor = _skip_paired_insertion(tokens, index + 1, insertion_run_end=insertion_run_end)
    if cursor >= len(tokens) or tokens[cursor] in _EN_CLAUSE_BOUNDARIES:
        return False
    if token in _EN_ROUTE_MODALS and tokens[cursor] != "be":
        return True
    if token in _EN_COPULAS and tokens[cursor] != "to":
        return True
    if token in _EN_TO_BE_AUXILIARIES and tokens[cursor] != "to":
        return True
    if tokens[cursor] == "to":
        cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
        if cursor >= len(tokens) or tokens[cursor] != "be":
            return cursor < len(tokens) and tokens[cursor] not in _EN_CLAUSE_BOUNDARIES
    cursor = _skip_paired_insertion(tokens, cursor + 1, insertion_run_end=insertion_run_end)
    if cursor < len(tokens) and tokens[cursor] in _EN_ROUTE_ADVERBS:
        cursor += 1
    return cursor < len(tokens) and tokens[cursor] not in _EN_CLAUSE_BOUNDARIES


def _starts_independent_clause_subject(
    tokens: tuple[str, ...],
    boundary_index: int,
    *,
    next_non_boundary: tuple[int, ...],
) -> bool:
    """Return whether a clause boundary introduces a non-coreferential subject."""
    cursor = next_non_boundary[boundary_index]
    if cursor < len(tokens) and tokens[cursor] in _EN_CLAUSE_CONNECTORS:
        cursor = next_non_boundary[cursor]
    if cursor >= len(tokens) or tokens[cursor] in _EN_SCOPE_PRONOUNS:
        return False
    return not (
        tokens[cursor] in _EN_ROUTE_MODALS
        or tokens[cursor] in _EN_COPULAS
        or tokens[cursor] in _EN_TO_BE_AUXILIARIES
        or tokens[cursor] in _EN_ROUTE_ADVERBS
        or tokens[cursor] in {"go", "goes", "belong", "belongs", "default", "defaults"}
        or _selection_action_to_self(tokens, cursor)
        or tokens[cursor] in {"assign", "route", "delegate"}
    )


def _starts_independent_parenthetical_subject(
    tokens: tuple[str, ...],
    opening_index: int,
    *,
    insertion_run_end: tuple[int, ...],
) -> bool:
    cursor = opening_index + 1
    if cursor < len(tokens) and tokens[cursor] in _EN_CLAUSE_CONNECTORS:
        cursor += 1
    if cursor >= len(tokens):
        return False
    metadata_cursor = cursor
    if tokens[metadata_cursor] in {"a", "an", "the", "this", "its"}:
        metadata_cursor += 1
    if metadata_cursor < len(tokens) and tokens[metadata_cursor] in _EN_SINGULAR_SCOPE_NOUNS:
        metadata_cursor += 1
    if metadata_cursor < len(tokens) and tokens[metadata_cursor] in _EN_TASK_METADATA_HEADS:
        predicate_cursor = metadata_cursor + 1
        return _scope_route_predicate_to_self(
            tokens,
            predicate_cursor,
            insertion_run_end=insertion_run_end,
        )
    if tokens[cursor] == "which":
        return (
            opening_index > 0
            and tokens[opening_index - 1] in _EN_TASK_METADATA_HEADS
            and _scope_route_predicate_to_self(
                tokens,
                cursor + 1,
                insertion_run_end=insertion_run_end,
            )
        )
    return False


def _english_scope_routes_to_self(segment: str) -> bool:
    """Linear, clause-aware scan for `global scope -> this Agent`."""
    matches = tuple(_EN_SCAN_TOKEN.finditer(segment))
    tokens = tuple(match.group(0).casefold() for match in matches)
    titlecase_tokens = frozenset(index for index, match in enumerate(matches) if match.group(0)[:1].isupper())
    paired_insertion_rows = [len(tokens)] * len(tokens)
    next_symmetric_delimiter = {delimiter: len(tokens) for delimiter in _EN_SYMMETRIC_INSERTION_DELIMITERS}
    next_non_boundary_rows = [len(tokens)] * len(tokens)
    next_non_boundary_index = len(tokens)
    for token_index in range(len(tokens) - 1, -1, -1):
        if tokens[token_index] in _EN_SYMMETRIC_INSERTION_DELIMITERS:
            paired_insertion_rows[token_index] = next_symmetric_delimiter[tokens[token_index]]
            next_symmetric_delimiter[tokens[token_index]] = token_index
        next_non_boundary_rows[token_index] = next_non_boundary_index
        if tokens[token_index] not in _EN_CLAUSE_BOUNDARIES:
            next_non_boundary_index = token_index
    parenthesis_stack: list[int] = []
    unbalanced_parentheses = False
    for token_index, token in enumerate(tokens):
        if token == "(":
            parenthesis_stack.append(token_index)
        elif token == ")":
            if parenthesis_stack:
                paired_insertion_rows[parenthesis_stack.pop()] = token_index
            else:
                unbalanced_parentheses = True
    if parenthesis_stack:
        unbalanced_parentheses = True
    insertion_run_rows = list(range(len(tokens)))
    for token_index in range(len(tokens) - 1, -1, -1):
        closing_index = paired_insertion_rows[token_index]
        if closing_index >= len(tokens):
            continue
        after_insertion = closing_index + 1
        insertion_run_rows[token_index] = (
            insertion_run_rows[after_insertion]
            if after_insertion < len(tokens) and insertion_run_rows[after_insertion] > after_insertion
            else after_insertion
        )
    insertion_run_end = tuple(insertion_run_rows)
    next_non_boundary = tuple(next_non_boundary_rows)
    unbalanced_symmetric_delimiter = any(sum(token == delimiter for token in tokens) % 2 for delimiter in _EN_SYMMETRIC_INSERTION_DELIMITERS)
    unbalanced_paired_delimiter = unbalanced_parentheses or unbalanced_symmetric_delimiter
    active_scope = False
    completed_non_route_predicate = False
    selection_action_seen = False
    assignment_action_seen = False
    assignment_scope = False
    suspended_scope: list[tuple[int, tuple[bool, bool, bool, bool, bool]]] = []
    index = 0
    while index < len(tokens):
        if suspended_scope and suspended_scope[-1][0] == index:
            (
                active_scope,
                completed_non_route_predicate,
                selection_action_seen,
                assignment_action_seen,
                assignment_scope,
            ) = suspended_scope.pop()[1]
            index += 1
            continue

        if (
            tokens[index] == "("
            and paired_insertion_rows[index] < len(tokens)
            and active_scope
            and completed_non_route_predicate
            and _starts_independent_parenthetical_subject(
                tokens,
                index,
                insertion_run_end=insertion_run_end,
            )
        ):
            suspended_scope.append(
                (
                    paired_insertion_rows[index],
                    (
                        active_scope,
                        completed_non_route_predicate,
                        selection_action_seen,
                        assignment_action_seen,
                        assignment_scope,
                    ),
                ),
            )
            active_scope = False
            completed_non_route_predicate = False
            selection_action_seen = False
            assignment_action_seen = False
            assignment_scope = False
            index += 1
            continue

        if tokens[index] in _EN_CLAUSE_BOUNDARIES:
            starts_new_subject = _starts_independent_clause_subject(
                tokens,
                index,
                next_non_boundary=next_non_boundary,
            )
            if starts_new_subject:
                selection_action_seen = False
                assignment_action_seen = False
                assignment_scope = False
                if completed_non_route_predicate:
                    active_scope = False
                    completed_non_route_predicate = False
            index = next_non_boundary[index]
            continue

        if (
            active_scope
            and completed_non_route_predicate
            and tokens[index] in _EN_CLAUSE_CONNECTORS
            and _starts_independent_clause_subject(
                tokens,
                index,
                next_non_boundary=next_non_boundary,
            )
        ):
            active_scope = False
            completed_non_route_predicate = False
            selection_action_seen = False
            assignment_action_seen = False
            assignment_scope = False

        if _selection_action_to_self(tokens, index):
            if active_scope:
                return True
            selection_action_seen = True
        elif tokens[index] in {"assign", "route", "delegate"}:
            assignment_action_seen = True
            assignment_scope = active_scope

        scope_end = _execution_scope_end(tokens, index)
        if scope_end is not None:
            if selection_action_seen:
                return True
            active_scope = True
            completed_non_route_predicate = False
            assignment_scope = assignment_action_seen
            if _scope_route_predicate_to_self(
                tokens,
                scope_end,
                insertion_run_end=insertion_run_end,
            ):
                return True
            index = scope_end
            continue

        if active_scope:
            if unbalanced_paired_delimiter and _directed_route_result_to_self(tokens, index):
                return True
            if _scope_route_predicate_to_self(
                tokens,
                index,
                insertion_run_end=insertion_run_end,
            ) or (assignment_scope and _to_self(tokens, index)):
                return True
            if _completed_non_route_predicate(
                tokens,
                index,
                titlecase_tokens=titlecase_tokens,
                insertion_run_end=insertion_run_end,
            ):
                completed_non_route_predicate = True
        index += 1
    return False


def description_routing_hijack(value: object) -> bool:
    """Return whether description copy tries to control Agent selection."""
    text = unicodedata.normalize("NFKC", str(value or ""))[:_MAX_ROUTING_DESCRIPTION_CHARS]
    # Mask the whole compound noun before looking for scope phrases.  A
    # follower-only rule cannot safely handle `all request and task IDs`: it
    # would stop at `and` and treat `all request` as an execution scope.
    text = _EN_COMPOUND_TASK_NOUN.sub(" compound-scope-noun ", text)
    segments = [segment.strip() for segment in _DESCRIPTION_SEGMENT_SPLIT.split(text) if segment.strip()]
    for index, segment in enumerate(segments):
        english_route_to_self = _EN_CHOOSE_SELF.search(segment) or _EN_ASSIGN_TO_SELF.search(segment)
        if english_route_to_self and _EN_ABSOLUTE_DIRECTIVE.search(segment):
            return True
        if _EN_PASSIVE_SELF_SELECTION.search(segment) and (_EN_TASK_SCOPE.search(segment) or _EN_ABSOLUTE_DIRECTIVE.search(segment)):
            return True
        if (
            _EN_SELF_ASSIGNED_TASK_SCOPE.search(segment)
            or _EN_SELF_USED_FOR_TASK_SCOPE.search(segment)
            or _EN_SELF_DEFAULT_AGENT.search(segment)
            or _EN_SELF_HANDLES_SCOPE.search(segment)
            or _EN_SELF_RESPONSIBLE_FOR_SCOPE.search(segment)
            or _english_scope_routes_to_self(segment)
        ):
            return True

        chinese_route_to_self = _ZH_CHOOSE_SELF.search(segment) or _ZH_ASSIGN_TO_SELF.search(segment)
        if chinese_route_to_self and (_ZH_TASK_SCOPE.search(segment) or _ZH_ABSOLUTE_DIRECTIVE.search(segment)):
            return True
        if (_ZH_SCOPE_OWNED_BY_SELF.search(segment) and _ZH_TASK_SCOPE.search(segment)) or _ZH_SCOPE_TO_SELF.search(segment):
            return True
        if _ZH_SELF_MUST_HANDLE.search(segment) and _ZH_TASK_SCOPE.search(segment):
            return True
        if _EN_SELF_PRIORITY.search(segment) or _ZH_SELF_PRIORITY.search(segment):
            return True

        # A short scope-only sentence may qualify the immediately following
        # imperative ("Every task. Choose me.").  Do not join arbitrary prose:
        # only an exact scope fragment and the next directed self-selection
        # clause participate.
        if index + 1 < len(segments):
            next_segment = segments[index + 1]
            if (_EN_SCOPE_FRAGMENT.fullmatch(segment) and _EN_ACTION_FRAGMENT.fullmatch(next_segment)) or (
                _EN_ACTION_FRAGMENT.fullmatch(segment) and _EN_SCOPE_FRAGMENT.fullmatch(next_segment)
            ):
                return True
            if (_ZH_SCOPE_FRAGMENT.fullmatch(segment) and _ZH_ACTION_FRAGMENT.fullmatch(next_segment)) or (
                _ZH_ACTION_FRAGMENT.fullmatch(segment) and _ZH_SCOPE_FRAGMENT.fullmatch(next_segment)
            ):
                return True
    return False


def normalize_agent_routing_description(value: object) -> tuple[str, str | None]:
    """Normalize untrusted routing copy and apply the shared hijack lint.

    Descriptions are injected into Lead/Planner rosters, so they have the same
    trust boundary as a user-authored prompt.  The second return value is a
    stable safety reason; callers must not publish or route the rejected text.
    """
    from bowhead_core.agents.exceptions import ManifestSecurityError
    from bowhead_core.agents.manifest_security import lint_user_append

    try:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = " ".join(
            "".join(" " if unicodedata.category(char).startswith("C") else char for char in text).split(),
        )[:_MAX_ROUTING_DESCRIPTION_CHARS]
        lint_user_append(text)
        if description_routing_hijack(text):
            return "", "description_routing_hijack"
    except ManifestSecurityError:
        return "", "description_routing_hijack"
    except Exception:
        return "", "description_safety_check_failed"
    return text, None


def _synthesize_manifest(name: str, profile: Any) -> AgentManifest:
    """给团队 agent 造一份**仅供超时估算**的 manifest。

    ⚠ 它不是权威：真正跑起来用的是 `_resolve_profile` 归一化过的 `SubagentProfile`。
    这里刻意只抄 `max_iterations`（`compute_task_timeout` 唯一会看的量），
    其余字段填最保守的合法值 —— 免得有人误以为这份 manifest 可以拿去执行。

    ★ **必须用 `model_construct` 绕开校验**（2026-08-11 独立 review 逮到的真缺陷）：
    `AgentManifest.name` 的 pattern 是 `^[a-z][a-z0-9-]{1,63}$`（**不含下划线**），
    而团队自建 agent 的名字校验是 `^[a-z][a-z0-9_-]{1,31}$`（**下划线合法**，
    见 `team_subagent_validation.py`）。两套规则不一致，于是一个叫 `market_researcher`
    的合法自建 agent 会让这里抛 `ValidationError`；而调用点在 try 之外，异常一路冒到
    `_maybe_propose_plan`（它被 await 时没有 try/except）⇒ **整个 turn 失败**，
    还会留下 `create_plan` 已建的 PROPOSED 孤儿行。
    改用 `model_construct` 而不是「放宽 pattern」：放宽会削弱**真** manifest 的
    文件系统逃逸防护（`system.file` 按 name 解析路径），而这份合成对象根本不落盘、
    不进 PROFILES、只被读 `.name` 与 `.max_iterations`。
    """
    from bowhead_core.agents.manifest import AgentManifest, SystemSpec, ToolsSpec  # noqa: F401

    return AgentManifest.model_construct(
        name=name,
        version=1,
        model="analyst",
        trust_class="read-only",
        max_iterations=max(1, min(200, int(getattr(profile, "max_iterations", 20) or 20))),
        max_output_tokens=_SYNTH_MAX_OUTPUT_TOKENS,
        description=(getattr(profile, "description", "") or name)[:500],
        system=None,
        tools=ToolsSpec(allow=["subagent_return"]),
        skills=None,
        input_schema=None,
        output_schema=None,
        examples=[],
        callable_peers=[],
        produces=[],
    )


async def build_plan_agent_admission(
    *,
    names: Iterable[str],
    registry: Any,
    workspace_id: str | None,
    subagent_runner: Any = None,
    parent_ctx: Any = None,
) -> PlanAgentAdmission:
    """把一组 agent 名字解析成准入表（caller-scoped）。

    优先级与聊天侧一致：**registry(system manifest) → 团队/自建**。
    registry 命中就用它（内置 agent 的权威仍是 yaml manifest，行为逐字节不变）；
    miss 才走 `subagent_runner.resolve_for_caller`，由**那一份**实现判定可见性与降权。

    `subagent_runner` / `parent_ctx` 缺任一 → 只解 registry（老行为，回滚绳）。
    """
    manifests: dict[str, AgentManifest] = {}
    team: set[str] = set()
    for name in dict.fromkeys(names):  # Deduplicate in order.
        if registry is not None:
            # KeyError = absent from registry (normal; use team branch); other errors = registry hiccup,
            # also use team branch rather than discard roster: an empty roster misreports a registry hiccup as no capable agent.
            try:
                manifests[name] = registry.get(name, workspace_id=workspace_id)
                continue
            except Exception:  # noqa: BLE001,S110
                pass
        if subagent_runner is None or parent_ctx is None:
            continue
        try:
            profile = await subagent_runner.resolve_for_caller(name, parent_ctx)
        except Exception:  # noqa: BLE001 — resolving one name fails only that entry
            profile = None
        if profile is not None:
            manifests[name] = _synthesize_manifest(name, profile)
            team.add(name)
    return PlanAgentAdmission(manifests=manifests, team_sourced=frozenset(team))


async def list_visible_team_subagents(
    store: Any,
    *,
    team_id: str | None,
    user_id: str | None,
    shared_grant_ids: Any = None,
) -> list[tuple[str, str, tuple[str, ...]]]:
    """这个调用者**看得见**哪些团队自建 agent → `[(name, description, native_tool_refs)]`。

    第三项是原生工具及已审定 Power BI 内置工具名，给 A 轴能力派生用
    （`derive_capabilities` 直接吃它：实测 pharma-competitive-research → {kb, web}）。
    ⚠ 它只用于**花名册展示与路由**，不是执行权限 —— 真执行时的白名单由
    `_resolve_profile` 的团队分支做降权后决定（安全逻辑只有那一份）。

    ★ 可见性判定只有这一份：`creator_only` 的 agent 只对创建者（或"分享给我"预取集里
    的那些）可见。聊天侧的 peer 花名册与 planner 花名册都调它 —— 若各写一遍，
    两边迟早漂移，而漂移的方向是**把别人的私有 agent 广播出去**。

    停用（`only_enabled=True`）与退休名单在此一并过滤。
    store 缺失/抛异常 → 返回空列表（团队自建能力关闭时的老行为）。

    ★★ 判据 6（§5.6.1 的兄弟判据）：**进花名册的 Agent 必须是执行准入会放行的**。
    本函数与 `agent_execution_binding.dispatchable_team_agent_ids` 求交 —— 那是准入
    自己的持久定义闸，不是这里重写的第二份。广告面宽于执行面时，Lead 会派一个必被拒的
    peer，用户看到的是「静默换人」或裸错误码。Legacy mutable definitions without a current release stay out of the roster.
    """
    from bowhead_core.subagent import RETIRED_SUBAGENT_NAMES

    if not team_id or store is None:
        return []
    if getattr(store, "db_path", None) is not None and not user_id:
        return []
    try:
        rows = await store.list_team_subagents(team_id, only_enabled=True)
    except Exception:  # noqa: BLE001 — unreadable team table must not destroy the entire roster
        return []
    dispatchable_ids: frozenset[str] | None = None
    if getattr(store, "db_path", None) is not None:
        try:
            from bowhead_core.agent_execution_binding import (
                dispatchable_team_agent_ids,
            )

            dispatchable_ids = await dispatchable_team_agent_ids(store.db_path, team_id=team_id)
        except Exception:  # noqa: BLE001 — unreadable release state cannot establish an effective version
            logger.warning("agent dispatchable gate unavailable; roster refused", exc_info=True)
            return []
    assigned_agent_ids: frozenset[str] | None = None
    access_decisions: dict[str, Any] | None = None
    if user_id and getattr(store, "db_path", None) is not None:
        try:
            from bowhead_core.agent_assignment_store import AgentAssignmentStore

            assignment_store = AgentAssignmentStore(store.db_path)
            schema_state = await assignment_store.runtime_schema_state()
        except Exception:  # noqa: BLE001 — ambiguous schema state must fail closed
            return []
        if schema_state != "cutover":
            return []
        try:
            assigned_agent_ids = await assignment_store.admissible_agent_ids(
                team_id=team_id,
                user_id=user_id,
            )
            from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

            access_decisions = await TeamSubagentAccessResolver(store.db_path).resolve_many(
                team_id=team_id,
                actor_user_id=user_id,
            )
        except Exception:  # noqa: BLE001 — assignment failure must fail closed
            return []
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for s in rows or []:
        name = str(getattr(s, "name", "") or "")
        if not name or name in RETIRED_SUBAGENT_NAMES:
            continue
        agent_id = str(getattr(s, "id", ""))
        if dispatchable_ids is not None and agent_id not in dispatchable_ids:
            continue  # Criterion 6: agents admission would reject stay out of roster.
        if assigned_agent_ids is not None:
            decision = (access_decisions or {}).get(agent_id)
            if agent_id not in assigned_agent_ids or decision is None or not decision.resolution.can_consume:
                continue
        if assigned_agent_ids is None and (
            getattr(s, "visibility", "team_wide") == "creator_only"
            and getattr(s, "created_by", None) != user_id
            and not (shared_grant_ids and getattr(s, "id", None) in shared_grant_ids)
        ):
            continue
        try:
            from bowhead_core.agents.manifest_security import lint_user_append

            lint_user_append(str(getattr(s, "system_prompt", "") or ""))
        except Exception as exc:  # an unsafe or uncheckable prompt never enters the routing roster
            logger.warning("agent routing prompt rejected: %s", type(exc).__name__)
            continue
        from bowhead_core.agent_listing_safety import scan_agent_listing

        listing_safety = scan_agent_listing(
            name=name,
            display_name=getattr(s, "display_name", ""),
            description=getattr(s, "description", ""),
        )
        if listing_safety.blocked:
            continue
        from bowhead_core.agent_capability_catalog import builtin_powerbi_operation

        refs = tuple(
            t for t in (getattr(s, "tool_refs", None) or ()) if not str(t).startswith("teammcp.") or builtin_powerbi_operation(str(t)) is not None
        )
        out.append((name, listing_safety.normalized_description, refs))
    return out


async def team_agent_runtime_allowed(
    store: Any,
    *,
    team_id: str | None,
    user_id: str | None,
    agent_id: str,
) -> bool:
    """Return the execution decision for one team Agent.

    An active personal assignment, current audience, and live release must all
    pass. A missing cutover or unreadable gate fails closed.

    This is ``team_agent_runtime_decision`` with the reason dropped; the criteria live there only.
    """
    decision = await team_agent_runtime_decision(
        store,
        team_id=team_id,
        user_id=user_id,
        agent_id=agent_id,
    )
    return decision.allowed


async def team_agent_runtime_decision(
    store: Any,
    *,
    team_id: str | None,
    user_id: str | None,
    agent_id: str,
    explain_all: bool = False,
    persist_blocked_listing: bool = True,
    materialize_team_defaults: bool = True,
    listing_blocked: bool | None = None,
) -> LayeredDecision:
    """The single implementation of the team-Agent runtime criteria, with the reason kept.

    ``explain_all=False`` (execution) stops at the first refusal exactly as before.
    ``explain_all=True`` (read-only explanation) evaluates every layer so an Owner or admin sees all
    that is missing, not just the first. ``persist_blocked_listing=False`` and
    ``materialize_team_defaults=False`` keep that path write-free while answering the same question.
    ``listing_blocked`` lets a caller that already scanned this request's current definition (the
    catalog page) pass the result instead of scanning it a second time. Execution callers never pass it.
    """
    decisions = await team_agent_runtime_decisions(
        store,
        team_id=team_id,
        user_id=user_id,
        agent_ids=(agent_id,),
        explain_all=explain_all,
        persist_blocked_listing=persist_blocked_listing,
        materialize_team_defaults=materialize_team_defaults,
        listing_blocked=None if listing_blocked is None else {agent_id: listing_blocked},
    )
    return decisions[agent_id]


async def team_agent_runtime_decisions(
    store: Any,
    *,
    team_id: str | None,
    user_id: str | None,
    agent_ids: Sequence[str],
    explain_all: bool = False,
    persist_blocked_listing: bool = True,
    materialize_team_defaults: bool = True,
    listing_blocked: Mapping[str, bool] | None = None,
    dispatchable_agent_ids: frozenset[str] | None = None,
) -> dict[str, LayeredDecision]:
    """``team_agent_runtime_decision`` for many Agents of one user, each layer read once for all of them.

    Per Agent the result is exactly the single call's: a layer runs only for Agents it would have run
    for (with ``explain_all=False`` an Agent already refused is not evaluated further), and a failure
    reading a shared layer refuses that layer for every Agent it was read for, as it would have one by one.
    """
    from bowhead_core.effective_authorization import (
        LAYER_ASSIGNMENT,
        LAYER_ASSIGNMENT_SCHEMA,
        LAYER_IDENTITY,
        LAYER_LISTING_SAFETY,
        LAYER_PROMPT_LINT,
        LAYER_RELEASE_SAFETY,
        LAYER_VISIBILITY,
        LayeredDecision,
        LayerVerdict,
        denied,
        passed,
    )

    ids = tuple(dict.fromkeys(agent_ids))

    def every(decision: LayeredDecision) -> dict[str, LayeredDecision]:
        return dict.fromkeys(ids, decision)

    missing_identity = LayeredDecision((denied(LAYER_IDENTITY, "missing_team_or_agent"),))
    if not team_id:
        return every(missing_identity)
    out: dict[str, LayeredDecision] = {agent_id: missing_identity for agent_id in ids if not agent_id}
    live = [agent_id for agent_id in ids if agent_id]
    if not live:
        return out
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return {**out, **dict.fromkeys(live, LayeredDecision((denied(LAYER_ASSIGNMENT_SCHEMA, "schema_unavailable"),)))}
    if not user_id:
        return {**out, **dict.fromkeys(live, LayeredDecision((denied(LAYER_IDENTITY, "missing_user"),)))}

    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    assignment_store = AgentAssignmentStore(db_path)
    try:
        schema_state = await assignment_store.runtime_schema_state()
    except Exception:
        return {**out, **dict.fromkeys(live, LayeredDecision((denied(LAYER_ASSIGNMENT_SCHEMA, "schema_state_unreadable"),)))}
    if schema_state == "unavailable":
        return {**out, **dict.fromkeys(live, LayeredDecision((denied(LAYER_ASSIGNMENT_SCHEMA, "schema_unavailable"),)))}
    if schema_state != "cutover":
        return {**out, **dict.fromkeys(live, LayeredDecision((denied(LAYER_ASSIGNMENT_SCHEMA, "schema_not_cutover"),)))}

    layers: dict[str, list[LayerVerdict]] = {agent_id: [] for agent_id in live}

    def pending() -> list[str]:
        return [agent_id for agent_id in live if explain_all or not any(layer.verdict == "deny" for layer in layers[agent_id])]

    try:
        active_ids = await assignment_store.admissible_agent_ids(
            team_id=team_id,
            user_id=user_id,
            materialize_team_defaults=materialize_team_defaults,
        )
    except Exception:
        for agent_id in pending():
            layers[agent_id].append(denied(LAYER_ASSIGNMENT, "assignment_unreadable"))
    else:
        refused = [agent_id for agent_id in pending() if agent_id not in active_ids]
        rows: dict[str, Any] | None = {}
        if refused:
            # Say which of the three it is: each has a different next step for the member. One read for the page.
            try:
                rows = await assignment_store.get_many(team_id=team_id, user_id=user_id)
            except Exception:
                rows = None
        for agent_id in pending():
            if agent_id in active_ids:
                layers[agent_id].append(passed(LAYER_ASSIGNMENT))
                continue
            if rows is None:
                layers[agent_id].append(denied(LAYER_ASSIGNMENT, "assignment_unreadable"))
                continue
            row = rows.get(agent_id)
            if row is None or row.personal_state == "removed":
                layers[agent_id].append(denied(LAYER_ASSIGNMENT, "not_hired"))
            elif row.personal_state != "active":
                layers[agent_id].append(denied(LAYER_ASSIGNMENT, "personally_disabled"))
            else:
                layers[agent_id].append(denied(LAYER_ASSIGNMENT, "access_revoked"))

    targets = pending()
    if targets:
        try:
            from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

            decisions = await TeamSubagentAccessResolver(db_path).resolve_subset(
                team_id=team_id,
                actor_user_id=user_id,
                subagent_ids=targets,
            )
        except Exception:
            for agent_id in targets:
                layers[agent_id].append(denied(LAYER_VISIBILITY, "access_unreadable"))
        else:
            for agent_id in targets:
                decision = decisions.get(agent_id)
                if decision is None:
                    layers[agent_id].append(denied(LAYER_VISIBILITY, "agent_not_found"))
                elif not decision.resolution.can_consume:
                    layers[agent_id].append(denied(LAYER_VISIBILITY, decision.resolution.denial_reason or "access_denied"))
                else:
                    layers[agent_id].append(passed(LAYER_VISIBILITY, decision.resolution.access_reason))

    # The roster and the materializer both refuse an Agent whose routing prompt fails the manifest
    # lint; the seam must say the same, or it reports "usable" for an Agent nobody can dispatch.
    targets = pending()
    if targets:
        from bowhead_core.agents.exceptions import ManifestSecurityError
        from bowhead_core.agents.manifest_security import lint_user_append

        try:
            from bowhead_core._db import open_shared_ctx

            marks = ",".join("?" for _ in targets)
            async with (
                open_shared_ctx(db_path) as db,
                db.execute(f"SELECT id, system_prompt FROM team_subagent WHERE team_id=? AND id IN ({marks})", (team_id, *targets)) as cursor,
            ):
                prompts = {str(row[0]): row[1] for row in await cursor.fetchall()}
        except Exception:
            for agent_id in targets:
                layers[agent_id].append(denied(LAYER_PROMPT_LINT, "prompt_unreadable"))
        else:
            for agent_id in targets:
                if agent_id not in prompts:
                    layers[agent_id].append(denied(LAYER_PROMPT_LINT, "agent_not_found"))
                    continue
                try:
                    lint_user_append(str(prompts[agent_id] or ""))
                except ManifestSecurityError:
                    layers[agent_id].append(denied(LAYER_PROMPT_LINT, "prompt_rejected"))
                except Exception:
                    layers[agent_id].append(denied(LAYER_PROMPT_LINT, "prompt_unreadable"))
                else:
                    layers[agent_id].append(passed(LAYER_PROMPT_LINT))

    for agent_id in pending():
        if listing_blocked is not None and agent_id in listing_blocked:
            blocked = listing_blocked[agent_id]
            layers[agent_id].append(denied(LAYER_LISTING_SAFETY, "listing_blocked") if blocked else passed(LAYER_LISTING_SAFETY))
            continue
        try:
            from bowhead_core.agent_listing_safety import AgentListingSafetyStore

            listing_safety = await AgentListingSafetyStore(db_path).scan_current_agent(
                team_id=team_id,
                agent_id=agent_id,
                persist=persist_blocked_listing,
            )
        except Exception:
            layers[agent_id].append(denied(LAYER_LISTING_SAFETY, "listing_safety_unreadable"))
        else:
            if listing_safety is None:
                layers[agent_id].append(denied(LAYER_LISTING_SAFETY, "agent_not_found"))
            elif listing_safety.blocked:
                layers[agent_id].append(denied(LAYER_LISTING_SAFETY, "listing_blocked"))
            else:
                layers[agent_id].append(passed(LAYER_LISTING_SAFETY))

    targets = pending()
    if targets:
        try:
            from bowhead_core.agent_execution_binding import dispatchable_team_agent_ids

            dispatchable_ids = (
                dispatchable_agent_ids if dispatchable_agent_ids is not None else await dispatchable_team_agent_ids(db_path, team_id=team_id)
            )
        except Exception:
            for agent_id in targets:
                layers[agent_id].append(denied(LAYER_RELEASE_SAFETY, "release_gate_unreadable"))
        else:
            for agent_id in targets:
                if agent_id in dispatchable_ids:
                    layers[agent_id].append(passed(LAYER_RELEASE_SAFETY))
                else:
                    layers[agent_id].append(denied(LAYER_RELEASE_SAFETY, "no_live_release_or_safety_not_passed"))
    return {**out, **{agent_id: LayeredDecision(tuple(layers[agent_id])) for agent_id in live}}
