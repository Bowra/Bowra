import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/en-US.json";
import {
  compactDiff,
  findSimilarLine,
  textDiff,
  SpaceInstructions,
  SpaceInstructionsDialog,
  withAppended,
} from "@/components/settings/space-instructions";
import { getSpaceInstructions, listSpaceInstructionVersions, saveSpaceInstructions } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { lineDiff } from "@/lib/line-diff";
import { notifyWorkspaceInstructionsSaved } from "@/lib/workspace-settings-notifications";

vi.mock("@/lib/api", () => ({
  getSpaceInstructions: vi.fn(),
  saveSpaceInstructions: vi.fn(),
  listSpaceInstructionVersions: vi.fn(),
}));
vi.mock("@/lib/use-space", () => ({ useSpace: () => ({ activeId: "ws_a", items: [] }) }));

const labels = messages.settings.space_instructions;
const prefillLabels = messages.feedback_prefill;
const fill = (text: string, values: Record<string, string | number>) =>
  Object.entries(values).reduce((out, [key, value]) => out.replace(new RegExp(`\\{${key}(, number)?\\}`, "g"), String(value)), text);

const current = {
  workspace_id: "ws_a",
  version: 2,
  content: "Answers are for sales leads.\n- New products: launched less than 24 months ago.",
  updated_by_name: "Ada",
  updated_at: "2026-10-15T08:00:00+00:00",
  max_chars: 6000,
  can_edit: true,
  archived: false,
  administrators: ["Ada", "Daniel Weber"],
};
const version = (n: number, content: string, extra: Record<string, unknown> = {}) => ({
  version: n,
  content,
  created_by: "usr_a",
  created_by_name: "Ada",
  created_at: `2026-10-1${n}T08:00:00+00:00`,
  restored_from: null,
  source_candidate_id: null,
  ...extra,
});
const history = [version(2, current.content), version(1, "Formal tone.")];

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getSpaceInstructions).mockResolvedValue(current);
  vi.mocked(listSpaceInstructionVersions).mockResolvedValue(history);
});
afterEach(cleanup);

async function editor() {
  return (await screen.findByRole("textbox", { name: labels.content_label })) as HTMLTextAreaElement;
}

it("saves an edit against the version the administrator started from and confirms the new version", async () => {
  vi.mocked(saveSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: "Answers are for executives." });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Answers are for executives." } });
  expect(screen.getByText(labels.unsaved)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() =>
    expect(saveSpaceInstructions).toHaveBeenCalledWith("ws_a", { content: "Answers are for executives.", expected_version: 2 }),
  );
  expect(await screen.findByText(fill(labels.saved_as, { version: 3 }))).toBeTruthy();
});

it("shows a version confirmed in chat, but never replaces a draft being edited", async () => {
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  expect((await editor()).value).toBe(current.content);
  const chatSaved = `${current.content}\n\nPrefer IQVIA data.`;
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: chatSaved });
  act(() => notifyWorkspaceInstructionsSaved({ teamId: "team_a", workspaceId: "ws_other" }));
  act(() => notifyWorkspaceInstructionsSaved({ teamId: "team_a", workspaceId: "ws_a" }));
  await waitFor(async () => expect((await editor()).value).toBe(chatSaved));
  expect(getSpaceInstructions).toHaveBeenCalledTimes(2);

  fireEvent.change(await editor(), { target: { value: "My draft" } });
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, version: 4, content: "Newer" });
  act(() => notifyWorkspaceInstructionsSaved({ teamId: "team_a", workspaceId: "ws_a" }));
  await Promise.resolve();
  expect((await editor()).value).toBe("My draft");
  expect(getSpaceInstructions).toHaveBeenCalledTimes(2);
});

it("restores an earlier version into the editor, names the version it becomes, and can undo", async () => {
  vi.mocked(saveSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: "Formal tone." });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "My draft" } });
  fireEvent.click(await screen.findByRole("button", { name: /^Version 1/ }));
  expect(screen.getByText(labels.history_first)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: labels.restore }));
  expect((await editor()).value).toBe("Formal tone.");
  expect(screen.getByText(fill(labels.restoring_notice, { version: 1, next: 3, current: 2 }))).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: labels.restoring_undo }));
  expect((await editor()).value).toBe("My draft");

  fireEvent.click(screen.getByRole("button", { name: labels.restore }));
  fireEvent.click(screen.getByRole("button", { name: fill(labels.save_as, { version: 3 }) }));
  await waitFor(() =>
    expect(saveSpaceInstructions).toHaveBeenCalledWith("ws_a", { content: "Formal tone.", expected_version: 2, restored_from: 1 }),
  );
});

