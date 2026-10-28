/**
 * File-type line icons for attachments and deliverables (Chat Cards Spec D10: no coloured "PPT / XLS" text
 * blocks — a Lucide line icon, with the extension written into the meta line instead).
 */
import {
  File,
  FileArchive,
  FileImage,
  FileSpreadsheet,
  FileText,
  Presentation,
  type LucideIcon,
} from "lucide-react";

export function fileExtension(filename: string | null | undefined): string {
  // Persisted attachments and live events can arrive without a name.
  if (typeof filename !== "string") return "";
  return filename.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1] ?? "";
}

/** Upper-case extension for the meta line ("PPTX"); "ZIP" for bundles; empty when unknown. */
export function fileExtensionLabel(filename: string | null | undefined, kind: "file" | "bundle" = "file"): string {
  if (kind === "bundle") return "ZIP";
  return fileExtension(filename).toUpperCase();
}

export function fileTypeIcon(filename: string | null | undefined, mime = "", kind: "file" | "bundle" = "file"): LucideIcon {
  const ext = fileExtension(filename);
  if (kind === "bundle" || ["zip", "rar", "7z", "tar", "gz"].includes(ext) || mime.includes("zip")) return FileArchive;
  if (["ppt", "pptx", "key", "odp"].includes(ext) || mime.includes("presentation")) return Presentation;
  if (["xls", "xlsx", "csv", "tsv", "ods"].includes(ext) || mime.includes("spreadsheet") || mime === "text/csv" || mime === "application/vnd.ms-excel") {
    return FileSpreadsheet;
  }
  if (["png", "jpg", "jpeg", "webp", "gif", "svg", "heic", "bmp"].includes(ext) || mime.startsWith("image/")) return FileImage;
  if (["pdf", "doc", "docx", "md", "markdown", "txt", "rtf", "html", "htm", "json"].includes(ext) || mime.startsWith("text/") || mime === "application/pdf" || mime.includes("wordprocessing")) {
    return FileText;
  }
  return File;
}
