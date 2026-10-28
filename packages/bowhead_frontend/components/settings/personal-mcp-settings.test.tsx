import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
}));
vi.mock("@/lib/personal-mcp-api", () => ({ personalMcpApi: mocks.api }));
import {
  PersonalMcpSettings,
  PersonalMcpAgentBindings,
} from "./personal-mcp-settings";
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.history.replaceState({}, "", "/");
});
function catalog(connections: unknown[] = []) {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") return { agents: [] };
    if (path === "/calls/history") return { calls: [] };
    return { connections };
  });
}
describe("personal MCP connection form", () => {
  it("shows the Slack callback failure reason and clears callback parameters", async () => {
    window.history.replaceState(
      {},
      "",
      "/connections/?slack=failed&slack_reason=oauth_exchange_failed&keep=1",
    );
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/capabilities") {
        return { enabled: true, slack_connectable: true };
      }
      return { connections: [] };
    });

    render(<PersonalMcpSettings />);

    const alert = await screen.findByText("slackNotice.oauthExchangeFailed");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(window.location.search).toBe("?keep=1");
  });

  it("keeps existing connections visible and opens add connection in a dialog", async () => {
    catalog([
      {
        id: "pmc_one",
        name: "CRM",
        endpoint: "https://crm.example/mcp",
        auth_type: "none",
        status: "ready",
        version: 1,
        tools: [],
      },
    ]);
    render(<PersonalMcpSettings />);

    await screen.findByText("CRM");
    expect(screen.queryByLabelText("name")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "addConnection" }));
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.getByLabelText("name")).toBeTruthy();
  });

  it("uses distinct state and action styles for enabled and disabled connections", async () => {
    catalog([
      {
        id: "pmc_ready",
        name: "Ready",
        endpoint: "https://ready.example/mcp",
        auth_type: "none",
        status: "ready",
        version: 1,
        tools: [],
      },
      {
        id: "pmc_disabled",
        name: "Disabled",
        endpoint: "https://disabled.example/mcp",
        auth_type: "none",
        status: "disabled",
        version: 1,
        tools: [],
      },
    ]);
    render(<PersonalMcpSettings />);

    await screen.findByText("Ready");
    expect(
      screen
        .getByText("ready")
        .classList.contains("personal-mcp-connection-status"),
    ).toBe(true);
    expect(screen.getByText("ready").classList.contains("is-ready")).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "disable" })
        .classList.contains("personal-mcp-connection-disable"),
    ).toBe(true);
    expect(
      screen
        .getByText("disabled")
        .classList.contains("personal-mcp-connection-status"),
    ).toBe(true);
    expect(screen.getByText("disabled").classList.contains("is-disabled")).toBe(
      true,
    );
    expect(
      screen
        .getByRole("button", { name: "enable" })
        .classList.contains("personal-mcp-connection-enable"),
    ).toBe(true);
  });

  it("submits API key fields and freezes the form during discovery", async () => {
    catalog();
    render(<PersonalMcpSettings />);
    await screen.findByRole("button", { name: "addConnection" });
    fireEvent.click(screen.getByRole("button", { name: "addConnection" }));
    await screen.findByLabelText("name");
    fireEvent.change(screen.getByLabelText("name"), {
      target: { value: "CRM" },
    });
    fireEvent.change(screen.getByLabelText("endpoint"), {
      target: { value: "https://crm.example/mcp" },
    });
    fireEvent.change(screen.getByLabelText("auth"), {
      target: { value: "api_key" },
    });
    fireEvent.change(screen.getByLabelText("header"), {
      target: { value: "X-CRM-Key" },
    });
    fireEvent.change(screen.getByLabelText("credential"), {
      target: { value: "test-key" },
    });
    let finish!: (result: unknown) => void;
    mocks.api.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "connect" }));
    expect(mocks.api).toHaveBeenCalledWith("", "POST", {
      name: "CRM",
      endpoint: "https://crm.example/mcp",
      auth_type: "api_key",
      value: "test-key",
      header: "X-CRM-Key",
    });
    expect(
      (screen.getByLabelText("name") as HTMLInputElement).matches(":disabled"),
    ).toBe(true);
    expect(
      (screen.getByLabelText("auth") as HTMLSelectElement).matches(":disabled"),
    ).toBe(true);
    finish({ changes: { added: ["search"], removed: [], changed: [] } });
    await screen.findByRole("status");
    expect(screen.queryByLabelText("credential")).toBeNull();
    expect(screen.queryByLabelText("name")).toBeNull();
  });
  it("requires a credential when changing an existing connection authentication type", async () => {
    catalog([
      {
        id: "pmc_one",
        name: "CRM",
        endpoint: "https://crm.example/mcp",
        auth_type: "none",
        status: "ready",
        version: 1,
        tools: [],
      },
    ]);
    render(<PersonalMcpSettings />);
    await screen.findByRole("button", { name: "editConnectionAction" });
    fireEvent.click(
      screen.getByRole("button", { name: "editConnectionAction" }),
    );
    fireEvent.change(screen.getByLabelText("auth"), {
      target: { value: "bearer" },
    });
    expect(
      (screen.getByLabelText("credential") as HTMLInputElement).required,
    ).toBe(true);
    expect(
      (screen.getByLabelText("credential") as HTMLInputElement).checkValidity(),
    ).toBe(false);
  });
  it("refreshes an existing connection with its stored credentials", async () => {
    catalog([
      {
        id: "pmc_one",
        name: "CRM",
        endpoint: "https://crm.example/mcp",
        auth_type: "api_key",
        status: "ready",
        version: 3,
        tools: [],
      },
    ]);
    render(<PersonalMcpSettings />);
    await screen.findByRole("button", { name: "refreshTools" });
    fireEvent.click(screen.getByRole("button", { name: "refreshTools" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith("/pmc_one", "PUT", {
        name: "CRM",
        endpoint: "https://crm.example/mcp",
        auth_type: "api_key",
        version: 3,
      }),
    );
  });
  it("shows load errors without exposing connection controls before authorization is known", async () => {
    mocks.api.mockRejectedValue(new Error("network"));
    render(<PersonalMcpSettings />);
    await screen.findByRole("alert");
    expect(screen.queryByLabelText("name")).toBeNull();
  });
});

