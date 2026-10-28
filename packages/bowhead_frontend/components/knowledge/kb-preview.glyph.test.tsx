import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import type { KbFileDTO } from "@/lib/kb-api";

import { KbPreview } from "./kb-preview";

// The browser build maps Mammoth's unzip to an ArrayBuffer reader; Node resolves the source entry,
// which rejects { arrayBuffer } internally and leaves an unhandled rejection behind. This case only
// needs a conversion failure, so fail the conversion directly.
vi.mock("mammoth", () => ({
  convertToHtml: () => Promise.reject(new Error("not a DOCX archive")),
}));

const docx = {
  file_id: "kbf_docx",
  filename: "contract.docx",
  mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
} as KbFileDTO;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KbPreview download glyph", () => {
  it("compact failure: → after the download button", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3, 4]))));
    renderIntl(<KbPreview file={docx} onClose={vi.fn()} />);
    const message = catalogText("zh-CN", "knowledge_base.preview.download_to_view");
    const button = await screen.findByRole("button", { name: (name) => name.startsWith(message) });
    expectRefreshedGlyph(button, "→", "end", message);
  });
});
