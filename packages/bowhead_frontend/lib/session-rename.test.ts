import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  reconcileSessionTitles,
  resetSessionTitleOverridesForTest,
  submitSessionRename,
  useSessionTitleOverrides,
} from "./session-rename";

function deferred() {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("optimistic session titles", () => {
  afterEach(() => {
    act(() => resetSessionTitleOverridesForTest());
  });

  it("keeps a settled title until the refreshed list reports it, then clears it", async () => {
    const { result } = renderHook(() => useSessionTitleOverrides());
    await act(async () => {
      await submitSessionRename("s_1", "New name", async () => undefined);
    });
    expect(result.current.get("s_1")).toBe("New name");

    // A list fetched before the save still has the old title: keep the override.
    act(() => reconcileSessionTitles([{ id: "s_1", title: "Old name" }]));
    expect(result.current.get("s_1")).toBe("New name");

    act(() => reconcileSessionTitles([{ id: "s_1", title: "New name" }]));
    expect(result.current.has("s_1")).toBe(false);
  });

  it("does not clear a rename that is still being saved", async () => {
    const { result } = renderHook(() => useSessionTitleOverrides());
    const save = deferred();
    let pending!: Promise<void>;
    act(() => {
      pending = submitSessionRename("s_1", "Saving", () => save.promise);
    });
    act(() => reconcileSessionTitles([{ id: "s_1", title: "Saving" }]));
    expect(result.current.get("s_1")).toBe("Saving");
    await act(async () => {
      save.resolve();
      await pending;
    });
  });

  it("lets only the latest rename roll back: an older failure keeps the newer title", async () => {
    const { result } = renderHook(() => useSessionTitleOverrides());
    const first = deferred();
    const second = deferred();
    let firstDone!: Promise<void>;
    let secondDone!: Promise<void>;
    act(() => {
      firstDone = submitSessionRename("s_1", "First", () => first.promise);
    });
    act(() => {
      secondDone = submitSessionRename("s_1", "Second", () => second.promise);
    });
    expect(result.current.get("s_1")).toBe("Second");

    await act(async () => {
      first.reject(new Error("late failure"));
      await expect(firstDone).rejects.toThrow("late failure");
    });
    expect(result.current.get("s_1")).toBe("Second");

    await act(async () => {
      second.resolve();
      await secondDone;
    });
    expect(result.current.get("s_1")).toBe("Second");
  });

  it("an older success does not settle over a newer pending rename that later fails", async () => {
    const { result } = renderHook(() => useSessionTitleOverrides());
    const first = deferred();
    const second = deferred();
    let firstDone!: Promise<void>;
    let secondDone!: Promise<void>;
    act(() => {
      firstDone = submitSessionRename("s_1", "First", () => first.promise);
    });
    act(() => {
      secondDone = submitSessionRename("s_1", "Second", () => second.promise);
    });
    await act(async () => {
      first.resolve();
      await firstDone;
    });
    // The newer rename is still pending, so a list showing "Second" must not clear it yet.
    act(() => reconcileSessionTitles([{ id: "s_1", title: "Second" }]));
    expect(result.current.get("s_1")).toBe("Second");

    await act(async () => {
      second.reject(new Error("failed"));
      await expect(secondDone).rejects.toThrow("failed");
    });
    expect(result.current.has("s_1")).toBe(false);
  });

  it("refreshes the session list only after a successful save", async () => {
    const listener = vi.fn();
    window.addEventListener("bowhead:sessions-changed", listener);
    await act(async () => {
      await submitSessionRename("s_1", "Ok", async () => undefined);
      await expect(
        submitSessionRename("s_2", "Bad", async () => {
          throw new Error("no");
        }),
      ).rejects.toThrow("no");
    });
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener("bowhead:sessions-changed", listener);
  });
});