it("hides direct personal settings and does not load connections when MCP is unlicensed", async () => {
  mocks.api.mockResolvedValue({ enabled: false });
  render(<PersonalMcpSettings />);
  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/capabilities"));
  expect(screen.queryByRole("heading", { name: "title" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(mocks.api.mock.calls).toEqual([["/capabilities"]]);
});
it("hides private agent binding configuration when MCP is unlicensed", async () => {
  mocks.api.mockResolvedValue({ enabled: false });
  render(<PersonalMcpAgentBindings agentId="osa_one" />);
  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/capabilities"));
  expect(screen.queryByRole("heading", { name: "agentMcpTitle" })).toBeNull();
  expect(mocks.api.mock.calls).toEqual([["/capabilities"]]);
});

it("groups personal tool bindings under their MCP server", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") {
      return {
        agents: [
          {
            id: "osa_one",
            display_name: "Researcher",
            revision_id: "rev_1",
            tool_ids_json: "[]",
            binding_version: 1,
          },
        ],
      };
    }
    return {
      connections: [
        {
          id: "pmc_search",
          name: "Search MCP",
          endpoint: "https://search.example/mcp",
          status: "ready",
          tools: [
            {
              id: "tool_search",
              remote_name: "web_search",
              description: "Search the web",
              selected: 1,
            },
          ],
        },
        {
          id: "pmc_calendar",
          name: "Calendar MCP",
          endpoint: "https://calendar.example/mcp",
          status: "ready",
          tools: [
            {
              id: "tool_calendar",
              remote_name: "get_events",
              description: "List events",
              selected: 1,
            },
          ],
        },
      ],
    };
  });
  render(<PersonalMcpAgentBindings agentId="osa_one" />);
  await screen.findByRole("heading", { name: "Search MCP" });
  expect(screen.getByRole("heading", { name: "Calendar MCP" })).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: /web_search/ })).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: /get_events/ })).toBeTruthy();
});

it("replays each MCP tool binding for the current agent without repeating its name", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") {
      return {
        agents: [{
          id: "osa_one",
          display_name: "Researcher",
          revision_id: "rev_1",
          tool_ids_json: '["tool_search"]',
          binding_version: 1,
        }],
      };
    }
    return {
      connections: [{
        id: "pmc_search",
        name: "Search MCP",
        endpoint: "https://search.example/mcp",
        status: "ready",
        tools: [
          { id: "tool_search", remote_name: "web_search", description: "Search the web", selected: 1 },
          { id: "tool_news", remote_name: "news_search", description: "Search the news", selected: 1 },
        ],
      }],
    };
  });

  const { container } = render(<PersonalMcpAgentBindings agentId="osa_one" />);

  expect((await screen.findByRole("checkbox", { name: /web_search/ }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole("checkbox", { name: /news_search/ }) as HTMLInputElement).checked).toBe(false);
  expect(container.querySelector(".personal-mcp-agent-binding > legend")).toBeNull();
  expect(container.querySelector(".personal-mcp-agent-binding")?.classList.contains("is-embedded")).toBe(true);
});

it("shows connection configuration when the combined MCP capability is enabled", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") return { agents: [] };
    if (path === "/calls/history") return { calls: [] };
    return { connections: [] };
  });
  render(<PersonalMcpSettings />);
  await screen.findByRole("heading", { name: "settingsTitle", level: 2 });
  expect(screen.getByRole("button", { name: "addConnection" })).toBeTruthy();
  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(""));
});

