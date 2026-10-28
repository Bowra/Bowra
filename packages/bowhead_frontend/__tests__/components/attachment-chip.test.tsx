/**
 * Sprint file-upload-presign Phase 3 — AttachmentChip state rendering.
 *
 * Chat Cards Spec §5 (D10/D11): line icon per file type (no coloured type blocks), one meta fact per
 * state, 28px inner buttons, bundle contents in the shared menu.
 *
 * chat-file-download #2 adds a 7th concern: a finished chip carrying
 * `downloadUrl` becomes a download trigger (shared `useFileDownload` state
 * machine with ChatInlineFile) — independent of whether `onRemove` (unpin/
 * dismiss) is also present, since the pinned-attachments strip needs both
 * on the same chip.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import {
  AttachmentChip,
  type AttachmentItem,
} from "@/components/chat/attachment-chip";

import messages from "@/messages/en-US.json";

const i18nWrap = (children: React.ReactNode) => (
  <NextIntlClientProvider locale="en-US" messages={messages}>
    {children}
  </NextIntlClientProvider>
);

function makeItem(overrides: Partial<AttachmentItem>): AttachmentItem {
  return {
    localId: "att_1",
    filename: "report.pdf",
    size: 2_500_000,
    mime: "application/pdf",
    status: "done",
    ...overrides,
  };
}

// ── State 1: small file ready ────────────────────────────────────

describe("AttachmentChip — state 1 (small ready)", () => {
  it("exposes current selected scope and the whole-file qualification in the existing chip", () => {
    const selectedContext = { version: 1 as const, sectionId: "sec", sectionRev: 1, viewId: "v", bindingId: "b", datasetDigest: "a".repeat(64), measureKeys: ["revenue"], tuples: [["2026-02"]] };
    const { container, getByText } = render(i18nWrap(<AttachmentChip item={makeItem({ selectedContext })} />));
    expect(getByText('Selected: [["2026-02"]]')).toBeTruthy();
    expect(container.querySelector(".v2-chip")!.getAttribute("title")).toContain("The attached file may include more data");
  });
  it("renders a line icon for the type, no progress bar and no spinner", () => {
    const { container } = render(
      i18nWrap(<AttachmentChip item={makeItem({})} onRemove={() => {}} />),
    );
    const chip = container.querySelector(".v2-chip");
    expect(chip).toBeTruthy();
    expect(container.querySelector(".v2-chip__bar")).toBeNull();
    expect(container.querySelector(".ui-card__spinner")).toBeNull();
    expect(container.querySelector(".v2-chip__icon svg")?.getAttribute("class")).toContain("lucide-file-text");
    // No coloured "PDF" text block.
    expect(chip?.textContent).not.toContain("PDF report");
  });

  it("writes the extension and size into the meta", () => {
    const { container } = render(
      i18nWrap(<AttachmentChip item={makeItem({})} onRemove={() => {}} />),
    );
    const meta = container.querySelector(".v2-chip__meta");
    expect(meta?.textContent).toBe("PDF · 2.4 MB");
  });

  it("uses the presentation and spreadsheet icons", () => {
    const { container } = render(
      i18nWrap(
        <>
          <AttachmentChip item={makeItem({ localId: "a", filename: "deck.pptx", mime: "" })} />
          <AttachmentChip item={makeItem({ localId: "b", filename: "sales.xlsx", mime: "" })} />
        </>,
      ),
    );
    const icons = Array.from(container.querySelectorAll(".v2-chip__icon svg")).map((svg) => svg.getAttribute("class"));
    expect(icons[0]).toContain("lucide-presentation");
    expect(icons[1]).toContain("lucide-file-spreadsheet");
  });

  it("outlines itself once when the same file is added again", () => {
    const { container } = render(
      i18nWrap(<AttachmentChip item={makeItem({})} onRemove={() => {}} flash />),
    );
    expect(container.querySelector(".v2-chip-wrap")?.className).toContain("is-flash");
  });
});

// ── State 2: large file uploading ────────────────────────────────

describe("AttachmentChip — state 2 (uploading)", () => {
  it("renders chip--uploading with bar + percentage", () => {
    const { container, getByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "uploading", progressPct: 62 })}
          onRemove={() => {}}
        />,
      ),
    );
    const chip = container.querySelector(".v2-chip");
    expect(chip?.className).toContain("v2-chip--uploading");
    expect((chip as HTMLElement).style.getPropertyValue("--chip-progress")).toBe("62%");
    expect(container.querySelector(".v2-chip__bar")).toBeTruthy();
    expect(getByText("62%")).toBeTruthy();
  });
});

// ── State 3: confirming ──────────────────────────────────────────

describe("AttachmentChip — state 3 (confirming)", () => {
  it("renders spinner instead of percentage", () => {
    const { container, queryByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "confirming", progressPct: 100 })}
          onRemove={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".ui-card__spinner")).toBeTruthy();
    // No percentage text in confirming
    expect(queryByText("100%")).toBeNull();
  });
});

// ── State 4: bundle uploading ────────────────────────────────────

describe("AttachmentChip — state 4 (bundle uploading)", () => {
  it("renders the archive icon and the progress bar", () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({
            kind: "bundle",
            filename: "finance_pack.zip",
            mime: "application/zip",
            status: "uploading",
            progressPct: 38,
          })}
          onRemove={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip__icon svg")?.getAttribute("class")).toContain("lucide-file-archive");
    expect(container.querySelector(".v2-chip__bar")).toBeTruthy();
  });
});

// ── State 5: bundle indexing ─────────────────────────────────────

describe("AttachmentChip — state 5 (bundle indexing)", () => {
  it("writes the indexing progress into the meta and offers the contents menu", () => {
    const { container, getByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({
            kind: "bundle",
            filename: "bundle.zip",
            mime: "application/zip",
            status: "indexing",
            indexedCount: 12,
            totalCount: 58,
            bundleChildren: [
              { filename: "a.csv", size: 1000, status: "done" },
              { filename: "b.csv", size: 2000, status: "busy" },
            ],
          })}
          onRemove={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip__meta")?.textContent).toBe("Indexing 12 of 58");
    const chev = container.querySelector(".v2-chip__chev");
    expect(chev?.getAttribute("aria-haspopup")).toBe("menu");
    expect(chev?.querySelector("svg")?.getAttribute("class")).toContain("lucide-chevron-down");
  });

  it("indexing chip with no children hides the chevron", () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({
            kind: "bundle",
            mime: "application/zip",
            status: "indexing",
            indexedCount: 5,
            totalCount: 10,
            // no bundleChildren → no popover possible → no chevron
          })}
          onRemove={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip__chev")).toBeNull();
  });
});

// ── State 6: error variants ──────────────────────────────────────

describe("AttachmentChip — state 6 (error variants)", () => {
  it("size error → red variant, NO retry pill", () => {
    const { container, queryByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "error", errorKind: "size" })}
          onRemove={() => {}}
          onRetry={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip--error")).toBeTruthy();
    expect(container.querySelector(".v2-chip--warning")).toBeNull();
    expect(queryByText(/retry/i)).toBeNull();
  });

  it("auth error → amber variant + Sign in retry pill", () => {
    const { container, getByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "error", errorKind: "auth" })}
          onRemove={() => {}}
          onRetry={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip--warning")).toBeTruthy();
    expect(getByText("Sign in")).toBeTruthy();
  });

  it("network error → amber variant + Retry pill", () => {
    const { container, getByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "error", errorKind: "network" })}
          onRemove={() => {}}
          onRetry={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip--warning")).toBeTruthy();
    expect(getByText(/Retry/)).toBeTruthy();
  });

  it("unsupported error → red variant, terminal", () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "error", errorKind: "unsupported" })}
          onRemove={() => {}}
          onRetry={() => {}}
        />,
      ),
    );
    expect(container.querySelector(".v2-chip--error")).toBeTruthy();
    expect(container.querySelector(".v2-chip__retry")).toBeNull();
  });
});

// ── Disclosure popover ──────────────────────────────────────────

describe("AttachmentChip — bundle children popover", () => {
  it("keeps completed Composer bundle details available after indexing", () => {
    const item = makeItem({
      kind: "bundle",
      filename: "bundle.zip",
      mime: "application/zip",
      status: "indexing",
      indexedCount: 0,
      totalCount: 1,
      bundleChildren: [
        { filename: "north.txt", size: 40, status: "busy" },
        { filename: "skipped.bin", size: 0, status: "skipped" },
      ],
    });
    const { getByRole, getByText, rerender, queryByRole } = render(
      i18nWrap(<AttachmentChip item={item} onRemove={() => {}} />),
    );
    rerender(i18nWrap(<AttachmentChip item={{
      ...item,
      status: "done",
      indexedCount: 1,
      bundleChildren: [
        { filename: "north.txt", size: 40, status: "done" },
        { filename: "skipped.bin", size: 0, status: "skipped" },
      ],
    }} onRemove={() => {}} />));

    expect(getByText("1 indexed · 1 failed")).toBeTruthy();
    fireEvent.click(getByRole("button", { name: "Show bundle contents" }));
    const menu = getByRole("menu", { name: "Show bundle contents" });
    expect(menu).toBeTruthy();
    // Failures come first.
    const rows = Array.from(menu.querySelectorAll('[role="menuitem"]')).map((row) => row.textContent);
    expect(rows).toEqual(["skipped.binSkipped", "north.txtIndexed"]);
    expect(getByText("north.txt")).toBeTruthy();
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(queryByRole("menu")).toBeNull();
  });

  it("closed initially; opens on the trigger and collapses more than six indexed files", async () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({
            kind: "bundle",
            mime: "application/zip",
            status: "indexing",
            indexedCount: 8,
            totalCount: 9,
            bundleChildren: [
              ...Array.from({ length: 8 }, (_, i) => ({ filename: `f${i}.csv`, size: 1000, status: "done" as const })),
              { filename: "beta.xlsx", size: 2000, status: "busy" },
            ],
          })}
          onRemove={() => {}}
        />,
      ),
    );
    expect(document.querySelector(".ui-card-bundle-menu")).toBeNull();
    const chev = container.querySelector(".v2-chip__chev") as HTMLButtonElement;
    act(() => { fireEvent.click(chev); });
    const menu = document.querySelector(".ui-card-bundle-menu") as HTMLElement;
    expect(menu).toBeTruthy();
    // Three of the eight indexed files, then "5 more indexed"; a file still indexing is counted on the chip only.
    expect(menu.querySelectorAll('[role="menuitem"]').length).toBe(3);
    expect(menu.textContent).toContain("5 more indexed");
    expect(menu.textContent).not.toContain("beta.xlsx");
    expect(menu.textContent).toContain("9 files");
  });
});

// ── chat-file-download #2: download affordance ──────────────────

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

describe("AttachmentChip — download affordance (chat-file-download #2)", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("URL", Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

  it("read-only done chip with downloadUrl is clickable (role=button, download aria-label)", () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip item={makeItem({ downloadUrl: "/api/v1/files/art_1" })} />,
      ),
    );
    const chip = container.querySelector(".v2-chip");
    expect(chip?.className).toContain("v2-chip--downloadable");
    expect(chip?.getAttribute("role")).toBe("button");
    expect(chip?.getAttribute("aria-label")).toBe("Download report.pdf");
  });

  it("read-only done chip WITHOUT downloadUrl stays non-interactive (no behavior change)", () => {
    const { container } = render(i18nWrap(<AttachmentChip item={makeItem({})} />));
    const chip = container.querySelector(".v2-chip");
    expect(chip?.className).not.toContain("v2-chip--downloadable");
    expect(chip?.getAttribute("role")).toBeNull();
  });

  it("Results Workspace makes the card open the object and keeps download as a separate button", () => {
    const onOpen = vi.fn();
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ downloadUrl: "/api/v1/files/art_1" })}
          onOpen={onOpen}
          openLabel="Open report.pdf in results"
        />,
      ),
    );

    const chip = container.querySelector(".v2-chip") as HTMLElement;
    expect(chip.className).toContain("v2-chip--openable");
    expect(chip.getAttribute("role")).toBeNull();
    const open = container.querySelector(
      ".v2-chip__open-hit",
    ) as HTMLButtonElement;
    expect(open.getAttribute("aria-label")).toBe(
      "Open report.pdf in results",
    );
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(fetchMock()).not.toHaveBeenCalled();

    const download = container.querySelector(
      ".v2-chip__dl",
    ) as HTMLButtonElement;
    expect(download.tagName).toBe("BUTTON");
    expect(download.getAttribute("aria-label")).toBe("Download report.pdf");
    expect(download.parentElement).toBe(open.parentElement);
  });

  it("clicking the chip downloads → downloading rail → done, meta text updates", async () => {
    // Deferred resolution so the "downloading" mid-state is deterministically
    // observable (an immediately-resolved mock can race past it before assert).
    let resolveFetch!: (v: Response) => void;
    fetchMock().mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveFetch = resolve; }),
    );
    const { container, getByText } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ downloadUrl: "/api/v1/files/art_1", size: 11 })}
        />,
      ),
    );
    const chip = container.querySelector(".v2-chip") as HTMLElement;
    act(() => { fireEvent.click(chip); });
    await waitFor(() => expect(chip.className).toContain("v2-chip--dl-downloading"));
    expect(container.querySelector(".v2-chip__bar")).toBeTruthy();

    act(() => resolveFetch(streamResponse("hello world")));
    await waitFor(() => expect(chip.className).toContain("v2-chip--dl-done"));
    expect(getByText("Downloaded")).toBeTruthy();
  });

  it("download HTTP failure → error state, meta shows retry hint", async () => {
    fetchMock().mockResolvedValueOnce(new Response(null, { status: 404 }));
    const { container, getByText } = render(
      i18nWrap(<AttachmentChip item={makeItem({ downloadUrl: "/api/v1/files/art_gone" })} />),
    );
    const chip = container.querySelector(".v2-chip") as HTMLElement;
    act(() => { fireEvent.click(chip); });
    await waitFor(() => expect(chip.className).toContain("v2-chip--dl-error"));
    expect(getByText(/Download failed/)).toBeTruthy();
  });

  it("pinned-strip shape: downloadUrl + onRemove coexist as flat siblings (no nested <button>-in-role=button)", async () => {
    const onRemove = vi.fn();
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ downloadUrl: "/api/v1/files/art_1" })}
          onRemove={onRemove}
          removeLabel="Unpin"
        />,
      ),
    );
    const chip = container.querySelector(".v2-chip") as HTMLElement;
    // review fix: the whole card is NOT a click target when onRemove is also
    // present — that would nest a real <button> (×) inside a role="button"
    // card. Download gets its own dedicated button instead.
    expect(chip.className).not.toContain("v2-chip--downloadable");
    expect(chip.getAttribute("role")).toBeNull();

    const dl = container.querySelector(".v2-chip__dl") as HTMLButtonElement;
    expect(dl.tagName).toBe("BUTTON");
    expect(dl.getAttribute("aria-label")).toBe("Download report.pdf");

    const x = container.querySelector(".v2-chip__x") as HTMLButtonElement;
    expect(x.getAttribute("aria-label")).toBe("Unpin");
    // dl and × are flat siblings, neither nested inside the other
    expect(dl.parentElement).toBe(chip);
    expect(x.parentElement).toBe(chip);

    act(() => { fireEvent.click(x); });
    expect(onRemove).toHaveBeenCalledWith("att_1");
    // × click is stopPropagation-guarded — must not also start a download
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("pinned-strip shape: clicking the dedicated download button downloads (independent of ×)", async () => {
    fetchMock().mockResolvedValueOnce(streamResponse("hello world"));
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ downloadUrl: "/api/v1/files/art_1", size: 11 })}
          onRemove={() => {}}
          removeLabel="Unpin"
        />,
      ),
    );
    const dl = container.querySelector(".v2-chip__dl") as HTMLButtonElement;
    act(() => { fireEvent.click(dl); });
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/files/art_1"),
      expect.objectContaining({ credentials: "include", signal: expect.any(AbortSignal) }),
    ));
  });

  it("chip not downloadable while upload is still in progress, even if downloadUrl is set early", () => {
    const { container } = render(
      i18nWrap(
        <AttachmentChip
          item={makeItem({ status: "uploading", progressPct: 40, downloadUrl: "/api/v1/files/art_1" })}
          onRemove={() => {}}
        />,
      ),
    );
    const chip = container.querySelector(".v2-chip");
    expect(chip?.className).not.toContain("v2-chip--downloadable");
  });
});
