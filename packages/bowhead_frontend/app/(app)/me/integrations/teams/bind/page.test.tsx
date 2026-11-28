import type { ReactNode } from "react";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { apiJson } from "@/lib/api-client";
import TeamsBindingPage from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams({ pair: "test-pairing" }),
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({
    status: "authed",
    me: { email: "member@example.test" },
  }),
}));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/api-client", () => ({
  apiJson: vi.fn(),
  ApiError: class extends Error {},
}));
afterEach(cleanup);

it("requires an explicit confirmation from the signed-in member", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ok: true });
  renderIntl(<TeamsBindingPage />);
  expect(screen.getByText("member@example.test")).toBeTruthy();
  expect(apiJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));
  expect(
    await screen.findByText("绑定成功。返回 Teams，发送你的问题。"),
  ).toBeTruthy();
  expect(apiJson).toHaveBeenCalledWith(
    "/api/v1/integrations/teams/bindings/confirm",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ token: "test-pairing" }),
    }),
  );
});
