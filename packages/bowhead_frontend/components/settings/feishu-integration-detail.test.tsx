import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({
  params: new Map<string, string>([["stage", "01"]]),
  push: vi.fn(),
  replace: vi.fn(),
  fetchConfig: vi.fn(),
  fetchDiagnostics: vi.fn(),
  fetchHealth: vi.fn(),
  updateConfig: vi.fn(),
  createSetup: vi.fn(),
  fetchSetup: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (key: string) => state.params.get(key) ?? null,
  }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: state.push, replace: state.replace }),
}));
vi.mock("@/lib/feishu-integration-api", () => ({
  fetchFeishuAdminConfig: () => state.fetchConfig(),
  fetchFeishuDiagnostics: () => state.fetchDiagnostics(),
  fetchFeishuHealth: () => state.fetchHealth(),
  updateFeishuAdminConfig: (body: unknown) => state.updateConfig(body),
  createFeishuSetupSession: () => state.createSetup(),
  fetchFeishuSetupSession: (id: string) => state.fetchSetup(id),
}));

import { FeishuIntegrationDetail } from "./feishu-integration-detail";

const originalClipboard = navigator.clipboard;
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

const config = {
  source: "db",
  tenant_key: "default",
  app_type: "internal",
  enabled: true,
  event_ingress: "webhook",
  app_id: "cli_123",
  bot_open_id: "ou_bot",
  group_public_answers_enabled: false,
  app_secret_configured: true,
  verification_token_configured: true,
  encrypt_key_configured: true,
  secret_sources: {
    app_secret: "db",
    verification_token: "db",
    encrypt_key: "db",
  },
  updated_at: null,
  updated_by_user_id: null,
};

const diagnostics = {
  ok: true,
  enabled: true,
  degraded: false,
  app_type: "internal",
  event_ingress: "webhook",
  group_public_answers_enabled: false,
  auth_mode: "oidc",
  prod_auth_mode: true,
  long_connection: null,
  missing_credentials: [],
  missing_config: [],
  app_id_configured: true,
  app_secret_configured: true,
  verification_token_configured: true,
  encrypt_key_configured: true,
  bot_open_id_configured: true,
  event_store_configured: true,
  event_subscription_url: "https://example.com/api/v1/feishu/events",
  card_callback_url: "https://example.com/api/v1/feishu/card",
  tenant_token_cache_keys: [],
  required_event_subscriptions: ["im.message.receive_v1"],
  required_permissions: ["im:message"],
  setup_checklist: [
    {
      key: "app_id",
      label: "APP_ID",
      status: "ok",
      detail: "Configured",
    },
    {
      key: "console",
      label: "飞书控制台",
      status: "manual",
      detail: "Manual",
    },
  ],
  launch_record_template: "# Launch record",
};

afterEach(() => {
  cleanup();
  document.getElementById("integration-manual-copy")?.remove();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: originalClipboard,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: originalScrollIntoView,
  });
  state.params = new Map([["stage", "01"]]);
  vi.clearAllMocks();
});

function arrange() {
  state.fetchConfig.mockResolvedValue(config);
  state.fetchDiagnostics.mockResolvedValue(diagnostics);
  state.fetchHealth.mockResolvedValue({
    ok: true,
    enabled: true,
    degraded: false,
  });
  state.updateConfig.mockResolvedValue(config);
}

