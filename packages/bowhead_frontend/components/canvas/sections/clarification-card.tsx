"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  cancelClarificationDirective,
  resolveClarification,
  MAX_DIRECTIVE_CHARS,
  MAX_EDITED_SUB_DESC_CHARS,
  MAX_EDITED_SUB_TITLE_CHARS,
  MAX_EDITED_SUBS_COUNT,
} from "@/lib/api";
import { ErrorNotice } from "@/components/error-notice";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { ruleClarificationErrorPresentation } from "@/lib/rule-clarification-error";
import { FormulaConfirmCard } from "./formula-confirm-card";
import { CharIcon, CharIconText } from "@/components/char-icon";

/** ClarificationCard
 *
 * V2.4 (kind=token): single-choice candidates resolved for the current task.
 * V2.7 P1 (kind=options): multiselect chips; Lead lists N analysis directions.
 * V2.7 P2 (kind=task_plan): multiselect + drag reorder; user chooses subtasks to spawn.
 *
 * Backend resolve route chooses side effects by kind:
 *   - token: resolve a choice for the current task
 *   - options/task_plan: resolve the selected choices
 */

export interface ClarifyCandidate {
  value: string;
  label: string;
  hint?: string;
  confidence?: string;
}

export type ClarificationKind =
  | "token"
  | "options"
  | "task_plan"
  // Retired 2026-10-16; old persisted cards still replay as plain choices.
  | "business_rule_review"
  | "ambiguous_turn"
  | "formula_confirm"
  | "concept_resolution"
  | "metric_formula";

export interface ClarificationCardProps {
  workspaceId: string;
  turnId?: string;
  requestId?: string;
  token: string;
  candidates: ClarifyCandidate[];
  rationale?: string;
  /** V2.7: kind selects UI (single-choice / multiselect / drag reorder); defaults to token. */
  kind?: ClarificationKind;
  /** V2.7: multiselect mode. options/task_plan default true. */
  multiSelect?: boolean;
  onChosen?: (chosen: { values: string[] }) => void;
}

