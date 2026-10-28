/**
 * Single source of supported knowledge-base upload formats.
 *
 * File-picker accept, selection/drop guards, drop guidance and unsupported-format error lists
 * all derive here instead of duplicating lists (old accept and validateFile had separate lists; adding .pptx
 * updated only one, making selectable files get rejected).
 *
 * Must align with backend `routes/kb.py::_ALLOWED_MIMES` + `_resolve_kb_mime`:
 * - Include .pptx only, not .ppt (python-pptx cannot read legacy binary PowerPoint).
 * - .csv uses backend CsvLoader (table splitting matches xlsx, PRD §10 Q5).
 */

export type KbUploadFormatId =
  | "pdf"
  | "word"
  | "ppt"
  | "excel"
  | "csv"
  | "markdown"
  | "txt"
  | "image"
  | "zip";

export const KB_UPLOAD_FORMATS: ReadonlyArray<{
  id: KbUploadFormatId;
  exts: readonly string[];
}> = [
  { id: "pdf", exts: [".pdf"] },
  { id: "word", exts: [".docx"] },
  { id: "ppt", exts: [".pptx"] },
  { id: "excel", exts: [".xlsx", ".xls"] },
  { id: "csv", exts: [".csv"] },
  { id: "markdown", exts: [".md", ".markdown"] },
  { id: "txt", exts: [".txt"] },
  { id: "image", exts: [".jpg", ".jpeg", ".png", ".webp"] },
  // 0.24.4 (kb-zip-bundle): server extracts and ingests files individually.
  { id: "zip", exts: [".zip"] },
];

/** `<input accept>` value. */
export const KB_ACCEPTED_EXT = KB_UPLOAD_FORMATS.flatMap((f) => f.exts).join(",");

/** Per-file cap 200 MB (zip supports the actual 119MB case). */
export const KB_MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

export function isKbAcceptedFileName(name: string): boolean {
  const lower = name.toLowerCase();
  return KB_UPLOAD_FORMATS.some((f) => f.exts.some((ext) => lower.endsWith(ext)));
}