it("on a conflict keeps the draft, opens the other administrator's version and names the consequence of saving", async () => {
  vi.mocked(saveSpaceInstructions)
    .mockRejectedValueOnce(new ApiError(409, "conflict", { code: "stale_version", body: { detail: { code: "stale_version", current_version: 3 } } }))
    .mockResolvedValueOnce({ ...current, version: 4, content: "Mine" });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Mine" } });
  const theirs = `${current.content}\n- West includes Tibet from July 2026.`;
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: theirs, updated_by_name: "Daniel Weber" });
  vi.mocked(listSpaceInstructionVersions).mockResolvedValue([version(3, theirs, { created_by_name: "Daniel Weber" }), ...history]);
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));

  expect(await screen.findByText(fill(labels.error_stale, { version: 3 }))).toBeTruthy();
  expect((await editor()).value).toBe("Mine");
  expect(await screen.findByText(labels.stale_badge)).toBeTruthy();
  expect(screen.getByText("- West includes Tibet from July 2026.")).toBeTruthy();
  expect(screen.getByText(labels.stale_base, { exact: false })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: labels.stale_save }));
  await waitFor(() => expect(saveSpaceInstructions).toHaveBeenLastCalledWith("ws_a", { content: "Mine", expected_version: 3 }));
});

it("after a conflict whose refetch fails, the next save targets the version the server reported", async () => {
  vi.mocked(saveSpaceInstructions)
    .mockRejectedValueOnce(new ApiError(409, "conflict", { code: "stale_version", body: { detail: { code: "stale_version", current_version: 3 } } }))
    .mockResolvedValueOnce({ ...current, version: 4, content: "Mine" });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Mine" } });
  vi.mocked(getSpaceInstructions).mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  fireEvent.click(await screen.findByRole("button", { name: labels.stale_save }));
  await waitFor(() => expect(saveSpaceInstructions).toHaveBeenLastCalledWith("ws_a", { content: "Mine", expected_version: 3 }));
});

it("clears the conflict when the other administrator saved exactly the same text", async () => {
  vi.mocked(saveSpaceInstructions)
    .mockRejectedValueOnce(new ApiError(409, "conflict", { code: "stale_version", body: { detail: { code: "stale_version", current_version: 3 } } }))
    .mockRejectedValueOnce(new ApiError(409, "unchanged", { code: "unchanged", body: { detail: { code: "unchanged" } } }));
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Same" } });
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: "Same" });
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  fireEvent.click(await screen.findByRole("button", { name: labels.stale_save }));
  expect(await screen.findByText(labels.error_unchanged)).toBeTruthy();
  expect(screen.queryByText(fill(labels.error_stale, { version: 3 }))).toBeNull();
  expect(screen.queryByRole("button", { name: labels.stale_save })).toBeNull();
});

it("blocks saving over the limit and says how much to cut", async () => {
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, max_chars: 10 });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "x".repeat(11) } });
  expect(screen.getByText(fill(labels.error_too_long, { length: 11, limit: 10 }))).toBeTruthy();
  expect(screen.getByText("1 character over the limit; shorten to save")).toBeTruthy();
  expect((screen.getByRole("button", { name: messages.common.save }) as HTMLButtonElement).disabled).toBe(true);
});

it("shows an unchanged save as a neutral note, not an error", async () => {
  vi.mocked(saveSpaceInstructions).mockRejectedValue(new ApiError(409, "unchanged", { code: "unchanged", body: { detail: { code: "unchanged" } } }));
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: `${current.content} ` } });
  fireEvent.change(await editor(), { target: { value: `${current.content}!` } });
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  const note = await screen.findByText(labels.error_unchanged);
  expect(note.closest("[role]")?.getAttribute("role")).toBe("status");
});

it("tells an administrator of an archived space why editing is unavailable", async () => {
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, can_edit: false, archived: true });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  expect(await screen.findByText(labels.error_archived)).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("shows members the instructions read-only with the administrators to ask, without history", async () => {
  vi.mocked(getSpaceInstructions).mockResolvedValue({ ...current, can_edit: false });
  renderIntl(<SpaceInstructions />, { locale: "en-US" });
  expect(await screen.findByText(fill(labels.read_only_names, { names: "Ada, Daniel Weber" }))).toBeTruthy();
  expect(screen.getByText(/Answers are for sales leads\./)).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(listSpaceInstructionVersions).not.toHaveBeenCalled();
});

it("turns the dialog footer into a confirmation when closing with unsaved changes", async () => {
  const onClose = vi.fn();
  renderIntl(<SpaceInstructionsDialog workspaceId="ws_a" name="Pharma Sales" onClose={onClose} />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Changed" } });
  fireEvent.click(screen.getAllByRole("button", { name: labels.close }).at(-1)!);
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByText(labels.close_unsaved)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: labels.keep_editing }));
  expect((await editor()).value).toBe("Changed");
  fireEvent.click(screen.getAllByRole("button", { name: labels.close }).at(-1)!);
  fireEvent.click(screen.getByRole("button", { name: labels.discard_close }));
  expect(onClose).toHaveBeenCalled();
});

