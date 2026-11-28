"use client";

/**
 * V7 Knowledge Base · /knowledge — KB management page (interaction design 2b).
 *
 * Left sidebar lists only content types: Files / Document Matrices / Extracted Results / Templates (?tab=files|matrix|records|templates).
 * Files is the kb-management file manager (scope, filters, search, bulk operations, upload); MatrixTab renders the other three.
 *
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.1 Q1): with the switch on the tabs are Knowledge bases (the directory,
 * `?tab=bases`, the default; old `?tab=files` links open it) / Document matrix / Extraction tables / Templates. The header's
 * Add files opens the upload dialog aimed at the current Space's default knowledge base (else My knowledge), and New
 * knowledge base opens ⑤. Legacy file links (`?file_id=`, citations, global search, the Files column) move to the file's
 * knowledge base page. With the switch off the page is exactly today's.
 */

import { Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { Plus, Upload } from "lucide-react";

import { PageStateHeading } from "@/components/shell/PageStateHeading";
import {
  PageActionButton,
  PageFrame,
  PageHeader,
  PageMobileAction,
  PageTabs,
} from "@/components/shell/page-frame";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { KbFilesManager } from "@/components/knowledge/kb-files-manager";
import { KbIndexNotice } from "@/components/knowledge/kb-index-notice";
import {
  KB_BASES_SECTIONS,
  KB_SECTIONS,
  type KbNavCounts,
  type KbSection,
  extractionTablesListedCount,
  matrixTemplatesListedCount,
  resolveKbSection,
} from "@/components/knowledge/kb-nav";
import { KbUploadModal } from "@/components/knowledge/kb-upload-modal";
import { KbCreateDialog } from "@/components/knowledge/bases/kb-create-dialog";
import { KbDirectory } from "@/components/knowledge/bases/kb-directory";
import { defaultUploadTarget } from "@/components/knowledge/bases/kb-directory-shared";
import {
  useKnowledgeBaseDirectory,
  useKnowledgeBaseNamer,
  useKnowledgeBasesEnabled,
} from "@/components/knowledge/bases/use-knowledge-bases";
import { MatrixTab, type MatrixSectionKey } from "@/components/knowledge/matrix/matrix-tab";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { requestSpaceAssignment } from "@/lib/auth-api";
import { fetchKbCapabilities, type KbCapabilities } from "@/lib/kb-capabilities-api";
import { listDocTypesWithStats } from "@/lib/kb-knowledge-tables-api";
import { listMatrices } from "@/lib/kb-matrix/snapshot-api";
import { listTeamTemplates } from "@/lib/kb-matrix/team-template-api";
import { templateViewsFrom, templatesListedCount } from "@/lib/kb-matrix/template-view";
import type { KbFileDTO, KbListScope, KbScope } from "@/lib/kb-api";
import { knowledgeBaseFileHref, knowledgeBaseHref } from "@/lib/knowledge-base-display";
import { directoryKnowledgeBases, knowledgeBaseOfFile, notifyKnowledgeBasesChanged } from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";
import { appPath } from "@/lib/base-path";
import { useAppRouter } from "@/lib/use-app-router";
import { toast } from "@/lib/use-toast";


export default function KnowledgePage() {
  const t = useTranslations("knowledge_base");
  return (
    <Suspense fallback={<div className="legacy-kb-page-loading">{t("loading")}</div>}>
      <KnowledgePageInner />
    </Suspense>
  );
}


function KnowledgePageInner() {
  const t = useTranslations("knowledge_base");
  const setupReturnT = useTranslations("settings.setup_return");
  const searchParams = useSearchParams();
  const router = useAppRouter();
  const auth = useAuthRedirect();
  const tTabs = useTranslations("knowledge_bases.tabs");
  const tDir = useTranslations("knowledge_bases.directory");
  // FEATURE_KNOWLEDGE_BASES: both read the same /me; either one being known is enough.
  const kbEnabled = useKnowledgeBasesEnabled() || auth.me?.capabilities?.knowledge_bases === true;
  const section: KbSection = resolveKbSection(
    searchParams?.get("tab") ?? null,
    searchParams?.get("view") ?? null,
    { knowledgeBases: kbEnabled },
  );
  const [navCounts, setNavCounts] = useState<
    { space: string; matrix: number; records: number; templates: number } | { space: string; failed: true } | null
  >(null);
  const [uploadRequest, setUploadRequest] = useState(0);
  // credential-readiness R9: inline status hints for three KB capabilities (member-readable endpoint; failures silently show no hint).
  const [kbCaps, setKbCaps] = useState<KbCapabilities | null>(null);
  // Null before space hydration; badge effect below depends on it and refetches on settlement/switch.
  const {
    activeId: activeSpaceId,
    items: spaces,
    loading: spacesLoading,
    error: spacesError,
    refresh: refreshSpaces,
  } = useSpaceSelection();
  const [assignmentState, setAssignmentState] = useState<"idle" | "submitting" | "sent" | "error">("idle");
  const fileIdParam = searchParams?.get("file_id") ?? null;
  const chunkIdParam = searchParams?.get("chunk_id") ?? null;
  // Deep-link scope: ?scope=personal|space|team|all (legacy workspace equals space; "all" comes from the scope filter);
  // space files also carry ?space=<space id>.
  const scopeRaw = searchParams?.get("scope") ?? null;
  const scopeParam: KbListScope | null = scopeRaw === "workspace"
    ? "space"
    : scopeRaw === "personal" || scopeRaw === "space" || scopeRaw === "team" || scopeRaw === "all" ? scopeRaw : null;
  const spaceParam = searchParams?.get("space") ?? null;
  const returnToSetup = searchParams?.get("from") === "setup"
    && Boolean(auth.me?.capabilities?.team_onboarding);
  const activeSpaceIsAccessible = Boolean(
    activeSpaceId && spaces.some((space) => space.id === activeSpaceId),
  );
  const hasNoAccessibleSpace = !spacesLoading && !spacesError && spaces.length === 0;
  // Knowledge bases: the shared directory read (tab count, upload default); no request with the switch off.
  const kbDirectory = useKnowledgeBaseDirectory({
    enabled: kbEnabled && auth.status === "authed" && activeSpaceIsAccessible,
  });
  const namer = useKnowledgeBaseNamer();
  const [kbUpload, setKbUpload] = useState<{ scope: KbScope; knowledgeBaseId: string | null } | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const uploadedRef = useRef<KbFileDTO[]>([]);
  // Legacy file links (citations "Open in knowledge base", global search, the Files column) open the file's
  // knowledge base page, which resolves the knowledge base and switches to the named Space first.
  const legacyFileRedirect = kbEnabled && section === "bases" && fileIdParam
    ? knowledgeBaseFileHref(fileIdParam, chunkIdParam) + (spaceParam ? `&space=${encodeURIComponent(spaceParam)}` : "")
    : null;
  const redirectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!legacyFileRedirect || redirectedRef.current === legacyFileRedirect) return;
    redirectedRef.current = legacyFileRedirect;
    router.replace(legacyFileRedirect);
  }, [legacyFileRedirect, router]);

  // Sidebar counts (interaction design 05) count only listed objects: Document Matrices = my matrices; Extracted Results = extraction tables with results;
  // Templates = everything in Templates view (built-in / mine / extraction tables, plus team library). Space switch resets to placeholders;
  // same-space view switching silently refetches (counts track creation/deletion), without flashing placeholders. Space comes from useSpaceSelection:
  // cold load fetches only after space settles; counts follow space switches.
  const searchKey = searchParams?.toString() ?? "";
  // Search, filter and sort parameters (`q`, `f`, `sort`; a matrix writes them as they change) never change these counts.
  const countsKey = useMemo(() => {
    const params = new URLSearchParams(searchKey);
    for (const key of ["q", "f", "sort"]) params.delete(key);
    return params.toString();
  }, [searchKey]);
  useEffect(() => {
    if (auth.status !== "authed" || !activeSpaceIsAccessible || !activeSpaceId) return;
    let cancelled = false;
    const space = activeSpaceId;
    setNavCounts((prev) => (prev && prev.space === space ? prev : null));
    Promise.all([
      listDocTypesWithStats(space),
      listMatrices(space).catch(() => []),
      listTeamTemplates(space).catch(() => []),
    ])
      .then(([docTypes, matrices, team]) => {
        if (cancelled) return;
        setNavCounts({
          space,
          matrix: matrices.length,
          // Knowledge bases (Q1): Extraction tables = definitions plus results; Templates = matrix templates only.
          records: kbEnabled ? extractionTablesListedCount(docTypes) : templateViewsFrom(docTypes).length,
          templates: kbEnabled ? matrixTemplatesListedCount(docTypes.items, team) : templatesListedCount(docTypes.items, team),
        });
      })
      .catch(() => { if (!cancelled) setNavCounts({ space, failed: true }); });
    return () => { cancelled = true; };
  }, [auth.status, activeSpaceId, activeSpaceIsAccessible, countsKey, kbEnabled]);

  // Header upload request applies only in Files view; clear on leaving so returning never reopens dialog.
  useEffect(() => {
    if (section !== "files") setUploadRequest(0);
  }, [section]);

  useEffect(() => {
    if (auth.status !== "authed" || !activeSpaceIsAccessible) return;
    let cancelled = false;
    fetchKbCapabilities().then((caps) => { if (!cancelled) setKbCaps(caps); });
    return () => { cancelled = true; };
  }, [auth.status, activeSpaceIsAccessible]);

  // Vision first (absence is most severe: scans do not parse at all), then reranking/row extraction.
  const degradedKbCaps = kbCaps
    ? (["kb_vision", "kb_rerank", "kb_row_extractor"] as const).filter((k) => kbCaps[k] !== "enabled")
    : [];

  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <PageFrame title={t("title")}>
        <div className="legacy-kb-page-loading">
          <PageStateHeading>{t("title")}</PageStateHeading>
          <LoadingSkeleton rows={5} label={t("loading")} />
        </div>
      </PageFrame>
    );
  }

  if (spacesLoading) {
    return (
      <PageFrame title={t("title")}>
        <div className="legacy-kb-page-loading">
          <PageStateHeading>{t("title")}</PageStateHeading>
          <LoadingSkeleton rows={5} label={t("loading")} />
        </div>
      </PageFrame>
    );
  }

  if (hasNoAccessibleSpace) {
    const canRequestAccess = Boolean(auth.me?.capabilities?.space_admin_v2);
    const requestAccess = async () => {
      if (!canRequestAccess || assignmentState !== "idle") return;
      setAssignmentState("submitting");
      try {
        await requestSpaceAssignment();
        setAssignmentState("sent");
      } catch {
        setAssignmentState("error");
      }
    };
    return (
      <PageFrame title={t("title")}>
        <section
          aria-labelledby="knowledge-space-access-title"
          className="legacy-kb-space-access"
        >
          <PageStateHeading>{t("title")}</PageStateHeading>
          <h2 id="knowledge-space-access-title" className="legacy-kb-space-access-title">
            {t("space_access.title")}
          </h2>
          <p className="legacy-kb-space-access-detail">
            {t("space_access.detail")}
          </p>
          {canRequestAccess ? (
            <>
              <button
                type="button"
                disabled={assignmentState === "submitting" || assignmentState === "sent"}
                onClick={() => void requestAccess()}
              >
                {t(`space_access.request_${assignmentState}`)}
              </button>
              {assignmentState === "error" ? <p role="alert">{t("space_access.request_error")}</p> : null}
            </>
          ) : null}
          <button type="button" onClick={refreshSpaces} className={canRequestAccess ? "legacy-kb-space-access-refresh is-after-request" : "legacy-kb-space-access-refresh"}>
            {t("space_access.refresh")}
          </button>
        </section>
      </PageFrame>
    );
  }

  if (!activeSpaceIsAccessible) {
    return (
      <PageFrame title={t("title")}>
        <section className="legacy-kb-space-select">
          <PageStateHeading>{t("title")}</PageStateHeading>
          <p className="legacy-kb-space-select-hint">{t("space_access.select")}</p>
          <button type="button" onClick={refreshSpaces}>{t("space_access.refresh")}</button>
        </section>
      </PageFrame>
    );
  }

  const sectionHref = (next: KbSection) => {
    const nextParams = new URLSearchParams(searchParams?.toString() ?? "");
    for (const key of ["view", "new", "from_dt", "file_id"]) nextParams.delete(key);
    nextParams.set("tab", next);
    return `/knowledge?${nextParams.toString()}`;
  };
  const openSection = (next: KbSection) => {
    if (next === section && !searchParams?.get("view") && !searchParams?.get("new")) return;
    router.push(sectionHref(next), { scroll: false });
  };
  const onUpload = () => {
    if (kbEnabled) {
      // The page owns the upload dialog here (the directory has no file table): aim it at the current Space's
      // default knowledge base, else My knowledge (PRD §4.7); the dialog lets the person pick another target.
      const target = defaultUploadTarget(kbDirectory.directory);
      setKbUpload({
        scope: target ? target.home : activeSpaceId ? "space" : "personal",
        knowledgeBaseId: target?.id ?? null,
      });
      return;
    }
    if (section !== "files") openSection("files");
    setUploadRequest((n) => n + 1);
  };
  // Single-file and ZIP completions arrive one file at a time; announce them once.
  const onKbUploaded = (dto: KbFileDTO) => {
    setKbUpload(null);
    uploadedRef.current.push(dto);
    if (uploadedRef.current.length > 1) return;
    queueMicrotask(() => {
      const batch = uploadedRef.current;
      uploadedRef.current = [];
      notifyKnowledgeBasesChanged();
      const kb = knowledgeBaseOfFile(batch[0], kbDirectory.directory);
      const text = kb
        ? batch.length === 1
          ? tDir("uploaded_one", { name: batch[0].filename, kb: namer(kb) })
          : tDir("uploaded_n", { n: batch.length, kb: namer(kb) })
        : batch.length === 1
          ? t("toast.uploaded_one", { name: batch[0].filename })
          : t("toast.uploaded_n", { n: batch.length });
      toast({
        kind: "success",
        text,
        action: kb ? { label: tDir("open_knowledge_base"), onAction: () => router.push(knowledgeBaseHref(kb.id)) } : undefined,
      });
    });
  };
  const counts: KbNavCounts =
    navCounts === null
      ? { matrix: null, records: null, templates: null }
      : "failed" in navCounts
        ? {}
        : { matrix: navCounts.matrix, records: navCounts.records, templates: navCounts.templates };
  if (kbEnabled) {
    // The directory tab counts the knowledge bases listed; a failed read shows no count.
    counts.bases = kbDirectory.directory
      ? directoryKnowledgeBases(kbDirectory.directory).length
      : kbDirectory.error ? undefined : null;
  }
  const tabSections = kbEnabled ? KB_BASES_SECTIONS : KB_SECTIONS;
  const tabLabel = (key: KbSection) => (kbEnabled ? tTabs(`section_${key}`) : t(`nav_v2.section_${key}`));
  if (kbEnabled) {
    return (
      <KnowledgeBasesPage
        title={t("title")}
        section={section}
        redirecting={Boolean(legacyFileRedirect)}
        returnToSetup={returnToSetup}
        capabilityNotice={degradedKbCaps.length > 0 && kbCaps ? (
          <CapabilityNotice degraded={degradedKbCaps} caps={kbCaps} canSetup={Boolean(auth.me?.capabilities?.team_onboarding)} />
        ) : null}
        tabs={(
          <PageTabs
            label={t("nav_v2.nav_label")}
            testId="kb-nav"
            onNavigate={(item) => openSection(item.key as KbSection)}
            items={tabSections.map((key) => ({
              key,
              label: tabLabel(key),
              href: sectionHref(key),
              current: key === section,
              count: counts[key],
              testId: `kb-nav-${key}`,
            }))}
          />
        )}
        description={
          section === "bases"
            ? tTabs("description_bases")
            : section === "records" || section === "templates"
              ? tTabs(`description_${section}`)
              : t("nav_v2.description_matrix")
        }
        isTeamAdmin={auth.isTeamAdmin}
        matrixKey={`${auth.me?.id}:${auth.me?.team_id}:${activeSpaceId}`}
        onUpload={onUpload}
        onCreate={() => setCreateOpen(true)}
      >
        {kbUpload ? (
          <KbUploadModal
            defaultScope={kbUpload.scope}
            defaultKnowledgeBaseId={kbUpload.knowledgeBaseId}
            onClose={() => setKbUpload(null)}
            onUploaded={onKbUploaded}
          />
        ) : null}
        {createOpen ? (
          <KbCreateDialog
            onClose={() => setCreateOpen(false)}
            onCreated={(kb) => {
              setCreateOpen(false);
              router.push(knowledgeBaseHref(kb.id));
            }}
          />
        ) : null}
      </KnowledgeBasesPage>
    );
  }
  return (
    // PageFrame (Header, Language & Paging Spec §2, §6 #15): shared header, underline tabs, 1120 column.
    <PageFrame
      title={t("title")}
      mobileActions={<PageMobileAction label={t("upload_button")} icon={Upload} onClick={onUpload} testId="kb-mobile-upload" />}
    >
      <div className="kb-page kb-page-v2">
        {returnToSetup ? (
          <div className="kb-v2-banner">
            <span>{setupReturnT("context")}</span>
            <button type="button" className="kb-v2-bannerlink" onClick={() => router.push("/setup")}>
              {setupReturnT("action")}
            </button>
          </div>
        ) : null}
        <PageHeader
          description={(
            <>
              {t(`nav_v2.description_${section}`)}
              {section === "files" && <> <a href={appPath("/files/")}>{t("nav_v2.library_files")}</a></>}
            </>
          )}
          // The Files view owns the page's primary action. Other tabs have their own primary
          // (New matrix, Run, New template), so the upload entry stays available as secondary.
          primary={section === "files" ? (
            <PageActionButton icon={Upload} data-testid="kb-head-upload" onClick={onUpload}>
              {t("upload_button")}
            </PageActionButton>
          ) : undefined}
          secondary={section === "files" ? undefined : (
            <PageActionButton variant="secondary" icon={Upload} data-testid="kb-head-upload" onClick={onUpload}>
              {t("upload_button")}
            </PageActionButton>
          )}
        />
        {degradedKbCaps.length > 0 && kbCaps && (
          <CapabilityNotice degraded={degradedKbCaps} caps={kbCaps} canSetup={Boolean(auth.me?.capabilities?.team_onboarding)} />
        )}
        <PageTabs
          label={t("nav_v2.nav_label")}
          testId="kb-nav"
          onNavigate={(item) => openSection(item.key as KbSection)}
          items={KB_SECTIONS.map((key) => ({
            key,
            label: t(`nav_v2.section_${key}`),
            href: sectionHref(key),
            current: key === section,
            count: counts[key],
            testId: `kb-nav-${key}`,
          }))}
        />
        {section === "files" ? <KbIndexNotice canManage={auth.isTeamAdmin} /> : null}
        <div className="kbshell">
          <div className="kbmain">
            {section === "files" ? (
              <KbFilesManager
                viewQuery={searchKey}
                initialFileId={fileIdParam}
                initialChunkId={chunkIdParam}
                initialScope={scopeParam}
                initialSpaceId={spaceParam}
                uploadRequest={uploadRequest}
              />
            ) : (
              // Switch off: resolveKbSection never yields "bases".
              <MatrixTab key={`${auth.me?.id}:${auth.me?.team_id}:${activeSpaceId}`} section={section as MatrixSectionKey} />
            )}
          </div>
        </div>
      </div>
    </PageFrame>
  );
}