export function ClarificationCard({
  workspaceId,
  turnId,
  requestId,
  token,
  candidates,
  rationale,
  kind = "token",
  multiSelect,
  onChosen,
}: ClarificationCardProps) {
  const t = useTranslations("clarification");
  const tErrors = useTranslations("errors");
  const tReasons = useTranslations("error_messages.reasons");
  // Sprint θ Phase 8d Bug 4a fix: ambiguous_turn also multiselect, allowing users to choose
  // multiple concurrent analysis directions. Formerly single-choice (P4.E original), but production users asked
  // for "几个方向都看看". Still supports explicit multiSelect prop override.
  const isMulti = multiSelect ?? (kind !== "token");
  const canSkip =
    kind === "token" ||
    kind === "options" ||
    kind === "task_plan" ||
    kind === "ambiguous_turn";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  // hitl-fix Phase D (F1+F6): remember last attempted values so error state
  // can render a "Retry" button. Without this, FE silent-fails on 404/network
  // errors with only a small `.err` text (prod evidence on 0.24.8 t_441f3758).
  const [lastAttempt, setLastAttempt] = useState<string[] | null>(null);
  const [hidden, setHidden] = useState(false);
  // Multiselect retains order (task_plan order-sensitive); single-choice chosen holds one value
  const [picked, setPicked] = useState<string[]>([]);
  const [chosenAck, setChosenAck] = useState<string[] | null>(null);
  const [skipAck, setSkipAck] = useState(false);

  // M158 #10 P1 (2026-04-29): editable HITL — task_plan kind only.
  // editedSubs initially maps original candidates to EditedSubtask; users edit title/description / add / delete.
  // userDirective: long free-form instruction, input collapsed by default.
  type EditedSubtask = { title: string; description: string };
  const [editedSubs, setEditedSubs] = useState<EditedSubtask[]>(
    () => kind === "task_plan"
      ? candidates.map((c) => ({ title: c.label, description: c.hint || "" }))
      : [],
  );
  // Sprint θ Phase 8d Bug 4c fix: "其他" custom input for ambiguous_turn —
  // user can type a free-form direction not covered by candidates.
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState("");
  // CTO review #13: discoverability — tooltip guides first visit to collapsed textarea.
  // After user opens once, localStorage flag suppresses further nudges.
  const [directiveOpen, setDirectiveOpen] = useState(false);
  const [userDirective, setUserDirective] = useState("");
  const [showDirectiveTip, setShowDirectiveTip] = useState(() => {
    if (typeof window === "undefined") return false;
    return kind === "task_plan"
      && !window.localStorage.getItem("bowhead.p1_directive_tooltip_seen");
  });
  // CTO review #5: revoke-directive button after acknowledgement.
  const [cancelling, setCancelling] = useState(false);
  const [cancelled, setCancelled] = useState(false);

  const projectError = (
    failure: unknown,
    {
      generic = t("submit_failed"),
      retryAction = false,
    }: {
      generic?: string;
      retryAction?: boolean;
    } = {},
  ) =>
    ruleClarificationErrorPresentation(failure, {
      surface: "chat",
      retryAction,
      copy: {
        generic,
        entitlementDenied: tReasons("entitlement_denied"),
        transport: {
          requestTimedOut: tErrors("transport.requestTimedOut"),
          serviceUnreachable: tErrors("transport.serviceUnreachable"),
          retry: tErrors("transport.retry"),
        },
      },
    });

  if (hidden) return null;

  if (!candidates || candidates.length === 0) return null;

  // δ-#3: formula_confirm uses dedicated FormulaConfirmCard (proposed + editable),
  // not chip-list / multiselect UI. candidates[0].value is LLM-proposed formula.
  // δ-#8.5 fix-11: payload carries kind (prevent caller treating free-form formula as candidate
  // value in labelOf lookup); onChosen passes [directive] only as traffic signal on this path.
  if (kind === "formula_confirm") {
    return (
      <FormulaConfirmCard
        workspaceId={workspaceId}
        turnId={turnId}
        requestId={requestId}
        token={token}
        rationale={rationale}
        proposedFormula={candidates[0]?.value ?? ""}
        onConfirmed={(payload) => {
          // payload = {kind: "formula_confirm", directive: string}
          onChosen?.({ values: [payload.directive] });
        }}
      />
    );
  }

  const togglePick = (val: string) => {
    setPicked((prev) =>
      prev.includes(val) ? prev.filter((v) => v !== val) : [...prev, val],
    );
  };

  const movePick = (val: string, dir: -1 | 1) => {
    setPicked((prev) => {
      const i = prev.indexOf(val);
      if (i < 0) return prev;
      const j = i + dir;
      if (j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  // M158 #10 P1: editable task_plan rows
  const updateSub = (i: number, field: "title" | "description", value: string) => {
    setEditedSubs((prev) =>
      prev.map((s, j) => (j === i ? { ...s, [field]: value } : s)),
    );
  };
  const addSub = () => {
    setEditedSubs((prev) => [...prev, { title: "", description: "" }]);
  };
  const deleteSub = (i: number) => {
    setEditedSubs((prev) => prev.filter((_, j) => j !== i));
  };
  const moveSub = (i: number, dir: -1 | 1) => {
    setEditedSubs((prev) => {
      const j = i + dir;
      if (j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  const submit = async (values: string[]) => {
    if (busy) return;
    if (values.length === 0) return;
    setBusy(true);
    setError(null);
    setLastAttempt(values);  // hitl-fix Phase D F1: remember for retry
    try {
      if (requestId) {
        // M158 #10 P1: send edited_subs + user_directive only for task_plan kind.
        // CTO review #8: editsChanged uses deep equality (position-sensitive title+description), preventing add-empty
        // task then delete from falsely sending unnecessary edited_subs when final state is unchanged.
        const isTaskPlan = kind === "task_plan";
        const directiveTrimmed = userDirective.trim();
        const validEdits = isTaskPlan
          ? editedSubs
              .map((s) => ({ title: s.title.trim(), description: s.description.trim() }))
              .filter((s) => s.title.length > 0)
          : [];
        const origSeq = isTaskPlan
          ? candidates.map((c) => ({
              title: (c.label || "").trim(),
              description: (c.hint || "").trim(),
            }))
          : [];
        const editsChanged = isTaskPlan && (
          validEdits.length !== origSeq.length
          || validEdits.some((s, i) =>
            s.title !== origSeq[i]?.title || s.description !== origSeq[i]?.description,
          )
        );
        await resolveClarification(requestId, {
          chosen_value: values[0],
          chosen_values: values,
          ...(isTaskPlan && editsChanged ? {
            edited_subs: validEdits.map((s) => ({
              title: s.title.slice(0, MAX_EDITED_SUB_TITLE_CHARS),
              description: s.description.slice(0, MAX_EDITED_SUB_DESC_CHARS),
            })),
          } : {}),
          ...(isTaskPlan && directiveTrimmed ? {
            user_directive: directiveTrimmed.slice(0, MAX_DIRECTIVE_CHARS),
          } : {}),
        });
      }
      setChosenAck(values);
      setSkipAck(false);
      onChosen?.({ values });
      setTimeout(() => setHidden(true), 4000);
    } catch (e) {
      setError(projectError(e, { retryAction: true }));
    } finally {
      setBusy(false);
    }
  };

  const submitSkip = async () => {
    if (busy || !canSkip) return;
    setBusy(true);
    setError(null);
    setLastAttempt(null);
    try {
      if (requestId) {
        await resolveClarification(requestId, {
          skip: true,
        });
      }
      setChosenAck([]);
      setSkipAck(true);
      onChosen?.({ values: [] });
      setTimeout(() => setHidden(true), 4000);
    } catch (e) {
      setError(projectError(e));
    } finally {
      setBusy(false);
    }
  };

  const labelOf = (val: string) =>
    candidates.find((c) => c.value === val)?.label ?? val;

  const headerTitle =
    kind === "token"
      ? <>{t("title.token_prefix")}<strong>{token}</strong>{t("title.token_suffix")}</>
      : kind === "options"
      ? <>{t("title.options")} <span className="muted">{t("title.options_hint")}</span></>
      : kind === "task_plan"
      ? <>{t("title.task_plan")} <span className="muted">{t("title.task_plan_hint")}</span></>
      : <>{t("title.options")} <span className="muted">{t("title.options_hint")}</span></>;

  return (
    <aside className="v2-clarification" data-token={token} data-kind={kind}>
      <header>
        <span className="icon" aria-hidden>?</span>
        <div className="meta">
          <div className="title">{headerTitle}</div>
          {rationale && <div className="rationale">{rationale}</div>}
          {/* W3-5: Datarc workspace context — show which Datarc workspace this clarification affects */}
          {workspaceId && (
            <div
              className="workspace-context"
              style={{
                fontSize: "0.6875rem",
                color: "var(--c-ink-3)",
                marginTop: 4,
                fontFamily: "var(--font-v2-mono, ui-monospace, monospace)",
              }}
            >
              workspace: <code>{workspaceId}</code>
            </div>
          )}
        </div>
      </header>

      {chosenAck ? (
        <div className="ack">
          <CharIconText ch="✓">
            {skipAck
              ? t("ack.skipped")
              : chosenAck.length === 1
                ? t("ack.confirmed_one", { label: labelOf(chosenAck[0]) })
                : t("ack.confirmed_many", { count: chosenAck.length })}
          </CharIconText>
          {/* CTO review #5: resolved task_plan injects into next turn within 30min;
              user can revoke anytime; retain revoked state without further injection. */}
          {kind === "task_plan" && requestId && !skipAck && !cancelled && (
            <button
              type="button"
              className="ack-cancel-directive"
              disabled={cancelling}
              onClick={async () => {
                if (cancelling) return;
                setCancelling(true);
                try {
                  const r = await cancelClarificationDirective(requestId);
                  if (r.cancelled) setCancelled(true);
                } catch (e) {
                  setError(projectError(e));
                }
                finally { setCancelling(false); }
              }}
              title={t("edit.directive_cancel_title")}
            >
              {cancelling ? t("btn.submitting") : t("edit.directive_cancel")}
            </button>
          )}
          {kind === "task_plan" && cancelled && (
            <span className="ack-cancelled"><CharIconText ch="✕">{t("edit.directive_cancelled")}</CharIconText></span>
          )}
        </div>
      ) : isMulti && kind === "task_plan" ? (
        // M158 #10 P1 (2026-04-29): task_plan uses editable rows + directive textarea
        // (User C strongly wants control). Do not use old chip ul.
        // Row: title input + description input + delete + move up/down. On submit, chosen_values selects
        // all editedSubs.length (run all by default); send edited_subs only when user changes content.
        <>
          <ul className="options options-multi options-edit">
            {editedSubs.map((s, i) => (
              <li key={i} className="edit-row">
                <span className="rank">#{i + 1}</span>
                <div className="edit-fields">
                  <input
                    type="text"
                    className="edit-title"
                    value={s.title}
                    placeholder={t("edit.title_placeholder")}
                    onChange={(e) => updateSub(i, "title", e.target.value)}
                    disabled={busy}
                    maxLength={MAX_EDITED_SUB_TITLE_CHARS}
                  />
                  <input
                    type="text"
                    className="edit-desc"
                    value={s.description}
                    placeholder={t("edit.description_placeholder")}
                    onChange={(e) => updateSub(i, "description", e.target.value)}
                    disabled={busy}
                    maxLength={MAX_EDITED_SUB_DESC_CHARS}
                  />
                </div>
                <span className="reorder">
                  <button
                    type="button"
                    className="reord-btn"
                    onClick={() => moveSub(i, -1)}
                    disabled={busy || i === 0}
                    aria-label={t("reorder.up")}
                  ><CharIcon ch="↑" aria-hidden /></button>
                  <button
                    type="button"
                    className="reord-btn"
                    onClick={() => moveSub(i, 1)}
                    disabled={busy || i === editedSubs.length - 1}
                    aria-label={t("reorder.down")}
                  ><CharIcon ch="↓" aria-hidden /></button>
                  <button
                    type="button"
                    className="reord-btn"
                    onClick={() => deleteSub(i)}
                    disabled={busy || editedSubs.length <= 1}
                    aria-label={t("edit.delete_task")}
                    title={t("edit.delete_task")}
                  ><CharIcon ch="✕" aria-hidden /></button>
                </span>
              </li>
            ))}
            <li className="add-row">
              <button
                type="button"
                className="add-btn"
                onClick={addSub}
                disabled={busy || editedSubs.length >= MAX_EDITED_SUBS_COUNT}
              >
                {t("edit.add_task")}
              </button>
            </li>
          </ul>

          {/* P1 directive textarea: collapsed by default; "+ 加备注 (高级)" expands */}
          <div className="directive-block">
            <button
              type="button"
              className="directive-toggle"
              onClick={() => {
                setDirectiveOpen((o) => !o);
                if (showDirectiveTip) {
                  setShowDirectiveTip(false);
                  try {
                    window.localStorage.setItem("bowhead.p1_directive_tooltip_seen", "1");
                  } catch { /* Ignore disabled localStorage */ }
                }
              }}
              disabled={busy}
            >
              {directiveOpen
                ? t("edit.directive_toggle_close")
                : t("edit.directive_toggle_open")}
            </button>
            {showDirectiveTip && !directiveOpen && (
              <span className="directive-tooltip">
                <CharIconText ch="💡">{t("edit.directive_first_tip")}</CharIconText>
              </span>
            )}
            {directiveOpen && (
              <>
                <textarea
                  className="directive-textarea"
                  value={userDirective}
                  onChange={(e) => setUserDirective(e.target.value.slice(0, MAX_DIRECTIVE_CHARS))}
                  placeholder={t("edit.directive_placeholder")}
                  maxLength={MAX_DIRECTIVE_CHARS}
                  rows={4}
                  disabled={busy}
                />
                <div className="directive-meta">
                  <span className="counter">
                    {t("edit.directive_counter", { count: userDirective.length })}
                  </span>
                  <span className="hint">{t("edit.directive_hint")}</span>
                </div>
              </>
            )}
          </div>

          {/* task_plan submit: chosen_values = all N (backend uses chosen_values on old path;
              edited_subs/directive use new P1 path to inject next-turn force_spawn) */}
          <div className="actions">
            <button
              type="button"
              className="primary"
              disabled={
                busy
                || editedSubs.length === 0
                || editedSubs.every((s) => !s.title.trim())
              }
              onClick={() => {
                const values = editedSubs
                  .map((_, i) => String(i))
                  .filter((_, i) => editedSubs[i].title.trim().length > 0);
                submit(values);
              }}
            >
              {busy
                ? t("btn.submitting")
                : t("btn.confirm_n", { count: editedSubs.filter((s) => s.title.trim()).length })}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={submitSkip}
              title={t("btn.skip_title")}
            >
              {t("btn.skip")}
            </button>
          </div>
        </>
      ) : isMulti ? (
        <>
          <ul className="options options-multi">
            {candidates.map((c) => {
              const idx = picked.indexOf(c.value);
              const isOn = idx >= 0;
              return (
                <li key={c.value} className={isOn ? "on" : ""}>
                  <button
                    type="button"
                    className="opt"
                    disabled={busy}
                    onClick={() => togglePick(c.value)}
                  >
                    <span className="check" aria-hidden>{isOn ? <CharIcon ch="✓" aria-hidden /> : ""}</span>
                    <span className="lbl">{c.label}</span>
                    {c.hint && <span className="hint">{c.hint}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
          {/* Sprint θ Phase 8d Bug 4c: "其他/自定义" custom input for ambiguous_turn —
              user types free-form direction; on submit it's added to picked list. */}
          {kind === "ambiguous_turn" && !customOpen && (
            <button
              type="button"
              className="secondary"
              style={{ marginTop: 8 }}
              onClick={() => setCustomOpen(true)}
              disabled={busy}
            >
              {t("btn.custom_input") /* "其他 / 自定义" */}
            </button>
          )}
          {kind === "ambiguous_turn" && customOpen && (
            <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
              <input
                type="text"
                className="custom-input"
                value={customText}
                placeholder={t("custom.placeholder")}
                onChange={(e) => setCustomText(e.target.value.slice(0, 200))}
                disabled={busy}
                style={{ flex: 1 }}
                aria-label={t("custom_answer_aria", { question: token })}
              />
              <button
                type="button"
                className="secondary"
                disabled={busy || !customText.trim()}
                onClick={() => {
                  const t2 = customText.trim();
                  if (t2) submit([t2]);
                }}
              >
                {t("btn.send_custom")}
              </button>
            </div>
          )}
          <div className="actions">
            <button
              type="button"
              className="primary"
              disabled={busy || picked.length === 0}
              onClick={() => submit(picked)}
            >
              {busy ? t("btn.submitting") : t("btn.confirm_n", { count: picked.length })}
            </button>
            {canSkip && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={submitSkip}
                title={t("btn.skip_title")}
              >
                {t("btn.skip")}
              </button>
            )}
            {/* Sprint θ Phase 8d Bug 4b + 4d: reject-all for ambiguous_turn uses backend skip resolve. */}
            {kind === "ambiguous_turn" && (
              <button
                type="button"
                className="secondary v2-clarification-reject"
                disabled={busy}
                onClick={() => void submitSkip()}
                title={t("btn.reject_all_title")}
              >
                {t("btn.reject_all")}
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <ul className="options">
            {candidates.map((c) => (
              <li key={c.value}>
                <button
                  type="button"
                  className="opt"
                  disabled={busy}
                  onClick={() => submit([c.value])}
                >
                  <span className="lbl">{c.label}</span>
                  {c.confidence && (
                    <span className={`conf conf-${c.confidence}`}>
                      {c.confidence}
                    </span>
                  )}
                  {c.hint && <span className="hint">{c.hint}</span>}
                </button>
              </li>
            ))}
          </ul>
          <div className="actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={submitSkip}
              title={t("btn.skip_title")}
            >
              {t("btn.skip")}
            </button>
          </div>
        </>
      )}

      {error && (
        <ErrorNotice
          message={error.message}
          action={error.action}
          diagnostics={error.diagnostics}
          surface="chat"
          onAction={
            lastAttempt && lastAttempt.length > 0
              ? () => submit(lastAttempt)
              : undefined
          }
          diagnosticsEnabled
        />
      )}
    </aside>
  );
}
