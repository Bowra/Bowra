import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { MeUser } from "@/lib/auth-api";
import type { AuthSessionValue } from "@/lib/auth/auth-session-context";
import { fetchCapabilityDiscoveryCatalog } from "@/lib/capability-catalog-api";
import {
  fetchBuiltinPlugins,
  fetchBuiltinTools,
  fetchDisableImpact,
  setBuiltinPluginMyState,
  setBuiltinPluginTeamState,
  setBuiltinToolTeamState,
  type BuiltinPluginState,
} from "@/lib/builtin-resources-api";
import { toast } from "@/lib/use-toast";
import { listSources, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";
import type { DiscoveryTool } from "@/lib/capability-catalog-api";
import { CapabilityCenter } from "./capability-center";

const state = vi.hoisted(() => ({ query: "", session: null as AuthSessionValue | null }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(state.query), useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => state.session }));
vi.mock("@/components/shell/AppShell", () => ({ AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/lib/capability-catalog-api", () => ({ fetchCapabilityDiscoveryCatalog: vi.fn() }));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/lib/team-tool-sources-api", () => ({ listSources: vi.fn() }));
vi.mock("@/lib/builtin-resources-api", () => ({
  fetchBuiltinPlugins: vi.fn(),
  fetchBuiltinTools: vi.fn(),
  fetchDisableImpact: vi.fn(),
  setBuiltinPluginMyState: vi.fn(),
  setBuiltinPluginTeamState: vi.fn(),
  setBuiltinToolTeamState: vi.fn(),
  setTeamPluginMyState: vi.fn(),
}));

function plugin(name: string, tools: string[], extra: Partial<BuiltinPluginState> = {}): BuiltinPluginState {
  return {
    name,
    version: 1,
    family: "general",
    selectable: true,
    listing: { "en-US": { name: name === "web-research" ? "Web research" : "Email", description: "Desc" } },
    tools,
    tool_count: tools.length,
    skill_count: 0,
    prompt_modules: [],
    availability: [],
    team_disabled: false,
    my_disabled: false,
    ...extra,
  };
}

function authenticate(role: "owner" | "member") {
  state.session = {
    me: { id: "u_1", team_id: "team_1", team_role: role, capabilities: {} } as MeUser,
    status: "authenticated",
    error: null,
    retry: vi.fn(),
    revalidateCapabilities: vi.fn(),
  };
}

describe("Capability Center resource state", () => {
  beforeEach(() => {
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({ tools: [], builtin_plugins: [], capabilities: [] });
    vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
      plugins: [plugin("web-research", ["web_search"]), plugin("email", ["send_email"], { version: 2 })],
      team_plugins_my_disabled: [],
    });
    vi.mocked(fetchBuiltinTools).mockResolvedValue([
      { name: "web_search", risk: "read", enabled: true },
      { name: "send_email", risk: "external_write", enabled: false },
    ]);
    vi.mocked(fetchDisableImpact).mockResolvedValue({
      kind: "builtin_plugin",
      id: "web-research",
      system_agents: [{ name: "lead" }],
      team_agents: [{ id: "ts_1", name: "weekly", display_name: "Weekly helper" }],
      scheduled_tasks: [{ id: "job_1", name: "Monday digest", agent_id: "ts_1" }],
    });
    vi.mocked(setBuiltinPluginTeamState).mockResolvedValue();
    vi.mocked(setBuiltinPluginMyState).mockResolvedValue();
    vi.mocked(setBuiltinToolTeamState).mockResolvedValue();
    vi.mocked(listSources).mockResolvedValue([]);
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows one resource-sectioned overview and narrows it with Only mine", async () => {
    vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
      plugins: [plugin("web-research", ["web_search"]), plugin("email", ["send_email"], { my_disabled: true })],
      team_plugins_my_disabled: [],
    });
    authenticate("owner");
    state.query = "";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("Web research")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "Plugins" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "Tools" })).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "Only mine" }));
    expect(screen.queryByText("Web research")).toBeNull();
    expect(screen.getByText("Email")).toBeTruthy();
    expect(screen.getByText("You turned off 1 Plugin")).toBeTruthy();
    expect(screen.queryByRole("heading", { level: 2, name: "Tools" })).toBeNull();
  });

  it("marks a Plugin whose Tools are not configured as not ready", async () => {
    vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
      plugins: [plugin("image-generation", ["image_generate"], { ready: false, unavailable_tools: ["image_generate"] })],
      team_plugins_my_disabled: [],
    });
    authenticate("member");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("Not ready")).toBeTruthy();
  });

  it("previews who is affected before a team disables a Plugin", async () => {
    authenticate("owner");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("v2")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Turn “Web research” on or off for the team" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Turning off “Web research” affects 2 agents")).toBeTruthy();
    expect(within(dialog).getByText("Weekly helper")).toBeTruthy();
    expect(within(dialog).getByText("Monday digest")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(setBuiltinPluginTeamState).toHaveBeenCalledWith("web-research", true));
  });

  it("applies a personal disable at once and hides the team switch from members", async () => {
    authenticate("member");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await screen.findByText("Web research");
    expect(screen.queryByRole("switch", { name: /for the team/ })).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "Turn “Web research” on or off for me" }));
    await waitFor(() => expect(setBuiltinPluginMyState).toHaveBeenCalledWith("web-research", true));
    expect(fetchDisableImpact).not.toHaveBeenCalled();
  });

  it("lists settings Plugins in a closed group of their own with their switches", async () => {
    vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
      plugins: [
        plugin("web-research", ["web_search"]),
        plugin("settings-team-administration", [], { family: "settings", selectable: false, listing: {} }),
      ],
      team_plugins_my_disabled: [],
    });
    authenticate("owner");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });

    const row = (await screen.findByText("Team Administration Settings")).closest("li")!;
    const group = row.closest("details")!;
    expect(group.open).toBe(false);
    expect(within(group).getByRole("heading", { name: /Used by the settings assistant/ }).textContent).toContain("1");
    expect(screen.queryByText("settings-team-administration")).toBeNull();
    expect(screen.getByRole("heading", { name: /Built-in Plugins/ }).textContent).toContain("1");
    // The switches decide which settings forms the assistant may open, so they stay.
    expect(within(row).getByRole("switch", { name: /“Team Administration Settings” on or off for the team/ })).toBeTruthy();
    fireEvent.click(within(row).getByRole("switch", { name: "Turn “Team Administration Settings” on or off for me" }));
    await waitFor(() => expect(setBuiltinPluginMyState).toHaveBeenCalledWith("settings-team-administration", true));
  });

  it("opens the settings group when a search matches one of its Plugins", async () => {
    vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
      plugins: [plugin("web-research", ["web_search"]), plugin("settings-space-preferences", [], { family: "settings", selectable: false, listing: {} })],
      team_plugins_my_disabled: [],
    });
    authenticate("member");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await screen.findByText("Space Preferences");
    fireEvent.change(screen.getByPlaceholderText(/Search/), { target: { value: "space pref" } });
    expect(screen.getByText("Space Preferences").closest("details")!.open).toBe(true);
    expect(screen.queryByText("Web research")).toBeNull();
  });

  it("offers Undo only after a personal disable was saved", async () => {
    vi.mocked(setBuiltinPluginMyState).mockRejectedValueOnce(new Error("offline"));
    authenticate("member");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await screen.findByText("Web research");
    fireEvent.click(screen.getByRole("switch", { name: "Turn “Web research” on or off for me" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "error", text: "Couldn't save. Try again." }));
    expect(vi.mocked(toast).mock.calls.some(([arg]) => (arg as { undo?: boolean }).undo)).toBe(false);
  });

  it("asks an administrator to confirm the risk before turning on a high-risk Tool", async () => {
    authenticate("owner");
    state.query = "type=tools";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    const toggle = await screen.findByRole("switch", { name: "Turn “Send Email” on or off" });
    fireEvent.click(toggle);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(setBuiltinToolTeamState).toHaveBeenCalledWith("send_email", { enabled: true, acknowledge_risk: true }),
    );
  });

  it("shows Tool details with who uses it and no per-call confirmation setting", async () => {
    authenticate("owner");
    state.query = "type=tools";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    fireEvent.click(await screen.findByRole("button", { name: "Details for “Web Search”" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Agents and scheduled tasks using it")).toBeTruthy();
    expect(await within(dialog).findByText("Weekly helper")).toBeTruthy();
    // Once a user grants access nothing asks again, so there is no "Always confirm" setting.
    expect(within(dialog).queryByRole("checkbox", { name: /Always confirm/ })).toBeNull();
    expect(fetchDisableImpact).toHaveBeenCalledWith("builtin_tool", "web_search");
  });

  it("summarizes Plugins and high-risk Tools on the overview", async () => {
    authenticate("owner");
    state.query = "";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("1 high-risk tool not turned on")).toBeTruthy();
    expect(screen.getByText("Built-in 2 · Team 0 · Off 0")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Manage team Plugins" })).toBeTruthy();
    expect(screen.queryByRole("heading", { level: 3, name: "Plugins" })).toBeNull();
    expect(screen.getByText("Built-in 2 · External 0")).toBeTruthy();
    expect(screen.getAllByText("v2").length).toBe(1);
  });

  it("counts team Plugins and external Tools by source on the overview", async () => {
    const tool = (stable_ref: string, source_kind: string, source_display_name: string) =>
      ({ stable_ref, display_name: stable_ref, description: "", source_kind, source_display_name, risk: "read", selectable: true, locked_reason: null }) as never;
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({
      tools: [tool("web_search", "native", "Bowhead"), tool("addPet", "teammcp", "pet"), tool("getPet", "teammcp", "pet"), tool("search", "mcp", "Cloudflare")],
      builtin_plugins: [],
      capabilities: [{ id: "tcap_1", name: "Store pack", description: "", tool_count: 2, skill_count: 1 }],
    });
    authenticate("member");
    state.query = "";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("Built-in 2 · Team 1 · Off 0")).toBeTruthy();
    expect(screen.getByText("Built-in 2 · External 3")).toBeTruthy();
    expect(screen.getByText("Sources: pet · Cloudflare")).toBeTruthy();
  });

  it("counts the same built-in Tools on the overview and the Tools page while Google Workspace is off", async () => {
    vi.mocked(fetchBuiltinTools).mockResolvedValue([
      { name: "web_search", risk: "read", enabled: true },
      { name: "send_email", risk: "external_write", enabled: true },
      { name: "google_doc_write", risk: "external_write", enabled: false },
    ]);
    authenticate("owner");
    state.query = "";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    expect(await screen.findByText("Built-in 2 · External 0")).toBeTruthy();
    expect(screen.queryByText(/high-risk tool/)).toBeNull();
    cleanup();
    state.query = "type=tools";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    const heading = await screen.findByRole("heading", { level: 2, name: /Built-in Tools/ });
    expect(heading.textContent).toBe("Built-in Tools 2");
  });

  it("waits for the Plugin state, and explains a failed state load with a retry", async () => {
    // On 116 the catalog answered about two seconds before the Plugin state; until then the page listed
    // only the settings-assistant Plugins, as Built-in Plugins one could choose for an Agent.
    let answer!: (value: null) => void;
    vi.mocked(fetchBuiltinPlugins).mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({
      tools: [],
      builtin_plugins: [{ id: "builtin:settings-space-preferences@5", name: "settings-space-preferences", tool_count: 7, skill_count: 1 }],
      capabilities: [],
    });
    authenticate("owner");
    state.query = "type=plugins";
    renderIntl(<CapabilityCenter />, { locale: "en-US" });
    await waitFor(() => expect(fetchCapabilityDiscoveryCatalog).toHaveBeenCalled());
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.queryByText("Space Preferences")).toBeNull();
    answer(null);
    expect((await screen.findByRole("alert")).textContent).toContain("Unable to load capabilities");
    expect(screen.queryByRole("heading", { name: /Built-in Plugins/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: /Built-in Plugins/ })).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Turn “Web research” on or off for the team" })).toBeTruthy();
  });

  describe("Tools page", () => {
    const external = (name: string, source: string, extra: Partial<DiscoveryTool> = {}): DiscoveryTool => ({
      stable_ref: `teammcp.${source}.${name}`,
      display_name: name,
      description: `${name} description`,
      display_name_en: name,
      description_en: `${name} description`,
      display_name_de: "",
      description_de: "",
      display_name_fr: "",
      description_fr: "",
      source_kind: "team_mcp",
      source_display_name: source,
      risk: "read",
      selectable: true,
      locked_reason: null,
      ...extra,
    });

    beforeEach(() => {
      state.query = "type=tools";
    });

    it("groups every built-in Tool by its Plugin, with knowledge management under Knowledge base", async () => {
      vi.mocked(fetchBuiltinPlugins).mockResolvedValue({
        plugins: [plugin("knowledge-management", ["kb_add_file"]), plugin("web-research", ["web_search"])],
        team_plugins_my_disabled: [],
      });
      vi.mocked(fetchBuiltinTools).mockResolvedValue([
        { name: "kb_add_file", risk: "artifact_write", enabled: true },
        { name: "web_search", risk: "read", enabled: true },
      ]);
      authenticate("owner");
      renderIntl(<CapabilityCenter />, { locale: "en-US" });
      const knowledge = await screen.findByRole("region", { name: "Knowledge base" });
      expect(within(knowledge).getByRole("heading", { name: "Add to Knowledge" })).toBeTruthy();
      expect(screen.queryByRole("region", { name: "Skills" })).toBeNull();
    });

    it("lists external Tools under their source with risk tags and the specific reason a locked one can't be added", async () => {
      vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({
        builtin_plugins: [],
        capabilities: [],
        tools: [
          external("findPets", "pet"),
          external("addPet", "pet", { risk: "external_write", selectable: false, locked_reason: "approval_executor_unavailable" }),
          external("execute_workflow", "n8n", { risk: "destructive" }),
        ],
      });
      authenticate("member");
      renderIntl(<CapabilityCenter />, { locale: "en-US" });
      const pet = await screen.findByRole("region", { name: "pet" });
      expect(within(pet).getByRole("heading", { level: 3 }).textContent).toBe("pet 2");
      const addPet = within(pet).getByRole("heading", { name: /addPet/ }).closest("li") as HTMLElement;
      expect(within(addPet).getByText("Writes externally")).toBeTruthy();
      expect(within(addPet).getByText("This deployment can't confirm this kind of write yet, so it can't be added to Agents.")).toBeTruthy();
      const findPets = within(pet).getByRole("heading", { name: "findPets" }).closest("li") as HTMLElement;
      expect(findPets.textContent).toBe("findPetsfindPets description");
      const n8n = screen.getByRole("region", { name: "n8n" });
      expect(within(n8n).getByText("Destructive")).toBeTruthy();
      // Members are not told about sources they cannot manage.
      expect(listSources).not.toHaveBeenCalled();
    });

    it("tells administrators how many sources are not on, since the catalog leaves their Tools out", async () => {
      vi.mocked(fetchCapabilityDiscoveryCatalog).mockResolvedValue({ builtin_plugins: [], capabilities: [], tools: [external("findPets", "pet")] });
      vi.mocked(listSources).mockResolvedValue([
        { id: "ots_1", status: "enabled" },
        { id: "ots_2", status: "disabled" },
        { id: "ots_3", status: "disabled" },
      ] as TeamToolSourcePublic[]);
      authenticate("owner");
      renderIntl(<CapabilityCenter />, { locale: "en-US" });
      const note = await screen.findByRole("note");
      expect(note.textContent).toContain("2 more external Tool sources are not turned on, so their Tools are not listed here.");
      expect(within(note).getByRole("link", { name: "Manage tool sources" }).getAttribute("href")).toBe("/team/settings/tools/");
    });

    it("shows members a state only for Tools the team turned off, and the system capabilities as always on", async () => {
      authenticate("member");
      renderIntl(<CapabilityCenter />, { locale: "en-US" });
      const email = (await screen.findByRole("heading", { name: /Send Email/ })).closest("li") as HTMLElement;
      expect(within(email).getByText("Off for the team")).toBeTruthy();
      const search = screen.getByRole("heading", { name: "Web Search" }).closest("li") as HTMLElement;
      expect(within(search).queryByText("On")).toBeNull();
      expect(within(search).queryByText("Off for the team")).toBeNull();
      expect(screen.queryByRole("switch")).toBeNull();
      const system = screen.getByTestId("system-capabilities");
      expect(within(system).getByText("Find conversation results")).toBeTruthy();
      expect(within(system).getByText("Always on")).toBeTruthy();
    });

    it("counts matches out of the total while searching, and says when nothing matches", async () => {
      authenticate("owner");
      renderIntl(<CapabilityCenter />, { locale: "en-US" });
      await screen.findByRole("heading", { level: 2, name: /Built-in Tools/ });
      fireEvent.change(screen.getByRole("searchbox", { name: "Search Tools" }), { target: { value: "web" } });
      expect(screen.getByRole("heading", { level: 2, name: /Built-in Tools/ }).textContent).toBe("Built-in Tools 1 / 2");
      fireEvent.change(screen.getByRole("searchbox", { name: "Search Tools" }), { target: { value: "zzz-no-tool" } });
      expect(screen.getByText("No Tools match your search.")).toBeTruthy();
    });
  });
});
