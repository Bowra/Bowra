/**
 * Knowledge bases: display names, labels and addresses (organization phase 1, slice 4).
 *
 * Default knowledge bases store no name; the interface renders it from the home: My → "My knowledge",
 * Space → the Space's name, Team → "Team knowledge base" (PRD §2, four locales under
 * `knowledge_bases.common`).
 */
import type { KnowledgeBaseAudience, KnowledgeBaseDTO, KnowledgeBaseHome, KnowledgeBaseLevel } from "./knowledge-bases-api";

/** `useTranslations("knowledge_bases.common")`. */
export type CommonTranslator = (key: string, values?: Record<string, string | number>) => string;

export type KnowledgeBaseNameSource = Pick<KnowledgeBaseDTO, "is_default" | "home" | "home_id" | "name">;

/** The name users see; `spaceName(id)` resolves a Space's name for its default knowledge base. */
export function knowledgeBaseName(
  kb: KnowledgeBaseNameSource,
  t: CommonTranslator,
  spaceName?: (spaceId: string) => string | null | undefined,
): string {
  if (!kb.is_default && kb.name) return kb.name;
  if (kb.home === "personal") return t("default_name_personal");
  if (kb.home === "team") return t("default_name_team");
  return spaceName?.(kb.home_id) || t("default_name_space");
}

export type AudienceLabelKey = "audience_owner" | "audience_members" | "audience_space_use" | "audience_space_edit" | "audience_team";

/** The "who can use it" label of a knowledge base (its audience and, for "all Space members", the default level). */
export function audienceLabelKey(kb: { audience: KnowledgeBaseAudience; audience_level: "use" | "edit" }): AudienceLabelKey {
  if (kb.audience === "owner") return "audience_owner";
  if (kb.audience === "members") return "audience_members";
  if (kb.audience === "team") return "audience_team";
  return kb.audience_level === "edit" ? "audience_space_edit" : "audience_space_use";
}

/** The caller's level label: owners see "Owner"; automatic managers and listed managers "Can manage". */
export function levelLabelKey(kb: Pick<KnowledgeBaseDTO, "level" | "owner">, userId: string | null | undefined): string {
  if (kb.owner && userId && kb.owner.user_id === userId) return "level_owner";
  return `level_${kb.level}`;
}

export function isKnowledgeBaseOwner(kb: Pick<KnowledgeBaseDTO, "owner">, userId: string | null | undefined): boolean {
  return Boolean(kb.owner && userId && kb.owner.user_id === userId);
}

export function levelKey(level: KnowledgeBaseLevel): string {
  return `level_${level}`;
}

/** The home label used for grouping (My / the Space's name / Team). */
export function homeLabel(home: KnowledgeBaseHome, t: CommonTranslator, spaceName?: string | null): string {
  if (home === "personal") return t("home_personal");
  if (home === "team") return t("home_team");
  return spaceName || t("home_space");
}

export type KnowledgeBasePageTab = "files" | "members" | "settings";

export const KNOWLEDGE_BASE_PAGE_TABS: readonly KnowledgeBasePageTab[] = ["files", "members", "settings"];

/** The knowledge base page tab named in the address; anything else opens Files. */
export function resolveKnowledgeBasePageTab(raw: string | null | undefined): KnowledgeBasePageTab {
  return raw === "members" || raw === "settings" ? raw : "files";
}

/**
 * The knowledge base page address (static export: one page, the ID in the query). Pass it to `useAppRouter()`
 * as is, or through `appPath()` for an anchor. Opening it grants nothing (PRD §4.2).
 */
export function knowledgeBaseHref(
  id: string,
  opts: { tab?: KnowledgeBasePageTab; fileId?: string | null; chunkId?: string | null } = {},
): string {
  const qs = new URLSearchParams({ id });
  if (opts.tab && opts.tab !== "files") qs.set("tab", opts.tab);
  if (opts.fileId) qs.set("file_id", opts.fileId);
  if (opts.chunkId) qs.set("chunk_id", opts.chunkId);
  return `/knowledge/base/?${qs.toString()}`;
}

/** Opens the knowledge base a knowledge file is in (resolved on the page) with the file selected. */
export function knowledgeBaseFileHref(fileId: string, chunkId?: string | null): string {
  const qs = new URLSearchParams({ file_id: fileId });
  if (chunkId) qs.set("chunk_id", chunkId);
  return `/knowledge/base/?${qs.toString()}`;
}

/** The directory (Knowledge page, Knowledge bases tab). */
export const KNOWLEDGE_DIRECTORY_HREF = "/knowledge/";
