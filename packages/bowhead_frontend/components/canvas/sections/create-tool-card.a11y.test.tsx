import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { previewSource } from "@/lib/team-tool-sources-api";

import { CreateToolCard } from "./create-tool-card";

vi.mock("@/lib/team-tool-sources-api", () => ({
  authorizeSource: vi.fn(),
  createSource: vi.fn(),
  previewSource: vi.fn(),
  proposeDescriptions: vi.fn(),
}));

describe("CreateToolCard accessibility", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("labels fields and moves focus with the settings flow", async () => {
    vi.mocked(previewSource).mockResolvedValue({
      route_away: false,
      kind: "openapi",
      operations: [
        {
          operation_id: "listWidgets",
          method: "GET",
          path: "/widgets",
          summary: "列出组件",
          is_write: false,
          tags: [],
        },
      ],
      count: 1,
      tags: [],
      query_auth_warnings: [],
      openapi_spec_json: JSON.stringify({
        openapi: "3.0.0",
        servers: [{ url: "https://api.example.com" }],
        paths: {},
      }),
    });

    renderIntl(
      <CreateToolCard manageStepFocus onClose={vi.fn()} />,
    );

    const sourceUrl = screen.getByRole("textbox", {
      name: "接口文档 (OpenAPI)",
    });
    expect(new Set(screen.getAllByRole("tab").map((tab) => tab.getAttribute("aria-controls"))).size).toBe(3);
    await waitFor(() => expect(document.activeElement).toBe(sourceUrl));
    fireEvent.change(sourceUrl, {
      target: { value: "https://api.example.com/openapi.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "解析" }));

    const sourceName = await screen.findByLabelText("来源名称");
    await waitFor(() => expect(document.activeElement).toBe(sourceName));
    expect(screen.getByLabelText("接口地址 (Base URL)")).toBeTruthy();
    expect(screen.getByLabelText("认证方式")).toBeTruthy();
    expect(
      screen.getByRole("checkbox", { name: "选择要导入的操作: listWidgets" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("textbox", {
        name: "给这个操作一句清晰的描述（可用时，智能体会据此判断何时调用）: listWidgets",
      }),
    ).toBeTruthy();

    fireEvent.change(screen.getByLabelText("认证方式"), {
      target: { value: "bearer" },
    });
    expect(screen.getByLabelText("Bearer Token")).toBeTruthy();
  });
});