/** Capability banner (vision, rerank, row extraction), shared by both page layouts. */
function CapabilityNotice({
  degraded,
  caps,
  canSetup,
}: {
  degraded: ReadonlyArray<"kb_vision" | "kb_rerank" | "kb_row_extractor">;
  caps: KbCapabilities;
  canSetup: boolean;
}) {
  const t = useTranslations("knowledge_base");
  const router = useAppRouter();
  return (
    <div className="kb-v2-banner" data-testid="kb-capability-notice">
      <span>{t("capability_notice.prefix")}</span>
      <span>
        {degraded
          .map((k) => t("capability_notice.item", {
            name: t(`capability_notice.names.${k}`),
            state: t(`capability_notice.states.${caps[k]}`),
          }))
          .join(t("capability_notice.separator"))}
      </span>
      {canSetup ? (
        <button
          type="button"
          className="kb-v2-bannerlink"
          data-testid="kb-capability-notice-setup"
          onClick={() => router.push("/setup")}
        >
          {t("capability_notice.view_setup")}
        </button>
      ) : (
        <span className="muted">{t("capability_notice.needs_admin")}</span>
      )}
    </div>
  );
}

/**
 * The Knowledge page with the knowledge base switch on (PRD §4.1): the directory tab and the three matrix tabs,
 * the header's Add files / New knowledge base, and the banners and notices of today's page.
 */
