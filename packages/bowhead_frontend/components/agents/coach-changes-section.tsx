"use client";

/**
 * "Coach's changes" (S7, C3): the fields Coach wrote into this draft, each with Undo, Reapply or View
 * conflict, plus Undo all. Undo only restores what Coach wrote; when the Owner also changed a field the row
 * shows the three values and changes nothing until the Owner chooses.
 */
import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { draftChangeSection, draftFieldLabelKey, type AgentConfigSectionKey } from "@/lib/agent-draft-changes";
import { ApiError } from "@/lib/api-client";
import { changeAgentCoachFields, type AgentDraftChange, type AgentWorkingCopyValue } from "@/lib/agents-api";
import { CardStatus, useCardStatus } from "./agent-ci-shared";

type CoachRow = AgentDraftChange & { coach: NonNullable<AgentDraftChange["coach"]> };

interface OmittedValue { type: string; hash: string; size: number }

function isMissing(value: unknown): boolean {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === 1 && (value as { state?: unknown }).state === "missing");
}

function isOmitted(value: unknown): value is OmittedValue {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) && "hash" in value && "size" in value);
}

function itemLabel(item: unknown): string {
  if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") return String(item);
  if (item && typeof item === "object") {
    const record = item as { name?: unknown; skill_ref?: { name?: unknown } };
    if (typeof record.skill_ref?.name === "string") return record.skill_ref.name;
    if (typeof record.name === "string") return record.name;
  }
  return JSON.stringify(item);
}

export function coachChangeRows(changes: readonly AgentDraftChange[]): CoachRow[] {
  return changes.filter((change): change is CoachRow => change.coach !== undefined);
}

function ValueText({ value }: { value: unknown }) {
  const t = useTranslations("agents_marketplace");
  if (isMissing(value)) return <span className="am-coach-empty">{t("coach_change_value_missing")}</span>;
  if (isOmitted(value)) return <span className="am-coach-empty">{t("coach_change_value_long", { size: value.size })}</span>;
  if (typeof value === "string") return <span className="am-coach-text">{value}</span>;
  if (Array.isArray(value)) return <span>{value.length ? value.map(itemLabel).join(t("version_list_separator")) : t("coach_change_value_missing")}</span>;
  return <span>{JSON.stringify(value)}</span>;
}

/** One line: what Coach's value adds or removes for a list, otherwise Coach's text. */
function RowDiff({ row }: { row: CoachRow }) {
  const t = useTranslations("agents_marketplace");
  const { before, value } = row.coach;
  if (Array.isArray(value) && (Array.isArray(before) || isMissing(before))) {
    const was = new Set((Array.isArray(before) ? before : []).map(itemLabel));
    const now = new Set(value.map(itemLabel));
    const added = [...now].filter((item) => !was.has(item));
    const removed = [...was].filter((item) => !now.has(item));
    return (
      <span className="am-coach-diff">
        {added.map((item) => <ins key={`+${item}`}>{t("coach_change_added", { item })}</ins>)}
        {removed.map((item) => <del key={`-${item}`}>{t("coach_change_removed", { item })}</del>)}
      </span>
    );
  }
  if (row.coach.state === "undone") return <del className="am-coach-diff"><ValueText value={value} /></del>;
  return <ins className="am-coach-diff"><ValueText value={value} /></ins>;
}

