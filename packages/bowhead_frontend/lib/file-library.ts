import type { FileDTO } from "./api";
import { apiJson } from "./api-client";

export const libraryViews = ["deliverables", "uploads", "run", "all"] as const;
export type LibraryView = (typeof libraryViews)[number] | "trash";
export interface LibraryFile extends FileDTO {
  delivered_at: string | null;
  recover_until: string | null;
  views: LibraryView[];
  lineage_id: string;
  kb_known: boolean;
  uncertain_upload?: boolean;
  kb_artifact_id?: string | null;
  pinned: boolean;
  kept: boolean;
  retention_reason?: string;
  retention_days?: number | null;
}
export type LibraryPage = { items: LibraryFile[]; next_cursor: string | null };
export type LibrarySummary = {
  counts: Partial<Record<LibraryView, number>>;
  bytes: Partial<Record<"assets" | "run" | "trash", number>>;
};
const base = "/api/v1/files/library";
export function libraryRead<T>(suffix: string, signal?: AbortSignal) {
  return apiJson<T>(base + suffix, { cache: "no-store", signal });
}
export function keepLibraryVersion(file: LibraryFile) {
  return apiJson(base + "/items/" + encodeURIComponent(file.file_id) + "/keep", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kept: !file.kept }),
  });
}