function KnowledgeBasesPage({
  title,
  section,
  redirecting,
  returnToSetup,
  capabilityNotice,
  tabs,
  description,
  isTeamAdmin,
  matrixKey,
  onUpload,
  onCreate,
  children,
}: {
  title: string;
  section: KbSection;
  /** A legacy file link is being moved to its knowledge base page. */
  redirecting: boolean;
  returnToSetup: boolean;
  capabilityNotice: ReactNode;
  tabs: ReactNode;
  description: string;
  isTeamAdmin: boolean;
  matrixKey: string;
  onUpload: () => void;
  onCreate: () => void;
  /** Dialogs owned by the page. */
  children: ReactNode;
}) {
  const t = useTranslations("knowledge_base");
  const tDir = useTranslations("knowledge_bases.directory");
  const setupReturnT = useTranslations("settings.setup_return");
  const router = useAppRouter();
  const onDirectory = section === "bases";
  const uploadLabel = tDir("add_files");
  return (
    <PageFrame
      title={title}
      mobileActions={(
        <>
          {onDirectory ? (
            <PageMobileAction label={tDir("new_knowledge_base")} icon={Plus} onClick={onCreate} testId="kb-mobile-new-kb" />
          ) : null}
          <PageMobileAction label={uploadLabel} icon={Upload} onClick={onUpload} testId="kb-mobile-upload" />
        </>
      )}
    >
      <div className="kb-page kb-page-v2 kbd-page">
        {returnToSetup ? (
          <div className="kb-v2-banner">
            <span>{setupReturnT("context")}</span>
            <button type="button" className="kb-v2-bannerlink" onClick={() => router.push("/setup")}>
              {setupReturnT("action")}
            </button>
          </div>
        ) : null}
        <PageHeader
          description={description}
          // The directory owns Add files as its primary action; the matrix tabs have their own primary, so
          // Add files stays available there as secondary.
          primary={onDirectory ? (
            <PageActionButton icon={Upload} data-testid="kb-head-upload" onClick={onUpload}>
              {uploadLabel}
            </PageActionButton>
          ) : undefined}
          secondary={onDirectory ? (
            <PageActionButton variant="secondary" icon={Plus} data-testid="kb-head-new-kb" onClick={onCreate}>
              {tDir("new_knowledge_base")}
            </PageActionButton>
          ) : (
            <PageActionButton variant="secondary" icon={Upload} data-testid="kb-head-upload" onClick={onUpload}>
              {uploadLabel}
            </PageActionButton>
          )}
        />
        {capabilityNotice}
        {tabs}
        {onDirectory ? <KbIndexNotice canManage={isTeamAdmin} /> : null}
        <div className="kbshell">
          <div className="kbmain">
            {onDirectory ? (
              redirecting ? (
                <LoadingSkeleton rows={5} label={t("loading")} />
              ) : (
                <KbDirectory />
              )
            ) : (
              <MatrixTab key={matrixKey} section={section as MatrixSectionKey} knowledgeBasesLayout />
            )}
          </div>
        </div>
      </div>
      {children}
    </PageFrame>
  );
}