export function CoachChangesSection({
  agentId,
  changes,
  workingCopy,
  editorDirty,
  onChanged,
  onOpenSection,
}: {
  agentId: string;
  changes: readonly AgentDraftChange[];
  workingCopy: AgentWorkingCopyValue | null;
  /** Unsaved edits in the editor: Coach's rows wait until they are saved or discarded. */
  editorDirty: boolean;
  onChanged: () => Promise<void>;
  onOpenSection?: (section: AgentConfigSectionKey) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const headingId = useId();
  const rows = coachChangeRows(changes);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useCardStatus();
  const [conflict, setConflict] = useState<CoachRow | null>(null);
  const [confirmRestore, setConfirmRestore] = useState(false);
  if (!rows.length) return null;

  const label = (row: CoachRow) => t(draftFieldLabelKey(row.path) as never);
  const applied = rows.filter((row) => row.coach.state === "applied");
  const conflicted = applied.filter((row) => row.coach.conflict);
  const undone = rows.filter((row) => row.coach.state === "undone");
  const disabledReason = editorDirty ? t("coach_change_save_first") : !workingCopy ? t("version_release_no_draft") : null;

  const run = async (key: string, action: "undo" | "reapply" | "keep", fields: string[], success: string) => {
    if (!workingCopy || busy) return;
    setBusy(key);
    setStatus(null);
    try {
      await changeAgentCoachFields(agentId, action, fields, workingCopy.row_version);
      setStatus({ tone: "success", message: success });
      await onChanged();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "agent_working_copy_version_conflict") {
        setStatus({ tone: "error", message: t("coach_change_stale") });
        await onChanged();
      } else if (cause instanceof ApiError && cause.code === "agent_change_undo_conflict") {
        // Someone changed a field after this list loaded; refresh it so the row offers View conflict.
        setStatus({ tone: "error", message: t("coach_change_conflict_refreshed") });
        await onChanged();
      } else {
        setStatus({ tone: "error", message: t("coach_change_failed"), cause });
      }
    } finally {
      setBusy(null);
    }
  };

  // One server write: the value before Coach goes back and the conflict is settled together.
  const restoreBefore = async (row: CoachRow) => {
    if (!workingCopy || busy) return;
    setBusy(`restore:${row.path}`);
    setStatus(null);
    try {
      await changeAgentCoachFields(agentId, "restore", [row.path], workingCopy.row_version);
      setConflict(null);
      setConfirmRestore(false);
      setStatus({ tone: "success", message: t("coach_change_restored", { field: label(row) }) });
      await onChanged();
    } catch (cause) {
      const stale = cause instanceof ApiError && cause.code === "agent_working_copy_version_conflict";
      setStatus({ tone: "error", message: stale ? t("coach_change_stale") : t("coach_change_failed"), cause });
      if (stale) await onChanged();
    } finally {
      setBusy(null);
    }
  };

  const count = applied.length
    ? undone.length
      ? t("coach_changes_count_mixed", { applied: applied.length, undone: undone.length })
      : t("coach_changes_count", { count: applied.length })
    : t("coach_changes_count_undone", { count: undone.length });

  return (
    <section className="am-coach-changes" aria-labelledby={headingId}>
      <header>
        <h3 id={headingId}>{t("coach_changes_title")}</h3>
        <span>{count}{conflicted.length ? `${t("version_list_separator")}${t("coach_changes_conflicted", { count: conflicted.length })}` : null}</span>
        {applied.length > 1 ? (
          <Button
            variant="ghost"
            size="sm"
            disabled={busy !== null || disabledReason !== null || conflicted.length > 0}
            loading={busy === "undo-all"}
            onClick={() => void run("undo-all", "undo", applied.map((row) => row.path), t("coach_change_undone_all", { count: applied.length }))}
          >
            {t("coach_changes_undo_all")}
          </Button>
        ) : null}
      </header>
      {conflicted.length && applied.length > 1 ? (
        <div className="am-ci-ist" data-tone="warning">
          <span className="am-ci-ist-text">
            {t("coach_changes_undo_all_blocked", { fields: conflicted.map(label).join(t("version_list_separator")) })}
          </span>
          {applied.length > conflicted.length ? (
            <span className="am-ci-ist-actions">
              <Button
                size="sm"
                variant="ghost"
                disabled={busy !== null || disabledReason !== null}
                onClick={() => {
                  const others = applied.filter((row) => !row.coach.conflict);
                  void run("undo-others", "undo", others.map((row) => row.path), t("coach_change_undone_all", { count: others.length }));
                }}
              >
                {t("coach_changes_undo_others", { count: applied.length - conflicted.length })}
              </Button>
            </span>
          ) : null}
        </div>
      ) : null}
      <ul>
        {rows.map((row) => {
          const isApplied = row.coach.state === "applied";
          const action = isApplied
            ? row.coach.conflict
              ? { label: t("coach_change_view_conflict"), onClick: () => setConflict(row) }
              : { label: t("coach_change_undo"), onClick: () => void run(`undo:${row.path}`, "undo", [row.path], t("coach_change_undone", { field: label(row) })) }
            : row.coach.conflict
              ? null
              : { label: t("coach_change_reapply"), onClick: () => void run(`reapply:${row.path}`, "reapply", [row.path], t("coach_change_reapplied", { field: label(row) })) };
          return (
            <li key={row.path} data-state={row.coach.state}>
              <span className="am-coach-field">
                {label(row)}
                {isApplied && row.coach.conflict ? <Badge tone="warning">{t("coach_change_badge_conflict")}</Badge> : null}
                {!isApplied ? <Badge tone="neutral">{t("coach_change_badge_undone")}</Badge> : null}
              </span>
              <RowDiff row={row} />
              {action ? (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy !== null || (disabledReason !== null && !row.coach.conflict)}
                  loading={busy === `undo:${row.path}` || busy === `reapply:${row.path}`}
                  onClick={action.onClick}
                >
                  {action.label}
                </Button>
              ) : <small>{t("coach_change_changed_after_undo")}</small>}
            </li>
          );
        })}
      </ul>
      {disabledReason ? <p className="am-ci-note">{disabledReason}</p> : null}
      <CardStatus status={status} />
      {conflict ? (
        <Dialog
          title={t("coach_conflict_title", { field: label(conflict) })}
          onClose={() => { setConflict(null); setConfirmRestore(false); }}
          busy={busy !== null}
          closeLabel={t("cancel")}
          footer={confirmRestore ? (
            <>
              <Button variant="secondary" disabled={busy !== null} onClick={() => setConfirmRestore(false)}>{t("cancel")}</Button>
              <Button variant="primary" loading={busy === `restore:${conflict.path}`} onClick={() => void restoreBefore(conflict)}>{t("coach_conflict_restore_confirm")}</Button>
            </>
          ) : (
            <>
              {onOpenSection ? (
                <Button variant="ghost" onClick={() => { const section = draftChangeSection(conflict.path); setConflict(null); onOpenSection(section); }}>
                  {t("coach_conflict_edit")}
                </Button>
              ) : null}
              <span className="am-ci-sp" />
              <Button
                variant="secondary"
                disabled={busy !== null || disabledReason !== null}
                loading={busy === `keep:${conflict.path}`}
                onClick={() => { const row = conflict; setConflict(null); void run(`keep:${row.path}`, "keep", [row.path], t("coach_change_kept", { field: label(row) })); }}
              >
                {t("coach_conflict_keep")}
              </Button>
              <Button
                variant="secondary"
                disabled={busy !== null || disabledReason !== null}
                onClick={() => setConfirmRestore(true)}
              >
                {t("coach_conflict_restore")}
              </Button>
            </>
          )}
        >
          <p className="am-ci-note">{confirmRestore ? t("coach_conflict_restore_body") : t("coach_conflict_body")}</p>
          <dl className="am-coach-compare">
            <div><dt>{t("coach_conflict_before")}</dt><dd><ValueText value={conflict.coach.before} /></dd></div>
            <div><dt>{t("coach_conflict_coach")}</dt><dd><ValueText value={conflict.coach.value} /></dd></div>
            <div data-current="true"><dt>{t("coach_conflict_current")}</dt><dd><ValueText value={conflict.draft} /></dd></div>
          </dl>
        </Dialog>
      ) : null}
    </section>
  );
}
