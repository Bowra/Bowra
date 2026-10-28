import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ComposerDraftHandoffError,
  registerComposerDraftHandoff,
  retryComposerDraftHandoff,
  waitForComposerDraftHandoff,
} from "./composer-draft-handoff";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

afterEach(() => vi.useRealTimers());

describe("same-document draft ownership transfer", () => {
  it("waits through successive owners without blocking another scope", async () => {
    const signature = crypto.randomUUID();
    const first = deferred();
    const second = deferred();
    registerComposerDraftHandoff(signature, () => first.promise);
    const restored = vi.fn();
    const reader = waitForComposerDraftHandoff(signature).then(restored);
    registerComposerDraftHandoff(signature, () => second.promise);
    await waitForComposerDraftHandoff(crypto.randomUUID());
    first.resolve();
    await first.promise;
    expect(restored).not.toHaveBeenCalled();
    second.resolve();
    await reader;
    expect(restored).toHaveBeenCalledOnce();
  });

  it("keeps failed writes fenced until an explicit retry succeeds", async () => {
    const signature = crypto.randomUUID();
    const finish = vi.fn(async (retry: boolean) => {
      if (!retry) throw new Error("synthetic storage failure");
    });
    registerComposerDraftHandoff(signature, finish);
    await expect(waitForComposerDraftHandoff(signature)).rejects.toBeInstanceOf(ComposerDraftHandoffError);
    await expect(waitForComposerDraftHandoff(signature)).rejects.toBeInstanceOf(ComposerDraftHandoffError);
    expect(finish).toHaveBeenCalledTimes(1);
    retryComposerDraftHandoff(signature);
    await waitForComposerDraftHandoff(signature);
    expect(finish.mock.calls).toEqual([[false], [true]]);
  });

  it("retries a failed predecessor before completing a later owner", async () => {
    const signature = crypto.randomUUID();
    const order: string[] = [];
    registerComposerDraftHandoff(signature, async (retry) => {
      if (!retry) throw new Error("synthetic storage failure");
      order.push("first");
    });
    registerComposerDraftHandoff(signature, async () => { order.push("second"); });
    await expect(waitForComposerDraftHandoff(signature)).rejects.toBeInstanceOf(ComposerDraftHandoffError);
    retryComposerDraftHandoff(signature);
    await waitForComposerDraftHandoff(signature);
    expect(order).toEqual(["first", "second"]);
  });

  it("times out the reader without cancelling or duplicating a pending writer", async () => {
    vi.useFakeTimers();
    const signature = crypto.randomUUID();
    const pending = deferred();
    const finish = vi.fn(() => pending.promise);
    registerComposerDraftHandoff(signature, finish);
    const result = expect(waitForComposerDraftHandoff(signature)).rejects.toBeInstanceOf(ComposerDraftHandoffError);
    await vi.advanceTimersByTimeAsync(5_000);
    await result;
    retryComposerDraftHandoff(signature);
    const restored = vi.fn();
    const retry = waitForComposerDraftHandoff(signature).then(restored);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(restored).not.toHaveBeenCalled();
    expect(finish).toHaveBeenCalledOnce();
    pending.resolve();
    await retry;
    await waitForComposerDraftHandoff(signature);
  });
});
