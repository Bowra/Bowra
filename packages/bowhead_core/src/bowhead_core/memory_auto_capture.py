"""高置信个人域**零点击直写**路（B 线批 1a ＝ 记忆二期 FR5 的落地形态）。

四格收货规则的强信号 × 个人域那一格：用户明说「以后都…」时**不再弹卡**，
收窄改写后直接写进个人记忆，下轮使用时明示「已按你的偏好…（可撤销）」。

授权依据（`memory-agent-prompt.md` §一 裁决 1）：用户说话本身就是授权。
行业同款：Windsurf / Claude Code / ChatGPT 个人域全自动 + 可撤销；
仓内活先例：`auto_learner` 的 `user directive → facts` 今天就是零确认跑在生产。

---

**这条路上有五道闸，每一道都是「不许省」的**，理由各不相同：

① **第一层关键词粗筛必须命中**（`memory_capture_trigger`）。
   判型是 LLM 出的，而 **LLM 的产出不能自己给自己发通行证**。没有这条确定性命中，
   哪怕分类器喊 `modification_request` 也不许免确认写入。
   同一条纪律见 `commit_memory_write`「置信度由写入身份决定，调用方无法传入」。

② **taint（输入含工具轨迹/第三方文本）一律降级为确认卡**。
   taint 由**服务端代码确定性计算**并作为必填参数传入 —— 不取 LLM 产出、不信前端回传。
   本模块**不猜**：调用方必须显式给值，给不出就别调这条路。

③ **起草产物过入站闸**（`echoes_escape_marker`）。起草是一次 LLM 调用，它的输入里
   有消毒过的用户原文；模型把 `[[escaped:...]]` 照抄回来，就会把安全记号写进用户的记忆。
   转义策略敢用的前提就是有这道回收闸。

④ **照过写入闸**（`memory_write_gate.check`）。自动写入不是「绕开闸」的许可证 ——
   语言族/一次性/无来源/含密钥四条判据一条不减。

⑤ **`commit_memory_write` 的判重与裁决表照走**。同一条偏好换个措辞再说一遍，
   只 bump `last_confirmed_at`，不新增行（2026-09-04 在 116 生产验过这条形状）。

---

⚠ **写入身份是 `feedback_confirmed`**，它在 `_HIGH_CONFIDENCE_SOURCES` 里 ⇒ `confidence=high`。
不是为了「让它更权威」，是为了**避免撞上用户手写的 high 条目时抛
`MemoryConfidenceConflictError` 整事务回滚**（裁决表唯一禁止的一格是 low 盖 high）。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bowhead_core.memory_capture_trigger import (
    DECISION_LANGUAGE_SETTING,
    DECISION_PERSISTENT,
    classify_user_message,
)
from bowhead_core.prompt_injection_defense import echoes_escape_marker, sanitize_for_injection

#: Draft-tier candidate origin (same value as bowhead_memory.candidates.ORIGIN_FEEDBACK_DRAFT).
#: ⚠ Do not import: bowhead_core does not depend on bowhead_memory (dependency runs the other way).
#: A value change would break parity here; test_memory_auto_capture.py guards equality on both sides.
ORIGIN_FEEDBACK_DRAFT = "feedback_draft"
#: Same values as bowhead_memory.candidates.ENQUEUE_* (no import, for the same reason above); guard tests ensure parity.
ENQUEUE_CREATED = "created"
ENQUEUE_DUPLICATE_PENDING = "duplicate_pending"

logger = logging.getLogger(__name__)

#: Write identity. Add to memory_write_policy._HIGH_CONFIDENCE_SOURCES to get high confidence.
CAPTURE_SOURCE = "feedback_confirmed"

#: Same limits as the manual-create endpoint routes/memory.py (key 128 / value 16KB).
#: Numbers exist in two places: separate constants because that side uses Pydantic field constraints, this side runtime checks.
#: Change both together; this comment is the reminder.
_MAX_KEY_CHARS = 128
_MAX_VALUE_CHARS = 16 * 1024
#: Same source as that endpoint's kind allowlist. Reject datarc_binding: operational rows,
#: which batch 1 established that gates/supersession must not touch.
_ALLOWED_KINDS = frozenset({"preference", "constraint"})

#: ★★ All rejection reasons → whether to show the user. The dividing line is substantive:
#: - False = nothing happened (gate off, no match, drafter failure). A message adds noise,
#:   and internal states such as flag_off should not appear to users.
#: - True = the user's request was not followed. Without explanation, the feature seems broken.
#:
#: ⚠ This table exists because rejection reasons were bare strings scattered through functions; anyone could add another,
#: while displaying their codes directly repeats the error-code-in-toast bug seen twice this sprint,
#: including on 116. A guard test (test_memory_auto_capture.py) AST-scans every
#: CaptureOutcome(reason=...) in this module and fails on unregistered reasons.
#:
#: ⚠ Frontend copy for user-visible reasons arrives with 1c (the inline card is its sole consumer,
#: currently absent). Guard tests prevent omissions then: every True entry must have Chinese and English copy.
REASON_USER_FACING: dict[str, bool] = {
    # —— Nothing happened; do not disturb the user ——
    "flag_off": False,
    # Users disabled it themselves and already know; no need to tell them again.
    "learning_paused": False,
    "trigger_none": False,
    # ★ Language requests are not cases where nothing happened: the user explicitly asked to change response language,
    # but the destination is a setting, not memory. Silently skipping means nobody responds to the request;
    # this was the dead-end language path recorded in the 2026-09-08 no-op report.
    "trigger_language_setting": True,
    "draft_failed": False,
    "draft_empty": False,
    # ★ The model judges this a requirement for this turn, not a lasting preference; tell the user.
    # First-layer keyword filtering recognizes 「记住…」 but not one-off phrasing such as 「…且不得改动其他章节内容」;
    # if the second layer identifies this and silently discards it, the user explicitly said 「记住」
    # yet nothing happens, without explanation (such cases were piling up in the observed 116 queue).
    "draft_says_not_lasting": True,
    "write_failed": False,
    "no_key": False,
    "confirmed": False,  # Duplicate: the same item repeated only bumps its timestamp; silence is appropriate.
    # —— The user's request was not followed; explain it ——
    "rate_limited": True,
    "tainted_input": True,
    # Taint downgrade attempted but failed to enqueue a candidate (write gate rejected / suppressed); no card exists to confirm. Do not tell the user:
    # the rejected draft was narrowed by the model and never seen by the user; explaining rejection of words they never said only confuses them.
    "tainted_not_enqueued": False,
    "draft_too_long": True,
    "kind_not_allowed": True,
    "draft_echoed_sanitizer_marker": True,
    "key_too_long": True,
    "blocked_by_stronger_entry": True,
}

#: Chinese and English copy for user-visible rejection reasons. Missing copy deliberately raises KeyError (same rule as memory_write_gate._MESSAGES):
#: fail immediately for a new reason without copy rather than falling back to a generic message.
REASON_MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        # ⚠ Same message as memory_write_gate.REASON_LANGUAGE_IS_A_SETTING:
        # describing the same issue differently on two paths makes users think they are different issues.
        # Do not import it: that side says it was not stored in long-term memory (a rejection);
        # this side intercepts before writing and redirects (a rerouting). Different tone, same conclusion.
        # ⚠ Describe only the memory-side facts; neither of these claims is allowed:
        #   ① It will not take effect next session — false. Saying 「以后都用英文回答」 makes Lead call config_propose(form_id=user_language), risk_tier=1, persisting directly without confirmation (observed on 116, 2026-09-14: zh-CN→en-US);
        #     Lead often already changed the setting in the same turn, so saying it will not take effect contradicts the answer.
        #   ② Change it in Settings — that page has no such control (sidebar LocaleSwitcher writes only UI language under policy B).
        # The endpoint calculating this message on reads cannot know whether Lead changed the setting that turn; describe only what holds in both cases.
        "trigger_language_setting": "回答语言由设置决定，不作为记忆保存，所以这条没有记进记忆。",
        # Same issue and tone as the write gate's REASON_ONE_OFF: this sounds like a requirement for this turn.
        "draft_says_not_lasting": "这听起来是这一轮的要求，没有记成长期偏好。如果想让我一直这样，直接说「以后都…」。",
        "rate_limited": "刚记过一条，这次先没记。稍后再说一次就会记下。",
        "tainted_input": "这轮用到了外部内容，为稳妥没有自动记住——你可以手动确认一下。",
        "draft_too_long": "这条太长了，没有记住。说得短一点更容易记准。",
        "kind_not_allowed": "这类内容不进个人记忆。",
        "draft_echoed_sanitizer_marker": "整理这条时出了点问题，没有记住。",
        "key_too_long": "这条的主题名太长，没有记住。",
        "blocked_by_stronger_entry": "你手写过一条同主题的记忆，这次没有覆盖它。要改请直接编辑那一条。",
    },
    "en-US": {
        "trigger_language_setting": "Answer language is a setting, not a memory, so this was not saved to memory.",
        "draft_says_not_lasting": (
            "This sounds like a request for this turn rather than a lasting preference, so it was not saved. "
            "If you want it every time, say “from now on…”."
        ),
        "rate_limited": "Just saved one a moment ago, so this one was skipped. Say it again later and it will be saved.",
        "tainted_input": "This turn involved external content, so nothing was saved automatically — you can confirm it by hand.",
        "draft_too_long": "This one is too long and was not saved. A shorter phrasing is easier to get right.",
        "kind_not_allowed": "This kind of content does not go into personal memory.",
        "draft_echoed_sanitizer_marker": "Something went wrong while tidying this up, so it was not saved.",
        "key_too_long": "The topic name for this one is too long, so it was not saved.",
        "blocked_by_stronger_entry": "You wrote a memory on this topic yourself; it was left untouched. Edit that one to change it.",
    },
    "de-DE": {
        "trigger_language_setting": "Die Antwortsprache ist eine Einstellung und keine Erinnerung. Deshalb wurde dies nicht als Erinnerung gespeichert.",
        "draft_says_not_lasting": "Das klingt nach einer Anforderung für diesen Gesprächsschritt und nicht nach einer dauerhaften Präferenz. Deshalb wurde sie nicht gespeichert. Wenn dies immer gelten soll, sagen Sie „ab jetzt …“.",
        "rate_limited": "Gerade wurde eine Erinnerung gespeichert. Diese wurde daher ausgelassen. Wiederholen Sie sie später, damit sie gespeichert wird.",
        "tainted_input": "In diesem Gesprächsschritt wurden externe Inhalte verwendet. Deshalb wurde nichts automatisch gespeichert. Sie können dies manuell bestätigen.",
        "draft_too_long": "Diese Angabe ist zu lang und wurde nicht gespeichert. Eine kürzere Formulierung lässt sich genauer merken.",
        "kind_not_allowed": "Diese Art von Inhalt wird nicht im persönlichen Gedächtnis gespeichert.",
        "draft_echoed_sanitizer_marker": "Beim Aufbereiten dieser Angabe ist ein Problem aufgetreten. Sie wurde nicht gespeichert.",
        "key_too_long": "Der Themenname dieser Angabe ist zu lang. Sie wurde nicht gespeichert.",
        "blocked_by_stronger_entry": "Sie haben selbst eine Erinnerung zu diesem Thema verfasst. Sie wurde nicht überschrieben. Bearbeiten Sie diese Erinnerung, wenn Sie sie ändern möchten.",
    },
    "fr-FR": {
        "trigger_language_setting": "La langue des réponses est un paramètre, pas un souvenir. Cet élément n’a donc pas été enregistré en mémoire.",
        "draft_says_not_lasting": "Cela ressemble à une demande pour cet échange plutôt qu’à une préférence durable ; elle n’a donc pas été enregistrée. Pour l’appliquer à chaque fois, dites « désormais… ».",
        "rate_limited": "Un souvenir vient d’être enregistré ; celui-ci a donc été ignoré. Répétez-le plus tard pour qu’il soit enregistré.",
        "tainted_input": "Cet échange utilise du contenu externe ; rien n’a donc été enregistré automatiquement. Vous pouvez le confirmer manuellement.",
        "draft_too_long": "Cet élément est trop long et n’a pas été enregistré. Une formulation plus courte est plus facile à mémoriser correctement.",
        "kind_not_allowed": "Ce type de contenu n’est pas enregistré dans la mémoire personnelle.",
        "draft_echoed_sanitizer_marker": "Un problème est survenu lors de la préparation de cet élément ; il n’a pas été enregistré.",
        "key_too_long": "Le nom du sujet de cet élément est trop long ; il n’a pas été enregistré.",
        "blocked_by_stronger_entry": "Vous avez vous-même écrit un souvenir sur ce sujet ; il n’a pas été remplacé. Modifiez ce souvenir pour le changer.",
    },
}


def reason_user_message(reason: str | None, *, lang: str = "en-US") -> str | None:
    """拒因对应的用户可见文案；内部拒因返回 `None`（**没有码可以显示**）。

    ★ 回执侧只该拿这个函数的返回值，不该拿 `CaptureOutcome.reason`。
    拿不到文案就是「这条不必告诉用户」，而不是「显示这个码」。
    """
    if not reason or not REASON_USER_FACING.get(reason, False):
        return None
    from bowhead_core.presentation_locale import resolve_presentation_locale

    table = REASON_MESSAGES[resolve_presentation_locale(lang)]
    return table[reason]  # Missing copy must fail instead of exposing an internal reason code.


#: Entire output when the drafter judges that the sentence should not be remembered.
#: ★ Test equality, not a prefix: out.upper().startswith("SKIP") silently discards the whole English preference
#: 「Skip the executive summary for weekly reports」
#: despite English entries existing in production (observed on 116). A protocol sentinel must stand alone,
#: never form part of the body.
DRAFT_SKIP_SENTINEL = "SKIP"

ACTION_WRITTEN = "written"
ACTION_SKIPPED = "skipped"
ACTION_REFUSED = "refused"
ACTION_NEEDS_CONFIRMATION = "needs_confirmation"
#: This belongs in settings, not memory. Separate from ACTION_SKIPPED because handling differs:
#: skip means nothing needs to happen; this means something should happen, just outside memory.
#: Combining them gives callers no way to distinguish them, which previously made a whole turn's language request silently disappear.
ACTION_SETTING_SUGGESTED = "setting_suggested"

#: Setting targeted by ACTION_SETTING_SUGGESTED (field name in user_settings).
#: User-facing entry: 「回答语言」 on the settings page.
SETTING_OUTPUT_LANGUAGE = "output_language"

_TRUE = {"1", "true", "yes", "on"}


@dataclass
class _CaptureRateLimiter:
    """起草档的成本护栏：per-user 冷却 + 日上限。

    ★ 为什么必须有：turn 收尾是**全通道共享**路径（定时 routine、飞书的 completed turn
    同样触发），每个命中的 turn 就是一次 LLM 起草调用。旁边那条蒸馏路
    （`bowhead_memory.candidates.DreamingLight`）早就有 `min_interval_seconds=60`
    + `daily_cap=40`，起草档一条都没接 —— 翻开 flag 就是没有闸门地放水。
    这里刻意抄它的形制与数值，两条路成本护栏同款。

    ⚠ **单 worker 前提**：进程内 dict，与 `DreamingLight` / `PauseStateCache` 同款
    （部署已实证）。重启清零可接受 —— 它是**成本护栏不是配额**，
    不承担「一天最多记 40 条」这种业务承诺。多 worker 部署时护栏会按 worker 数放大，
    要真配额得挪到库里。
    """

    min_interval_seconds: float = 60.0
    daily_cap: int = 40
    _last_at: dict[str, float] = field(default_factory=dict)
    _daily: dict[str, tuple[str, int]] = field(default_factory=dict)

    def take(self, user_id: str) -> bool:
        """允许则记账并返回 True；被限流返回 False（**不记账**）。"""
        now_t = time.monotonic()
        last = self._last_at.get(user_id)
        if last is not None and (now_t - last) < self.min_interval_seconds:
            return False
        today = datetime.now(tz=UTC).date().isoformat()
        day, count = self._daily.get(user_id, (today, 0))
        if day == today and count >= self.daily_cap:
            return False
        self._last_at[user_id] = now_t
        self._daily[user_id] = (today, count + 1) if day == today else (today, 1)
        return True


#: Process-local singleton. Tests must pass their own instance instead of modifying it.
_DEFAULT_RATE_LIMITER = _CaptureRateLimiter()


def outcome_gives_the_utterance_a_home(outcome: Any) -> bool:
    """自动捕获这一轮是否已经让用户那句话**有了着落**——碰撞矩阵②的判据。

    ＝写进了记忆（WRITTEN），**或**已有一张卡在等用户确认（NEEDS_CONFIRMATION：
    taint 降级新建的卡 / 判重命中已存在的卡）。
    ★ 此前只认 WRITTEN。而生产上绝大多数轮次本轮用过工具 ⇒ 走 taint 降级出卡，
    这张卡挡不住 NL 意图对同一句话再发一张。
    （NEEDS_CONFIRMATION 的「无候选池」那一支也没有卡，但那时 NL 意图同样没有候选池可写，不冲突。）
    """
    return getattr(outcome, "action", None) in (ACTION_WRITTEN, ACTION_NEEDS_CONFIRMATION)


def escalate_persisted(invocations: Any) -> bool:
    """本轮 `feedback_escalate` 是否**成功落库**了一条团队候选（碰撞矩阵③的判据）。

    ★ 看结构化回执，不看「调没调」：工具会因目标不存在 / 同名歧义 / 配额被拒，
    那时回执是 `{"ok": False, "error": ...}`，用户的话并没有着落，个人这条路不该让路。
    成功回执是 `{"ok": True, "candidate_id": ..., "outcome": created|merged|duplicate}`——
    merged / duplicate 也算有着落（团队候选池里已经有这件事）。
    ★ 递归看 `nested`：同 `deliverable_ledger.count_memory_receipts`，工具可能被包在子调用里。
    """
    import json as _json

    for inv in invocations or ():
        if getattr(inv, "tool_name", "") == "feedback_escalate":
            res = getattr(inv, "result", None)
            if isinstance(res, str):
                try:
                    res = _json.loads(res)
                except ValueError:
                    res = None
            if isinstance(res, dict) and res.get("ok") is True and res.get("candidate_id"):
                return True
        if escalate_persisted(getattr(inv, "nested", None) or ()):
            return True
    return False


def auto_capture_enabled() -> bool:
    """Gate for line B batch 1a. Default on; when set to 0 this module takes no step and behavior is unchanged."""
    return os.environ.get("FEATURE_MEMORY_AUTO_CAPTURE", "1").strip().lower() in _TRUE


@dataclass(frozen=True)
class CaptureOutcome:
    """一次直写尝试的结果。`reason` 只在非 written 时有值，用于回执与空跑报告。"""

    action: str
    entry_id: str | None = None
    reason: str | None = None
    stored_text: str | None = None
    #: Target setting for ACTION_SETTING_SUGGESTED; None for other actions.
    #: ★ Supply only the setting name; do not guess the requested language. Extracting a target value from a request to always use English
    #: needs another classifier; a wrong guess changes a setting the user never requested.
    #: UI directs users to that form; they choose the value themselves.
    suggested_setting: str | None = None


#: Drafter: narrow spoken instructions to one narrowly scoped memory. Injected (provided by caller by default),
#: allowing router-independent tests and leaving the drafting tier choice to the caller.
Drafter = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class DraftResult:
    """起草 + 内容校验的结果。`text` 与 `reason` **恰有一个**非空。"""

    text: str | None = None
    reason: str | None = None


async def draft_personal_memory(
    *,
    user_text: str,
    drafter: Drafter,
    kind: str = "preference",
    revision_comment: str | None = None,
    trace: str = "",
) -> DraftResult:
    """把一句口语收窄成一条记忆，并跑完**内容侧**的三道校验。

    ★★ 抽出来是因为它有**两个调用方**：turn 收尾的零点击直写，与用户点「改一下」
    之后的重新起草。两边各写一遍的话，第二条路迟早比第一条少一道闸
    —— 而少的那道一定是最不显眼的 `echoes_escape_marker`（转义记号回收），
    它正是「转义而不是拦截」这个策略敢用的前提。仓里「一个概念 N 处实现」
    的教训已经吃过多次。

    ⚠ 这里**不做**授权、taint、限流、写闸与裁决：那些是**路径**的判据，
    两条路各不相同（重新起草是用户点出来的，不需要再过一次授权前置）。
    本函数只管「这段文本本身能不能用」。

    `revision_comment` 是用户说的「哪里不对」。★ 它和 `user_text` 一样是
    **用户输入**，同样要过 `sanitize_for_injection` —— 它会被拼进起草提示词，
    不消毒就是给了一个直连起草器的注入口。
    """
    parts = [sanitize_for_injection(user_text)]
    if revision_comment and revision_comment.strip():
        parts.append("用户对上一版的意见：" + sanitize_for_injection(revision_comment))
    try:
        drafted = await drafter("\n".join(parts))
    except Exception as e:  # noqa: BLE001 - Drafting failure must not fail the caller.
        logger.warning("memory_auto_capture.draft_failed trace=%s err=%s", trace, e)
        return DraftResult(reason="draft_failed")
    text = " ".join(str(drafted or "").split())
    if not text:
        return DraftResult(reason="draft_empty")
    # ★★ Enforce the semantic second-layer criterion here, rather than leaving it to the drafter implementation.
    # Previously only build_router_drafter understood this protocol (using startswith);
    # swapping drafters invalidated it entirely. Teaching a protocol in prompts without backend recognition
    # is a recurring repository bug; the backend must enforce it.
    # ⚠ Equality check: see DRAFT_SKIP_SENTINEL above.
    if text.strip().upper() == DRAFT_SKIP_SENTINEL:
        return DraftResult(reason="draft_says_not_lasting")
    # ⚠ Copy limits and the kind allowlist from the REST write path (routes/memory.py:
    # key≤128 / value≤16KB / kind allowlist). The LLM generates this body with uncontrolled length:
    # a 5KB entry exhausts the injection budget and users cannot edit it (the memory page's edit endpoint
    # rejects over-limit values). A kind outside the allowlist makes resolve_memory_policy use _HIDDEN_POLICY,
    # persisting an entry that users cannot see or revoke.
    if len(text) > _MAX_VALUE_CHARS:
        return DraftResult(reason="draft_too_long")
    if kind not in _ALLOWED_KINDS:
        return DraftResult(reason="kind_not_allowed")
    if echoes_escape_marker(text):
        # Our own emitted escape marker in output means the model mistook display replacement for original text.
        logger.warning("memory_auto_capture.draft_echoed_marker trace=%s", trace)
        return DraftResult(reason="draft_echoed_sanitizer_marker")
    return DraftResult(text=text)


async def capture_modification_request(
    *,
    user_text: str,
    suggested_key: str,
    user_id: str,
    turn_id: str | None,
    workspace_id: str | None,
    user_memory_store: Any,
    drafter: Drafter,
    input_is_tainted: bool,
    user_paused: bool,
    kind: str = "preference",
    rate_limiter: _CaptureRateLimiter | None = None,
    candidate_store: Any | None = None,
) -> CaptureOutcome:
    """走完五道闸；只有全过才写。任何一道拦下都**不写**并说明原因。

    `input_is_tainted` 与 `user_paused` 都是**必填**：调用方（服务端）负责确定性计算，
    不给默认值 —— 默认值会让新调用点在没想过这件事的情况下静默拿到「不 taint、没暂停」。
    """
    if not auto_capture_enabled():
        return CaptureOutcome(ACTION_SKIPPED, reason="flag_off")

    # ★★ If the user disabled automatic learning, take no steps at all.
    # This precedes taint: taint means unsafe this time; pause means this path should never run.
    #
    # ⚠ This gate did not exist (caught in 2026-09-09 review): PauseStateCache.is_paused
    # was checked only on the distillation path (DreamingLight), never on the zero-click direct-write path.
    # Explicitly disabling automatic learning still allowed automatic writes, without even asking
    # (the suggestion path at least displayed a card). Independent switches controlling separate paths are a recurring pattern here.
    if user_paused:
        return CaptureOutcome(ACTION_SKIPPED, reason="learning_paused")

    # ① Authorization first: deterministic match; do not trust LLM self-attestation.
    decision, _hits = classify_user_message(user_text)
    if decision == DECISION_LANGUAGE_SETTING:
        # ★ Reroute instead of skip. Language requests have a specific destination (the settings page's 「回答语言」),
        # just outside memory; treating them as skip leaves the user's request unanswered.
        return CaptureOutcome(
            ACTION_SETTING_SUGGESTED,
            reason=f"trigger_{decision}",
            suggested_setting=SETTING_OUTPUT_LANGUAGE,
        )
    if decision != DECISION_PERSISTENT:
        return CaptureOutcome(ACTION_SKIPPED, reason=f"trigger_{decision}")

    # ②.5 Cost guard: after matching, before drafting.
    # Before matching would consume quota for ordinary nonmatching conversation; after drafting would block only after spending money.
    # Taint comes before it because that branch never calls the LLM and must not consume quota.
    limiter = rate_limiter if rate_limiter is not None else _DEFAULT_RATE_LIMITER
    if not limiter.take(user_id):
        logger.info("memory_auto_capture.rate_limited user=%s turn=%s", user_id, turn_id)
        return CaptureOutcome(ACTION_SKIPPED, reason="rate_limited")

    # ③ Draft + validate content (same function as redrafting via the edit action; see draft_personal_memory).
    _draft = await draft_personal_memory(user_text=user_text, drafter=drafter, kind=kind, trace=str(turn_id or ""))
    if _draft.text is None:
        # Drafter failure / empty output ⇒ nothing happened; content rejection ⇒ a refusal.
        # Drafter failure/empty output and a model judgment not to remember both mean nothing happened;
        # only invalid content (too long/wrong kind/echoed escape marker) constitutes a refusal.
        _act = ACTION_SKIPPED if _draft.reason in ("draft_failed", "draft_empty", "draft_says_not_lasting") else ACTION_REFUSED
        return CaptureOutcome(_act, reason=_draft.reason)
    text = _draft.text

    # ② Taint → downgrade to a confirmation card; never bypass confirmation.
    #
    # ⚠ Changed here by user decision on 2026-09-09. Previously returned NEEDS_CONFIRMATION immediately,
    # a mere label: nothing created the card, so the user's request still disappeared.
    # Taint meant any tool used this turn; observed on 116 in 395/400 = 98.8% of turns,
    # leaving this path reachable in at most 1.2%, effectively disabling the feature.
    #
    # Now enqueue a candidate: third-party text never reaches the confirmation-free path (original security boundary unchanged),
    # while the user's actual words survive and need only one click.
    # ★ After drafting: the candidate needs narrowed body text or the card has nothing to display.
    if input_is_tainted:
        if candidate_store is None:
            # Without a candidate store, fall back to old behavior (report only, no write). Log a warning: this is missing capability,
            # not a normal path; silence recreates correct classification with no handling.
            logger.warning("memory_auto_capture.tainted_no_candidate_store turn=%s", turn_id)
            return CaptureOutcome(ACTION_NEEDS_CONFIRMATION, reason="tainted_input")
        try:
            cand, enqueue_outcome = await candidate_store.enqueue_if_new_with_outcome(
                user_id=user_id,
                kind=kind,
                key=str(suggested_key or "").strip().lower(),
                value=text,
                reason=None,
                source_turn_id=turn_id,
                workspace_id=workspace_id,
                origin=ORIGIN_FEEDBACK_DRAFT,
            )
        except Exception as e:  # noqa: BLE001 - Enqueue failure must not fail a turn.
            logger.warning("memory_auto_capture.enqueue_failed turn=%s err=%s", turn_id, e)
            # ★ Do not report tainted_input: that reason promises a waiting confirmation card, which enqueue failure never created.
            return CaptureOutcome(ACTION_SKIPPED, reason="write_failed")
        if enqueue_outcome == ENQUEUE_CREATED and cand is not None:
            return CaptureOutcome(
                ACTION_NEEDS_CONFIRMATION,
                reason="tainted_input",
                entry_id=getattr(cand, "id", None),
                stored_text=text,
            )
        if enqueue_outcome == ENQUEUE_DUPLICATE_PENDING:
            # Identical content already awaits confirmation in the candidate store; a card exists, just not newly created this turn.
            return CaptureOutcome(ACTION_NEEDS_CONFIRMATION, reason="tainted_input")
        # ★★ All others mean no card: write gate rejection (narrowed draft is one-off / contains credentials / requests language…) or suppression.
        # Previously all three None cases reported tainted_input, promising a nonexistent card (checklist ④).
        # ⚠ Do not add a distinct suppression reason: this path uses origin feedback_draft, whose decisions
        # never write suppression (test_deciding_a_feedback_draft_writes_no_suppression); that branch is currently unreachable.
        # A separate reason would be dead code protected only by its own tests.
        logger.info("memory_auto_capture.tainted_not_enqueued turn=%s outcome=%s", turn_id, enqueue_outcome)
        return CaptureOutcome(ACTION_SKIPPED, reason="tainted_not_enqueued")

    # ④ Write gate: automatic writes are no license to bypass the gate.
    from bowhead_core.memory_write_gate import check as gate_check

    key = str(suggested_key or "").strip().lower()
    if len(key) > _MAX_KEY_CHARS:
        return CaptureOutcome(ACTION_REFUSED, reason="key_too_long")
    # ⚠ Do not pass actor_type. Gate ② (attribution) checks for turn_id or a UI actor;
    # actor_type="user" specifically means manual creation on the memory page in the gate's definition.
    # Passing it unconditionally satisfies attribution, allowing unsourced memory even with turn_id=None,
    # precisely what REASON_NO_ATTRIBUTION prevents. Caught in closure review on 2026-09-08.
    verdict = gate_check(
        value_text=text,
        has_turn_id=bool(turn_id),
        key=key or None,
    )
    if not verdict.allowed:
        return CaptureOutcome(ACTION_REFUSED, reason=verdict.reason)

    # ⑤ Decision table + deduplication
    if not key:
        # key enables reusing one key for one topic; without a classifier-provided key, do not auto-write.
        # Inventing one creates a second entry for the same thing, precisely what supersession semantics prevent.
        return CaptureOutcome(ACTION_SKIPPED, reason="no_key")
    try:
        result = await user_memory_store.commit_memory_write(
            user_id=user_id,
            kind=kind,
            key=key,
            value=text,
            source=CAPTURE_SOURCE,
            workspace_id=workspace_id,
            created_source_turn_id=turn_id,
            # ⚠ Likewise do not impersonate user: actor_type becomes last_modified_actor_type,
            # which the memory page displays as who edited it. Auto-captured entries would look identical to
            # manual user edits; users could not distinguish their own entries. Use this path's own identity.
            actor_type=CAPTURE_SOURCE,
            actor_id=user_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("memory_auto_capture.write_failed turn=%s err=%s", turn_id, e)
        return CaptureOutcome(ACTION_SKIPPED, reason="write_failed")

    entry = getattr(result, "entry", None)
    action = str(getattr(result, "action", "") or "")
    logger.info(
        "memory_auto_capture.captured turn=%s user=%s key=%s action=%s",
        turn_id,
        user_id,
        key,
        action or "?",
    )
    # ★ If the decision table says blocked, nothing was written. Reporting written makes 1c disclosure
    # promise an applied, revocable preference even though no memory exists and Undo has no target
    # (entry=None ⇒ entry_id=None). Caught in closure review on 2026-09-08:
    # previously this always returned ACTION_WRITTEN.
    if action == "blocked":
        blocked_by = getattr(result, "blocked_by", None)
        return CaptureOutcome(
            ACTION_REFUSED,
            reason="blocked_by_stronger_entry",
            entry_id=getattr(blocked_by, "id", None),
        )
    return CaptureOutcome(
        ACTION_WRITTEN,
        entry_id=getattr(entry, "id", None),
        reason=action or None,
        stored_text=text,
    )


# Drafting-tier prompt for narrowly rewriting remembered instructions.
# The prompt itself warns against turning a limited scope preference into a broad
# prohibition that would remove analytical depth from future turns.
_DRAFT_PROMPT = """\
把用户这句口语指令改写成**一条作用面尽量窄**的长期偏好，只输出改写后的正文。

