"use client";

/**
 * V7 Knowledge Base · /knowledge — KB management page (interaction design 2b).
 *
 * Left sidebar lists only content types: Files / Document Matrices / Extracted Results / Templates (?tab=files|matrix|records|templates).
 * Files is the kb-management file manager (scope, filters, search, bulk operations, upload); MatrixTab renders the other three.
 */

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { Upload } from "lucide-react";

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
import { KB_SECTIONS, type KbNavCounts, type KbSection, resolveKbSection } from "@/components/knowledge/kb-nav";
import { MatrixTab } from "@/components/knowledge/matrix/matrix-tab";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { requestSpaceAssignment } from "@/lib/auth-api";
import { fetchKbCapabilities, type KbCapabilities } from "@/lib/kb-capabilities-api";
import { listDocTypesWithStats } from "@/lib/kb-knowledge-tables-api";
import { listMatrices } from "@/lib/kb-matrix/snapshot-api";
import { listTeamTemplates } from "@/lib/kb-matrix/team-template-api";
import { templateViewsFrom, templatesListedCount } from "@/lib/kb-matrix/template-view";
import type { KbListScope } from "@/lib/kb-api";
import { useSpaceSelection } from "@/lib/use-space";
import { appPath } from "@/lib/base-path";
import { useAppRouter } from "@/lib/use-app-router";


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
  const section: KbSection = resolveKbSection(searchParams?.get("tab") ?? null, searchParams?.get("view") ?? null);
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

  // Sidebar counts (interaction design 05) count only listed objects: Document Matrices = my matrices; Extracted Results = extraction tables with results;
  // Templates = everything in Templates view (built-in / mine / extraction tables, plus team library). Space switch resets to placeholders;
  // same-space view switching silently refetches (counts track creation/deletion), without flashing placeholders. Space comes from useSpaceSelection:
  // cold load fetches only after space settles; counts follow space switches.
  const searchKey = searchParams?.toString() ?? "";
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
          records: templateViewsFrom(docTypes).length,
          templates: templatesListedCount(docTypes.items, team),
        });
      })
      .catch(() => { if (!cancelled) setNavCounts({ space, failed: true }); });
    return () => { cancelled = true; };
  }, [auth.status, activeSpaceId, activeSpaceIsAccessible, searchKey]);

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
    if (section !== "files") openSection("files");
    setUploadRequest((n) => n + 1);
  };
  const counts: KbNavCounts =
    navCounts === null
      ? { matrix: null, records: null, templates: null }
      : "failed" in navCounts
        ? {}
        : { matrix: navCounts.matrix, records: navCounts.records, templates: navCounts.templates };
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
          primary={(
            <PageActionButton icon={Upload} data-testid="kb-head-upload" onClick={onUpload}>
              {t("upload_button")}
            </PageActionButton>
          )}
        />
        {degradedKbCaps.length > 0 && kbCaps && (
          <div className="kb-v2-banner" data-testid="kb-capability-notice">
            <span>{t("capability_notice.prefix")}</span>
            <span>
              {degradedKbCaps
                .map((k) => t("capability_notice.item", {
                  name: t(`capability_notice.names.${k}`),
                  state: t(`capability_notice.states.${kbCaps[k]}`),
                }))
                .join(t("capability_notice.separator"))}
            </span>
            {auth.me?.capabilities?.team_onboarding ? (
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
              <MatrixTab key={`${auth.me?.id}:${auth.me?.team_id}:${activeSpaceId}`} section={section} />
            )}
          </div>
        </div>
      </div>
    </PageFrame>
  );
}
