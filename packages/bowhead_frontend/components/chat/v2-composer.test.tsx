/**
 * Phase 1.7-D — V2Composer prop preservation matrix.
 *
 * The restyle for v8 (border-radius 14, ＋ attach glyph, mono hint inside frame,
 * replay control) MUST keep every existing prop surface working. This
 * spec asserts each of the 7 prop surfaces still functions + adds the new
 * 1.7-D affordances (hint, replay, borderRadius).
 *
 * Mocks:
 *   · next-intl — return the key (and a known string for v8_hint)
 *     so visual assertions are deterministic.
 *   · SpacePicker + ModelPicker — heavy hooks (workspace/model store); stub.
 *   · uploadFile / lib/api — stub upload pipeline; only validateUpload + the
 *     UPLOAD_* constants are referenced statically.
 *   · use-autocomplete — return inert state so Tab tests can flip on demand.
 *   · dispatchRememberLine — observe call to assert /remember short-circuit.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

let authSessionMock: {
  me: { id?: string; team_id?: string; capabilities?: Record<string, unknown> } | null;
} | null = null;

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => authSessionMock,
}));

const bundleUploadMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/kb-api", () => ({ uploadKbBundle: bundleUploadMock }));

const googlePickerMocks = vi.hoisted(() => ({ pickDriveFiles: vi.fn() }));
vi.mock("@/lib/google-picker", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/google-picker")>()),
  pickDriveFiles: googlePickerMocks.pickDriveFiles,
}));

const recoveryMocks = vi.hoisted(() => ({
  read: vi.fn(),
  write: vi.fn(),
  clear: vi.fn(),
  discard: vi.fn(),
  readReference: vi.fn(),
  removeReferences: vi.fn(),
}));

vi.mock("@/lib/composer-ordinary-draft", async () => {
  const actual = await vi.importActual<typeof import("@/lib/composer-ordinary-draft")>(
    "@/lib/composer-ordinary-draft",
  );
  return {
    ...actual,
    readComposerOrdinaryDraft: recoveryMocks.read,
    readComposerInputDraft: recoveryMocks.read,
    writeComposerOrdinaryDraft: recoveryMocks.write,
    writeComposerInputDraft: recoveryMocks.write,
    clearComposerOrdinaryDraft: recoveryMocks.clear,
    clearComposerInputDraft: recoveryMocks.clear,
    discardComposerOrdinaryDraft: recoveryMocks.discard,
    readComposerReferenceDraft: recoveryMocks.readReference,
    removeComposerDraftReferences: recoveryMocks.removeReferences,
  };
});

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    // Return human-readable strings for keys we assert on; raw key otherwise.
    if (key === "v8_hint") return "Bowhead 会先列步骤、再回答";
    if (key === "running_hint") return "正在回答 · 可停止";
    if (key === "disabled_hint") return "输入已禁用";
    if (key === "send_btn") return "发送";
    if (key === "stop_label") return "Stop";
    if (key === "stop_title") return "Stop current turn";
    if (key === "attach") return "附件";
    if (key === "placeholder") return "Ask…";
    if (key === "send_title") return "发送 (⏎)";
    if (key === "upload_title") return "Upload";
    if (key === "waiting_uploads") return "Waiting uploads";
    if (key === "tab_complete_hint") return "Tab to complete";
    if (key === "cycle_hint") return "↑↓ cycle";
    if (key === "chart_selection" || key === "chart_selection_detail") return `Selected: ${values?.scope}`;
    if (key === "upload_failed_for") return `${values?.filename}: ${values?.err}`;
    return key;
  },
}));

vi.mock("@/components/space-picker", () => ({
  SpacePicker: () => <span data-testid="mock-ws-picker">WS</span>,
}));

vi.mock("./model-picker", () => ({
  ModelPicker: ({ disabled }: { disabled?: boolean }) => (
    <button type="button" data-testid="mock-model-picker" disabled={disabled}>MM</button>
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    uploadFile: vi.fn(async () => ({
      file_id: "fid_1",
      filename: "f.txt",
      size: 4,
      mime: "text/plain",
    })),
  };
});

const dispatchRememberMock = vi.fn(
  async (_line: string) => ({ ok: true as const, key: "fact-1" }),
);
vi.mock("@/lib/remember-slash", () => ({
  dispatchRememberLine: (text: string) => dispatchRememberMock(text),
}));

let suggestionMock = "";
let candidatesMock: string[] = [];
vi.mock("@/lib/use-autocomplete", () => ({
  useAutocomplete: () => ({
    suggestion: suggestionMock,
    candidates: candidatesMock,
    index: 0,
    cycle: vi.fn(),
    dismiss: vi.fn(),
  }),
  isAutocompleteEnabled: () => false,
  readCachedHistory: () => [],
  historyFromSessions: () => [],
}));

// Import AFTER mocks register so they apply.
import { V2Composer, type ComposerDraftControl, type ComposerDraftSubmissionContext } from "./v2-composer";
import { uploadFile } from "@/lib/api";
import type { AttachmentRef } from "@/lib/types";
import { ApiError } from "@/lib/api-client";
import { PresignExpiredError, UploadHttpError } from "@/lib/upload-xhr";
import { ComposerOrdinaryDraftInvalidError } from "@/lib/composer-ordinary-draft";
import { ComposerRecoveryUnavailableError } from "@/lib/composer-local-persistence-db";
import { composerDraftScopeSignature } from "@/lib/composer-persistence-scope";
import { setPlanningModePref } from "@/lib/planning-mode-pref";
import {
  clearAllComposerTransientDrafts,
  retainComposerTransientDraft,
  readComposerTransientDraft,
} from "@/lib/composer-transient-draft";

let DRAFT_SCOPE = {
  userId: "u_1",
  teamId: "team_1",
  workspaceId: "ws_1",
  requestedSessionId: null,
  authSessionGeneration: "asg_0123456789abcdef",
};
let OTHER_DRAFT_SCOPE = {
  ...DRAFT_SCOPE,
  workspaceId: "ws_2",
};
beforeEach(() => {
  // Unmount can retain a failed handoff. Each test starts a distinct login,
  // rather than inheriting another case's intentionally unrecovered scope.
  DRAFT_SCOPE = { ...DRAFT_SCOPE, authSessionGeneration: `asg_${crypto.randomUUID().replaceAll("-", "")}` };
  OTHER_DRAFT_SCOPE = { ...DRAFT_SCOPE, workspaceId: "ws_2" };
  clearAllComposerTransientDrafts();
  window.sessionStorage.clear();
  recoveryMocks.read.mockResolvedValue(null);
  recoveryMocks.write.mockResolvedValue({ status: "written", revision: "rev_default" });
  recoveryMocks.clear.mockResolvedValue(true);
  recoveryMocks.discard.mockResolvedValue(undefined);
  recoveryMocks.readReference.mockResolvedValue(null);
  recoveryMocks.removeReferences.mockResolvedValue(false);
});

afterEach(async () => {
  await act(async () => { cleanup(); });
  vi.clearAllMocks();
  suggestionMock = "";
  candidatesMock = [];
  authSessionMock = null;
});

describe("Same-scope composer handoff", () => {
  it.each(["scope-object", "reference-capability", "failure-policy"])("keeps the editing baseline across a %s refresh", async (refresh) => {
    let stored = { message: "Original local draft", attachments: [], revision: "original", updatedAt: new Date().toISOString() };
    recoveryMocks.read.mockImplementation(async () => structuredClone(stored));
    recoveryMocks.write.mockImplementation(async (_scope, draft, expectedRevision) => {
      if (stored.revision !== expectedRevision) return { status: "conflict", revision: stored.revision };
      stored = { ...stored, ...draft, revision: "unexpected-replacement" };
      return { status: "written", revision: stored.revision };
    });
    const onSend = vi.fn(async () => false);
    const view = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Original local draft"));
    stored = { ...stored, message: "Competing draft must survive", revision: "competing" };
    view.rerender(<V2Composer onSend={onSend}
      draftPersistenceScope={refresh === "scope-object" ? { ...DRAFT_SCOPE } : DRAFT_SCOPE}
      referenceInputEnabled={refresh === "reference-capability"}
      draftPersistenceFailureMode={refresh === "failure-policy" ? "warn" : "block"} />);
    await waitFor(() => expect((screen.getByLabelText("发送") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(screen.queryByText("draft_conflict")).not.toBeNull());
    expect(onSend).not.toHaveBeenCalled();
    expect(stored.message).toBe("Competing draft must survive");
    expect(stored.revision).toBe("competing");
  });

  it.each(["Synthetic A", "Synthetic B", ""])("retains the final edit %j across queued saves and remount", async (finalText) => {
    let releaseFirst!: () => void;
    let releaseFinal!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const finalGate = new Promise<void>((resolve) => { releaseFinal = resolve; });
    let stored: { message: string; attachments: AttachmentRef[]; revision: string; updatedAt: string } | null = null;
    let writes = 0;
    recoveryMocks.read.mockImplementation(async () => structuredClone(stored));
    recoveryMocks.write.mockImplementation(async (_scope, draft, expectedRevision) => {
      const sequence = ++writes;
      if (sequence === 1) await firstGate;
      if (sequence === 2) await finalGate;
      if ((stored?.revision ?? null) !== expectedRevision) return { status: "conflict" };
      stored = draft.message || draft.attachments.length
        ? { ...draft, revision: `revision-${sequence}`, updatedAt: new Date().toISOString() }
        : null;
      return { status: "written", revision: stored?.revision ?? null };
    });
    const onSend = vi.fn(async (_message: string) => false);
    const scope = { ...DRAFT_SCOPE, requestedSessionId: `s_handoff_${finalText || "clear"}` };
    const first = render(<V2Composer onSend={onSend} draftPersistenceScope={scope} />);
    try {
      const input = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(input.disabled).toBe(false));
      fireEvent.change(input, { target: { value: "Synthetic A" } });
      await waitFor(() => expect(writes).toBe(1));
      fireEvent.change(input, { target: { value: finalText } });
      first.unmount();
      for (let mount = 0; mount < 3; mount += 1) {
        const next = render(<V2Composer onSend={onSend} draftPersistenceScope={scope} />);
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(true);
        if (mount < 2) next.unmount();
      }
      await act(async () => { releaseFirst(); });
      await waitFor(() => expect(writes).toBe(2));
      await act(async () => { releaseFinal(); });
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(finalText);
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(false);
        expect(screen.queryByText("draft_conflict")).toBeNull();
      });
      if (finalText) {
        fireEvent.click(screen.getByLabelText("发送"));
        await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
        expect(onSend.mock.calls[0][0]).toBe(finalText);
      } else {
        expect(stored).toBeNull();
        expect(onSend).not.toHaveBeenCalled();
      }
    } finally {
      await act(async () => { releaseFirst(); releaseFinal(); });
    }
  });

  it.each(["space", "auth"])("does not block or rebind another %s while the predecessor writes", async (change) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const originalScope = DRAFT_SCOPE;
    recoveryMocks.write.mockImplementation(async (scope) => {
      if (scope === originalScope) await gate;
      return { status: "written", revision: "saved" };
    });
    const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={originalScope} />);
    await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "old identity draft" } });
    await waitFor(() => expect(recoveryMocks.write).toHaveBeenCalledOnce());
    const nextScope = change === "space" ? OTHER_DRAFT_SCOPE : { ...originalScope, authSessionGeneration: "other-login" };
    view.rerender(<V2Composer onSend={() => {}} draftPersistenceScope={nextScope} />);
    try {
      await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false));
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "new identity draft" } });
      await act(async () => { release(); });
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("new identity draft");
      const oldWrites = recoveryMocks.write.mock.calls.filter(([, draft]) => draft.message === "old identity draft");
      expect(oldWrites.length).toBeGreaterThanOrEqual(2);
      expect(oldWrites.every(([scope]) => scope === originalScope)).toBe(true);
    } finally {
      await act(async () => { release(); });
    }
  });

  it("waits for an accepted Send and its delayed cleanup before restoring an empty draft", async () => {
    let accept!: (value: boolean) => void;
    let clear!: () => void;
    const accepted = new Promise<boolean>((resolve) => { accept = resolve; });
    const cleared = new Promise<void>((resolve) => { clear = resolve; });
    const onSend = vi.fn(() => accepted);
    let stored: unknown = null;
    recoveryMocks.read.mockImplementation(async () => stored);
    recoveryMocks.write.mockImplementation(async (_scope, draft) => {
      stored = { ...draft, revision: "submitted", updatedAt: new Date().toISOString() };
      return { status: "written", revision: "submitted" };
    });
    recoveryMocks.clear.mockImplementation(async () => { await cleared; stored = null; return true; });
    const first = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "send once" } });
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    first.unmount();
    render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    try {
      await act(async () => { accept(true); });
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(true);
      await act(async () => { clear(); });
      await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false));
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
      expect(stored).toBeNull();
      expect(onSend).toHaveBeenCalledOnce();
    } finally {
      await act(async () => { accept(true); clear(); });
    }
  });

  it("does not resurrect a draft when New work finishes discarding after unmount", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    let stored: unknown = { message: "discard this", attachments: [], revision: "old", updatedAt: new Date().toISOString() };
    recoveryMocks.read.mockImplementation(async () => stored);
    recoveryMocks.clear.mockImplementation(async () => { await pending; stored = null; return true; });
    const control = { current: null as ComposerDraftControl | null };
    const first = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} draftControlRef={control} />);
    await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("discard this"));
    const discarded = control.current!.discardForNewWork();
    first.unmount();
    render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    await act(async () => { finish(); expect(await discarded).toBe(true); });
    await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    expect(recoveryMocks.write).not.toHaveBeenCalled();
  });

  it("retains completed attachments and exact references across a delayed final save", async () => {
    const document = {
      schema_version: 1 as const,
      nodes: [
        { type: "reference" as const, node_id: "12345678-1234-4234-8234-123456789abc", label: "method", parameters: { period: "2026Q3" }, resource: { kind: "skill" as const, source_kind: "team" as const, id: "sk_1", version: `sha256:${"a".repeat(64)}` } },
        { type: "text" as const, text: " Analyze this source." },
      ],
    };
    const attachment = { fileId: "art_ready", filename: "source.pdf", mime: "application/pdf", size: 12 };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let stored: unknown = null;
    recoveryMocks.read.mockImplementation(async () => structuredClone(stored));
    recoveryMocks.write.mockImplementation(async (_scope, draft) => {
      await gate;
      stored = { ...draft, message: "/method Analyze this source.", schemaVersion: 2, revision: "saved_reference", updatedAt: new Date().toISOString() };
      return { status: "written", revision: "saved_reference" };
    });
    const onSend = vi.fn(async (..._args: unknown[]) => false);
    const consumed = vi.fn();
    const first = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} referenceInputEnabled
      prefillInputDocument={document} onPrefillInputDocumentConsumed={consumed} prefillAttachments={[attachment]} />);
    await waitFor(() => expect(consumed).toHaveBeenCalledWith("applied"));
    await screen.findByText("source.pdf");
    first.unmount();
    render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} referenceInputEnabled />);
    await act(async () => { release(); });
    await waitFor(() => expect((screen.getByLabelText("发送") as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText("reference_saved_locked")).toBeNull();
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    expect(onSend.mock.calls[0][1]).toEqual([attachment]);
    expect(onSend.mock.calls[0][3]).toMatchObject({ referenceInputDocument: document });
    expect(stored).toMatchObject({ input_document: document, attachments: [attachment] });
  });
});

describe("Chat upload context", () => {
  it("adds the same file only once, outlining the existing chip and announcing it (D10)", async () => {
    vi.mocked(uploadFile).mockImplementation(async (file) => ({
      file_id: `art_${file.name}`, filename: file.name, size: file.size, mime: file.type, url: "/file",
    }));
    try {
      const view = render(<V2Composer onSend={vi.fn()} draftPersistenceScope={DRAFT_SCOPE} />);
      const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
      await waitFor(() => expect(input.disabled).toBe(false));
      fireEvent.change(input, { target: { files: [new File(["abc"], "sales.csv", { type: "text/csv", lastModified: 1 })] } });
      await waitFor(() => expect(view.container.querySelectorAll(".v2-chip")).toHaveLength(1));
      fireEvent.change(input, { target: { files: [new File(["abc"], "sales.csv", { type: "text/csv", lastModified: 1 })] } });
      expect(view.container.querySelectorAll(".v2-chip")).toHaveLength(1);
      expect(view.container.querySelector(".v2-chip-wrap")?.className).toContain("is-flash");
      expect(view.container.querySelector(".v2-composer-attachments [role=\"status\"]")?.textContent).toBe("duplicate");
      expect(uploadFile).toHaveBeenCalledTimes(1);
      // Same name and size but edited since: a different file, uploaded again.
      fireEvent.change(input, { target: { files: [new File(["xyz"], "sales.csv", { type: "text/csv", lastModified: 2 })] } });
      await waitFor(() => expect(view.container.querySelectorAll(".v2-chip")).toHaveLength(2));
      expect(uploadFile).toHaveBeenCalledTimes(2);
    } finally { vi.mocked(uploadFile).mockReset(); }
  });

  it.each([0, 4])("enforces the attachment limit across one picker batch with %s existing files", async (existing) => {
    vi.mocked(uploadFile).mockImplementation(async (file) => ({
      file_id: `art_${file.name}`, filename: file.name, size: file.size, mime: file.type, url: "/file",
    }));
    try {
      const onSend = vi.fn();
      const view = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE}
        prefillAttachments={Array.from({ length: existing }, (_, i) => ({ fileId: `art_old_${i}`, filename: `old_${i}.txt`, size: 1, mime: "text/plain" }))} />);
      const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
      await waitFor(() => {
        expect(input.disabled).toBe(false);
        expect(view.container.querySelectorAll(".v2-chip")).toHaveLength(existing);
      });
      const files = Array.from({ length: 6 - existing }, (_, i) => new File(["x"], `new_${i}.txt`, { type: "text/plain" }));
      fireEvent.change(input, { target: { files } });
      await waitFor(() => expect(view.container.querySelectorAll(".v2-chip--error").length).toBe(1));
      expect(uploadFile).toHaveBeenCalledTimes(5 - existing);
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onSend).toHaveBeenCalled());
      expect(onSend.mock.calls[0][1]).toHaveLength(5);
    } finally { vi.mocked(uploadFile).mockReset(); }
  });

  it("polls exact bundle members without client-clock or list-page assumptions", async () => {
    const child = { file_id: "kbf_child", artifact_id: "art_child", filename: "child.txt", size_bytes: 5, parse_status: "queued" };
    bundleUploadMock.mockResolvedValueOnce({
      uploaded: [child], skipped: [{ filename: "skipped.bin" }], total_entries: 2,
      bundle_filename: "archive.zip", bundle_artifact_id: "art_root", bundle_mime: "application/zip", bundle_size: 456,
    });
    const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await waitFor(() => expect(input.disabled).toBe(false));
    let polls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (!String(url).includes("/api/v1/kb/files?")) return new Response("{}");
      const query = new URL(url, "http://test").searchParams;
      polls++;
      return new Response(JSON.stringify({ files: query.getAll("file_ids").includes(child.file_id)
        ? [{ ...child, parse_status: polls === 1 ? "parsing" : "done" }]
        : [], total: 1 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      fireEvent.change(input, { target: { files: [new File(["zip"], "archive.zip", { type: "application/zip" })] } });
      fireEvent.click(await screen.findByRole("button", { name: "show_bundle_children" }));
      await waitFor(() => expect(polls).toBe(1), { timeout: 4000 });
      expect(screen.getByText("skipped.bin")).toBeTruthy();
      await waitFor(() => expect(screen.getByLabelText("发送").hasAttribute("disabled")).toBe(false), { timeout: 4000 });
      const query = new URL(String(fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/kb/files?"))![0]), "http://test").searchParams;
      expect(query.getAll("file_ids")).toEqual([child.file_id]);
      expect(query.has("since")).toBe(false);
    } finally { view.unmount(); vi.unstubAllGlobals(); }
  }, 10000);

  it("ends a stalled bundle progress poll with a removable error", async () => {
    bundleUploadMock.mockResolvedValueOnce({
      uploaded: [{ file_id: "kbf_stalled", artifact_id: "art_stalled", filename: "child.txt", size_bytes: 5, parse_status: "queued" }],
      skipped: [], total_entries: 1, bundle_filename: "stalled.zip", bundle_artifact_id: "art_stalled_root", bundle_mime: "application/zip", bundle_size: 456,
    });
    const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await waitFor(() => expect(input.disabled).toBe(false));
    const timeout = globalThis.setTimeout;
    const timer = vi.spyOn(globalThis, "setTimeout").mockImplementation(((...args: Parameters<typeof setTimeout>) => {
      const [callback, ms, ...rest] = args;
      return timeout(callback, ms === 3000 ? 0 : ms, ...rest);
    }) as typeof setTimeout);
    let polls = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/api/v1/kb/files?")) polls++;
      return new Response(JSON.stringify({ files: [] }));
    }));
    try {
      fireEvent.change(input, { target: { files: [new File(["zip"], "stalled.zip", { type: "application/zip" })] } });
      await waitFor(() => expect(polls).toBe(200), { timeout: 5000 });
      await waitFor(() => expect(view.container.querySelector(".v2-chip--error, .v2-chip--warning")).not.toBeNull());
      fireEvent.click(screen.getByRole("button", { name: "dismiss" }));
      expect(screen.queryByText("stalled.zip")).toBeNull();
    } finally { view.unmount(); timer.mockRestore(); vi.unstubAllGlobals(); }
  }, 10000);

  it("rejects an oversized bundle before it can poison draft persistence", async () => {
    bundleUploadMock.mockResolvedValueOnce({
      uploaded: Array.from({ length: 101 }, (_, i) => ({ file_id: `kbf_${i}`, artifact_id: `art_${i}`, filename: `${i}.txt`, size_bytes: 1, parse_status: "done" })),
      skipped: [], total_entries: 101, bundle_filename: "large.zip", bundle_artifact_id: "art_oversized", bundle_mime: "application/zip", bundle_size: 456,
    });
    const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { files: [new File(["zip"], "large.zip", { type: "application/zip" })] } });
    await waitFor(() => expect(view.container.querySelector(".v2-chip--error")).not.toBeNull());
    expect(recoveryMocks.write.mock.calls.every(([, draft]) => !draft.attachments.some((ref: AttachmentRef) => ref.fileId === "art_oversized"))).toBe(true);
  });

  it("keeps completed bundles editable when their combined file count exceeds Send admission", async () => {
    const refs = [0, 1].map((bundle) => ({ fileId: `art_root_${bundle}`, filename: `${bundle}.zip`, size: 1, mime: "application/zip",
      bundleArtifactIds: Array.from({ length: 60 }, (_, i) => `art_${bundle}_${i}`) }));
    const onSend = vi.fn();
    render(<V2Composer onSend={onSend} prefillAttachments={refs} />);
    fireEvent.click(screen.getByLabelText("发送"));
    await act(async () => {});
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText("too_many_attachments")).toBeTruthy();
    expect(screen.getByText("0.zip")).toBeTruthy();
    expect(screen.getByText("1.zip")).toBeTruthy();
  });

  it.each([null, "s_existing"])("uses the displayed draft, not global selection, for session %s", async (requestedSessionId) => {
    vi.stubGlobal("localStorage", { getItem: () => "ws_unrelated" });
    const scope = { ...DRAFT_SCOPE, requestedSessionId };
    const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={scope} />);
    await waitFor(() => expect(view.container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    const file = new File(["{}"], "data.json", { type: "application/json" });
    fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    await waitFor(() => expect(uploadFile).toHaveBeenCalledWith(file, expect.objectContaining({
      sessionId: requestedSessionId ?? undefined,
      spaceHeaders: { "X-Space-Id": scope.workspaceId, "X-Workspace-Id": scope.workspaceId, "X-Team-Id": scope.teamId },
    })));
    vi.unstubAllGlobals();
  });

  it.each(["", "application/x-zip-compressed"])("sends the persisted bundle metadata for browser MIME %s", async (mime) => {
    bundleUploadMock.mockResolvedValueOnce({
      uploaded: [{ file_id: "kbf_child", artifact_id: "art_child", filename: "child.txt", size_bytes: 5, parse_status: "done" }],
      skipped: [], total_entries: 1, bundle_filename: "archive.zip", bundle_artifact_id: "art_root", bundle_mime: "application/zip", bundle_size: 456,
    });
    const onSend = vi.fn();
    const view = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await waitFor(() => expect(input.disabled).toBe(false));
    const file = new File(["zip"], "archive.zip", { type: mime });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(bundleUploadMock).toHaveBeenCalledWith(file, expect.objectContaining({
      scope_id: DRAFT_SCOPE.userId,
      spaceHeaders: { "X-Space-Id": DRAFT_SCOPE.workspaceId, "X-Workspace-Id": DRAFT_SCOPE.workspaceId, "X-Team-Id": DRAFT_SCOPE.teamId },
    })));
    await waitFor(() => expect(screen.getByLabelText("发送").hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend.mock.calls[0][1]).toEqual([expect.objectContaining({ fileId: "art_root", filename: "archive.zip", mime: "application/zip", size: 456, bundleArtifactIds: ["art_child"] })]);
  });

  it("waits for a resolved draft scope before accepting a file", async () => {
    const view = render(<V2Composer onSend={() => {}} />);
    fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["{}"], "data.json", { type: "application/json" })] },
    });
    await act(async () => {});
    expect(uploadFile).not.toHaveBeenCalled();
    expect(view.container.querySelector(".v2-chip")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "tools_title" }));
    expect((screen.getByRole("button", { name: "upload_file" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each(["space", "session", "identity"])("aborts and ignores old progress/results after changing %s", async (change) => {
    let resolveUpload!: (value: Awaited<ReturnType<typeof uploadFile>>) => void;
    vi.mocked(uploadFile).mockImplementationOnce(() => new Promise((resolve) => { resolveUpload = resolve; }));
    const onSend = vi.fn();
    const view = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    await waitFor(() => expect(view.container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["late"], "late.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(uploadFile).toHaveBeenCalledTimes(1));
    const options = vi.mocked(uploadFile).mock.calls[0][1]!;
    const next = {
      ...DRAFT_SCOPE,
      ...(change === "space" ? { workspaceId: "ws_other" } : change === "session" ? { requestedSessionId: "s_other" } : { authSessionGeneration: "asg_other" }),
    };
    view.rerender(<V2Composer onSend={onSend} draftPersistenceScope={next} />);
    await act(async () => {});
    expect(options.signal?.aborted).toBe(true);
    await act(async () => {
      options.onProgress?.(100);
      resolveUpload({ file_id: "art_late", filename: "late.txt", size: 4, mime: "text/plain", url: "/api/v1/files/art_late" });
    });
    expect(view.container.textContent).not.toContain("late.txt");
    view.rerender(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    await act(async () => {});
    expect(view.container.textContent).not.toContain("late.txt");
    expect(onSend).not.toHaveBeenCalled();
    expect(recoveryMocks.write.mock.calls.every(([, draft]) => !draft.attachments.some((ref: AttachmentRef) => ref.fileId === "art_late"))).toBe(true);
  });
});

describe("V2Composer prop-preservation matrix (Phase 1.7-D)", () => {
  it.each(["docx", "dotx", "pptx", "potx"])("routes ZIP-MIME .%s through file upload, not KB bundle", async (ext) => {
    authSessionMock = { me: { id: "u_1", team_id: "team_1" } };
    const onSend = vi.fn();
    const { container } = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
    const file = new File(["synthetic"], `template.${ext}`, { type: "application/zip" });
    vi.mocked(uploadFile).mockResolvedValueOnce({
      file_id: "art_office", filename: file.name, size: file.size,
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", url: "/api/v1/files/art_office",
    });
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    await waitFor(() => expect(uploadFile).toHaveBeenCalledWith(file, expect.any(Object)));
    await waitFor(() => expect(screen.getByLabelText("发送").hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend.mock.calls[0][1]).toEqual([expect.objectContaining({ fileId: "art_office", filename: file.name })]);
    expect(onSend.mock.calls[0][1][0].bundleArtifactIds).toBeUndefined();
  });

  it("shows an Office rejection through the safe translation contract", async () => {
    const { container } = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    vi.mocked(uploadFile).mockRejectedValueOnce(new ApiError(415, "RAW_PACKAGE_PATH", { code: "file_office_invalid_package" }));
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["bad"], "template.pptx", { type: "application/octet-stream" })] },
    });
    await waitFor(() => expect(container.querySelector(".v2-chip--error")?.getAttribute("title")).toContain("errors.office_invalid_package"));
    expect(container.innerHTML).not.toContain("RAW_PACKAGE_PATH");
  });

  it.each([
    [new PresignExpiredError(), "errors.upload_expired"],
    [new UploadHttpError(500, "<html>Internal Server Error</html>"), "errors.service_unavailable"],
  ] as const)("upload failure %# shows a localized reason, never the raw XHR prose or status", async (failure, key) => {
    const { container } = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    vi.mocked(uploadFile).mockRejectedValueOnce(failure);
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["x"], "data.csv", { type: "text/csv" })] },
    });
    await waitFor(() => expect(container.querySelector(".v2-chip--error, .v2-chip--warning")).not.toBeNull());
    const chip = container.querySelector(".v2-chip--error, .v2-chip--warning")!;
    expect(chip.getAttribute("title")).toContain(key);
    expect(container.textContent + String(chip.getAttribute("title"))).not.toMatch(/presign expired|object store|\b(403|500)\b|Internal Server Error/);
    // The chip keeps a short state; the sentence goes to the composer notice slot, never inside the chip.
    const notice = screen.getByTestId("composer-upload-failed");
    expect(notice.textContent).toBe("upload_failed_notice");
    expect(chip.contains(notice)).toBe(false);
  });

  describe("batch A responsive layout", () => {
    it("reacts directly to the latest server planning capability", () => {
      authSessionMock = {
        me: { capabilities: { planning: true } },
      };
      const { rerender } = render(<V2Composer onSend={() => {}} />);

      // ★2026-08-14: move the control from the ＋ panel back to the toolbar (three-state selector, design §5).
      // This still tests immediate reflection of server capabilities; only the lookup location changed.
      expect(screen.getByRole("button", { name: "planning_mode_trigger" })).toBeTruthy();

      authSessionMock = {
        me: { capabilities: { planning: false } },
      };
      rerender(<V2Composer onSend={() => {}} />);

      expect(screen.queryByRole("button", { name: "planning_mode_trigger" })).toBeNull();
    });

    it("renders the compact-first three-zone DOM without the helper hint", () => {
      authSessionMock = {
        me: {
          capabilities: {
            planning: true,
          },
        },
      };

      const { container } = render(<V2Composer onSend={() => {}} />);
      const root = container.querySelector("[data-composer-layout-v2]");
      expect(root?.getAttribute("data-layout-mode")).toBe("compact");
      expect(root?.querySelector(".composer-v2__toolbar")?.children).toHaveLength(2);
      expect(screen.queryByTestId("v2-composer-hint")).toBeNull();
    });

    it("allows a completed attachment to send without helper text", async () => {
      authSessionMock = {
        me: {
          capabilities: {
            planning: true,
          },
        },
      };
      const onSend = vi.fn();
      render(
        <V2Composer
          onSend={onSend}
          prefillAttachments={[{
            fileId: "art_attachment_only",
            filename: "evidence.pdf",
            size: 1024,
            mime: "application/pdf",
          }]}
        />,
      );
      await waitFor(() => expect(screen.getByText("evidence.pdf")).toBeTruthy());
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).toHaveBeenCalledWith("", [expect.objectContaining({
        fileId: "art_attachment_only",
      })]);
    });

    it("keeps the exact draft locked until send-init is definitively accepted", async () => {
      let settle!: (accepted: boolean) => void;
      const onSend = vi.fn(() => new Promise<boolean>((resolve) => {
        settle = resolve;
      }));
      render(<V2Composer onSend={onSend} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "不要提前清掉" } });
      fireEvent.click(screen.getByLabelText("发送"));

      expect(textarea.value).toBe("不要提前清掉");
      expect(textarea.readOnly).toBe(true);
      const preparing = screen.getByLabelText("preparing");
      expect(preparing.getAttribute("aria-busy")).toBe("true");
      expect(preparing.textContent).toBe("");
      expect(preparing.querySelector(".composer-v2__send-spinner")).toBeTruthy();

      settle(true);
      await waitFor(() => expect(textarea.value).toBe(""));
      expect(textarea.readOnly).toBe(false);
    });

    it("clears only the exact persisted revision accepted by send-init", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_submitted",
      });
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "CAS 草稿" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => {
        expect(recoveryMocks.clear).toHaveBeenCalledWith(
          DRAFT_SCOPE,
          "rev_submitted",
        );
      });
    });

    it("New work discards the saved draft by its latest revision and never writes it back", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.write.mockResolvedValue({ status: "written", revision: "rev_typed" });
      recoveryMocks.clear.mockResolvedValue(true);
      const control: { current: ComposerDraftControl | null } = { current: null };
      const { unmount } = render(
        <V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} draftControlRef={control} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "已保存的一版" } });
      await waitFor(() => expect(recoveryMocks.write).toHaveBeenCalledTimes(1));
      // Revision two is still debounced, not persisted: discarding must prevent both timed and unmount fallback saves from writing it back.
      fireEvent.change(textarea, { target: { value: "已保存的一版，又补了半句" } });

      let discarded: boolean | undefined;
      await act(async () => {
        discarded = await control.current?.discardForNewWork();
      });
      expect(discarded).toBe(true);
      expect(recoveryMocks.clear).toHaveBeenCalledWith(DRAFT_SCOPE, "rev_typed");
      // The debounce timer can fire before the page remounts.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 300));
      });
      expect(recoveryMocks.write).toHaveBeenCalledTimes(1);
      unmount();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 300));
      });
      expect(recoveryMocks.write).toHaveBeenCalledTimes(1);
      expect(control.current).toBeNull();
    });

    it("New work keeps a draft whose saved revision it does not own", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "别的标签页的新稿",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.clear.mockResolvedValue(false);
      const control: { current: ComposerDraftControl | null } = { current: null };
      render(
        <V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} draftControlRef={control} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.value).toBe("别的标签页的新稿"));
      await waitFor(() => expect(textarea.readOnly).toBe(false));

      let discarded: boolean | undefined;
      await act(async () => {
        discarded = await control.current?.discardForNewWork();
      });
      expect(discarded).toBe(false);
      expect(recoveryMocks.clear).toHaveBeenCalledWith(DRAFT_SCOPE, "rev_hydrated");
    });

    it("keeps accepted legacy text locked when local cleanup becomes unavailable", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_submitted",
      });
      recoveryMocks.clear.mockRejectedValue(
        new ComposerRecoveryUnavailableError("cleanup unavailable"),
      );
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "accepted once" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(screen.queryByLabelText("preparing")).toBeNull());
      expect(textarea.value).toBe("accepted once");
      expect(textarea.readOnly).toBe(true);
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
      expect(onSend).toHaveBeenCalledTimes(1);

      recoveryMocks.clear.mockResolvedValue(true);
      fireEvent.click(screen.getByRole("button", { name: "draft_cleanup_retry" }));
      await waitFor(() => expect(textarea.value).toBe(""));
      expect(textarea.readOnly).toBe(false);
      expect(recoveryMocks.clear).toHaveBeenLastCalledWith(
        DRAFT_SCOPE,
        "rev_submitted",
      );
    });

    it("adopts a newer canonical draft while retrying accepted cleanup", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_submitted",
      });
      recoveryMocks.clear.mockRejectedValueOnce(
        new ComposerRecoveryUnavailableError("cleanup unavailable"),
      );
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "accepted once" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(textarea.readOnly).toBe(true));

      recoveryMocks.clear.mockResolvedValue(false);
      recoveryMocks.read.mockResolvedValue({
        message: "newer draft",
        attachments: [],
        updatedAt: "2026-08-03T00:00:10Z",
        revision: "rev_newer",
      });
      fireEvent.click(screen.getByRole("button", { name: "draft_cleanup_retry" }));

      await waitFor(() => expect(textarea.value).toBe("newer draft"));
      expect(textarea.readOnly).toBe(false);
      expect(onSend).toHaveBeenCalledTimes(1);
    });

    it("ignores a stale hydration result after reconcile owns the scope", async () => {
      const resolvers: Array<(draft: unknown) => void> = [];
      recoveryMocks.read.mockImplementation(() => new Promise((resolve) => {
        resolvers.push(resolve);
      }));
      const staleAction = {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start" as const,
        attempted: true as const,
        sourceRevision: "rev_stale",
        sessionId: null,
      };
      const onPendingActionHydrated = vi.fn();
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={0}
          recoveryLocked
          onPendingActionHydrated={onPendingActionHydrated}
        />,
      );

      rerender(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={1}
          recoveryLocked={false}
          onPendingActionHydrated={onPendingActionHydrated}
        />,
      );
      await waitFor(() => expect(resolvers).toHaveLength(2));
      resolvers[1]?.({
        message: "canonical draft",
        attachments: [],
        updatedAt: "2026-08-03T00:00:02Z",
        revision: "rev_canonical",
      });
      await waitFor(() => expect(
        (screen.getByRole("textbox") as HTMLTextAreaElement).value,
      ).toBe("canonical draft"));
      resolvers[0]?.({
        message: "stale marker",
        attachments: [],
        updatedAt: "2026-08-03T00:00:01Z",
        revision: "rev_stale",
        pendingAction: staleAction,
      });
      await act(async () => undefined);

      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
        .toBe("canonical draft");
      expect(onPendingActionHydrated).not.toHaveBeenCalled();
    });

    it("keeps denied preflight input editable without reconciling an unsubmitted action", async () => {
      const onSend = vi.fn(async () => true);
      const resolveProtocol = vi.fn()
        .mockResolvedValueOnce(null)
        .mockResolvedValue("terminal_v1");
      render(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={resolveProtocol}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      const input = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(input.readOnly).toBe(false));
      fireEvent.change(input, { target: { value: "Keep this editing request" } });
      const readsBeforeSend = recoveryMocks.read.mock.calls.length;
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(resolveProtocol).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(screen.getByLabelText("发送").hasAttribute("disabled")).toBe(false));
      expect(onSend).not.toHaveBeenCalled();
      expect(recoveryMocks.read).toHaveBeenCalledTimes(readsBeforeSend);
      expect(input.value).toBe("Keep this editing request");
      expect(input.readOnly).toBe(false);
      expect(screen.queryByText("draft_save_failed")).toBeNull();
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(input.value).toBe(""));
    });

    it("leaves Terminal draft settlement to the action owner", async () => {
      recoveryMocks.read.mockResolvedValueOnce({
        message: "same text later",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_terminal",
      });
      const onSend = vi.fn(async () => true);
      const first = render(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={async () => "terminal_v1"}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("same text later");
      });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
      });
      first.unmount();
      expect(recoveryMocks.clear).not.toHaveBeenCalled();

      recoveryMocks.read.mockResolvedValueOnce({
        message: "same text later",
        attachments: [],
        updatedAt: "2026-08-03T00:00:10Z",
        revision: "rev_new_later",
      });
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("same text later");
      });
    });

    it("claims an unchanged persisted revision before submitting", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "already persisted",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      const onSend = vi.fn(async () => false);
      render(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.value).toBe("already persisted"));
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(onSend).toHaveBeenCalledWith(
        "already persisted",
        undefined,
        "rev_default",
      ));
      expect(recoveryMocks.write).toHaveBeenCalledWith(
        DRAFT_SCOPE,
        expect.objectContaining({ message: "already persisted" }),
        "rev_hydrated",
      );
    });

    it("does not rewrite an unchanged hydrated draft during autosave", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "already persisted",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);

      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("already persisted");
      });
      await new Promise((resolve) => window.setTimeout(resolve, 250));

      expect(recoveryMocks.write).not.toHaveBeenCalled();
    });

    it("does not send when the durable draft write fails", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      recoveryMocks.write.mockRejectedValue(new Error("indexedDB write failed"));
      const onSend = vi.fn(async () => true);
      render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "must persist first" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await screen.findByRole("alert");
      expect(onSend).not.toHaveBeenCalled();
      expect(textarea.value).toBe("must persist first");
    });

    it("adopts the post-marker revision after a definitive rejection", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_hydrated",
        })
        .mockResolvedValueOnce({
          message: "rejected but editable",
          attachments: [],
          updatedAt: "2026-08-03T00:00:01Z",
          revision: "rev_after_marker",
        });
      recoveryMocks.write
        .mockResolvedValueOnce({ status: "written", revision: "rev_submitted" })
        .mockResolvedValueOnce({ status: "written", revision: "rev_next" });
      const onSend = vi.fn(async () => false);
      render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "rejected but editable" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(textarea.readOnly).toBe(false));

      fireEvent.change(textarea, { target: { value: "edited after rejection" } });
      await waitFor(() => {
        expect(recoveryMocks.write).toHaveBeenLastCalledWith(
          DRAFT_SCOPE,
          expect.objectContaining({ message: "edited after rejection" }),
          "rev_after_marker",
        );
      });
      expect(screen.queryByText("draft_conflict")).toBeNull();
    });

    it("reconciles the exact persisted state after an external recovery resend", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "recovered action text",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_marker",
        })
        .mockResolvedValueOnce(null);
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={0}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.value).toBe("recovered action text"));

      rerender(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={1}
        />,
      );

      await waitFor(() => expect(textarea.value).toBe(""));
      expect(recoveryMocks.read).toHaveBeenCalledTimes(2);
    });

    it("unlocks a retained send conflict once external recovery restores the persisted draft", async () => {
      // An in-memory fallback start never had a durable draft, so the retained
      // send finds none and marks a conflict. The page then restores the draft
      // durably and bumps the reconcile epoch; adopting that exact persisted
      // state resolves the conflict instead of leaving the input read-only.
      const onSend = vi.fn(async () => false);
      const { rerender } = render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={0}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "fresh fallback draft" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(textarea.readOnly).toBe(true));

      recoveryMocks.read.mockResolvedValue({
        message: "fresh fallback draft",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_restored",
      });
      rerender(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={1}
        />,
      );

      await waitFor(() => expect(textarea.readOnly).toBe(false));
      expect(textarea.value).toBe("fresh fallback draft");
      expect(screen.queryByText("draft_conflict")).toBeNull();
    });

    it("fails closed when external recovery cannot re-read persisted state", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "recovered action text",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_marker",
        })
        .mockRejectedValueOnce(new Error("indexedDB unavailable"));
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
          draftReconcileEpoch={0}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.value).toBe("recovered action text"));

      rerender(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
          draftReconcileEpoch={1}
        />,
      );

      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      expect(textarea.value).toBe("recovered action text");
    });

    it("does not offer discard when reconcile finds an unreadable action marker", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "recovered action text",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_marker",
        })
        .mockRejectedValueOnce(new ComposerOrdinaryDraftInvalidError(
          "future pending action",
          true,
        ));
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={0}
        />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("recovered action text");
      });

      rerender(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={1}
        />,
      );

      await screen.findByRole("alert");
      expect(screen.queryByRole("button", { name: "draft_discard" })).toBeNull();
    });

    it("does not send a write that settles after the Composer scope changed", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_hydrated",
      });
      let resolveWrite!: (value: { status: "written"; revision: string }) => void;
      recoveryMocks.write.mockReturnValue(new Promise((resolve) => {
        resolveWrite = resolve;
      }));
      const onSend = vi.fn(async () => true);
      const { rerender } = render(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "old scope" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(recoveryMocks.write).toHaveBeenCalled());

      rerender(
        <V2Composer onSend={onSend} draftPersistenceScope={OTHER_DRAFT_SCOPE} />,
      );
      resolveWrite({ status: "written", revision: "rev_late" });
      await act(async () => undefined);

      expect(onSend).not.toHaveBeenCalled();
    });

    it("hydrates a newer cross-tab draft instead of clearing it after acceptance", async () => {
      const pendingAction = {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start" as const,
        attempted: true as const,
        sourceRevision: "rev_newer",
        sessionId: null,
      };
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_hydrated",
        })
        .mockResolvedValueOnce({
          message: "newer tab draft",
          attachments: [],
          updatedAt: "2026-08-03T00:00:01Z",
          revision: "rev_newer",
          pendingAction,
        });
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_submitted",
      });
      recoveryMocks.clear.mockResolvedValue(false);
      const onPendingActionHydrated = vi.fn();
      render(
        <V2Composer
          onSend={async () => true}
          draftPersistenceScope={DRAFT_SCOPE}
          onPendingActionHydrated={onPendingActionHydrated}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "submitted draft" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(textarea.value).toBe("newer tab draft"));
      expect(onPendingActionHydrated).toHaveBeenCalledWith(
        expect.objectContaining({ pendingAction }),
      );
    });

    it("fails closed when accepted-draft cleanup cannot re-read the marker", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_hydrated",
        })
        .mockRejectedValueOnce(new Error("indexedDB unavailable"));
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_submitted",
      });
      recoveryMocks.clear.mockResolvedValue(false);
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "keep locked after cleanup failure" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      expect(textarea.value).toBe("keep locked after cleanup failure");
    });

    it("fails closed when a rejected action cannot re-read its retained draft", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_hydrated",
        })
        .mockRejectedValueOnce(new Error("indexedDB unavailable"));
      const onSend = vi.fn(async () => false);
      render(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "keep rejected draft locked" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      expect(textarea.value).toBe("keep rejected draft locked");
    });

    it("blocks mutation after a draft read failure until explicit discard", async () => {
      recoveryMocks.read.mockRejectedValue(new Error("indexedDB corrupt"));
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      expect(recoveryMocks.write).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole("button", { name: "draft_discard" }));
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      expect(recoveryMocks.discard).toHaveBeenCalledWith(DRAFT_SCOPE);
    });

    it("keeps default-off Chat editable when local draft storage is unavailable", async () => {
      recoveryMocks.read.mockRejectedValue(
        new ComposerRecoveryUnavailableError("indexedDB unavailable"),
      );
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );

      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.change(textarea, { target: { value: "legacy send remains available" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => {
        expect(onSend).toHaveBeenCalledWith("legacy send remains available", undefined);
      });
      expect(recoveryMocks.write).not.toHaveBeenCalled();
    });

    it("fails closed on an unknown draft read failure even in storage warn mode", async () => {
      recoveryMocks.read.mockRejectedValue(new Error("unexpected draft decoder failure"));
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );

      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      fireEvent.change(textarea, { target: { value: "must stay fenced" } });
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();
    });

    it("shows a saved reference document without feeding its labels into the text composer", async () => {
      recoveryMocks.read.mockRejectedValue(new DOMException("reference envelope", "VersionError"));
      recoveryMocks.readReference.mockResolvedValue({
        schemaVersion: 2, message: "/method task", attachments: [], revision: "ref_1", updatedAt: "2026-09-09",
        input_document: { schema_version: 1, nodes: [
          { type: "reference", node_id: "n_1", label: "method", parameters: { period: "2026Q3" },
            resource: { kind: "skill", source_kind: "team", id: "sk_1", version: "r_1" } },
          { type: "text", text: " task" },
        ] },
      });
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
      await screen.findByRole("region", { name: "reference_saved_title" });
      expect(screen.getByText("/method")).toBeTruthy();
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();
      expect(recoveryMocks.write).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "reference_remove_all" }));
      await screen.findByText("reference_remove_conflict");
      expect(recoveryMocks.removeReferences).toHaveBeenCalledWith(DRAFT_SCOPE, "ref_1");
      expect(screen.getByText("/method")).toBeTruthy();
    });

    it("does not manufacture an empty transient draft when a version-blocked reader unmounts", async () => {
      recoveryMocks.read.mockRejectedValue(new DOMException("newer draft", "VersionError"));
      const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      await screen.findByText("draft_version_blocked");
      view.unmount();
      expect(readComposerTransientDraft(composerDraftScopeSignature(DRAFT_SCOPE))).toBeNull();
    });

    it("keeps the same input, focus and typed text when the first draft scope resolves", async () => {
      const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={null} />);
      const textarea = view.container.querySelector("textarea")!;
      textarea.focus();
      fireEvent.change(textarea, { target: { value: "typed while the Space loads" } });
      view.rerender(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      await waitFor(() => expect(recoveryMocks.read).toHaveBeenCalledWith(DRAFT_SCOPE));
      await waitFor(() => expect(view.container.querySelector("textarea")?.readOnly).toBe(false));
      expect(view.container.querySelector("textarea")).toBe(textarea);
      expect(textarea.isConnected).toBe(true);
      expect(document.activeElement).toBe(textarea);
      expect(textarea.value).toBe("typed while the Space loads");
    });

    it("starts a fresh input when moving from one draft scope to another", async () => {
      const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = view.container.querySelector("textarea")!;
      view.rerender(<V2Composer onSend={() => {}} draftPersistenceScope={OTHER_DRAFT_SCOPE} />);
      await waitFor(() => expect(recoveryMocks.read).toHaveBeenCalledWith(OTHER_DRAFT_SCOPE));
      expect(textarea.isConnected).toBe(false);
    });

    it("does not show a late reference preview after moving to another space", async () => {
      let finish!: (draft: unknown) => void;
      recoveryMocks.read.mockRejectedValueOnce(new DOMException("reference draft", "VersionError"));
      recoveryMocks.readReference.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
      const view = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      await waitFor(() => expect(recoveryMocks.readReference).toHaveBeenCalledWith(DRAFT_SCOPE));
      recoveryMocks.read.mockResolvedValue(null);
      view.rerender(<V2Composer onSend={() => {}} draftPersistenceScope={OTHER_DRAFT_SCOPE} />);
      await act(async () => finish({
        schemaVersion: 2, message: "private draft", attachments: [], revision: "private_rev", updatedAt: "2026-09-09",
        input_document: { schema_version: 1, nodes: [{ type: "text", text: "private draft" }] },
      }));
      expect(screen.queryByRole("region", { name: "reference_saved_title" })).toBeNull();
      expect(screen.queryByText("private draft")).toBeNull();
    });

    it("preserves a newer draft and offers upgrade guidance without a discard bypass", async () => {
      recoveryMocks.read.mockRejectedValue(new DOMException("newer draft", "VersionError"));
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toContain("draft_version_blocked");
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(true);
      expect(screen.queryByRole("button", { name: "draft_discard" })).toBeNull();
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();
      expect(recoveryMocks.write).not.toHaveBeenCalled();
    });

    it("fails closed on a malformed persisted action even in storage warn mode", async () => {
      recoveryMocks.read.mockRejectedValue(
        new ComposerOrdinaryDraftInvalidError(
          "composer pending action invalid",
          true,
        ),
      );
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          draftPersistenceScope={DRAFT_SCOPE}
          draftPersistenceFailureMode="warn"
        />,
      );

      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await screen.findByRole("alert");
      expect(textarea.readOnly).toBe(true);
      expect(screen.queryByRole("button", { name: "draft_discard" })).toBeNull();
      fireEvent.change(textarea, { target: { value: "must not bypass corrupt marker" } });
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();
    });

    it("hydrates persisted attachments before merging one-shot prefill", async () => {
      let resolveDraft!: (value: unknown) => void;
      recoveryMocks.read.mockReturnValue(new Promise((resolve) => {
        resolveDraft = resolve;
      }));
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          prefillAttachments={[{
            fileId: "art_prefill",
            filename: "prefill.pdf",
            size: 2,
            mime: "application/pdf",
          }]}
        />,
      );
      expect(screen.queryByText("prefill.pdf")).toBeNull();

      await act(async () => {
        resolveDraft({
          message: "",
          attachments: [{
            fileId: "art_saved",
            filename: "saved.csv",
            size: 1,
            mime: "text/csv",
          }],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_saved",
        });
      });

      await waitFor(() => {
        expect(screen.getByText("saved.csv")).toBeTruthy();
        expect(screen.getByText("prefill.pdf")).toBeTruthy();
      });
    });

    it("consumes text prefill only after ordinary-draft hydration completes", async () => {
      let resolveDraft!: (value: null) => void;
      recoveryMocks.read.mockReturnValue(new Promise((resolve) => {
        resolveDraft = resolve;
      }));
      const onPrefillConsumed = vi.fn();
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          prefillText="hydration-safe prefill"
          onPrefillConsumed={onPrefillConsumed}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(textarea.value).toBe("");
      expect(textarea.readOnly).toBe(true);
      expect(onPrefillConsumed).not.toHaveBeenCalled();

      await act(async () => resolveDraft(null));

      await waitFor(() => expect(textarea.value).toBe("hydration-safe prefill"));
      expect(onPrefillConsumed).toHaveBeenCalledWith("applied");
    });

    it("prefills an exact Agent reference once and keeps an existing unsent reference", async () => {
      const document = {
        schema_version: 1 as const,
        nodes: [
          { type: "reference" as const, node_id: "12345678-1234-4234-8234-123456789abc", resource: { kind: "agent" as const, source_kind: "team" as const, id: "osa_1", version: "arv_1", assignment_id: "asa_1", release_id: "arl_1" }, label: "公告核对", parameters: {} },
          { type: "text" as const, text: " " },
        ],
      };
      const onConsumed = vi.fn();
      const { rerender } = render(<V2Composer onSend={() => {}} referenceInputEnabled prefillInputDocument={document} onPrefillInputDocumentConsumed={onConsumed} />);
      await waitFor(() => expect(onConsumed).toHaveBeenCalledWith("applied"));

      rerender(<V2Composer onSend={() => {}} referenceInputEnabled prefillInputDocument={null} onPrefillInputDocumentConsumed={onConsumed} />);
      onConsumed.mockClear();
      rerender(<V2Composer onSend={() => {}} referenceInputEnabled prefillInputDocument={document} onPrefillInputDocumentConsumed={onConsumed} />);
      await waitFor(() => expect(onConsumed).toHaveBeenCalledWith("discarded"));
    });

    it("defers prefill across a failed draft read and applies it after retry", async () => {
      recoveryMocks.read
        .mockRejectedValueOnce(new Error("indexedDB unavailable"))
        .mockResolvedValueOnce(null);
      const onPrefillConsumed = vi.fn();
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          prefillText="do not consume early"
          onPrefillConsumed={onPrefillConsumed}
        />,
      );

      await screen.findByRole("alert");
      expect(onPrefillConsumed).not.toHaveBeenCalled();
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");

      fireEvent.click(screen.getByRole("button", { name: "draft_retry" }));
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("do not consume early");
      });
      expect(onPrefillConsumed).toHaveBeenCalledWith("applied");
    });

    it("acknowledges a stale prefill as discarded when a hydrated draft exists", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "persisted draft wins",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_saved",
      });
      const onPrefillConsumed = vi.fn();
      render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          prefillText="stale prefill"
          onPrefillConsumed={onPrefillConsumed}
        />,
      );

      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("persisted draft wins");
        // Hydration renders its value before the prefill-consumption effect.
        expect(onPrefillConsumed).toHaveBeenCalledWith("discarded");
      });
    });

    it("atomically switches scope and flushes the final edit without waiting 150ms", async () => {
      recoveryMocks.read.mockImplementation(async (scope: typeof DRAFT_SCOPE) => ({
        message: scope.workspaceId === "ws_2" ? "workspace two" : "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: scope.workspaceId === "ws_2" ? "rev_ws_2" : "rev_ws_1",
      }));
      const { rerender } = render(
        <V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      const first = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(first.readOnly).toBe(false));

      fireEvent.change(first, { target: { value: "last edit before scope switch" } });
      rerender(
        <V2Composer onSend={() => {}} draftPersistenceScope={OTHER_DRAFT_SCOPE} />,
      );

      const second = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(second).not.toBe(first);
      await waitFor(() => expect(second.value).toBe("workspace two"));
      await waitFor(() => {
        expect(recoveryMocks.write).toHaveBeenCalledWith(
          DRAFT_SCOPE,
          expect.objectContaining({ message: "last edit before scope switch" }),
          "rev_ws_1",
        );
      });
    });

    it("restores the final edit after an old-scope flush loses its CAS", async () => {
      recoveryMocks.read.mockImplementation(async (scope: typeof DRAFT_SCOPE) => ({
        message: scope.workspaceId === "ws_2" ? "workspace two" : "older A",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: scope.workspaceId === "ws_2" ? "rev_ws_2" : "rev_ws_1",
      }));
      recoveryMocks.write.mockImplementation(async (scope: typeof DRAFT_SCOPE) => (
        scope.workspaceId === "ws_1"
          ? { status: "conflict" as const, revision: "rev_other" }
          : { status: "written" as const, revision: "rev_ws_2_next" }
      ));
      const { rerender } = render(
        <V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("older A");
      });
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "last unsaved A" },
      });

      rerender(
        <V2Composer onSend={() => {}} draftPersistenceScope={OTHER_DRAFT_SCOPE} />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("workspace two");
      });
      rerender(
        <V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />,
      );

      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("last unsaved A");
      });
      expect(screen.queryByText("draft_conflict")).toBeNull();
    });

    it("requires explicit recovery after an old-scope flush becomes unavailable", async () => {
      recoveryMocks.read.mockImplementation(async (scope: typeof DRAFT_SCOPE) => ({
        message: scope.workspaceId === "ws_2" ? "workspace two" : "older A",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: scope.workspaceId === "ws_2" ? "rev_ws_2" : "rev_ws_1",
      }));
      recoveryMocks.write.mockImplementation(async (scope: typeof DRAFT_SCOPE) => {
        if (scope.workspaceId === "ws_1") {
          throw new ComposerRecoveryUnavailableError("storage unavailable");
        }
        return { status: "written" as const, revision: "rev_ws_2_next" };
      });
      const onSend = vi.fn();
      const { rerender } = render(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} draftPersistenceFailureMode="warn" />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("older A");
      });
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "unavailable final A" },
      });

      rerender(
        <V2Composer onSend={() => {}} draftPersistenceScope={OTHER_DRAFT_SCOPE} />,
      );
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("workspace two");
      });
      rerender(
        <V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} draftPersistenceFailureMode="warn" />,
      );

      await screen.findByText("draft_handoff_blocked");
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(true);
      fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
      expect(onSend).not.toHaveBeenCalled();
      expect(readComposerTransientDraft(composerDraftScopeSignature(DRAFT_SCOPE))?.message).toBe("unavailable final A");
      recoveryMocks.write.mockImplementation(async (_scope, draft) => {
        recoveryMocks.read.mockResolvedValue({ ...draft, revision: "rev_recovered", updatedAt: new Date().toISOString() });
        return { status: "written", revision: "rev_recovered" };
      });
      fireEvent.click(screen.getByRole("button", { name: "draft_handoff_retry" }));
      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("unavailable final A");
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false);
      });
    });

    it("flushes the old-scope draft when protocol preflight is still pending", async () => {
      recoveryMocks.read.mockImplementation(async (scope: typeof DRAFT_SCOPE) => ({
        message: scope.workspaceId === "ws_2" ? "workspace two" : "",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: scope.workspaceId === "ws_2" ? "rev_ws_2" : "rev_ws_1",
      }));
      let resolveProtocol!: (protocol: "terminal_v1") => void;
      const onResolveDraftSubmissionProtocol = vi.fn(() => new Promise<"terminal_v1">(
        (resolve) => {
          resolveProtocol = resolve;
        },
      ));
      const onSend = vi.fn(async () => true);
      const { rerender } = render(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={onResolveDraftSubmissionProtocol}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      const first = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(first.readOnly).toBe(false));
      fireEvent.change(first, { target: { value: "preflight pending draft" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onResolveDraftSubmissionProtocol).toHaveBeenCalled());

      rerender(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={onResolveDraftSubmissionProtocol}
          draftPersistenceScope={OTHER_DRAFT_SCOPE}
        />,
      );
      resolveProtocol("terminal_v1");

      await waitFor(() => {
        expect(recoveryMocks.write).toHaveBeenCalledWith(
          DRAFT_SCOPE,
          expect.objectContaining({ message: "preflight pending draft" }),
          "rev_ws_1",
        );
      });
      expect(onSend).not.toHaveBeenCalled();
    });

    it("reuses the hydrated revision for a Terminal action without a draft rewrite", async () => {
      recoveryMocks.read.mockResolvedValue({
        message: "terminal action",
        attachments: [],
        updatedAt: "2026-08-03T00:00:00Z",
        revision: "rev_terminal",
      });
      const onSend = vi.fn(async () => false);
      render(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={async () => "terminal_v1"}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );

      await waitFor(() => {
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
          .toBe("terminal action");
      });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(onSend).toHaveBeenCalledWith(
        "terminal action",
        undefined,
        "rev_terminal",
        {
          writeProtocol: "terminal_v1",
          persistenceAvailable: true,
          draftMessage: "terminal action",
        },
      ));
      expect(recoveryMocks.write).not.toHaveBeenCalled();
    });

    it("does not hand off when protocol resolution rejects the action", async () => {
      const onSend = vi.fn(async () => true);
      render(
        <V2Composer
          onSend={onSend}
          onResolveDraftSubmissionProtocol={async () => null}
          draftPersistenceScope={DRAFT_SCOPE}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "blocked by preflight" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(screen.queryByLabelText("preparing")).toBeNull());
      expect(onSend).not.toHaveBeenCalled();
      expect(textarea.value).toBe("blocked by preflight");
    });

    it.each(["local", "latest"])("keeps upload ownership when choosing the %s reference draft during a delayed save", async (choice) => {
      const document = (label: string) => ({ schema_version: 1 as const, nodes: [
        { type: "reference" as const, node_id: "12345678-1234-4234-8234-123456789abc", label, parameters: {}, resource: { kind: "skill" as const, source_kind: "team" as const, id: "sk_1", version: `sha256:${"a".repeat(64)}` } },
      ] });
      let finishUpload!: (value: Awaited<ReturnType<typeof uploadFile>>) => void;
      vi.mocked(uploadFile).mockImplementationOnce(() => new Promise((resolve) => { finishUpload = resolve; }));
      recoveryMocks.read.mockResolvedValueOnce(null).mockResolvedValue({
        message: "/Remote", input_document: document("Remote"), attachments: [], revision: "remote_revision", updatedAt: "2026-10-04T00:00:00Z",
      });
      recoveryMocks.write.mockResolvedValue({ status: "conflict", revision: "remote_revision" });
      const consumed = vi.fn();
      const view = render(<V2Composer onSend={() => false} draftPersistenceScope={DRAFT_SCOPE} referenceInputEnabled
        prefillInputDocument={document("Local")} onPrefillInputDocumentConsumed={consumed} />);
      await waitFor(() => expect(consumed).toHaveBeenCalledWith("applied"));
      const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
      fireEvent.change(input, { target: { files: [new File(["test"], "pending.txt", { type: "text/plain" })] } });
      await screen.findByText("draft_conflict");
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));
      await screen.findByRole("button", { name: "reference_keep_local" });
      let finishSave!: (value: { status: "written"; revision: string }) => void;
      recoveryMocks.write.mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve; }));
      fireEvent.click(screen.getByRole("button", { name: choice === "local" ? "reference_keep_local" : "reference_use_latest" }));
      await waitFor(() => expect(finishSave).toBeDefined());
      await act(async () => { finishUpload({ file_id: "art_completed", filename: "pending.txt", size: 4, mime: "text/plain", url: "/api/v1/files/art_completed" }); });
      await waitFor(() => expect(view.container.querySelector(".v2-chip--done")).not.toBeNull());
      recoveryMocks.write.mockResolvedValue({ status: "written", revision: "settled_revision" });
      await act(async () => { finishSave({ status: "written", revision: "chosen_revision" }); });
      await waitFor(() => expect(screen.queryByText("draft_conflict")).toBeNull());
      if (choice === "local") {
        expect(view.container.querySelector(".v2-chip--done")).not.toBeNull();
        await waitFor(() => expect(recoveryMocks.write).toHaveBeenLastCalledWith(DRAFT_SCOPE, expect.objectContaining({
          input_document: document("Local"), attachments: [expect.objectContaining({ fileId: "art_completed" })],
        }), "chosen_revision"));
      } else {
        expect(view.container.querySelector(".v2-chip--done")).toBeNull();
      }
    });

    it.each(["file", "bundle", "failure"])("retains an in-flight %s result until the draft conflict is explicitly merged", async (kind) => {
      let settle!: () => void;
      const completion = new Promise<void>((resolve) => { settle = resolve; });
      const filename = kind === "bundle" ? "pending.zip" : "pending.txt";
      if (kind !== "bundle") vi.mocked(uploadFile).mockImplementationOnce(async () => {
        await completion;
        if (kind === "failure") throw new Error("Upload failed");
        return { file_id: "art_completed", filename, size: 4, mime: "text/plain", url: "/api/v1/files/art_completed" };
      });
      else bundleUploadMock.mockImplementationOnce(async () => {
        await completion;
        return {
          uploaded: [{ file_id: "kbf_child", artifact_id: "art_child", filename: "child.txt", size_bytes: 4, parse_status: "done" }],
          skipped: [], total_entries: 1, bundle_filename: filename, bundle_artifact_id: "art_completed", bundle_mime: "application/zip", bundle_size: 4,
        };
      });
      recoveryMocks.read.mockResolvedValueOnce(null).mockResolvedValue({
        message: "Remote intent", attachments: [], revision: "remote_revision", updatedAt: "2026-10-04T00:00:00Z",
      });
      recoveryMocks.write.mockResolvedValue({ status: "conflict", revision: "remote_revision" });
      const onSend = vi.fn(async (..._args: unknown[]) => false);
      const view = render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
      const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
      await waitFor(() => expect(input.disabled).toBe(false));
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "Local intent" } });
      fireEvent.change(input, { target: { files: [new File(["test"], filename, { type: kind === "bundle" ? "application/zip" : "text/plain" })] } });
      await screen.findByText("draft_conflict");
      const writesAtConflict = recoveryMocks.write.mock.calls.length;
      await act(async () => { settle(); await completion; });
      await waitFor(() => expect(view.container.querySelector(kind === "failure" ? ".v2-chip--error" : ".v2-chip--done")).not.toBeNull());
      expect(screen.getByText("draft_conflict")).toBeTruthy();
      expect(screen.getByLabelText("发送").hasAttribute("disabled")).toBe(true);
      expect(recoveryMocks.write).toHaveBeenCalledTimes(writesAtConflict);
      expect(onSend).not.toHaveBeenCalled();
      recoveryMocks.write.mockResolvedValue({ status: "written", revision: "merged_revision" });
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));
      await waitFor(() => expect(screen.queryByText("draft_conflict")).toBeNull());
      if (kind === "failure") {
        expect(view.container.querySelector(".v2-chip--error")).not.toBeNull();
        return;
      }
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
      expect(onSend.mock.calls[0][1]).toEqual([expect.objectContaining({
        fileId: "art_completed", filename, ...(kind === "bundle" ? { bundleArtifactIds: ["art_child"] } : {}),
      })]);
    });

    it("keeps the current tab draft and offers a deterministic merge on CAS conflict", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_initial",
        })
        .mockResolvedValueOnce({
          message: "other tab draft",
          attachments: [],
          updatedAt: "2026-08-03T00:00:01Z",
          revision: "rev_other",
        })
        .mockResolvedValueOnce({
          message: "other tab draft",
          attachments: [],
          updatedAt: "2026-08-03T00:00:01Z",
          revision: "rev_other",
        });
      recoveryMocks.write.mockResolvedValue({
        status: "conflict",
        revision: "rev_other",
      });
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "current tab draft" } });

      await screen.findByText("draft_conflict");
      expect(textarea.value).toBe("current tab draft");
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));

      await waitFor(() => {
        expect(textarea.value).toBe("current tab draft\n\nother tab draft");
      });
      expect(screen.queryByText("draft_conflict")).toBeNull();
    });

    it("coalesces a lost CAS when the winning writer saved the same draft", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_initial",
        })
        .mockResolvedValueOnce({
          message: "same intent",
          attachments: [],
          updatedAt: "2026-08-03T00:00:01Z",
          revision: "rev_winner",
        });
      recoveryMocks.write.mockResolvedValue({
        status: "conflict",
        revision: "rev_winner",
      });
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "same intent" } });

      await waitFor(() => expect(recoveryMocks.read).toHaveBeenCalledTimes(2));
      expect(screen.queryByText("draft_conflict")).toBeNull();
      expect(textarea.readOnly).toBe(false);
      expect(textarea.value).toBe("same intent");
    });

    it("stops offering discard if the stored draft becomes incompatible before discard", async () => {
      recoveryMocks.read.mockRejectedValue(new Error("invalid draft"));
      recoveryMocks.discard.mockRejectedValue(new DOMException("future envelope", "VersionError"));
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      fireEvent.click(await screen.findByRole("button", { name: "draft_discard" }));
      await screen.findByText("draft_version_blocked");
      expect(screen.queryByRole("button", { name: "draft_discard" })).toBeNull();
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(true);
      expect(recoveryMocks.write).not.toHaveBeenCalled();
    });

    it("keeps local text and shows upgrade guidance when a conflict winner has a newer format", async () => {
      retainComposerTransientDraft(composerDraftScopeSignature(DRAFT_SCOPE), {
        message: "local intent", attachments: [], expectedRevision: "rev_old",
      });
      recoveryMocks.read
        .mockResolvedValueOnce({ message: "remote intent", attachments: [], updatedAt: "2026-09-08", revision: "rev_new" })
        .mockRejectedValueOnce(new DOMException("future envelope", "VersionError"));
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} />);
      await screen.findByText("draft_conflict");
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));
      await screen.findByText("draft_version_blocked");
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("local intent");
      expect(screen.queryByRole("button", { name: "draft_discard" })).toBeNull();
      expect(recoveryMocks.write).not.toHaveBeenCalled();
      expect(onSend).not.toHaveBeenCalled();
    });

    it("does not resurrect abandoned transient text after adopting a pending action", async () => {
      const pendingAction = {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start" as const,
        attempted: true as const,
        sourceRevision: "rev_marker",
        sessionId: null,
      };
      const markerDraft = {
        message: "canonical pending action",
        attachments: [],
        updatedAt: "2026-08-03T00:00:01Z",
        revision: "rev_marker",
        pendingAction,
      };
      retainComposerTransientDraft(composerDraftScopeSignature(DRAFT_SCOPE), {
        message: "abandoned local intent",
        attachments: [],
        expectedRevision: "rev_older",
      });
      recoveryMocks.read.mockResolvedValue(markerDraft);
      const onPendingActionHydrated = vi.fn();
      const mounted = render(
        <V2Composer
          onSend={() => {}}
          draftPersistenceScope={DRAFT_SCOPE}
          onPendingActionHydrated={onPendingActionHydrated}
        />,
      );

      await screen.findByText("draft_conflict");
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
        .toBe("abandoned local intent");
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));

      await waitFor(() => expect(screen.queryByText("draft_conflict")).toBeNull());
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
        .toBe("canonical pending action");
      expect(onPendingActionHydrated).toHaveBeenCalledWith(markerDraft);

      mounted.unmount();
      recoveryMocks.read.mockResolvedValue(null);
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      await waitFor(() => expect(recoveryMocks.read).toHaveBeenCalledTimes(3));
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    });

    it("keeps the local intent when the winning tab deleted the remote draft", async () => {
      recoveryMocks.read
        .mockResolvedValueOnce({
          message: "",
          attachments: [],
          updatedAt: "2026-08-03T00:00:00Z",
          revision: "rev_initial",
        })
        .mockResolvedValueOnce(null)
        .mockResolvedValueOnce(null);
      recoveryMocks.write
        .mockResolvedValueOnce({ status: "conflict", revision: null })
        .mockResolvedValueOnce({ status: "written", revision: "rev_local" });
      render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "stale local intent" } });

      await screen.findByText("draft_conflict");
      fireEvent.click(screen.getByRole("button", { name: "draft_merge_latest" }));

      await waitFor(() => expect(screen.queryByText("draft_conflict")).toBeNull());
      expect(textarea.readOnly).toBe(false);
      expect(textarea.value).toBe("stale local intent");
    });

    it("keeps the draft editable but blocks submit while Stop awaits durable ack", async () => {
      let resolveCancel!: () => void;
      const onCancel = vi.fn(() => new Promise<void>((resolve) => {
        resolveCancel = resolve;
      }));
      const onSteer = vi.fn();
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={onSteer}
          running
          onCancel={onCancel}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "do not race stop" } });

      fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      const stopping = screen.getByRole("button", { name: "stopping" }) as HTMLButtonElement;
      expect(stopping.disabled).toBe(true);
      expect(stopping.getAttribute("aria-busy")).toBe("true");
      expect(screen.getByTestId("composer-action-status").textContent).toBe("stopping");
      const composer = textarea.closest(".v2-composer");
      expect(composer?.classList.contains("is-stopping")).toBe(true);
      expect(composer?.classList.contains("is-preparing")).toBe(false);
      expect(textarea.readOnly).toBe(false);
      fireEvent.change(textarea, { target: { value: "draft while stopping" } });
      expect(textarea.value).toBe("draft while stopping");
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
      expect(onSteer).not.toHaveBeenCalled();
      fireEvent.click(stopping);
      expect(onCancel).toHaveBeenCalledTimes(1);

      resolveCancel();
      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
      });
      expect(textarea.readOnly).toBe(false);
    });

    it("distinguishes an accepted Stop request from the transport wait", () => {
      const onCancel = vi.fn();
      render(
        <V2Composer
          onSend={() => {}}
          running
          onCancel={onCancel}
          stopRequested
        />,
      );

      const accepted = screen.getByRole("button", {
        name: "stop_requested",
      }) as HTMLButtonElement;
      expect(accepted.disabled).toBe(true);
      expect(accepted.getAttribute("aria-busy")).toBe("true");
      expect(screen.getByTestId("composer-action-status").textContent)
        .toBe("stop_requested");
      fireEvent.click(accepted);
      expect(onCancel).not.toHaveBeenCalled();
    });

    it("does not steal focus from controls when Stop only locks submission", async () => {
      authSessionMock = {
        me: {
          capabilities: {
            planning: true,
          },
        },
      };
      const { rerender } = render(<V2Composer onSend={() => {}} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      const model = screen.getByTestId("mock-model-picker") as HTMLButtonElement;
      model.focus();
      expect(document.activeElement).toBe(model);

      rerender(<V2Composer onSend={() => {}} stopRequested />);

      await waitFor(() => expect(document.activeElement).toBe(model));
      expect(textarea.readOnly).toBe(false);
    });

    it("allows autocomplete and attachment draft edits while Stop is pending", async () => {
      suggestionMock = "_completion";
      candidatesMock = ["draft_completion"];
      let resolveCancel!: () => void;
      const onCancel = vi.fn(() => new Promise<void>((resolve) => {
        resolveCancel = resolve;
      }));
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={() => {}}
          running
          onCancel={onCancel}
          prefillAttachments={[{
            fileId: "art_locked",
            filename: "locked.pdf",
            size: 1,
            mime: "application/pdf",
          }]}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "draft" } });
      await waitFor(() => expect(screen.getByText("locked.pdf")).toBeTruthy());
      fireEvent.click(screen.getByRole("button", { name: "Stop" }));

      fireEvent.keyDown(textarea, { key: "Tab" });
      expect(textarea.value).toBe("draft_completion");
      expect((screen.getByRole("button", { name: "dismiss" }) as HTMLButtonElement).disabled)
        .toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "dismiss" }));
      expect(screen.queryByText("locked.pdf")).toBeNull();

      resolveCancel();
      await waitFor(() => expect(textarea.readOnly).toBe(false));
    });

    it("handles a rejected Stop promise without leaving the composer locked", async () => {
      const onCancel = vi.fn(async () => {
        throw new Error("network failure");
      });
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={() => {}}
          running
          onCancel={onCancel}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
      });
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).readOnly).toBe(false);
    });

    it("preserves the draft after a definitive send-init rejection", async () => {
      const onSend = vi.fn(async () => false);
      render(<V2Composer onSend={onSend} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "请保留" } });
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(textarea.readOnly).toBe(false));
      expect(textarea.value).toBe("请保留");
    });

    it("hides the plan-gated view without unmounting its draft controller", () => {
      const { rerender } = render(<V2Composer onSend={() => {}} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "保留这段草稿" } });

      rerender(<V2Composer onSend={() => {}} hidden />);
      expect(textarea.value).toBe("保留这段草稿");
      rerender(<V2Composer onSend={() => {}} />);

      expect(screen.getByRole("textbox")).toBe(textarea);
      expect(textarea.value).toBe("保留这段草稿");
    });

    it("fences every submit path until the IME final input has settled", async () => {
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;

      fireEvent.compositionStart(textarea);
      expect(textarea.readOnly).toBe(false);
      fireEvent.change(textarea, { target: { value: "拼" } });
      expect(textarea.value).toBe("拼");
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();

      fireEvent.compositionEnd(textarea);
      fireEvent.change(textarea, { target: { value: "拼音" } });
      await waitFor(() => {
        expect(textarea.readOnly).toBe(false);
        expect((screen.getByLabelText("发送") as HTMLButtonElement).disabled).toBe(false);
      });

      fireEvent.compositionStart(textarea);
      expect(textarea.readOnly).toBe(false);
      fireEvent.change(textarea, { target: { value: "拼音输入" } });
      expect(textarea.value).toBe("拼音输入");
      fireEvent.compositionEnd(textarea);
      fireEvent.change(textarea, { target: { value: "拼音输入完成" } });
      await waitFor(() => {
        expect((screen.getByLabelText("发送") as HTMLButtonElement).disabled).toBe(false);
      });
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).toHaveBeenCalledWith("拼音输入完成", undefined);
    });

    it("keeps recovery text selectable, freezes submit, and preserves Stop", () => {
      const onSend = vi.fn();
      const onCancel = vi.fn();
      const { rerender } = render(
        <V2Composer
          onSend={onSend}
          recoveryLocked
          prefillText="可复制的恢复草稿"
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(textarea.readOnly).toBe(true);
      expect(textarea.disabled).toBe(false);
      textarea.focus();
      textarea.select();
      expect(document.activeElement).toBe(textarea);
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).not.toHaveBeenCalled();

      rerender(
        <V2Composer
          onSend={onSend}
          onCancel={onCancel}
          running
          recoveryLocked
          prefillText="可复制的恢复草稿"
        />,
      );
      const stop = screen.getByRole("button", { name: /stop/i }) as HTMLButtonElement;
      expect(stop.disabled).toBe(false);
      fireEvent.click(stop);
      expect(onCancel).toHaveBeenCalledTimes(1);
    });

    it("replays an attachment-only failed turn with its exact bundle identity", () => {
      const onSend = vi.fn();
      const bundle = {
        fileId: "art_bundle",
        filename: "evidence.zip",
        size: 100,
        mime: "application/zip",
        bundleArtifactIds: ["art_child_a", "art_child_b"],
      };
      const control: { current: ComposerDraftControl | null } = { current: null };
      render(
        <V2Composer
          onSend={onSend}
          lastUserText=""
          lastUserAttachments={[bundle]}
          draftControlRef={control}
        />,
      );

      // The failure card's Retry replays through the composer control (D4).
      act(() => { control.current!.requestReplay(); });
      expect(onSend).toHaveBeenCalledWith("", [bundle]);
    });

    it("preserves bundle root and child membership through prefill submit", async () => {
      const onSend = vi.fn();
      const bundle = {
        fileId: "art_bundle_prefill",
        filename: "quarterly.zip",
        size: 100,
        mime: "application/zip",
        bundleArtifactIds: ["art_q1", "art_q2"],
      };
      render(<V2Composer onSend={onSend} prefillAttachments={[bundle]} />);
      await waitFor(() => expect(screen.getByText("quarterly.zip")).toBeTruthy());

      fireEvent.click(screen.getByLabelText("发送"));
      expect(onSend).toHaveBeenCalledWith("", [bundle]);
    });
  });

  describe("borderRadius-14 (1.7-D visual)", () => {
    it("renders parent-driven state feedback inside the Composer", () => {
      render(
        <V2Composer
          onSend={() => {}}
          statusMessage="Stopped draft restored"
        />,
      );
      expect(screen.getByTestId("composer-state-note").textContent)
        .toBe("Stopped draft restored");
    });

    it("renders the outer .v2-composer container", () => {
      const { container } = render(<V2Composer onSend={() => {}} />);
      const outer = container.querySelector(".v2-composer");
      expect(outer).toBeTruthy();
      expect(outer?.className).toContain("v2-composer");
    });

    it("globals.css .v2-composer rule has border-radius: 14px (post-1.7-D)", () => {
      // jsdom doesn't evaluate external CSS for computed style. Parse the
      // globals.css source directly so the spec stays grep-stable.
      const cssPath = path.resolve(__dirname, "../../app/globals.css");
      const css = readFileSync(cssPath, "utf-8");
      // Match the `.v2-composer {...}` rule (NOT .v2-composer-wrap / textarea / etc).
      // The rule starts with `.v2-composer {` on its own line.
      const m = css.match(/\n\.v2-composer\s*\{[\s\S]*?\n\}/);
      expect(m).toBeTruthy();
      const rule = m![0];
      expect(rule).toMatch(/border-radius:\s*14px/);
    });
  });

  describe("replay after a failed turn (Chat Cards Spec D4: Retry lives only on the failure card)", () => {
    const renderWithControl = (props: Partial<Parameters<typeof V2Composer>[0]> = {}) => {
      const control: { current: ComposerDraftControl | null } = { current: null };
      render(<V2Composer onSend={() => {}} lastUserText="last query" draftControlRef={control} {...props} />);
      return control;
    };

    it("keeps Send (disabled on empty input) instead of a Replay button", () => {
      renderWithControl();
      expect(screen.queryByText("重放")).toBeNull();
      expect((screen.getByLabelText("发送") as HTMLButtonElement).disabled).toBe(true);
    });

    it("the failure card's Retry fires onSend(lastUserText) through the composer control", () => {
      const onSend = vi.fn();
      const control = renderWithControl({ onSend });
      let accepted = false;
      act(() => { accepted = control.current!.requestReplay(); });
      expect(accepted).toBe(true);
      expect(onSend).toHaveBeenCalledWith("last query");
    });

    it("keeps Replay single-flight until the accepted attempt settles", async () => {
      let settle!: (accepted: boolean) => void;
      const onSend = vi.fn(() => new Promise<boolean>((resolve) => {
        settle = resolve;
      }));
      const control = renderWithControl({ onSend });
      act(() => {
        control.current!.requestReplay();
        control.current!.requestReplay();
      });
      expect(onSend).toHaveBeenCalledTimes(1);
      expect(screen.getByLabelText("preparing").getAttribute("aria-busy")).toBe("true");

      await act(async () => { settle(true); });
      await waitFor(() => expect(screen.getByLabelText("发送")).toBeTruthy());
      act(() => { control.current!.requestReplay(); });
      expect(onSend).toHaveBeenCalledTimes(2);
    });

    it("failure-card Retry goes through the server replay single-flight (no double send)", async () => {
      let settle!: (accepted: boolean) => void;
      const onReplay = vi.fn(() => new Promise<boolean>((resolve) => {
        settle = resolve;
      }));
      const control = renderWithControl({ onReplay });

      let first = false;
      let second = true;
      act(() => {
        first = control.current!.requestReplay();
        second = control.current!.requestReplay();
      });
      expect(first).toBe(true);
      expect(second).toBe(false);
      expect(onReplay).toHaveBeenCalledTimes(1);

      await act(async () => { settle(true); });
      await waitFor(() => expect(screen.getByLabelText("发送")).toBeTruthy());
    });

    it("shows Stop while running", () => {
      renderWithControl({ running: true, onCancel: () => {} });
      expect(screen.getByRole("button", { name: /stop/i })).toBeTruthy();
    });
  });

  describe("prop preservation (regression guards — DO NOT REMOVE)", () => {
    it("autoFocus: focuses the textarea for an explicit New work reset", () => {
      render(<V2Composer onSend={() => {}} autoFocus />);
      expect(document.activeElement).toBe(screen.getByRole("textbox"));
    });

    it("prefillText: textarea value reflects prefill", () => {
      render(<V2Composer onSend={() => {}} prefillText="hello world" />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(ta.value).toBe("hello world");
    });

    it("keeps manually edited chart questions while replacing their explicit selected reference", async () => {
      const selectedContext = { version: 1 as const, sectionId: "sec", sectionRev: 1, viewId: "v", bindingId: "b", datasetDigest: "a".repeat(64), measureKeys: ["revenue"], tuples: [["A"]] };
      const attachment = { fileId: "art", filename: "data.csv", size: 42, mime: "text/csv", referenceKind: "result" as const, selectedContext };
      const view = render(<V2Composer onSend={vi.fn()} prefillText="Explain A" prefillAttachments={[attachment]} />);
      const input = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(input, { target: { value: "Keep my exact question" } });
      view.rerender(<V2Composer onSend={vi.fn()} prefillText="Explain B" prefillAttachments={[{ ...attachment, selectedContext: { ...selectedContext, tuples: [["B"]] } }]} />);
      await waitFor(() => expect(input.value).toBe("Keep my exact question"));
      expect(screen.getByText("data.csv").closest(".v2-chip")?.getAttribute("title")).toContain('[["B"]]');
    });
    it("onSteer: when running+onSteer set, Enter fires onSteer (not onSend)", () => {
      const onSend = vi.fn();
      const onSteer = vi.fn();
      render(
        <V2Composer onSend={onSend} onSteer={onSteer} running />,
      );
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "nudge" } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
      expect(onSteer).toHaveBeenCalledWith("nudge", undefined);
      expect(onSend).not.toHaveBeenCalled();
    });

    it("onSteer: async false keeps the user's draft in place", async () => {
      const onSend = vi.fn();
      const onSteer = vi.fn(async () => false);
      render(
        <V2Composer onSend={onSend} onSteer={onSteer} running />,
      );
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "keep this steer" } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });

      await waitFor(() => expect(onSteer).toHaveBeenCalledWith("keep this steer", undefined));
      expect(onSend).not.toHaveBeenCalled();
      expect(ta.value).toBe("keep this steer");
    });

    it("onSteer: async success clears only the submitted draft", async () => {
      const onSteer = vi.fn(async () => true);
      render(
        <V2Composer onSend={() => {}} onSteer={onSteer} running />,
      );
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "ship this steer" } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });

      await waitFor(() => expect(ta.value).toBe(""));
    });

    it("onSteer: ignores a stale draft reconcile that finishes after acceptance", async () => {
      let resolveSteer!: (accepted: boolean) => void;
      let resolveReconcile!: (draft: unknown) => void;
      recoveryMocks.read
        .mockResolvedValueOnce(null)
        .mockReturnValueOnce(new Promise((resolve) => {
          resolveReconcile = resolve;
        }));
      recoveryMocks.write.mockResolvedValue({
        status: "written",
        revision: "rev_steer",
      });
      const onSteer = vi.fn(() => new Promise<boolean>((resolve) => {
        resolveSteer = resolve;
      }));
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          onSteer={onSteer}
          running
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={0}
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "accepted steer" } });
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
      await waitFor(() => expect(onSteer).toHaveBeenCalled());

      rerender(
        <V2Composer
          onSend={() => {}}
          onSteer={onSteer}
          running
          draftPersistenceScope={DRAFT_SCOPE}
          draftReconcileEpoch={1}
        />,
      );
      await waitFor(() => expect(recoveryMocks.read).toHaveBeenCalledTimes(2));

      resolveSteer(true);
      await waitFor(() => expect(textarea.value).toBe(""));
      resolveReconcile({
        message: "accepted steer",
        attachments: [],
        updatedAt: "2026-08-16T00:00:00Z",
        revision: "rev_steer",
      });
      await act(async () => undefined);

      expect(textarea.value).toBe("");
      expect(textarea.readOnly).toBe(false);
    });

    it("onSteer: accepts an attachment-only draft with its complete reference", async () => {
      const onSteer = vi.fn(async () => true);
      const attachment = {
        fileId: "art_steer",
        filename: "evidence.pdf",
        size: 42,
        mime: "application/pdf",
      };
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={onSteer}
          running
          prefillAttachments={[attachment]}
        />,
      );

      await waitFor(() => expect(screen.getByText("evidence.pdf")).toBeTruthy());
      fireEvent.click(screen.getByLabelText("发送"));

      await waitFor(() => expect(onSteer).toHaveBeenCalledWith("", [attachment]));
      await waitFor(() => expect(screen.queryByText("evidence.pdf")).toBeNull());
    });

    it("sessionTexts Tab autocomplete: Tab accepts suggestion", () => {
      // Arrange: mock returns a suggestion so Tab handler triggers accept.
      suggestionMock = "_completion";
      candidatesMock = ["abc_completion"];
      render(
        <V2Composer
          onSend={() => {}}
          sessionTexts={["abc_completion"]}
        />,
      );
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "abc" } });
      fireEvent.keyDown(ta, { key: "Tab" });
      // Value should be original + suggestion concatenated.
      expect(ta.value).toBe("abc_completion");
    });

    it("onCancel: Stop button mounts when running+onCancel", () => {
      const onCancel = vi.fn();
      render(
        <V2Composer onSend={() => {}} running onCancel={onCancel} />,
      );
      const cancelBtn = screen.getByRole("button", { name: /stop/i });
      expect(cancelBtn.textContent).toContain("Stop");
      fireEvent.click(cancelBtn);
      expect(onCancel).toHaveBeenCalledTimes(1);
    });

    it("compact mode exposes the translated tools button with the wide-plus glyph", () => {
      render(<V2Composer onSend={() => {}} />);
      const toolsButton = screen.getByRole("button", { name: "tools_title" });
      expect(toolsButton.textContent).toBe("＋");
    });

    it("tools sheet opens as a modal labelled by the trigger and closes on Escape", () => {
      render(<V2Composer onSend={() => {}} />);
      const toolsButton = screen.getByRole("button", { name: "tools_title" });
      toolsButton.focus();
      fireEvent.click(toolsButton);

      const sheet = screen.getByRole("dialog", { name: "tools_title" });
      expect(sheet.getAttribute("aria-modal")).toBe("true");
      expect(sheet.id).toBe(toolsButton.getAttribute("aria-controls"));
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "tools_close" }));

      fireEvent.keyDown(document.activeElement!, { key: "Escape" });
      expect(screen.queryByRole("dialog", { name: "tools_title" })).toBeNull();
      expect(toolsButton.getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(toolsButton);
    });

    it("offers Google Drive selection only with the capability and inserts plain name-link lines", async () => {
      authSessionMock = { me: { capabilities: { google_workspace: false } } };
      const { rerender } = render(<V2Composer onSend={() => {}} />);
      fireEvent.click(screen.getByRole("button", { name: "tools_title" }));
      expect(screen.queryByRole("button", { name: /composer_entry/ })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "tools_close" }));

      authSessionMock = { me: { capabilities: { google_workspace: true } } };
      rerender(<V2Composer onSend={() => {}} />);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "对比这两份" } });
      googlePickerMocks.pickDriveFiles.mockResolvedValueOnce([
        { id: "a1", name: "季度报告", mimeType: "application/vnd.google-apps.document", url: "https://docs.google.com/document/d/a1/edit" },
        { id: "b2", name: "预算", mimeType: "application/vnd.google-apps.spreadsheet", url: "https://docs.google.com/spreadsheets/d/b2/edit" },
      ]);
      fireEvent.click(screen.getByRole("button", { name: "tools_title" }));
      fireEvent.click(screen.getByRole("button", { name: /composer_entry/ }));

      await waitFor(() =>
        expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
          "对比这两份\n季度报告 https://docs.google.com/document/d/a1/edit\n预算 https://docs.google.com/spreadsheets/d/b2/edit",
        ),
      );
    });

    it("/remember slash: short-circuits and skips onSend", async () => {
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "/remember hello" } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
      // /remember was dispatched (async), onSend NOT called.
      await waitFor(() => expect(dispatchRememberMock).toHaveBeenCalled());
      expect(onSend).not.toHaveBeenCalled();
    });

    it.each([
      [{ ok: false as const, reason: "syntax" as const, msg: "syntax: /remember [kind:]key=value" }, "remember_failed_syntax", false],
      [
        { ok: false as const, reason: "failed" as const, msg: "remember: 500", error: new UploadHttpError(500, "boom") },
        "remember_failed_failed",
        true,
      ],
      [
        { ok: false as const, reason: "refused" as const, refusal: "contains_secret" as const, msg: "gate prose", error: new UploadHttpError(422, "{}") },
        "refused_contains_secret",
        true,
      ],
    ])("/remember failure %# shows the localized reason, not the raw message", async (outcome, key, copyable) => {
      dispatchRememberMock.mockResolvedValueOnce(outcome as never);
      render(<V2Composer onSend={() => {}} />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "/remember nonsense" } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });

      const message = await screen.findByTestId("composer-slash-msg");
      expect(message.textContent).toBe(key);
      expect(message.textContent).not.toMatch(/syntax:|remember:|500/);
      const copy = screen.queryByRole("button", { name: "copyShort" });
      expect(Boolean(copy)).toBe(copyable);
      if (copy) expect(message.contains(copy)).toBe(false);
    });

    it.each(["/new", "/lang zh-CN"])(
      "%s stays ordinary chat until a real command owner is wired",
      (input) => {
        const onSend = vi.fn();
        render(<V2Composer onSend={onSend} />);
        const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
        fireEvent.change(textarea, { target: { value: input } });
        fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

        expect(onSend).toHaveBeenCalledWith(input, undefined);
        expect(dispatchRememberMock).not.toHaveBeenCalled();
      },
    );

    it("keeps attachments when a slash command cannot accept them", async () => {
      const onSend = vi.fn();
      render(
        <V2Composer
          onSend={onSend}
          prefillAttachments={[{
            fileId: "art_slash",
            filename: "evidence.pdf",
            size: 12,
            mime: "application/pdf",
          }]}
        />,
      );
      await screen.findByText("evidence.pdf");
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: "/remember hello" } });
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

      expect(screen.getByText("slash_attachments_unsupported")).toBeTruthy();
      expect(screen.getByText("evidence.pdf")).toBeTruthy();
      expect(textarea.value).toBe("/remember hello");
      expect(dispatchRememberMock).not.toHaveBeenCalled();
      expect(onSend).not.toHaveBeenCalled();
    });

    it("AttachmentRef[] chips: hidden file input present so chip pipeline works", () => {
      const { container } = render(<V2Composer onSend={() => {}} />);
      const fileInput = container.querySelector("input[type='file']");
      expect(fileInput).toBeTruthy();
      // accept attribute is passed in via UPLOAD_ACCEPT — non-empty.
      expect(fileInput?.getAttribute("accept")).toBeTruthy();
    });

    it("onSend: Enter on non-empty text fires onSend with trimmed value", () => {
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "  hello  " } });
      fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
      expect(onSend).toHaveBeenCalledWith("hello", undefined);
    });

    it("disabled: textarea is disabled when disabled=true (no steer mode)", () => {
      render(<V2Composer onSend={() => {}} disabled />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(ta.disabled).toBe(true);
    });

    it("steer mode keeps textarea enabled even with running=true", () => {
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={() => {}}
          running
          disabled
        />,
      );
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      // running+onSteer → inSteerMode → textarea NOT disabled (user can nudge).
      expect(ta.disabled).toBe(false);
    });

    it("steer mode keeps the attachment pipeline enabled despite parent disabled", async () => {
      const { container } = render(
        <V2Composer
          draftPersistenceScope={DRAFT_SCOPE}
          onSend={() => {}}
          onSteer={() => {}}
          running
          disabled
        />,
      );
      await act(async () => {});
      const input = container.querySelector<HTMLInputElement>("input[type='file']");
      await waitFor(() => expect(input?.disabled).toBe(false));
      const file = new File(["data"], "steer-note.txt", { type: "text/plain" });
      fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
      await waitFor(() => expect(screen.getByText("steer-note.txt")).toBeTruthy());
    });

    it("settles a registered reference journal only after the accepted draft CAS", async () => {
      const order: string[] = [];
      recoveryMocks.clear.mockImplementationOnce(async () => {
        order.push("draft");
        return true;
      });
      const onSteer = vi.fn(async (
        _text: string,
        _attachments?: AttachmentRef[],
        _revision?: string | null,
        context?: ComposerDraftSubmissionContext | null,
      ) => {
        if (!context) throw new Error("missing durable steer context");
        context.finalizeReferenceSubmission = async () => {
          order.push("journal");
        };
        return true;
      });
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={onSteer}
          onResolveDraftSubmissionProtocol={async () => "legacy_v0"}
          draftPersistenceScope={DRAFT_SCOPE}
          running
          disabled
        />,
      );
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.readOnly).toBe(false));
      fireEvent.change(textarea, { target: { value: "queued reference" } });
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

      await waitFor(() => expect(order).toEqual(["draft", "journal"]));
      expect(onSteer).toHaveBeenCalledOnce();
    });

    it("keeps next-turn model selection enabled while a steer-capable turn runs", () => {
      authSessionMock = {
        me: {
          capabilities: {
            planning: true,
          },
        },
      };
      render(
        <V2Composer
          onSend={() => {}}
          onSteer={() => {}}
          running
          disabled
        />,
      );
      expect((screen.getByTestId("mock-model-picker") as HTMLButtonElement).disabled).toBe(false);
    });
  });

  describe("readOnly (licensing §13.2 — over_grace / suspended)", () => {
    it.each(["readOnly", "recoveryLocked"])("fences an upload response after %s takes ownership", async (lock) => {
      let settleUpload!: (value: {
        file_id: string;
        filename: string;
        size: number;
        mime: string;
        url: string;
      }) => void;
      vi.mocked(uploadFile).mockImplementationOnce(() => new Promise((resolve) => {
        settleUpload = resolve;
      }));
      const { container, rerender } = render(<V2Composer draftPersistenceScope={DRAFT_SCOPE} onSend={() => {}} />);
      const input = container.querySelector<HTMLInputElement>("input[type='file']");
      await waitFor(() => expect(input?.disabled).toBe(false));
      const file = new File(["late"], "late.txt", { type: "text/plain" });
      fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
      await waitFor(() => {
        expect(container.querySelector(".v2-chip--uploading")).not.toBeNull();
      });

      rerender(<V2Composer draftPersistenceScope={DRAFT_SCOPE} onSend={() => {}} readOnly={lock === "readOnly"} recoveryLocked={lock === "recoveryLocked"} />);
      await act(async () => {
        settleUpload({
          file_id: "fid_late",
          filename: "late.txt",
          size: 4,
          mime: "text/plain",
          url: "/api/v1/files/fid_late/download",
        });
        await Promise.resolve();
      });

      expect(container.querySelector(".v2-chip--uploading")).not.toBeNull();
      expect(container.querySelector(".v2-chip--done")).toBeNull();
      expect(container.querySelector(".v2-chip--error")).toBeNull();
    });

    it("removes send, locks textarea, swaps placeholder, and shows the 只读模式 chip", () => {
      render(<V2Composer onSend={() => {}} readOnly />);
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(ta.disabled).toBe(false);
      expect(ta.readOnly).toBe(true);
      expect(ta.placeholder).toBe("readonly_placeholder"); // composer.readonly_placeholder key (mocked → raw key)
      expect(screen.queryByLabelText("发送")).toBeNull();
      expect(screen.getByTestId("ent-ro-chip")).not.toBeNull();
    });

    it("blocks the Enter submit path as well as the visible send button", () => {
      const onSend = vi.fn();
      render(<V2Composer onSend={onSend} readOnly prefillText="cannot send" />);
      fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", shiftKey: false });
      expect(onSend).not.toHaveBeenCalled();
    });

    it("refuses a failure-card replay while read-only (no re-send)", () => {
      const onSend = vi.fn();
      const control: { current: ComposerDraftControl | null } = { current: null };
      render(
        <V2Composer onSend={onSend} readOnly lastUserText="上次的问题" draftControlRef={control} />,
      );
      let accepted = true;
      act(() => { accepted = control.current!.requestReplay(); });
      expect(accepted).toBe(false);
      expect(onSend).not.toHaveBeenCalled();
      expect(screen.queryByLabelText("发送")).toBeNull();
    });

    it("keeps the responsive toolbar strictly read-only", () => {
      authSessionMock = {
        me: {
          capabilities: {
            planning: true,
          },
        },
      };

      const { container } = render(<V2Composer onSend={() => {}} readOnly />);
      const toolbar = container.querySelector(".composer-v2__toolbar");
      expect(toolbar?.querySelector('[data-testid="ent-ro-chip"]')).not.toBeNull();
      expect(toolbar?.querySelector("button")).toBeNull();
      expect(toolbar?.querySelector('[data-testid="mock-model-picker"]')).toBeNull();
    });
  });

  describe("prefillAttachments (file-management-v2 P2「在新会话中使用」)", () => {
    const REF = {
      fileId: "art_upload",
      filename: "Q1-经营复盘.pptx",
      size: 1_200_000,
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    };

    it("把已存在的 artifact 直接挂成 done 态 chip（不重传字节）", async () => {
      render(<V2Composer onSend={() => {}} prefillAttachments={[REF]} />);
      await waitFor(() => {
        expect(screen.getByText("Q1-经营复盘.pptx")).not.toBeNull();
      });
    });

    it("发送时带上该 artifact 的 fileId", async () => {
      const sent: unknown[] = [];
      render(
        <V2Composer
          onSend={(text, atts) => {
            sent.push({ text, atts });
          }}
          prefillAttachments={[REF]}
        />,
      );
      await waitFor(() => expect(screen.getByText("Q1-经营复盘.pptx")).not.toBeNull());
      const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
      fireEvent.change(ta, { target: { value: "基于这份文件分析" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(sent.length).toBe(1));
      const payload = sent[0] as { text: string; atts?: { fileId?: string }[] };
      expect(payload.text).toBe("基于这份文件分析");
      expect(payload.atts?.[0]?.fileId).toBe("art_upload");
    });

    it("连续合并不同引用并按 artifact 身份去重", async () => {
      const { rerender } = render(<V2Composer onSend={() => {}} prefillAttachments={null} />);
      // First simulate an attachment already uploading; try overwriting it with a second prefill.
      rerender(<V2Composer onSend={() => {}} prefillAttachments={[REF]} />);
      await waitFor(() => expect(screen.getByText("Q1-经营复盘.pptx")).not.toBeNull());
      // Results Workspace can reference another stable deliverable in the same draft.
      rerender(
        <V2Composer
          onSend={() => {}}
          prefillAttachments={[
            REF,
            REF,
            { ...REF, fileId: "art_other", filename: "另一个.pdf" },
          ]}
        />,
      );
      await waitFor(() => expect(screen.getByText("另一个.pdf")).not.toBeNull());
      expect(screen.getAllByText("Q1-经营复盘.pptx")).toHaveLength(1);
    });

    it("重复引用已有 chip 时也消费事件，避免之后复活", async () => {
      const consumed = vi.fn();
      const { rerender } = render(
        <V2Composer
          onSend={() => {}}
          prefillAttachments={[REF]}
          onPrefillAttachmentsConsumed={consumed}
        />,
      );
      await waitFor(() => expect(consumed).toHaveBeenCalledWith([REF]));
      consumed.mockClear();

      const duplicateEvent = { ...REF };
      rerender(
        <V2Composer
          onSend={() => {}}
          prefillAttachments={[duplicateEvent]}
          onPrefillAttachmentsConsumed={consumed}
        />,
      );

      await waitFor(() =>
        expect(consumed).toHaveBeenCalledWith([duplicateEvent]),
      );
      expect(screen.getAllByText(REF.filename)).toHaveLength(1);
    });

    it("发送时保留 result 引用类型", async () => {
      const onSend = vi.fn();
      render(
        <V2Composer
          onSend={onSend}
          prefillAttachments={[{ ...REF, referenceKind: "result" }]}
        />,
      );
      await waitFor(() => expect(screen.getByText("Q1-经营复盘.pptx")).not.toBeNull());
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "继续分析" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
      expect(onSend.mock.calls[0]?.[1]).toEqual([
        expect.objectContaining({
          fileId: "art_upload",
          referenceKind: "result",
        }),
      ]);
    });

    it("消费回执清空事件队列后，下一次引用不会把已发送成果挂回", async () => {
      const resultRef = { ...REF, referenceKind: "result" as const };
      const consumed = vi.fn();
      const { rerender } = render(
        <V2Composer
          onSend={() => true}
          prefillAttachments={[resultRef]}
          onPrefillAttachmentsConsumed={consumed}
        />,
      );
      await waitFor(() => expect(consumed).toHaveBeenCalledWith([resultRef]));

      rerender(
        <V2Composer
          onSend={() => true}
          prefillAttachments={[]}
          onPrefillAttachmentsConsumed={consumed}
        />,
      );
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "先分析" } });
      fireEvent.click(screen.getByLabelText("发送"));
      await waitFor(() => expect(screen.queryByText(REF.filename)).toBeNull());

      const next = {
        ...resultRef,
        fileId: "art_next",
        filename: "下一份.csv",
      };
      rerender(
        <V2Composer
          onSend={() => true}
          prefillAttachments={[next]}
          onPrefillAttachmentsConsumed={consumed}
        />,
      );
      await waitFor(() => expect(screen.getByText("下一份.csv")).not.toBeNull());
      expect(screen.queryByText(REF.filename)).toBeNull();
    });
  });
});

// ─────────────────────────────────────────────────────────────
// Planning three-state selector (Claude Design 8b6a92a0, "计划三态选择器", direction 1c)
//
// Five handoff guards, each mutation-tested (breaking the behavior must make the test fail).
// Guard ⑤, auto stays off the wire, lives in use-turn-runner.submission.test.ts; this file leaves it alone.
// Preferences use real localStorage (no mock), also exercising planning-mode-pref.
// ─────────────────────────────────────────────────────────────

const PLAN_PREF_KEY = "bowhead.planning_mode";

// This suite's jsdom provides only partial localStorage (no setItem; planning-mode-pref itself
// guards against this). Install an in-memory stub to **exercise the real preference layer**, rather than mocking it.
let planPrefStore: Record<string, string> = {};
let planPrefOriginal: Storage | undefined;

function seedPlanningMode(m: "auto" | "planning" | "off") {
  planPrefStore[PLAN_PREF_KEY] = m;
}

describe("Planning 三态选择器", () => {
  beforeEach(() => {
    authSessionMock = { me: { capabilities: { planning: true } } };
    planPrefStore = {};
    planPrefOriginal = window.localStorage;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => planPrefStore[k] ?? null,
        setItem: (k: string, v: string) => {
          planPrefStore[k] = v;
        },
        removeItem: (k: string) => {
          delete planPrefStore[k];
        },
      },
    });
  });
  afterEach(() => {
    if (planPrefOriginal !== undefined) {
      Object.defineProperty(window, "localStorage", { configurable: true, value: planPrefOriginal });
    }
  });

  it.each([true, false])("honors reference Plan availability=%s in the button and submit path", async (enabled) => {
    setPlanningModePref("planning");
    authSessionMock = {
      me: { id: "u_1", team_id: "team_1", capabilities: { planning: true, composer: { reference_plan: enabled } } },
    };
    const document = {
      schema_version: 1 as const,
      nodes: [
        { type: "reference" as const, node_id: "12345678-1234-4234-8234-123456789abc", resource: { kind: "agent" as const, source_kind: "team" as const, id: "osa_1", version: "arv_1", assignment_id: "asa_1", release_id: "arl_1" }, label: "Analyst", parameters: {} },
        { type: "text" as const, text: " Compare the two periods." },
      ],
    };
    const onSend = vi.fn((..._args: unknown[]) => false);
    const consumed = vi.fn();
    try {
      render(<V2Composer onSend={onSend} draftPersistenceScope={DRAFT_SCOPE} referenceInputEnabled
        prefillInputDocument={document} onPrefillInputDocumentConsumed={consumed} />);
      await waitFor(() => expect(consumed).toHaveBeenCalledWith("applied"));
      const send = screen.getByLabelText("发送") as HTMLButtonElement;
      expect(send.disabled).toBe(!enabled);
      fireEvent.click(send);
      if (enabled) {
        await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
        expect(onSend.mock.calls[0][3]).toMatchObject({ referenceInputDocument: document });
      } else {
        expect(onSend).not.toHaveBeenCalled();
      }
    } finally {
      act(() => setPlanningModePref("auto"));
    }
  });

  it("闸①: off 与 auto 的触发器渲染必须不同（三态 class 互不相同）", () => {
    const seen = new Map<string, string>();
    for (const m of ["auto", "planning", "off"] as const) {
      seedPlanningMode(m);
      const { container, unmount } = render(<V2Composer onSend={() => {}} />);
      const btn = container.querySelector(".composer-v2__planning");
      expect(btn, `${m} 态应有触发器`).toBeTruthy();
      seen.set(m, btn!.className);
      unmount();
    }
    // Mutation: set cls to "" for all three PLANNING_META states → this must fail.
    expect(new Set(seen.values()).size, JSON.stringify([...seen])).toBe(3);
    expect(seen.get("off")).toContain("is-muted");
    expect(seen.get("planning")).toContain("is-active");
  });

  it("闸②: 从 off 选「自动」落 auto，且触发器本身不改值", () => {
    seedPlanningMode("off");
    render(<V2Composer onSend={() => {}} />);
    const trigger = screen.getByRole("button", { name: "planning_mode_trigger" });

    fireEvent.click(trigger);
    // The old two-state implementation already called setMode("planning") here: clicking off directly forced a plan.
    expect(planPrefStore[PLAN_PREF_KEY]).toBe("off");

    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((i) => i.getAttribute("data-mode"))).toEqual(["auto", "planning", "off"]);
    fireEvent.click(items[0]);
    expect(planPrefStore[PLAN_PREF_KEY]).toBe("auto");
  });

  it.each(["auto", "planning", "off"] as const)(
    "闸③: compact 档 %s 态控件仍在工具栏（不再退进 ＋ 面板）",
    (m) => {
      seedPlanningMode(m);
      const { container } = render(<V2Composer onSend={() => {}} />);
      const root = container.querySelector("[data-composer-layout-v2]");
      expect(root?.getAttribute("data-layout-mode")).toBe("compact");
      // Mutation: restore the `planningMode === "planning"` condition → both auto/off cases must fail.
      expect(container.querySelector(".composer-v2__toolbar .composer-v2__planning")).toBeTruthy();
    },
  );

  // ★Observed on device 116 (1.0.97): the render guard covered compact but omitted ultra (<360),
  // while the planning row in the ＋ panel was removed in the same change → no entry anywhere in ultra.
  // jsdom's getBoundingClientRect always returns 0; `commitWidth` returns early, so mode stays at
  // useState's initial "compact"; the three guard ③ cases above **never reach the ultra branch**.
  // The fixture itself hid this gap. Stub the width to 300 here to actually exercise it.
  it.each(["auto", "planning", "off"] as const)(
    "闸③b: ultra 档 %s 态控件仍在工具栏（＋ 面板已无计划行，退不回去）",
    (m) => {
      seedPlanningMode(m);
      const originalRect = HTMLElement.prototype.getBoundingClientRect;
      HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
        return { x: 0, y: 0, width: 300, height: 44, top: 0, right: 300, bottom: 44, left: 0, toJSON: () => ({}) } as DOMRect;
      };
      try {
        const { container } = render(<V2Composer onSend={() => {}} />);
        const root = container.querySelector("[data-composer-layout-v2]");
        // Positive control: prove the ultra branch was reached first, or the next assertion could pass for the wrong reason.
        expect(root?.getAttribute("data-layout-mode")).toBe("ultra");
        // Mutation: revert the render guard to `layoutMode === "compact"` → all three cases fail.
        expect(container.querySelector(".composer-v2__toolbar .composer-v2__planning")).toBeTruthy();
      } finally {
        HTMLElement.prototype.getBoundingClientRect = originalRect;
      }
    },
  );

  it.each(["auto", "planning", "off"] as const)("闸④: %s 态 DOM 里零 aria-pressed", (m) => {
    seedPlanningMode(m);
    const { container } = render(<V2Composer onSend={() => {}} />);
    const btn = container.querySelector(".composer-v2__planning")!;
    expect(btn.hasAttribute("aria-pressed"), "aria-pressed 对三态是错的断言").toBe(false);
    expect(btn.getAttribute("aria-haspopup")).toBe("menu");
  });

  it("菜单项用 menuitemradio + aria-checked 表达当前态（恰好一个选中）", () => {
    seedPlanningMode("planning");
    render(<V2Composer onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "planning_mode_trigger" }));
    const checked = screen
      .getAllByRole("menuitemradio")
      .filter((i) => i.getAttribute("aria-checked") === "true");
    expect(checked).toHaveLength(1);
    expect(checked[0].getAttribute("data-mode")).toBe("planning");
  });

  it("键盘打开时焦点落在当前态，方向键在三态间移动，Esc 回到触发器", async () => {
    seedPlanningMode("planning");
    render(<V2Composer onSend={() => {}} />);
    const trigger = screen.getByRole("button", { name: "planning_mode_trigger" });
    fireEvent.click(trigger, { detail: 0 });
    const menu = screen.getByRole("menu");
    await waitFor(() => expect(document.activeElement?.getAttribute("data-mode")).toBe("planning"));

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement?.getAttribute("data-mode")).toBe("off");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement?.getAttribute("data-mode")).toBe("auto");

    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(planPrefStore[PLAN_PREF_KEY]).toBe("planning");
  });

  it("点菜单外关闭，不改值", async () => {
    seedPlanningMode("off");
    render(<V2Composer onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "planning_mode_trigger" }));
    expect(screen.getByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(planPrefStore[PLAN_PREF_KEY]).toBe("off");
  });

  it("打开时 composer 被锁定：菜单收起，焦点退回输入框", async () => {
    seedPlanningMode("auto");
    const { container, rerender } = render(<V2Composer onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "planning_mode_trigger" }), { detail: 0 });
    const menu = screen.getByRole("menu");
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));

    // When locked (recovering), the trigger remains but is disabled; restoring focus to it would fail.
    rerender(<V2Composer onSend={() => {}} recoveryLocked />);
    expect(
      (screen.getByRole("button", { name: "planning_mode_trigger" }) as HTMLButtonElement).disabled,
    ).toBe(true);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    const root = container.querySelector("[data-composer-surface]");
    await waitFor(() => {
      expect(document.activeElement).not.toBe(document.body);
      expect(root?.contains(document.activeElement)).toBe(true);
    });
  });
});

describe("persistent send-blocked reason and the file drop overlay", () => {

  async function startPendingUpload(ui: React.ReactElement) {
    vi.mocked(uploadFile).mockImplementationOnce(() => new Promise(() => {}));
    const view = render(ui);
    await waitFor(() => expect(view.container.querySelector<HTMLInputElement>("input[type='file']")?.disabled).toBe(false));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "按附件分析" } });
    const input = view.container.querySelector<HTMLInputElement>("input[type='file']");
    fireEvent.change(input as HTMLInputElement, { target: { files: [new File(["a"], "a.txt", { type: "text/plain" })] } });
    await waitFor(() => expect(view.container.querySelector(".v2-chip--uploading")).not.toBeNull());
    return view;
  }

  it("while an attachment uploads, writes the reason below the input, links it to send via aria-describedby and keeps the tooltip as Send", async () => {
    await startPendingUpload(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    const note = await screen.findByTestId("composer-send-blocked");
    expect(note.textContent).toBe("send_blocked_uploading");
    const send = screen.getByRole("button", { name: "发送" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    expect(send.getAttribute("aria-describedby")?.split(" ")).toContain(note.id);
    expect(send.getAttribute("title")).toBe("发送 (⏎)");
  });

  it("shows a parent reason (e.g. no space on home) when sending is blocked, but not while running with Stop", () => {
    const { rerender } = render(<V2Composer onSend={() => {}} disabled sendBlockedReason="还没有可用的空间" />);
    expect(screen.getByTestId("composer-send-blocked").textContent).toBe("还没有可用的空间");
    rerender(<V2Composer onSend={() => {}} disabled running onCancel={() => {}} sendBlockedReason="x" />);
    expect(screen.queryByTestId("composer-send-blocked")).toBeNull();
    rerender(<V2Composer onSend={() => {}} disabled running />);
    expect(screen.getByTestId("composer-send-blocked").textContent).toBe("send_blocked_running");
  });

  it("without a chat column the composer itself is the drop target and the overlay says whether the drop is accepted", async () => {
    const { container } = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    const wrap = container.querySelector(".v2-composer-wrap") as HTMLElement;
    const event = new Event("dragenter", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", {
      value: { types: ["Files"], items: [{ kind: "file", type: "text/csv" }], files: [], dropEffect: "none" },
    });
    act(() => { wrap.dispatchEvent(event); });
    const overlay = await screen.findByTestId("composer-file-drop");
    expect(wrap.contains(overlay)).toBe(true);
    expect(overlay.getAttribute("data-state")).toBe("accept");
    expect(screen.getByTestId("composer-drop-status").textContent).toContain("drop_accept_title");
  });

  it.each([
    ["an oversized file", "big.csv", "text/csv", 3 * 1024 * 1024 * 1024, "size"],
    ["an unsupported type", "deck.pptx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 100, "unsupported"],
  ] as const)("dropping %s takes the attachment error kind from local prevalidation (%s)", async (_label, name, type, size, kind) => {
    const { container } = render(<V2Composer onSend={() => {}} draftPersistenceScope={DRAFT_SCOPE} />);
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(false));
    const wrap = container.querySelector(".v2-composer-wrap") as HTMLElement;
    const file = new File(["x"], name, { type });
    Object.defineProperty(file, "size", { value: size });
    const drop = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(drop, "dataTransfer", {
      value: { types: ["Files"], items: [{ kind: "file", type }], files: [file], dropEffect: "none" },
    });
    act(() => { wrap.dispatchEvent(drop); });
    await waitFor(() => expect(container.querySelector(".v2-chip--error, .v2-chip--warning")).not.toBeNull());
    const chip = container.querySelector(".v2-chip--error, .v2-chip--warning")!;
    expect(chip.textContent).toContain(`err_${kind}`);
    expect(uploadFile).not.toHaveBeenCalledWith(file, expect.anything());
  });
});

describe("V2Composer · submit request", () => {
  it("sends the current input once per new request through the ordinary Send path", async () => {
    const onSend = vi.fn(async (_message: string) => false);
    const view = render(<V2Composer onSend={onSend} />);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { value: "Make the quarterly answer include YoY" } });
    view.rerender(<V2Composer onSend={onSend} submitRequest={0} />);
    expect(onSend).not.toHaveBeenCalled();
    view.rerender(<V2Composer onSend={onSend} submitRequest={1} />);
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toBe("Make the quarterly answer include YoY");
    view.rerender(<V2Composer onSend={onSend} submitRequest={1} />);
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});

describe("alignment card continue through the real composer draft", () => {
  it("stages @Coach with the continue message only into an empty composer, and sends only that exact message", async () => {
    const { composerAcceptsContinue, shouldSendStagedContinue } = await import("@/lib/coach-alignment-continue");
    const { inputDocumentDisplayText } = await import("@/lib/composer-input-document");
    const control = { current: null as ComposerDraftControl | null };
    const consumed = vi.fn();
    const coach = (text: string) => ({
      schema_version: 1 as const,
      nodes: [
        { type: "reference" as const, node_id: "12345678-1234-4234-8234-123456789abc", resource: { kind: "agent" as const, source_kind: "system" as const, id: "system:agent-coach", version: "sha256:" + "a".repeat(64) }, label: "Coach", parameters: {} },
        { type: "text" as const, text: ` ${text}` },
      ],
    });
    const props = { onSend: () => {}, referenceInputEnabled: true, draftControlRef: control, onPrefillInputDocumentConsumed: consumed };
    const view = render(<V2Composer {...props} />);
    await waitFor(() => expect(control.current).not.toBeNull());

    // Empty composer: the card may stage "@Coach …", and only that exact message is sent.
    expect(composerAcceptsContinue(control.current)).toBe(true);
    const staged = coach("I’ve marked the answers.");
    view.rerender(<V2Composer {...props} prefillInputDocument={staged} />);
    await waitFor(() => expect(consumed).toHaveBeenCalledWith("applied"));
    const pending = inputDocumentDisplayText(staged);
    expect(pending).toBe("@Coach I’ve marked the answers.");
    expect(shouldSendStagedContinue(pending, "applied", control.current)).toBe(true);
    expect(shouldSendStagedContinue("@Coach Something else", "applied", control.current)).toBe(false);
    // The same words without the Coach reference would go to the Lead: never sent as the continue message.
    expect(shouldSendStagedContinue(pending, "applied", { ...control.current!, readInput: () => ({ text: pending, hasAttachments: false, hasInputDocument: false }) })).toBe(false);
    view.unmount();

    // The Owner's own draft: the card isn't let in, and a staged message is discarded rather than sent.
    consumed.mockClear();
    const again = render(<V2Composer {...props} />);
    const input = again.getByRole("textbox") as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { value: "my own question" } });
    expect(composerAcceptsContinue(control.current)).toBe(false);
    again.rerender(<V2Composer {...props} prefillInputDocument={coach("Keep the original wording.")} />);
    await waitFor(() => expect(consumed).toHaveBeenCalledWith("discarded"));
    expect(input.value).toBe("my own question");
    expect(shouldSendStagedContinue("@Coach Keep the original wording.", "discarded", control.current)).toBe(false);
  });
});

