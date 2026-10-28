/**
 * An "@Agent request" input document for a Team or system Agent, resolved through the composer's own
 * reference catalog: the same exact, currently authorized reference the Owner would pick from the @ menu.
 * Used by Use this Agent (a fresh chat) and by an alignment card telling Coach to continue.
 */
import type { ComposerPersistenceScope } from "@/lib/composer-persistence-scope";
import { parseInputDocument, type InputDocument } from "@/lib/composer-input-document";
import { randomUuid } from "@/lib/random-id";
import { getReferenceCatalog } from "@/lib/reference-catalog-api";

/** `null` when the Agent isn't in the catalog or isn't usable right now; throws when the catalog can't be read. */
export async function resolveAgentReferenceDocument(
  scope: ComposerPersistenceScope,
  agent: { agentId: string; label: string; text?: string },
  signal: AbortSignal,
): Promise<InputDocument | null> {
  // Labels can change between the source page and this chat. Search by label first, then scan the
  // current catalog by stable Agent ID.
  for (const query of [agent.label, ""]) {
    let cursor: string | null = null;
    do {
      const result = await getReferenceCatalog(scope, { trigger: "@", query, entryMode: "ordinary", selectedRefs: [] }, signal, cursor);
      const item = result.groups.flatMap((group) => group.items).find((candidate) => (
        candidate.kind === "agent"
        && (candidate.source_kind === "team" || candidate.source_kind === "system")
        && candidate.id === agent.agentId
      ));
      if (item?.kind === "agent") {
        if (item.availability !== "baseline" || !item.resource) return null;
        return parseInputDocument({
          schema_version: 1,
          nodes: [
            { type: "reference", node_id: randomUuid(), resource: item.resource, label: item.label, parameters: {} },
            { type: "text", text: agent.text ? ` ${agent.text}` : " " },
          ],
        });
      }
      cursor = result.next_cursor;
    } while (cursor && !signal.aborted);
    if (signal.aborted) return null;
  }
  return null;
}
