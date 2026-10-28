/**
 * Phase 1.6 G4 — type-narrowing helper for `lead.final.payload.inline_artifacts`.
 *
 * Backend `_collect_inline_artifacts` shapes each entry to match
 * `ChatInlineArtifact` exactly. This util defends against malformed entries
 * (replay drift, partial backends, JSON corruption) by dropping bad rows
 * rather than crashing the render.
 */

import type { ChatInlineArtifact } from "@/components/chat/step-list/ChatBubbleNew";
import { appPath } from "@/lib/base-path";

/**
 * Narrow `lead.final.payload` (unknown) to a typed list of
 * `ChatInlineArtifact`. Returns [] when payload is missing / shape is
 * wrong / no inline_artifacts field.
 */
export function extractInlineArtifacts(
  finalPayload: unknown,
): ChatInlineArtifact[] {
  if (!finalPayload || typeof finalPayload !== "object") return [];
  const raw = (finalPayload as Record<string, unknown>).inline_artifacts;
  if (!Array.isArray(raw)) return [];
  const out: ChatInlineArtifact[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as Record<string, unknown>;
    const id = typeof e.id === "string" ? e.id : "";
    if (!id) continue;
    if (e.kind === "chart" && e.spec && typeof e.spec === "object") {
      out.push({
        kind: "chart",
        id,
        spec: e.spec as Extract<ChatInlineArtifact, { kind: "chart" }>["spec"],
      });
    } else if (e.kind === "kpi" && e.data && typeof e.data === "object") {
      out.push({
        kind: "kpi",
        id,
        data: e.data as Extract<ChatInlineArtifact, { kind: "kpi" }>["data"],
      });
    } else if (e.kind === "table" && e.data && typeof e.data === "object") {
      out.push({
        kind: "table",
        id,
        data: e.data as Extract<ChatInlineArtifact, { kind: "table" }>["data"],
      });
    } else if (
      e.kind === "image" &&
      typeof e.url === "string" &&
      typeof e.download_url === "string" &&
      // Defense: accept only same-origin artifact paths (the backend's sole valid generated shape), blocking
      // payload drift/hallucination pointing <img src> off-origin (cookie-carrying GET exfiltration surface).
      e.url.startsWith("/api/v1/files/") &&
      e.download_url.startsWith("/api/v1/files/")
    ) {
      // multimodal-v1 — raster deliverable inline preview (url carries
      // ?disposition=inline; download_url is the attachment route).
      out.push({
        kind: "image",
        id,
        // chat-inline-figure: [figure:] anchors use canvas section ids (sec_*), whereas
        // image id is the artifact id (art_*, used by ChatInlineImage to open Results).
        // Backend also supplies section_id; pass it through as an alias so anchors resolve image figures.
        ...(typeof e.section_id === "string" && e.section_id
          ? { sectionId: e.section_id }
          : {}),
        data: {
          id,
          filename: typeof e.filename === "string" ? e.filename : "image.png",
          mime: typeof e.mime === "string" ? e.mime : undefined,
          url: appPath(e.url),
          downloadUrl: appPath(e.download_url),
          alt: typeof e.alt === "string" ? e.alt : undefined,
          sizeBytes: typeof e.size_bytes === "number" ? e.size_bytes : undefined,
        },
      });
    } else if (
      e.kind === "file" &&
      typeof e.filename === "string" &&
      typeof e.download_url === "string"
    ) {
      // P5a deliverable download chip. Map the backend snake_case shape onto the
      // file variant's `data`; filename + download_url are load-bearing, the rest
      // are cosmetic (file_type defaults from the extension, size/slides optional).
      // Mirror the backend `_file_type_label` cap (1-5 alnum) so this drift-only
      // fallback can't render a 15-char badge from an odd filename.
      const extMatch = e.filename.match(/\.([a-z0-9]{1,5})$/i);
      const ext = extMatch ? extMatch[1].toUpperCase() : "FILE";
      out.push({
        kind: "file",
        id,
        data: {
          id,
          filename: e.filename,
          fileType: typeof e.file_type === "string" ? e.file_type : ext,
          mime: typeof e.mime === "string" ? e.mime : undefined,
          downloadUrl: appPath(e.download_url),
          sizeBytes: typeof e.size_bytes === "number" ? e.size_bytes : undefined,
          slideCount: typeof e.slide_count === "number" ? e.slide_count : undefined,
        },
      });
    }
  }
  return out;
}
