import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({ push: vi.fn(), fetchOverview: vi.fn() }));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: state.push }),
}));
vi.mock("@/lib/api", () => ({
  fetchIntegrationOverview: () => state.fetchOverview(),
}));

import { SettingsIntegrations } from "./integrations";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SettingsIntegrations", () => {
  it("shows only enabled, healthy providers as connected", async () => {
    state.fetchOverview.mockResolvedValue({
      items: [
        {
          provider: "feishu",
          available: true,
          configured: true,
          enabled: true,
          healthy: true,
        },
        {
          provider: "slack",
          available: true,
          configured: true,
          enabled: false,
          healthy: false,
        },
        {
          provider: "teams",
          available: true,
          configured: false,
          enabled: false,
          healthy: false,
        },
      ],
    });

    renderIntl(<SettingsIntegrations />);

    expect(await screen.findByText("运行正常")).toBeInTheDocument();
    expect(
      screen.getByText("已保存配置，可继续完成接入。"),
    ).toBeInTheDocument();
    expect(screen.getByText("尚未配置。")).toBeInTheDocument();
    expect(screen.queryByText("第三方回调地址")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "开始接入" })[0]);
    expect(state.push).toHaveBeenCalledWith(
      "/team/settings/integrations/slack/",
    );
  });

  it("does not offer Slack when the deployment does not support it", async () => {
    state.fetchOverview.mockResolvedValue({
      items: [
        {
          provider: "feishu",
          available: true,
          configured: false,
          enabled: false,
          healthy: false,
        },
        {
          provider: "slack",
          available: false,
          configured: false,
          enabled: false,
          healthy: false,
        },
        {
          provider: "teams",
          available: true,
          configured: false,
          enabled: false,
          healthy: false,
        },
      ],
    });

    renderIntl(<SettingsIntegrations />);

    expect(await screen.findByText("飞书")).toBeInTheDocument();
    expect(screen.queryByText("Slack")).not.toBeInTheDocument();
  });
});
