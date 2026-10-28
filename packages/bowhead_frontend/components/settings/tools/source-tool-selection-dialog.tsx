"use client";

/**
 * "Choose Tools" for an existing team MCP tool source: re-pick which tools of the latest
 * saved snapshot the source imports (for example after Refresh Tool List added new ones).
 *
 * The list comes from GET /team-tool-sources/{id}/tools; saving reuses PATCH
 * /team-tool-sources/{id} with `selected_op_ids`, so the server's validation stays the only
 * authority. Adding tools saves directly. Removing imported tools while the Impact Guard is on
 * first previews the impact (Agents, running tasks, schedules) inside this dialog and needs an
 * explicit danger confirmation; the save then carries the impact token and one idempotency key
 * per preview. A stale token (409 / 422) re-runs the preview and asks for review again.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle, Search } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { randomUuid } from "@/lib/random-id";
import {
  listSourceTools,
  patchSource,
  previewSourceImpact,
  TeamToolApiError,
  type ResourceChangeImpact,
  type SourceToolCatalog,
  type TeamToolSourcePublic,
} from "@/lib/team-tool-sources-api";

type ErrorKey =
  | "choose_tools_error_load"
  | "choose_tools_error_save"
  | "choose_tools_error_forbidden"
  | "refresh_error_not_found"
  | "impact_unavailable";

type Load =
  | { kind: "loading" }
  | { kind: "error"; error: ErrorKey }
  | { kind: "ready"; catalog: SourceToolCatalog };

type Confirm = { impact: ResourceChangeImpact; idempotencyKey: string; selection: string[] };

/** Show the search box only when scanning the list by eye gets slow. */
const SEARCH_THRESHOLD = 8;

function errorKey(error: unknown, fallback: ErrorKey): ErrorKey {
  if (error instanceof TeamToolApiError) {
    if (error.status === 404) return "refresh_error_not_found";
    if (error.status === 401 || error.status === 403) return "choose_tools_error_forbidden";
    if (error.status === 503) return "impact_unavailable";
  }
  return fallback;
}

/** The confirmed impact no longer matches the source or its dependencies: preview again. */
function isStaleSave(error: unknown): boolean {
  return error instanceof TeamToolApiError && (error.status === 409 || error.status === 422);
}

export interface ToolSelectionSaved {
  imported: number;
  paused_schedule_ids: string[];
}

