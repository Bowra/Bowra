"use client";

/** AiFillPanel — configuration-copilot Smart fill (verbatim Claude Design screens 01/02/03/05/06).
 *
 * At connected form top: description + capability explanation + Generate (btn-sec, always lighter than form Save).
 * Generation fills only, never submits (host onApply maps fields into state); failure/rate limiting changes only
 * one explanation line, manual input always available; at most one clarification round (answer joins description, stateless backend).
 * After generation focus summary, never autoscroll/autosubmit (design motion list).
 */

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import {
  AlertCircle,
  Check,
  Info,
  RefreshCw,
  Sparkles,
  Spinner,
  Wand,
} from "@/components/icons";

import { ApiError } from "@/lib/api-client";
import {
  type ConfigDraftResult,
  createConfigDraft,
} from "@/lib/config-drafts-api";

type PanelPhase = "init" | "busy" | "done" | "clarify" | "fail" | "limit" | "rejected" | "unavailable";

/** Host applyDraft receipt (R4 review P2-6/P2-17): filled/warn = actual admitted count
 *  (kept-protected skipped fields excluded); notes = readable issues mapped to no field
 *  (e.g. unmatched space intent), displayed below summary. void falls back to field_meta counts. */
export interface AiFillApplyReceipt {
  filled: number;
  warn: number;
  notes?: string[];
}

export interface AiFillPanelProps {
  formId: string;
  getCurrentValues: () => Record<string, unknown>;
  /** Successful generation: host fills fields/field_meta into form state (no submission), returns actual admitted receipt. */
  onApply: (result: ConfigDraftResult) => AiFillApplyReceipt | void;
  /** Clear suggestions: host restores AI-filled fields not manually edited. */
  onClear: () => void;
  /** Accept all (host changes ai/warn fields to done). */
  onAcceptAll: () => void;
  /** Generating (host gives unfilled fields pending skeleton). */
  onBusyChange?: (busy: boolean) => void;
  /** Batch 1-d: request extracted raw secrets (only opt-in forms receive them). */
  includeSecrets?: boolean;
  /** Batch 1-d: direct secret side-channel fill; raw value bypasses model, returning from pasted text to this browser. */
  onSecrets?: (hits: NonNullable<ConfigDraftResult["secrets"]>) => void;
  /** Batch 1-d: surface-specific degraded wording (shared “生成没成功” too weak for provider configuration:
   *  often current model unreachable; explain next action clearly). */
  failMessageKey?: string;
}

