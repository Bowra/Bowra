import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en-US.json";
import { PRODUCT_NAME } from "@/lib/product-identity";
import CommunitySetupPage from "./activation";

const mocks = vi.hoisted(() => ({ replace: vi.fn(), status: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ replace: mocks.replace }) }));
vi.mock("@/lib/auth-api", () => ({ getBootstrapStatus: mocks.status, postAuthJson: mocks.post }));
beforeEach(() => {
  vi.clearAllMocks();
  mocks.status.mockResolvedValue({ required: true, edition: "community" });
  mocks.post.mockResolvedValue({ ok: true });
});
afterEach(cleanup);
function show() {
  return render(
    <NextIntlClientProvider locale="en-US" messages={en}>
      <CommunitySetupPage />
    </NextIntlClientProvider>,
  );
}
async function fill() {
  await waitFor(() => expect(screen.getByLabelText("Setup token")).not.toBeDisabled());
  fireEvent.change(screen.getByLabelText("Setup token"), { target: { value: "operator-proof" } });
  fireEvent.change(screen.getByLabelText("Team name"), { target: { value: "Research" } });
  fireEvent.change(screen.getByLabelText("Owner email"), { target: { value: "owner@example.com" } });
  fireEvent.change(screen.getByLabelText("Owner password"), { target: { value: "A-strong-password-1" } });
}
describe("first owner setup", () => {
  it("submits server proof and owner credentials once and opens ordinary login", async () => {
    show();
    expect(screen.getByRole("heading", { name: `Set up ${PRODUCT_NAME}` })).toBeInTheDocument();
    expect(screen.getByText("Run this command on the application server with the same configuration to obtain the one-time setup token.")).toBeInTheDocument();
    expect(screen.getByText("python -m bowhead_gateway.installation setup-token")).toBeInTheDocument();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Create team" }));
    await waitFor(() =>
      expect(mocks.post).toHaveBeenCalledWith("/api/v1/bootstrap/community", {
        setup_token: "operator-proof",
        team_name: "Research",
        email: "owner@example.com",
        password: "A-strong-password-1",
        display_name: undefined,
      }),
    );
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login/"));
  });
  it("shows a useful token error without rendering the rejected secret", async () => {
    mocks.post.mockRejectedValue({ code: "invalid_setup_token" });
    show();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Create team" }));
    await screen.findByText("The setup token is invalid. Generate it on this application server and try again.");
    expect(mocks.replace).not.toHaveBeenCalled();
  });
  it("redirects an initialized instance to login without accepting another owner", async () => {
    mocks.status.mockResolvedValue({ required: false, edition: "community" });
    show();
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login/"));
    expect(screen.getByRole("button", { name: "Create team" })).toBeDisabled();
  });
});
