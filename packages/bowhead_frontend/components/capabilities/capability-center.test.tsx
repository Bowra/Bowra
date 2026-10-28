import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/en-US.json";
import { renderIntl } from "@/__tests__/intl-render";
import type { MeUser } from "@/lib/auth-api";
import type { AuthSessionValue } from "@/lib/auth/auth-session-context";
import {
  fetchCapabilityDiscoveryCatalog,
  type CapabilityDiscoveryCatalog,
} from "@/lib/capability-catalog-api";
import { CapabilityCenter } from "./capability-center";

const state = vi.hoisted(() => ({ query: "", session: null as AuthSessionValue | null }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(state.query), useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => state.session }));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children, mobileTitle }: { children: React.ReactNode; mobileTitle: string }) => (
    <>
      <div data-testid="bar-title">{mobileTitle}</div>
      {children}
    </>
  ),
}));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/lib/capability-catalog-api", () => ({ fetchCapabilityDiscoveryCatalog: vi.fn() }));

const member = {
  id: "u_1",
  team_id: "team_1",
  team_role: "member",
  capabilities: {
    subagent_creator: true,
    subagent_management_v2: true,
    personal_mcp: true,
  },
} as MeUser;
function authenticate(me: MeUser) {
  state.session = {
    me,
    status: "authenticated",
    error: null,
    retry: vi.fn(),
    revalidateCapabilities: vi.fn(),
  };
}

