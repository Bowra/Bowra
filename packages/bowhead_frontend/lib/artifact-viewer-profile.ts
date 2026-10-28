import type { ResultsViewerMode } from "./results-workspace-route";

export type ArtifactViewerProfile =
  | "html"
  | "image"
  | "video"
  | "table"
  | "json"
  | "markdown"
  | "text"
  | "pdf"
  | "docx"
  | "pptx"
  | "xlsx"
  | "unsupported";

const HTML_MIMES = new Set(["text/html", "application/xhtml+xml"]);
const VIDEO_MIMES = new Set(["video/mp4", "video/webm", "video/ogg"]);
const TABLE_MIMES = new Set(["text/csv", "text/tab-separated-values"]);
const JSON_MIMES = new Set([
  "application/json",
  "application/ld+json",
  "application/x-ndjson",
  "application/jsonl",
]);
const MARKDOWN_MIMES = new Set([
  "text/markdown",
  "text/x-markdown",
]);

const PROFILE_MODES: Record<ArtifactViewerProfile, readonly ResultsViewerMode[]> = {
  html: ["preview", "raw", "details"],
  image: ["preview", "details"],
  video: ["preview", "details"],
  table: ["preview", "raw", "details"],
  json: ["preview", "raw", "details"],
  markdown: ["preview", "raw", "details"],
  text: ["preview", "raw", "details"],
  pdf: ["preview", "details"],
  docx: ["preview", "details"],
  pptx: ["preview", "details"],
  xlsx: ["preview", "details"],
  unsupported: ["details"],
};

export function deriveArtifactViewerProfile(
  mime: string | null | undefined,
  filename = "",
): ArtifactViewerProfile {
  const normalized = (mime ?? "").split(";", 1)[0]?.trim().toLowerCase() ?? "";

  if (HTML_MIMES.has(normalized)) return "html";
  // SVG is active XML content. It must not enter the ordinary raster image path.
  if (normalized === "image/svg+xml") return "unsupported";
  if (normalized.startsWith("image/")) return "image";
  if (VIDEO_MIMES.has(normalized)) return "video";
  if (TABLE_MIMES.has(normalized)) return "table";
  if (JSON_MIMES.has(normalized) || normalized.endsWith("+json")) return "json";
  if (MARKDOWN_MIMES.has(normalized)) return "markdown";
  if (normalized === "application/pdf") return "pdf";
  if (normalized.includes("wordprocessingml")) return "docx";
  if (normalized.includes("presentationml") || normalized === "application/vnd.ms-powerpoint") {
    return "pptx";
  }
  if (
    normalized.includes("spreadsheetml")
    || normalized === "application/vnd.ms-excel"
  ) {
    return "xlsx";
  }
  if (normalized.startsWith("text/") || ["application/xml", "application/yaml", "application/x-yaml",
    "application/javascript", "application/x-javascript", "application/typescript", "application/x-python"].includes(normalized)) return "text";

  // Historical generic MIME rows can recover only inert, readable profiles.
  // Active HTML/SVG and binary document renderers never activate from suffix alone.
  if (normalized === "application/octet-stream" || normalized === "") {
    const suffix = filename.toLowerCase().split(".").pop();
    if (suffix === "csv" || suffix === "tsv") return "table";
    if (suffix === "json" || suffix === "jsonl" || suffix === "ndjson") return "json";
    if (suffix === "md" || suffix === "markdown") return "markdown";
    if (["txt", "log", "sql", "py", "js", "ts", "css", "xml", "yaml", "yml"].includes(suffix ?? "")) {
      return "text";
    }
  }

  return "unsupported";
}

export function viewerModesForProfile(
  profile: ArtifactViewerProfile,
): readonly ResultsViewerMode[] {
  return PROFILE_MODES[profile];
}

export function normalizeViewerMode(
  requested: ResultsViewerMode,
  modes: readonly ResultsViewerMode[],
): ResultsViewerMode {
  if (modes.includes(requested)) return requested;
  return modes[0] ?? "details";
}

export function previewUsesTextDto(profile: ArtifactViewerProfile): boolean {
  // PDF/DOCX use the bounded server extractor as an immutable-artifact
  // validation gate before the browser loads a heavyweight binary renderer.
  return ["table", "json", "markdown", "text", "pdf", "docx", "pptx", "xlsx"].includes(profile);
}