describe("FeishuIntegrationDetail", () => {
  it("separates the four stages and marks human work as manual", async () => {
    arrange();
    renderIntl(<FeishuIntegrationDetail />);

    await waitFor(() => {
      expect(screen.getByDisplayValue("cli_123")).toBeInTheDocument();
    });
    expect(screen.getAllByText("飞书控制台").length).toBeGreaterThan(0);
    expect(screen.getAllByText("自动诊断").length).toBeGreaterThan(0);
    expect(screen.getAllByText("生产验收").length).toBeGreaterThan(0);
    expect(screen.getAllByText("待人工核对")).toHaveLength(2);
    expect(screen.getAllByText("待人工验收")).toHaveLength(2);
    expect(screen.getByRole("radio", { name: "Webhook" })).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "长连接" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /停用事件入口/ })).toBeNull();

    fireEvent.click(
      screen.getAllByText("飞书控制台").find((node) => node.tagName === "STRONG")!,
    );
    const eventGroup = screen.getByRole("button", { name: /必需事件/ });
    const permissionGroup = screen.getByRole("button", { name: /必需权限/ });
    expect(eventGroup).toHaveAttribute("aria-expanded", "false");
    expect(permissionGroup).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(eventGroup);
    expect(eventGroup).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(
      screen.getAllByText("生产验收").find((node) => node.tagName === "STRONG")!,
    );
    expect(
      await screen.findByText(/管理员页不能生成绑定链接或二维码/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /绑定|解绑/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /自动.*smoke/i })).toBeNull();
  });

  it("intercepts a stage change when credentials are dirty", async () => {
    arrange();
    renderIntl(<FeishuIntegrationDetail />);

    const appId = await screen.findByLabelText("APP_ID");
    fireEvent.change(appId, { target: { value: "cli_changed" } });
    fireEvent.click(
      screen.getAllByText("飞书控制台").find((node) => node.tagName === "STRONG")!,
    );

    expect(
      await screen.findByRole("dialog", {
        name: "离开前保存修改？",
      }),
    ).toBeInTheDocument();
    expect(state.replace).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "放弃改动" }));
    expect(state.replace).toHaveBeenCalledWith(
      "/team/settings/integrations/feishu/?stage=02",
    );
  });

  it("requires confirmation before enabling public group answers", async () => {
    arrange();
    renderIntl(<FeishuIntegrationDetail />);

    const toggle = await screen.findByRole("switch", {
      name: "群聊公开回答",
    });
    fireEvent.click(toggle);
    expect(
      screen.getByRole("dialog", { name: "确认开启群聊公开回答" }),
    ).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(screen.getByRole("button", { name: "确认开启" }));
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("keeps an automatically healthy page folded without a stage query", async () => {
    arrange();
    state.params = new Map();
    const { container } = renderIntl(<FeishuIntegrationDetail />);

    await waitFor(() => {
      expect(screen.getByText("飞书 / Lark")).toBeInTheDocument();
      expect(container.querySelectorAll("details[open]")).toHaveLength(0);
    });
  });

  it("keeps runtime policy problems out of the automatic-pass state", async () => {
    arrange();
    state.params = new Map();
    state.fetchDiagnostics.mockResolvedValue({
      ...diagnostics,
      auth_mode: "dev",
      prod_auth_mode: false,
    });
    const { container } = renderIntl(<FeishuIntegrationDetail />);

    await waitFor(() => {
      expect(container.querySelectorAll("details[open]")).toHaveLength(1);
      expect(screen.getAllByText("需要处理").length).toBeGreaterThan(0);
      expect(
        screen.getAllByText("当前不是生产认证模式").length,
      ).toBeGreaterThan(0);
    });
  });

  it("maps a legacy disabled ingress to the master switch without showing a disabled choice", async () => {
    arrange();
    state.params = new Map([["stage", "03"]]);
    state.fetchConfig.mockResolvedValue({
      ...config,
      enabled: true,
      event_ingress: "disabled",
    });
    state.fetchDiagnostics.mockResolvedValue({
      ...diagnostics,
      event_ingress: "disabled",
    });

    renderIntl(<FeishuIntegrationDetail />);

    const masterSwitch = await screen.findByRole("switch", {
      name: "启用飞书集成",
    });
    expect(masterSwitch).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "Webhook" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByRole("button", { name: "Disabled" })).toBeNull();
    expect(screen.getAllByText("已停用").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未运行").length).toBeGreaterThan(0);
    expect(screen.getByText("自动检查已通过")).toBeInTheDocument();
    expect(screen.queryByText("自动检查通过")).toBeNull();

    expect(
      await screen.findByText("飞书集成已停用"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Bowhead 侧自动检查已通过")).toBeNull();
  });

  it("keeps a stopped integration with missing credentials visibly incomplete", async () => {
    arrange();
    state.params = new Map();
    state.fetchConfig.mockResolvedValue({
      ...config,
      enabled: false,
      app_id: "",
      bot_open_id: "",
      app_secret_configured: false,
      verification_token_configured: false,
    });
    state.fetchDiagnostics.mockResolvedValue({
      ...diagnostics,
      enabled: false,
      missing_credentials: [
        "app_id",
        "app_secret",
        "verification_token",
        "bot_open_id",
      ],
      app_id_configured: false,
      app_secret_configured: false,
      verification_token_configured: false,
      bot_open_id_configured: false,
    });
    state.fetchHealth.mockResolvedValue({
      ok: true,
      enabled: false,
      degraded: false,
    });

    renderIntl(<FeishuIntegrationDetail />);

    expect(
      await screen.findByText("需要配置 · 4 项待处理"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("已停用").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未运行").length).toBeGreaterThan(0);
    expect(screen.queryByText("自动检查已通过")).toBeNull();
  });

  it("skips an incomplete setup without writing configuration", async () => {
    arrange();
    state.params = new Map();
    state.fetchConfig.mockResolvedValue({
      ...config,
      enabled: false,
      app_id: "",
      app_secret_configured: false,
    });
    state.fetchDiagnostics.mockResolvedValue({
      ...diagnostics,
      enabled: false,
      missing_credentials: ["app_id", "app_secret"],
      app_id_configured: false,
      app_secret_configured: false,
    });
    state.fetchHealth.mockResolvedValue({
      ok: true,
      enabled: false,
      degraded: false,
    });

    renderIntl(<FeishuIntegrationDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "暂时跳过" }));

    expect(state.push).toHaveBeenCalledWith(
      "/team/settings/integrations/",
    );
    expect(state.updateConfig).not.toHaveBeenCalled();
  });

  it("opens official consoles and distinguishes callback copy actions", async () => {
    arrange();
    state.params = new Map([["stage", "02"]]);
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    renderIntl(<FeishuIntegrationDetail />);

    const feishuConsole = await screen.findByRole("link", {
      name: "打开飞书开放平台",
    });
    expect(feishuConsole).toHaveAttribute("href", "https://open.feishu.cn/app");
    expect(feishuConsole).toHaveAttribute("target", "_blank");
    expect(
      screen.getByRole("link", { name: "打开 Lark Developer Console" }),
    ).toHaveAttribute("href", "https://open.larksuite.com/app");
    expect(
      screen.getByRole("button", { name: "复制事件回调" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制卡片回调" }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledWith({
        behavior: "auto",
        block: "start",
      });
      expect(
        screen.getByText("飞书控制台", { selector: "strong" }).closest("summary"),
      ).toHaveFocus();
    });
  });

  it.each(["02", "03"] as const)(
    "exits stage %s without writing configuration",
    async (stage) => {
      arrange();
      state.params = new Map([["stage", stage]]);

      renderIntl(<FeishuIntegrationDetail />);

      const quickExit = await screen.findByTestId(
        `stage-${stage}-quick-exit`,
      );
      expect(quickExit).toHaveTextContent("暂时跳过");
      fireEvent.click(quickExit);

      expect(state.push).toHaveBeenCalledWith(
        "/team/settings/integrations/",
      );
      expect(state.updateConfig).not.toHaveBeenCalled();
    },
  );

  it("ends production acceptance without inventing a completion state", async () => {
    arrange();
    state.params = new Map([["stage", "04"]]);
    renderIntl(<FeishuIntegrationDetail />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "返回集成总览，稍后继续",
      }),
    );

    expect(state.push).toHaveBeenCalledWith(
      "/team/settings/integrations/",
    );
    expect(screen.getAllByText("待人工验收")).toHaveLength(2);
    expect(state.updateConfig).not.toHaveBeenCalled();
  });

  it("keeps manual console work out of automatic diagnostics", async () => {
    arrange();
    state.params = new Map([["stage", "03"]]);

    renderIntl(<FeishuIntegrationDetail />);

    expect(
      await screen.findByRole("button", { name: "已就绪 · 1 项" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Manual")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "已就绪 · 1 项" }));
    expect(
      screen.getByRole("button", { name: "已就绪 · 1 项" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/Configured/)).toBeInTheDocument();
    expect(screen.queryByText("Manual")).toBeNull();
  });

  it("allows a healthy stage to collapse from its summary", async () => {
    arrange();
    state.params = new Map([["stage", "02"]]);
    const { container } = renderIntl(<FeishuIntegrationDetail />);

    const stageTitle = await screen.findByText("飞书控制台", {
      selector: "strong",
    });
    expect(container.querySelectorAll("details[open]")).toHaveLength(1);
    fireEvent.click(stageTitle);

    expect(container.querySelectorAll("details[open]")).toHaveLength(0);
    expect(state.replace).toHaveBeenCalledWith(
      "/team/settings/integrations/feishu/",
    );
  });

  it("announces a completed recheck without changing manual states", async () => {
    arrange();
    state.params = new Map([["stage", "03"]]);
    renderIntl(<FeishuIntegrationDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "重新检查" }));

    expect(
      await screen.findByText("重新检查完成，页面状态已更新。"),
    ).toBeInTheDocument();
    expect(state.fetchDiagnostics).toHaveBeenCalledTimes(2);
    expect(screen.getAllByText("待人工核对")).toHaveLength(2);
    expect(screen.getAllByText("待人工验收")).toHaveLength(2);
  });

  it("selects the source text when clipboard access fails", async () => {
    arrange();
    state.params = new Map([["stage", "03"]]);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: vi.fn().mockRejectedValue(new Error("denied")),
      },
    });
    renderIntl(<FeishuIntegrationDetail />);

    fireEvent.click(
      await screen.findByRole("button", { name: "复制上线记录" }),
    );

    expect(
      await screen.findByText(
        "无法复制上线记录草稿，内容已选中，请按 Ctrl/Cmd+C 手动复制。",
      ),
    ).toBeInTheDocument();
    const fallback = document.getElementById(
      "integration-manual-copy",
    ) as HTMLTextAreaElement;
    expect(fallback).toHaveValue("# Launch record");
    expect(fallback.selectionStart).toBe(0);
    expect(fallback.selectionEnd).toBe("# Launch record".length);
  });

  describe("page acceptance A", () => {
    it("linkage rules render as a table and as narrow-screen list rows with the same content", async () => {
      arrange();
      renderIntl(<FeishuIntegrationDetail />);
      await screen.findByLabelText("APP_ID");
      const list = screen.getByRole("list", { name: "主开关与接入方式规则" });
      const rows = Array.from(list.querySelectorAll("li"));
      const tableRows = Array.from(document.querySelectorAll("table tbody tr"));
      expect(rows).toHaveLength(5);
      expect(tableRows).toHaveLength(5);
      rows.forEach((row, index) => {
        const [status, ingress, result] = Array.from(tableRows[index].querySelectorAll("td"), (td) => td.textContent);
        expect(row.querySelector(".ui-integ-linkage-row__title")).toHaveTextContent(result!);
        expect(row.querySelector(".ui-integ-linkage-row__meta")).toHaveTextContent(`事件入口 · ${ingress}`);
        expect(row.querySelector(".ui-integ-linkage-row__trail")).toHaveTextContent(status!);
      });
      expect(rows[0].querySelector(".ui-integ-linkage-row__trail")).toHaveClass("is-good");
    });

    it("保存交给保存栏：阶段操作栏不再有「保存配置」，改动后保存栏出现，保存与放弃都生效", async () => {
      arrange();
      renderIntl(<FeishuIntegrationDetail />);
      const appId = await screen.findByLabelText("APP_ID");
      expect(screen.queryByRole("button", { name: "保存配置" })).toBeNull();
      expect(screen.queryByText("有未保存的更改")).toBeNull();

      fireEvent.change(appId, { target: { value: "cli_changed" } });
      expect(await screen.findByText("有未保存的更改")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "放弃" }));
      expect(screen.getByLabelText("APP_ID")).toHaveValue("cli_123");
      expect(screen.queryByText("有未保存的更改")).toBeNull();

      fireEvent.change(screen.getByLabelText("APP_ID"), { target: { value: "cli_saved" } });
      fireEvent.click(await screen.findByRole("button", { name: "保存" }));
      await waitFor(() => {
        expect(state.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ app_id: "cli_saved" }));
      });
    });
  });
});


it("preserves an acknowledged save when the following status read fails", async () => {
  arrange();
  renderIntl(<FeishuIntegrationDetail />);
  const input = await screen.findByLabelText("APP_ID");
  state.updateConfig.mockResolvedValueOnce({ ...config, app_id: "cli_saved" });
  state.fetchDiagnostics.mockRejectedValueOnce(new Error("private refresh detail"));
  fireEvent.change(input, { target: { value: "cli_saved" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByText(/配置已保存，但连接状态未能刷新/)).toBeTruthy();
  expect(screen.queryByText("有未保存的更改")).toBeNull();
  expect(input).toHaveValue("cli_saved");
  fireEvent.click(screen.getAllByRole("button", { name: "重新检查" })[0]);
  await waitFor(() => expect(screen.queryByText(/配置已保存，但连接状态未能刷新/)).toBeNull());
  expect(state.updateConfig).toHaveBeenCalledOnce();
});
