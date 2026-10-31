import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/api-client";
import type { DocumentJob } from "@/lib/document-drafts";
import messages from "@/messages/en-US.json";
import { DocumentDraftReview, PAGE_VIEW_DWELL_MS, isDefaultShapeName } from "./document-draft-review";

vi.mock("@/lib/api-client", () => ({ apiFetch: vi.fn() }));

const draft: DocumentJob = {
  id: "djob_review",
  origin_turn_id: "t_document",
  state: "review",
  can_confirm: true,
  request_text: "Use the uploaded template",
  draft_digest: "a".repeat(64),
  draft: {
    files: {},
    manifest: {
      changes: [
        {
          slide: 1,
          before: "Old title",
          after: "New title",
          evidence: [
            { source_name: "request", pointer: null, value: "New title" },
          ],
        },
      ],
      retained_unsupported_text: [],
    },
    preview: {
      pages: [{ name: "page-1.png" }],
      declared_font_fallbacks: [],
    },
  },
};

function view(job = draft, onConfirm = vi.fn(), busy = false) {
  return (
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <DocumentDraftReview job={job} busy={busy} onConfirm={onConfirm} />
    </NextIntlClientProvider>
  );
}

/** A blocked footer primary stays focusable (aria-disabled); busy primaries are natively disabled. */
function blocked(button: HTMLButtonElement) {
  return button.disabled || button.getAttribute("aria-disabled") === "true";
}

function confirmButton() {
  return screen.getByRole("button", {
    name: "Confirm and publish",
  }) as HTMLButtonElement;
}

it("requires every page when comparison is unavailable without claiming no changes", async () => {
  const job: DocumentJob = { ...draft, draft: { ...draft.draft!, requires_full_review: true,
    manifest: { ...draft.draft!.manifest, comparison: "unavailable", changes: [] },
    preview: { ...draft.draft!.preview, pages: [{ name: "page-1.png" }, { name: "page-2.png" }] },
  } };
  const onConfirm = vi.fn();
  render(view(job, onConfirm));
  expect(screen.getByText(messages.documents.comparison_unavailable)).toBeTruthy();
  expect(screen.queryByText("No changes on this slide.")).toBeNull();
  const first = await screen.findByRole("img", { name: "Slide 1 of 2" });
  view_(first);
  expect(blocked(confirmButton())).toBe(true);
  expect(onConfirm).not.toHaveBeenCalled();
});

it("requires every page of a script-authored draft without marking unchanged pages as edited", async () => {
  const job: DocumentJob = { ...draft, draft: { ...draft.draft!, requires_full_review: true, preview: {
    ...draft.draft!.preview, pages: [{ name: "page-1.png" }, { name: "page-2.png" }],
  } } };
  const onConfirm = vi.fn();
  render(view(job, onConfirm));
  const first = await screen.findByRole("img", { name: "Slide 1 of 2" });
  view_(first);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(blocked(confirmButton())).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Next unviewed page" }));
  const second = await screen.findByRole("img", { name: "Slide 2 of 2" });
  view_(second);
  await waitFor(() => expect(blocked(confirmButton())).toBe(false));
  fireEvent.click(confirmButton());
  expect(onConfirm).toHaveBeenCalledWith(draft.draft_digest);
});

/** A page counts as viewed once its loaded preview stays visible for the dwell time (decision D5). */
function view_(image: HTMLElement) {
  fireEvent.load(image);
  act(() => {
    vi.advanceTimersByTime(PAGE_VIEW_DWELL_MS);
  });
}

