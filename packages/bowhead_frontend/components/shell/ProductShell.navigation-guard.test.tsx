/**
 * New Session encounters the page's leave guard (unsaved model-settings changes): synchronously intercept, then show an in-app
 * confirmation; after confirming, proceed() resumes the original navigation. The sidebar receives "cancelled" at interception;
 * during the subsequent full-page navigation, show progress again or the button can be clicked and open another confirmation.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BEFORE_APP_NAVIGATION_EVENT,
  type AppNavigationDetail,
} from "@/lib/use-app-router";

import { ProductShell } from "./ProductShell";

const { navigateAppDocument, results } = vi.hoisted(() => ({
  navigateAppDocument: vi.fn(),
  results: [] as string[],
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));
vi.mock("@/lib/use-app-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-app-router")>();
  return { ...actual, navigateAppDocument };
});
vi.mock("@/components/unified-sidebar", () => ({
  UnifiedSidebar: ({
    onNewSession,
    navigationPending,
  }: {
    onNewSession: () => Promise<string>;
    navigationPending?: boolean;
  }) => (
    <button
      type="button"
      disabled={navigationPending}
      aria-busy={navigationPending || undefined}
      onClick={async () => results.push(await onNewSession())}
    >
      sidebar-new
    </button>
  ),
}));
vi.mock("@/components/sessions-drawer", () => ({
  SessionsDrawer: () => null,
}));
vi.mock("@/lib/use-session-archive-capability", () => ({
  useSessionArchiveCapability: () => true,
}));

afterEach(() => {
  cleanup();
  navigateAppDocument.mockClear();
  results.length = 0;
});

function installAsyncGuard() {
  const proceeds: Array<() => void> = [];
  const guard = (event: Event) => {
    event.preventDefault();
    proceeds.push((event as CustomEvent<AppNavigationDetail>).detail.proceed);
  };
  window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
  return { proceeds, remove: () => window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, guard) };
}

describe("ProductShell New work behind an asynchronous leave guard", () => {
  it("shows the navigation as pending once the guard lets it proceed", async () => {
    const guard = installAsyncGuard();
    try {
      render(<ProductShell>page</ProductShell>);
      const action = await screen.findByRole("button", { name: "sidebar-new" });

      fireEvent.click(action);
      await waitFor(() => expect(results).toEqual(["cancelled"]));
      expect(guard.proceeds).toHaveLength(1);
      expect(navigateAppDocument).not.toHaveBeenCalled();
      expect(action).not.toBeDisabled();

      // User chooses Leave in the confirmation dialog.
      guard.proceeds[0]!();

      expect(navigateAppDocument).toHaveBeenCalledWith("/");
      await waitFor(() => expect(action).toBeDisabled());
      expect(action.getAttribute("aria-busy")).toBe("true");
    } finally {
      guard.remove();
    }
  });

  it("leaves the action idle when the guard never proceeds (user kept editing)", async () => {
    const guard = installAsyncGuard();
    try {
      render(<ProductShell>page</ProductShell>);
      const action = await screen.findByRole("button", { name: "sidebar-new" });
      fireEvent.click(action);
      await waitFor(() => expect(results).toEqual(["cancelled"]));
      expect(action).not.toBeDisabled();
      expect(navigateAppDocument).not.toHaveBeenCalled();
    } finally {
      guard.remove();
    }
  });
});
