"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { ErrorNotice } from "@/components/error-notice";
import { resolveClarification, MAX_DIRECTIVE_CHARS } from "@/lib/api";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { ruleClarificationErrorPresentation } from "@/lib/rule-clarification-error";
import { CharIconText } from "@/components/char-icon";

/** FormulaConfirmCard (δ-#3)
 *
 * When Confidence Gate < threshold, judge LLM proposes a formula through HITL for user editing/confirmation.
 *
 * UI:
 *   - rationale: "我准备用 EBITDA = 营收-COGS, 你们公司这样吗?"
 *   - proposed (read-only): LLM-proposed version
 *   - editable textarea: user can edit
 *   - Confirm button: resolve(chosen_value=proposed, user_directive=edited)
 *
 * Backend resolve applies _sanitize_directive(kind="formula_confirm") for a second filter
 * (block __import__ / exec( / <script>, etc.); after passing, auto_learner writes the directive into
 * RegisteredFact (type formula_definition, source=user_directive, is_user_corrected=true).
 *
 * δ-#8.5 fix-11: edit state persists in sessionStorage keyed by requestId so
 * remount (drawer toggle / SSE replay / route change) doesn't lose user edits.
 * "Use proposed" disabled when proposed empty. onConfirmed payload carries
 * `kind: "formula_confirm"` so callers can disambiguate from candidate-value semantics.
 *
 * MVP omits react-diff-view (~80KB): separate proposed/edited boxes are clear enough; add it in later polish.
 */

const _SS_PREFIX = "bowhead.formula_confirm.edit:";

function _readPersisted(requestId: string | undefined, fallback: string): string {
  if (!requestId || typeof window === "undefined") return fallback;
  try {
    const v = window.sessionStorage.getItem(_SS_PREFIX + requestId);
    return v != null ? v : fallback;
  } catch {
    return fallback;
  }
}

function _writePersisted(requestId: string | undefined, value: string): void {
  if (!requestId || typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(_SS_PREFIX + requestId, value);
  } catch {
    /* sessionStorage off / quota; lose edit on remount, not catastrophic */
  }
}

function _clearPersisted(requestId: string | undefined): void {
  if (!requestId || typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(_SS_PREFIX + requestId);
  } catch {
    /* ignore */
  }
}

export interface FormulaConfirmResolvedPayload {
  /** Discriminator so callers can branch instead of treating as candidate value. */
  kind: "formula_confirm";
  /** Free-form formula the user submitted (proposed or edited). */
  directive: string;
}

export interface FormulaConfirmCardProps {
  workspaceId: string;
  turnId?: string;
  requestId?: string;
  /** Token = fact key displayed in Chinese (e.g. "EBITDA"). */
  token: string;
  /** "I plan to use X; does your company use it this way?" */
  rationale?: string;
  /** LLM-proposed formula, taken from candidates[0].value. */
  proposedFormula: string;
  onConfirmed?: (resolved: FormulaConfirmResolvedPayload) => void;
}

export function FormulaConfirmCard({
  workspaceId,
  turnId: _turnId,
  requestId,
  token,
  rationale,
  proposedFormula,
  onConfirmed,
}: FormulaConfirmCardProps) {
  const t = useTranslations("clarification");
  const tErrors = useTranslations("errors");
  const tReasons = useTranslations("error_messages.reasons");
  const [edited, setEdited] = useState(() => _readPersisted(requestId, proposedFormula));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const [lastAttempt, setLastAttempt] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);

  const projectError = (value: unknown) =>
    ruleClarificationErrorPresentation(value, {
      surface: "chat",
      retryAction: true,
      copy: {
        generic: t("submit_failed"),
        entitlementDenied: tReasons("entitlement_denied"),
        transport: {
          requestTimedOut: tErrors("transport.requestTimedOut"),
          serviceUnreachable: tErrors("transport.serviceUnreachable"),
          retry: tErrors("transport.retry"),
        },
      },
    });

  // Persist edit state on every change so remount doesn't lose user input.
  useEffect(() => {
    _writePersisted(requestId, edited);
  }, [requestId, edited]);

  if (confirmed) {
    return (
      <aside
        className="v2-clarification"
        data-token={token}
        data-kind="formula_confirm"
        role="status"
        aria-live="polite"
      >
        <div className="ack"><CharIconText ch="✓">{t("formula.ack_confirmed")}</CharIconText></div>
      </aside>
    );
  }

  const proposedTrim = proposedFormula.trim();

  const submit = async (directive: string) => {
    if (busy || !requestId) return;
    setBusy(true);
    setError(null);
    setLastAttempt(directive);
    try {
      await resolveClarification(requestId, {
        chosen_value: proposedFormula,
        user_directive: directive.slice(0, MAX_DIRECTIVE_CHARS),
      });
      _clearPersisted(requestId);
      setConfirmed(true);
      onConfirmed?.({ kind: "formula_confirm", directive });
    } catch (e) {
      setError(projectError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="v2-clarification" data-token={token} data-kind="formula_confirm">
      <header>
        <span className="icon" aria-hidden>?</span>
        <div className="meta">
          <div className="title">
            {t("title.formula_confirm")} <strong>{token}</strong>
            <span className="muted"> {t("title.formula_confirm_hint")}</span>
          </div>
          {rationale && <div className="rationale">{rationale}</div>}
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

      <div className="formula-pair">
        <label className="formula-label">{t("formula.proposed_label")}</label>
        <pre className="formula-proposed" aria-readonly>
          {proposedFormula}
        </pre>

        <label className="formula-label" htmlFor={`fc-edit-${requestId ?? "x"}`}>
          {t("formula.edited_label")}
        </label>
        <textarea
          id={`fc-edit-${requestId ?? "x"}`}
          className="formula-edited"
          value={edited}
          onChange={(e) => setEdited(e.target.value.slice(0, MAX_DIRECTIVE_CHARS))}
          placeholder={t("formula.edit_placeholder")}
          maxLength={MAX_DIRECTIVE_CHARS}
          rows={4}
          disabled={busy}
        />
      </div>

      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !edited.trim()}
          onClick={() => submit(edited.trim())}
        >
          {busy ? t("btn.submitting") : t("formula.confirm_btn")}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy || !proposedTrim}
          onClick={() => submit(proposedFormula)}
          title={t("formula.use_proposed_btn")}
        >
          {t("formula.use_proposed_btn")}
        </button>
      </div>

      {error ? (
        <ErrorNotice
          message={error.message}
          action={error.action}
          diagnostics={error.diagnostics}
          surface="chat"
          onAction={
            lastAttempt === null
              ? undefined
              : () => submit(lastAttempt)
          }
          diagnosticsEnabled
        />
      ) : null}
    </aside>
  );
}
