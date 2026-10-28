import type { SessionGroupDTO } from "./api";
import type { SessionGroup } from "./types";

/**
 * Normalize the Gateway session list once for every sidebar presentation.
 * Gateway keeps snake_case protocol fields; the UI model uses camelCase.
 */
export function adaptSessionGroups(dtos: SessionGroupDTO[]): SessionGroup[] {
  return dtos.map((group) => ({
    id: group.id,
    label: group.label,
    items: group.items.map((item) => ({
      id: item.id,
      title: item.title,
      intents: item.intents ?? [],
      time: item.time ?? "",
      updatedAt: item.updated_at ?? null,
      state: item.state,
      unread: item.unread,
      pinned: item.pinned,
      archived: item.archived,
      skillApplied: item.skill_applied,
    })),
  }));
}

/**
 * Merge paged session lists the way the Gateway pages them: cursor pages reuse
 * the first page's group ids, so items are appended to the group with the same
 * id (new ids append a group), and the first occurrence of a session id wins.
 * Callers pass the freshest page first so its copy of a session is the one kept.
 */
export function mergeSessionGroups(
  ...pages: readonly (readonly SessionGroup[])[]
): SessionGroup[] {
  const merged: SessionGroup[] = [];
  const byId = new Map<string, SessionGroup>();
  const seen = new Set<string>();
  for (const groups of pages) {
    for (const group of groups) {
      let target = byId.get(group.id);
      if (!target) {
        target = { id: group.id, label: group.label, items: [] };
        byId.set(group.id, target);
        merged.push(target);
      }
      for (const item of group.items) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        target.items.push(item);
      }
    }
  }
  return merged;
}
