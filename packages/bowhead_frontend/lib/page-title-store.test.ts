import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getPageTitleSnapshot,
  registerPageTitle,
  subscribePageTitle,
} from "./page-title-store";

const cleanups: Array<() => void> = [];

afterEach(() => {
  while (cleanups.length) cleanups.pop()?.();
});

function register(ownerId: string, title: string | null | undefined) {
  const registration = registerPageTitle(ownerId, title);
  cleanups.push(registration.unregister);
  return registration;
}

describe("page title store", () => {
  it("uses a deterministic registration stack and restores its parent", () => {
    const parent = register("page", " Parent ");
    expect(getPageTitleSnapshot()).toBe("Parent");

    const child = register("dialog", "Child");
    expect(getPageTitleSnapshot()).toBe("Child");

    child.unregister();
    expect(getPageTitleSnapshot()).toBe("Parent");
    parent.unregister();
    expect(getPageTitleSnapshot()).toBeNull();
  });

  it("cleans up the exact registration during duplicate-owner lifecycles", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const first = register("canvas", "First");
    const second = register("canvas", "Second");

    expect(getPageTitleSnapshot()).toBe("Second");
    expect(warn).toHaveBeenCalledOnce();

    first.unregister();
    expect(getPageTitleSnapshot()).toBe("Second");
    second.unregister();
    expect(getPageTitleSnapshot()).toBeNull();
    warn.mockRestore();
  });

  it("notifies subscribers only when the active normalized title changes", () => {
    const listener = vi.fn();
    const unsubscribe = subscribePageTitle(listener);
    const parent = register("page", "Page");
    const hidden = register("hidden", " Page ");

    expect(listener).toHaveBeenCalledOnce();

    hidden.unregister();
    expect(listener).toHaveBeenCalledOnce();
    parent.unregister();
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("updates a title in place without changing registration priority", () => {
    const parent = register("page", "Page");
    const child = register("dialog", "Dialog");

    parent.update("Updated page");
    expect(getPageTitleSnapshot()).toBe("Dialog");

    child.update("Updated dialog");
    expect(getPageTitleSnapshot()).toBe("Updated dialog");

    child.unregister();
    expect(getPageTitleSnapshot()).toBe("Updated page");
    parent.unregister();
  });
});
