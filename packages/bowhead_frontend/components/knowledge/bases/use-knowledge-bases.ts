"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES) hooks: the switch, the shared directory read of the current
 * Space and the display-name resolver. Everything new renders only when `useKnowledgeBasesEnabled()` is true;
 * with the switch off today's Knowledge page, Files dialogs, upload dialog and file details stay unchanged.
 */
import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { knowledgeBaseName, type KnowledgeBaseNameSource } from "@/lib/knowledge-base-display";
import { knowledgeBaseDirectoryResource, type KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";
import { useSharedResource } from "@/lib/shared-resource";
import { useSpaceSelection } from "@/lib/use-space";

/** `/me` `capabilities.knowledge_bases`; false until the session is known (fail closed to today's pages). */
export function useKnowledgeBasesEnabled(): boolean {
  return useOptionalAuthSession()?.me?.capabilities?.knowledge_bases === true;
}

export interface KnowledgeBaseDirectoryState {
  directory: KnowledgeBaseDirectory | null;
  /** First load in flight (a background refresh keeps the previous directory on screen). */
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

/** The directory of the current Space (My, this Space, Team), shared by every reader on the page. */
export function useKnowledgeBaseDirectory({ enabled = true }: { enabled?: boolean } = {}): KnowledgeBaseDirectoryState {
  const { activeId, loading: spacesLoading } = useSpaceSelection();
  const key = enabled && !spacesLoading ? (activeId ?? "") : null;
  const snapshot = useSharedResource(knowledgeBaseDirectoryResource, key);
  return {
    directory: snapshot.data ?? null,
    loading: key === null || snapshot.status === "idle" || snapshot.loading,
    error: snapshot.error,
    refresh: snapshot.refresh,
  };
}

/** Resolves the name users see, including default knowledge bases named after their home. */
export function useKnowledgeBaseNamer(): (kb: KnowledgeBaseNameSource) => string {
  const t = useTranslations("knowledge_bases.common");
  const { items } = useSpaceSelection();
  return useCallback(
    (kb: KnowledgeBaseNameSource) => knowledgeBaseName(kb, t, (spaceId) => items.find((space) => space.id === spaceId)?.name ?? null),
    [t, items],
  );
}
