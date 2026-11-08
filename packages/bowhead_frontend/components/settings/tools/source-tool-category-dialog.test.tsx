import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn() }));
vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
}));
vi.mock("@/lib/team-tool-sources-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/team-tool-sources-api")>();
  return { ...actual, getSourceToolCategories: mocks.get, saveSourceToolCategories: mocks.save };
});
import { TeamToolApiError, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";
import { SourceToolCategoryDialog } from "./source-tool-category-dialog";

const source = { id: "ots_crm", name: "CRM" } as TeamToolSourcePublic;
const categories = {
  enabled: true,
  updated_at: "2026-11-08T00:00:00Z",
  items: [
    {
      name: "lookup",
      description: "Find a contact",
      imported: true,
      annotation_category: "unknown",
      confirmed_category: null,
      effective_category: "unknown",
      needs_reconfirmation: true,
      confirmed_at: null,
    },
    {
      name: "remove",
      description: "Delete a contact",
      imported: true,
      annotation_category: "destructive",
      confirmed_category: null,
      effective_category: "destructive",
      needs_reconfirmation: false,
      confirmed_at: null,
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("Confirm Tool Categories", () => {
  it("offers no category below the service's marking and flags changed tools", async () => {
    mocks.get.mockResolvedValue(categories);
    render(<SourceToolCategoryDialog source={source} onClose={() => undefined} onSaved={() => undefined} />);
    const remove = (await screen.findByLabelText('category_select_label {"tool":"remove"}')) as HTMLSelectElement;
    const disabled = Array.from(remove.options).filter((option) => option.disabled).map((option) => option.value);
    expect(disabled).toEqual(["read", "external_write"]);
    expect(screen.getByText(/category_reconfirm/)).toBeTruthy();
  });

  it("sends only the changed tools and reports success", async () => {
    mocks.get.mockResolvedValue(categories);
    mocks.save.mockResolvedValue(categories);
    const onSaved = vi.fn();
    render(<SourceToolCategoryDialog source={source} onClose={() => undefined} onSaved={onSaved} />);
    const lookup = await screen.findByLabelText('category_select_label {"tool":"lookup"}');
    expect((screen.getByRole("button", { name: "save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(lookup, { target: { value: "read" } });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledWith("ots_crm", { lookup: "read" }));
    expect(onSaved).toHaveBeenCalled();
  });

  it("explains a refused save without closing", async () => {
    mocks.get.mockResolvedValue(categories);
    mocks.save.mockRejectedValue(new TeamToolApiError(409, "conflict", "team_tool_category_unavailable"));
    const onSaved = vi.fn();
    render(<SourceToolCategoryDialog source={source} onClose={() => undefined} onSaved={onSaved} />);
    fireEvent.change(await screen.findByLabelText('category_select_label {"tool":"lookup"}'), { target: { value: "external_write" } });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    expect(await screen.findByText("category_error_unavailable")).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
