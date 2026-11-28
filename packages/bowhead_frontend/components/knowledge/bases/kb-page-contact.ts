/**
 * Whom to ask about a knowledge base you cannot change (FEATURE_KNOWLEDGE_BASES, PRD §4.11 "Use only" and empty
 * states): the values of the ICU `who` select (+ `name`) in `knowledge_bases.page.use_only_notice` and
 * `knowledge_bases.files.empty_viewer_body`.
 */
import type { KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";

export type KnowledgeBaseContactWho = "owner" | "space_admin" | "team_admin" | "other";

/** Its owner by name; else the automatic managers of a default (Space or Team admins); else its managers. */
export function knowledgeBaseContact(
  kb: Pick<KnowledgeBaseDTO, "owner" | "is_default" | "home">,
): { who: KnowledgeBaseContactWho; name: string } {
  if (kb.owner?.name) return { who: "owner", name: kb.owner.name };
  if (kb.is_default && kb.home === "space") return { who: "space_admin", name: "" };
  if (kb.is_default && kb.home === "team") return { who: "team_admin", name: "" };
  return { who: "other", name: "" };
}
