import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  acceptInvite: vi.fn(),
  continueInviteIntent: vi.fn(),
  exchangeInviteIntent: vi.fn(),
  getAuthCapabilities: vi.fn(),
  storeInvitePreview: vi.fn(),
  storeInviteResult: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    acceptInvite: (...args: unknown[]) => mocks.acceptInvite(...args),
    continueInviteIntent: (...args: unknown[]) => mocks.continueInviteIntent(...args),
    exchangeInviteIntent: (...args: unknown[]) => mocks.exchangeInviteIntent(...args),
    getAuthCapabilities: (...args: unknown[]) => mocks.getAuthCapabilities(...args),
    storeInvitePreview: (...args: unknown[]) => mocks.storeInvitePreview(...args),
    storeInviteResult: (...args: unknown[]) => mocks.storeInviteResult(...args),
  };
});

import { InviteApiError } from "@/lib/invite-api";

import InvitePage from "./page";

const origLocation = window.location;

const preview = {
  status: "ready",
  invite_type: "workspace",
  invited_email_mask: "a***@datarc.test",
  expires_at: "2026-07-15T00:00:00+00:00",
  team: { name: "Datarc Strategy" },
  workspace: { name: "Market Lab", role: "member" },
} as const;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.searchParams = new URLSearchParams();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
  window.history.pushState({}, "", "/invite");
});

function renderPage(locale: "en-US" | "zh-CN" = "en-US") {
  const messages = locale === "en-US" ? enMessages : zhMessages;
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <InvitePage />
    </NextIntlClientProvider>,
  );
}

