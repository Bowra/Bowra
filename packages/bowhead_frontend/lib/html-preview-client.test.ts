// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { prepareHtmlInWorker } from "./html-preview-client";
import { HTML_PREVIEW_BUDGET } from "./html-preview-budget";

class ControlledWorker {
  static latest: ControlledWorker;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessageerror: (() => void) | null = null;
  terminate = vi.fn();
  postMessage = vi.fn();
  constructor() { ControlledWorker.latest = this; }
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("bounded HTML worker lifetime", () => {
  it("terminates stalled preparation at its deadline without returning partial HTML", async () => {
    vi.useFakeTimers(); vi.stubGlobal("Worker", ControlledWorker);
    const pending = prepareHtmlInWorker("<p>fixture</p>", true, new AbortController().signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: "HtmlPreviewBudgetError" });
    await vi.advanceTimersByTimeAsync(HTML_PREVIEW_BUDGET.deadlineMs);
    await rejected;
    expect(ControlledWorker.latest.terminate).toHaveBeenCalledOnce();
  });

  it("terminates preparation when the viewer changes identity or unmounts", async () => {
    vi.stubGlobal("Worker", ControlledWorker);
    const controller = new AbortController();
    const pending = prepareHtmlInWorker("<p>fixture</p>", true, controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(ControlledWorker.latest.terminate).toHaveBeenCalledOnce();
  });

  it("terminates after a successful result", async () => {
    vi.stubGlobal("Worker", ControlledWorker);
    const pending = prepareHtmlInWorker("<p>fixture</p>", false, new AbortController().signal);
    ControlledWorker.latest.onmessage!({ data: { ok: true, result: null } } as MessageEvent);
    await expect(pending).resolves.toBeNull();
    expect(ControlledWorker.latest.terminate).toHaveBeenCalledOnce();
  });
});

it("cancels queued work without creating an additional preparation worker", async () => {
  vi.stubGlobal("Worker", ControlledWorker);
  const first = new AbortController(); const second = new AbortController(); const queued = new AbortController();
  const one = prepareHtmlInWorker("one", true, first.signal);
  const two = prepareHtmlInWorker("two", true, second.signal);
  const lastActive = ControlledWorker.latest;
  const three = prepareHtmlInWorker("three", true, queued.signal);
  expect(ControlledWorker.latest).toBe(lastActive);
  queued.abort();
  await expect(three).rejects.toMatchObject({ name: "AbortError" });
  first.abort(); second.abort();
  await expect(one).rejects.toMatchObject({ name: "AbortError" });
  await expect(two).rejects.toMatchObject({ name: "AbortError" });
});
