import "@testing-library/jest-dom/vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/en-US.json";
import type { MeUser } from "@/lib/auth-api";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  active: vi.fn(),
  me: null as Pick<MeUser, "capabilities"> | null,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => ({ me: mocks.me }),
}));
vi.mock("@/lib/shopify-content-confirmation-api", () => ({
  shopifyContentConfirmationApi: mocks.api,
}));
vi.mock("@/lib/turn-store", () => ({ useHasActiveToolStep: mocks.active }));

import { ShopifyContentConfirmations } from "./shopify-content-confirmations";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.me = { capabilities: { shopify_content_confirmations: true } };
  mocks.active.mockReturnValue(false);
  mocks.api.mockResolvedValue({ confirmations: [] });
});

describe("Shopify content confirmation capability", () => {
  it.each([null, {}, { capabilities: {} }, { capabilities: { shopify_content_confirmations: false } }])(
    "does not query an unavailable capability even with an active Shopify tool: %j",
    async (me) => {
      mocks.me = me;
      mocks.active.mockReturnValue(true);
      render(<ShopifyContentConfirmations />, {
        wrapper: ({ children }) => (
          <NextIntlClientProvider locale="en-US" messages={messages}>
            {children}
          </NextIntlClientProvider>
        ),
      });
      await act(async () => {});
      expect(mocks.api).not.toHaveBeenCalled();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    },
  );

  it("checks for a pending review on mount when enabled without an active tool", async () => {
    render(<ShopifyContentConfirmations />, {
      wrapper: ({ children }) => (
        <NextIntlClientProvider locale="en-US" messages={messages}>
          {children}
        </NextIntlClientProvider>
      ),
    });
    await waitFor(() => expect(mocks.api).toHaveBeenCalledExactlyOnceWith(""));
  });

  it("starts recovery when capability arrives and stops polling when it is disabled", async () => {
    mocks.me = null;
    mocks.active.mockReturnValue(true);
    const view = render(<ShopifyContentConfirmations />, {
      wrapper: ({ children }) => (
        <NextIntlClientProvider locale="en-US" messages={messages}>
          {children}
        </NextIntlClientProvider>
      ),
    });
    expect(mocks.api).not.toHaveBeenCalled();
    mocks.me = { capabilities: { shopify_content_confirmations: true } };
    view.rerender(<ShopifyContentConfirmations />);
    await waitFor(() => expect(mocks.api).toHaveBeenCalledTimes(1));
    mocks.me = { capabilities: { shopify_content_confirmations: false } };
    view.rerender(<ShopifyContentConfirmations />);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mocks.api).toHaveBeenCalledTimes(1);
    mocks.me = { capabilities: { shopify_content_confirmations: true } };
    view.rerender(<ShopifyContentConfirmations />);
    await waitFor(() => expect(mocks.api).toHaveBeenCalledTimes(2));
  });
});