规则：
1. **必须收窄**。给出适用场景，别写成放之四海的禁令。
   反例（真实事故）：「仅分析用户指定的部门」→ 被逐轮放大成「不要扩展任何维度」，
   把全部分析深度掐死了。
   好：「以后都要详细」→「销售分析类问题默认给完整复盘（现状+归因+建议）」
   坏：「以后都要详细」→「所有回答都要详细」
2. 只写用户要的**做法**，不要加你自己的推断、不要加理由、不要加前后缀。
3. 一句话，40 字以内，用用户说话的语言。
4. 写之前先过这三问，任何一问答「不」就**只输出一个词**：SKIP
   - **以后还有用吗**？只对这一轮成立的（「这次先按月看」「其余章节不要动」
     「这张图先去掉图例」）→ SKIP。它不是长期偏好，记下来会在以后每一轮误导你。
   - **别处是不是已经有了**？工作区词表、业务规则、或用户已有的记忆里已经写着的
     → SKIP。重复记一遍只会让两处慢慢对不上。
   - **是不是在说「你该怎么做」**？个人记忆只管风格、形态、语言、详略。
     说的是「数据/口径应该是什么」（指标怎么算、概念怎么定义、某个数字对不对）
     → SKIP，那属于团队，有自己的入口。
   ⚠ SKIP 要**单独成一条输出**，不要写成「SKIP，因为…」，也不要放在正文开头。
