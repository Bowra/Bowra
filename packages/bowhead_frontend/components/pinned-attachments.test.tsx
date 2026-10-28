/**
 * chat-file-download #2 — PinnedAttachmentsStrip now renders `AttachmentChip`
 * chips (shared download state machine with the sent-message attachment strip
 * / ChatInlineFile) instead of bespoke read-only markup. This was the one
 * surface with zero download affordance at all; lock in that a dedicated
 * `.v2-chip__dl` button downloads AND the × still unpins independently.
 *
 * review fix: because this strip needs BOTH click-to-download and click-to-
 * unpin on the same chip, the outer `.v2-chip` is NOT itself a role="button"
 * click target here (that would nest a real <button> — the × — inside
 * another interactive element). Only the sent-message strip (read-only, no
 * unpin) makes the whole card clickable; see attachment-chip.test.tsx for
 * that shape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { PinnedAttachmentsStrip } from "./pinned-attachments";
import * as api from "@/lib/api";

import messages from "@/messages/en-US.json";

const i18nWrap = (children: React.ReactNode) => (
  <NextIntlClientProvider locale="en-US" messages={messages}>
    {children}
  </NextIntlClientProvider>
);

function streamResponse(body: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-length": String(encoder.encode(body).length) },
  });
}

describe("PinnedAttachmentsStrip", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("URL", Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.spyOn(api, "fetchPinnedAttachments").mockResolvedValue([
      { id: "art_1", filename: "capex_model.xlsx", mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", size: 4096 },
    ]);
    vi.spyOn(api, "unpinAttachmentFromSession").mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    delete window.__BOWHEAD_BASE_PATH__;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a downloadable AttachmentChip with a dedicated download button, not a whole-card click target", async () => {
    const { container, findByText } = render(i18nWrap(<PinnedAttachmentsStrip sessionId="s_1" />));
    await findByText("capex_model.xlsx");
    const chip = container.querySelector(".v2-chip");
    expect(chip?.className).not.toContain("v2-chip--downloadable");
    expect(chip?.getAttribute("role")).toBeNull();
    const dl = container.querySelector(".v2-chip__dl") as HTMLButtonElement;
    expect(dl.tagName).toBe("BUTTON");
    expect(dl.getAttribute("aria-label")).toBe("Download capex_model.xlsx");
  });

  it("clicking the download button downloads the file (fetch called with same-origin files URL)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(streamResponse("data"));
    const { container, findByText } = render(i18nWrap(<PinnedAttachmentsStrip sessionId="s_1" />));
    await findByText("capex_model.xlsx");
    const dl = container.querySelector(".v2-chip__dl") as HTMLElement;
    act(() => { fireEvent.click(dl); });
    // Same-origin downloads go through the shared authenticated transport (credentials + abort signal).
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/files/art_1"),
      expect.objectContaining({ credentials: "include", signal: expect.any(AbortSignal) }),
    ));
  });

  it.each(["", "/bowhead", "/ai/apps/bowhead"])("hands large downloads to the browser on entry %s", async (prefix) => {
    window.__BOWHEAD_BASE_PATH__ = prefix;
    vi.mocked(api.fetchPinnedAttachments).mockResolvedValueOnce([
      { id: "art_1", filename: "large.csv", mime: "text/csv", size: 20 * 1024 * 1024 },
    ]);
    const { container, findByText } = render(i18nWrap(<PinnedAttachmentsStrip sessionId="s_1" />));
    await findByText("large.csv");
    fireEvent.click(container.querySelector(".v2-chip__dl")!);
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce());
    const anchor = vi.mocked(HTMLAnchorElement.prototype.click).mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.getAttribute("href")).toBe(`${prefix}/api/v1/files/art_1`);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("clicking × unpins without triggering a download", async () => {
    const { container, findByText } = render(i18nWrap(<PinnedAttachmentsStrip sessionId="s_1" />));
    await findByText("capex_model.xlsx");
    const x = container.querySelector(".v2-chip__x") as HTMLButtonElement;
    expect(x.getAttribute("aria-label")).toBe(
      "Unpin (artifact stays, no longer injected into future turns)",
    );
    act(() => { fireEvent.click(x); });
    await waitFor(() => expect(api.unpinAttachmentFromSession).toHaveBeenCalledWith("s_1", "art_1"));
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("download button and unpin × are independent, flat siblings — no nested interactive elements", async () => {
    const { container, findByText } = render(i18nWrap(<PinnedAttachmentsStrip sessionId="s_1" />));
    await findByText("capex_model.xlsx");
    const chip = container.querySelector(".v2-chip") as HTMLElement;
    const dl = chip.querySelector(".v2-chip__dl") as HTMLElement;
    const x = chip.querySelector(".v2-chip__x") as HTMLElement;
    // both are direct children of the chip, not nested inside each other
    expect(dl.parentElement).toBe(chip);
    expect(x.parentElement).toBe(chip);
    expect(dl.contains(x)).toBe(false);
    expect(x.contains(dl)).toBe(false);
  });
});
