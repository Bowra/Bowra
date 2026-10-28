import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/en-US.json";
import { SpaceAboutButton } from "@/components/space-about";
import { getSpaceInstructions } from "@/lib/api";

vi.mock("@/lib/api", () => ({ getSpaceInstructions: vi.fn() }));
vi.mock("@/lib/use-space", () => ({ useSpace: () => ({ activeId: "ws_a", activeName: "Pharma Sales", items: [] }) }));
const push = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }) }));

const labels = messages.space_about;
const value = {
  workspace_id: "ws_a",
  version: 4,
  content: "Answers are for regional sales managers.",
  updated_by_name: "Lin Yue",
  updated_at: "2026-10-15T06:32:00+00:00",
  max_chars: 6000,
  can_edit: false,
  archived: false,
  administrators: ["Daniel Weber", "Lin Yue"],
};

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it("shows members the instructions and whom to ask, without an edit action", async () => {
  vi.mocked(getSpaceInstructions).mockResolvedValue(value);
  renderIntl(<SpaceAboutButton />, { locale: "en-US" });
  fireEvent.click(screen.getByRole("button", { name: labels.button }));
  expect(await screen.findByText(value.content)).toBeTruthy();
  expect(screen.getByRole("dialog", { name: "Instructions for “Pharma Sales”" })).toBeTruthy();
  expect(screen.getByText(labels.hint.replace("{names}", "Daniel Weber, Lin Yue"))).toBeTruthy();
  expect(screen.queryByRole("button", { name: labels.edit })).toBeNull();
  expect(getSpaceInstructions).toHaveBeenCalledWith("ws_a");
});

it("says when a Space has no instructions and lets an administrator jump to editing", async () => {
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...value, version: 0, content: "", can_edit: true });
  renderIntl(<SpaceAboutButton />, { locale: "en-US" });
  fireEvent.click(screen.getByRole("button", { name: labels.button }));
  expect(await screen.findByText(labels.empty)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: labels.edit }));
  expect(push).toHaveBeenCalledWith("/space/settings/instructions/");
});
