"use client";

/**
 * Knowledge-base UX cleanup FR7 — Knowledge Base column in Library › Files (design 07·A).
 *
 * Status comes from server `FileDTO.kb`; the five states share the knowledge-base page's `kbFileState` mapping; stop keeping
 * a local Parsing state on this page (lost on refresh, without indicating the processing stage).
 */
import { useTranslations } from "next-intl";

import { useKnowledgeBaseDirectory, useKnowledgeBaseNamer, useKnowledgeBasesEnabled } from "@/components/knowledge/bases/use-knowledge-bases";
import type { FileDTO, FileKbStatus } from "@/lib/api";
import { appPath } from "@/lib/base-path";
import { kbFileState, type KbFileState } from "@/lib/kb-file-status";
import { knowledgeBaseFileHref } from "@/lib/knowledge-base-display";
import { KNOWLEDGE_BASE_HOMES } from "@/lib/knowledge-bases-api";

export type KbColumnView =
  | { kind: "added"; state: KbFileState; pct: number | null; href: string; outdated?: boolean }
  /** Present in the knowledge base, but the record lies outside your visible scope: show no status or navigation. */
  | { kind: "added_hidden" }
  | { kind: "suggested" }
  | { kind: "not_added"; canAdd: boolean };

/**
 * Deep link to this knowledge-base file: open its scope (space files include their space, which the knowledge-base page switches to) and details drawer.
 */
export function kbFileHref(kb: Pick<FileKbStatus, "file_id" | "scope"> & { workspace_id?: string | null }): string {
  const q = new URLSearchParams({ scope: kb.scope });
  if (kb.scope === "space" && kb.workspace_id) q.set("space", kb.workspace_id);
  q.set("file_id", kb.file_id);
  return appPath(`/knowledge/?${q.toString()}`);
}

/**
 * Where "View in knowledge base" (and its progress / retry variants) goes. Knowledge bases on: the knowledge base page,
 * which resolves the file's knowledge base and selects the file; Space files carry their Space. Off: today's address.
 */
export function kbStatusHref(
  kb: Pick<FileKbStatus, "file_id" | "scope"> & { workspace_id?: string | null },
  knowledgeBases: boolean,
): string {
  if (!knowledgeBases) return kbFileHref(kb);
  const href = knowledgeBaseFileHref(kb.file_id);
  return appPath(kb.scope === "space" && kb.workspace_id ? `${href}&space=${encodeURIComponent(kb.workspace_id)}` : href);
}

export function kbColumnView(
  f: Pick<FileDTO, "in_kb" | "kb" | "kb_promotable" | "kind" | "kb_outdated">,
  knowledgeBases = false,
): KbColumnView {
  if (f.kb) {
    const state = kbFileState(f.kb);
    return {
      kind: "added",
      state,
      pct: state === "processing" ? (f.kb.parse_progress_pct ?? null) : null,
      href: kbStatusHref(f.kb, knowledgeBases),
      // File versions: the entry indexes an older version; the user adds this one to update it.
      ...(f.kb_outdated ? { outdated: true } : {}),
    };
  }
  if (f.in_kb) return { kind: "added_hidden" };
  // Promotion suggestions reuse the existing signal: AI deliverable and eligible to add.
  if (f.kb_promotable && f.kind === "deliverable") return { kind: "suggested" };
  return { kind: "not_added", canAdd: f.kb_promotable };
}

const BADGE_CLASS: Record<KbFileState, string> = {
  uploading: "proc",
  queued: "proc",
  processing: "proc",
  ready: "avail",
  failed: "failed",
};

const LINK_KEY: Record<KbFileState, string> = {
  uploading: "kb_col.view_progress",
  queued: "kb_col.view_progress",
  processing: "kb_col.view_progress",
  ready: "kb_col.view_in_kb",
  failed: "kb_col.retry_in_kb",
};

/**
 * Knowledge bases on: "Added to <knowledge base> · status", named from the current Space's directory (My, this
 * Space, Team). A knowledge base the directory does not list (another Space's) keeps today's "Added · status".
 */
function AddedToKnowledgeBase({ knowledgeBaseId, className, status }: { knowledgeBaseId: string; className: string; status: string }) {
  const t = useTranslations("files_page");
  const { directory } = useKnowledgeBaseDirectory();
  const name = useKnowledgeBaseNamer();
  const kb = directory ? KNOWLEDGE_BASE_HOMES.flatMap((home) => directory.sections[home] ?? []).find((item) => item.id === knowledgeBaseId) : undefined;
  return <span className={className}>{kb ? t("kb_col.added_to", { name: name(kb), status }) : t("kb_col.added", { status })}</span>;
}

export function KbColumn({ file, onAdd }: { file: FileDTO; onAdd: () => void }) {
  const t = useTranslations("files_page");
  const tk = useTranslations("knowledge_base");
  const knowledgeBases = useKnowledgeBasesEnabled();
  const view = kbColumnView(file, knowledgeBases);
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  if (view.kind === "added") {
    const status = `${tk(`file_state.${view.state}`)}${view.pct != null ? ` ${view.pct}%` : ""}`;
    const badgeClass = `kbbadge ${BADGE_CLASS[view.state]}`;
    const knowledgeBaseId = knowledgeBases ? file.kb?.knowledge_base_id : null;
    return (
      <div className="kbcol" data-testid="kb-col">
        {knowledgeBaseId ? (
          <AddedToKnowledgeBase knowledgeBaseId={knowledgeBaseId} className={badgeClass} status={status} />
        ) : (
          <span className={badgeClass}>{t("kb_col.added", { status })}</span>
        )}
        <a href={view.href} onClick={stop}>{t(LINK_KEY[view.state])}</a>
        {view.outdated ? (
          <>
            <span className="kbcol-none" data-testid="kb-outdated">{t("kb_col.older_version")}</span>
            {file.kb_promotable ? (
              <button
                type="button"
                className="kbcol-link"
                data-testid="kb-add-version"
                onClick={(e) => { e.stopPropagation(); onAdd(); }}
              >{t("kb_col.add_this_version", { version: file.version_no ?? 1 })}</button>
            ) : null}
          </>
        ) : null}
      </div>
    );
  }
  if (view.kind === "added_hidden") {
    return (
      <div className="kbcol" data-testid="kb-col">
        <span className="kbbadge">{t("kb_col.added_plain")}</span>
      </div>
    );
  }
  const addLink = (
    <button
      type="button"
      className="kbcol-link"
      data-testid="kb-add"
      onClick={(e) => { e.stopPropagation(); onAdd(); }}
    >{t("kb.kb_add")}</button>
  );
  if (view.kind === "suggested") {
    return (
      <div className="kbcol" data-testid="kb-col">
        <span className="kbbadge sugg">{t("kb.kb_suggest")}</span>
        {addLink}
      </div>
    );
  }
  return (
    <div className="kbcol" data-testid="kb-col">
      <span className="kbcol-none">{t("kb_col.not_added")}</span>
      {view.canAdd ? addLink : null}
    </div>
  );
}
