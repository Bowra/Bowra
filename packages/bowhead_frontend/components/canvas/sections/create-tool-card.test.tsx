/**
 * CreateToolCard — source step.
 *
 * mcp-connect-repair F2: adding an MCP server used to have NO entry of its own. A pasted MCP
 * address went through the OpenAPI-first probe, which 502'd on every real MCP endpoint (116
 * measured 4/4), so the only way in was a curated preset. The backend now recovers, and this
 * covers the explicit third tab: it must take the DIRECT MCP branch, never the OpenAPI probe.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const previewSource = vi.fn();
const createSource = vi.fn();

vi.mock("@/lib/team-tool-sources-api", () => ({
  previewSource: (...args: unknown[]) => previewSource(...args),
  createSource: (...args: unknown[]) => createSource(...args),
  proposeDescriptions: vi.fn(),
  authorizeSource: vi.fn(),
}));

import { CreateToolCard } from "./create-tool-card";

function typeAddress(value: string) {
  const input = screen.getByPlaceholderText(/https:\/\//);
  fireEvent.change(input, { target: { value } });
}

describe("CreateToolCard source step", () => {
  beforeEach(() => {
    cleanup(); // This suite has no automatic cleanup; without manual cleanup, previous-case DOM leaves same-named elements
    previewSource.mockReset();
    createSource.mockReset();
  });

  it("offers a dedicated MCP server tab", () => {
    renderIntl(<CreateToolCard onClose={() => {}} />);
    expect(screen.getByRole("tab", { name: "MCP 服务器" })).toBeTruthy();
  });

  it("按来源类型解释数据会怎样被读取", () => {
    renderIntl(<CreateToolCard onClose={() => {}} />);

    expect(screen.getByText(/当前部署的出站策略/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
    expect(screen.getByText(/访问令牌仅用于该服务/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "直接粘贴 OpenAPI JSON" }));
    expect(screen.getByText(/不会额外抓取远程文档/)).toBeTruthy();
  });

  it("previews the MCP tab as kind=mcp, not as an OpenAPI spec_url", async () => {
    previewSource.mockResolvedValue({
      route_away: false,
      kind: "mcp",
      operations: [{ operation_id: "ask", method: "MCP", path: "", summary: "ask", is_write: false, tags: [] }],
      count: 1,
      tags: [],
      tools_schema: [{ name: "ask", description: "ask", input_schema: {} }],
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
    typeAddress("https://mcp.example.com/mcp");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));

    await waitFor(() => expect(previewSource).toHaveBeenCalled());
    expect(previewSource).toHaveBeenCalledTimes(1); // no OpenAPI-first round trip
    expect(previewSource).toHaveBeenCalledWith({ kind: "mcp", mcp_url: "https://mcp.example.com/mcp" });
  });

  it("carries the discovered MCP Prompt metadata cache into create", async () => {
    const prompts = [
      {
        name: "profitability",
        title: "Profitability",
        description: "Explain profitability",
        arguments: [
          {
            name: "period",
            description: "Reporting period",
            required: true,
          },
        ],
      },
    ];
    previewSource.mockResolvedValue({
      route_away: false,
      kind: "mcp",
      operations: [
        {
          operation_id: "search",
          method: "MCP",
          path: "",
          summary: "Search",
          is_write: false,
          tags: [],
        },
      ],
      count: 1,
      tags: [],
      tools_schema: [
        { name: "search", description: "Search", input_schema: {} },
      ],
      prompts_schema: prompts,
      prompts_count: 1,
    });
    createSource.mockResolvedValue({
      id: "ots_prompt",
      name: "Prompt server",
      op_count: 1,
      available_op_count: 1,
      approval_unavailable_count: 0,
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
    typeAddress("https://mcp.example.com/mcp");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));
    fireEvent.change(await screen.findByLabelText("来源名称"), {
      target: { value: "Prompt server" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建工具" }));

    await waitFor(() =>
      expect(createSource).toHaveBeenCalledWith(
        expect.objectContaining({ prompts_schema: prompts }),
      ),
    );
  });

  it("still previews the OpenAPI tab as spec_url", async () => {
    previewSource.mockResolvedValue({
      route_away: false,
      operations: [],
      count: 0,
      tags: [],
      openapi_spec_json: '{"openapi":"3.0.0","paths":{}}',
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    typeAddress("https://api.example.com/openapi.json");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));

    await waitFor(() => expect(previewSource).toHaveBeenCalled());
    expect(previewSource).toHaveBeenCalledWith({ spec_url: "https://api.example.com/openapi.json" });
  });
  it("carries an optional token into the MCP preview so a protected server can be listed", async () => {
    // A token-protected MCP server refuses an unauthenticated tools/list, and that 401 is
    // indistinguishable from "needs OAuth" — without sending the credential at preview time
    // GitHub's hosted server (PAT over Bearer) dead-ends in the OAuth flow.
    previewSource.mockResolvedValue({
      route_away: false,
      kind: "mcp",
      operations: [{ operation_id: "search", method: "MCP", path: "", summary: "s", is_write: false, tags: [] }],
      count: 1,
      tags: [],
      tools_schema: [{ name: "search", description: "s", input_schema: {} }],
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
    typeAddress("https://api.githubcopilot.com/mcp/");
    fireEvent.change(screen.getByLabelText("访问令牌"), { target: { value: "ghp_secret" } });
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));

    await waitFor(() => expect(previewSource).toHaveBeenCalled());
    expect(previewSource).toHaveBeenCalledWith({
      kind: "mcp",
      mcp_url: "https://api.githubcopilot.com/mcp/",
      auth_type: "bearer",
      auth: { value: "ghp_secret" },
    });
  });

  it("omits the credential when the MCP tab has no token", async () => {
    previewSource.mockResolvedValue({ route_away: false, kind: "mcp", operations: [], count: 0, tags: [], tools_schema: [] });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
    typeAddress("https://mcp.deepwiki.com/mcp");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));

    await waitFor(() => expect(previewSource).toHaveBeenCalled());
    expect(previewSource).toHaveBeenCalledWith({ kind: "mcp", mcp_url: "https://mcp.deepwiki.com/mcp" });
  });

  it("opens on the MCP tab when the Lead classified the address as MCP", () => {
    // The Lead already says "这是 hosted MCP 端点" in its answer; before kindHint it had no way to
    // pass that on, so the address landed under a field labelled 接口文档 (OpenAPI).
    renderIntl(<CreateToolCard onClose={() => {}} specUrlHint="https://gitmcp.io/docs" kindHint="mcp" />);
    expect(screen.getByRole("tab", { name: "MCP 服务器" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByPlaceholderText(/https:\/\//) as HTMLInputElement).value).toBe("https://gitmcp.io/docs");
  });

  it("keeps the OpenAPI tab when the Lead gave no classification", () => {
    renderIntl(<CreateToolCard onClose={() => {}} specUrlHint="https://api.example.com/openapi.json" />);
    expect(screen.getByRole("tab", { name: "接口文档 (OpenAPI)" }).getAttribute("aria-selected")).toBe("true");
  });

  it("keeps currently unavailable writes selectable and reports the authoritative create counts", async () => {
    previewSource.mockResolvedValue({
      route_away: false,
      kind: "openapi",
      draft_source_id: "ots_abcdef123456",
      draft_source_token: "signed-preview-token",
      operations: [
        { operation_id: "listWidgets", method: "GET", path: "/widgets", summary: "List", is_write: false, approval_executable: null, tags: [] },
        { operation_id: "createWidget", method: "POST", path: "/widgets", summary: "Create", is_write: true, approval_executable: false, tags: [] },
      ],
      count: 2,
      tags: [],
      query_auth_warnings: [],
      openapi_spec_json: JSON.stringify({ openapi: "3.0.0", servers: [{ url: "https://api.example.com" }], paths: {} }),
    });
    createSource.mockResolvedValue({
      id: "ots_abcdef123456",
      name: "Widgets",
      op_count: 2,
      available_op_count: 1,
      approval_unavailable_count: 1,
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    typeAddress("https://api.example.com/openapi.json");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));
    fireEvent.change(await screen.findByLabelText("来源名称"), { target: { value: "Widgets" } });

    expect(screen.getByText("写 · 当前不可执行")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择要导入的操作: createWidget" }));
    expect(screen.getByText(/会被保存，但不会提供给智能体/)).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: /仍要导入当前可执行/ })).toBeNull();

    const create = screen.getByRole("button", { name: "创建工具" });
    expect((create as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(create);
    await waitFor(() => expect(createSource).toHaveBeenCalledWith(expect.objectContaining({
      draft_source_id: "ots_abcdef123456",
      draft_source_token: "signed-preview-token",
      selected_op_ids: expect.arrayContaining(["listWidgets", "createWidget"]),
    })));
    expect(await screen.findByText(/1 个操作现在可供智能体使用/)).toBeTruthy();
    expect(screen.getByText(/另有 1 个写操作当前不可执行/)).toBeTruthy();
  });

  it("requires acknowledgement only for writes the approval executor can run", async () => {
    previewSource.mockResolvedValue({
      route_away: false,
      kind: "openapi",
      draft_source_id: "ots_123456abcdef",
      draft_source_token: "signed-preview-token",
      operations: [
        { operation_id: "updateWidget", method: "PATCH", path: "/widgets/{id}", summary: "Update", is_write: true, approval_executable: true, tags: [] },
      ],
      count: 1,
      tags: [],
      query_auth_warnings: [],
      openapi_spec_json: JSON.stringify({ openapi: "3.0.0", servers: [{ url: "https://api.example.com" }], paths: {} }),
    });

    renderIntl(<CreateToolCard onClose={() => {}} />);
    typeAddress("https://api.example.com/openapi.json");
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));
    fireEvent.change(await screen.findByLabelText("来源名称"), { target: { value: "Widgets" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "选择要导入的操作: updateWidget" }));

    expect(screen.getByText("写 · 需审批")).toBeTruthy();
    expect(screen.getByText(/每次执行都必须经过管理员审批/)).toBeTruthy();
    const create = screen.getByRole("button", { name: "创建工具" });
    expect((create as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "我已知晓，仍要导入当前可执行的写操作" }));
    expect((create as HTMLButtonElement).disabled).toBe(false);
  });
});
