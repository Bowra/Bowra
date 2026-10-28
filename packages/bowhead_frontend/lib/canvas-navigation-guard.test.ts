import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requestConfirm: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("./confirm-dialog-store", () => ({ requestConfirm: mocks.requestConfirm }));
vi.mock("./use-toast", () => ({ toast: mocks.toast }));

import {
  confirmCanvasNavigation,
  hasCanvasUnsavedChanges,
  registerCanvasNavigationGuard,
  registerSubagentDraftNavigationGuard,
} from "./canvas-navigation-guard";
import { BEFORE_APP_NAVIGATION_EVENT } from "./use-app-router";

let cleanup: (() => void) | null = null;

afterEach(() => {
  cleanup?.();
  cleanup = null;
  vi.clearAllMocks();
});

describe("canvas-navigation-guard", () => {
  it("allows navigation when no dirty report is registered", async () => {
    expect(hasCanvasUnsavedChanges()).toBe(false);
    await expect(confirmCanvasNavigation()).resolves.toBe(true);
    expect(mocks.requestConfirm).not.toHaveBeenCalled();
  });

  it("blocks navigation when the user cancels the dirty-report prompt", async () => {
    const flush = vi.fn();
    cleanup = registerCanvasNavigationGuard({ hasUnsaved: () => true, flush });
    mocks.requestConfirm.mockResolvedValue(false);

    await expect(confirmCanvasNavigation()).resolves.toBe(false);
    expect(flush).not.toHaveBeenCalled();
  });

  it("flushes before navigation when the user confirms", async () => {
    let dirty = true;
    const flush = vi.fn().mockImplementation(async () => { dirty = false; return { ok: true }; });
    cleanup = registerCanvasNavigationGuard({ hasUnsaved: () => dirty, flush });
    mocks.requestConfirm.mockResolvedValue(true);

    await expect(confirmCanvasNavigation()).resolves.toBe(true);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("blocks navigation and shows an error toast when flush reports a save failure", async () => {
    const flush = vi.fn().mockResolvedValue({ ok: false, message: "network down" });
    cleanup = registerCanvasNavigationGuard({ hasUnsaved: () => true, flush });
    mocks.requestConfirm.mockResolvedValue(true);

    await expect(confirmCanvasNavigation()).resolves.toBe(false);
    expect(mocks.toast).toHaveBeenCalledWith({
      kind: "error",
      text: expect.stringContaining("network down"),
    });
  });

  it("waits for the draft editor before changing the active chat", async () => {
    const confirmLeave = vi.fn().mockResolvedValue(false);
    cleanup = registerSubagentDraftNavigationGuard({ confirmLeave });
    await expect(confirmCanvasNavigation()).resolves.toBe(false);
    expect(confirmLeave).toHaveBeenCalledOnce();
    expect(mocks.requestConfirm).not.toHaveBeenCalled();
  });
});

it("never resumes an earlier navigation after a newer destination replaces it", async () => {
  let resolveFirst!: (allowed: boolean) => void;
  const confirmLeave = vi.fn().mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve; })).mockResolvedValueOnce(true);
  cleanup = registerSubagentDraftNavigationGuard({ confirmLeave });
  const first = confirmCanvasNavigation();
  await expect(confirmCanvasNavigation()).resolves.toBe(true);
  resolveFirst(true);
  await expect(first).resolves.toBe(false);
});

it("invalidates an older pending destination even when the newer route is clean", async () => {
  let dirty = true;
  let finish!: (allowed: boolean) => void;
  const proceed = vi.fn();
  cleanup = registerSubagentDraftNavigationGuard({
    hasUnsaved: () => dirty,
    confirmLeave: () => new Promise((resolve) => { finish = resolve; }),
  });
  const earlier = confirmCanvasNavigation();
  dirty = false;
  const next = new CustomEvent(BEFORE_APP_NAVIGATION_EVENT, { cancelable: true, detail: { proceed } });
  window.dispatchEvent(next);
  expect(next.defaultPrevented).toBe(false);
  finish(true);
  await expect(earlier).resolves.toBe(false);
  expect(proceed).not.toHaveBeenCalled();
});

it("rechecks an earlier object's input after a later object finishes its decision", async () => {
  let changed = false;
  const first = registerSubagentDraftNavigationGuard({ confirmLeave: async () => true, hasUnsaved: () => changed });
  const second = registerSubagentDraftNavigationGuard({ confirmLeave: async () => { changed = true; return true; }, hasUnsaved: () => false });
  try { await expect(confirmCanvasNavigation()).resolves.toBe(false); }
  finally { first(); second(); }
});

it("does not extend an approval to a reopened controller with the same view key", async () => {
  let replace!: () => void;
  let unregisterOriginal!: () => void;
  let unregisterReopened: (() => void) | undefined;
  unregisterOriginal = registerSubagentDraftNavigationGuard({ confirmLeave: () => new Promise((resolve) => { replace = () => { unregisterOriginal(); unregisterReopened = registerSubagentDraftNavigationGuard({ confirmLeave: async () => true }); resolve(true); }; }) });
  const decision = confirmCanvasNavigation();
  replace();
  await expect(decision).resolves.toBe(false);
  unregisterReopened?.();
});
