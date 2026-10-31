"use client";


import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { AlertCircle, ChevronDown, ChevronRight, CircleCheck, Copy, History, Info, Lock, RotateCcw } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ListTime } from "@/components/ui/list-time";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { SaveBar } from "@/components/ui/save-bar";
import {
  getSpaceInstructions,
  listSpaceInstructionVersions,
  saveSpaceInstructions,
  type SpaceInstructions as SpaceInstructionsValue,
  type SpaceInstructionsVersion,
} from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { copyTextWithFallback } from "@/lib/clipboard";
import { lineDiff, type DiffLine } from "@/lib/line-diff";
import { useSpace } from "@/lib/use-space";
import { WORKSPACE_INSTRUCTIONS_SAVED_EVENT, type WorkspaceSettingsSavedTarget } from "@/lib/workspace-settings-notifications";

import styles from "./space-instructions.module.css";

// ── Pure helpers ────────────────────────────────────────────────────────────

export type DiffRow = DiffLine | { kind: "gap"; count: number };

/** Line diff where an empty base adds every line (no phantom deletion of one empty line). */
export function textDiff(base: string, next: string): DiffLine[] {
  if (!base) return next ? next.split("\n").map((text) => ({ kind: "add" as const, text })) : [];
  return lineDiff(base, next);
}

/** Changed lines with `context` unchanged lines around them; longer unchanged runs collapse into a gap row. */
export function compactDiff(input: DiffLine[], context = 1): DiffRow[] {
  // A blank separator line added or removed next to real changes reads as an empty "+"/"−" row, so show it as
  // plain context; only a diff made purely of blank lines keeps them marked.
  const substantive = input.some((line) => line.kind !== "ctx" && line.text.trim());
  const lines = substantive ? input.map((line) => (line.kind !== "ctx" && !line.text.trim() ? { kind: "ctx" as const, text: line.text } : line)) : input;
  const keep = lines.map(() => false);
  input.forEach((line, index) => {
    if (line.kind === "ctx") return;
    for (let k = Math.max(0, index - context); k <= Math.min(lines.length - 1, index + context); k += 1) keep[k] = true;
  });
  const rows: DiffRow[] = [];
  let hidden = 0;
  lines.forEach((line, index) => {
    if (keep[index]) {
      if (hidden) rows.push({ kind: "gap", count: hidden });
      hidden = 0;
      rows.push(line);
    } else {
      hidden += 1;
    }
  });
  if (hidden && rows.length) rows.push({ kind: "gap", count: hidden });
  return rows;
}

function lineKey(line: string): string {
  // Ignore list markers so "- New products…" and "New products…" compare equal.
  return Array.from(line.replace(/^[\s\-*•·\d.、)）]+/u, "").trim()).slice(0, 6).join("");
}

/**
 * 1-based number of a current line that starts like one of the added lines, or null. A plain text hint for C5:
 * it never blocks applying.
 */
export function findSimilarLine(current: string, added: string[]): number | null {
  const currentLines = current.split("\n");
  for (const line of added) {
    const key = lineKey(line);
    if (Array.from(key).length < 6) continue;
    const index = currentLines.findIndex((candidate) => lineKey(candidate) === key);
    if (index >= 0) return index + 1;
  }
  return null;
}

/** The current instructions with the adopted wording added after a blank line. */
export function withAppended(current: string, text: string): string {
  const addition = text.trim();
  return current.trim() ? `${current.trim()}\n\n${addition}` : addition;
}

function detailOf(error: unknown): Record<string, unknown> {
  if (!(error instanceof ApiError)) return {};
  const body = error.body as { detail?: unknown } | undefined;
  return body && typeof body.detail === "object" && body.detail !== null ? (body.detail as Record<string, unknown>) : {};
}

// ── Editor state ────────────────────────────────────────────────────────────

type Message = { tone: "neutral" | "danger"; text: string; icon: "info" | "alert" | "lock"; kind?: "unchanged" | "forbidden" };

