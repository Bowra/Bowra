"use client";

/**
 * Old extraction-table management address. Extraction tables now live in the knowledge base's left-side Templates view (interaction design 2b); retain this route for static export,
 * solely to redirect bookmarks and old links there. With knowledge bases enabled (FEATURE_KNOWLEDGE_BASES), extraction
 * tables moved to the Extraction tables tab (`?tab=records`, PRD Q1), so the redirect waits for the session to know the switch.
 */

import { useEffect } from "react";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useAppRouter } from "@/lib/use-app-router";

export default function KnowledgeDocTypesRedirectPage() {
  const router = useAppRouter();
  const session = useOptionalAuthSession();
  // Without a session provider (or once it settles) the switch is known; while it loads, wait.
  const settled = !session || session.status !== "loading";
  const knowledgeBases = session?.me?.capabilities?.knowledge_bases === true;
  useEffect(() => {
    if (!settled) return;
    router.replace(knowledgeBases ? "/knowledge?tab=records" : "/knowledge?tab=templates");
  }, [router, settled, knowledgeBases]);
  return <div aria-busy="true" />;
}
