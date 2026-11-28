/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): small helpers shared by the directory, the New and Delete dialogs.
 */
import { ApiError } from "@/lib/api-client";
import { kbFileError, type KbFileErrorKey } from "@/lib/kb-file-error";
import type { KnowledgeBaseDirectory, KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";

/** `useTranslations("knowledge_base")`, which holds the `errors.*` messages. */
type KbTranslator = (key: KbFileErrorKey) => string;

/**
 * The user-facing text of a knowledge base API failure: the finite code's message when the server named one
 * (or the transport/status explanation), else `fallback` — never raw server text. `kbFileError`'s own
 * fallback names files, not knowledge bases, so it is replaced by the caller's.
 */
export function knowledgeBaseErrorText(error: unknown, t: KbTranslator, fallback: string): string {
  const key = kbFileError(error, "load");
  return key === "errors.load_failed" ? fallback : t(key);
}

/** The current file count a 409 `knowledge_base_files_changed` reports, or null for any other failure. */
export function filesChangedCount(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.code !== "knowledge_base_files_changed") return null;
  const body = error.body as { detail?: { error?: { file_count?: unknown } }; error?: { file_count?: unknown } } | null | undefined;
  const raw = body?.detail?.error?.file_count ?? body?.error?.file_count ?? error.details?.file_count;
  const count = typeof raw === "number" ? raw : Number(raw);
  return Number.isInteger(count) && count >= 0 ? count : null;
}

/** Where "Add files" adds from the directory (PRD §4.7): the current Space's default, else My knowledge. */
export function defaultUploadTarget(directory: KnowledgeBaseDirectory | null | undefined): KnowledgeBaseDTO | null {
  if (!directory) return null;
  return (
    directory.sections.space?.find((kb) => kb.is_default)
    ?? directory.sections.personal?.find((kb) => kb.is_default)
    ?? null
  );
}

/** Characters as the server counts them (code points), for the name and description limits. */
export function textLength(value: string): number {
  return [...value].length;
}

export const KNOWLEDGE_BASE_NAME_MAX = 120;
export const KNOWLEDGE_BASE_DESCRIPTION_MAX = 1000;