function useSpaceInstructionsEditor(workspaceId: string) {
  const t = useTranslations("settings.space_instructions");
  const [saved, setSaved] = useState<SpaceInstructionsValue | null>(null);
  const [draft, setDraftValue] = useState("");
  const [versions, setVersions] = useState<SpaceInstructionsVersion[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [restoring, setRestoring] = useState<{ version: number; previousDraft: string } | null>(null);
  /** Another administrator saved `version` while this draft was based on `base`. */
  const [stale, setStale] = useState<{ version: number; base: number } | null>(null);
  const [savedAs, setSavedAs] = useState<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const loadVersions = useCallback(async () => {
    try {
      const items = await listSpaceInstructionVersions(workspaceId);
      if (mounted.current) setVersions(items);
    } catch {
      if (mounted.current) setVersions(null);
    }
  }, [workspaceId]);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const value = await getSpaceInstructions(workspaceId);
      if (!mounted.current) return null;
      setSaved(value);
      setDraftValue(value.content);
      if (value.can_edit) void loadVersions();
      return value;
    } catch (reason) {
      if (mounted.current) {
        setLoadError(reason instanceof ApiError && [403, 404].includes(reason.status) ? t("load_denied") : t("load_failed"));
      }
      return null;
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [workspaceId, loadVersions, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // A suggestion confirmed in chat saves a new version. Show it here unless the open editor holds unsaved
  // work; such a draft keeps its base and goes through the usual conflict prompt on save.
  const idle = useRef(true);
  idle.current = Boolean(saved) && !saving && !restoring && !stale && draft.trim() === saved?.content;
  useEffect(() => {
    const onSaved = (event: Event) => {
      if ((event as CustomEvent<WorkspaceSettingsSavedTarget>).detail?.workspaceId !== workspaceId || !idle.current) return;
      void getSpaceInstructions(workspaceId)
        .then((value) => {
          if (!mounted.current || !idle.current) return;
          setSaved(value);
          setDraftValue(value.content);
          setMessage(null);
          if (value.can_edit) void loadVersions();
        })
        .catch(() => undefined);
    };
    window.addEventListener(WORKSPACE_INSTRUCTIONS_SAVED_EVENT, onSaved);
    return () => window.removeEventListener(WORKSPACE_INSTRUCTIONS_SAVED_EVENT, onSaved);
  }, [workspaceId, loadVersions]);

  // "Unchanged" is not an error: it fades after four seconds or on the next keystroke.
  useEffect(() => {
    if (message?.kind !== "unchanged") return;
    const timer = window.setTimeout(() => setMessage((current) => (current?.kind === "unchanged" ? null : current)), 4000);
    return () => window.clearTimeout(timer);
  }, [message]);

  const setDraft = (value: string) => {
    setDraftValue(value);
    setRestoring(null);
    setSavedAs(null);
    setMessage((current) => (current?.kind === "unchanged" ? null : current));
  };

  const reset = () => {
    if (!saved) return;
    setDraftValue(saved.content);
    setRestoring(null);
    setStale(null);
    setMessage(null);
  };

  const restore = (item: SpaceInstructionsVersion) => {
    setRestoring({ version: item.version, previousDraft: restoring?.previousDraft ?? draft });
    setDraftValue(item.content);
    setSavedAs(null);
    setMessage(null);
  };

  const undoRestore = () => {
    if (!restoring) return;
    setDraftValue(restoring.previousDraft);
    setRestoring(null);
  };

  const save = async (options: { sourceCandidateId?: string } = {}): Promise<SpaceInstructionsValue | null> => {
    if (!saved || saving) return null;
    setSaving(true);
    setMessage(null);
    try {
      const updated = await saveSpaceInstructions(workspaceId, {
        content: draft,
        expected_version: saved.version,
        ...(restoring ? { restored_from: restoring.version } : {}),
        ...(options.sourceCandidateId ? { source_candidate_id: options.sourceCandidateId } : {}),
      });
      if (!mounted.current) return updated;
      setSaved(updated);
      setDraftValue(updated.content);
      setRestoring(null);
      setStale(null);
      setSavedAs(updated.version);
      void loadVersions();
      return updated;
    } catch (reason) {
      if (!mounted.current) return null;
      const detail = detailOf(reason);
      const code = reason instanceof ApiError ? reason.code : undefined;
      if (code === "stale_version") {
        // Keep the draft and move the base to the newer version, so the next save deliberately replaces it.
        try {
          const latest = await getSpaceInstructions(workspaceId);
          if (mounted.current) {
            setStale({ version: latest.version, base: saved.version });
            setSaved(latest);
          }
        } catch {
          // Without the newer text, still move the base to the version the server reported, so "Save my text
          // anyway" targets it instead of hitting the same conflict again.
          const current = Number(detail.current_version ?? saved.version + 1);
          if (mounted.current) {
            setStale({ version: current, base: saved.version });
            setSaved({ ...saved, version: current });
          }
        }
        void loadVersions();
      } else if (code === "unchanged") {
        // After a conflict, the other administrator may have saved exactly this text: nothing is left to resolve.
        setStale(null);
        setRestoring(null);
        setMessage({ tone: "neutral", icon: "info", text: t("error_unchanged"), kind: "unchanged" });
      } else if (code === "too_long") {
        setMessage({ tone: "danger", icon: "alert", text: t("error_too_long", { length: Number(detail.length ?? 0), limit: Number(detail.limit ?? saved.max_chars) }) });
      } else if (code === "workspace_archived") {
        setMessage({ tone: "danger", icon: "lock", text: t("error_archived") });
      } else if (reason instanceof ApiError && reason.status === 403) {
        setMessage({ tone: "danger", icon: "lock", text: t("error_forbidden"), kind: "forbidden" });
      } else {
        setMessage({ tone: "danger", icon: "alert", text: t("save_failed") });
      }
      return null;
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const length = draft.trim().length;
  const maxChars = saved?.max_chars ?? 0;
  return {
    saved,
    draft,
    setDraft,
    versions,
    loading,
    loadError,
    load,
    saving,
    message,
    restoring,
    undoRestore,
    restore,
    stale,
    savedAs,
    save,
    reset,
    length,
    tooLong: Boolean(saved) && length > maxChars,
    dirty: Boolean(saved) && draft.trim() !== saved?.content,
  };
}

type Editor = ReturnType<typeof useSpaceInstructionsEditor>;

// ── Shared pieces ───────────────────────────────────────────────────────────

const ICONS = { info: Info, alert: AlertCircle, lock: Lock };

function InlineNotice({ tone, icon, children, action }: { tone?: "neutral" | "accent" | "danger" | "warning" | "positive"; icon: keyof typeof ICONS | "restore" | "check"; children: ReactNode; action?: ReactNode }) {
  const Icon = icon === "restore" ? RotateCcw : icon === "check" ? CircleCheck : ICONS[icon];
  return (
    <div className={styles.notice} data-tone={tone ?? "neutral"} role={tone === "danger" ? "alert" : "status"}>
      <Icon size={14} aria-hidden />
      <span>{children}</span>
      {action}
    </div>
  );
}

function DiffView({ rows, limit, caption }: { rows: DiffRow[]; limit?: number; caption?: ReactNode }) {
  const t = useTranslations("settings.space_instructions");
  const [all, setAll] = useState(false);
  const visible = !all && limit && rows.length > limit ? rows.slice(0, limit) : rows;
  const rest = rows.slice(visible.length).filter((row) => row.kind === "add" || row.kind === "del").length;
  return (
    <div className={styles.diff}>
      {caption ? <div className={styles.diffCaption}>{caption}</div> : null}
      {visible.map((row, index) =>
        row.kind === "gap" ? (
          <div key={index} className={styles.diffLine} data-kind="note">
            {t("diff_unchanged_lines", { count: row.count })}
          </div>
        ) : (
          <div key={index} className={styles.diffLine} data-kind={row.kind}>
            <span aria-hidden>{row.kind === "add" ? "+" : row.kind === "del" ? "−" : ""}</span>
            <span>
              {row.kind === "add" ? <span className={styles.srOnly}>{t("diff_added")}</span> : row.kind === "del" ? <span className={styles.srOnly}>{t("diff_removed")}</span> : null}
              {row.text || " "}
            </span>
          </div>
        ),
      )}
      {visible.length < rows.length ? (
        <button type="button" className={styles.diffMore} onClick={() => setAll(true)}>
          {t("history_more", { count: rest || rows.length - visible.length })}
        </button>
      ) : null}
    </div>
  );
}

/** Line diff of a suggested Space instructions text, for the Configuration Agent's confirmation card in chat. */
export function SpaceInstructionsDiff({ before, after }: { before: string; after: string }) {
  return <DiffView rows={compactDiff(textDiff(before, after))} limit={40} />;
}

function VersionItem({
  item,
  previous,
  editor,
  open,
  onToggle,
  onRestored,
}: {
  item: SpaceInstructionsVersion;
  previous: SpaceInstructionsVersion | undefined;
  editor: Editor;
  open: boolean;
  onToggle: () => void;
  onRestored?: () => void;
}) {
  const t = useTranslations("settings.space_instructions");
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const bodyId = useId();
  const current = editor.saved?.version === item.version;
  const isNew = editor.stale?.version === item.version;
  const isBase = editor.stale?.base === item.version;
  const picked = editor.restoring?.version === item.version;
  const compareBase = isNew && editor.stale ? editor.versions?.find((version) => version.version === editor.stale?.base) : previous;
  // Without the previous version (beyond the loaded history) there is nothing honest to compare: show the text.
  const comparable = Boolean(compareBase) || item.version === 1;
  const diff = useMemo(() => (open && comparable ? textDiff(compareBase?.content ?? "", item.content) : []), [open, comparable, compareBase, item.content]);
  const rows = useMemo(() => compactDiff(diff), [diff]);
  const added = diff.filter((line) => line.kind === "add").map((line) => line.text);
  const notes = [
    isBase ? t("stale_base") : null,
    item.restored_from !== null ? t("restored_from", { version: item.restored_from }) : null,
    item.source_candidate_id ? t("from_suggestion", { id: item.source_candidate_id }) : null,
  ].filter(Boolean);
  return (
    <li>
      <button type="button" className={styles.versionRow} data-state={isNew ? "new" : picked ? "picked" : undefined} aria-expanded={open} aria-controls={bodyId} onClick={onToggle}>
        <span className={styles.versionName}>
          {t("version_label", { version: item.version })}
          {current && !isNew ? <Badge tone="accent" size="sm">{t("history_current")}</Badge> : null}
          {isNew ? <Badge tone="warning" size="sm">{t("stale_badge")}</Badge> : null}
          {picked ? <Badge tone="accent" size="sm">{t("restoring_badge")}</Badge> : null}
        </span>
        {open ? <ChevronDown size={14} className={styles.versionChevron} aria-hidden /> : <ChevronRight size={14} className={styles.versionChevron} aria-hidden />}
        <span className={styles.versionMeta}>
          {item.created_by_name} · <ListTime value={item.created_at} />
          {notes.map((text) => ` · ${text}`)}
        </span>
        {!open ? <span className={styles.versionPreview}>{item.content ? item.content.split("\n")[0] : t("version_cleared")}</span> : null}
      </button>
      {open ? (
        <div id={bodyId} className={styles.versionBody}>
          {full || !comparable ? (
            <pre className={styles.fullText}>{item.content || t("version_cleared")}</pre>
          ) : (
            <DiffView
              rows={rows}
              limit={8}
              caption={compareBase ? t("history_compare", { version: compareBase.version }) : t("history_first")}
            />
          )}
          <div className={styles.versionActions}>
            {isNew ? (
              added.length ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void copyTextWithFallback(added.join("\n"), t("copy_prompt")).then((ok) => setCopied(ok))}
                >
                  <Copy size={14} aria-hidden />
                  {copied ? t("copied") : t("stale_copy")}
                </Button>
              ) : null
            ) : !current && editor.saved?.can_edit ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  editor.restore(item);
                  onRestored?.();
                }}
              >
                <RotateCcw size={14} aria-hidden />
                {t("restore")}
              </Button>
            ) : null}
            {comparable ? (
              <Button size="sm" variant="ghost" aria-pressed={full} onClick={() => setFull((value) => !value)}>
                {full ? t("history_diff") : t("history_full")}
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function VersionHistory({ editor, onRestored }: { editor: Editor; onRestored?: () => void }) {
  const t = useTranslations("settings.space_instructions");
  const [openVersion, setOpenVersion] = useState<number | null>(null);
  // A conflict opens the other administrator's version, so the change is visible without searching.
  useEffect(() => {
    if (editor.stale) setOpenVersion(editor.stale.version);
  }, [editor.stale]);
  const versions = editor.versions ?? [];
  if (!versions.length) {
    return (
      <div className={styles.empty}>
        <b>{t("history_empty_title")}</b>
        <span>{t("history_empty_body")}</span>
      </div>
    );
  }
  return (
    <ul className={styles.versions} aria-label={t("history_title")}>
      {versions.map((item, index) => (
        <VersionItem
          key={item.version}
          item={item}
          previous={versions[index + 1]}
          editor={editor}
          open={openVersion === item.version}
          onToggle={() => setOpenVersion((value) => (value === item.version ? null : item.version))}
          onRestored={onRestored}
        />
      ))}
    </ul>
  );
}

function HistoryRail({ editor }: { editor: Editor }) {
  const t = useTranslations("settings.space_instructions");
  const count = editor.versions?.length ?? 0;
  return (
    <aside className={styles.rail} aria-label={t("history_title")}>
      <div className={styles.railHead}>
        <History size={14} aria-hidden />
        <span>{t("history_title")}</span>
        {count ? <span>{t("history_count", { count })}</span> : null}
      </div>
      <VersionHistory editor={editor} />
      {count ? <p className={styles.railNote}>{t("restore_hint")}</p> : null}
    </aside>
  );
}

function HistoryDisclosure({ editor, expanded, onClick, className }: { editor: Editor; expanded?: boolean; onClick: () => void; className?: string }) {
  const t = useTranslations("settings.space_instructions");
  const count = editor.versions?.length ?? 0;
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <button type="button" className={`${styles.disclosure}${className ? ` ${className}` : ""}`} aria-expanded={expanded} onClick={onClick}>
      <History size={14} aria-hidden />
      <span>{editor.stale ? t("stale_disclosure", { version: editor.stale.version }) : t("history_title")}</span>
      <span>{count ? t("history_count", { count }) : t("history_empty_title")}</span>
      <Chevron size={14} aria-hidden />
    </button>
  );
}

function EditorNotices({ editor }: { editor: Editor }) {
  const t = useTranslations("settings.space_instructions");
  const saved = editor.saved;
  if (!saved) return null;
  return (
    <>
      {editor.restoring ? (
        <InlineNotice
          tone="accent"
          icon="restore"
          action={
            <Button size="sm" variant="ghost" onClick={editor.undoRestore}>
              {t("restoring_undo")}
            </Button>
          }
        >
          {t("restoring_notice", { version: editor.restoring.version, next: saved.version + 1, current: saved.version })}
        </InlineNotice>
      ) : null}
      {editor.stale ? (
        <InlineNotice tone="danger" icon="alert">
          {t("error_stale", { version: editor.stale.version })}
        </InlineNotice>
      ) : null}
      {editor.message ? (
        <InlineNotice tone={editor.message.tone} icon={editor.message.icon}>
          {editor.message.text}
        </InlineNotice>
      ) : null}
    </>
  );
}

function EditorField({ editor, id }: { editor: Editor; id: string }) {
  const t = useTranslations("settings.space_instructions");
  const saved = editor.saved!;
  const counterId = `${id}-counter`;
  const errorId = `${id}-error`;
  return (
    <div className={styles.editor}>
      <span className="configuration-field-label"><label className={styles.editorHead} htmlFor={id}>
        {t("content_label")}
      </label><ConfigurationFieldHelp contract={saved.field_contract} field="space_instructions.content" label={t("content_label")} /></span>
      <textarea
        id={id}
        className={styles.textarea}
        value={editor.draft}
        placeholder={t("placeholder")}
        aria-invalid={editor.tooLong || undefined}
        aria-describedby={editor.tooLong ? `${errorId} ${counterId}` : counterId}
        onChange={(event) => editor.setDraft(event.target.value)}
      />
      {editor.tooLong ? (
        <div id={errorId} className={styles.fieldError}>
          <AlertCircle size={14} aria-hidden />
          <span>{t("error_too_long", { length: editor.length, limit: saved.max_chars })}</span>
        </div>
      ) : null}
      <VersionLine editor={editor} counterId={counterId} />
    </div>
  );
}

function VersionLine({ editor, counterId, counter = true }: { editor: Editor; counterId?: string; counter?: boolean }) {
  const t = useTranslations("settings.space_instructions");
  const saved = editor.saved!;
  return (
    <div className={styles.counter}>
      <span>
        {saved.version > 0 ? (
          <>
            {t("last_updated", { version: saved.version, name: saved.updated_by_name ?? "—" })} · <ListTime value={saved.updated_at} />
          </>
        ) : (
          t("never_saved")
        )}
      </span>
      {counter ? (
        <span id={counterId} className={editor.tooLong ? styles.over : undefined}>
          {t("counter", { length: editor.length, limit: saved.max_chars })}
        </span>
      ) : null}
    </div>
  );
}

function ReadOnlyBody({ editor }: { editor: Editor }) {
  const t = useTranslations("settings.space_instructions");
  const saved = editor.saved!;
  const names = saved.administrators.join(t("name_separator"));
  return (
    <>
      {saved.archived ? (
        <InlineNotice tone="warning" icon="lock">
          {t("error_archived")}
        </InlineNotice>
      ) : (
        <InlineNotice icon="lock">{names ? t("read_only_names", { names }) : t("read_only")}</InlineNotice>
      )}
      {saved.content ? <p className={styles.readOnly}>{saved.content}</p> : <p className={styles.readOnly}>{t("read_only_empty")}</p>}
      {saved.version > 0 ? <VersionLine editor={editor} counter={false} /> : null}
    </>
  );
}

function LoadState({ editor }: { editor: Editor }) {
  const t = useTranslations("settings.space_instructions");
  if (editor.loading) return <LoadingSkeleton variant="form" rows={3} label={t("loading")} />;
  return (
    <InlineNotice
      tone="danger"
      icon="alert"
      action={
        <Button size="sm" variant="secondary" onClick={() => void editor.load()}>
          {t("retry")}
        </Button>
      }
    >
      {editor.loadError ?? t("load_failed")}
    </InlineNotice>
  );
}

/** Save-bar or dialog-footer copy for the current editor state. */
function useSaveCopy(editor: Editor) {
  const t = useTranslations("settings.space_instructions");
  const tCommon = useTranslations("common");
  const saved = editor.saved;
  const next = (saved?.version ?? 0) + 1;
  if (editor.tooLong && saved) {
    return { message: t("too_long_bar", { count: editor.length - saved.max_chars }), tone: "danger" as const, save: tCommon("save"), discard: tCommon("discard") };
  }
  if (editor.stale) {
    return { message: t("stale_bar", { version: editor.stale.version }), tone: "neutral" as const, save: t("stale_save"), discard: t("stale_discard") };
  }
  if (editor.restoring) {
    return { message: t("restoring_bar", { version: editor.restoring.version }), tone: "neutral" as const, save: t("save_as", { version: next }), discard: tCommon("discard") };
  }
  return { message: t("unsaved"), tone: "neutral" as const, save: tCommon("save"), discard: tCommon("discard") };
}

// ── Space settings › Instructions ───────────────────────────────────────────

/** Space settings › Instructions: one administrator-written text that every conversation in the Space follows. */
export function SpaceInstructions() {
  const t = useTranslations("settings.space_instructions");
  const space = useSpace();
  const workspaceId = space.activeId;
  if (!workspaceId) return <InlineNotice icon="info">{t("no_active_space")}</InlineNotice>;
  return <SpaceInstructionsPage key={workspaceId} workspaceId={workspaceId} />;
}

export function SpaceInstructionsPage({ workspaceId }: { workspaceId: string }) {
  const t = useTranslations("settings.space_instructions");
  const editor = useSpaceInstructionsEditor(workspaceId);
  const copy = useSaveCopy(editor);
  const [historySheet, setHistorySheet] = useState(false);
  const fieldId = useId();
  const saved = editor.saved;
  return (
    <div className={styles.page}>
      <header className="ui-page-head">
        <h1 className="ui-page-title">{t("title")}</h1>
        <p className="ui-page-desc">{t("subtitle")}</p>
      </header>
      {!saved ? (
        <LoadState editor={editor} />
      ) : !saved.can_edit ? (
        <ReadOnlyBody editor={editor} />
      ) : (
        <>
          <div className={styles.split}>
            <div className={styles.editor}>
              <EditorNotices editor={editor} />
              {editor.savedAs !== null && !editor.dirty ? (
                <InlineNotice tone="positive" icon="check">
                  {t("saved_as", { version: editor.savedAs ?? 0 })}
                </InlineNotice>
              ) : null}
              <EditorField editor={editor} id={fieldId} />
              <HistoryDisclosure editor={editor} className={styles.narrowOnly} onClick={() => setHistorySheet(true)} />
            </div>
            <HistoryRail editor={editor} />
          </div>
          <SaveBar
            dirty={editor.dirty || editor.stale !== null}
            saving={editor.saving}
            saveDisabled={editor.tooLong}
            message={copy.tone === "danger" ? <span className={styles.over}>{copy.message}</span> : copy.message}
            saveLabel={copy.save}
            discardLabel={copy.discard}
            onSave={() => void editor.save()}
            onDiscard={editor.reset}
          />
          {historySheet ? (
            <Dialog title={t("history_title")} presentation="sheet" onClose={() => setHistorySheet(false)}>
              <VersionHistory editor={editor} onRestored={() => setHistorySheet(false)} />
              <p className={styles.railNote}>{t("restore_hint")}</p>
            </Dialog>
          ) : null}
        </>
      )}
    </div>
  );
}

// ── Dialog: Team settings › Spaces and the suggestion inbox ────────────────

export interface SpaceInstructionsPrefill {
  text: string;
  candidateId: string;
  proposer: string;
  evidenceCount: number;
}

function PrefillReview({ editor, prefill, reviewed, onReviewed }: { editor: Editor; prefill: SpaceInstructionsPrefill; reviewed: boolean; onReviewed: (value: boolean) => void }) {
  const t = useTranslations("settings.space_instructions");
  const tPrefill = useTranslations("feedback_prefill");
  const saved = editor.saved!;
  const proposed = withAppended(saved.content, prefill.text);
  const diff = useMemo(() => textDiff(saved.content, proposed), [saved.content, proposed]);
  const rows = useMemo(() => compactDiff(diff), [diff]);
  const similar = useMemo(() => {
    const added = diff.filter((line) => line.kind === "add" && line.text.trim()).map((line) => line.text);
    return findSimilarLine(saved.content, added);
  }, [diff, saved.content]);
  return (
    <>
      <InlineNotice tone="warning" icon="alert">
        <b>{tPrefill("banner")}</b>
        <span className={styles.bannerMeta}>{tPrefill("banner_meta", { proposer: prefill.proposer, count: prefill.evidenceCount, candidate: prefill.candidateId })}</span>
      </InlineNotice>
      <section className={styles.review} aria-label={t("review_title")}>
        <div className={styles.reviewHead}>
          <b>{t("review_title")}</b>
          <span>{t("review_versions", { version: saved.version })}</span>
          <span>{tPrefill("diff_legend")}</span>
        </div>
        <DiffView rows={rows} />
        {similar !== null ? (
          <InlineNotice icon="info">{t("similar_line", { line: similar })}</InlineNotice>
        ) : null}
      </section>
      <label className={styles.check}>
        <input type="checkbox" checked={reviewed} onChange={(event) => onReviewed(event.target.checked)} />
        <span>{tPrefill("reviewed")}</span>
      </label>
    </>
  );
}

/**
 * Instructions for one Space in a dialog (a full-height sheet on phones). Save stays inside the footer; closing with
 * unsaved changes turns the footer into a confirmation instead of stacking a second dialog. With `prefill`, the first
 * step reviews the adopted wording as a line diff (the Agent-instructions prefill pattern) before it reaches the editor.
 */
export function SpaceInstructionsDialog({
  workspaceId,
  name,
  prefill,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  name: string;
  prefill?: SpaceInstructionsPrefill;
  onClose: () => void;
  /** Called after a successful save; `fromPrefill` says whether the saved text carried the applied suggestion. */
  onSaved?: (value: SpaceInstructionsValue, saved: { fromPrefill: boolean }) => void;
}) {
  const t = useTranslations("settings.space_instructions");
  const tPrefill = useTranslations("feedback_prefill");
  const tCommon = useTranslations("common");
  const editor = useSpaceInstructionsEditor(workspaceId);
  const copy = useSaveCopy(editor);
  const fieldId = useId();
  const [step, setStep] = useState<"review" | "edit">(prefill ? "review" : "edit");
  const [reviewed, setReviewed] = useState(false);
  const [applied, setApplied] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const saved = editor.saved;
  const dirty = editor.dirty || editor.stale !== null;
  // A conflict opens the history so the other administrator's version is in view.
  useEffect(() => {
    if (editor.stale) setHistoryOpen(true);
  }, [editor.stale]);
  useEffect(() => {
    if (dirty) return;
    setConfirmClose(false);
  }, [dirty]);

  const requestClose = () => {
    if (editor.saving) return;
    if (dirty && step === "edit") setConfirmClose(true);
    else onClose();
  };

  const save = async () => {
    // Claim the suggestion only while the draft still carries its wording (not after Discard, a restore or deleting it).
    const fromPrefill = Boolean(applied && prefill && editor.draft.includes(prefill.text.trim()));
    const value = await editor.save(fromPrefill && prefill ? { sourceCandidateId: prefill.candidateId } : {});
    if (value) {
      setApplied(false);
      onSaved?.(value, { fromPrefill });
    }
  };

  const forbidden = editor.message?.kind === "forbidden";
  let footer: ReactNode = null;
  if (saved?.can_edit && step === "review" && prefill) {
    footer = (
      <div className={styles.footer}>
        <span className={styles.footerStatus}>{t("review_hint")}</span>
        <Button variant="secondary" onClick={() => setStep("edit")}>
          {tPrefill("discard")}
        </Button>
        <Button
          variant="primary"
          disabled={!reviewed}
          onClick={() => {
            editor.setDraft(withAppended(saved.content, prefill.text));
            setApplied(true);
            setStep("edit");
          }}
        >
          {tPrefill("apply")}
        </Button>
      </div>
    );
  } else if (confirmClose) {
    footer = (
      <div className={styles.footer} data-tone="warning">
        <span className={styles.footerStatus}>
          <AlertCircle size={14} aria-hidden />
          {t("close_unsaved")}
        </span>
        <Button variant="secondary" onClick={() => setConfirmClose(false)}>
          {t("keep_editing")}
        </Button>
        <Button variant="danger" onClick={onClose}>
          {t("discard_close")}
        </Button>
      </div>
    );
  } else if (forbidden) {
    const names = saved?.administrators.join(t("name_separator")) ?? "";
    footer = (
      <div className={styles.footer}>
        <span className={styles.footerStatus}>{names ? t("forbidden_footer_names", { names }) : t("forbidden_footer")}</span>
        <Button variant="secondary" onClick={() => void copyTextWithFallback(editor.draft, t("copy_prompt")).then((ok) => setCopied(ok))}>
          <Copy size={14} aria-hidden />
          {copied ? t("copied") : t("copy_text")}
        </Button>
        <Button variant="secondary" onClick={onClose}>
          {t("close")}
        </Button>
      </div>
    );
  } else if (saved?.can_edit) {
    const showSaved = editor.savedAs !== null && !dirty;
    footer = (
      <div className={styles.footer}>
        <span className={styles.footerStatus} data-tone={showSaved ? "positive" : copy.tone} role="status">
          {showSaved ? (
            <>
              <CircleCheck size={14} aria-hidden />
              {t("saved_as", { version: editor.savedAs ?? 0 })}
            </>
          ) : dirty ? (
            copy.message
          ) : null}
        </span>
        <Button variant="secondary" onClick={requestClose}>
          {t("close")}
        </Button>
        <Button variant="primary" disabled={!dirty || editor.tooLong || editor.saving} loading={editor.saving} onClick={() => void save()}>
          {editor.saving ? tCommon("saving") : copy.save}
        </Button>
      </div>
    );
  } else if (saved) {
    footer = (
      <Button variant="secondary" onClick={onClose}>
        {t("close")}
      </Button>
    );
  }

  return (
    <Dialog title={t("dialog_title", { name })} width={720} presentation="sheet" busy={editor.saving} onClose={requestClose} footer={footer}>
      <div className={styles.dialog}>
        {!saved ? (
          <LoadState editor={editor} />
        ) : !saved.can_edit ? (
          <ReadOnlyBody editor={editor} />
        ) : step === "review" && prefill ? (
          <PrefillReview editor={editor} prefill={prefill} reviewed={reviewed} onReviewed={setReviewed} />
        ) : (
          <>
            <p className={styles.subtitle}>{t("subtitle")}</p>
            <EditorNotices editor={editor} />
            <EditorField editor={editor} id={fieldId} />
            <HistoryDisclosure editor={editor} expanded={historyOpen} onClick={() => setHistoryOpen((value) => !value)} />
            {historyOpen ? (
              <>
                <VersionHistory editor={editor} />
                <p className={styles.railNote}>{t("restore_hint")}</p>
              </>
            ) : null}
          </>
        )}
      </div>
    </Dialog>
  );
}