"""

#: Drafting-tier timeout, shorter than classification: only one sentence. Give up if stuck; do not delay turn completion.
_DRAFT_TIMEOUT_SEC = 20.0


def build_router_drafter(router: Any, *, trace_id: str = "") -> Drafter:
    """用 router 的 echo 档做起草器（与 `auto_learner` 同一档、同一形制）。

    ★ 它**只在第一层粗筛命中时**被调用（0.5% 的 turn），所以「不新增每轮 LLM 调用」
    这条约束是成立的 —— 但它确实是一次新调用，配额/冷却见下方 TODO。
    """

    async def _draft(text: str) -> str:
        import asyncio

        from bowhead_router.types import LLMRequest, Message

        req = LLMRequest(
            model="",
            messages=[Message(role="user", content=text[:2000])],
            system=_DRAFT_PROMPT,
            temperature=0.1,
            max_output_tokens=120,
            trace_id=trace_id,
        )
        resp = await asyncio.wait_for(router.invoke("echo", req), timeout=_DRAFT_TIMEOUT_SEC)
        out = str(getattr(resp, "content", "") or "").strip()
        # Exit when the model judges this is not a lasting preference; it understands context better than keyword filtering.
        # ⚠ Return unchanged. draft_personal_memory owns SKIP detection
        # (equality check; see DRAFT_SKIP_SENTINEL). Previously this used
        # startswith("SKIP") to return an empty string: two separate checks with different criteria,
        # silently swallowing the entire English preference 「Skip the executive summary…」.
        return out

    return _draft


__all__ = [
    "build_router_drafter",
    "ACTION_NEEDS_CONFIRMATION",
    "ACTION_REFUSED",
    "ACTION_SETTING_SUGGESTED",
    "ACTION_SKIPPED",
    "ACTION_WRITTEN",
    "CAPTURE_SOURCE",
    "REASON_MESSAGES",
    "REASON_USER_FACING",
    "SETTING_OUTPUT_LANGUAGE",
    "CaptureOutcome",
    "auto_capture_enabled",
    "DraftResult",
    "outcome_gives_the_utterance_a_home",
    "escalate_persisted",
    "capture_modification_request",
    "draft_personal_memory",
    "reason_user_message",
]
