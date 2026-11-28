import { listFiles, type FileDTO } from "@/lib/api";
import { readApiError } from "@/lib/api-client";
import { appFetch } from "@/lib/base-path";
import type { ComposerDraftScope } from "@/lib/composer-ordinary-draft";
import { ASSET_KINDS } from "@/lib/files-taxonomy";
import { downloadKbFileUrl, fetchKbFiles, type KbFileDTO } from "@/lib/kb-api";
import { spaceHeader } from "@/lib/space-storage";

export const COMPOSER_LIBRARY_SCOPES = ["recent", "team", "space", "mine"] as const;
export type LibraryScope = (typeof COMPOSER_LIBRARY_SCOPES)[number];

interface LibraryFileId {
  scope: Exclude<LibraryScope, "recent">;
  id: string;
}

export interface ComposerLibraryFile extends LibraryFileId {
  filename: string;
  mime: string;
  size: number;
  date: string;
}

const RECENT_LIMIT = 8;
export const LIBRARY_PAGE_SIZE = 20;

export function libraryFileKey({ scope, id }: LibraryFileId): string {
  return `${scope}:${id}`;
}

function recentKey({ userId, teamId, workspaceId }: ComposerDraftScope): string {
  return `bowhead.composer-library.recent.v1:${JSON.stringify([userId, teamId, workspaceId])}`;
}

function readRecent(context: ComposerDraftScope): LibraryFileId[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(recentKey(context)) ?? "[]");
    return Array.isArray(value)
      ? value
          .slice(0, RECENT_LIMIT)
          .filter(
            (item): item is LibraryFileId =>
              item != null &&
              (item.scope === "team" || item.scope === "space" || item.scope === "mine") &&
              typeof item.id === "string" &&
              item.id.length > 0 &&
              item.id.length <= 128,
          )
      : [];
  } catch {
    return [];
  }
}

export function rememberComposerLibraryFile(context: ComposerDraftScope, file: LibraryFileId): void {
  const recent = readRecent(context).filter((item) => libraryFileKey(item) !== libraryFileKey(file));
  try {
    localStorage.setItem(
      recentKey(context),
      JSON.stringify([{ scope: file.scope, id: file.id }, ...recent].slice(0, RECENT_LIMIT)),
    );
  } catch {
    // Draft attachments remain available when browser storage is disabled.
  }
}

function knowledgeFile(file: KbFileDTO): ComposerLibraryFile | null {
  if ((file.scope !== "team" && file.scope !== "space") || file.source_health?.state === "paused") return null;
  return {
    scope: file.scope,
    id: file.file_id,
    filename: file.filename,
    mime: file.mime_type,
    size: file.size_bytes,
    date: file.uploaded_at,
  };
}

function ownedFile(file: FileDTO): ComposerLibraryFile {
  return {
    scope: "mine",
    id: file.file_id,
    filename: file.filename,
    mime: file.mime,
    size: file.size,
    date: file.created_at,
  };
}

export async function browseComposerLibrary(
  context: ComposerDraftScope,
  scope: LibraryScope,
  query: string,
  offset: number,
  signal: AbortSignal,
): Promise<{ files: ComposerLibraryFile[]; hasMore: boolean }> {
  const init = { headers: spaceHeader(context), signal, cache: "no-store" as const };
  if (scope === "recent" && !query) {
    const recent = readRecent(context);
    const knowledgeIds = recent.filter((item) => item.scope !== "mine").map((item) => item.id);
    const [knowledge, owned] = await Promise.all([
      knowledgeIds.length > 0
        ? fetchKbFiles({ scope: "all", file_ids: knowledgeIds, status: "done", limit: RECENT_LIMIT }, init)
        : null,
      Promise.all(
        recent
          .filter((item) => item.scope === "mine")
          .map(async ({ id }) =>
            (await listFiles({ q: id, kind: ASSET_KINDS, limit: 1 }, init)).items.find((item) => item.file_id === id),
          ),
      ),
    ]);
    const files = new Map(
      [
        ...(knowledge?.files.map(knowledgeFile).filter((file) => file !== null) ?? []),
        ...owned.filter((file) => file !== undefined).map(ownedFile),
      ].map((file) => [libraryFileKey(file), file]),
    );
    return { files: recent.flatMap((item) => files.get(libraryFileKey(item)) ?? []), hasMore: false };
  }
  const list = { q: query, offset, limit: LIBRARY_PAGE_SIZE };
  if (scope === "mine") {
    const result = await listFiles({ ...list, kind: ASSET_KINDS }, init);
    return { files: result.items.map(ownedFile), hasMore: offset + LIBRARY_PAGE_SIZE < result.total };
  }
  const [result, owned] = await Promise.all([
    fetchKbFiles({ ...list, scope: scope === "recent" ? "all" : scope, status: "done" }, init),
    scope === "recent" ? listFiles({ ...list, kind: ASSET_KINDS }, init) : null,
  ]);
  return {
    files: [...result.files.map(knowledgeFile).filter((file) => file !== null), ...(owned?.items.map(ownedFile) ?? [])],
    hasMore:
      (result.has_more ?? offset + LIBRARY_PAGE_SIZE < result.total) ||
      offset + LIBRARY_PAGE_SIZE < (owned?.total ?? 0),
  };
}

export async function readComposerLibraryFile(
  context: ComposerDraftScope,
  file: ComposerLibraryFile,
  signal: AbortSignal,
): Promise<File> {
  const path =
    file.scope === "mine" ? `/api/v1/files/${encodeURIComponent(file.id)}?transport=proxy` : downloadKbFileUrl(file.id);
  const response = await appFetch(path, {
    headers: spaceHeader(context),
    signal,
    cache: "no-store",
    redirect: "error",
  });
  if (!response.ok) throw await readApiError(response);
  const bytes = await response.blob();
  if (bytes.size !== file.size) throw new Error("The selected file changed.");
  return new File([bytes], file.filename, { type: file.mime, lastModified: Date.parse(file.date) || 0 });
}