export function AiFillPanel({ formId, getCurrentValues, onApply, onClear, onAcceptAll, onBusyChange, includeSecrets, onSecrets, failMessageKey }: AiFillPanelProps) {
  // Batch 0.5 P2-13: independent namespace; cross-form primitive cannot hard-bind host i18n namespace.
  const t = useTranslations("settings.config_copilot");
  const [phase, setPhase] = useState<PanelPhase>("init");
  const [text, setText] = useState("");
  const [clarification, setClarification] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [summary, setSummary] = useState<{ filled: number; warn: number } | null>(null);
  const [hostNotes, setHostNotes] = useState<string[]>([]);
  // Batch 1-c: backend redacts description secrets, leaving fields empty; undisclosed this looks like silent copilot incapability.
  const [secretRedacted, setSecretRedacted] = useState(false);
  const sumRef = useRef<HTMLDivElement | null>(null);

  /** Clarification concatenation preserves answer first, yielding text prefix (review P2-4: whole slice silently swallowed answer). */
  const joinAnswer = (base: string, reply: string) =>
    `${base.slice(0, Math.max(0, 4000 - reply.length - 1))}\n${reply}`;

  const generate = async (effectiveText: string) => {
    if (!effectiveText.trim() || phase === "busy") return;
    setPhase("busy");
    onBusyChange?.(true);
    try {
      const result = await createConfigDraft({
        form_id: formId,
        text: effectiveText.trim().slice(0, 4000),
        current_values: getCurrentValues(),
        ...(includeSecrets ? { include_secrets: true } : {}),
      });
      onBusyChange?.(false);
      // Redaction precedes generation, independent of status; clarification/degraded rounds must disclose too.
      setSecretRedacted(result.secret_redacted);
      // Same side channel: return secrets even for clarification/degradation, when users most need direct filling.
      if (result.secrets?.length) onSecrets?.(result.secrets);
      if (result.status === "clarification" && result.clarification) {
        setClarification(result.clarification);
        setAnswer("");
        setPhase("clarify");
        return;
      }
      if (result.status !== "ok") {
        setPhase("fail");
        return;
      }
      setClarification(result.clarification);
      // R4 review P2-6: summary uses actual host admit count (excluding kept skips); absent receipt falls back to metadata.
      const receipt = onApply(result) ?? undefined;
      const metas = Object.entries(result.field_meta).filter(([, m]) => m.origin === "generated");
      setSummary(
        receipt
          ? { filled: receipt.filled, warn: receipt.warn }
          : { filled: metas.length, warn: metas.filter(([, m]) => m.needs_confirm).length },
      );
      setHostNotes(receipt?.notes ?? []);
      setPhase("done");
      // Focus summary (no scrolling or unrelated focus theft).
      requestAnimationFrame(() => sumRef.current?.focus({ preventScroll: true }));
    } catch (error) {
      onBusyChange?.(false);
      if (error instanceof ApiError) {
        if (error.status === 429) { setPhase("limit"); return; }
        if (error.status === 422) { setPhase("rejected"); return; }
        if (error.status === 409) { setPhase("unavailable"); return; }
      }
      setPhase("fail");
    }
  };

  const clear = () => {
    onClear();
    setSummary(null);
    setHostNotes([]);
    setClarification(null);
    setSecretRedacted(false);
    setPhase("init");
  };

  const busy = phase === "busy";
  const generateLabel = phase === "done" || phase === "fail" ? t("regenerate") : t("generate");

  let caption: React.ReactNode = (
    <span className="afp-cap"><span className="ic"><Info size={13} /></span>{t("cap")}</span>
  );
  if (busy) caption = <span className="afp-cap"><span className="ic spin"><Spinner size={13} /></span>{t("generating")}</span>;
  if (phase === "fail") caption = <span className="afp-cap err"><span className="ic"><AlertCircle size={13} /></span>{t(failMessageKey ?? "fail")}</span>;
  if (phase === "rejected") caption = <span className="afp-cap err"><span className="ic"><AlertCircle size={13} /></span>{t("rejected")}</span>;
  if (phase === "unavailable") caption = <span className="afp-cap err"><span className="ic"><AlertCircle size={13} /></span>{t("unavailable")}</span>;
  if (phase === "limit") caption = <span className="afp-cap"><span className="ic"><Info size={13} /></span>{t("limit")}</span>;

  return (
    <div className="afp" data-testid="ai-fill-panel">
      <div className="afp-hd">
        <span className="ic"><Wand size={14} /></span>
        <span className="t">{t("title")}</span>
        <span className="sp" />
      </div>
      <div className="ctl">
        <textarea
          value={text}
          rows={2}
          maxLength={4000}
          placeholder={t("placeholder")}
          onChange={(e) => setText(e.target.value)}
          style={{
            fontSize: "0.8125rem", color: "var(--c-ink)", background: "var(--c-surface)",
            border: "1px solid var(--c-line)", borderRadius: 8, padding: "10px 12px",
            width: "100%", display: "block", resize: "none", lineHeight: 1.6, minHeight: 62,
          }}
        />
      </div>

      {phase === "clarify" && clarification ? (
        <div className="afp-ask" data-testid="afp-ask">
          <div className="q-hd"><span className="ic"><AlertCircle size={14} /></span>{t("ask_title")}</div>
          <div className="q">{clarification}</div>
          <div className="row">
            <input
              value={answer}
              maxLength={1000}
              placeholder={t("ask_placeholder")}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && answer.trim()) void generate(joinAnswer(text, answer)); }}
              style={{
                fontSize: "0.8125rem", color: "var(--c-ink)", background: "var(--c-surface)",
                border: "1px solid var(--c-line)", borderRadius: 8, padding: "10px 12px", display: "block",
              }}
            />
            <button type="button" className="btn-sec" disabled={!answer.trim()} onClick={() => void generate(joinAnswer(text, answer))}>
              {t("ask_go")}
            </button>
          </div>
        </div>
      ) : null}

      {phase === "done" && clarification ? (
        <div className="afp-ask" data-testid="afp-ask">
          <div className="q-hd"><span className="ic"><AlertCircle size={14} /></span>{t("ask_title")}</div>
          <div className="q">{clarification}</div>
          <div className="row">
            <input
              value={answer}
              maxLength={1000}
              placeholder={t("ask_placeholder")}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && answer.trim()) void generate(joinAnswer(text, answer)); }}
              style={{
                fontSize: "0.8125rem", color: "var(--c-ink)", background: "var(--c-surface)",
                border: "1px solid var(--c-line)", borderRadius: 8, padding: "10px 12px", display: "block",
              }}
            />
            <button type="button" className="btn-sec" disabled={!answer.trim()} onClick={() => void generate(joinAnswer(text, answer))}>
              {t("ask_go")}
            </button>
          </div>
        </div>
      ) : null}

      {phase === "done" && summary ? (
        <div className="afp-sum" ref={sumRef} tabIndex={-1} data-testid="afp-sum">
          <span className="t">
            {t("sum_filled", { n: summary.filled })}
            {summary.warn > 0
              ? <span className="w">{t("sum_confirm", { m: summary.warn })}</span>
              : <b>{t("sum_confirm", { m: 0 })}</b>}
          </span>
          <button type="button" className="mini acc" onClick={onAcceptAll}><Check size={11} />{t("accept_all")}</button>
          <button type="button" className="mini" onClick={clear}>{t("clear")}</button>
        </div>
      ) : null}

      {secretRedacted && phase !== "init" && phase !== "busy" ? (
        // User-facing rule 4: secrets never entered model, so fields empty; not failure, require manual input.
        <div data-testid="afp-secret-note">
          <span className="afp-cap"><span className="ic"><Info size={13} /></span>{t("secret_redacted")}</span>
        </div>
      ) : null}

      {phase === "done" && hostNotes.length > 0 ? (
        // R4 review P2-17: issues mapped to no field (e.g. unmatched space intent) no longer silently discarded.
        <div data-testid="afp-notes">
          {hostNotes.map((note) => (
            <span className="afp-cap" key={note}><span className="ic"><Info size={13} /></span>{note}</span>
          ))}
        </div>
      ) : null}

      <div className="afp-foot">
        {caption}
        {phase === "clarify" ? (
          <button type="button" className="btn-ghost2" onClick={clear}>{t("clear")}</button>
        ) : (
          <button
            type="button"
            className="btn-sec"
            disabled={busy || phase === "limit" || phase === "unavailable" || !text.trim()}
            onClick={() => void generate(text)}
            data-testid="afp-generate"
          >
            {busy
              ? <span className="spin"><Spinner size={14} /></span>
              : phase === "done" || phase === "fail"
                ? <RefreshCw size={14} />
                : <Sparkles size={14} />}
            {generateLabel}
          </button>
        )}
      </div>
    </div>
  );
}
