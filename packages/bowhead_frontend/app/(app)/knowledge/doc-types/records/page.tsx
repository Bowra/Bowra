"use client";

/**
 * Old extraction-results address (/knowledge/doc-types/records/?dt=…). Results now live in the knowledge base's left-side Extraction Results view
 * (interaction design 2b); retain this route for static export, solely to redirect bookmarks and old links to the corresponding extraction table's results.
 */

import { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";

import { useAppRouter } from "@/lib/use-app-router";

export default function KnowledgeRecordsRedirectPage() {
  return (
    <Suspense fallback={<div aria-busy="true" />}>
      <RecordsRedirect />
    </Suspense>
  );
}

function RecordsRedirect() {
  const router = useAppRouter();
  const docTypeId = useSearchParams()?.get("dt") ?? "";
  const route = docTypeId
    ? `/knowledge?tab=records&view=tpl_${encodeURIComponent(docTypeId)}`
    : "/knowledge?tab=records";
  useEffect(() => {
    router.replace(route);
  }, [route, router]);
  return <div aria-busy="true" />;
}
