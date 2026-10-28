import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const { getTeamOperations, saveTeamOperationsTimezone } = vi.hoisted(() => ({ getTeamOperations: vi.fn(), saveTeamOperationsTimezone: vi.fn() }));
vi.mock("@/lib/team-operations-api", () => ({ getTeamOperations, saveTeamOperationsTimezone }));

import { TeamOperationsContent } from "./team-operations-content";

beforeEach(() => {
  getTeamOperations.mockReset();
  saveTeamOperationsTimezone.mockReset();
  getTeamOperations.mockImplementation(async (days: number) => ({
    timezone: "Asia/Shanghai",
    days: Array.from({ length: days }, (_, offset) => ({
      day: `2026-10-${String(offset + 1).padStart(2, "0")}`,
      login_users: offset === 0 ? 2 : 0,
      sessions: 0,
      input_tokens: 0,
      output_tokens: 0,
      background_tokens: 0,
      skill_loads: 0,
      tool_loads: 0,
      tool_calls: 0,
    })),
  }));
});

it("shows daily zeros and switches between complete-day ranges", async () => {
  renderIntl(<TeamOperationsContent />);
  expect(await screen.findByText("2026-10-01")).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(8);
  fireEvent.click(screen.getByRole("button", { name: "昨天" }));
  await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(2));
  expect(getTeamOperations).toHaveBeenCalledWith(1);
  expect(screen.getAllByText("0", { selector: "td" }).length).toBeGreaterThan(0);
});

it("saves a team reporting time zone before refreshing the report", async () => {
  saveTeamOperationsTimezone.mockResolvedValue({ timezone: "UTC" });
  renderIntl(<TeamOperationsContent />);
  await screen.findByText("2026-10-01");
  fireEvent.change(screen.getByRole("combobox", { name: "报表时区" }), { target: { value: "UTC" } });
  fireEvent.click(screen.getByRole("button", { name: "保存时区" }));
  await waitFor(() => expect(saveTeamOperationsTimezone).toHaveBeenCalledWith("UTC"));
  await waitFor(() => expect(getTeamOperations).toHaveBeenCalledTimes(2));
});
