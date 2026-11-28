"use client";

/**
 * One knowledge base's page (FEATURE_KNOWLEDGE_BASES, PRD §4.2, mockups ② KbFiles, ⑩ States, ⑫ PhoneKb).
 *
 * Address: `/knowledge/base/?id=<kbase_…>&tab=files|members|settings[&file_id][&chunk_id]`. File-only links
 * (`?file_id=X[&chunk_id=Y][&space=Z]`, from citations, global search and the Files column) switch to a listed
 * Space first, read the file and replace the address with the file's knowledge base (its `knowledge_base_id`, or
 * the scope's default). Opening an address grants nothing: an unknown or inaccessible knowledge base gives exactly
 * the not-found state (⑩a), with no hint whether it exists.
 *
 * Header: breadcrumb "Knowledge › My / Space / Team", the name, the description, badges (audience, your level,
 * updated) and actions hidden by level (⋯ Settings / Delete, Share or Who has access, Add files). Tabs Files /
 * Members / Settings (managers only) keep the tab in the address. With the switch off nothing here is reachable:
 * the API answers 404, so the page shows the not-found state (file-only links fall back to today's Knowledge page).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Upload } from "lucide-react";

import { PageStateHeading } from "@/components/shell/PageStateHeading";
import { PageActionButton, PageFrame, PageHeader } from "@/components/shell/page-frame";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useListDate } from "@/components/ui/list-time";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { KbFilesManager } from "@/components/knowledge/kb-files-manager";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { appPath } from "@/lib/base-path";
import { getKbFile, type KbFileDTO } from "@/lib/kb-api";
import { kbFileError } from "@/lib/kb-file-error";
import {
  audienceLabelKey,
  homeLabel,
  isKnowledgeBaseOwner,
  knowledgeBaseHref,
  KNOWLEDGE_DIRECTORY_HREF,
  levelLabelKey,
  type KnowledgeBasePageTab,
} from "@/lib/knowledge-base-display";
import {
  getKnowledgeBase,
  knowledgeBaseOfFile,
  KNOWLEDGE_BASES_CHANGED_EVENT,
  notifyKnowledgeBasesChanged,
  type KnowledgeBaseDTO,
} from "@/lib/knowledge-bases-api";
import { useAppRouter } from "@/lib/use-app-router";
import { useMediaQuery } from "@/lib/use-media-query";
import { useSpaceSelection } from "@/lib/use-space";

import { KbDeleteDialog } from "./kb-delete-dialog";
import { KbMembersPanel } from "./kb-members-panel";
import { knowledgeBaseContact } from "./kb-page-contact";
import { KbSettingsPanel } from "./kb-settings-panel";
import { KbShareDialog } from "./kb-share-dialog";
import { useKnowledgeBaseDirectory, useKnowledgeBaseNamer, useKnowledgeBasesEnabled } from "./use-knowledge-bases";

/** Phones (< 768): the shared page header is hidden, so badges and actions move into the page body. */
const PHONE_QUERY = "(max-width: 767.98px)";

export interface KbPageProps {
  /** `?id=`: the knowledge base. */
  knowledgeBaseId: string | null;
  /** `?tab=` (already resolved; anything unknown opens Files). */
  tab: KnowledgeBasePageTab;
  /** `?file_id=`: a file to open (with `id`), or the file whose knowledge base to open (without it). */
  fileId: string | null;
  /** `?chunk_id=`: the chunk to pin in the file details. */
  chunkId: string | null;
  /** `?space=`: switch to this Space first when it is one of the user's listed Spaces. */
  spaceId: string | null;
  /** The whole query string (the file table's view state). */
  viewQuery: string;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; kb: KnowledgeBaseDTO }
  | { kind: "not_found" }
  | { kind: "error"; message: string };

/** 404 (unknown, other Team, deleted, no level) and 403 answer the same: never reveal whether it exists. */
function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** A session that expired mid-visit goes to the login page and comes back to this address. */
function redirectToLogin(): void {
  if (typeof window === "undefined") return;
  const next = `/knowledge/base/${window.location.search}`;
  window.location.replace(appPath(`/login/?next=${encodeURIComponent(next)}`));
}

