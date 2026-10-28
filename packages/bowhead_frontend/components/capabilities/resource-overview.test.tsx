import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/use-skills", () => ({
  useSkills: () => ({
    data: {
      builtin: [{ name: "roe", title: "ROE attribution", description: "Split ROE", enabled: true, source_type: "builtin" }],
      user_upload: [{ id: "s1", name: "weekly", title: "Weekly report", description: "My method", enabled: false, source_type: "user_upload" }],
      user_generated: [],
    },
  }),
}));
vi.mock("@/lib/personal-mcp-api", () => ({
  personalMcpApi: vi.fn(async () => ({ connections: [{ id: "c1", name: "Slack" }] })),
}));
vi.mock("@/lib/api", () => ({
  fetchScheduledJobs: vi.fn(async () => ({
    items: [
      { id: "j1", name: "Monday digest", status: "active" },
      { id: "j2", name: "Monthly close", status: "paused" },
    ],
  })),
}));

import { PersonalOverview, SkillsOverview } from "./resource-overview";

describe("Capability Center overview sections", () => {
  afterEach(cleanup);

  it("shows Skill counts and the viewer's own Skills first", () => {
    renderIntl(<SkillsOverview onlyMine={false} />, { locale: "en-US" });
    expect(screen.getByText("1 mine · 1 built-in")).toBeTruthy();
    const titles = screen.getAllByRole("heading", { level: 2 }).map((item) => item.textContent);
    expect(titles).toEqual(["Weekly report", "ROE attribution"]);
    expect(screen.getByText("Off")).toBeTruthy();
  });

  it("shows only the viewer's Skills with Only mine", () => {
    renderIntl(<SkillsOverview onlyMine />, { locale: "en-US" });
    expect(screen.queryByText("ROE attribution")).toBeNull();
  });

  it("shows personal connections and scheduled task state as one card per entry", async () => {
    renderIntl(
      <PersonalOverview
        entries={[
          { id: "connections", href: "/connections/", fallback: <a href="/connections/">My Connections</a> },
          { id: "scheduled", href: "/team/settings/scheduled/", fallback: <a href="/team/settings/scheduled/">Scheduled Tasks</a> },
        ]}
      />,
      { locale: "en-US" },
    );
    expect(await screen.findByText("1 connected")).toBeTruthy();
    expect(screen.getByText("Slack")).toBeTruthy();
    expect(await screen.findByText("1 running · 1 paused")).toBeTruthy();
    expect(screen.getByText("Monday digest · Monthly close (Paused)")).toBeTruthy();
    // The live card replaces the plain entry card and opens the same page.
    expect(screen.queryByText("My Connections")).toBeNull();
    expect(screen.getByRole("link", { name: /My connections/ }).getAttribute("href")).toContain("/connections/");
  });

  it("keeps the plain entry card when the summary cannot be read and skips unavailable entries", async () => {
    const { fetchScheduledJobs } = await import("@/lib/api");
    const { personalMcpApi } = await import("@/lib/personal-mcp-api");
    vi.mocked(fetchScheduledJobs).mockRejectedValueOnce(new Error("offline"));
    vi.mocked(personalMcpApi).mockClear();
    renderIntl(
      <PersonalOverview entries={[{ id: "scheduled", href: "/team/settings/scheduled/", fallback: <a href="/team/settings/scheduled/">Scheduled Tasks</a> }]} />,
      { locale: "en-US" },
    );
    expect(await screen.findByText("Scheduled Tasks")).toBeTruthy();
    expect(personalMcpApi).not.toHaveBeenCalled();
    expect(screen.queryByText("1 connected")).toBeNull();
  });
});