export function SourceToolSelectionDialog({
  source,
  highlight = [],
  onClose,
  onSaved,
  returnFocusRef,
}: {
  source: TeamToolSourcePublic;
  /** Tool names to mark as new (for example the tools a refresh just added). */
  highlight?: string[];
  onClose: () => void;
  onSaved: (result: ToolSelectionSaved) => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("settings.tools");
  const tCommon = useTranslations("common");
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ErrorKey | null>(null);
  const [staleNotice, setStaleNotice] = useState(false);
  const alive = useRef(true);
  // Tool names are server-defined, so element ids use the row index instead.
  const idPrefix = useId();
  const highlighted = useMemo(() => new Set(highlight), [highlight]);

  const loadCatalog = useCallback(async () => {
    setLoad({ kind: "loading" });
    try {
      const catalog = await listSourceTools(source.id);
      if (!alive.current) return;
      setSelected(new Set(catalog.items.filter((item) => item.imported).map((item) => item.operation_id)));
      setLoad({ kind: "ready", catalog });
    } catch (error) {
      if (alive.current) setLoad({ kind: "error", error: errorKey(error, "choose_tools_error_load") });
    }
  }, [source.id]);

  useEffect(() => {
    alive.current = true;
    void loadCatalog();
    return () => {
      alive.current = false;
    };
  }, [loadCatalog]);

  const catalog = load.kind === "ready" ? load.catalog : null;
  const initial = useMemo(
    () => new Set((catalog?.items ?? []).filter((item) => item.imported).map((item) => item.operation_id)),
    [catalog],
  );
  // Keep the snapshot's order so the saved selection reads like the server's listing.
  const selection = useMemo(
    () => (catalog?.items ?? []).map((item) => item.operation_id).filter((id) => selected.has(id)),
    [catalog, selected],
  );
  const removed = useMemo(() => [...initial].filter((id) => !selected.has(id)), [initial, selected]);
  const added = selection.filter((id) => !initial.has(id));
  const unchanged = removed.length === 0 && added.length === 0;
  const normalizedQuery = query.trim().toLowerCase();
  const visible = (catalog?.items ?? []).filter((item) =>
    !normalizedQuery
    || item.operation_id.toLowerCase().includes(normalizedQuery)
    || item.summary.toLowerCase().includes(normalizedQuery));

  const toggle = (id: string) => {
    setSaveError(null);
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const previewImpact = async (nextSelection: string[]): Promise<boolean> => {
    const impact = await previewSourceImpact(source.id, {
      action: "patch",
      proposed_change: { selected_op_ids: nextSelection },
    });
    if (!alive.current) return false;
    if (impact.blocking || !impact.impact_token) {
      setSaveError("impact_unavailable");
      setConfirm(null);
      return false;
    }
    setConfirm({ impact, idempotencyKey: randomUuid(), selection: nextSelection });
    return true;
  };

  const save = async () => {
    if (!catalog || selection.length === 0 || unchanged) return;
    setSaving(true);
    setSaveError(null);
    setStaleNotice(false);
    try {
      if (removed.length > 0 && source.impact_guard_enabled) {
        await previewImpact(selection);
        return;
      }
      const result = await patchSource(source.id, { selected_op_ids: selection });
      if (alive.current) onSaved({ imported: result.op_count, paused_schedule_ids: [] });
    } catch (error) {
      if (alive.current) setSaveError(errorKey(error, "choose_tools_error_save"));
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const confirmSave = async () => {
    const token = confirm?.impact.impact_token;
    if (!confirm || !token) return;
    setSaving(true);
    setSaveError(null);
    setStaleNotice(false);
    try {
      const result = await patchSource(
        source.id,
        { selected_op_ids: confirm.selection },
        { impactToken: token, idempotencyKey: confirm.idempotencyKey },
      );
      if (alive.current) onSaved({ imported: result.op_count, paused_schedule_ids: result.paused_schedule_ids ?? [] });
    } catch (error) {
      if (!alive.current) return;
      if (isStaleSave(error)) {
        try {
          if (await previewImpact(confirm.selection)) setStaleNotice(true);
        } catch (previewError) {
          setConfirm(null);
          setSaveError(errorKey(previewError, "choose_tools_error_save"));
        }
      } else {
        // Keep the same idempotency key so a retry after a lost response is not applied twice.
        setSaveError(errorKey(error, "choose_tools_error_save"));
      }
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const footer = (
    <>
      {confirm ? (
        <Button onClick={() => { setConfirm(null); setStaleNotice(false); setSaveError(null); }} disabled={saving} data-dialog-initial-focus="">
          {t("choose_tools_back")}
        </Button>
      ) : (
        <Button onClick={onClose} disabled={saving} data-dialog-initial-focus="">
          {t("cancel")}
        </Button>
      )}
      {load.kind === "error" ? (
        <Button variant="primary" onClick={() => void loadCatalog()}>
          {tCommon("retry")}
        </Button>
      ) : confirm ? (
        <Button variant="danger" onClick={() => void confirmSave()} disabled={saving}>
          {saving ? t("choose_tools_saving") : t("choose_tools_confirm")}
        </Button>
      ) : (
        <Button
          variant="primary"
          onClick={() => void save()}
          disabled={!catalog || saving || unchanged || selection.length === 0}
        >
          {saving ? t("choose_tools_saving") : t("save")}
        </Button>
      )}
    </>
  );

  return (
    <Dialog
      title={t("choose_tools_title", { name: source.name })}
      onClose={onClose}
      busy={saving}
      width={600}
      returnFocusRef={returnFocusRef}
      footer={footer}
    >
      {staleNotice ? <Notice tone="info" className="legacy-tools-dialog-error">{t("impact_changed")}</Notice> : null}
      {saveError ? <Notice tone="error" className="legacy-tools-dialog-error">{t(saveError)}</Notice> : null}
      {load.kind === "loading" ? (
        <p role="status" className="legacy-tools-refresh-loading">{t("choose_tools_loading")}</p>
      ) : load.kind === "error" ? (
        <Notice tone="error">{t(load.error)}</Notice>
      ) : confirm ? (
        <section className="legacy-tools-refresh-section is-warning" aria-labelledby="tool-choose-removed">
          <h3 id="tool-choose-removed" className="legacy-tools-refresh-heading">
            <AlertTriangle size={13} aria-hidden="true" /> {t("choose_tools_removed_heading", { n: removed.length })}
          </h3>
          <ul className="legacy-tools-refresh-list">
            {removed.map((name) => (
              <li key={name} translate="no" className="legacy-ai-tools-op is-removed">{name}</li>
            ))}
          </ul>
          <p className="legacy-tools-refresh-note">{t("choose_tools_removed_note")}</p>
          <div className="legacy-tools-impact" data-testid="tool-choose-impact">
            <div className="legacy-tools-impact-grid">
              {[
                t("impact_current_agents", { n: confirm.impact.summary.current_agent_count }),
                t("impact_draft_agents", { n: confirm.impact.summary.draft_agent_count }),
                t("impact_running_tasks", { n: confirm.impact.summary.running_task_count }),
                t("impact_schedules", { n: confirm.impact.summary.scheduled_binding_count }),
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
              {confirm.impact.required_follow_up.length > 0
                ? `${t("refresh_schedules_paused", { n: confirm.impact.required_follow_up.length })} `
                : ""}
              {t("impact_runtime_note")}
            </div>
          </div>
        </section>
      ) : (
        <div className="legacy-tools-refresh">
          <p className="legacy-tools-dialog-lead">{t("choose_tools_lead")}</p>
          {load.catalog.items.length > SEARCH_THRESHOLD ? (
            <div className="legacy-tools-choose-search">
              <Search size={14} aria-hidden="true" />
              <input
                type="search"
                name="tool_selection_search"
                autoComplete="off"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("choose_tools_search")}
                aria-label={t("choose_tools_search")}
                className="ui-input legacy-input-tools"
              />
            </div>
          ) : null}
          <p className="legacy-tools-choose-count" aria-live="polite">
            {t("choose_tools_count", { selected: selection.length, total: load.catalog.items.length })}
          </p>
          <ul className="legacy-tools-choose-list" aria-label={t("choose_tools_list_label")}>
            {visible.map((item, index) => {
              const checked = selected.has(item.operation_id);
              const inputId = `${idPrefix}-tool-${index}`;
              return (
                <li key={item.operation_id} className={`legacy-tools-choose-row${checked ? " is-on" : ""}`}>
                  <input
                    id={inputId}
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(item.operation_id)}
                    disabled={saving}
                    aria-describedby={item.summary ? `${inputId}-desc` : undefined}
                  />
                  <div className="legacy-tools-choose-label">
                    <label htmlFor={inputId} className="legacy-tools-choose-name">
                      <span translate="no">{item.operation_id}</span>
                      {highlighted.has(item.operation_id) ? (
                        <span className="legacy-tools-choose-badge is-new">{t("choose_tools_new_badge")}</span>
                      ) : null}
                      {item.is_write ? (
                        <span className="legacy-tools-choose-badge is-write">
                          {item.approval_executable === true ? t("choose_tools_write_badge") : t("choose_tools_write_unavailable_badge")}
                        </span>
                      ) : null}
                    </label>
                    {item.summary ? (
                      <span id={`${inputId}-desc`} className="legacy-tools-choose-desc">{item.summary}</span>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
          {visible.length === 0 ? <p className="legacy-tools-refresh-note">{t("choose_tools_no_match")}</p> : null}
          {selection.length === 0 ? (
            <p className="legacy-tools-refresh-note is-warn">{t("choose_tools_empty_note")}</p>
          ) : removed.length > 0 && source.impact_guard_enabled ? (
            <p className="legacy-tools-refresh-note">{t("choose_tools_review_note", { n: removed.length })}</p>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
