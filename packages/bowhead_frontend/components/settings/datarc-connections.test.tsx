/**
 * SettingsDatarcConnections render and progressive-error-disclosure tests.
 * No backend/stack: the API module is mocked, while real zh-CN/en-US catalogs
 * verify the user-facing copy and the shared diagnostic disclosure.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import deMessages from "@/messages/de-DE.json";
import zhMessages from "@/messages/zh-CN.json";

const listConnectionsMock = vi.fn();
const createConnectionMock = vi.fn();
const validateConnectionMock = vi.fn();
const rotateConnectionKeyMock = vi.fn();
const setConnectionStatusMock = vi.fn();
vi.mock("@/lib/admin-datarc-connections-api", () => ({
  listConnections: (...a: unknown[]) => listConnectionsMock(...a),
  createConnection: (...a: unknown[]) => createConnectionMock(...a),
  validateConnection: (...a: unknown[]) => validateConnectionMock(...a),
  rotateConnectionKey: (...a: unknown[]) => rotateConnectionKeyMock(...a),
  setConnectionStatus: (...a: unknown[]) => setConnectionStatusMock(...a),
  isDatarcConnectionErrorCode: (value: unknown) =>
    typeof value === "string" &&
    value.startsWith("datarc_connection_"),
}));

vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: () => true,
}));

import { SettingsDatarcConnections } from "./datarc-connections";

const _CONN = {
  id: "dc_1",
  team_id: "team_a",
  name: "prod",
  base_url: "https://demo.datarc.cn",
  account_uid: "acc",
  key_hint: "wxyz",
  status: "active" as const,
  created_at: "t",
  updated_at: "t",
};

const _REQUEST_ID = "0123456789abcdef0123456789abcdef";

function connectionApiError(
  code:
    | "datarc_connection_auth_rejected"
    | "datarc_connection_load_failed",
  rawMessage: string,
) {
  return new ApiError(503, rawMessage, {
    code,
    requestId: _REQUEST_ID,
    diagnostics: {
      schemaVersion: 1,
      disclosure: "admin",
      status: 503,
      code,
      diagnosticCode: "datarc_connection_failure",
      requestId: _REQUEST_ID,
      safeDetail:
        code === "datarc_connection_load_failed"
          ? "phase=load; failure_class=storage"
          : "phase=validate; failure_class=access",
      retryable: code === "datarc_connection_load_failed",
    },
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SettingsDatarcConnections", () => {
  it("does not blame the connection for a programming exception while loading", async () => {
    listConnectionsMock.mockRejectedValueOnce(new TypeError("Cannot read private configuration"));
    renderIntl(<SettingsDatarcConnections />);
    expect(await screen.findByText(zhMessages.settings.datarc.errors.load_failed)).toBeTruthy();
    expect(screen.queryByText(zhMessages.settings.datarc.errors.service_unreachable)).toBeNull();
    expect(screen.queryByText(/private configuration/)).toBeNull();
  });

  it("renders the empty state when there are no connections", async () => {
    listConnectionsMock.mockResolvedValue([]);
    renderIntl(<SettingsDatarcConnections />);
    expect(
      await screen.findByText(
        "暂无连接。添加一个以启用本团队的 BI / 数据查询。",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByText(/当前团队同一时间只有一个启用/),
    ).toBeNull();
  });

  it("renders a connection card with a MASKED key hint (never the secret)", async () => {
    listConnectionsMock.mockResolvedValue([_CONN]);
    renderIntl(<SettingsDatarcConnections />);
    expect(await screen.findByText("prod")).toBeTruthy();
    expect(
      screen.getByText(/北极九章数据源同一时间只能启用一个/),
    ).toBeTruthy();
    expect(screen.getByText(/••••wxyz/)).toBeTruthy();
    expect(screen.getByText("https://demo.datarc.cn")).toBeTruthy();
  });

  it("chooses the source type before opening its configuration", async () => {
    listConnectionsMock.mockResolvedValue([]);
    renderIntl(<SettingsDatarcConnections />);
    await screen.findByText(
      "暂无连接。添加一个以启用本团队的 BI / 数据查询。",
    );
    fireEvent.click(screen.getByText("+ 添加数据源"));
    expect(screen.getByText("配置北极九章")).toBeTruthy();
    expect(
      (
        screen.getByRole("textbox", {
          name: "Base URL",
        }) as HTMLInputElement
    ).value,
    ).toBe("");
  });

  it("requires an explicit activation choice when the same source type is active", async () => {
    listConnectionsMock.mockResolvedValue([_CONN]);
    createConnectionMock.mockResolvedValue({ ..._CONN, id: "dc_2", status: "archived" });
    renderIntl(<SettingsDatarcConnections />);
    await screen.findByText("prod");

    fireEvent.click(screen.getByText("+ 添加数据源"));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "saved-only" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://new.datarc.cn" } });
    fireEvent.change(screen.getByLabelText("鉴权密钥"), { target: { value: "new-secret" } });

    expect((screen.getByText("保存") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/仅保留新配置/));
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => {
      expect(createConnectionMock).toHaveBeenCalledWith(expect.objectContaining({ status: "archived" }));
    });
  });

  it("localizes a typed load error and copies only audited diagnostics", async () => {
    const rawMessage =
      "postgresql://internal.example secret=upstream-password";
    listConnectionsMock.mockRejectedValue(
      connectionApiError(
        "datarc_connection_load_failed",
        rawMessage,
      ),
    );
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderIntl(<SettingsDatarcConnections />);

    expect(
      await screen.findByText(
        "暂时无法加载数据源连接，请刷新页面后重试。",
      ),
    ).toBeTruthy();
    expect(screen.getByText("查看技术详情")).toBeTruthy();
    expect(screen.getByText(_REQUEST_ID)).toBeTruthy();
    expect(document.body.textContent).not.toContain(rawMessage);
    expect(
      screen.queryByText(
        "暂无连接。添加一个以启用本团队的 BI / 数据查询。",
      ),
    ).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "复制诊断信息" }),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain(
      "error_code: datarc_connection_load_failed",
    );
    expect(copied).toContain(`request_id: ${_REQUEST_ID}`);
    expect(copied).toContain(
      "safe_detail: phase=load; failure_class=storage",
    );
    expect(copied).not.toContain("internal.example");
    expect(copied).not.toContain("upstream-password");
  });

  it("shows the German license message in the German interface, not the Chinese one", async () => {
    // Previously only English / Other were distinguished, so authorization-related errors in the German UI used the Chinese catalog.
    listConnectionsMock.mockRejectedValue(
      new ApiError(403, "seat limit", { code: "seat_exceeded" }),
    );

    renderIntl(<SettingsDatarcConnections />, { locale: "de-DE" });

    expect(
      await screen.findByText(deMessages.entitlement_error.codes.seat_exceeded),
    ).toBeTruthy();
    expect(screen.queryByText(zhMessages.entitlement_error.codes.seat_exceeded)).toBeNull();
  });

  it("explains a shared team-role denial instead of showing a generic load failure", async () => {
    listConnectionsMock.mockRejectedValue(
      new ApiError(403, "team admin required", {
        code: "insufficient_role",
      }),
    );

    renderIntl(<SettingsDatarcConnections />);

    expect(
      await screen.findByText(
        "当前账号权限不足，无法执行此操作；请联系团队负责人。",
      ),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "team admin required",
    );
    expect(screen.queryByText("查看技术详情")).toBeNull();
  });

  it("shows an actionable English message for a typed validation failure", async () => {
    listConnectionsMock.mockResolvedValue([]);
    validateConnectionMock.mockRejectedValue(
      connectionApiError(
        "datarc_connection_auth_rejected",
        "Authorization: Bearer upstream-secret",
      ),
    );
    renderIntl(<SettingsDatarcConnections />, {
      locale: "en-US",
    });
    await screen.findByText(
      "No connections yet. Add one to enable BI / data queries for this team.",
    );
    fireEvent.click(screen.getByText("+ Add data source"));
    fireEvent.change(
      screen.getByRole("textbox", { name: "Base URL" }),
      { target: { value: "https://demo.datarc.cn" } },
    );
    fireEvent.change(screen.getByLabelText("Auth key"), {
      target: { value: "client-secret" },
    });
    fireEvent.click(screen.getByText("Test connection"));

    expect(
      await screen.findByText(
        "Datarc rejected these connection credentials. Check the auth key and account permissions, then try again.",
      ),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("upstream-secret");
    expect(screen.getByText("View technical details")).toBeTruthy();
  });
});