describe("InvitePage intent flow", () => {
  it("exchanges the raw URL token, clears it, and accepts with the server intent", async () => {
    const accepted = {
      invite_type: "workspace",
      status: "accepted",
      next_route: "/invite/ready?state=workspace_ready",
    };
    mocks.searchParams = new URLSearchParams("token=invite-token");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.exchangeInviteIntent.mockResolvedValue(preview);
    mocks.continueInviteIntent.mockResolvedValue({ ...preview, next_action: "accept" });
    mocks.acceptInvite.mockResolvedValue({ ok: true, invite_result: accepted });

    renderPage();

    await waitFor(() => expect(mocks.exchangeInviteIntent).toHaveBeenCalledWith("invite-token"));
    expect(window.location.search).toBe("?resume=1");
    expect(window.sessionStorage.getItem("bowhead.invite.token")).toBeNull();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/invite/",
        search: "?resume=1",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    fireEvent.click(await screen.findByRole("button", { name: /^Accept invite/ }));
    await waitFor(() => expect(mocks.acceptInvite).toHaveBeenCalledWith(undefined));
    expect(mocks.storeInviteResult).toHaveBeenCalledWith(accepted);
    expect(window.location.replace).toHaveBeenCalledWith(
      "/ai/apps/bowhead/invite/ready/?state=workspace_ready",
    );
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("routes password activation without carrying the raw invite token", async () => {
    mocks.searchParams = new URLSearchParams("token=invite-token");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.exchangeInviteIntent.mockResolvedValue(preview);
    mocks.continueInviteIntent.mockResolvedValue({
      ...preview,
      next_action: "activate_password",
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /^Create profile/ }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/invite/activate/"));
    expect(mocks.push.mock.calls.flat().join(" ")).not.toContain("invite-token");
  });

  it("fails closed when an identity is blocked", async () => {
    mocks.searchParams = new URLSearchParams("resume=1");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.continueInviteIntent.mockResolvedValue({
      ...preview,
      next_action: "identity_blocked",
    });

    renderPage();

    const button = await screen.findByRole("button", { name: /Contact your administrator/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.exchangeInviteIntent).not.toHaveBeenCalled();
  });

  it.each([
    ["en-US", "This invitation has expired"],
    ["zh-CN", "邀请已过期"],
  ] as const)(
    "renders a structured terminal error in %s without exposing its code",
    async (locale, title) => {
      mocks.searchParams = new URLSearchParams("token=invite-token");
      mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
      mocks.exchangeInviteIntent.mockRejectedValue(
        new InviteApiError(410, "transport fallback", "invite_expired"),
      );

      renderPage(locale);

      expect(await screen.findByRole("heading", { name: title })).toBeVisible();
      expect(screen.queryByText("invite_expired")).not.toBeInTheDocument();
    },
  );

  it("explains a policy-blocked invitation in Chinese", async () => {
    mocks.searchParams = new URLSearchParams("resume=1");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.continueInviteIntent.mockResolvedValue({
      ...preview,
      next_action: "policy_blocked",
    });

    renderPage("zh-CN");

    expect(
      await screen.findByText(
        "当前团队策略不允许通过此邀请继续，请联系团队管理员。",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: /联系管理员/ }),
    ).toBeDisabled();
  });
});

describe("InvitePage terminal invitations after the URL token is replaced", () => {
  it.each([
    ["invite_expired", 410, "邀请已过期"],
    ["invite_revoked", 410, "邀请已被撤销"],
    ["invite_consumed", 409, "这条邀请已经接受过"],
  ] as const)(
    "keeps the %s result when the page swaps ?token= for ?resume=1 mid-exchange",
    async (code, status, title) => {
      let rejectExchange: ((reason: unknown) => void) | undefined;
      mocks.searchParams = new URLSearchParams("token=invite-token");
      mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
      mocks.exchangeInviteIntent.mockReturnValue(
        new Promise((_resolve, reject) => {
          rejectExchange = reject;
        }),
      );
      // If intent creation failed, continue reports a missing intent; letting it overwrite the error would incorrectly show an invitation-check failure.
      mocks.continueInviteIntent.mockRejectedValue(
        new InviteApiError(401, "intent missing", "invite_intent_missing"),
      );

      const view = renderPage("zh-CN");
      await waitFor(() => expect(mocks.exchangeInviteIntent).toHaveBeenCalledWith("invite-token"));
      expect(window.location.search).toBe("?resume=1");

      // Next useSearchParams follows the page's own history.replaceState changes and triggers rerendering.
      mocks.searchParams = new URLSearchParams("resume=1");
      view.rerender(
        <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
          <InvitePage />
        </NextIntlClientProvider>,
      );
      rejectExchange?.(new InviteApiError(status, "transport fallback", code));

      expect(await screen.findByRole("heading", { name: title })).toBeVisible();
      expect(screen.queryByText("无法检查这条邀请")).not.toBeInTheDocument();
      expect(mocks.continueInviteIntent).not.toHaveBeenCalled();
      expect(mocks.exchangeInviteIntent).toHaveBeenCalledTimes(1);
    },
  );
});

describe("InvitePage inviter", () => {
  it("names the inviter by display name when the preview provides one", async () => {
    mocks.searchParams = new URLSearchParams("token=invite-token");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.exchangeInviteIntent.mockResolvedValue({ ...preview, inviter_display_name: "Li Hua" });
    mocks.continueInviteIntent.mockResolvedValue({
      ...preview,
      inviter_display_name: "Li Hua",
      next_action: "activate_password",
    });

    renderPage();

    expect(await screen.findByText("Li Hua invited you to join “Market Lab”")).toBeVisible();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Join “Market Lab”");
  });

  it("leaves the inviter line out when there is no display name", async () => {
    mocks.searchParams = new URLSearchParams("token=invite-token");
    mocks.getAuthCapabilities.mockResolvedValue({ invite_activation: true });
    mocks.exchangeInviteIntent.mockResolvedValue(preview);
    mocks.continueInviteIntent.mockResolvedValue({ ...preview, next_action: "activate_password" });

    renderPage();

    expect(await screen.findByRole("heading", { level: 1, name: "Join “Market Lab”" })).toBeVisible();
    expect(screen.queryByText(/invited you to join/)).not.toBeInTheDocument();
  });
});
