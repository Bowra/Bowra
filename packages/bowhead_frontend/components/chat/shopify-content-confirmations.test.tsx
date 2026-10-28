import "@testing-library/jest-dom/vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  active: vi.fn(),
  poll: vi.fn(),
  items: [] as unknown[],
}));
vi.mock("@/lib/shopify-content-confirmation-api", () => ({
  shopifyContentConfirmationApi: mocks.api,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => ({ me: { capabilities: { shopify_content_confirmations: true } } }),
}));
vi.mock("@/lib/turn-store", () => ({ useHasActiveToolStep: mocks.active }));
vi.mock("@/lib/use-confirmation-queue-poll", () => ({
  useConfirmationQueuePoll: mocks.poll,
}));
import { ShopifyContentConfirmations } from "./shopify-content-confirmations";

const item = {
  id: "scc_test",
  team_id: "team_test",
  turn_id: "t_test",
  session_id: "s_test",
  shop_domain: "fixture.myshopify.com",
  resource: "page",
  object_id: "gid://shopify/Page/1",
  title: "About us",
  handle: "about-us",
  state: "unpublished",
  edits: [
    {
      field: "body_html",
      before: '<img src="x" onerror="alert(1)">',
      after: "<p>Reviewed story</p>",
    },
  ],
  expires_at: "2026-10-09T12:03:00Z",
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.items = [item];
  mocks.active.mockReturnValue(true);
  let initialized = false;
  mocks.poll.mockImplementation(({ onItems }) => {
    if (!initialized) {
      initialized = true;
      onItems(mocks.items);
    }
  });
  mocks.api.mockResolvedValue({ ok: true });
});

describe("Shopify requester content review", () => {
  it("shows exact target and both texts without executing source HTML", () => {
    const { container } = renderIntl(<ShopifyContentConfirmations />, {
      locale: "en-US",
    });
    expect(screen.getByText("fixture.myshopify.com")).toBeInTheDocument();
    expect(screen.getByText(item.edits[0].before)).toBeInTheDocument();
    expect(screen.getByText(item.edits[0].after)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(mocks.active).toHaveBeenCalledWith(".shopify_apply_content_update", "suffix");
    expect(mocks.api).not.toHaveBeenCalled();
  });

  it("sends only the explicit snapshot decision and prevents double clicks", async () => {
    let release!: (value: unknown) => void;
    mocks.api.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    renderIntl(<ShopifyContentConfirmations />, { locale: "en-US" });
    const approve = screen.getByRole("button", { name: "Apply these changes" });
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith("/scc_test/decision", "POST", { decision: "approve" });
    release({ ok: true });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("retains the review on decision failure and allows explicit rejection", async () => {
    mocks.api.mockRejectedValueOnce(new Error("private details"));
    renderIntl(<ShopifyContentConfirmations />, { locale: "zh-CN" });
    fireEvent.click(screen.getByRole("button", { name: "应用这些修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未能记录你的决定");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText("private details")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "拒绝修改" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenLastCalledWith("/scc_test/decision", "POST", {
        decision: "reject",
      }),
    );
  });
});
