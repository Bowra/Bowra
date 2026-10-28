"use client";

/**
 * "Refresh Tool List" for a team MCP tool source: preview the server's current tool list
 * against the saved one, then apply exactly that previewed candidate.
 *
 * Opening the dialog runs the preview (loading → diff / no changes / error). The diff also lists
 * tools whose description or input schema changed; when the gateway reports that list empty, the
 * dialog says the tool list is up to date instead of hedging about possible edits. The dialog
 * itself is the Impact Guard confirmation: when imported tools disappear it shows the
 * affected Agents, running tasks and schedules, and the confirm button turns red. Apply
 * sends the short-lived impact token with one idempotency key per preview, so retrying
 * after a lost response cannot apply twice. A stale or invalid token (409 / 422) re-runs
 * the preview and asks for review again instead of failing.
 */

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { randomUuid } from "@/lib/random-id";
import {
  applyToolSourceRefresh,
  previewToolSourceRefresh,
  type ToolSourceChangedTool,
  type ToolSourceRefreshPreview,
  type ToolSourceRefreshResult,
} from "@/lib/team-tool-source-refresh-api";
import { TeamToolApiError, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";

type ErrorKey =
  | "refresh_error_unreachable"
  | "refresh_error_impact_unavailable"
  | "refresh_error_too_large"
  | "refresh_error_not_found"
  | "refresh_error_forbidden"
  | "refresh_error_generic"
  | "refresh_apply_error_generic";

type Phase =
  | { kind: "loading" }
  | { kind: "error"; error: ErrorKey }
  | { kind: "ready"; preview: ToolSourceRefreshPreview; idempotencyKey: string };

/** Map a failed request to user-facing copy; status codes and error codes never reach the UI. */
function refreshErrorKey(error: unknown, stage: "preview" | "apply"): ErrorKey {
  if (error instanceof TeamToolApiError) {
    if (error.status === 502) return "refresh_error_unreachable";
    if (error.status === 503) return "refresh_error_impact_unavailable";
    if (error.status === 413) return "refresh_error_too_large";
    if (error.status === 404) return "refresh_error_not_found";
    if (error.status === 401 || error.status === 403) return "refresh_error_forbidden";
  }
  return stage === "apply" ? "refresh_apply_error_generic" : "refresh_error_generic";
}

/** The candidate no longer matches the source or its dependencies: preview again. */
function isStaleApply(error: unknown): boolean {
  return error instanceof TeamToolApiError && (error.status === 409 || error.status === 422);
}

function NameList({ names, tone }: { names: string[]; tone?: "removed" }) {
  return (
    <ul className="legacy-tools-refresh-list">
      {names.map((name) => (
        <li key={name} translate="no" className={`legacy-ai-tools-op${tone === "removed" ? " is-removed" : ""}`}>
          {name}
        </li>
      ))}
    </ul>
  );
}

function ChangedList({ tools }: { tools: ToolSourceChangedTool[] }) {
  const t = useTranslations("settings.tools");
  return (
    <ul className="legacy-tools-refresh-changed">
      {tools.map((tool) => (
        <li key={tool.name} className="legacy-tools-refresh-changed-row">
          <span translate="no" className="legacy-ai-tools-op">{tool.name}</span>
          <span className="legacy-tools-refresh-changed-meta">
            {[
              ...tool.fields.map((field) => t(field === "description" ? "refresh_changed_description" : "refresh_changed_parameters")),
              tool.imported ? t("refresh_changed_imported") : t("refresh_changed_not_imported"),
            ].join(" · ")}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RefreshDiff({ source, preview }: { source: TeamToolSourcePublic; preview: ToolSourceRefreshPreview }) {
  const t = useTranslations("settings.tools");
  const dropped = new Set(preview.selected_dropped);
  const removedOnly = preview.removed.filter((name) => !dropped.has(name));
  // [] is the legacy "every tool" selection: new tools are exposed with the source.
  const importsEverything = source.selected_op_ids.length === 0;
  const disablesSource = !importsEverything
    && preview.selected_dropped.length > 0
    && source.selected_op_ids.every((name) => dropped.has(name));
  // `changed` is absent only from gateways that predate it; then edits to existing tools are unknown.
  const changed = preview.changed ?? null;
  const hasToolChanges = preview.added.length > 0 || preview.removed.length > 0 || (changed?.length ?? 0) > 0;
  const hasPromptChanges = preview.prompts_added.length > 0 || preview.prompts_removed.length > 0;
  const { summary } = preview;
  const scheduleCount = preview.required_follow_up.length;

  if (!hasToolChanges && !hasPromptChanges) {
    return (
      <div className="legacy-tools-refresh">
        <p className="legacy-tools-refresh-empty">{changed ? t("refresh_up_to_date") : t("refresh_no_changes")}</p>
        <p className="legacy-tools-dialog-note">
          {changed
            ? t("refresh_up_to_date_note", { n: preview.tools_count })
            : t("refresh_no_changes_note", { n: preview.tools_count })}
        </p>
      </div>
    );
  }

  return (
    <div className="legacy-tools-refresh">
      <p className="legacy-tools-dialog-lead">{t("refresh_lead", { n: preview.tools_count })}</p>

      {preview.selected_dropped.length > 0 ? (
        <section className="legacy-tools-refresh-section is-warning" aria-labelledby="tool-refresh-dropped">
          <h3 id="tool-refresh-dropped" className="legacy-tools-refresh-heading">
            <AlertTriangle size={13} aria-hidden="true" /> {t("refresh_dropped_heading", { n: preview.selected_dropped.length })}
          </h3>
          <NameList names={preview.selected_dropped} tone="removed" />
          <p className="legacy-tools-refresh-note">
            {disablesSource ? t("refresh_source_disabled_note") : t("refresh_dropped_note")}
          </p>
          <div className="legacy-tools-impact" data-testid="tool-refresh-impact">
            <div className="legacy-tools-impact-grid">
              {[
                t("impact_current_agents", { n: summary.current_agent_count }),
                t("impact_draft_agents", { n: summary.draft_agent_count }),
                t("impact_running_tasks", { n: summary.running_task_count }),
                t("impact_schedules", { n: summary.scheduled_binding_count }),
              ].map((label, index) => (
                <div
                  key={label}
                  className={`legacy-tools-impact-cell${index % 2 ? " is-col2" : ""}${index >= 2 ? " is-row2" : ""}`}
                >
                  {label}
                </div>
              ))}
            </div>
            <div className="legacy-tools-impact-note">
              {scheduleCount > 0 ? `${t("refresh_schedules_paused", { n: scheduleCount })} ` : ""}
              {t("impact_runtime_note")}
            </div>
          </div>
        </section>
      ) : null}

      {preview.added.length > 0 ? (
        <section className="legacy-tools-refresh-section" aria-labelledby="tool-refresh-added">
          <h3 id="tool-refresh-added" className="legacy-tools-refresh-heading">
            {t("refresh_added_heading", { n: preview.added.length })}
          </h3>
          <NameList names={preview.added} />
          <p className="legacy-tools-refresh-note">
            {importsEverything ? t("refresh_added_note_all") : t("refresh_added_note_selected")}
          </p>
        </section>
      ) : null}

      {changed && changed.length > 0 ? (
        <section className="legacy-tools-refresh-section" aria-labelledby="tool-refresh-changed">
          <h3 id="tool-refresh-changed" className="legacy-tools-refresh-heading">
            {t("refresh_changed_heading", { n: changed.length })}
          </h3>
          <ChangedList tools={changed} />
          <p className="legacy-tools-refresh-note">{t("refresh_changed_note")}</p>
        </section>
      ) : null}

      {removedOnly.length > 0 ? (
        <section className="legacy-tools-refresh-section" aria-labelledby="tool-refresh-removed">
          <h3 id="tool-refresh-removed" className="legacy-tools-refresh-heading">
            {t("refresh_removed_heading", { n: removedOnly.length })}
          </h3>
          <NameList names={removedOnly} tone="removed" />
          <p className="legacy-tools-refresh-note">{t("refresh_removed_note")}</p>
        </section>
      ) : null}

      {hasPromptChanges ? (
        <section className="legacy-tools-refresh-section" aria-labelledby="tool-refresh-prompts">
          <h3 id="tool-refresh-prompts" className="legacy-tools-refresh-heading">
            {t("refresh_prompts_heading")}
          </h3>
          {preview.prompts_added.length > 0 ? (
            <>
              <p className="legacy-tools-refresh-note">{t("refresh_prompts_added", { n: preview.prompts_added.length })}</p>
              <NameList names={preview.prompts_added} />
            </>
          ) : null}
          {preview.prompts_removed.length > 0 ? (
            <>
              <p className="legacy-tools-refresh-note">{t("refresh_prompts_removed", { n: preview.prompts_removed.length })}</p>
              <NameList names={preview.prompts_removed} tone="removed" />
            </>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

export function SourceRefreshDialog({
  source,
  onClose,
  onApplied,
  returnFocusRef,
}: {
  source: TeamToolSourcePublic;
  onClose: () => void;
  onApplied: (result: ToolSourceRefreshResult) => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("settings.tools");
  const tCommon = useTranslations("common");
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<ErrorKey | null>(null);
  const [staleNotice, setStaleNotice] = useState(false);
  const alive = useRef(true);
  // Only the latest preview may populate the dialog: the server keeps one candidate per
  // source and actor, so an older response would carry a replaced digest.
  const previewSeq = useRef(0);

  const runPreview = useCallback(async () => {
    const seq = ++previewSeq.current;
    setPhase({ kind: "loading" });
    setApplyError(null);
    try {
      const preview = await previewToolSourceRefresh(source.id);
      if (alive.current && seq === previewSeq.current) {
        setPhase({ kind: "ready", preview, idempotencyKey: randomUuid() });
      }
    } catch (error) {
      if (alive.current && seq === previewSeq.current) {
        setStaleNotice(false);
        setPhase({ kind: "error", error: refreshErrorKey(error, "preview") });
      }
    }
  }, [source.id]);

  useEffect(() => {
    alive.current = true;
    void runPreview();
    return () => {
      alive.current = false;
    };
  }, [runPreview]);

  const apply = async () => {
    if (phase.kind !== "ready") return;
    setApplying(true);
    setApplyError(null);
    setStaleNotice(false);
    try {
      const result = await applyToolSourceRefresh(source.id, {
        candidateDigest: phase.preview.candidate_digest,
        impactToken: phase.preview.impact_token,
        idempotencyKey: phase.idempotencyKey,
      });
      if (alive.current) onApplied(result);
    } catch (error) {
      if (!alive.current) return;
      if (isStaleApply(error)) {
        setStaleNotice(true);
        await runPreview();
      } else {
        // Keep the same idempotency key so a retry after a lost response is not applied twice.
        setApplyError(refreshErrorKey(error, "apply"));
      }
    } finally {
      if (alive.current) setApplying(false);
    }
  };

  const ready = phase.kind === "ready" ? phase.preview : null;
  const dropsImportedTools = Boolean(ready && ready.selected_dropped.length > 0);

  return (
    <Dialog
      title={t("refresh_title", { name: source.name })}
      onClose={onClose}
      busy={applying}
      width={560}
      returnFocusRef={returnFocusRef}
      footer={(
        <>
          <Button onClick={onClose} disabled={applying} data-dialog-initial-focus="">
            {t("cancel")}
          </Button>
          {phase.kind === "error" ? (
            <Button variant="primary" onClick={() => { setStaleNotice(false); void runPreview(); }}>
              {tCommon("retry")}
            </Button>
          ) : (
            <Button
              variant={dropsImportedTools ? "danger" : "primary"}
              onClick={() => void apply()}
              disabled={phase.kind !== "ready" || applying}
            >
              {applying ? t("refresh_applying") : t("refresh_apply")}
            </Button>
          )}
        </>
      )}
    >
      {staleNotice ? <Notice tone="info" className="legacy-tools-dialog-error">{t("refresh_stale")}</Notice> : null}
      {applyError ? <Notice tone="error" className="legacy-tools-dialog-error">{t(applyError)}</Notice> : null}
      {phase.kind === "loading" ? (
        <p role="status" className="legacy-tools-refresh-loading">{t("refresh_loading")}</p>
      ) : phase.kind === "error" ? (
        <Notice tone="error">{t(phase.error)}</Notice>
      ) : (
        <RefreshDiff source={source} preview={phase.preview} />
      )}
    </Dialog>
  );
}