async function reviewPage() {
  const image = await screen.findByRole("img", { name: "Slide 1 of 1" });
  view_(image);
  fireEvent.click(screen.getByRole("checkbox"));
  return image;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.resetAllMocks();
  let sequence = 0;
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => `blob:private-review-${++sequence}`),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.mocked(apiFetch).mockImplementation(
    async () => new Response(new Blob(["preview"], { type: "image/png" })),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("private document review content", () => {
  it.each(["answer", "same-name-upload", "different-hash", "question"])("attributes evidence from the authorized projection: %s", async (origin) => {
    const source = { source_name: "Document clarification history.json", source_sha256: "c".repeat(64) };
    const job = structuredClone(draft);
    job.clarification_evidence_sources = origin === "same-name-upload" ? [] : [source];
    job.draft!.manifest.changes[0].evidence = [{
      ...source,
      source_sha256: origin === "different-hash" ? "d".repeat(64) : source.source_sha256,
      pointer: origin === "question" ? "/answers/0/question" : "/answers/0/answer",
      value: "New title",
    }];
    render(view(job));
    if (origin === "answer") {
      expect(await screen.findByText("Your clarification answer · New title")).toBeTruthy();
    } else {
      expect(await screen.findByText(`File “${source.source_name}”`)).toBeTruthy();
      expect(screen.queryByText("Your clarification answer · New title")).toBeNull();
    }
  });

  it("requires only the changed slides to be viewed before confirming (D5)", async () => {
    const onConfirm = vi.fn();
    render(view({
      ...draft,
      draft: {
        ...draft.draft!,
        manifest: { ...draft.draft!.manifest, changes: [{ ...draft.draft!.manifest.changes[0], slide: 2 }] },
        preview: {
          ...draft.draft!.preview,
          pages: [{ name: "page-1.png" }, { name: "page-2.png" }, { name: "page-3.png" }],
        },
      },
    }, onConfirm));
    view_(await screen.findByRole("img", { name: "Slide 1 of 3" }));
    expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("1 changed slide not viewed yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const second = await screen.findByRole("img", { name: "Slide 2 of 3" });
    fireEvent.load(second);
    // Loading alone is not viewing: the slide must stay on screen for the dwell time.
    expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    act(() => {
      vi.advanceTimersByTime(PAGE_VIEW_DWELL_MS);
    });
    expect(screen.getByText("Check the review box first")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(blocked(confirmButton())).toBe(false);
    fireEvent.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledExactlyOnceWith(draft.draft_digest);
    expect(screen.getByText("Viewed 2 of 3 slides")).toBeTruthy();
    expect(vi.mocked(apiFetch).mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/documents/djob_review/preview/page-1.png",
      "/api/v1/documents/djob_review/preview/page-2.png",
    ]);
  });

  it("passes the reviewed digest to the command owner only after loaded pages and acknowledgement", async () => {
    const onConfirm = vi.fn();
    render(view(draft, onConfirm));
    expect(blocked(confirmButton())).toBe(true);
    const image = await screen.findByRole("img");
    view_(image);
    expect(blocked(confirmButton())).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledExactlyOnceWith(draft.draft_digest);
    // Before / after are rendered as readable values, never JSON.
    expect(screen.getByText("Old title")).toBeTruthy();
    expect(screen.getByText("New title")).toBeTruthy();
    expect(apiFetch).toHaveBeenCalledExactlyOnceWith(
      "/api/v1/documents/djob_review/preview/page-1.png",
      { signal: expect.any(AbortSignal), priority: "high" },
    );
  });

  it.each([
    ["digest", { ...draft, draft_digest: "b".repeat(64) }],
    ["job", { ...draft, id: "djob_successor" }],
  ])(
    "resets review when the %s changes without a parent remount",
    async (_label, updated) => {
      const onConfirm = vi.fn();
      const mounted = render(view(draft, onConfirm));
      await reviewPage();
      expect(blocked(confirmButton())).toBe(false);
      mounted.rerender(view(updated, onConfirm));
      expect(blocked(confirmButton())).toBe(true);
      expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
        false,
      );
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:private-review-1");
      expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
      view_(await screen.findByRole("img"));
      fireEvent.click(screen.getByRole("checkbox"));
      fireEvent.click(confirmButton());
      expect(onConfirm).toHaveBeenCalledExactlyOnceWith(updated.draft_digest);
      expect(apiFetch).toHaveBeenCalledTimes(2);
    },
  );

  it("retains review for the same immutable version while honoring current permission and busy state", async () => {
    const mounted = render(view());
    await reviewPage();
    // Without publish permission the footer only explains; no checkbox or publish button (read-only state).
    mounted.rerender(view({ ...draft, can_confirm: false }));
    expect(screen.queryByRole("button", { name: "Confirm and publish" })).toBeNull();
    expect(screen.getByText("You can review this draft but can’t publish it.")).toBeTruthy();
    mounted.rerender(view(draft, vi.fn(), true));
    expect(blocked(confirmButton())).toBe(true);
    mounted.rerender(view());
    expect(blocked(confirmButton())).toBe(false);
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it("invalidates a previously loaded page when its image fails", async () => {
    const onConfirm = vi.fn();
    render(view(draft, onConfirm));
    const image = await reviewPage();
    expect(blocked(confirmButton())).toBe(false);
    fireEvent.error(image);
    expect(blocked(confirmButton())).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain(
      "Load the preview to review and publish.",
    );
    fireEvent.click(confirmButton());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("does not approve a failed authenticated preview read", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(null, { status: 403 }));
    render(view());
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("checkbox"));
    expect(blocked(confirmButton())).toBe(true);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("retries only the failed preview read without confirming or generating work", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(null, { status: 503 }),
    );
    const onConfirm = vi.fn();
    render(view(draft, onConfirm));
    await screen.findByRole("alert");
    expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(blocked(confirmButton())).toBe(true);
    view_(await screen.findByRole("img"));
    fireEvent.click(screen.getByRole("checkbox"));
    expect(blocked(confirmButton())).toBe(false);
    expect(apiFetch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(apiFetch).mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/documents/djob_review/preview/page-1.png",
      "/api/v1/documents/djob_review/preview/page-1.png",
    ]);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("does not fetch an empty preview name or approve a preview with no pages", () => {
    render(
      view({
        ...draft,
        draft: {
          ...draft.draft!,
          preview: { pages: [], declared_font_fallbacks: [] },
        },
      }),
    );
    fireEvent.click(screen.getByRole("checkbox"));
    expect(blocked(confirmButton())).toBe(true);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("requires a digest even after all preview requirements are met", async () => {
    render(view({ ...draft, draft_digest: undefined }));
    await reviewPage();
    expect(blocked(confirmButton())).toBe(true);
  });

  it("aborts an old digest read and ignores its late body while displaying the new version", async () => {
    let resolveBody!: (blob: Blob) => void;
    const response = new Response();
    const blob = vi.spyOn(response, "blob").mockReturnValue(
      new Promise((resolve) => {
        resolveBody = resolve;
      }),
    );
    vi.mocked(apiFetch).mockResolvedValueOnce(response);
    const mounted = render(view());
    await waitFor(() => expect(blob).toHaveBeenCalledOnce());
    const signal = vi.mocked(apiFetch).mock.calls[0][1]!.signal!;
    mounted.rerender(view({ ...draft, draft_digest: "b".repeat(64) }));
    expect(signal.aborted).toBe(true);
    await screen.findByRole("img");
    await act(async () => {
      resolveBody(new Blob(["obsolete preview"]));
    });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(blocked(confirmButton())).toBe(true);
  });

  it("isolates a closed view from late reads and requires review again on same-key reopen", async () => {
    let resolveRead!: (response: Response) => void;
    vi.mocked(apiFetch).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRead = resolve;
        }),
    );
    const mounted = render(view());
    const signal = vi.mocked(apiFetch).mock.calls[0][1]!.signal!;
    mounted.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolveRead(new Response(new Blob(["obsolete preview"])));
    });
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    const reopened = render(view());
    await reviewPage();
    reopened.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:private-review-1");
    render(view());
    expect(blocked(confirmButton())).toBe(true);
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
  });
});

