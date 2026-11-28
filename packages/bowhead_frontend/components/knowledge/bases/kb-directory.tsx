"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.1 ① and §4.12 ⑪): the directory on the Knowledge page.
 *
 * Three sections — My, the current Space (titled with its name) and the Team — list every knowledge base the
 * viewer can use with its audience, the viewer's level, the file count (failures flagged) and the last update.
 * The row ⋯ menu offers Share (read-only "Who has access" for people who cannot manage it; never on My knowledge
 * bases, which cannot be shared), Settings (managers) and Delete knowledge base… (owner or automatic manager,
 * never on defaults).
 *
 * Search filters knowledge bases by name or description and also lists files whose name matches, with the
 * knowledge base they are in. Deep search searches the content of every knowledge base the viewer can use
 * (today's "Scope: All" use case) and lists the hit files with their knowledge base and hit location. Opening a
 * file opens its knowledge base with the file selected. Phones (< 640 px) get list rows instead of tables.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { BookOpen } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { ListTime } from "@/components/ui/list-time";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { TableFrame } from "@/components/ui/table-frame";
import { kbChunkLocation, kbHighlight } from "@/components/knowledge/kb-file-drawer";
import { IconMag, IconSliders } from "@/components/knowledge/kb-icons";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { appPath } from "@/lib/base-path";
import { fetchKbFiles, searchKb, type KbFileDTO, type KbScope, type KbSearchResultChunk } from "@/lib/kb-api";
import { kbFileError } from "@/lib/kb-file-error";
import {
  audienceLabelKey,
  isKnowledgeBaseOwner,
  knowledgeBaseFileHref,
  knowledgeBaseHref,
  levelLabelKey,
} from "@/lib/knowledge-base-display";
import {
  directoryKnowledgeBases,
  knowledgeBaseOfFile,
  type KnowledgeBaseDirectory,
  type KnowledgeBaseDTO,
  type KnowledgeBaseHome,
} from "@/lib/knowledge-bases-api";
import { readableChunkText, snippetWindow } from "@/lib/kb-snippet";
import { shouldHandleAppLinkClick, shouldHandleMenuLinkSelect } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { useSpaceSelection } from "@/lib/use-space";

import { KbDeleteDialog } from "./kb-delete-dialog";
import { knowledgeBaseErrorText } from "./kb-directory-shared";
import { KbMigrationNotices } from "./kb-migration-notice";
import { KbShareDialog } from "./kb-share-dialog";
import { useKnowledgeBaseDirectory, useKnowledgeBaseNamer } from "./use-knowledge-bases";

/** Files listed under a name search (the rest are reached with a more specific name). */
export const DIRECTORY_FILE_LIMIT = 20;
const NAME_SEARCH_DEBOUNCE_MS = 300;
/** File rows resolved from deep-search hits in one request (the list route accepts 100 IDs). */
const DEEP_FILE_LIMIT = 100;
const SCOPES: readonly KbScope[] = ["personal", "space", "team"];

type Namer = (kb: KnowledgeBaseDTO) => string;

interface FileResult {
  fileId: string;
  filename: string;
  uploadedAt: string | null;
  kb: KnowledgeBaseDTO | null;
  hit?: KbSearchResultChunk;
}

function matchesKnowledgeBase(kb: KnowledgeBaseDTO, name: string, needle: string): boolean {
  if (!needle) return true;
  return name.toLowerCase().includes(needle) || (kb.description ?? "").toLowerCase().includes(needle);
}

function fileHref(result: Pick<FileResult, "fileId" | "kb" | "hit">): string {
  const chunkId = result.hit?.chunk_id ?? null;
  return result.kb
    ? knowledgeBaseHref(result.kb.id, { fileId: result.fileId, chunkId })
    : knowledgeBaseFileHref(result.fileId, chunkId);
}

/** In-app navigation for a link: plain clicks route in place, modified clicks keep the browser's behaviour. */
function useLinkNavigation() {
  const router = useAppRouter();
  return useCallback(
    (href: string) => (event: MouseEvent<HTMLElement>) => {
      if (!shouldHandleAppLinkClick(event)) return;
      event.preventDefault();
      router.push(href);
    },
    [router],
  );
}

export function KbDirectory() {
  const t = useTranslations("knowledge_bases.directory");
  const tc = useTranslations("knowledge_bases.common");
  const tKb = useTranslations("knowledge_base");
  const router = useAppRouter();
  const phone = useListRowsLayout();
  const { directory, loading, error, refresh } = useKnowledgeBaseDirectory();
  const namer = useKnowledgeBaseNamer();
  const { activeId, items: spaces } = useSpaceSelection();
  const userId = useOptionalAuthSession()?.me?.id ?? null;
  const searchId = useId();

  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [deep, setDeep] = useState(false);
  const [searchTick, setSearchTick] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [shareKb, setShareKb] = useState<KnowledgeBaseDTO | null>(null);
  const [deleteKb, setDeleteKb] = useState<KnowledgeBaseDTO | null>(null);

  useEffect(() => () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
  }, []);

  const spaceId = directory?.space_id ?? activeId ?? null;
  const spaceName = spaces.find((space) => space.id === spaceId)?.name ?? null;
  const all = useMemo(() => directoryKnowledgeBases(directory), [directory]);

  const onQuery = (value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    // Name search follows typing; deep search runs on Enter (semantic search is slower and costs more).
    if (!deep) debounceRef.current = setTimeout(() => setApplied(value.trim()), NAME_SEARCH_DEBOUNCE_MS);
  };
  const submit = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setApplied(query.trim());
    setSearchTick((n) => n + 1);
  };
  const toggleDeep = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setDeep((value) => !value);
    setApplied(query.trim());
    setSearchTick((n) => n + 1);
  };

  const needle = query.trim().toLowerCase();
  const totalFiles = all.reduce((sum, kb) => sum + (kb.file_count ?? 0), 0);

  const actionsFor = (kb: KnowledgeBaseDTO): RowAction[] => {
    const settingsHref = knowledgeBaseHref(kb.id, { tab: "settings" });
    return [
      // My knowledge bases cannot be shared (members API: knowledge_base_not_shareable).
      ...(kb.home !== "personal"
        ? [{ id: "share", label: kb.can.manage ? t("action_share") : t("action_who_has_access"), onSelect: () => setShareKb(kb) }]
        : []),
      ...(kb.can.manage
        ? [{
            id: "settings",
            label: t("action_settings"),
            href: appPath(settingsHref),
            onSelect: (event: MouseEvent<HTMLElement>) => {
              if (!shouldHandleMenuLinkSelect(event)) return;
              event.preventDefault();
              router.push(settingsHref);
            },
          }]
        : []),
      ...(kb.can.delete && !kb.is_default
        ? [{ id: "delete", label: t("action_delete"), danger: true, onSelect: () => setDeleteKb(kb) }]
        : []),
    ];
  };

  const sections: Array<{ home: KnowledgeBaseHome; title: string; desc: string; empty: string }> = [
    { home: "personal", title: tc("home_personal"), desc: t("section_personal_desc"), empty: t("empty_personal") },
    ...(directory?.space_id || spaceId
      ? [{ home: "space" as const, title: spaceName || tc("home_space"), desc: t("section_space_desc"), empty: t("empty_space") }]
      : []),
    { home: "team", title: tc("home_team"), desc: t("section_team_desc"), empty: t("empty_team") },
  ];

  const defaultName = (home: KnowledgeBaseHome, fallback: string) => {
    const kb = directory?.sections[home]?.find((item) => item.is_default);
    return kb ? namer(kb) : fallback;
  };

  const searching = applied.length > 0;

  let body: ReactNode;
  if (!directory && loading) {
    body = <LoadingSkeleton rows={6} label={tc("loading")} />;
  } else if (!directory && error) {
    body = (
      <Notice tone="error" className="kbd-load-error">
        <span data-testid="kb-directory-error">
          {knowledgeBaseErrorText(error, tKb, t("load_failed_generic"))}
        </span>{" "}
        <Button size="sm" onClick={refresh} data-testid="kb-directory-retry">
          {tc("retry")}
        </Button>
      </Notice>
    );
  } else if (deep) {
    body = searching ? (
      <DeepResults
        key={`${applied}:${searchTick}`}
        query={applied}
        directory={directory}
        namer={namer}
        spaceId={spaceId}
        phone={phone}
      />
    ) : (
      <p className="kbd-hint" data-testid="kb-deep-hint">{t("deep_hint")}</p>
    );
  } else {
    const visible = sections.map((section) => ({
      ...section,
      rows: (directory?.sections[section.home] ?? []).filter((kb) => matchesKnowledgeBase(kb, namer(kb), needle)),
    }));
    const anyMatch = visible.some((section) => section.rows.length > 0);
    body = (
      <>
        {needle && !anyMatch ? (
          <p className="kbd-empty-line" data-testid="kb-directory-no-match">{t("search_kbs_empty", { q: query.trim() })}</p>
        ) : null}
        {visible
          .filter((section) => !needle || section.rows.length > 0)
          .map((section) => (
            <DirectorySection
              key={section.home}
              home={section.home}
              title={section.title}
              desc={section.desc}
              empty={section.empty}
              rows={section.rows}
              phone={phone}
              namer={namer}
              userId={userId}
              actionsFor={actionsFor}
            />
          ))}
        {searching ? (
          <NameFileResults
            key={`${applied}:${searchTick}`}
            query={applied}
            directory={directory}
            namer={namer}
            phone={phone}
          />
        ) : null}
      </>
    );
  }

  return (
    <div className="kbd" data-testid="kb-directory">
      <KbMigrationNotices
        personalName={defaultName("personal", tc("default_name_personal"))}
        spaceName={defaultName("space", spaceName || tc("default_name_space"))}
        teamName={defaultName("team", tc("default_name_team"))}
      />
      <div className="kbd-toolbar">
        <div className={`kb-search kbd-search${deep ? " deep" : ""}`}>
          <span className="mag" aria-hidden><IconMag /></span>
          <input
            id={searchId}
            type="search"
            aria-label={t("search_label")}
            placeholder={deep ? tKb("filter.deep_placeholder") : t("search_placeholder")}
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
            data-testid="kb-directory-search"
          />
        </div>
        <button
          type="button"
          className={`kb-filterchip kb-deep-toggle${deep ? " active" : ""}`}
          aria-pressed={deep}
          onClick={toggleDeep}
          data-testid="kb-directory-deep"
        >
          <span className="kb-chipicon" aria-hidden><IconSliders /></span>
          {tKb("filter.deep_search")}
        </button>
        <span className="kbd-grow" />
        {directory ? (
          <span className="kbd-total" data-testid="kb-directory-total">{t("total", { kbs: all.length, files: totalFiles })}</span>
        ) : null}
      </div>
      {body}
      {shareKb ? (
        <KbShareDialog kb={shareKb} onClose={() => setShareKb(null)} onChanged={refresh} />
      ) : null}
      {deleteKb ? (
        <KbDeleteDialog kb={deleteKb} onClose={() => setDeleteKb(null)} onDeleted={() => setDeleteKb(null)} />
      ) : null}
    </div>
  );
}

