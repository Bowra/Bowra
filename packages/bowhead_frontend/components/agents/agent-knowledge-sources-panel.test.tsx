import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { getAgentKnowledgeSources } from "@/lib/agents-api";
import { AgentKnowledgeSourcesPanel } from "./agent-knowledge-sources-panel";

vi.mock("@/lib/agents-api", () => ({ getAgentKnowledgeSources: vi.fn() }));
const changed = vi.fn();
const selected = { schema_version: 1, mode: "selected_files", file_ids: ["kbf_one"] };
const catalog = { enabled: true, available: true, items: [{ id: "kbf_one", name: "Source One", scope: "personal" }], selected_items: [{ id: "kbf_one", name: "Source One", scope: "personal" }], total: 1, max_selected: 64, top_k_min: 1, top_k_max: 50 };
function Harness({ initial = selected }: { initial?: unknown }) {
  const [value, setValue] = useState(initial);
  return <AgentKnowledgeSourcesPanel agentId="osa_1" value={value} disabled={false} onChange={(next) => { changed(next); setValue(next); }} />;
}
describe("Knowledge source selection", () => {
  beforeEach(() => { changed.mockReset(); vi.mocked(getAgentKnowledgeSources).mockReset().mockResolvedValue(catalog); });
  afterEach(cleanup);
  it("keeps removing the last source distinct from broadening to current context", async () => {
    renderIntl(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: "移除 Source One" }));
    expect(changed.mock.lastCall![0]).toEqual({ ...selected, file_ids: [] });
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "context_readable" } });
    expect(changed.mock.lastCall![0]).toEqual({ schema_version: 1, mode: "context_readable" });
  });
  it("preserves unavailable identities without exposing their names", async () => {
    vi.mocked(getAgentKnowledgeSources).mockResolvedValue({ ...catalog, items: [], selected_items: [] });
    renderIntl(<Harness />);
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    expect(screen.getByText("第 1 个已选来源当前不可用")).toBeTruthy();
    expect(screen.queryByText("Source One")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("ignores stale search responses", async () => {
    let complete!: (value: typeof catalog) => void;
    vi.mocked(getAgentKnowledgeSources).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; })).mockResolvedValueOnce({ ...catalog, items: [], selected_items: [] });
    renderIntl(<Harness />);
    await waitFor(() => expect(getAgentKnowledgeSources).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "new" } });
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    await act(async () => complete(catalog));
    expect(screen.queryByText("Source One")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("explains unavailable source services while preserving the selection", async () => {
    vi.mocked(getAgentKnowledgeSources).mockResolvedValue({ ...catalog, available: false, items: [], selected_items: [] });
    renderIntl(<Harness />);
    expect((await screen.findByRole("alert")).textContent).toContain("无法加载来源");
    expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(true);
    expect(changed).not.toHaveBeenCalled();
  });
  it("keeps unknown policy fields unchanged", async () => {
    renderIntl(<Harness initial={{ ...selected, future: true }} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
});