export function KbPage({ knowledgeBaseId, tab: tabParam, fileId, chunkId, spaceId, viewQuery }: KbPageProps) {
  const tKb = useTranslations("knowledge_base");
  const tc = useTranslations("knowledge_bases.common");
  const tp = useTranslations("knowledge_bases.page");
  const router = useAppRouter();
  const routerRef = useRef(router);
  routerRef.current = router;
  const enabled = useKnowledgeBasesEnabled();
  const userId = useOptionalAuthSession()?.me?.id ?? null;
  const namer = useKnowledgeBaseNamer();
  const listDate = useListDate();
  const phone = useMediaQuery(PHONE_QUERY);
  const { activeId, items: spaces, loading: spacesLoading, setActiveId } = useSpaceSelection();

  // ?space=: switch only to a listed Space, once per link, and read after the switch (Space reads use the current Space).
  const listedSpace = spaceId && !spacesLoading && spaces.some((space) => space.id === spaceId) ? spaceId : null;
  const switchedSpace = useRef<string | null>(null);
  useEffect(() => {
    if (!listedSpace || switchedSpace.current === listedSpace) return;
    switchedSpace.current = listedSpace;
    if (activeId !== listedSpace) setActiveId(listedSpace);
  }, [listedSpace, activeId, setActiveId]);
  const spaceReady = !spacesLoading && (listedSpace === null || activeId === listedSpace);

  // ── File-only links: resolve the file's knowledge base, then replace the address ──
  const needsResolve = enabled && !knowledgeBaseId && Boolean(fileId);
  const [resolvedFile, setResolvedFile] = useState<KbFileDTO | null>(null);
  const [fileFailed, setFileFailed] = useState(false);
  useEffect(() => {
    if (!needsResolve || !spaceReady || !fileId) return;
    let cancelled = false;
    setResolvedFile(null);
    setFileFailed(false);
    getKbFile(fileId).then(
      (dto) => { if (!cancelled) setResolvedFile(dto); },
      (error) => {
        if (cancelled) return;
        if (isUnauthorized(error)) { redirectToLogin(); return; }
        setFileFailed(true);
      },
    );
    return () => { cancelled = true; };
  }, [needsResolve, spaceReady, fileId, activeId]);
  // A file without a knowledge base ID is in its scope's default, which the directory names.
  const directoryNeeded = needsResolve && resolvedFile !== null && !resolvedFile.knowledge_base_id;
  const directoryState = useKnowledgeBaseDirectory({ enabled: directoryNeeded });
  const defaultOfFile = directoryNeeded && !directoryState.loading && resolvedFile
    ? knowledgeBaseOfFile(resolvedFile, directoryState.directory)
    : null;
  const resolvedId = !needsResolve || !resolvedFile ? null : resolvedFile.knowledge_base_id || defaultOfFile?.id || null;
  useEffect(() => {
    if (!resolvedId || !fileId) return;
    routerRef.current.replace(knowledgeBaseHref(resolvedId, { fileId, chunkId }), { scroll: false });
  }, [resolvedId, fileId, chunkId]);
  // Switch off: file-only links keep working on today's Knowledge page (rollback-safe).
  useEffect(() => {
    if (enabled || knowledgeBaseId || !fileId) return;
    const qs = new URLSearchParams({ file_id: fileId });
    if (chunkId) qs.set("chunk_id", chunkId);
    if (spaceId) qs.set("space", spaceId);
    routerRef.current.replace(`/knowledge/?${qs.toString()}`, { scroll: false });
  }, [enabled, knowledgeBaseId, fileId, chunkId, spaceId]);

  // ── The knowledge base ──
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const loadSeq = useRef(0);
  // After this page deleted the knowledge base, a refresh must not flash the not-found state before leaving.
  const deletedRef = useRef(false);
  const loadMessage = useCallback(
    (error: unknown) => {
      const key = kbFileError(error, "load");
      return key === "errors.load_failed" ? tp("load_failed_body") : tKb(key);
    },
    [tKb, tp],
  );
  const load = useCallback(
    async (background: boolean) => {
      if (!knowledgeBaseId || deletedRef.current) return;
      const seq = ++loadSeq.current;
      if (!background) setState((prev) => (prev.kind === "ready" && prev.kb.id === knowledgeBaseId ? prev : { kind: "loading" }));
      try {
        const kb = await getKnowledgeBase(knowledgeBaseId);
        if (seq !== loadSeq.current || deletedRef.current) return;
        setState({ kind: "ready", kb });
      } catch (error) {
        if (seq !== loadSeq.current || deletedRef.current) return;
        if (isUnauthorized(error)) { redirectToLogin(); return; }
        if (isNotFound(error)) { setState({ kind: "not_found" }); return; }
        // A failed background refresh keeps the knowledge base on screen.
        setState((prev) => (background && prev.kind === "ready" ? prev : { kind: "error", message: loadMessage(error) }));
      }
    },
    [knowledgeBaseId, loadMessage],
  );
  useEffect(() => {
    if (!enabled || !knowledgeBaseId || !spaceReady) return;
    void load(false);
  }, [enabled, knowledgeBaseId, spaceReady, activeId, load]);
  // Every knowledge base write (here, in dialogs, uploads, moves) refreshes this page in the background.
  useEffect(() => {
    if (!enabled || !knowledgeBaseId) return;
    const onChanged = () => { void load(true); };
    window.addEventListener(KNOWLEDGE_BASES_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(KNOWLEDGE_BASES_CHANGED_EVENT, onChanged);
  }, [enabled, knowledgeBaseId, load]);

  // ── Tabs (kept in the address with replace; other parameters stay) ──
  const [tab, setTab] = useState<KnowledgeBasePageTab>(tabParam);
  useEffect(() => { setTab(tabParam); }, [tabParam]);
  const changeTab = (next: KnowledgeBasePageTab) => {
    setTab(next);
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (next === "files") params.delete("tab");
    else {
      params.set("tab", next);
      // File details live in the Files tab only.
      params.delete("file_id");
      params.delete("chunk_id");
    }
    const qs = params.toString();
    routerRef.current.replace(qs ? `/knowledge/base/?${qs}` : "/knowledge/base/", { scroll: false });
  };

  // Header "Add files" opens the upload dialog in the Files tab; leaving the tab clears the request so
  // returning never reopens the dialog.
  const [uploadRequest, setUploadRequest] = useState(0);
  useEffect(() => { if (tab !== "files") setUploadRequest(0); }, [tab]);
  const [dialog, setDialog] = useState<"share" | "delete" | null>(null);

  const loadingFrame = (
    <PageFrame title={tKb("title")}>
      <div className="legacy-kb-page-loading">
        <PageStateHeading>{tKb("title")}</PageStateHeading>
        <LoadingSkeleton rows={5} label={tc("loading")} />
      </div>
    </PageFrame>
  );
  // ⑩a: the page title stays; the same answer for "deleted", "no access" and "never existed".
  const notFoundFrame = (
    <PageFrame title={tKb("title")}>
      <PageHeader />
      <section className="kbp-state" aria-labelledby="kbp-not-found-title" data-testid="kb-page-not-found">
        <h2 id="kbp-not-found-title" className="kbp-state__title">{tc("not_found_title")}</h2>
        <p className="kbp-state__body">{tc("not_found_body")}</p>
        <Button onClick={() => router.push(KNOWLEDGE_DIRECTORY_HREF)}>{tc("back_to_directory")}</Button>
      </section>
    </PageFrame>
  );
  const errorFrame = (message: string, retry: () => void) => (
    <PageFrame title={tKb("title")}>
      <PageHeader />
      <section className="kbp-state" role="alert" data-testid="kb-page-error">
        <h2 className="kbp-state__title">{tp("load_failed_title")}</h2>
        <p className="kbp-state__body">{message}</p>
        <Button onClick={retry}>{tc("retry")}</Button>
      </section>
    </PageFrame>
  );

  if (!knowledgeBaseId) {
    if (!fileId) return notFoundFrame;
    // Switch off: the effect above sends the link to today's Knowledge page.
    if (!enabled) return loadingFrame;
    if (fileFailed) return notFoundFrame;
    if (directoryNeeded && directoryState.error) return errorFrame(loadMessage(directoryState.error), directoryState.refresh);
    if (directoryNeeded && !directoryState.loading && !defaultOfFile) return notFoundFrame;
    return loadingFrame;
  }
  if (!enabled || state.kind === "not_found") return notFoundFrame;
  if (state.kind === "loading") return loadingFrame;
  if (state.kind === "error") return errorFrame(state.message, () => void load(false));

  const kb = state.kb;
  const name = namer(kb);
  const spaceName = kb.home === "space" ? spaces.find((space) => space.id === kb.home_id)?.name ?? null : null;
  const owner = isKnowledgeBaseOwner(kb, userId);
  const updated = listDate(kb.updated_at);
  const effectiveTab: KnowledgeBasePageTab = tab === "settings" && !kb.can.manage ? "files" : tab;
  const canShare = kb.home !== "personal";
  const manageShare = kb.can.manage && kb.home !== "personal";

  const addFiles = () => {
    if (effectiveTab !== "files") changeTab("files");
    setUploadRequest((n) => n + 1);
  };
  const reload = () => { void load(true); };
  const moreActions: RowAction[] = [
    ...(kb.can.manage ? [{ id: "settings", label: tp("menu_settings"), onSelect: () => changeTab("settings") }] : []),
    ...(kb.can.delete ? [{ id: "delete", label: tp("menu_delete"), danger: true, onSelect: () => setDialog("delete") }] : []),
  ];
  const moreMenu = moreActions.length > 0
    ? <RowActionsMenu label={tc("more_actions_for", { name })} actions={moreActions} className="kbp-more" />
    : null;
  const shareButton = canShare ? (
    <PageActionButton variant="secondary" data-testid="kb-page-share" onClick={() => setDialog("share")}>
      {manageShare ? tp("share") : tp("who_has_access")}
    </PageActionButton>
  ) : null;
  const addButton = kb.can.add_files ? (
    <PageActionButton icon={Upload} data-testid="kb-page-add-files" onClick={addFiles}>{tp("add_files")}</PageActionButton>
  ) : null;

  const crumb = (
    <nav className="kbp-crumb" aria-label={tp("breadcrumb_label")}>
      <a
        href={appPath(KNOWLEDGE_DIRECTORY_HREF)}
        onClick={(event) => {
          if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          router.push(KNOWLEDGE_DIRECTORY_HREF);
        }}
      >
        {tc("breadcrumb_root")}
      </a>
      <span className="kbp-crumb__sep" aria-hidden="true">›</span>
      <span>{homeLabel(kb.home, tc, spaceName)}</span>
    </nav>
  );
  const meta = (
    <div className="kbp-meta" data-testid="kb-page-meta">
      <Badge tone="outline">{tc(audienceLabelKey(kb))}</Badge>
      {kb.is_default ? <Badge>{tc("default_badge")}</Badge> : null}
      <Badge tone={owner ? "accent" : "neutral"}>{tc("your_level", { level: tc(levelLabelKey(kb, userId)) })}</Badge>
      {updated ? <span className="kbp-updated" title={updated.title}>{tc("updated", { time: updated.label })}</span> : null}
    </div>
  );
  const contact = knowledgeBaseContact(kb);

  return (
    <PageFrame title={name} mobileActions={moreMenu}>
      <div className="kbp-page" data-testid="kb-page">
        {phone ? (
          // ⑫: the top bar names the page; the body keeps the crumb, badges and wrapped actions.
          <div className="kbp-phone-head">
            <PageStateHeading>{name}</PageStateHeading>
            {crumb}
            {meta}
            {shareButton || addButton ? <div className="kbp-phone-actions">{shareButton}{addButton}</div> : null}
          </div>
        ) : (
          <header className="ui-page-head ui-page-header kbp-header">
            <div className="ui-page-header__copy">
              <div className="ui-page-header__crumb">{crumb}</div>
              <h1 className="ui-page-title">{name}</h1>
              {kb.description ? <p className="ui-page-desc">{kb.description}</p> : null}
              {meta}
            </div>
            {moreMenu || shareButton || addButton ? (
              <div className="ui-page-header__actions">{moreMenu}{shareButton}{addButton}</div>
            ) : null}
          </header>
        )}
        {!kb.can.add_files ? (
          <p className="kbp-use-only" data-testid="kb-page-use-only">{tp("use_only_notice", contact)}</p>
        ) : null}
        <Tabs<KnowledgeBasePageTab> value={effectiveTab} onValueChange={changeTab} idBase="kbp">
          <TabList appearance="underline" className="kbp-tabs" aria-label={tp("tabs_label")}>
            <Tab value="files" data-testid="kb-page-tab-files">
              {tp("tab_files")}
              <span className="cnt">{kb.file_count}</span>
            </Tab>
            <Tab value="members" data-testid="kb-page-tab-members">{tp("tab_members")}</Tab>
            {kb.can.manage ? <Tab value="settings" data-testid="kb-page-tab-settings">{tp("tab_settings")}</Tab> : null}
          </TabList>
          <TabPanel value="files" className="kbp-panel kbp-panel--files">
            <KbFilesManager
              key={kb.id}
              knowledgeBase={kb}
              viewQuery={viewQuery}
              initialFileId={fileId}
              initialChunkId={chunkId}
              uploadRequest={uploadRequest}
            />
          </TabPanel>
          <TabPanel value="members" className="kbp-panel">
            <KbMembersPanel kb={kb} onChanged={reload} />
          </TabPanel>
          {kb.can.manage ? (
            <TabPanel value="settings" className="kbp-panel">
              <KbSettingsPanel
                kb={kb}
                onUpdated={(next) => setState({ kind: "ready", kb: next })}
                onReload={reload}
                onRequestDelete={() => setDialog("delete")}
                onOpenMembers={() => changeTab("members")}
              />
            </TabPanel>
          ) : null}
        </Tabs>
      </div>
      {dialog === "share" ? <KbShareDialog kb={kb} onClose={() => setDialog(null)} onChanged={reload} /> : null}
      {dialog === "delete" ? (
        <KbDeleteDialog
          kb={kb}
          onClose={() => setDialog(null)}
          onDeleted={() => {
            deletedRef.current = true;
            setDialog(null);
            notifyKnowledgeBasesChanged();
            router.push(KNOWLEDGE_DIRECTORY_HREF);
          }}
        />
      ) : null}
    </PageFrame>
  );
}
