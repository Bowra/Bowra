import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import PresetDirectory, { type ConnectorPreset } from "./preset-directory";

const PRESETS: ConnectorPreset[] = [
  { id: "gh", name: "GitHub", icon: "git", kind: "openapi", auth: "bearer", spec_url: "https://api.github.com/openapi.json" },
  { id: "gh-mcp", name: "GitHub MCP", icon: "git", kind: "mcp", auth: "oauth", mcp_url: "https://mcp.github.com/sse" },
  { id: "wx", name: "公开天气", icon: "cloud", kind: "openapi", auth: "none", spec_url: "https://w.example.com/openapi.json" },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PresetDirectory", () => {
  it("renders nothing when no presets (gated by empty prop)", () => {
    const { container } = renderIntl(<PresetDirectory presets={[]} onPick={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a card per preset with kind + auth pills", () => {
    renderIntl(<PresetDirectory presets={PRESETS} onPick={() => {}} />);
    expect(screen.getByText("GitHub")).toBeTruthy();
    expect(screen.getByText("GitHub MCP")).toBeTruthy();
    expect(screen.getByText("公开天气")).toBeTruthy();
    // kind pills
    expect(screen.getAllByText("REST").length).toBe(2);
    expect(screen.getAllByText("MCP").length).toBe(1);
    // auth pills — oauth → 授权, bearer → 密钥, none → 免鉴权
    expect(screen.getByText("授权")).toBeTruthy();
    expect(screen.getByText("密钥")).toBeTruthy();
    expect(screen.getByText("免鉴权")).toBeTruthy();
    // count line
    expect(screen.getByText("3 个可用")).toBeTruthy();
  });

  it("calls onPick with the picked preset", () => {
    const onPick = vi.fn();
    renderIntl(<PresetDirectory presets={PRESETS} onPick={onPick} />);
    fireEvent.click(screen.getByTitle("预填「GitHub MCP」并进入配置"));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick.mock.calls[0][0].id).toBe("gh-mcp");
  });

  it("shows the empty state (no authorized presets)", () => {
    renderIntl(<PresetDirectory presets={[]} onPick={() => {}} state="empty" />);
    expect(screen.getByText("暂无可用的预置连接器")).toBeTruthy();
  });

  it("filters via search when there are more than 6 presets", () => {
    const many: ConnectorPreset[] = Array.from({ length: 8 }, (_, i) => ({
      id: `c${i}`,
      name: i === 0 ? "Notion" : `Conn ${i}`,
      icon: "doc",
      kind: "mcp",
      auth: "oauth",
    }));
    renderIntl(<PresetDirectory presets={many} onPick={() => {}} />);
    const search = screen.getByPlaceholderText("搜索连接器…");
    fireEvent.change(search, { target: { value: "notion" } });
    expect(screen.getByText("Notion")).toBeTruthy();
    expect(screen.queryByText("Conn 3")).toBeNull();
  });
});
