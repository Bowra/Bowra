import { describe, expect, it } from "vitest";

import {
  deriveArtifactViewerProfile,
  normalizeViewerMode,
  viewerModesForProfile,
} from "./artifact-viewer-profile";

describe("artifact viewer profile", () => {
  it.each([
    ["text/html", "html"],
    ["image/png", "image"],
    ["image/svg+xml", "unsupported"],
    ["video/mp4", "video"],
    ["text/csv", "table"],
    ["application/problem+json", "json"],
    ["text/markdown", "markdown"],
    ["text/plain; charset=utf-8", "text"],
    ["application/pdf", "pdf"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"],
    ["application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"],
    ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"],
    ["application/zip", "unsupported"],
  ])("maps %s to %s", (mime, profile) => {
    expect(deriveArtifactViewerProfile(mime)).toBe(profile);
  });

  it("only uses suffix fallback for inert generic files", () => {
    expect(deriveArtifactViewerProfile("application/octet-stream", "rows.csv")).toBe("table");
    expect(deriveArtifactViewerProfile("application/octet-stream", "app.html")).toBe("unsupported");
    expect(deriveArtifactViewerProfile("application/octet-stream", "drawing.svg")).toBe("unsupported");
  });

  it("exposes only representations that actually exist", () => {
    expect(viewerModesForProfile("image")).toEqual([
      "preview",
      "details",
    ]);
    expect(viewerModesForProfile("pptx")).toEqual([
      "preview",
      "details",
    ]);
    expect(viewerModesForProfile("unsupported")).toEqual([
      "details",
    ]);
    expect(viewerModesForProfile("html")).toEqual([
      "preview",
      "raw",
      "details",
    ]);
    expect(viewerModesForProfile("video")).toEqual(["preview", "details"]);
    for (const profile of ["pdf", "docx", "xlsx"] as const) {
      expect(viewerModesForProfile(profile)).toEqual(["preview", "details"]);
    }
    for (const mime of ["application/xml", "application/yaml", "application/javascript"]) {
      expect(deriveArtifactViewerProfile(mime)).toBe("text");
    }
  });

  it("normalizes an invalid route mode to the first supported mode", () => {
    expect(normalizeViewerMode("raw", ["preview", "details"])).toBe("preview");
    expect(normalizeViewerMode("preview", ["details"])).toBe("details");
  });
});
