/**
 * kb-management · file-type icon meta (tag + color + label) derived from filename.
 * Shared by the files table and the bundle upload list.
 *
 * i18n: this is a non-React .ts module (no useTranslations). label is resolved
 * per-call via staticTranslator("kb_file_type") against the current UI locale.
 * typeMeta()/typeMetaKey is evaluated at render time, so the label is locale-reactive.
 */
import { staticTranslator } from "@/lib/locale-messages";

export interface TypeMeta { tag: string; color: string; label: string }

type LabelKey = "document" | "sheet" | "slide" | "image" | "file";

function labelFor(key: LabelKey): string {
  return staticTranslator("kb_file_type")(key);
}

interface TypeMetaRaw { tag: string; color: string; labelKey: LabelKey }

const TYPE_META: Record<string, TypeMetaRaw> = {
  pdf: { tag: "PDF", color: "#B43A2A", labelKey: "document" },
  docx: { tag: "DOC", color: "#3F6F8C", labelKey: "document" },
  doc: { tag: "DOC", color: "#3F6F8C", labelKey: "document" },
  md: { tag: "MD", color: "#3F6F8C", labelKey: "document" },
  markdown: { tag: "MD", color: "#3F6F8C", labelKey: "document" },
  txt: { tag: "TXT", color: "#3F6F8C", labelKey: "document" },
  xlsx: { tag: "XLS", color: "#5B7A4F", labelKey: "sheet" },
  xls: { tag: "XLS", color: "#5B7A4F", labelKey: "sheet" },
  csv: { tag: "CSV", color: "#5B7A4F", labelKey: "sheet" },
  pptx: { tag: "PPT", color: "#D88A3C", labelKey: "slide" },
  ppt: { tag: "PPT", color: "#D88A3C", labelKey: "slide" },
  png: { tag: "IMG", color: "#9A938E", labelKey: "image" },
  jpg: { tag: "IMG", color: "#9A938E", labelKey: "image" },
  jpeg: { tag: "IMG", color: "#9A938E", labelKey: "image" },
  webp: { tag: "IMG", color: "#9A938E", labelKey: "image" },
};

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function typeMeta(name: string): TypeMeta {
  const raw = TYPE_META[extOf(name)];
  if (raw) return { tag: raw.tag, color: raw.color, label: labelFor(raw.labelKey) };
  return { tag: (extOf(name) || "DOC").toUpperCase().slice(0, 3), color: "#9A938E", label: labelFor("file") };
}
