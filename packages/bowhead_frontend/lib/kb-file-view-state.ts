import type { KbListScope } from "./kb-api";

export interface KbFileViewState {
  /** "all" (every readable scope) is offered by the scope filter. */
  scope: KbListScope;
  query: string;
  deep: boolean;
  status: string[];
  types: string[];
  days: number | null;
  sort: "uploadedAt" | "name" | "sizeMB";
  order: "asc" | "desc";
  page: number;
}

/** URL view state; `scope` is null when the URL names no scope (the caller decides: keep the current one, or personal). */
export type KbFileViewFromUrl = Omit<KbFileViewState, "scope"> & { scope: KbListScope | null };

export function readKbFileView(search: string): KbFileViewFromUrl {
  const p = new URLSearchParams(search);
  const scope = p.get("scope");
  const sort = p.get("sort");
  const page = Number(p.get("page") || 1);
  const pick = (key: string, allowed: string[]) => [...new Set((p.get(key) || "").split(",").filter((v) => allowed.includes(v)))];
  return {
    scope: scope === "space" || scope === "workspace" ? "space" : scope === "team" ? "team" : scope === "all" ? "all" : scope === "personal" ? "personal" : null,
    query: p.get("q") || "",
    deep: p.get("deep") === "1",
    status: pick("status", ["processing", "done", "failed"]),
    types: pick("type", ["pdf", "doc", "sheet", "slide", "image"]),
    days: [7, 30, 90].includes(Number(p.get("days"))) ? Number(p.get("days")) : null,
    sort: sort === "name" || sort === "sizeMB" ? sort : "uploadedAt",
    order: p.get("order") === "asc" ? "asc" : "desc",
    page: Number.isSafeInteger(page) && page > 0 ? page : 1,
  };
}

/** Preserve navigation and source parameters; only replace owned view keys. */
export function writeKbFileView(search: string, view: KbFileViewState): URLSearchParams {
  const p = new URLSearchParams(search);
  const values = {
    scope: view.scope, q: view.query, deep: view.deep ? "1" : "",
    status: [...view.status].sort().join(","), type: [...view.types].sort().join(","),
    days: view.days?.toString() || "", sort: view.sort, order: view.order,
    page: view.page > 1 ? String(view.page) : "",
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) p.set(key, value); else p.delete(key);
  }
  return p;
}