describe("CapabilityCenter member flows", () => {
  beforeEach(() => {
    state.query = "";
    authenticate(member);
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockReset();
  });
  afterEach(() => {
    cleanup();
    delete window.__BOWHEAD_BASE_PATH__;
  });

  it("retains personal authoring without team management", () => {
    state.query = "view=my";
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(screen.getByRole("link", { name: /Open My Agents/ }).getAttribute("href")).toBe(
      "/bowhead/agents/manage/created/",
    );
    expect(screen.queryByRole("link", { name: "Team Management" })).toBeNull();
    expect(screen.getByRole("heading", { name: "My Skills" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "My Connections" })).toBeTruthy();
    expect(fetchCapabilityDiscoveryCatalog).not.toHaveBeenCalled();
  });

  it("keeps the Plugin and Tool catalog cards for members while Plugin state is unavailable", () => {
    state.query = "";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(screen.getByRole("link", { name: /Open Plugins/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Open Tools/ })).toBeTruthy();
  });

  it("denies a member team view without rendering configuration links", () => {
    state.query = "view=team";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(screen.getByRole("alert").textContent).toContain("do not have access");
    expect(document.querySelector('a[href="/team/settings/tools/"]')).toBeNull();
    expect(fetchCapabilityDiscoveryCatalog).not.toHaveBeenCalled();
  });

  it("supports catalog failure and retry without showing transport diagnostics", async () => {
    state.query = "type=capabilities";
    vi.mocked(fetchCapabilityDiscoveryCatalog)
      .mockRejectedValueOnce(new Error("secret transport detail"))
      .mockResolvedValueOnce({
        tools: [],
        capabilities: [
          {
            id: "tcap_1",
            name: "Research",
            description: "Read sources",
            tool_count: 2,
            skill_count: 1,
          },
        ],
      });
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Unable to load"));
    expect(screen.queryByText("secret transport detail")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Research" })).toBeTruthy());
    expect(screen.getByText("2 tools · 1 skill")).toBeTruthy();
  });

  it("discards late catalog results after changing teams", async () => {
    state.query = "type=capabilities";
    let finish!: (value: CapabilityDiscoveryCatalog) => void;
    vi.mocked(fetchCapabilityDiscoveryCatalog)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValueOnce({ tools: [], capabilities: [] });
    const rendered = renderIntl(<CapabilityCenter />, { locale: "en-US" });
    authenticate({ ...member, team_id: "team_2" });
    rendered.rerender(
      <NextIntlClientProvider locale="en-US" messages={messages}>
        <CapabilityCenter />
      </NextIntlClientProvider>,
    );
    await waitFor(() => expect(fetchCapabilityDiscoveryCatalog).toHaveBeenCalledTimes(2));
    await act(async () =>
      finish({
        tools: [],
        capabilities: [
          {
            id: "tcap_old",
            name: "Old team data",
            description: "Private",
            tool_count: 1,
            skill_count: 0,
          },
        ],
      }),
    );
    expect(screen.queryByText("Old team data")).toBeNull();
  });

  it.each(["en-US", "zh-CN"] as const)("shows enabled system Plugins separately from an empty team list in %s", async (locale) => {
    state.query = "type=capabilities";
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({
      tools: [], capabilities: [], builtin_plugins: [{ id: "builtin:settings-personal-appearance@2", name: "settings-personal-appearance", tool_count: 7, skill_count: 1 }],
    });
    renderIntl(<CapabilityCenter />, { locale });
    const label = locale === "zh-CN" ? "个人外观" : "Personal Appearance";
    await waitFor(() => expect(screen.getByRole("heading", { name: label })).toBeTruthy());
    expect(screen.getByRole("region", { name: locale === "zh-CN" ? "内置插件" : "Built-in Plugins" })).toBeTruthy();
    // With no team Plugins the team group is hidden, as in the Agent picker.
    expect(screen.queryByRole("region", { name: locale === "zh-CN" ? "团队插件" : "Team Plugins" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Manage Team Plugins" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: label } });
    expect(screen.getByRole("heading", { name: label })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "missing-plugin" } });
    expect(screen.queryByRole("heading", { name: label })).toBeNull();
  });

  it("keeps disabled system Plugins and unconfigured third-party operations out of discovery", async () => {
    state.query = "type=capabilities";
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({ tools: [], capabilities: [], builtin_plugins: [] });
    const rendered = renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await waitFor(() => expect(screen.getByText("No resources are available yet.")).toBeTruthy());
    expect(screen.queryByRole("region", { name: "Built-in Plugins" })).toBeNull();
    state.query = "type=tools";
    rendered.rerender(<NextIntlClientProvider locale="en-US" messages={messages}><CapabilityCenter /></NextIntlClientProvider>);
    await waitFor(() => expect(fetchCapabilityDiscoveryCatalog).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("heading", { name: "Read Google Docs, Sheets and Slides" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Query Power BI" })).toBeNull();
  });

  it("shows and searches operations returned for enabled third-party connections", async () => {
    state.query = "type=tools";
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({
      capabilities: [], builtin_plugins: [],
      tools: [
        { stable_ref: "teammcp.ibt_finance.pbi_execute_dax", display_name: "Query Power BI", description: "Query the selected semantic model", source_display_name: "Power BI - Finance" },
        { stable_ref: "teammcp.ibt_google.gworkspace_read", display_name: "Read Google Docs, Sheets and Slides", description: "Read linked files", source_display_name: "Google Workspace - Team" },
      ].map((tool) => ({ ...tool, display_name_en: tool.display_name, description_en: tool.description, display_name_de: "", description_de: "", display_name_fr: "", description_fr: "", source_kind: "team_mcp", risk: "read", selectable: true, locked_reason: null })),
    });
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Query Power BI" })).toBeTruthy());
    expect(screen.getByRole("heading", { name: "Read Google Docs, Sheets and Slides" })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Power BI - Finance" } });
    expect(screen.getByRole("heading", { name: "Query Power BI" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Read Google Docs, Sheets and Slides" })).toBeNull();
  });

  it.each([
    ["", "Capability Center"],
    ["type=tools", "Tools"],
    ["type=capabilities", "Plugins"],
    ["view=my&type=tools", "Capability Center"],
  ])("shows the top-bar title as the page H1 for %j", async (query, title) => {
    state.query = query;
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({ tools: [], capabilities: [], builtin_plugins: [] });
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    // Equal text lets the large-title pattern show the title once.
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(title);
    expect(screen.getByTestId("bar-title").textContent).toBe(title);
  });
});
