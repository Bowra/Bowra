import { documentCanvasTitle } from "@/lib/document-drafts";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "@/__tests__/fixtures/document-draft-canvas.json";
import { apiFetch, apiJson, ApiError } from "@/lib/api-client";
import { parseDocumentJob } from "@/lib/document-draft-client";
import messages from "@/messages/en-US.json";
import {
  DocumentDraftCanvasObject,
  type DocumentDraftCanvasObjectProps,
} from "./document-draft-canvas-object";
import { PAGE_VIEW_DWELL_MS } from "@/components/documents/document-draft-review";

vi.mock("@/lib/api-client", async (original) => ({
  ...(await original<typeof import("@/lib/api-client")>()),
  apiFetch: vi.fn(),
  apiJson: vi.fn(),
}));
const context = fixture.context;
const review = parseDocumentJob(fixture.review, context.jobId);
const published = parseDocumentJob(fixture.published, context.jobId);
const get = vi.fn<() => Promise<unknown>>();
const post = vi.fn<(body: string) => Promise<unknown>>();
const isCurrent = vi.fn(() => true);
const onPublished = vi.fn();
const onStateChange = vi.fn();

function view(overrides: Partial<DocumentDraftCanvasObjectProps> = {}) {
  return (
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <DocumentDraftCanvasObject
        context={context}
        isCurrent={isCurrent}
        onPublished={onPublished}
        onStateChange={onStateChange}
        {...overrides}
      />
    </NextIntlClientProvider>
  );
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
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
async function acknowledge() {
  // Changed slides count as viewed after the loaded preview stays on screen (decision D5).
  fireEvent.load(await screen.findByRole("img", { name: "Slide 1 of 1" }));
  act(() => {
    vi.advanceTimersByTime(PAGE_VIEW_DWELL_MS);
  });
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(blocked(confirmButton())).toBe(false));
}
async function clickConfirm() {
  await acknowledge();
  fireEvent.click(confirmButton());
}
/** Status reads moved from an in-body button to the toolbar “⋯ › Refresh”. */
async function retryRead() {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Refresh" }));
}
async function openUsage() {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Usage…" }));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.resetAllMocks();
  isCurrent.mockReturnValue(true);
  get.mockResolvedValue(review);
  post.mockResolvedValue(published);
  vi.mocked(apiJson).mockImplementation(
    async <T,>(_path: RequestInfo | URL, init?: RequestInit): Promise<T> =>
      (await (init?.method === "POST" ? post(String(init.body)) : get())) as T,
  );
  vi.mocked(apiFetch).mockImplementation(
    async () => new Response(new Blob(["preview"], { type: "image/png" })),
  );
  let count = 0;
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => `blob:private-canvas-${++count}`),
      revokeObjectURL: vi.fn(),
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("exact private document Canvas adapter", () => {
  it("reads the exact session with captured headers and exposes only real published associations", async () => {
    render(view());
    await screen.findByRole("img");
    expect(apiJson).toHaveBeenCalledExactlyOnceWith(
      "/api/v1/sessions/s_doc/documents/djob_canvas_fixture",
      {
        signal: expect.any(AbortSignal),
        cache: "no-store",
        headers: {
          "X-Team-Id": "team_doc",
          "X-Space-Id": "ws_doc",
          "X-Workspace-Id": "ws_doc",
        },
      },
    );
    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/documents/djob_canvas_fixture/preview/page-1.png",
      {
        signal: expect.any(AbortSignal),
        priority: "high",
        headers: {
          "X-Team-Id": "team_doc",
          "X-Space-Id": "ws_doc",
          "X-Workspace-Id": "ws_doc",
        },
      },
    );
    expect(screen.getByText("Source").parentElement?.textContent).toContain("Your request · Quarter two");
    expect(onPublished).not.toHaveBeenCalled();
    await clickConfirm();
    await waitFor(() =>
      expect(onPublished).toHaveBeenCalledExactlyOnceWith({
        context,
        originTurnId: "t_doc",
        draftDigest: review.draft_digest,
        source: {
          artifactId: "art_canvas_source",
          sha256: fixture.review.draft.files["candidate.pptx"].sha256,
        },
        pdf: {
          artifactId: "art_canvas_pdf",
          sha256: fixture.review.draft.files["candidate.pdf"].sha256,
        },
      }),
    );
    expect(onStateChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        context,
        title: "fixture - Updated.pptx",
        state: "published",
        phase: "ready",
        closePolicy: "detach",
      }),
    );
    expect(get).toHaveBeenCalledTimes(2);
  });

  it.each([
    { ...context, sessionId: "" },
    { ...context, instanceId: "" },
  ])("does not read an incomplete pointer", async (invalid) => {
    render(view({ context: invalid }));
    expect(screen.getByText("This draft can’t be opened")).toBeTruthy();
    expect(apiJson).not.toHaveBeenCalled();
  });

  it("shows read failure, rejects wrong-job data and retries without writing", async () => {
    get
      .mockRejectedValueOnce(new Error("private infrastructure details"))
      .mockResolvedValueOnce({ ...review, id: "wrong-job" });
    render(view());
    await screen.findByRole("alert");
    await retryRead();
    await screen.findByRole("alert");
    expect(screen.queryByRole("img")).toBeNull();
    await retryRead();
    await screen.findByRole("img");
    expect(post).not.toHaveBeenCalled();
  });

  it("resets acknowledgement when preflight finds a different digest", async () => {
    get
      .mockResolvedValueOnce(review)
      .mockResolvedValue({ ...review, draft_digest: "b".repeat(64) });
    render(view());
    await clickConfirm();
    await screen.findByText(messages.documents.error_stale);
    expect(post).not.toHaveBeenCalled();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      false,
    );
    expect(blocked(confirmButton())).toBe(true);
  });

  it("rechecks read-only access before dispatch", async () => {
    get
      .mockResolvedValueOnce(review)
      .mockResolvedValue({ ...review, can_confirm: false });
    render(view());
    await clickConfirm();
    await screen.findByText(messages.documents.canvas_read_only);
    // Read-only review shows no publish button at all.
    expect(screen.queryByRole("button", { name: "Confirm and publish" })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("locks double clicks and retains the original instance while another Tab is selected", async () => {
    const pending = deferred<unknown>();
    post.mockReturnValue(pending.promise);
    const mounted = render(view());
    await acknowledge();
    act(() => {
      confirmButton().click();
      confirmButton().click();
    });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    // A retained Tab has the same instance predicate; selection is not command authority.
    mounted.rerender(view({ isCurrent: () => true }));
    await act(async () => pending.resolve(published));
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect(onPublished.mock.calls[0][0].context).toEqual(context);
  });

  it("reconciles a lost response with a read and retries only the original command", async () => {
    post
      .mockRejectedValueOnce(new TypeError("Lost response"))
      .mockResolvedValue(published);
    render(view());
    await clickConfirm();
    await screen.findByText(messages.documents.canvas_confirmation_unknown);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3));
    expect(onPublished).not.toHaveBeenCalled();
    await retryRead();
    await screen.findByRole("button", { name: "Retry publishing" });
    expect(post).toHaveBeenCalledTimes(1);
    fireEvent.click(
      screen.getByRole("button", { name: "Retry publishing" }),
    );
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledTimes(2);
    expect(JSON.parse(post.mock.calls[0][0])).toEqual(
      JSON.parse(post.mock.calls[1][0]),
    );
  });

  it("reports publication from canonical readback after a lost response", async () => {
    get
      .mockResolvedValueOnce(review)
      .mockResolvedValueOnce(review)
      .mockResolvedValue(published);
    post.mockRejectedValueOnce(new TypeError("Lost response"));
    const mounted = render(view());
    await clickConfirm();
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    mounted.rerender(view({ refreshKey: 1 }));
    await screen.findAllByText(messages.documents.state_published);
    expect(screen.getByTestId("document-published-card").getAttribute("data-intent")).toBe("success");
    expect(post).toHaveBeenCalledTimes(1);
    expect(onPublished).toHaveBeenCalledTimes(1);
  });

  it("retains unknown publication when the retry preflight is rejected", async () => {
    post.mockRejectedValueOnce(new TypeError("Lost response"));
    render(view());
    await clickConfirm();
    await screen.findByRole("button", { name: "Retry publishing" });
    get.mockRejectedValueOnce(new ApiError(409, "Read conflict"));
    fireEvent.click(
      screen.getByRole("button", { name: "Retry publishing" }),
    );
    await screen.findByText(messages.documents.canvas_confirmation_unknown);
    expect(post).toHaveBeenCalledTimes(1);
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("keeps a dispatched outcome unknown when write access is lost during retry", async () => {
    post.mockRejectedValueOnce(new TypeError("Lost response"));
    render(view());
    await clickConfirm();
    await screen.findByRole("button", { name: "Retry publishing" });
    get.mockResolvedValue({ ...review, can_confirm: false });
    fireEvent.click(
      screen.getByRole("button", { name: "Retry publishing" }),
    );
    await screen.findByText(messages.documents.canvas_read_only);
    expect(
      screen.getByText(messages.documents.canvas_confirmation_unknown),
    ).toBeTruthy();
    expect(post).toHaveBeenCalledTimes(1);
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("treats a mismatched confirmation receipt as unknown, not completion", async () => {
    post.mockResolvedValue({ ...published, draft_digest: "b".repeat(64) });
    render(view());
    await clickConfirm();
    await screen.findByText(messages.documents.canvas_confirmation_unknown);
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("refreshes a known rejection without claiming publication", async () => {
    get
      .mockResolvedValueOnce(review)
      .mockResolvedValueOnce(review)
      .mockResolvedValue({
        ...review,
        state: "cancelled",
        draft: undefined,
      });
    post.mockRejectedValue(
      new ApiError(409, "Conflict", { code: "document_revision_conflict" }),
    );
    render(view());
    await clickConfirm();
    await screen.findByText("Cancelled", { exact: true });
    expect(onPublished).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("does not complete the original command with a different published version", async () => {
    get
      .mockResolvedValueOnce(review)
      .mockResolvedValueOnce(review)
      .mockResolvedValue({ ...published, draft_digest: "b".repeat(64) });
    post.mockRejectedValue(new TypeError("Lost response"));
    render(view());
    await clickConfirm();
    await screen.findByText(messages.documents.error_stale);
    expect(onPublished).not.toHaveBeenCalled();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("does not create a blob for a preview arriving after scope disposal", async () => {
    const page = deferred<Response>();
    vi.mocked(apiFetch).mockReturnValue(page.promise);
    const mounted = render(view());
    await screen.findByText("Quarter one");
    mounted.rerender(
      view({ context: { ...context, authSessionGeneration: "auth_next" } }),
    );
    isCurrent.mockReturnValue(false);
    await act(async () => page.resolve(new Response(new Blob(["late"]))));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("clears private content on denied preview and fences late callbacks", async () => {
    const page = deferred<Response>();
    vi.mocked(apiFetch).mockReturnValue(page.promise);
    render(view());
    await screen.findByText("Quarter one");
    await act(async () => page.resolve(new Response(null, { status: 403 })));
    await screen.findByText(messages.documents.error_access);
    expect(screen.queryByText("Quarter one")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(onStateChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "unavailable", state: null }),
    );
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("clears content on exact-reader denial and ignores an earlier pending publication", async () => {
    const pending = deferred<unknown>();
    post.mockReturnValue(pending.promise);
    const mounted = render(view());
    await clickConfirm();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    get.mockRejectedValue(new ApiError(404, "Unavailable"));
    mounted.rerender(view({ refreshKey: 1 }));
    await screen.findByText(messages.documents.error_access);
    await act(async () => pending.resolve(published));
    expect(screen.queryByRole("img")).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
    expect(onPublished).not.toHaveBeenCalled();
  });

  it("disposes inaccessible scope before a late GET can restore content", async () => {
    const pending = deferred<unknown>();
    get.mockReturnValue(pending.promise);
    const mounted = render(view());
    isCurrent.mockReturnValue(false);
    mounted.rerender(view());
    await act(async () => pending.resolve(review));
    expect(screen.queryByRole("img")).toBeNull();
    expect(onPublished).not.toHaveBeenCalled();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("blocks dispatch if the registered instance changes during the preflight read", async () => {
    const preflight = deferred<unknown>();
    get.mockResolvedValueOnce(review).mockReturnValue(preflight.promise);
    render(view());
    await clickConfirm();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    isCurrent.mockReturnValue(false);
    await act(async () => preflight.resolve(review));
    expect(post).not.toHaveBeenCalled();
  });

  it("closing only detaches; reopening recovers the same command and rejects old instance results", async () => {
    const oldPost = deferred<unknown>();
    post.mockReturnValueOnce(oldPost.promise).mockResolvedValue(published);
    const old = render(view());
    await clickConfirm();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const postSignal = vi
      .mocked(apiJson)
      .mock.calls.find(([, init]) => init?.method === "POST")![1]!.signal!;
    old.unmount();
    expect(postSignal.aborted).toBe(true);
    onPublished.mockClear();
    const reopened = { ...context, instanceId: "reopened" };
    render(view({ context: reopened }));
    await screen.findByRole("img");
    await act(async () => oldPost.resolve(published));
    expect(onPublished).not.toHaveBeenCalled();
    expect(blocked(confirmButton())).toBe(true);
    await clickConfirm();
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][0]).toBe(post.mock.calls[1][0]);
    expect(onPublished.mock.calls[0][0].context.instanceId).toBe("reopened");
  });

  it("fences an old read and preview after the same object remounts", async () => {
    const oldRead = deferred<unknown>();
    get.mockReturnValueOnce(oldRead.promise);
    const mounted = render(view());
    mounted.rerender(
      view({ context: { ...context, instanceId: "replacement" } }),
    );
    await screen.findByRole("img");
    await act(async () => oldRead.resolve(published));
    expect(onPublished).not.toHaveBeenCalled();
    expect(onStateChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        context: expect.objectContaining({ instanceId: "replacement" }),
        state: "review",
      }),
    );
  });

  it("loads published state on cold reopen without a confirmation", async () => {
    get.mockResolvedValue(published);
    render(view());
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect(post).not.toHaveBeenCalled();
  });
});

describe("explicit revision handoff", () => {
  const revisable = { ...review, can_revise: true };
  const stage = vi.fn<NonNullable<DocumentDraftCanvasObjectProps["onRequestRevision"]>>();
  beforeEach(() => {
    stage.mockResolvedValue({ status: "applied", draftRevision: "local_revision" });
  });
  async function revise() {
    fireEvent.click(await screen.findByRole("button", { name: "Revise in chat" }));
  }
  it("requires an explicit action and canonical permission before staging an exact target without writes", async () => {
    get.mockResolvedValue(revisable);
    render(view({ onRequestRevision: stage }));
    await screen.findByRole("button", { name: "Revise in chat" });
    expect(stage).not.toHaveBeenCalled();
    await revise();
    await waitFor(() => expect(stage).toHaveBeenCalledTimes(1));
    expect(stage.mock.calls[0][0]).toMatchObject({
      context,
      target: {
        kind: "document_revision",
        version: 1,
        session_id: context.sessionId,
        job_id: context.jobId,
        draft_digest: review.draft_digest,
      },
      // The Composer revision bar names the real draft and its number (the fixture is draft 1).
      display: { title: documentCanvasTitle(revisable, ""), version: 1 },
    });
    expect(stage.mock.calls[0][0].display?.title).toMatch(/\.pptx$/);
    expect(get).toHaveBeenCalledTimes(2);
    expect(post).not.toHaveBeenCalled();
    await screen.findByText("Draft added to Chat. Check the input target, describe your changes and send when ready.");
    await revise();
    await waitFor(() => expect(stage).toHaveBeenCalledTimes(2));
    expect(stage.mock.calls[1][0].handoffId).toBe(stage.mock.calls[0][0].handoffId);
  });
  it("does not infer revision permission from confirmation access or a legacy projection", async () => {
    render(view({ onRequestRevision: stage }));
    await screen.findByRole("checkbox");
    expect(screen.queryByRole("button", { name: "Revise in chat" })).toBeNull();
  });
  it("retains the review and explains an occupied Composer without publishing", async () => {
    get.mockResolvedValue(revisable);
    stage.mockResolvedValue({ status: "blocked", reason: "draft_not_empty" });
    render(view({ onRequestRevision: stage }));
    await revise();
    await screen.findByText("Keep or clear your current input before selecting this draft.");
    expect(screen.getByRole("checkbox")).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });
  it("rejects a replaced baseline before handing off", async () => {
    get.mockResolvedValueOnce(revisable).mockResolvedValue({ ...revisable, draft_digest: "b".repeat(64) });
    render(view({ onRequestRevision: stage }));
    await revise();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Revise in chat" }) as HTMLButtonElement).disabled).toBe(false),
    );
    expect(stage).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });
  it("clears inaccessible content after revision preflight denial", async () => {
    get.mockResolvedValueOnce(revisable).mockRejectedValue(new ApiError(403, "Denied"));
    render(view({ onRequestRevision: stage }));
    await revise();
    await screen.findByText(messages.documents.error_access);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(stage).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });
  it("does not hand off a closed reader and invalidates a captured getter on unmount", async () => {
    const delayed = deferred<unknown>();
    get.mockResolvedValueOnce(revisable).mockImplementationOnce(() => delayed.promise);
    const rendered = render(view({ onRequestRevision: stage }));
    await revise();
    rendered.unmount();
    await act(async () => delayed.resolve(revisable));
    expect(stage).not.toHaveBeenCalled();
    get.mockResolvedValue(revisable);
    const reopened = render(view({ onRequestRevision: stage }));
    await revise();
    await waitFor(() => expect(stage).toHaveBeenCalledTimes(1));
    expect(stage.mock.calls[0][0].isCurrent()).toBe(true);
    reopened.unmount();
    expect(stage.mock.calls[0][0].isCurrent()).toBe(false);
  });
});


it("opens only original published artifact pointers and ignores clicks after revocation", async () => {
  get.mockResolvedValue(published);
  const open = vi.fn();
  render(view({ onOpenPublished: open }));
  const source = await screen.findByRole("button", { name: "Open PPTX" });
  fireEvent.click(source);
  expect(open).toHaveBeenCalledExactlyOnceWith(context, "art_canvas_source");
  fireEvent.click(screen.getByRole("button", { name: "Open PDF" }));
  expect(open).toHaveBeenLastCalledWith(context, "art_canvas_pdf");
  isCurrent.mockReturnValue(false);
  fireEvent.click(source);
  expect(open).toHaveBeenCalledTimes(2);
  expect(post).not.toHaveBeenCalled();
});

it("keeps the original request separate from an unfinished document name", async () => {
  get.mockResolvedValue({ ...review, state: "question", draft: undefined, can_confirm: false, question: "Which quarter?" });
  const returnToChat = vi.fn();
  render(view({ onReturnToChat: returnToChat }));
  await screen.findByText("Which quarter?");
  // The question is a ChatCard that asks for input and sends the user back to the chat to answer.
  const card = screen.getByTestId("document-question-card");
  expect(card.getAttribute("data-intent")).toBe("input");
  expect(card.textContent).toContain(messages.documents.question_eyebrow);
  fireEvent.click(screen.getByRole("button", { name: messages.documents.answer_in_chat }));
  expect(returnToChat).toHaveBeenCalledOnce();
  await waitFor(() => expect(onStateChange).toHaveBeenLastCalledWith(expect.objectContaining({
    title: "Template presentation",
    objectStatus: "needs_input",
    state: "question",
  })));
  expect(post).not.toHaveBeenCalled();
});

describe("terminal document failure recovery", () => {
  const failed = {
    ...review, state: "failed", draft: undefined, draft_digest: null,
    can_confirm: true, can_revise: false, error_code: "document_information_incomplete",
    usage: { settled_credits: 11, pending: false },
  };

  it("explains unsupported earlier answers without retrying or publishing the old task", async () => {
    get.mockResolvedValue({ ...failed, error_code: "document_history_unavailable" });
    render(view());
    await screen.findByText(messages.documents.error_history);
    expect(screen.queryByRole("button", { name: "Confirm and publish" })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("reports incomplete planning and settled usage without granting failed-state publication", async () => {
    get.mockResolvedValue(failed);
    const returnToChat = vi.fn();
    render(view({ onReturnToChat: returnToChat }));
    await screen.findByText(messages.documents.error_incomplete);
    // The state report follows the render that shows the text (effects flush after DOM updates).
    await waitFor(() => expect(onStateChange).toHaveBeenLastCalledWith(expect.objectContaining({ status: "Not completed", state: "failed" })));
    // D7: settled usage is not shown in the body; it lives in “⋯ › Usage”.
    expect(screen.queryByText("Usage · 11 credits")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm and publish" })).toBeNull();
    fireEvent.click(screen.getByText("Your request"));
    expect(screen.getByText(review.request_text)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Return to chat" }));
    expect(returnToChat).toHaveBeenCalledExactlyOnceWith(context);
    expect(post).not.toHaveBeenCalled();
  });

  it("checks only the captured job once while retaining controls and reconciles the returned lifecycle", async () => {
    const delayed = deferred<unknown>();
    get.mockResolvedValueOnce(failed).mockImplementationOnce(() => delayed.promise);
    render(view());
    const check = await screen.findByRole("button", { name: "Check status" });
    fireEvent.click(check);
    fireEvent.click(check);
    expect(check.getAttribute("aria-disabled")).toBe("true");
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByText(messages.documents.error_incomplete)).toBeTruthy();
    expect(vi.mocked(apiJson).mock.calls.every(([path, init]) => path === "/api/v1/sessions/s_doc/documents/djob_canvas_fixture" && init?.method !== "POST")).toBe(true);
    await act(async () => delayed.resolve({ ...review, error_code: "planning_invalid_response" }));
    await screen.findByRole("img");
    // The state report follows the render that shows the text (effects flush after DOM updates).
    await waitFor(() => expect(onStateChange).toHaveBeenLastCalledWith(expect.objectContaining({ status: "Ready for review", state: "review" })));
    expect(screen.queryByText(messages.documents.error_planning)).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("withholds retained candidate files and write actions when the canonical job failed", async () => {
    get.mockResolvedValue({ ...failed, draft: review.draft, draft_digest: review.draft_digest });
    const stage = vi.fn();
    render(view({ onRequestRevision: stage, onOpenPublished: vi.fn() }));
    await screen.findByText(messages.documents.error_incomplete);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm and publish" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revise in chat" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open PPTX" })).toBeNull();
    expect(apiFetch).not.toHaveBeenCalled();
    expect(stage).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it.each([
    ["planning_quote_mismatch", "error_planning"],
    ["document_model_unavailable", "error_model"],
    ["planning_unreviewed_provider_secret", "canvas_failure_generic"],
    [null, "canvas_failure_generic"],
  ] as const)("uses reviewed advice for %s without rendering the raw error", async (code, key) => {
    get.mockResolvedValue({ ...failed, error_code: code });
    render(view());
    await screen.findByText(messages.documents[key]);
    // The state report follows the render that shows the text (effects flush after DOM updates).
    await waitFor(() => expect(onStateChange).toHaveBeenLastCalledWith(expect.objectContaining({ status: "Generation failed" })));
    if (code) expect(screen.queryByText(code)).toBeNull();
  });

  it.each([
    [{ settled_credits: 0, pending: false }, "0 credits", null],
    [{ settled_credits: 11, pending: true }, "11 credits", "usage_pending"],
    [{ settled_credits: 11, pending: false, settlement_pending: true }, "11 credits", "settlement_pending"],
    [undefined, null, "canvas_usage_unavailable"],
  ] as const)("separates known settled usage from pending or absent accounting (%j)", async (usage, cost, note) => {
    get.mockResolvedValue({ ...failed, usage });
    render(view());
    await screen.findByText(messages.documents.error_incomplete);
    if (note) expect(screen.getByText(messages.documents[note])).toBeTruthy();
    if (cost) {
      await openUsage();
      expect(await screen.findByText(`Usage · ${cost}`)).toBeTruthy();
    } else expect(screen.queryByRole("menuitem", { name: "Usage…" })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("clears original failure details on denied read and refuses navigation after revocation", async () => {
    get.mockResolvedValueOnce(failed).mockRejectedValueOnce(new ApiError(403, "Unavailable"));
    const returnToChat = vi.fn();
    render(view({ onReturnToChat: returnToChat }));
    const navigate = await screen.findByRole("button", { name: "Return to chat" });
    isCurrent.mockReturnValue(false);
    fireEvent.click(navigate);
    expect(returnToChat).not.toHaveBeenCalled();
    isCurrent.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByText(messages.documents.error_access);
    expect(screen.queryByText(messages.documents.error_incomplete)).toBeNull();
    expect(screen.queryByText("Your request")).toBeNull();
    expect(screen.queryByRole("button", { name: "Return to chat" })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("ignores a late failure read after close and does not route it to a reopened instance", async () => {
    const delayed = deferred<unknown>();
    get.mockResolvedValueOnce(failed).mockImplementationOnce(() => delayed.promise).mockResolvedValueOnce(review);
    const rendered = render(view());
    fireEvent.click(await screen.findByRole("button", { name: "Check status" }));
    rendered.unmount();
    render(view());
    await screen.findByRole("img");
    const calls = onStateChange.mock.calls.length;
    await act(async () => delayed.resolve(failed));
    expect(onStateChange).toHaveBeenCalledTimes(calls);
    expect(screen.queryByText(messages.documents.error_incomplete)).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });
});
