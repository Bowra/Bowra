"use client";

/**
 * Old extraction-table management address. Extraction tables now live in the knowledge base's left-side Templates view (interaction design 2b); retain this route for static export,
 * solely to redirect bookmarks and old links there.
 */

import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function KnowledgeDocTypesRedirectPage() {
  const router = useAppRouter();
  useEffect(() => {
    router.replace("/knowledge?tab=templates");
  }, [router]);
  return <div aria-busy="true" />;
}
