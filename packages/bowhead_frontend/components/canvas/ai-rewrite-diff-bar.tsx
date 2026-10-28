/**
 * AIRewriteDiffBar: rewrite progress/result bar (design D2Regenerating / D2Result).
 *
 * Selection-level and block-level (BlockAIBar) rewrites share this result pipeline:
 *   - state.active           → rc-regen-chip (spinner + Stop)
 *   - state.reviewing → rc-result-bar (Accept / Revert); design has no Retry
 *   - state.unverified       → amber "未确认来源" badge (PRD AC7 + value-drift)
 *
 * feedback-0713 #8: fixed at report-area center (matching BlockAIBar).
 * Previously floating-ui anchored to rewritten text + flip() moved above paragraphs near viewport bottom,
 * with a different anchor from BlockAIBar (block versus coordsAtPos), shifting dialog position within one operation;
 * reporter decided to restore fixed centering.
 */

"use client";

import { FloatingPortal } from "@floating-ui/react";
import type { Editor } from "@tiptap/react";
import { useTranslations } from "next-intl";
import { useId } from "react";
import type { useAIRewrite } from "@/lib/use-ai-rewrite";
import { Spark, Check } from "@/components/icons";
import { useReportCenterStyle } from "./use-report-center";
import { ErrorDiagnosticsInline } from "@/components/error-notice";

type RewriteHandle = ReturnType<typeof useAIRewrite>;

export function AIRewriteDiffBar({
  rewrite,
  editor,
}: {
  rewrite: RewriteHandle;
  editor: Editor | null;
}) {
  const t = useTranslations("ai_edit");
  const { state, cancel, accept, reject, clearError, retry } = rewrite;
  const open = state.active || state.reviewing || !!state.error;
  const resultMessageId = useId();

  // feedback-0713 #8: fixed at report-area center (same positioning as BlockAIBar)
  const centerStyle = useReportCenterStyle(editor, open);

  if (!open) return null;

  return (
    <FloatingPortal>
      <div style={{ ...centerStyle, zIndex: 999 }}>
        {state.active ? (
          <div className="rc-regen-chip" role="status">
            <span className="rc-spinner" aria-hidden />
            <span>{t("rewriting")}</span>
            <button type="button" onClick={cancel} className="rc-pill-btn ghost legacy-rc-pill--compact">
              {t("stop")}
            </button>
          </div>
        ) : state.error ? (
          <div className="rc-result-bar error legacy-rc-result-bar">
            <Spark width={13} height={13} className="legacy-rc-result-spark legacy-rc-result-spark--error" />
            {/* Announce only what happened and what to do; status codes/request IDs live outside the announcement in "复制诊断". */}
            <div className="rc-result-error-message" role="alert">
              <strong>{t("error_title")}</strong>
              <span>{t(`error_${state.error}`)}</span>
            </div>
            <ErrorDiagnosticsInline error={state.errorCause} surface="task" />
            {/* Retry the failed rewrite unchanged (same preset, instruction and original passage). */}
            {state.error !== "content_changed" && state.error !== "invalid_request" && (
              <button type="button" onClick={retry} className="rc-pill-btn ghost" data-testid="ai-rewrite-retry">
                {t("retry")}
              </button>
            )}
            <button type="button" onClick={clearError} className="rc-pill-btn ghost">
              {t("close")}
            </button>
          </div>
        ) : (
          // Nonmodal editor result bar lets users read rewritten body and accept/revert, without taking focus or blocking background,
          // so use a named action group rather than a dialog.
          <div className="rc-result-bar legacy-rc-result-bar" role="group" aria-labelledby={resultMessageId}>
            <Spark width={13} height={13} className="legacy-rc-result-spark" />
            {/* Feishu 07.06 #15a: expose the unverified explanation (previously hidden in hover title,
                leaving users confused by "未确认来源"). Body explicitly explains pending-number checks and why. */}
            <span id={resultMessageId} className="legacy-rc-grow">
              {state.unverified ? t("unverified_hint") : t("done")}
            </span>
            {state.unverified && (
              <span className="rc-badge amber" title={t("unverified_title")}>
                {t("unverified_badge")}
              </span>
            )}
            <button type="button" onClick={accept} className="rc-pill-btn accept">
              <Check width={11} height={11} /> {t("accept")}
            </button>
            <button type="button" onClick={reject} className="rc-pill-btn ghost">
              {t("revert")}
            </button>
          </div>
        )}
      </div>
    </FloatingPortal>
  );
}
