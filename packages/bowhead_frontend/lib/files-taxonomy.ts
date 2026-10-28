/**
 * file-management-v2 P1 — Files-page taxonomy (PRD v2 §2.1 / §4.2.2).
 *
 * Sole authority: `kind` determines whether an artifact is an asset or run data.
 * User-visible classification reduces to 2 primary + 1 secondary dimensions:
 *   primary asset class (AssetClass here) · primary file type (mime_family) · secondary business category (category)
 * Raw `source` / `origin` enums are **no longer user-facing** (Data tab still consumes origin).
 */
import type { FileDTO } from "@/lib/api";

/** PRD §2.1: assets = user inputs + finished system outputs. */
export const ASSET_KINDS = ["user_upload", "deliverable", "canvas_export"] as const;
/** PRD §2.1: run data = byproducts of agent work (runtime exhaust). */
export const RUN_KINDS = ["tool_data", "sandbox_output", "web_clip"] as const;

export type FilesTab = "assets" | "run" | "trash";

/** Kinds requested by each tab; trash uses include_deleted rather than kind filtering. */
export function kindsForTab(tab: FilesTab): readonly string[] | undefined {
  if (tab === "assets") return ASSET_KINDS;
  if (tab === "run") return RUN_KINDS;
  return undefined;
}

/** Category-column badge types. `kb` takes priority: it is the file's current identity. */
export type AssetClass = "upload" | "result" | "export" | "kb";

/**
 * kind + in_kb → badge.
 *
 * Check `in_kb` first: a user-uploaded manual incorporated into KB becomes a KB file to the user,
 * rather than an upload; badges track identity (also the basis for unified deletion entry points, D2).
 */
export function assetClassOf(file: Pick<FileDTO, "kind" | "in_kb">): AssetClass {
  if (file.in_kb) return "kb";
  switch (file.kind) {
    case "deliverable":
      return "result";
    case "canvas_export":
      return "export";
    default:
      return "upload";
  }
}

/**
 * Category chip → list query, filtered server-side before pagination (previously filtered current page, leaving a few rows but unchanged page count).
 * Same rule as assetClassOf: KB membership takes priority over other categories.
 */
export function assetClassQuery(want: AssetClass | "all"): { kind: readonly string[]; in_kb?: 0 | 1 } {
  switch (want) {
    case "all":
      return { kind: ASSET_KINDS };
    case "kb":
      return { kind: ASSET_KINDS, in_kb: 1 };
    case "result":
      return { kind: ["deliverable"], in_kb: 0 };
    case "export":
      return { kind: ["canvas_export"], in_kb: 0 };
    case "upload":
      return { kind: ASSET_KINDS.filter((k) => k !== "deliverable" && k !== "canvas_export"), in_kb: 0 };
  }
}

/**
 * Drawer Source: humanize internal source values.
 * Hard constraint: raw English enums must never appear anywhere user-visible (PRD §4.2.2).
 */
export function humanSource(file: Pick<FileDTO, "kind" | "source">): string {
  switch (file.kind) {
    case "user_upload":
      return "userUpload";
    case "deliverable":
      return "aiGenerated";
    case "canvas_export":
      return "reportExport";
    case "web_clip":
      return "webClip";
    case "sandbox_output":
      return "sandbox";
    default:
      return "toolRun";
  }
}

export const MIME_FAMILIES = [
  "all",
  "presentation",
  "pdf",
  "spreadsheet",
  "document",
  "text",
  "image",
  "other",
] as const;

export const SORT_OPTIONS = ["created_desc", "last_used_desc", "size_desc"] as const;

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** Truncate the middle, preserving extension (design midTruncate). */
export function midTruncate(name: string, max = 34): string {
  if (name.length <= max) return name;
  return `${name.slice(0, max - 12)}…${name.slice(-11)}`;
}
