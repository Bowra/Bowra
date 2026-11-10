import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

type Me = null | { capabilities?: Record<string, boolean | undefined> };

const mocks = vi.hoisted(() => ({
  me: null as Me,
}));

vi.mock("@/lib/auth/auth-session-context", () => ({ useAuthSession: () => ({ me: mocks.me }) }));
vi.mock("@/components/chat/personal-mcp-confirmations", () => ({
  PersonalMcpConfirmations: () => <div data-testid="personal-mcp-dialog" />,
}));
vi.mock("@/components/chat/email-send-confirmations", () => ({
  EmailSendConfirmations: () => <div data-testid="email-send-dialog" />,
}));
vi.mock("@/components/chat/shopify-content-confirmations", () => ({
  ShopifyContentConfirmations: () => <div data-testid="shopify-content-dialog" />,
}));
vi.mock("@/components/chat/team-tool-confirmations", () => ({
  TeamToolConfirmations: () => <div data-testid="team-tool-dialog" />,
}));
vi.mock("./ToolConfirmationNotice", () => ({
  ToolConfirmationNotice: () => <div data-testid="tool-confirmation-notice" />,
}));

import { ToolConfirmationSurfaces } from "./ToolConfirmationSurfaces";

const DIALOGS = ["personal-mcp-dialog", "email-send-dialog", "shopify-content-dialog", "team-tool-dialog"];

afterEach(() => {
  cleanup();
  mocks.me = null;
});

describe("ToolConfirmationSurfaces", () => {
  it.each<Me>([null, { capabilities: {} }, { capabilities: { inline_tool_confirmations: false } }])(
    "keeps the four dialogs and no notice when inline confirmation is off (%j)",
    (me) => {
      mocks.me = me;
      render(<ToolConfirmationSurfaces />);
      for (const id of DIALOGS) expect(screen.getByTestId(id)).toBeInTheDocument();
      expect(screen.queryByTestId("tool-confirmation-notice")).toBeNull();
    },
  );

  it("mounts only the notice when inline confirmation is on", () => {
    mocks.me = { capabilities: { inline_tool_confirmations: true } };
    render(<ToolConfirmationSurfaces />);
    expect(screen.getByTestId("tool-confirmation-notice")).toBeInTheDocument();
    for (const id of DIALOGS) expect(screen.queryByTestId(id)).toBeNull();
  });
});
