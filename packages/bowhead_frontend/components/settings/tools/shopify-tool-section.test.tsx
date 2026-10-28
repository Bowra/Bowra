import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { TeamBuiltinTool } from "@/lib/team-builtin-tools-api";

const api = vi.hoisted(() => ({
  createShopifyTool: vi.fn(),
  updateShopifyTool: vi.fn(),
  setShopifyEnabled: vi.fn(),
  deleteShopifyTool: vi.fn(),
  createShopifyPlugin: vi.fn(),
  createShopifyContentPlugin: vi.fn(),
}));
vi.mock("@/lib/team-builtin-tools-api", () => api);
import { ShopifyToolSection } from "./shopify-tool-section";

const tool: TeamBuiltinTool = {
  id: "ibt_one",
  provider: "shopify",
  name: "Commerce",
  display_name: "Shopify - Commerce",
  endpoint: "https://fixture.myshopify.com",
  key_hint: null,
  status: "enabled",
  configured: true,
  created_at: "t",
  updated_at: "t",
  scope_state: "verified",
  scope: {},
  scope_checked_at: "t",
  supported_tools: ["shopify_shop_context"],
  shop_domain: "fixture.myshopify.com",
  client_id: "client",
  connection_revision: 4,
};
beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
});
afterEach(cleanup);

describe("Shopify connection management", () => {
  it("creates a verified connection and clears the secret after save", async () => {
    const changed = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderIntl(<ShopifyToolSection tools={[]} createRequest={1} onChanged={changed} />, { locale: "en-US" });
    fireEvent.change(screen.getByLabelText("Connection name"), { target: { value: "Commerce" } });
    fireEvent.change(screen.getByLabelText("Shop domain"), { target: { value: "fixture.myshopify.com" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client" } });
    fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));
    await waitFor(() =>
      expect(api.createShopifyTool).toHaveBeenCalledWith({
        name: "Commerce",
        shop_domain: "fixture.myshopify.com",
        client_id: "client",
        client_secret: "new-secret",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(changed).toHaveBeenCalledOnce();
    rerender(<div />);
    renderIntl(<ShopifyToolSection tools={[]} createRequest={2} onChanged={changed} />, { locale: "en-US" });
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
  });

  it("keeps shop identity fixed and sends the revision when editing without a new secret", async () => {
    renderIntl(<ShopifyToolSection tools={[tool]} createRequest={0} onChanged={vi.fn().mockResolvedValue(undefined)} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Shop domain")).toBeDisabled();
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));
    await waitFor(() =>
      expect(api.updateShopifyTool).toHaveBeenCalledWith("ibt_one", {
        name: "Commerce",
        client_id: "client",
        client_secret: undefined,
        connection_revision: 4,
      }),
    );
  });

  it("creates a Plugin for this connection and reports where to select it", async () => {
    renderIntl(<ShopifyToolSection tools={[tool]} createRequest={0} onChanged={vi.fn()} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Create analysis Plugin" }));
    await waitFor(() => expect(api.createShopifyPlugin).toHaveBeenCalledWith("ibt_one"));
    expect(await screen.findByText("Plugin created. Select it in the Agent editor to use this shop.")).toBeInTheDocument();
  });

  it("offers a separate content Plugin only when its Tools are available", async () => {
    const { unmount } = renderIntl(<ShopifyToolSection tools={[tool]} createRequest={0} onChanged={vi.fn()} />, { locale: "en-US" });
    expect(screen.queryByRole("button", { name: "Create content Plugin" })).not.toBeInTheDocument();
    unmount();
    renderIntl(
      <ShopifyToolSection
        tools={[
          {
            ...tool,
            supported_tools: [...tool.supported_tools, "shopify_preview_content_update"],
          },
        ]}
        createRequest={0}
        onChanged={vi.fn()}
      />,
      { locale: "en-US" },
    );
    fireEvent.click(screen.getByRole("button", { name: "Create content Plugin" }));
    await waitFor(() => expect(api.createShopifyContentPlugin).toHaveBeenCalledWith("ibt_one"));
    expect(api.createShopifyPlugin).not.toHaveBeenCalled();
  });


  it("requires delete confirmation and never shows raw provider errors", async () => {
    api.deleteShopifyTool.mockRejectedValue({ code: "upstream", message: "secret-private-provider-response" });
    renderIntl(<ShopifyToolSection tools={[tool]} createRequest={0} onChanged={vi.fn()} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.deleteShopifyTool).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteShopifyTool).toHaveBeenCalledWith("ibt_one"));
    expect(await screen.findByText("Could not complete the operation. Check the app configuration and try again.")).toBeInTheDocument();
    expect(screen.queryByText("secret-private-provider-response")).not.toBeInTheDocument();
  });
});
