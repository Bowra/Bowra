import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";

const state = vi.hoisted(() => ({
  fetchConfig: vi.fn(),
  replace: vi.fn(),
  deployment: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: state.replace }),
}));
vi.mock("@/lib/slack-integration-api", () => ({
  fetchSlackAdminConfig: () => state.fetchConfig(),
  beginSlackInstall: vi.fn(),
  updateSlackAdminConfig: vi.fn(),
}));
vi.mock("@/lib/settings-nav-counts", () => ({
  loadDeploymentFacts: () => state.deployment(),
}));

import { SlackIntegrationDetail } from "./slack-integration-detail";

const originalClipboard = navigator.clipboard;

beforeEach(() => {
  state.deployment.mockResolvedValue({ local: true, openapi: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: originalClipboard });
});

describe("SlackIntegrationDetail", () => {
  it("returns to integrations without requesting the config outside private deployments", async () => {
    state.deployment.mockResolvedValue({ local: false, openapi: false });

    renderIntl(<SlackIntegrationDetail />);

    await waitFor(() => expect(state.replace).toHaveBeenCalledWith("/team/settings/integrations/"));
    expect(state.fetchConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Slack" })).toBeNull();
  });

  it("returns to integrations when the config route reports Slack unavailable", async () => {
    state.deployment.mockResolvedValue(null);
    state.fetchConfig.mockRejectedValue(new ApiError(404, "not found"));

    renderIntl(<SlackIntegrationDetail />);

    await waitFor(() => expect(state.replace).toHaveBeenCalledWith("/team/settings/integrations/"));
    expect(screen.queryByRole("heading", { name: "Slack" })).toBeNull();
  });

  it("does not show deployment-mode copy in the administrator page", async () => {
    state.fetchConfig.mockResolvedValue({
      configured: false,
      status: "draft",
      channel_answers_enabled: false,
    });

    renderIntl(<SlackIntegrationDetail />);

    expect(await screen.findByRole("heading", { name: "Slack" })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/local|Local Only/i);
  });

  it("provides direct, actionable Slack App creation instructions", async () => {
    state.fetchConfig.mockResolvedValue({ configured: false, status: "draft", channel_answers_enabled: false });

    renderIntl(<SlackIntegrationDetail />);

    const createAppLink = await screen.findByRole("link", { name: "打开 Slack App 创建页" });
    expect(createAppLink).toHaveAttribute("href", "https://api.slack.com/apps");
    expect(screen.getByText("Starter app")).toBeTruthy();
    expect(screen.getByText("Create and Install")).toBeTruthy();
    expect(screen.getByText("Your app credentials")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "保存 Slack App 凭据" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "登记团队 Bot 回调并安装" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "开放个人 Slack MCP" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "允许在被 @ 后向频道线程公开回答" })).toBeTruthy();
  });

  it("copies each callback value from its icon button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    state.fetchConfig.mockResolvedValue({
      configured: true,
      status: "verified",
      channel_answers_enabled: false,
      event_url: "https://example.com/events",
      command_url: "https://example.com/commands",
      interaction_url: "https://example.com/interactions",
      oauth_callback_url: "https://example.com/oauth/callback",
      personal_mcp_oauth_callback_url: "https://example.com/personal-mcp/slack/oauth/callback",
      slack_settings: {
        events: "https://api.slack.com/apps/A1/event-subscriptions",
        slash_commands: "https://api.slack.com/apps/A1/slash-commands",
        interactivity: "https://api.slack.com/apps/A1/interactive-messages",
      },
      required_scopes: ["app_mentions:read", "chat:write"],
    });

    renderIntl(<SlackIntegrationDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "复制 Event Request URL" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://example.com/events"));
    expect(screen.getByRole("button", { name: "已复制 Event Request URL" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制 团队 Slack Bot OAuth Redirect URL" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制 个人 Slack MCP OAuth Redirect URL" })).toBeTruthy();
    expect(screen.getByText("User Token Scopes")).toBeTruthy();
    expect(screen.getByText("不是 Bot Token Scopes", { exact: false })).toBeTruthy();
    for (const scope of ["search:read.public", "search:read.private", "search:read.mpim", "search:read.im", "search:read.files", "search:read.users", "channels:history", "groups:history", "mpim:history", "im:history", "channels:read", "groups:read", "mpim:read", "im:read", "users:read", "users:read.email"]) {
      expect(screen.getByText(scope)).toBeTruthy();
    }
    expect(screen.getByRole("button", { name: "安装到 Slack" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "配置 Slack 回调" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "打开 Event Subscriptions" })).toHaveAttribute("href", "https://api.slack.com/apps/A1/event-subscriptions");
    expect(screen.getByRole("button", { name: "复制 订阅事件" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制 命令" })).toBeTruthy();
    expect(screen.queryByText("安装时请求的 Bot Token Scopes")).toBeNull();
  });
});

describe("SlackIntegrationDetail back button glyph", () => {
  it("renders ← before the back label", async () => {
    state.fetchConfig.mockResolvedValue({ configured: false, status: "draft", channel_answers_enabled: false });
    renderIntl(<SlackIntegrationDetail />);
    await screen.findByRole("heading", { name: "Slack" });
    const message = catalogText("zh-CN", "settings.integrations.detail_common.back");
    const back = screen.getByRole("button", { name: (name) => name.endsWith(message) });
    expectRefreshedGlyph(back, "←", "start", message);
  });
});
