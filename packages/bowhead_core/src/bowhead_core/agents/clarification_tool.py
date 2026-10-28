"""Sprint θ Phase 8b-3: clarification_ask tool (sentinel-based).

Per tech-design §0.6 F3 fix (NOT exception-based — `tool_registry.execute`
swallows all exceptions per tool_registry.py:915, so raising would silently
fail). Instead returns a sentinel dict that lead_loop checks after each
tool call.

Per L3 fix: signature uses `clarification_id` (uuid) + `aborted_turn_id`
(current turn that's terminating).

Per U1 fix: lead_loop emits BOTH `clarification_required` AND `done` SSE so
frontend SSE state machine transitions out of [Streaming] state.

Per PRD §3.7 abort cleanup spec:
- Lead loop catches sentinel → emits SSE → cancels in-flight peers via
  asyncio.CancelledError → flushes partial cost as `outcome=clarification_abort`
- Partial canvas writes from earlier hops are NOT rolled back (clarification
  doesn't unwind history)
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Literal

from bowhead_router.types import ToolSchema

from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

logger = logging.getLogger(__name__)


# Sentinel key — lead_loop checks this on every tool result
SENTINEL_CLARIFICATION_PENDING = "_clarification_pending"


def is_clarification_sentinel(tool_result: Any) -> bool:
    """Lead loop checks every tool result with this; True → abort + emit SSE.

    Matches two shapes:
    1. `clarification_ask` tool output (Lead explicitly asks): carries
       `_clarification_pending: True`.
    2. ar2-v2 `bi_query` needs_clarification (slot resolver hit ambiguity):
       carries `{ok: False, error_kind: "needs_clarification",
       clarification: {options: [...], prompt: "..."}, rows: []}`.
       Per 2026-05-18 dogfood t_746c80ecc7ed: this shape was reaching Lead
       and the LLM was writing prose-with-options instead of emitting the
       structured `clarification_required` event — FE rendered text but no
       clickable buttons.
    """
    if not isinstance(tool_result, dict):
        return False
    if tool_result.get(SENTINEL_CLARIFICATION_PENDING) is True:
        return True
    if tool_result.get("error_kind") == "needs_clarification":
        clar = tool_result.get("clarification")
        if isinstance(clar, dict) and clar.get("options"):
            return True
    return False


# Frontend renders choices and free text. `rank_priorities` was removed from tool schema on 2026-09-07:
# it never had ranking UI; cards rendered it as checkboxes, **discarding the user's order**,
# a claimed capability silently downgraded with information loss. Removal hides this value from the model.
SUPPORTED_QUESTION_TYPES = frozenset({"single_select", "multi_select", "free_text"})


def _normalize_question_type(raw_type: Any) -> str:
    """把 `type` 归一到前端能渲染的选项或自由输入形态。

    ⚠ `rank_priorities` 必须显式映射到 `multi_select`，**不能**让它掉进默认的
    `single_select`：前端今天就是按「非 single_select ⇒ 复选组」渲的
    （`TurnContainer` 的 `single` 判据 + 卡片的 `isQuestionSingle` 同源），
    映射成 multi 才是渲染逐字节不变；掉进 single 会把「勾几个再确认」变成
    「点一下就提交」——那是行为回归。存量事件/库行里的旧取值同样走这里。
    （`test_b5_clarification_schema_upgrade.py` 曾有一句注释说它「downgraded to
    single_select at FE」，那句与代码不符，本次一并订正。）
    """
    value = str(raw_type or "single_select")
    if value == "rank_priorities":
        return "multi_select"
    return value if value in SUPPORTED_QUESTION_TYPES else "single_select"


MAX_OPTIONS_PER_QUESTION = 4


def _questions_over_option_limit(raw_questions: Any) -> list[dict[str, Any]]:
    """Questions whose valid options exceed the per-question limit, with the overflow."""
    if not isinstance(raw_questions, list):
        return []
    over: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict) or not isinstance(raw.get("options"), list):
            continue
        if _normalize_question_type(raw.get("type")) == "free_text":
            # free_text takes no options; normalization already drops a free_text question that carries any.
            continue
        options = [str(opt).strip()[:80] for opt in raw["options"] if isinstance(opt, str) and opt.strip()]
        if len(options) > MAX_OPTIONS_PER_QUESTION:
            over.append(
                {
                    "question_index": index,
                    "question": str(raw.get("question") or "")[:200],
                    "option_count": len(options),
                    "rejected_options": options[MAX_OPTIONS_PER_QUESTION:],
                }
            )
    return over


def normalize_clarification_questions(raw_questions: Any) -> list[dict[str, Any]]:
    """Normalize B5 `questions` while preserving every valid question.

    The B5 schema allows 1-3 questions. Older code collapsed this to
    `questions[0]`, which made multi-facet cards lose everything except the
    first question on the SSE payload.
    """
    if not isinstance(raw_questions, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        q_text_raw = raw.get("question")
        if not isinstance(q_text_raw, str) or not q_text_raw.strip():
            continue
        raw_options = raw.get("options") or []
        if not isinstance(raw_options, list):
            raw_options = []
        # Match schema maxItems (ask_user_input_v0: 2-4 options/question). The tool
        # handler rejects longer lists before this point; the cut only guards old
        # persisted rows and non-tool producers.
        options = [str(opt).strip()[:80] for opt in raw_options if isinstance(opt, str) and opt.strip()][:MAX_OPTIONS_PER_QUESTION]
        q_type = _normalize_question_type(raw.get("type"))
        if (q_type == "free_text" and raw.get("options") != []) or (q_type != "free_text" and not options):
            continue
        out.append(
            {
                "question": q_text_raw.strip()[:200],
                "options": options,
                "type": q_type,
            }
        )
        if len(out) >= 3:
            break
    return out


def _flatten_question_candidates(questions: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for q in questions:
        for opt in q.get("options") or []:
            if isinstance(opt, str):
                candidates.append(opt)
    return candidates[:12]  # ≤3 questions × ≤4 options


def _inc_clarification_metric(
    *,
    type: str,  # noqa: A002
    caller: str,
    source_kind: str,
    outcome: str,
    help_text: str,
) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_clarification_ask_call_total",
            labels={
                "type": type,
                "caller": caller,
                "source_kind": source_kind,
                "outcome": outcome,
            },
            help=help_text,
        )
    except Exception:
        logger.debug("clarification_ask metric emit failed", exc_info=True)


def extract_clarification_event_payload(tool_result: Any) -> dict[str, Any]:
    """Normalize either sentinel shape to the `clarification_required` event
    payload (candidates / fallback_text / clarification_id). Called from
    lead_orchestrator after `is_clarification_sentinel` matches."""
    if not isinstance(tool_result, dict):
        return {
            "candidates": [],
            "fallback_text": None,
            "clarification_id": None,
            "questions": [],
            # connector-context T1#6: all three returns carry the tool key (prevent consumer-side KeyError)
            "tool": None,
        }
    # Shape 1: clarification_ask sentinel — already flat.
    if tool_result.get(SENTINEL_CLARIFICATION_PENDING) is True:
        return {
            "candidates": tool_result.get("candidates", []),
            "fallback_text": tool_result.get("fallback_text"),
            "clarification_id": tool_result.get("clarification_id"),
            "questions": normalize_clarification_questions(
                tool_result.get("questions"),
            ),
            "tool": None,
        }
    # Shape 2: bi_query needs_clarification — pull from `clarification.options`.
    clar = tool_result.get("clarification") or {}
    raw_options = clar.get("options") or []
    candidates: list[str] = []
    option_pairs: list[dict[str, str]] = []
    for opt in raw_options:
        if isinstance(opt, dict):
            label = opt.get("label") or opt.get("value")
            if label:
                candidates.append(str(label))
                # Batch 2 (PRD §3.3b): forward (value,label) pairs faithfully; tool_context_select's
                # value is the datarc workspace ID and label the display name; resolution writes user preferences by value.
                # For legacy shape-2 producers carrying only label, value falls back to label.
                option_pairs.append(
                    {
                        "value": str(opt.get("value") or label),
                        "label": str(label),
                    }
                )
        elif isinstance(opt, str):
            candidates.append(opt)
            option_pairs.append({"value": opt, "label": opt})
    return {
        "candidates": candidates,
        "fallback_text": clar.get("prompt"),
        "clarification_id": clar.get("clarification_id"),
        "questions": normalize_clarification_questions(clar.get("questions")),
        # Batch 2: dedicated kind + structured option pairs + remember toggle (generic shape-1 / legacy shape-2
        # have kind=None/degraded pairs; CH2 merge branches by presence without breaking compatibility).
        "clar_kind": clar.get("kind"),
        "option_pairs": option_pairs,
        "remember_option": bool(clar.get("remember_option", False)),
        # connector-context T1#6: forward connector discriminator faithfully (previously lost on the first hop,
        # forcing resolution to hardcode datarc: B1 broken chain). Nonconnector card = None.
        "tool": clar.get("tool"),
    }


def make_clarification_sentinel(
    *,
    candidates: list[str],
    fallback_text: str | None,
    aborted_turn_id: str,
    clarification_id: str | None = None,
    type: str = "single_select",  # noqa: A002
    questions: list[dict[str, Any]] | None = None,
    store_persisted: bool | Literal["deferred"] = False,
) -> dict[str, Any]:
    """Construct sentinel dict per tech-design §0.6 L3 schema.

    bi-hitl-cleanup B5 (2026-05-21): added `type` param (default 'single_select'
    for backward compat with all pre-B5 callers). When B5-shape `questions` is
    used, the type derives from `questions[0].type`; FE V4ClarificationCard
    currently ignores `type` (renders single-select chips for all), but the
    field is carried through so future FE rendering (multi_select / rank UI)
    can read it without backend changes.

    `store_persisted`（2026-09-04 HITL 审计 §5）：`clarification_id` 背后有没有
    真的 ClarificationStore 行。**缺省 False = 证明不了**——编排层据此补写一次，
    补不上就不发这张点了必 404 的卡。哨兵自带的 uuid 本身不是行存在的证据。

    `store_persisted="deferred"`: the orchestrator owns persistence (bounded and
    reference Lead cards) and assigns the real id when it writes the row, so the
    sentinel carries `clarification_id=None` instead of a throwaway uuid.
    """
    deferred = store_persisted == "deferred"
    return {
        SENTINEL_CLARIFICATION_PENDING: True,
        "candidates": candidates,
        "fallback_text": fallback_text,
        "aborted_turn_id": aborted_turn_id,
        "clarification_id": None if deferred else (clarification_id or str(uuid.uuid4())),
        "type": type,
        "questions": normalize_clarification_questions(questions),
        "store_persisted": "deferred" if deferred else store_persisted is True,
    }


def register_clarification_tool(registry: ToolRegistry) -> None:
    """Register clarification_ask tool. Lead manifest tool whitelist includes it."""
    registry.register(ToolSpec(schema=_clarification_ask_schema(), handler=_clarification_ask))


def _clarification_ask_schema() -> ToolSchema:
    return ToolSchema(
        name="clarification_ask",
        description=(
            # bi-hitl-cleanup B4 (2026-05-21): self-contained description per
            # Anthropic ask_user_input_v0 style. Source-agnostic — this is a
            # cross-source elicitation primitive (BI / KB / web / memory all
            # use it the same way). Per discovery §4.4 L0 generic layer.
            "Ask the user when a required value is missing or the request has "
            "a consequential ambiguity that conversation context cannot resolve. "
            "This creates a clarification card and pauses the task; the answer "
            "continues by the runtime's supported resume path.\n"
            "\n"
            "WHEN TO USE\n"
            "- A value required to complete the task is absent, cannot be "
            "  derived from supplied data, and only the user can provide it. "
            "  If the user asks you to obtain it before acting, call this tool "
            "  alone before delegating or using other tools.\n"
            "- Or the query has multiple defensible interpretations, no "
            "  available tool or context can disambiguate them, and choosing "
            "  incorrectly would meaningfully change the answer.\n"
            "\n"
            "Anchor examples (BI workspace):\n"
            "- User says '客单价同比' — formula is ambiguous (GMV/订单 vs 净销售/订单 "
            "  vs 净销售/活跃用户). Ask: which formula.\n"
            "- User says '我们重点几个店的本月销售' — '重点' is the user's term, "
            "  workspace has no '重点店' dim. Ask: which stores count as 重点.\n"
            "\n"
            "CRITICAL — WHEN NOT TO USE\n"
            "Skip the tool and answer/proceed in these cases:\n"
            "- The user already gave the constraint in this conversation. "
            "  CHECK CONVERSATION FIRST. Don't re-ask what they said.\n"
            "- A non-critical detail permits a reasonable stated assumption. "
            "  Never invent a required numeric or business value.\n"
            "- The user is venting / sharing opinion / stating a fact. Not a "
            "  query — don't 'clarify' a feeling.\n"
            "- The query is fully clear, just hard. Hard ≠ ambiguous. Run the "
            "  analysis; report findings + caveats.\n"
            "- 'Should I do A or B?' style — user is asking for your recommendation, "
            "  not stating ambiguity. Recommend with reasoning, don't bounce back.\n"
            "\n"
            "OUTPUT FORMAT\n"
            "- 1-3 clarifying questions per call (one per ambiguous facet; "
            "  bundle related ones, don't fragment).\n"
            "- For choice questions, provide 2-4 short options. Cover the realistic space; "
            "  user can enter another answer if you missed it.\n"
            "- When the user must supply a missing value (for example an equity amount), "
            "  use type='free_text' and options=[]; never invent value choices.\n"
            "- Options must be **answerable choices**, NOT executable queries: "
            "  ✅ '净销售额 / 订单数' ❌ '查2026年4月Top10店销售'.\n"
            "- Before calling, write ONE prose sentence in your turn output "
            "  framing why you're asking (e.g. '客单价有几种常见算法, 您指哪一个?'). "
            "  Then call the tool. After the call, the turn ends.\n"
            "\n"
            "DO NOT fabricate specific values in options — no specific dates "
            "(2026年4月), Top-N (Top 10), business entities (brand names), or "
            "percentages (同比 5%). These imply data exists when it may not. "
            "Options should be dimension/category choices the user can answer "
            "from their own intent, not values that pretend to come from data.\n"
            "\n"
            "SCHEMA (preferred, B5 — Anthropic ask_user_input_v0 aligned)\n"
            "  questions: list[Question]  # 1-3 questions, each with options/type\n"
            "    Question = {\n"
            "      question: str           # the framed question (1-2 sentences)\n"
            "      options: list[str]      # 2-4 choice labels; [] for free_text\n"
            "      type: 'single_select' | 'multi_select' | 'free_text'\n"
            "    }\n"
            "  fallback_text: str?         # prose shown when questions empty\n"
            "\n"
            "LEGACY SCHEMA (backward-compat, still accepted)\n"
            "  candidates: list[str]       # 1-5 option labels (treated as single_select)\n"
            "  fallback_text: str?\n"
            "\n"
            "TYPE — single_select when options are mutually exclusive (pick "
            "ONE: which formula / period / definition). multi_select when the "
            "clarification is ELICITING the scope of a deliverable the user "
            "wants — options are NOT exclusive and you will honor ALL the user "
            "picks (which report sections / which dimensions to break down by / "
            "which channels to include). It renders as a real multi-select card.\n"
            "\n"
            "EXAMPLE single_select (disambiguation — one formula)\n"
            "  clarification_ask(questions=[{\n"
            "    'question': '客单价用哪个公式?',\n"
            "    'options': ['GMV / 订单数', '净销售额 / 订单数', '净销售额 / 活跃用户数'],\n"
            "    'type': 'single_select',\n"
            "  }])\n"
            "EXAMPLE multi_select (elicitation — scope of a deliverable)\n"
            "  clarification_ask(questions=[{\n"
            "    'question': '这份经营报告要覆盖哪些板块?',\n"
            "    'options': ['营业额趋势', '利润率', '客流量', '会员复购'],\n"
            "    'type': 'multi_select',\n"
            "  }])\n"
            "EXAMPLE free_text (a required missing value)\n"
            "  clarification_ask(questions=[{\n"
            "    'question': '请补充平均所有者权益是多少？',\n"
            "    'options': [],\n"
            "    'type': 'free_text',\n"
            "  }])\n"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "questions": {
                    "type": "array",
                    "description": (
                        "B5 preferred shape: 1-3 questions, each with options + "
                        "type. Use free_text with options=[] for a missing value "
                        "the user must provide. Use multi_select when eliciting the scope of a "
                        "deliverable (non-exclusive options you'll all honor — "
                        "e.g. which report sections / dimensions / channels); the "
                        "FE renders a real multi-select card with a submit button. "
                        "single_select for mutually-exclusive disambiguation."
                    ),
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "question": {
                                "type": "string",
                                "maxLength": 200,
                                "description": "Framed question text shown to user.",
                            },
                            "options": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 80},
                                "minItems": 0,
                                "maxItems": 4,
                            },
                            "type": {
                                "type": "string",
                                "enum": ["single_select", "multi_select", "free_text"],
                                "default": "single_select",
                                "description": (
                                    "single_select = pick ONE (mutually-exclusive "
                                    "choices: which formula / period / definition). "
                                    "multi_select = pick ONE OR MORE (non-exclusive: "
                                    "which sections / dimensions / channels to include "
                                    "in a deliverable). free_text = enter a missing "
                                    "value; options must be empty."
                                ),
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 80},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": (
                        "Legacy single-question shape (backward-compat). If `questions` is provided, this is ignored. Treated as single_select."
                    ),
                },
                "fallback_text": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "无好候选时的降级提示文本 (可选; 候选/问题为空时必传)",
                },
            },
            # P9 Eng fix (2026-05-21): require at least one of {questions,
            # candidates, fallback_text}. Without this gate, LLM emitting
            # clarification_ask({}) produces an empty card with 0 options + no
            # text — useless UX. Gate forces LLM to provide some content; the
            # handler's S8.b default-message guard is now a true belt-and-
            # suspenders (was the only line of defense pre-fix).
            "anyOf": [
                {"required": ["questions"]},
                {"required": ["candidates"]},
                {"required": ["fallback_text"]},
            ],
        },
    )


def clarification_refusal(note: str) -> dict[str, Any]:
    """Return model feedback without a pending card or a root failure."""
    return {
        "ok": False,
        "clarification_refused_on_resume": True,
        "error_kind": "clarification_refused_on_resume",
        "note": note,
    }


async def _clarification_ask(
    args: dict[str, Any],
    ctx: ToolExecutionContext,
) -> dict[str, Any]:
    """Sentinel-return tool handler. Returns dict with sentinel key.

    lead_loop checks for sentinel after EACH tool call; if found → emits SSE +
    aborts loop. This avoids raising exception (which tool_registry.execute would
    swallow per F3 finding).
    """
    # The reference Lead must wait for the orchestrator to prove this was its
    # only tool action, then insert card and checkpoint in one transaction.
    reference_lead = getattr(ctx, "_reference_lead_runtime", None) is not None
    from bowhead_core.clarification_rounds import available as bounded_questions_available
    from bowhead_core.clarification_rounds import worker_identity

    bounded_questions = await bounded_questions_available(ctx)
    # Existing cards retain their one-round policy. New bounded cards must pass
    # durable scope, worker, answer and budget checks before emitting a sentinel.
    # Only the tracked legacy writer supports bounded checkpoints. Other
    # channels and Terminal retain their existing first-card contract.
    bounded_admission = bool(worker_identity(ctx)["owner_id"])
    if not bounded_questions and (getattr(ctx, "_resume_turn_id", None) or bounded_admission):
        _inc_clarification_metric(
            type="single_select",
            caller="resume_guard",
            source_kind="generic",
            outcome="suppressed_on_resume",
            help_text="clarification_ask refused on a resumed turn (one clarification round per turn).",
        )
        return clarification_refusal(
            "The current clarification policy or worker does not admit another question. Continue with existing answers and state any remaining limitation."
        )
    # bi-hitl-cleanup B5 (2026-05-21): accept either new `questions` shape
    # (preferred, B5 — Anthropic ask_user_input_v0 aligned) OR legacy
    # `candidates` shape (backward-compat). Preserve all valid B5 questions on
    # the sentinel; keep a flat candidates list for legacy FE / store rows.
    #
    # P11 final review fix (2026-05-21): the schema's `anyOf [questions |
    # candidates | fallback_text]` gate is decorative (registry._validate_jsonschema
    # at agents/registry.py:80 doesn't run anyOf — LLM-prompt hint only).
    # Add a handler-level reject mirroring the schema gate so empty calls
    # don't silently produce default-text cards.
    questions = args.get("questions")
    fallback_text = args.get("fallback_text")
    sentinel_type = "single_select"
    normalized_questions: list[dict[str, Any]] = []
    # P11 fix: handler-level reject mirroring schema anyOf gate. Only triggers
    # when ALL three keys are absent (true empty call). Explicit
    # `candidates: []` is treated as the legacy S8.b path — LLM signaled
    # "no good options" and we route to default-text. This preserves the
    # pre-existing contract while still rejecting `clarification_ask({})`.
    if "questions" not in args and "candidates" not in args and not fallback_text:
        _inc_clarification_metric(
            type="single_select",
            caller="tool_handler_reject_empty",
            source_kind="generic",
            outcome="rejected_empty_args",
            help_text="clarification_ask called with empty args — rejected (P11 fix).",
        )
        return {
            SENTINEL_CLARIFICATION_PENDING: False,
            "error_kind": "schema_invalid",
            "error_message": ("clarification_ask requires at least one of {questions, candidates, fallback_text}"),
        }

    over_limit = _questions_over_option_limit(questions)
    if over_limit:
        _inc_clarification_metric(
            type="single_select",
            caller="tool_handler_reject_options",
            source_kind="generic",
            outcome="rejected_too_many_options",
            help_text="clarification_ask rejected: a question had more than the maximum options.",
        )
        # A rejection must carry the right answer: name the exact limit and the
        # options that did not fit so the model can merge or drop them.
        details = " ".join(
            f"Question {item['question_index'] + 1} ({item['question']!r}) has {item['option_count']} options; "
            f"these would not fit: {item['rejected_options']}."
            for item in over_limit
        )
        return {
            SENTINEL_CLARIFICATION_PENDING: False,
            "ok": False,
            "error_kind": "schema_invalid",
            "max_options_per_question": MAX_OPTIONS_PER_QUESTION,
            "questions_over_limit": over_limit,
            "error_message": (
                f"clarification_ask accepts at most {MAX_OPTIONS_PER_QUESTION} options per question; nothing was shown to the user. "
                f"{details} Merge or drop options so each question has at most {MAX_OPTIONS_PER_QUESTION}, "
                "then call clarification_ask again."
            ),
        }

    normalized_questions = normalize_clarification_questions(questions)
    if normalized_questions:
        candidates = _flatten_question_candidates(normalized_questions)
        if any(q.get("type") == "multi_select" for q in normalized_questions):
            sentinel_type = "multi_select"
        else:
            sentinel_type = str(
                normalized_questions[0].get("type") or "single_select",
            )
        sentinel_type = _normalize_question_type(sentinel_type)
        # If LLM gave question text but no fallback, use the lone question as
        # intro; multi-question cards render each question as per-option subtext.
        if not fallback_text:
            fallback_text = normalized_questions[0]["question"] if len(normalized_questions) == 1 else "请选择一个最关键的方向继续"
        # B5 telemetry: track emit distribution by type (informs future FE work)
        _inc_clarification_metric(
            type=sentinel_type,
            caller="tool_handler",
            source_kind="generic",
            outcome="emitted",
            help_text="clarification_ask tool handler emit by type (B5 telemetry).",
        )
    else:
        # Legacy `candidates` shape (backward-compat)
        candidates = args.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []
        candidates = [str(c)[:80] for c in candidates if isinstance(c, str)][:5]
        # bi-hitl-cleanup P8 (2026-05-21): close legacy-path telemetry gap —
        # without this, only B5 questions-shape callers were counted, masking
        # legacy traffic during the migration window.
        _inc_clarification_metric(
            type="single_select",
            caller="tool_handler_legacy",
            source_kind="generic",
            outcome="emitted",
            help_text="clarification_ask tool handler emit (legacy candidates shape).",
        )

    # If candidates empty, fallback_text becomes mandatory (S8.b case)
    if not candidates and not fallback_text:
        fallback_text = "您的问题我没听明白, 能换个问法吗?"

    if bounded_questions and not await bounded_questions_available(ctx, questions=normalized_questions, fallback=fallback_text):
        return clarification_refusal(
            "The question was already asked or the shared pause budget is unavailable. Use the existing answers; do not ask it again."
        )

    aborted_turn_id = getattr(ctx, "turn_id", None) or "unknown"

    # B1 fix (hitl-fix sprint): persist clarification request to ClarificationStore
    # so FE `POST /api/v1/clarifications/{id}/resolve` can find the row. Without
    # this, the sentinel uuid was emitted via SSE but never persisted, so /resolve
    # returned 404 (prod evidence: turn t_441f3758 on 0.24.8).
    #
    # 0.26.2 fix — build minimum resume_context and pass to put() atomically.
    # Prior bug: orchestrator did set_resume_context() in a SECOND write that
    # could silent-fail, leaving rows with resume_context_json=NULL → /resolve
    # returned action="noop" → FE chat-send → duplicate-content turns (prod
    # s_e5f180688e54, 7/7 ambiguous_turn rows empty). Tool handler doesn't
    # know phase_plan/dispatch_window yet — orchestrator may UPDATE later
    # with richer fields. But the bare minimum (user_text + identity) is
    # written here so /resolve always returns turn_resume.
    persisted_request_id: str | None = None
    store = getattr(ctx, "clarification_store", None)
    if store is not None and not reference_lead and not bounded_questions:
        from bowhead_core.clarification_store import (
            ClarificationCandidate,
        )
        from bowhead_core.language_context import RESUME_LANGUAGE_KEY, add_resume_time_zone, language_context_snapshot

        _user_text = getattr(ctx, "_psi_turn_user_text", "") or ""
        _resume_ctx: dict[str, Any] = {
            "user_text": _user_text,
            "parent_turn_id": aborted_turn_id,
            "session_id": getattr(ctx, "session_id", None),
            "member_uid": getattr(ctx, "member_uid", "") or "",
            "workspace_id": getattr(ctx, "workspace_id", "") or "",
            "active_datarc_project": getattr(ctx, "active_datarc_project", None),
            "user_id": getattr(ctx, "user_id", "") or "",
            "team_id": getattr(ctx, "team_id", None),
            "access_context": getattr(ctx, "access_context", None),
            "kb_role": getattr(ctx, "kb_role", "user"),
            "paradigm_v2": bool(getattr(ctx, "paradigm_v2", False)),
            "preferred_provider": getattr(ctx, "preferred_provider", None),
            "preferred_model": getattr(ctx, "preferred_model", None),
        }
        # Batch 7-F: snapshot this turn's language too. Resume previously hardcoded `LanguageContext.default()`,
        # making English users revert to Chinese after one clarification (README §6 gap 1, still unfixed since registration).
        _lang_snapshot = language_context_snapshot(ctx)
        if _lang_snapshot is not None:
            _resume_ctx[RESUME_LANGUAGE_KEY] = _lang_snapshot
        add_resume_time_zone(_resume_ctx)
        try:
            req = await store.put(
                turn_id=aborted_turn_id,
                user_id=getattr(ctx, "user_id", "") or "",
                workspace_id=getattr(ctx, "workspace_id", "") or "",
                token="",  # clarification_ask is for vague queries, not single token
                rationale="Lead clarification_ask: query 太模糊, 候选挑选或自定义",
                candidates=[
                    ClarificationCandidate(
                        value=c,
                        label=c,
                        hint="",
                        confidence="",
                        kind="dim_value",
                    )
                    for c in candidates
                ],
                kind="ambiguous_turn",
                multi_select=(sentinel_type == "multi_select"),
                session_id=getattr(ctx, "session_id", None) or None,
                resume_context=_resume_ctx,
            )
            persisted_request_id = req.request_id
        except Exception:  # Persistence failure → mark unpersisted; orchestration repairs it / withholds the card (audit §5)
            logger.exception(
                "clarification_ask store.put failed turn=%s — 标记 store_persisted=False, 交编排层补写",
                aborted_turn_id,
            )

    sentinel = make_clarification_sentinel(
        candidates=candidates,
        fallback_text=fallback_text,
        aborted_turn_id=aborted_turn_id,
        clarification_id=persisted_request_id,  # B1 fix: prefer store uuid
        type=sentinel_type,
        questions=normalized_questions,
        # An unwired store (test/misconfiguration) also counts as unpersisted: orchestration retries once, then withholds the card if it still fails.
        # Bounded and reference Lead cards are written by the orchestrator, which assigns the id.
        store_persisted="deferred" if (reference_lead or bounded_questions) else persisted_request_id is not None,
    )

    logger.info(
        "clarification_ask_invoked turn_id=%s candidates_count=%d clarification_id=%s store_persisted=%s",
        aborted_turn_id,
        len(candidates),
        sentinel["clarification_id"],
        persisted_request_id is not None,
    )

    return sentinel