it("reviews adopted wording as a line diff before it reaches the editor and records the suggestion on save", async () => {
  const text = "- New products: launched less than 24 months ago; say so when an answer uses another definition.";
  const combined = withAppended(current.content, text);
  vi.mocked(saveSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: combined });
  const onSaved = vi.fn();
  renderIntl(
    <SpaceInstructionsDialog
      workspaceId="ws_a"
      name="Pharma Sales"
      prefill={{ text, candidateId: "fbc_8d14", proposer: "Wang Lei", evidenceCount: 4 }}
      onClose={vi.fn()}
      onSaved={onSaved}
    />,
    { locale: "en-US" },
  );
  expect(await screen.findByText(prefillLabels.banner)).toBeTruthy();
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.getByText(fill(labels.similar_line, { line: 2 }))).toBeTruthy();
  const apply = screen.getByRole("button", { name: prefillLabels.apply }) as HTMLButtonElement;
  expect(apply.disabled).toBe(true);
  expect(screen.queryByRole("textbox", { name: labels.content_label })).toBeNull();

  fireEvent.click(screen.getByRole("checkbox", { name: prefillLabels.reviewed }));
  fireEvent.click(apply);
  expect((await editor()).value).toBe(combined);
  expect(saveSpaceInstructions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() =>
    expect(saveSpaceInstructions).toHaveBeenCalledWith("ws_a", { content: combined, expected_version: 2, source_candidate_id: "fbc_8d14" }),
  );
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ version: 3 }), { fromPrefill: true }));
});

it("discarding the prefill keeps the current version and a later save does not claim the suggestion", async () => {
  vi.mocked(saveSpaceInstructions).mockResolvedValue({ ...current, version: 3, content: "Other" });
  const onSaved = vi.fn();
  renderIntl(
    <SpaceInstructionsDialog
      workspaceId="ws_a"
      name="Pharma Sales"
      prefill={{ text: "Use fiscal quarters.", candidateId: "fbc_1", proposer: "Wang Lei", evidenceCount: 1 }}
      onClose={vi.fn()}
      onSaved={onSaved}
    />,
    { locale: "en-US" },
  );
  fireEvent.click(await screen.findByRole("button", { name: prefillLabels.discard }));
  expect((await editor()).value).toBe(current.content);
  fireEvent.change(await editor(), { target: { value: "Other" } });
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() => expect(saveSpaceInstructions).toHaveBeenCalledWith("ws_a", { content: "Other", expected_version: 2 }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.anything(), { fromPrefill: false }));
});

it("keeps the text and offers to copy it when the save is forbidden", async () => {
  vi.mocked(saveSpaceInstructions).mockRejectedValue(new ApiError(403, "forbidden", { body: { detail: {} } }));
  renderIntl(<SpaceInstructionsDialog workspaceId="ws_a" name="Finance Close" onClose={vi.fn()} />, { locale: "en-US" });
  fireEvent.change(await editor(), { target: { value: "Changed" } });
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  expect(await screen.findByText(labels.error_forbidden)).toBeTruthy();
  expect(screen.getByText(fill(labels.forbidden_footer_names, { names: "Ada, Daniel Weber" }))).toBeTruthy();
  expect(screen.getByRole("button", { name: labels.copy_text })).toBeTruthy();
  expect((await editor()).value).toBe("Changed");
});

it("diffs against empty instructions as pure additions", () => {
  expect(textDiff("", "a\nb")).toEqual([{ kind: "add", text: "a" }, { kind: "add", text: "b" }]);
  expect(textDiff("", "")).toEqual([]);
});

it("shows the blank separator before appended text as context, not an empty added line", () => {
  const rows = compactDiff(lineDiff("a", withAppended("a", "new rule")));
  expect(rows).toEqual([{ kind: "ctx", text: "a" }, { kind: "ctx", text: "" }, { kind: "add", text: "new rule" }]);
  expect(compactDiff(lineDiff("a", "a\n"))).toEqual([{ kind: "ctx", text: "a" }, { kind: "add", text: "" }]);
});

it("collapses long unchanged runs and finds a similar existing line", () => {
  const rows = compactDiff(lineDiff("a\nb\nc\nd\ne", "a\nb\nc\nd\ne\nf"));
  expect(rows).toEqual([{ kind: "gap", count: 4 }, { kind: "ctx", text: "e" }, { kind: "add", text: "f" }]);
  expect(findSimilarLine("Intro\n- 新品指上市未满 24 个月的产品。", ["- 新品指上市未满 24 个月的产品；同一个回答里如果用了别的界定，要先写明。"])).toBe(2);
  expect(findSimilarLine("Intro", ["Totally new line here"])).toBeNull();
});
