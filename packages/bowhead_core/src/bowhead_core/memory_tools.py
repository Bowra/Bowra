"""记住-rebuild (dogfood s_3e854 item 2): the `remember` / `recall` Lead tools.

The dogfood failure: a user says「记住 X」, the Lead replies「我记住了」, and NOTHING
persists — there was no write tool, so the confirmation was free-form prose decoupled
from any store. These two deterministic tools close that loop:

- ``remember(kind, text)`` → ``MemoryNotesStore.append`` at USER scope (the user's own
  long-term memory, single-segment-per-user, backs the /memory page). Returns the stored
  bullet so the Lead's「已记住」quotes a real write (PRD D7), never hallucinated.
- ``recall(query?)`` → reads the user's notes back, optionally filtered by a topic word.

The recall READ-into-the-prompt path is ``build_memory_pack`` (wired in lead_orchestrator,
injected as ``<remembered_about_user>``); these tools are the WRITE side + an on-demand
mid-turn lookup (PRD D4).

Per ``clarification_tool.py``: ``tool_registry.execute`` swallows exceptions, so handlers
return error dicts (never raise) — the returned dict is what the LLM sees.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema

from bowhead_core.memory_notes import (
    MAX_BULLET_CHARS,
    NotesCharLimitError,
    NotesLockedError,
)
from bowhead_core.prompt_injection_defense import sanitize_for_injection
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

logger = logging.getLogger(__name__)

# kind → Chinese label prefix on stored bullet, so /memory + recall show the category.
_KIND_LABELS: dict[str, str] = {
    "rule": "规则",
    "preference": "偏好",
    "fact": "事实",
    "relationship": "称谓",
    "note": "备注",
}
_DEFAULT_KIND = "note"

# Source tag on note row: distinguish agent-written remember from a UI edit.
_REMEMBER_SOURCE = "user_directive"
# Phase 2: nl_intent kinds the candidate store accepts (its enum, minus datarc_binding).
_NL_INTENT_KINDS = ("preference", "constraint", "note")


def _norm_kind(raw: Any) -> str:
    k = str(raw or "").strip().lower()
    return k if k in _KIND_LABELS else _DEFAULT_KIND


# chrome-i18n (dogfood residue): labels are baked into stored text; English users' notes previously had Chinese prefixes
# such as `• 偏好：In KPI cards…`. Choose labels at write time by user output_language (same source as other chrome).
_KIND_LABELS_EN: dict[str, str] = {
    "rule": "Rule",
    "preference": "Preference",
    "fact": "Fact",
    "relationship": "Relationship",
    "note": "Note",
}


_KIND_LABELS_BY_LOCALE: dict[str, dict[str, str]] = {
    "zh-CN": _KIND_LABELS,
    "en-US": _KIND_LABELS_EN,
    "de-DE": {
        "rule": "Regel",
        "preference": "Präferenz",
        "fact": "Tatsache",
        "relationship": "Beziehung",
        "note": "Notiz",
    },
    "fr-FR": {
        "rule": "Règle",
        "preference": "Préférence",
        "fact": "Fait",
        "relationship": "Relation",
        "note": "Note",
    },
}


_MEMORY_COPY: dict[str, dict[str, str]] = {
    "zh-CN": {
        "memory_unavailable": "长期记忆暂不可用，未能保存。",
        "no_user": "无法确定用户身份，未能保存到个人记忆。",
        "empty_text": "没有可记住的内容。",
        "deduped": "这条已经在你的长期记忆里了。",
        "locked": "你正在编辑记忆，请稍后再让我记一次。",
        "entry_locked": "这条记忆被锁定，未能修改。",
        "rejected": "这条内容无法保存（过长或格式问题）。",
        "write_failed": "保存长期记忆时出错。",
        "stored": "已存为长期记忆，可在「设置 › 记忆」中查看或修改。",
        "too_long": "这条太长了（{length} 字，上限 {limit}），没有保存；请精简成一句话。",
        "missing_key": "需要一个主题标识 key（英文 snake_case，如 kpi_money_unit）。同一主题请复用已有的 key。",
        "confidence_conflict": "这条与你之前明确说过的记忆冲突，没有覆盖；请先确认要不要改。",
    },
    "en-US": {
        "memory_unavailable": "Long-term memory is temporarily unavailable; this was not saved.",
        "no_user": "Your identity could not be determined; this was not saved to personal memory.",
        "empty_text": "There is no content to remember.",
        "deduped": "This is already in your long-term memory.",
        "locked": "You are editing your memory. Ask me to remember this again later.",
        "entry_locked": "This memory is locked and could not be changed.",
        "rejected": "This content could not be saved because it is too long or has an invalid format.",
        "write_failed": "An error occurred while saving long-term memory.",
        "stored": "Saved to long-term memory. You can view or edit it in Settings › Memory.",
        "too_long": "This is too long ({length} characters; limit {limit}) and was not saved. Shorten it to one sentence.",
        "missing_key": "A topic key is required (English snake_case, such as kpi_money_unit). Reuse the existing key for the same topic.",
        "confidence_conflict": "This conflicts with a memory you previously stated explicitly and did not replace it. Confirm whether you want to change it first.",
    },
    "de-DE": {
        "memory_unavailable": "Das Langzeitgedächtnis ist vorübergehend nicht verfügbar. Diese Angabe wurde nicht gespeichert.",
        "no_user": "Ihre Identität konnte nicht ermittelt werden. Diese Angabe wurde nicht in Ihrem persönlichen Gedächtnis gespeichert.",
        "empty_text": "Es gibt keinen Inhalt zum Merken.",
        "deduped": "Diese Angabe befindet sich bereits in Ihrem Langzeitgedächtnis.",
        "locked": "Sie bearbeiten gerade Ihre Erinnerung. Bitten Sie mich später erneut, diese Angabe zu speichern.",
        "entry_locked": "Diese Erinnerung ist gesperrt und konnte nicht geändert werden.",
        "rejected": "Dieser Inhalt konnte wegen seiner Länge oder eines ungültigen Formats nicht gespeichert werden.",
        "write_failed": "Beim Speichern des Langzeitgedächtnisses ist ein Fehler aufgetreten.",
        "stored": "Im Langzeitgedächtnis gespeichert. Sie können die Angabe unter „Einstellungen › Erinnerungen“ ansehen oder bearbeiten.",
        "too_long": "Diese Angabe ist zu lang ({length} Zeichen; Grenze {limit}) und wurde nicht gespeichert. Kürzen Sie sie auf einen Satz.",
        "missing_key": "Ein Themenschlüssel ist erforderlich (englisches snake_case, zum Beispiel kpi_money_unit). Verwenden Sie für dasselbe Thema den vorhandenen Schlüssel.",
        "confidence_conflict": "Diese Angabe widerspricht einer zuvor ausdrücklich von Ihnen angegebenen Erinnerung und hat sie nicht ersetzt. Bestätigen Sie zuerst, ob Sie sie ändern möchten.",
    },
    "fr-FR": {
        "memory_unavailable": "La mémoire à long terme est temporairement indisponible ; cet élément n’a pas été enregistré.",
        "no_user": "Votre identité n’a pas pu être déterminée ; cet élément n’a pas été enregistré dans votre mémoire personnelle.",
        "empty_text": "Il n’y a aucun contenu à mémoriser.",
        "deduped": "Cet élément figure déjà dans votre mémoire à long terme.",
        "locked": "Vous modifiez votre mémoire. Demandez-moi de mémoriser cet élément de nouveau plus tard.",
        "entry_locked": "Ce souvenir est verrouillé et n’a pas pu être modifié.",
        "rejected": "Ce contenu n’a pas pu être enregistré, car il est trop long ou son format est invalide.",
        "write_failed": "Une erreur est survenue lors de l’enregistrement dans la mémoire à long terme.",
        "stored": "Enregistré dans la mémoire à long terme. Vous pouvez le consulter ou le modifier dans « Paramètres › Mémoire ».",
        "too_long": "Cet élément est trop long ({length} caractères ; limite {limit}) et n’a pas été enregistré. Réduisez-le à une phrase.",
        "missing_key": "Une clé de sujet est nécessaire (en anglais, au format snake_case, comme kpi_money_unit). Réutilisez la clé existante pour un même sujet.",
        "confidence_conflict": "Cet élément contredit un souvenir que vous avez explicitement indiqué auparavant et ne l’a pas remplacé. Confirmez d’abord si vous souhaitez le modifier.",
    },
}


#: Model-only instruction attached to receipts that carry internal identifiers. `message` and `stored` are
#: written for the user; ids exist only so the model can pass them to later tool calls.
_INTERNAL_IDS_NOTE = (
    "id, superseded, supersede_blocked, similar_to and blocked_by are internal identifiers for later tool calls "
    "(e.g. supersedes). Never show them, or the topic key, to the user."
)


def _memory_message(key: str, lang: str, **values: Any) -> str:
    from bowhead_core.presentation_locale import resolve_presentation_locale

    return _MEMORY_COPY[resolve_presentation_locale(lang)][key].format(**values)


def _format_bullet(kind: str, text: str, lang: str = "en-US") -> str:
    """Localize only the category label; preserve the supplied memory text and length cap."""
    from bowhead_core.presentation_locale import resolve_presentation_locale

    locale = resolve_presentation_locale(lang)
    labels = _KIND_LABELS_BY_LOCALE[locale]
    separator = "：" if locale == "zh-CN" else ": "
    bullet = f"{labels.get(kind, labels[_DEFAULT_KIND])}{separator}{text}"
    if len(bullet) > MAX_BULLET_CHARS:
        bullet = bullet[: MAX_BULLET_CHARS - 1] + "…"
    return bullet


def _strip_to_text(line: str) -> str:
    """Reduce a stored note line to its bare text: drop the "• "/"📌 " glyph that
    MemoryNotesStore.append prepends, then a known kind label prefix ("规则：" …).
    Used for dedup so the comparison is on the SEMANTIC text — independent of the
    glyph and of which `kind` label the LLM happened to pick this call."""
    s = line.lstrip("•📌 ").strip()
    for locale, labels in _KIND_LABELS_BY_LOCALE.items():
        separator = "：" if locale == "zh-CN" else ": "
        for label in labels.values():
            prefix = f"{label}{separator}"
            if s.startswith(prefix):
                return s[len(prefix) :].strip()
    return s


async def remember_note(
    store: Any,
    *,
    user_id: str,
    kind: str,
    text: str,
    turn_id: str | None = None,
    source: str = _REMEMBER_SOURCE,
    lang: str = "en-US",
) -> dict[str, Any]:
    """Core memory write: normalize → dedupe → resurrection-safe + lock-respecting
    persist to memory_notes. Shared by the explicit `remember` tool (_remember) and
    the implicit nl_intent post-turn capture hook (Phase 2). Returns a result dict;
    never raises (callers feed the dict to the LLM or log it)."""
    kind = _norm_kind(kind)
    # Collapse all whitespace (incl. newlines) so the stored form matches what
    # MemoryNotesStore.append persists (it replaces \n with space) — keeps dedup exact.
    text = " ".join(str(text or "").split())

    if store is None:
        return {"ok": False, "error": "memory_unavailable", "message": _memory_message("memory_unavailable", lang)}
    if not user_id:
        return {"ok": False, "error": "no_user", "message": _memory_message("no_user", lang)}
    if not text:
        return {"ok": False, "error": "empty_text", "message": _memory_message("empty_text", lang)}

    bullet = _format_bullet(kind, text, lang)

    # Load the user's ACTIVE (non-trashed) note once — used for both dedup and the
    # resurrection-safe write below. Best-effort: a read failure falls back to a
    # plain append (pre-fix behavior).
    active = None
    active_load_ok = True
    try:
        active = await store.get(user_id=user_id)
    except Exception:
        active_load_ok = False

    # Idempotent dedup: compare the SEMANTIC text (glyph + kind-label stripped) for
    # EXACT equality against each stored line. Exact (not substring) so a shorter
    # rule that is a substring of a longer one is NOT falsely dropped; label-stripped
    # so the same directive restated under a different `kind` doesn't persist twice.
    # This keeps the ⑤ `好的` confirmation loop from re-appending every turn.
    if active and active.content:
        stored_texts = {_strip_to_text(ln) for ln in active.content.split("\n") if ln.strip()}
        if text in stored_texts:
            return {"ok": True, "stored": bullet, "kind": kind, "scope": "user", "deduped": True, "message": _memory_message("deduped", lang)}

    # No ACTIVE note → fresh write via upsert_content (so a /memory reset's TRASHED
    # row isn't resurrected: append() reads include_trashed=True and would un-delete
    # the old content). upsert_content has no lock check, unlike append — so re-assert
    # the editing lock here to keep the user-vs-agent boundary on the lock-held +
    # /memory-reset race (a reset trashes the row WITHOUT clearing editing_locked_until,
    # so the lock can be held while active is None). Review P2.
    fresh_write = active_load_ok and active is None
    if fresh_write:
        try:
            lock_held = await store.is_lock_active(user_id=user_id)
        except Exception:
            lock_held = False
        if lock_held:
            return {"ok": False, "error": "locked", "message": _memory_message("locked", lang)}

    try:
        if fresh_write:
            await store.upsert_content(
                user_id=user_id,
                content="• " + bullet,
                source=source,
                source_turn_id=turn_id,
            )
        else:
            await store.append(
                user_id=user_id,
                bullet=bullet,
                source=source,
                source_turn_id=turn_id,
            )
    except NotesLockedError:
        return {"ok": False, "error": "locked", "message": _memory_message("locked", lang)}
    except (NotesCharLimitError, ValueError) as e:
        logger.warning("remember.write_rejected user=%s err=%s", user_id, e)
        return {"ok": False, "error": "rejected", "message": _memory_message("rejected", lang)}
    except Exception as e:
        logger.exception("remember.write_failed user=%s err=%s", user_id, e)
        return {"ok": False, "error": "write_failed", "message": _memory_message("write_failed", lang)}

    logger.info("remember.stored user=%s kind=%s source=%s len=%d", user_id, kind, source, len(bullet))
    return {
        "ok": True,
        "stored": bullet,
        "kind": kind,
        "scope": "user",
        "message": _memory_message("stored", lang),
    }


# ─────────────────────────────────────────────────────────────────────
# Memory redesign batch 1: route `remember` to canonical storage (`memory_entries`)
# Upstream: docs/sprints/memory-spike-2026-08-29/tech-design.md §4.2 (a)(a2)(a3)
# ─────────────────────────────────────────────────────────────────────

#: Tool kinds (five user/model-readable words) → `memory_entries.kind` (four policy-recognized kinds).
#: `fact` / `relationship` have no policy category; store as `note` (free-text background),
#: with `note_text_v1` value_schema determined by kind in `commit_memory_write`,
#: never guessed via `_value_schema_for` (inferred `text_v1` fails policy, silently dropping the entry).
_ENTRY_KIND_BY_TOOL_KIND: dict[str, str] = {
    "rule": "constraint",
    "preference": "preference",
    "fact": "note",
    "relationship": "note",
    "note": "note",
}


def _normalize_memory_key(raw: Any) -> str:
    """把写入方给的主题 key 收成 snake_case。

    ★ 这里**不做**「缺了就按内容算一个」的兜底。`kind='note'` 的老 key 正是内容哈希
    （`_stable_note_key`）——换一个字就是另一个 key ⇒ 自由文本记忆永远判不出同主题，
    取代也就永远做不了（tech-design §4.2 a3）。主题必须由写入方命名。
    """
    text = str(raw or "").strip().lower()
    out = [ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in text]
    key = "_".join(part for part in "".join(out).split("_") if part)
    return key[:60]


async def _notes_hold_same_text(
    store: Any,
    *,
    user_id: str,
    text: str,
    memory_store: Any = None,
) -> bool:
    """存量 notes 里是否已经有逐字相同的一条。

    ⚠ 这条兜底**只在 §5 存量迁移跑完之前成立**：那之前 `build_memory_pack` 还在注入
    notes 段，不查一下就写，同一条偏好会在 `<remembered_about_user>` 里出现两次。

    ★ 迁移跑完之后它必须**失效**，否则会从「防重复」变成「谎报」：
    `memory_pack` 对已迁移用户不再注入 notes（`memory_storage_ready()` 且
    `is_legacy_notes_migrated()` 时跳过），而 notes 行是**故意保留**的回滚位 ——
    于是「notes 里有逐字相同的一行」不再意味着「这条记忆在起作用」。
    具体翻车：用户把某条迁移条目移进回收站，再对 agent 说同样一句话 ⇒
    notes 仍逐字命中 ⇒ 早返回「这条已经在你的长期记忆里了」，而它哪里都不在。
    2026-09-04 收口 review 逮到，彼时 116 上迁移已跑完、canonical 已开。
    """
    from bowhead_core.memory_write_policy import is_verbatim_duplicate

    if store is None:
        return False
    # Migration marker is **per-user**: unmigrated users still need this fallback; migrated users must bypass it.
    # Unreadable marker → **conservatively use fallback** (temporary duplication beats risk of false confirmation).
    if memory_store is not None:
        try:
            if await memory_store.is_legacy_notes_migrated(user_id):
                return False
        except Exception as e:  # noqa: BLE001 - Unreadable migration marker must not fail all of remember
            logger.warning("remember.migration_flag_failed user=%s err=%s", user_id, e)
    try:
        note = await store.get(user_id=user_id)
    except Exception:
        return False
    content = getattr(note, "content", "") or ""
    return any(is_verbatim_duplicate(text, _strip_to_text(line)) for line in content.split("\n") if line.strip())


async def _remember_canonical(args: dict[str, Any], ctx: ToolExecutionContext, *, lang: str) -> dict[str, Any]:
    """闸开时的 `remember`：写 `memory_entries`，带主题 key / 类型 / 取代声明。"""
    store = getattr(ctx, "user_memory_store", None)
    user_id = getattr(ctx, "user_id", "") or ""
    kind = _norm_kind(args.get("kind"))
    text = " ".join(str(args.get("text") or "").split())
    key = _normalize_memory_key(args.get("key"))

    if store is None:
        return {"ok": False, "error": "memory_unavailable", "message": _memory_message("memory_unavailable", lang)}
    if not user_id:
        return {"ok": False, "error": "no_user", "message": _memory_message("no_user", lang)}
    if not text:
        return {"ok": False, "error": "empty_text", "message": _memory_message("empty_text", lang)}
    # ★ Canonical path needs its own length gate: notes had `MAX_BULLET_CHARS` (overflow raises
    # `NotesCharLimitError`); rerouting removed that protection entirely, allowing 8KB bodies to persist verbatim
    # and enter `<remembered_about_user>` **every turn**. Meanwhile receipt's `stored` is a bullet
    # truncated to 500 characters; tool description requires quoting stored to confirm what was saved,
    # so the model confirms text **different from** what actually persisted.
    if len(text) > MAX_BULLET_CHARS:
        return {
            "ok": False,
            "error": "rejected",
            "message": _memory_message("too_long", lang, length=len(text), limit=MAX_BULLET_CHARS),
        }
    if not key:
        # Reject missing key instead of inventing one: invented keys can only derive from content, so changes create new topics
        # and prevent supersede detection. Receipt tells the model what is missing, allowing same-turn repair/retry.
        return {
            "ok": False,
            "error": "missing_key",
            "message": _memory_message("missing_key", lang),
        }

    raw_supersedes = args.get("supersedes")
    if isinstance(raw_supersedes, str):
        raw_supersedes = [raw_supersedes]
    supersedes = [str(s).strip() for s in (raw_supersedes or []) if str(s or "").strip()]

    bullet = _format_bullet(kind, text, lang)
    # ⚠ Nonempty `supersedes` **must not** use this fallback: early return swallows the entire supersede declaration
    # (`还是只看 Top 5，把 Top 10 那条去掉`: text treated as duplicate, Top 10 keeps entering prompt).
    # Same shape already fixed independently in `commit_memory_write` and approve;
    # this is the third location. Cost: temporarily one copy each in notes and entries
    # (gone after §5 legacy migration), far less severe than losing an explicit user supersede request.
    if not supersedes and await _notes_hold_same_text(
        getattr(ctx, "memory_notes_store", None),
        user_id=user_id,
        text=text,
        memory_store=store,
    ):
        return {
            "ok": True,
            "stored": bullet,
            "kind": kind,
            "scope": "user",
            "deduped": True,
            "message": _memory_message("deduped", lang),
        }

    from bowhead_core.user_memory import MemoryEntryLockedError

    try:
        result = await store.commit_memory_write(
            user_id=user_id,
            kind=_ENTRY_KIND_BY_TOOL_KIND.get(kind, "note"),
            key=key,
            value=text,
            # Confidence is not a parameter: derive high deterministically from this identity string. See memory_write_policy.
            source=_REMEMBER_SOURCE,
            memory_type=args.get("memory_type"),
            supersedes=supersedes,
            created_source_turn_id=getattr(ctx, "turn_id", None),
        )
    except MemoryEntryLockedError:
        # A locked entry on this topic, not the notes editing lock: keep the codes distinct for the receipt.
        return {"ok": False, "error": "entry_locked", "message": _memory_message("entry_locked", lang)}
    except Exception as e:
        logger.exception("remember.canonical_write_failed user=%s err=%s", user_id, e)
        return {"ok": False, "error": "write_failed", "message": _memory_message("write_failed", lang)}

    if result.action == "blocked":
        # Currently unreachable: `remember` always writes as `user_directive` ⇒ high, while decision table rejects only
        # low → high. Keep it because store owns overwrite criteria; tool must not assume permanently high,
        # and a future second writer needs a receipt rather than treating entry=None as success.
        blocked_id = result.blocked_by.id if result.blocked_by else None
        logger.info("remember.blocked user=%s target=%s", user_id, blocked_id)
        return {
            "ok": False,
            "error": "confidence_conflict",
            "blocked_by": blocked_id,
            "message": _memory_message("confidence_conflict", lang),
            "internal_note": _INTERNAL_IDS_NOTE,
        }

    entry = result.entry
    if entry is None:
        # Module contract: return error dicts, never raise (`tool_registry.execute` swallows entire exceptions,
        # leaving the model nothing). Previously assert disappeared under `python -O`,
        # then entry.id raised AttributeError, breaking the same contract twice.
        logger.warning("remember.entry_missing user=%s action=%s", user_id, result.action)
        return {"ok": False, "error": "write_failed", "message": _memory_message("write_failed", lang)}
    logger.info(
        "remember.stored user=%s id=%s action=%s kind=%s key=%s superseded=%d",
        user_id,
        entry.id,
        result.action,
        entry.kind,
        entry.key,
        len(result.superseded),
    )
    # The topic key is omitted: the model wrote it in this call's arguments, and later turns read keys from
    # `<remembered_about_user>`. Echoing it back only invited prose like 「已存为长期记忆（key：…）」.
    # `id` stays: same-turn `supersedes`, the turn receipt and the deliverable ledger all read it.
    out: dict[str, Any] = {
        "ok": True,
        "id": entry.id,
        "stored": bullet,
        "kind": kind,
        "scope": "user",
        "action": result.action,
        "internal_note": _INTERNAL_IDS_NOTE,
    }
    if result.action == "confirmed":
        out["deduped"] = True
        out["message"] = _memory_message("deduped", lang)
        return out
    if result.superseded:
        out["superseded"] = list(result.superseded)
    if result.supersede_blocked:
        out["supersede_blocked"] = list(result.supersede_blocked)
    if result.near_duplicates:
        # Name similar entries only, never merge: automatic merging can silently consume a genuinely new preference.
        out["similar_to"] = list(result.near_duplicates)
    out["message"] = _memory_message("stored", lang)
    return out


async def _recall_canonical(ctx: ToolExecutionContext, *, user_id: str) -> list[str]:
    """**只**返回 canonical 条目（带 `[mem_xxx]`）。notes 那一段由调用方 `_recall` 追加。

    ⚠ 别照着函数名想当然：这里**不含** notes。完整的 recall 读面必须是
    「canonical 条目 + 尚未迁移的 notes 原文」两段 —— 只读 entries 会让 §5 迁移作业
    跑完之前的老用户 `recall` **直接返空**（记忆还整块躺在 notes 里）。
    从第二个地方复用本函数时，记得把 notes 那段也补上。
    """
    lines: list[str] = []
    store = getattr(ctx, "user_memory_store", None)
    if store is not None:
        try:
            entries = await store.list_product_for_user(user_id, workspace_id=getattr(ctx, "workspace_id", None) or None)
        except Exception as e:
            logger.warning("recall.entries_failed user=%s err=%s", user_id, e)
            entries = []
        for e in entries:
            value = e.parsed_value()
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"[{e.id}] {e.key}: {text}")
    return lines


async def _remember(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    """Explicit `remember` tool handler; records the call for this turn's memory receipt.

    Every invocation is an explicit request, whatever its result, so the receipt row is
    written from the structured tool result here and never from the Lead's prose.
    """
    from bowhead_core.memory_turn_requests import from_remember_result, record_best_effort, store_for

    result = await _remember_and_store(args, ctx)
    await record_best_effort(
        store_for(getattr(ctx, "user_memory_store", None), getattr(ctx, "memory_notes_store", None)),
        user_id=getattr(ctx, "user_id", None),
        turn_id=getattr(ctx, "turn_id", None),
        request=from_remember_result(result),
    )
    return result


async def _remember_and_store(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    """Write path of the explicit `remember` tool.

    已分类存储写入 `memory_entries`，保留 id、主题 key、置信度与取代声明。
    未就绪旧库暂用 notes 兼容存储；所有写入均执行长期记忆过滤。
    """
    from bowhead_core.memory_readiness import memory_storage_ready
    from bowhead_core.report_chrome_text import chrome_lang

    lang = chrome_lang(getattr(ctx, "language_context", None))

    # ── Memory redesign batch 2: write gate runs **before branches** (tech-design §4.3) ──
    # Also disabled in production ⇒ enabling write gate alone still let remember write one-off requests/language preferences/secrets
    # directly into memory_notes, making the gate ineffective (flag docs even described them as independent).
    from bowhead_core import memory_write_gate

    verdict = memory_write_gate.check(
        value_text=" ".join(str(args.get("text") or "").split()),
        has_turn_id=bool(getattr(ctx, "turn_id", None)),
        # `remember` writes through an agent for the user, without a UI actor; attribution relies on turn_id.
        actor_type=None,
        key=str(args.get("key") or ""),
        lang=lang,
    )
    if not verdict.allowed:
        logger.info(
            "remember.gate_refused user=%s reason=%s",
            getattr(ctx, "user_id", "") or "",
            verdict.reason,
        )
        return {"ok": False, "error": verdict.reason, "message": verdict.message}

    if memory_storage_ready():
        return await _remember_canonical(args, ctx, lang=lang)
    return await remember_note(
        getattr(ctx, "memory_notes_store", None),
        user_id=getattr(ctx, "user_id", "") or "",
        kind=args.get("kind"),
        text=args.get("text"),
        turn_id=getattr(ctx, "turn_id", None),
        lang=lang,
    )


@dataclass(frozen=True)
class NlIntentCapture:
    """A confident implicit detection and what the candidate enqueue did with it."""

    candidate: Any | None
    enqueue_outcome: str


async def capture_nl_intent(
    *,
    classifier: Any,
    candidate_store: Any,
    user_text: str,
    user_id: str,
    turn_id: str | None = None,
    min_confidence: float = 0.8,
    router: Any | None = None,
) -> NlIntentCapture | None:
    """Phase 2 — IMPLICIT memory capture. Classify the user's turn for memory intent
    and, on a confident hit, ENQUEUE a review candidate (NOT a silent write).

    Why a candidate, not an auto-commit: an implicit capture is a model GUESS (the user
    did NOT say「记住」). Auto-committing it to memory_notes would inject a phantom memory
    into every future Lead prompt (the pack force-injects all notes) with no in-turn
    confirmation surface — the exact probabilistic-write over-capture the audit warns
    against. The candidate lands on the /memory「AI 建议」queue for one-click approval;
    on approve it appends to memory_notes. Explicit「记住」(the deterministic remember
    tool) is unaffected and still auto-commits (D3) — so the two paths never collide.

    Flag-gated by the caller: ``classifier`` is None when BOWHEAD_NL_INTENT_ENABLED is
    off → no-op. Conservative ``min_confidence`` is a pre-filter so the queue isn't
    flooded. NEVER raises — implicit capture must not affect the turn. Returns the
    candidate together with the recorded enqueue outcome (created, duplicate_pending,
    suppressed or gate_refused). Returns None when nothing confident was detected or the
    enqueue produced no outcome (unavailable or failed); only a recorded outcome may back a
    memory receipt.
    """
    if classifier is None or candidate_store is None or not user_id or not (user_text or "").strip():
        return None
    try:
        # credential-readiness D2.1: forward this turn's effective Router (BYO team).
        # Pass kwarg only when present: this function's never-raises contract swallows TypeError into
        # silent None, so minimal fake classifiers without a router parameter can silently stop working while tests stay green.
        if router is not None:
            intent = await classifier.classify(user_text, router=router)
        else:
            intent = await classifier.classify(user_text)
    except Exception as e:
        logger.warning("nl_intent.classify_failed err=%s", e)
        return None
    content = (getattr(intent, "proposed_content", None) or "").strip()
    conf = float(getattr(intent, "confidence", 0.0) or 0.0)
    if not getattr(intent, "detected", False) or not content or conf < min_confidence:
        logger.info(
            "nl_intent.skip detected=%s conf=%.2f min=%.2f has_content=%s", getattr(intent, "detected", False), conf, min_confidence, bool(content)
        )
        return None
    kind = getattr(intent, "kind", "note")
    if kind not in _NL_INTENT_KINDS:
        kind = "note"
    try:
        cand, enqueue_outcome = await candidate_store.enqueue_if_new_with_outcome(
            user_id=user_id,
            kind=kind,
            key=content[:60],
            value=content,
            reason=f"AI 从对话中识别（置信度 {conf:.2f}）",
            source_turn_id=turn_id,
        )
    except Exception as e:
        logger.warning("nl_intent.enqueue_failed err=%s", e)
        return None
    logger.info("nl_intent.enqueued user=%s kind=%s conf=%.2f outcome=%s", user_id, kind, conf, enqueue_outcome)
    return NlIntentCapture(candidate=cand, enqueue_outcome=str(enqueue_outcome))


async def _recall(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    from bowhead_core.memory_readiness import memory_storage_ready

    store = getattr(ctx, "memory_notes_store", None)
    user_id = getattr(ctx, "user_id", "") or ""
    query = str(args.get("query") or "").strip()
    canonical = memory_storage_ready()

    if not user_id or (store is None and not canonical):
        return {"found": False, "items": []}

    note = None
    if store is not None:
        try:
            note = await store.get(user_id=user_id)
        except Exception as e:
            logger.warning("recall.read_failed user=%s err=%s", user_id, e)
            if not canonical:
                return {"found": False, "items": []}

    if not canonical and (not note or not note.content):
        return {"found": False, "items": []}

    # Strip the leading bullet glyphs so the Lead reads clean lines; sanitize each
    # (the result is returned as a role="tool" message and fed back to the LLM — the
    # same injection surface the pack escapes for the system prompt).
    raw_lines: list[str] = []
    if canonical:
        # Batch 1 (tech-design §4.2 a2): canonical entries first (with [mem_xxx], supersedes handles),
        # then legacy notes verbatim; until §5 migration finishes, notes contain all existing users' memory.
        # Reading only entries makes their recall **return empty immediately** (user story 4 regressed at once).
        raw_lines.extend(await _recall_canonical(ctx, user_id=user_id))
    content = getattr(note, "content", "") or ""
    raw_lines.extend(ln.lstrip("•📌 ").strip() for ln in content.split("\n") if ln.strip())
    lines = [sanitize_for_injection(ln) for ln in raw_lines if ln]
    if not lines:
        return {"found": False, "items": []}
    if query:
        low_q = query.lower()
        # Tokens ≥2 chars only — a single CJK char ("张") otherwise matches every
        # line containing it. Multi-token queries require ALL tokens (AND), not ANY,
        # so "head to head" doesn't match a line that merely contains "to".
        toks = [t for t in low_q.split() if len(t) >= 2]

        def _hit(line: str) -> bool:
            low = line.lower()
            if low_q in low:  # whole-query substring (covers spaceless CJK)
                return True
            return bool(toks) and all(t in low for t in toks)

        matched = [ln for ln in lines if _hit(ln)]
    else:
        matched = lines

    return {"found": bool(matched), "items": matched, "query": query or None}


def register_memory_tools(registry: ToolRegistry) -> None:
    """Register `remember` + `recall`. Called from the gateway right after
    ``build_default_registry()`` (mirrors ``register_clarification_tool`` etc.).

    Idempotent (``registry.register`` overwrites by name). The tools become
    callable by the Lead via ``lead.yaml`` ``tools.allow`` →
    ``PROFILES['lead'].tool_whitelist`` (agent_runner_adapter.manifest_to_profile).
    """
    from bowhead_core.memory_readiness import memory_storage_ready

    # Read gate once at **registration**: tool schemas are a static LLM contract and must not change shape per turn.
    # With gate off, both `if canonical` blocks below are false ⇒ description/input_schema byte-identical to today.
    canonical = memory_storage_ready()
    remember_description = (
        "把用户的一条持久信息存入长期记忆（跨会话保留）。"
        "**仅当**用户明确要你记住、或陈述一条会长期生效的偏好/规则/事实/称谓时调用"
        "（如『以后 head to head 都对比同环比』『我的预算口径是…』『我老板叫张总』）。"
        "kind: rule(规则)/preference(偏好)/fact(事实)/relationship(称谓)/note(备注)；"
        "text: 用一句话写清要记住什么，用用户的原意。"
        "存到用户个人记忆，用户可在「设置 › 记忆」中查看/修改（对用户提起位置时就这样说）。"
        "key、mem_xxx id 等是内部标识，只用于工具调用，不要写进给用户的回答。"
        "一次性、带具体日期的事（如『下周三去北京』）不要存，那属于定时任务。"
        "**只有在返回 ok=True 后**才能对用户说『已记住』，并引用返回的 stored 字段确认存了什么。"
    )
    remember_properties: dict[str, Any] = {
        "kind": {
            "type": "string",
            "enum": ["rule", "preference", "fact", "relationship", "note"],
            "description": "记住内容的类型",
        },
        "text": {
            "type": "string",
            "description": "要记住的内容，一句话，≤500 字",
        },
    }
    remember_required = ["kind", "text"]
    if canonical:
        # Memory redesign batch 1 (tech-design §4.2 a/a3/d-2): add three write-contract slots.
        # `supersedes` is central: model has long been asked to express replacement, but previously could write only reason,
        # **free text no code parsed** (net margin proposed 5 times as a result).
        remember_description += (
            "key: 主题标识（英文 snake_case，如 kpi_money_unit / profit_metric）。"
            "**同一个主题必须复用同一个 key**——system prompt 的 <remembered_about_user> 里"
            "『· 』后面等号左边的就是现有 key，改口径时照抄那个 key，别新起一个。"
            "memory_type: procedural（呈现/格式：单位、图表类型、排版）"
            "或 semantic（口径/定义：指标定义、必含板块、业务术语）。"
            "supersedes: 当这条要**取代**已有记忆时，填那条的 id"
            "（<remembered_about_user> 里方括号中的 mem_xxx，可多条）；"
            "旧条会进回收站可恢复，不会硬删。"
        )
        remember_properties["key"] = {
            "type": "string",
            "description": "主题标识，英文 snake_case；同一主题复用已有 key",
        }
        remember_properties["memory_type"] = {
            "type": "string",
            "enum": ["procedural", "semantic"],
            "description": "procedural=呈现/格式；semantic=口径/定义",
        }
        remember_properties["supersedes"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "被这条取代的旧记忆 id（mem_xxx），没有就不填",
        }
        remember_required = ["kind", "key", "text", "memory_type"]

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="remember",
                description=remember_description,
                input_schema={
                    "type": "object",
                    "properties": remember_properties,
                    "required": remember_required,
                },
            ),
            handler=_remember,
        )
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="recall",
                description=(
                    "查你为当前用户记住的长期记忆。query 可选：给一个主题词只返回相关条目，"
                    "留空返回全部。注意 system prompt 顶部 <remembered_about_user> 通常已含已记住项，"
                    "多数情况直接用即可，只在需要确认细节或怀疑遗漏时才调本工具。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "可选主题词过滤；留空返回全部已记住项",
                        },
                    },
                },
            ),
            handler=_recall,
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
