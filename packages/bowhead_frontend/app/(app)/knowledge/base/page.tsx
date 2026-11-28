"use client";

/**
 * /knowledge/base/ — one knowledge base's page (FEATURE_KNOWLEDGE_BASES, PRD §4.2).
 *
 * Static export: one page with the knowledge base in the query (`?id=<kbase_…>&tab=files|members|settings
 * [&file_id][&chunk_id]`), or a file-only link (`?file_id=X[&chunk_id=Y][&space=Z]`) that the page resolves to the
 * file's knowledge base. `KbPage` owns loading, not-found and the tabs; this route only reads the address.
 */

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { PageStateHeading } from "@/components/shell/PageStateHeading";
import { PageFrame } from "@/components/shell/page-frame";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { KbPage } from "@/components/knowledge/bases/kb-page";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { resolveKnowledgeBasePageTab } from "@/lib/knowledge-base-display";

export default function KnowledgeBaseRoutePage() {
  const t = useTranslations("knowledge_bases.common");
  return (
    <Suspense fallback={<div className="legacy-kb-page-loading">{t("loading")}</div>}>
      <KnowledgeBaseRoute />
    </Suspense>
  );
}

function KnowledgeBaseRoute() {
  const t = useTranslations("knowledge_base");
  const tc = useTranslations("knowledge_bases.common");
  const searchParams = useSearchParams();
  const auth = useAuthRedirect();

  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <PageFrame title={t("title")}>
        <div className="legacy-kb-page-loading">
          <PageStateHeading>{t("title")}</PageStateHeading>
          <LoadingSkeleton rows={5} label={tc("loading")} />
        </div>
      </PageFrame>
    );
  }

  return (
    <KbPage
      knowledgeBaseId={searchParams?.get("id") || null}
      tab={resolveKnowledgeBasePageTab(searchParams?.get("tab"))}
      fileId={searchParams?.get("file_id") || null}
      chunkId={searchParams?.get("chunk_id") || null}
      spaceId={searchParams?.get("space") || null}
      viewQuery={searchParams?.toString() ?? ""}
    />
  );
}
