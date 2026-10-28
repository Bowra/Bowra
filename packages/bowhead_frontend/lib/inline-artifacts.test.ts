/**
 * Phase 1.6 G4 — `extractInlineArtifacts` defensive narrowing.
 *
 * Backend ships well-shaped entries via `_collect_inline_artifacts`; this
 * test pins the FE behavior under malformed / partial input so a wire-shape
 * drift doesn't crash the chat bubble render.
 */

import { describe, expect, it } from "vitest";

import { extractInlineArtifacts } from "./inline-artifacts";

describe("extractInlineArtifacts", () => {
  it("returns [] for null / undefined / non-object", () => {
    expect(extractInlineArtifacts(undefined)).toEqual([]);
    expect(extractInlineArtifacts(null)).toEqual([]);
    expect(extractInlineArtifacts("string")).toEqual([]);
    expect(extractInlineArtifacts(42)).toEqual([]);
  });

  it("returns [] when inline_artifacts field is missing", () => {
    expect(extractInlineArtifacts({ exec_summary: "x" })).toEqual([]);
  });

  it("returns [] when inline_artifacts is not an array", () => {
    expect(extractInlineArtifacts({ inline_artifacts: "nope" })).toEqual([]);
    expect(extractInlineArtifacts({ inline_artifacts: { 0: "x" } })).toEqual([]);
  });

  it("narrows chart entry shape", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "chart", id: "sec_1", spec: { type: "bar", x: ["a"] } },
      ],
    });
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("chart");
    expect(out[0].id).toBe("sec_1");
    if (out[0].kind === "chart") {
      expect(out[0].spec).toEqual({ type: "bar", x: ["a"] });
    }
  });

  it("narrows kpi entry shape", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "kpi",
          id: "sec_2",
          data: { id: "sec_2", kind: "kpi_row", cells: [{ label: "GMV" }] },
        },
      ],
    });
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("kpi");
    if (out[0].kind === "kpi") {
      expect(out[0].data.kind).toBe("kpi_row");
    }
  });

  it("narrows table entry shape", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "table",
          id: "sec_3",
          data: {
            id: "sec_3",
            kind: "data_table",
            caption: "Top 5",
            columns: [{ key: "k" }],
            rows: [],
          },
        },
      ],
    });
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("table");
    if (out[0].kind === "table") {
      expect(out[0].data.caption).toBe("Top 5");
    }
  });

  it("narrows file (deliverable) entry shape — pptx with slides", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "file",
          id: "art_1",
          filename: "consultant-deck.pptx",
          file_type: "PPTX",
          mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          download_url: "/api/v1/files/art_1",
          size_bytes: 78000,
          slide_count: 7,
        },
      ],
    });
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("file");
    if (out[0].kind === "file") {
      expect(out[0].data.fileType).toBe("PPTX");
      expect(out[0].data.downloadUrl).toBe("/api/v1/files/art_1");
      expect(out[0].data.sizeBytes).toBe(78000);
      expect(out[0].data.slideCount).toBe(7);
    }
  });

  it("narrows file entry for any type (xlsx) without slides", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "file",
          id: "art_2",
          filename: "财务模型.xlsx",
          file_type: "XLSX",
          download_url: "/api/v1/files/art_2",
          size_bytes: 4096,
        },
      ],
    });
    expect(out).toHaveLength(1);
    if (out[0].kind === "file") {
      expect(out[0].data.fileType).toBe("XLSX");
      expect(out[0].data.slideCount).toBeUndefined();
      expect(out[0].data.filename).toBe("财务模型.xlsx");
    }
  });

  it("derives file_type from extension when backend omits it", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "file", id: "art_3", filename: "report.PDF", download_url: "/api/v1/files/art_3" },
      ],
    });
    expect(out).toHaveLength(1);
    if (out[0].kind === "file") {
      expect(out[0].data.fileType).toBe("PDF");  // upper-cased extension fallback
    }
  });

  it("drops file entry missing filename or download_url", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "file", id: "no_name", download_url: "/api/v1/files/x" },  // no filename
        { kind: "file", id: "no_url", filename: "a.pptx" },  // no download_url
        { kind: "file", id: "ok", filename: "a.pptx", download_url: "/api/v1/files/ok" },
      ],
    });
    expect(out.map((a) => a.id)).toEqual(["ok"]);
  });

  it("drops entries with no id", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "chart", spec: {} },  // missing id
        { kind: "chart", id: "", spec: {} },  // empty id
        { kind: "chart", id: "good", spec: {} },
      ],
    });
    expect(out.map((a) => a.id)).toEqual(["good"]);
  });

  it("drops chart entry missing spec", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "chart", id: "bad" },  // no spec
        { kind: "chart", id: "good", spec: { type: "line" } },
      ],
    });
    expect(out.map((a) => a.id)).toEqual(["good"]);
  });

  it("drops unknown kinds and malformed image entries", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        // image is a valid kind (multimodal-v1), but missing url/download_url is malformed → drop
        { kind: "image", id: "x", data: {} },
        { kind: "hologram", id: "y" },  // Truly unknown kind → drop
        { kind: "chart", id: "ok", spec: {} },
      ],
    });
    expect(out.map((a) => a.id)).toEqual(["ok"]);
  });

  it("narrows image (raster deliverable) entries — multimodal-v1", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "image",
          id: "art_img_000000001",
          url: "/api/v1/files/art_img_000000001?disposition=inline",
          download_url: "/api/v1/files/art_img_000000001",
          filename: "gen_海报_1.png",
          mime: "image/png",
          alt: "夏季促销海报",
          size_bytes: 245000,
        },
      ],
    });
    expect(out).toHaveLength(1);
    const a = out[0];
    expect(a.kind).toBe("image");
    if (a.kind === "image") {
      expect(a.data.url).toContain("disposition=inline");
      expect(a.data.downloadUrl).toBe("/api/v1/files/art_img_000000001");
      expect(a.data.filename).toBe("gen_海报_1.png");
      expect(a.data.sizeBytes).toBe(245000);
    }
  });

  it("rejects image entries with non-artifact URLs (exfil guard)", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        {
          kind: "image",
          id: "art_evil",
          url: "https://evil.example.com/x.png",
          download_url: "/api/v1/files/art_evil",
          filename: "x.png",
        },
      ],
    });
    expect(out).toHaveLength(0);
  });

  it("preserves order from backend (seq)", () => {
    const out = extractInlineArtifacts({
      inline_artifacts: [
        { kind: "chart", id: "a", spec: {} },
        { kind: "kpi", id: "b", data: { id: "b", kind: "kpi_row", cells: [] } },
        { kind: "table", id: "c", data: { id: "c", kind: "data_table" } },
      ],
    });
    expect(out.map((a) => a.id)).toEqual(["a", "b", "c"]);
  });
});