// ─── one section ────────────────────────────────────────────────────────

function DirectorySection({
  home,
  title,
  desc,
  empty,
  rows,
  phone,
  namer,
  userId,
  actionsFor,
}: {
  home: KnowledgeBaseHome;
  title: string;
  desc: string;
  empty: string;
  rows: KnowledgeBaseDTO[];
  phone: boolean;
  namer: Namer;
  userId: string | null;
  actionsFor: (kb: KnowledgeBaseDTO) => RowAction[];
}) {
  const t = useTranslations("knowledge_bases.directory");
  const tc = useTranslations("knowledge_bases.common");
  const router = useAppRouter();
  const navigate = useLinkNavigation();
  const headingId = useId();
  const levelOf = (kb: KnowledgeBaseDTO) =>
    isKnowledgeBaseOwner(kb, userId) ? (
      <Badge tone="accent" size={phone ? "sm" : "md"}>{tc("level_owner")}</Badge>
    ) : (
      <span className="kbd-muted">{tc(levelLabelKey(kb, userId))}</span>
    );

  return (
    <section className="kbd-sec" aria-labelledby={headingId} data-testid={`kb-directory-section-${home}`}>
      <div className="kbd-sec-head">
        <h2 id={headingId} className="kbd-sec-title">{title}</h2>
        <span className="kbd-sec-desc">{desc}</span>
      </div>
      {rows.length === 0 ? (
        <p className="kbd-empty-line">{empty}</p>
      ) : phone ? (
        <ListRows label={title} className="kbd-list">
          {rows.map((kb) => {
            const name = namer(kb);
            return (
              <ListRow
                key={kb.id}
                title={(
                  <span className="kbd-list-title" data-testid={`kb-directory-row-${kb.id}`}>
                    <span>{name}</span>
                    {kb.is_default ? <Badge size="sm">{tc("default_badge")}</Badge> : null}
                  </span>
                )}
                openLabel={name}
                onOpen={() => router.push(knowledgeBaseHref(kb.id))}
                subtitle={kb.description || undefined}
                meta={(
                  <>
                    {tc(audienceLabelKey(kb))}
                    {" · "}
                    {tc("files_n", { n: kb.file_count })}
                    {kb.failed_count > 0 ? <>{" · "}<span className="kbd-failed-text">{tc("failed_n", { n: kb.failed_count })}</span></> : null}
                    {kb.updated_at ? <>{" · "}<ListTime value={kb.updated_at} /></> : null}
                  </>
                )}
                status={levelOf(kb)}
                actions={<RowActionsMenu label={tc("more_actions_for", { name })} actions={actionsFor(kb)} />}
              />
            );
          })}
        </ListRows>
      ) : (
        <TableFrame className="kbd-table-frame">
          <table className="ui-table kbd-table">
            <thead>
              <tr>
                <th scope="col">{t("col_name")}</th>
                <th scope="col">{t("col_audience")}</th>
                <th scope="col">{t("col_level")}</th>
                <th scope="col" className="ui-num">{t("col_files")}</th>
                <th scope="col">{t("col_updated")}</th>
                <th scope="col" className="ui-table-actions"><span className="sr-only">{t("col_actions")}</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((kb) => {
                const name = namer(kb);
                const href = knowledgeBaseHref(kb.id);
                return (
                  <tr key={kb.id} data-testid={`kb-directory-row-${kb.id}`}>
                    <td>
                      <div className="kbd-row-title">
                        <span className="kbd-kb-icon" aria-hidden><BookOpen /></span>
                        <span className="kbd-row-text">
                          <span className="kbd-cell-main">
                            <a href={appPath(href)} onClick={navigate(href)} className="kbd-name">{name}</a>
                            {kb.is_default ? <Badge size="sm">{tc("default_badge")}</Badge> : null}
                          </span>
                          {kb.description ? <span className="kbd-cell-sub">{kb.description}</span> : null}
                        </span>
                      </div>
                    </td>
                    <td><Badge tone="outline">{tc(audienceLabelKey(kb))}</Badge></td>
                    <td>{levelOf(kb)}</td>
                    <td className="ui-num">
                      <span className="kbd-files">
                        {kb.file_count}
                        {kb.failed_count > 0 ? <Badge tone="danger" size="sm">{tc("failed_n", { n: kb.failed_count })}</Badge> : null}
                      </span>
                    </td>
                    <td><ListTime className="kbd-muted" value={kb.updated_at} /></td>
                    <td className="ui-table-actions">
                      <RowActionsMenu label={tc("more_actions_for", { name })} actions={actionsFor(kb)} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableFrame>
      )}
    </section>
  );
}

// ─── file results (name search and deep search) ────────────────────────────

function FileResultsTable({
  label,
  results,
  query,
  phone,
  namer,
}: {
  label: string;
  results: FileResult[];
  query: string;
  phone: boolean;
  namer: Namer;
}) {
  const t = useTranslations("knowledge_bases.directory");
  const tKb = useTranslations("knowledge_base");
  const router = useAppRouter();
  const navigate = useLinkNavigation();
  const hitLine = (result: FileResult) => {
    if (!result.hit) return null;
    const location = kbChunkLocation(tKb, result.hit);
    const snip = snippetWindow(readableChunkText(result.hit.content), query);
    return (
      <span className="kbd-cell-sub kbd-hit" data-testid="kb-directory-hit">
        {location ? <span className="kbd-hit-loc">{location}</span> : null}
        …{snip.before}{snip.match ? <mark>{snip.match}</mark> : null}{snip.after}…
      </span>
    );
  };
  if (phone) {
    return (
      <ListRows label={label} className="kbd-list">
        {results.map((result) => (
          <ListRow
            key={result.fileId}
            title={<span data-testid={`kb-directory-file-${result.fileId}`}>{result.filename}</span>}
            openLabel={t("open_file", { name: result.filename })}
            onOpen={() => router.push(fileHref(result))}
            subtitle={result.kb ? namer(result.kb) : undefined}
            meta={result.hit ? hitLine(result) : result.uploadedAt ? <ListTime value={result.uploadedAt} /> : undefined}
          />
        ))}
      </ListRows>
    );
  }
  return (
    <TableFrame className="kbd-table-frame">
      <table className="ui-table kbd-table kbd-file-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">{t("files_col_name")}</th>
            <th scope="col">{t("files_col_kb")}</th>
            <th scope="col">{t("files_col_added")}</th>
          </tr>
        </thead>
        <tbody>
          {results.map((result) => {
            const href = fileHref(result);
            return (
              <tr key={result.fileId} data-testid={`kb-directory-file-${result.fileId}`}>
                <td>
                  <span className="kbd-row-text">
                    <span className="kbd-cell-main">
                      <a
                        href={appPath(href)}
                        onClick={navigate(href)}
                        className="kbd-name"
                        aria-label={t("open_file", { name: result.filename })}
                      >
                        {result.hit ? result.filename : kbHighlight(result.filename, query)}
                      </a>
                    </span>
                    {hitLine(result)}
                  </span>
                </td>
                <td data-testid="kb-directory-file-kb">{result.kb ? namer(result.kb) : "—"}</td>
                <td><ListTime className="kbd-muted" value={result.uploadedAt} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableFrame>
  );
}

function ResultsSection({ title, desc, children, testId }: { title: string; desc: string; children: ReactNode; testId: string }) {
  const headingId = useId();
  return (
    <section className="kbd-sec" aria-labelledby={headingId} data-testid={testId}>
      <div className="kbd-sec-head">
        <h2 id={headingId} className="kbd-sec-title">{title}</h2>
        <span className="kbd-sec-desc">{desc}</span>
      </div>
      {children}
    </section>
  );
}

type Loaded<T> = { status: "loading" } | { status: "error"; error: unknown } | { status: "done"; value: T };

/** Name search: files whose name contains the query, in every scope the viewer can read. Mounted per query. */
function NameFileResults({
  query,
  directory,
  namer,
  phone,
}: {
  query: string;
  directory: KnowledgeBaseDirectory | null;
  namer: Namer;
  phone: boolean;
}) {
  const t = useTranslations("knowledge_bases.directory");
  const tKb = useTranslations("knowledge_base");
  const [state, setState] = useState<Loaded<{ files: KbFileDTO[]; more: boolean }>>({ status: "loading" });
  useEffect(() => {
    let cancelled = false;
    fetchKbFiles({ scope: "all", q: query, limit: DIRECTORY_FILE_LIMIT, order_by: "uploaded_at", order_dir: "desc" })
      .then((res) => {
        if (!cancelled) setState({ status: "done", value: { files: res.files, more: Boolean(res.has_more) || res.total > res.files.length } });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", error });
      });
    return () => {
      cancelled = true;
    };
  }, [query]);
  const title = t("files_heading");
  let content: ReactNode;
  if (state.status === "loading") content = <p className="kbd-empty-line">{t("files_loading")}</p>;
  else if (state.status === "error") {
    content = (
      <Notice tone="error">
        {t("files_failed", { error: tKb(kbFileError(state.error, "search")) })}
      </Notice>
    );
  } else if (state.value.files.length === 0) {
    content = <p className="kbd-empty-line" data-testid="kb-directory-files-empty">{t("files_empty", { q: query })}</p>;
  } else {
    const results: FileResult[] = state.value.files.map((file) => ({
      fileId: file.file_id,
      filename: file.filename,
      uploadedAt: file.uploaded_at,
      kb: knowledgeBaseOfFile(file, directory),
    }));
    content = (
      <>
        <FileResultsTable label={title} results={results} query={query} phone={phone} namer={namer} />
        {state.value.more ? <p className="kbd-empty-line">{t("files_more", { n: DIRECTORY_FILE_LIMIT })}</p> : null}
      </>
    );
  }
  return (
    <ResultsSection title={title} desc={t("files_heading_desc", { q: query })} testId="kb-directory-files">
      {content}
    </ResultsSection>
  );
}

/** Deep search across every knowledge base the viewer can use. Mounted per submitted query. */
function DeepResults({
  query,
  directory,
  namer,
  spaceId,
  phone,
}: {
  query: string;
  directory: KnowledgeBaseDirectory | null;
  namer: Namer;
  spaceId: string | null;
  phone: boolean;
}) {
  const t = useTranslations("knowledge_bases.directory");
  const tKb = useTranslations("knowledge_base");
  const [state, setState] = useState<Loaded<{ results: FileResult[]; partial: boolean }>>({ status: "loading" });
  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Every scope the viewer can read, as the file table's "All" does; no Space scope without a Space.
      const scopeFilter = SCOPES.filter((scope) => scope !== "space" || spaceId);
      const res = await searchKb(query, { scope_filter: scopeFilter });
      const firstHit = new Map<string, KbSearchResultChunk>();
      for (const chunk of res.chunks) if (!firstHit.has(chunk.file_id)) firstHit.set(chunk.file_id, chunk);
      const ids = [...firstHit.keys()].slice(0, DEEP_FILE_LIMIT);
      // One read resolves each file's knowledge base (and drops files the viewer can no longer read).
      const files = ids.length
        ? await fetchKbFiles({ scope: "all", file_ids: ids, limit: DEEP_FILE_LIMIT }).then((r) => r.files).catch(() => null)
        : [];
      const byId = new Map((files ?? []).map((file) => [file.file_id, file]));
      const results: FileResult[] = ids.flatMap((id) => {
        const hit = firstHit.get(id)!;
        const file = byId.get(id);
        if (files && !file) return [];
        return [{
          fileId: id,
          filename: file?.filename ?? hit.file_name,
          uploadedAt: file?.uploaded_at ?? null,
          kb: file ? knowledgeBaseOfFile(file, directory) : null,
          hit,
        }];
      });
      if (!cancelled) setState({ status: "done", value: { results, partial: Boolean(res.partial) } });
    })().catch((error: unknown) => {
      if (!cancelled) setState({ status: "error", error });
    });
    return () => {
      cancelled = true;
    };
    // The directory only names knowledge bases; a refresh must not repeat the search.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, spaceId]);

  const title = t("deep_heading");
  let content: ReactNode;
  if (state.status === "loading") content = <p className="kbd-empty-line">{t("deep_searching")}</p>;
  else if (state.status === "error") {
    content = <Notice tone="error">{t("deep_failed", { error: tKb(kbFileError(state.error, "search")) })}</Notice>;
  } else {
    // Re-resolve names from the latest directory (a rename or new default name shows without searching again).
    const results = state.value.results.map((result) => ({
      ...result,
      kb: result.kb ? (directoryKnowledgeBases(directory).find((kb) => kb.id === result.kb!.id) ?? result.kb) : null,
    }));
    content = (
      <>
        {state.value.partial ? <Notice tone="info">{t("deep_partial")}</Notice> : null}
        {results.length === 0 ? (
          <p className="kbd-empty-line" data-testid="kb-directory-deep-empty">{t("deep_empty", { q: query })}</p>
        ) : (
          <>
            <p className="kbd-count" data-testid="kb-directory-deep-count">{t("deep_count", { n: results.length })}</p>
            <FileResultsTable label={title} results={results} query={query} phone={phone} namer={namer} />
          </>
        )}
      </>
    );
  }
  return (
    <ResultsSection title={title} desc={t("deep_desc")} testId="kb-directory-deep-results">
      {content}
    </ResultsSection>
  );
}