it("loads thumbnails that scroll into view after the current page, without counting them as viewed", async () => {
  const observed: Array<{ callback: IntersectionObserverCallback; targets: Element[] }> = [];
  vi.stubGlobal("IntersectionObserver", class {
    targets: Element[] = [];
    constructor(callback: IntersectionObserverCallback) { observed.push({ callback, targets: this.targets }); }
    observe(target: Element) { this.targets.push(target); }
    disconnect() {}
    unobserve() {}
    takeRecords() { return []; }
  });
  vi.mocked(apiFetch).mockResolvedValue(new Response(new Blob(["png"]), { status: 200 }));
  const job = { ...draft, draft: { ...draft.draft!, preview: { ...draft.draft!.preview, pages: [{ name: "page-1.png" }, { name: "page-2.png" }, { name: "page-3.png" }] } } };
  render(view(job));
  await screen.findByRole("img", { name: "Slide 1 of 3" });
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
  const strip = observed.at(-1)!;
  const third = strip.targets.find((target) => (target as HTMLElement).dataset.page === "3")!;
  act(() => strip.callback([{ isIntersecting: true, target: third } as unknown as IntersectionObserverEntry], {} as IntersectionObserver));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiFetch).mock.calls[1][0]).toContain("page-3.png");
  expect(vi.mocked(apiFetch).mock.calls[1][1]).toMatchObject({ priority: "low" });
  // Only the current page's dwell can mark a page viewed.
  expect(screen.getByText("Viewed 0 of 3 slides")).toBeTruthy();
});

it("lists the slides that use a substituted font", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(new Blob(["png"]), { status: 200 }));
  const job = { ...draft, draft: { ...draft.draft!, preview: { ...draft.draft!.preview,
    declared_font_fallbacks: [{ declared_font: "Source Han Sans", environment_fallback: "Noto Sans CJK", slides: [3, 6] }] } } };
  render(view(job));
  fireEvent.click(await screen.findByRole("button", { name: /^Font substitutions/ }));
  expect(screen.getByText("Source Han Sans → Noto Sans CJK (slides 3, 6)")).toBeTruthy();
});

it("describes shapes that only carry a default PowerPoint name by their slide", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(new Blob(["png"]), { status: 200 }));
  const change = draft.draft!.manifest.changes[0];
  const job = { ...draft, draft: { ...draft.draft!, manifest: { ...draft.draft!.manifest, changes: [
    { ...change, shape_name: "TextBox 5" },
    { ...change, shape_name: "Quarter highlights" },
  ] } } };
  render(view(job));
  await screen.findByRole("img", { name: "Slide 1 of 1" });
  expect(screen.getByText("Text box on slide 1")).toBeTruthy();
  expect(screen.queryByText(/TextBox 5/)).toBeNull();
  expect(screen.getByText(/Quarter highlights/)).toBeTruthy();
  expect(isDefaultShapeName("文本框 3")).toBe(true);
  expect(isDefaultShapeName("Google Shape;123")).toBe(true);
  expect(isDefaultShapeName("本季亮点")).toBe(false);
});