it("presents recent calls as readable status cards", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/calls/history") {
      return {
        calls: [
          {
            id: "pmi_one",
            connection_name: "Search MCP",
            tool_name: "web_search",
            status: "ok",
            created_at: "2030-01-01T00:00:00Z",
            duration_ms: 120,
          },
          {
            id: "pmi_two",
            connection_name: "CRM MCP",
            tool_name: "update_contact",
            status: "unknown",
            created_at: "2030-01-01T00:01:00Z",
            duration_ms: null,
          },
        ],
      };
    }
    return { connections: [] };
  });
  render(<PersonalMcpSettings />);
  await screen.findByRole("tab", { name: "history" });
  fireEvent.click(screen.getByRole("tab", { name: "history" }));
  await screen.findByText("Search MCP");
  expect(screen.getByText("callStatus.ok")).toBeTruthy();
  expect(screen.getByText("callStatus.unknown")).toBeTruthy();
  expect(screen.getByText("historyUnknown")).toBeTruthy();
  expect(mocks.api).toHaveBeenCalledWith("/calls/history");
});

it("separates connections, agent bindings, and call history into tabs", async () => {
  catalog();
  render(<PersonalMcpSettings />);
  await screen.findByRole("tab", { name: "title" });
  expect(
    screen.getByRole("tab", { name: "title" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(screen.queryByRole("heading", { name: "agentMcpTitle" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "history" })).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "agentBindings" }));
  expect(
    await screen.findByRole("heading", { name: "agentMcpTitle" }),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("tab", { name: "history" }));
  expect(await screen.findByRole("heading", { name: "history" })).toBeTruthy();
});

it("does not claim there are no private agents while bindings are still loading", async () => {
  let finishBindings!: (value: unknown) => void;
  mocks.api.mockImplementation((path: string) => {
    if (path === "/capabilities") return Promise.resolve({ enabled: true });
    if (path === "/agents/bindings") {
      return new Promise((resolve) => {
        finishBindings = resolve;
      });
    }
    return Promise.resolve({ connections: [] });
  });
  render(<PersonalMcpAgentBindings />);
  await screen.findByRole("heading", { name: "agentMcpTitle" });
  await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/agents/bindings"));
  expect(screen.queryByText("noPrivateAgents")).toBeNull();

  finishBindings({ agents: [] });
  expect(await screen.findByText("noPrivateAgents")).toBeTruthy();
});

it("names the private agents bound to a connection before it is deleted", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") {
      return {
        agents: [
          { id: "osa_one", display_name: "Researcher", revision_id: "rev_1", tool_ids_json: '["tool_crm"]', binding_version: 1 },
          { id: "osa_two", display_name: "Writer", revision_id: "rev_2", tool_ids_json: '["tool_other"]', binding_version: 1 },
          { id: "osa_three", display_name: "Closer", revision_id: "rev_3", tool_ids_json: '["tool_crm_write"]', binding_version: 1 },
        ],
      };
    }
    return {
      connections: [
        {
          id: "pmc_crm",
          name: "CRM",
          endpoint: "https://crm.example/mcp",
          auth_type: "none",
          status: "ready",
          version: 1,
          tools: [
            { id: "tool_crm", remote_name: "lookup", description: "", selected: 1, schema_digest: "d1" },
            { id: "tool_crm_write", remote_name: "update", description: "", selected: 1, schema_digest: "d2" },
          ],
        },
      ],
    };
  });
  render(<PersonalMcpSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "delete" }));
  expect(screen.getByText("deleteNotice")).toBeTruthy();
  expect(
    await screen.findByText(
      'deleteAffectsAgents {"count":2,"names":"ResearcheragentNameSeparatorCloser"}',
    ),
  ).toBeTruthy();
});

it("titles an agent without a display name by its slug", async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path === "/capabilities") return { enabled: true };
    if (path === "/agents/bindings") {
      return {
        agents: [
          { id: "osa_blank", display_name: "  ", slug: "sales-helper", revision_id: "rev_1", tool_ids_json: "[]", binding_version: 0 },
          { id: "osa_named", display_name: "Researcher", slug: "researcher", revision_id: "rev_2", tool_ids_json: "[]", binding_version: 0 },
        ],
      };
    }
    return { connections: [] };
  });
  const { container } = render(<PersonalMcpAgentBindings />);
  await screen.findByText("Researcher");
  const titles = [...container.querySelectorAll(".personal-mcp-agent-binding > legend")].map(
    (legend) => legend.textContent,
  );
  expect(titles).toEqual(["sales-helper", "Researcher"]);
});

it("labels empty tool change lists as no changes, not as the no-auth option", async () => {
  catalog([
    {
      id: "pmc_one",
      name: "CRM",
      endpoint: "https://crm.example/mcp",
      auth_type: "none",
      status: "ready",
      version: 3,
      tools: [],
    },
  ]);
  render(<PersonalMcpSettings />);
  await screen.findByRole("button", { name: "refreshTools" });
  mocks.api.mockImplementationOnce(async () => ({
    changes: { added: ["search"], removed: [], changed: [] },
  }));
  fireEvent.click(screen.getByRole("button", { name: "refreshTools" }));
  await screen.findByText("added: search");
  expect(screen.getByText("removed: noToolChanges")).toBeTruthy();
  expect(screen.getByText("changed: noToolChanges")).toBeTruthy();
  expect(screen.queryByText(/: none$/)).toBeNull();
});
