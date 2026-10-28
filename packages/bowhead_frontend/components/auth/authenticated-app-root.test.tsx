import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  pathname: "/",
}));

vi.mock("next/navigation", () => ({
  usePathname: () => state.pathname,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  AuthSessionProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="auth-provider">{children}</div>
  ),
}));
vi.mock("@/components/auth/authenticated-app-boundary", () => ({
  AuthenticatedAppBoundary: ({
    children,
    chrome,
  }: {
    children: React.ReactNode;
    chrome: string;
  }) => (
    <div data-testid="auth-boundary" data-chrome={chrome}>
      {children}
    </div>
  ),
}));
vi.mock("@/components/authenticated-app-chrome", () => ({
  AuthenticatedAppChrome: ({
    children,
    chrome,
  }: {
    children: React.ReactNode;
    chrome: string;
  }) => (
    <div data-testid="app-chrome" data-chrome={chrome}>
      {children}
    </div>
  ),
}));
vi.mock("@/components/brand-provider", () => ({
  BrandProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="brand-provider">{children}</div>
  ),
}));
vi.mock("@/components/brand-metadata-controller", () => ({
  BrandMetadataController: () => <div data-testid="brand-metadata" />,
}));
vi.mock("@/components/chat/toast-container", () => ({
  ToastContainer: () => <div data-testid="toast" />,
}));

vi.mock("@/components/chat/personal-mcp-confirmations", () => ({ PersonalMcpConfirmations: () => <div data-testid="personal-mcp-confirmations" /> }));
vi.mock("@/components/chat/email-send-confirmations", () => ({ EmailSendConfirmations: () => <div data-testid="email-send-confirmations" /> }));

import { AuthenticatedAppRoot } from "./authenticated-app-root";

afterEach(() => {
  cleanup();
  state.pathname = "/";
  window.history.replaceState({}, "", "/");
});

describe("AuthenticatedAppRoot", () => {
  it("gates a product route before mounting its chrome", () => {
    render(
      <AuthenticatedAppRoot>
        <main>product</main>
      </AuthenticatedAppRoot>,
    );

    expect(screen.getByTestId("auth-provider")).toBeTruthy();
    expect(screen.getByTestId("brand-provider")).toBeTruthy();
    expect(screen.getByTestId("auth-boundary").dataset.chrome).toBe(
      "product-shell",
    );
    expect(screen.getByTestId("app-chrome").dataset.chrome).toBe(
      "product-shell",
    );
  });

  it("gates onboarding with standalone chrome", () => {
    state.pathname = "/onboarding/profile/";
    window.history.replaceState({}, "", state.pathname);

    render(
      <AuthenticatedAppRoot>
        <main>onboarding</main>
      </AuthenticatedAppRoot>,
    );

    expect(screen.getByTestId("auth-boundary").dataset.chrome).toBe(
      "standalone",
    );
    expect(screen.getByTestId("app-chrome").dataset.chrome).toBe(
      "standalone",
    );
  });

  it("keeps deterministic spike routes outside authenticated APIs", () => {
    state.pathname = "/spike/p31/";
    window.history.replaceState({}, "", state.pathname);

    render(
      <AuthenticatedAppRoot>
        <main>spike</main>
      </AuthenticatedAppRoot>,
    );

    expect(screen.queryByTestId("auth-provider")).toBeNull();
    expect(screen.queryByTestId("app-chrome")).toBeNull();
    expect(screen.getByTestId("brand-provider")).toBeTruthy();
    expect(screen.getByText("spike")).toBeTruthy();
  });
});

vi.mock("@/components/chat/shopify-content-confirmations", () => ({ ShopifyContentConfirmations: () => <div data-testid="shopify-content-confirmations" /> }));
