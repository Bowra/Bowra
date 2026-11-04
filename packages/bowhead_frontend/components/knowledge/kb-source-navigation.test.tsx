import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import type { KbFileDTO } from "@/lib/kb-api";
import type { SourceNavigation } from "@/lib/kb-source-navigation-api";
import { KbSourceNavigation } from "./kb-source-navigation";

const api = vi.hoisted(() => ({ fetchSourceNavigation: vi.fn(), saveSourceNavigation: vi.fn(), generateSourceDescription: vi.fn(), saveSourceCategory: vi.fn(), findSourceCategories: vi.fn() }));
vi.mock("@/lib/kb-source-navigation-api", () => api);
const file = { file_id: "file-a", version: 1, scope: "space" } as KbFileDTO;
const value: SourceNavigation = { file_id: "file-a", generation_id: "generation-a", source_version: 2, navigation_revision: 0, description: "", description_kind: "name", coverage: "sampled", categories: [], category_options: [], can_edit: true, generation_enabled: true, generation_model: "text-a", description_job: null };
const mount = () => render(<NextIntlClientProvider locale="en-US" messages={messages}><KbSourceNavigation file={file} /></NextIntlClientProvider>);

beforeEach(() => { vi.clearAllMocks(); api.fetchSourceNavigation.mockResolvedValue(value); api.saveSourceNavigation.mockResolvedValue({}); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("source discovery editing", () => {
  it("submits generation over HTTP without native randomUUID and clears the busy state", async () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(7) });
    try {
      api.generateSourceDescription.mockResolvedValue({ job_id: "job-http" });
      mount();
      const button = await screen.findByRole("button", { name: "Generate description" });
      fireEvent.click(button);
      await waitFor(() => expect(api.generateSourceDescription).toHaveBeenCalledTimes(1));
      expect(api.generateSourceDescription.mock.calls[0][1]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      await waitFor(() => expect(button).not.toBeDisabled());
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("preserves manual drafts and their revision when a background job updates the displayed result", async () => {
    vi.useFakeTimers();
    api.fetchSourceNavigation.mockResolvedValueOnce({ ...value, description_job: { id: "job-a", state: "dispatching", error_code: null } });
    mount();
    await act(async () => {});
    fireEvent.change(screen.getByLabelText("Manual description"), { target: { value: "My unsaved draft" } });
    api.fetchSourceNavigation.mockResolvedValue({ ...value, navigation_revision: 3, description: "Another maintainer's edit", description_kind: "manual" });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByLabelText("Manual description")).toHaveValue("My unsaved draft");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await act(async () => {});
    expect(api.saveSourceNavigation.mock.calls[0][0].navigation_revision).toBe(0);
    expect(api.saveSourceNavigation.mock.calls[0][1]).toEqual({ manual_description: "My unsaved draft" });
  });

  it("does not turn Matrix text into a file-wide manual description when saving categories", async () => {
    api.fetchSourceNavigation.mockResolvedValue({ ...value, description: "Private Matrix text", description_kind: "matrix", category_options: [{ id: "category-a", name: "Travel", aliases: [], scope: "space", parent_id: null, revision: 1 }] });
    mount();
    await screen.findByText("Private Matrix text");
    expect(screen.getByLabelText("Manual description")).toHaveValue("");
    fireEvent.click(screen.getByRole("checkbox", { name: "Travel" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveSourceNavigation).toHaveBeenCalled());
    expect(api.saveSourceNavigation.mock.calls[0][1]).toEqual({ category_ids: ["category-a"] });
  });

  it("keeps read-only source information readable without offering edits or paid generation", async () => {
    api.fetchSourceNavigation.mockResolvedValue({ ...value, can_edit: false, description_kind: "manual", description: "Travel procedures", generation_model: null });
    mount();
    await screen.findByText("Travel procedures");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate description" })).not.toBeInTheDocument();
    expect(api.generateSourceDescription).not.toHaveBeenCalled();
  });

  it("reuses an uncertain action id on an explicit retry instead of submitting a new paid action", async () => {
    api.generateSourceDescription.mockRejectedValue(new Error("network failure"));
    mount();
    const button = await screen.findByRole("button", { name: "Generate description" });
    fireEvent.click(button);
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    await waitFor(() => expect(api.generateSourceDescription).toHaveBeenCalledTimes(2));
    expect(api.generateSourceDescription.mock.calls[0][1]).toBe(api.generateSourceDescription.mock.calls[1][1]);
  });

  it("preserves a read-only drawer even when the caller can maintain the source", async () => {
    render(<NextIntlClientProvider locale="en-US" messages={messages}><KbSourceNavigation file={file} readOnly /></NextIntlClientProvider>);
    await screen.findByText("Source discovery");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate description" })).not.toBeInTheDocument();
  });

  it("starts a fresh explicit retry only after reconciling its own terminal action receipt", async () => {
    api.generateSourceDescription.mockRejectedValue(new Error("network failure"));
    mount();
    const button = await screen.findByRole("button", { name: "Generate description" });
    fireEvent.click(button);
    await waitFor(() => expect(button).not.toBeDisabled());
    const original = api.generateSourceDescription.mock.calls[0][1];
    api.fetchSourceNavigation.mockResolvedValue({ ...value, description_job: { id: "old-job", state: "failed", error_code: "output_invalid", action_id: "different-action" } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.fetchSourceNavigation).toHaveBeenCalledTimes(2));
    await act(async () => {});
    fireEvent.click(button);
    await waitFor(() => expect(api.generateSourceDescription).toHaveBeenCalledTimes(2));
    expect(api.generateSourceDescription.mock.calls[1][1]).toBe(original);
    await waitFor(() => expect(button).not.toBeDisabled());
    api.fetchSourceNavigation.mockResolvedValue({ ...value, description_job: { id: "own-job", state: "failed", error_code: "output_invalid", action_id: original } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.fetchSourceNavigation).toHaveBeenCalledTimes(3));
    await act(async () => {});
    fireEvent.click(button);
    await waitFor(() => expect(api.generateSourceDescription).toHaveBeenCalledTimes(3));
    expect(api.generateSourceDescription.mock.calls[2][1]).not.toBe(original);
  });

});
