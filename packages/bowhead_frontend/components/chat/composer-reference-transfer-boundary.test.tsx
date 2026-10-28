import { afterEach, expect, it, vi } from "vitest";
import * as transient from "@/lib/composer-transient-draft";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import fixtures from "../../../../contracts/chat-references/input-document-v1.json";
import { ComposerReferenceTransferBoundary } from "./composer-reference-transfer-boundary";
import type { ComposerNoticeItem } from "./composer-notices";
import {
  composerReferenceHandoffKey,
  type ComposerReferenceSnapshot,
} from "@/lib/composer-reference-transfer";
import { composerDraftScopeSignature } from "@/lib/composer-persistence-scope";
import {
  clearAllComposerTransientDrafts,
  retainComposerReferenceTransientDraft,
} from "@/lib/composer-transient-draft";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
const fixture = fixtures.valid.find((item) => item.name === "exact_agent_and_parameterized_skill")!;
const draft = {
  schemaVersion: 2,
  message: fixture.display_text!,
  input_document: fixture.normalized,
  attachments: [],
} as ComposerReferenceSnapshot;
const scope = {
  userId: "u1",
  teamId: "team1",
  workspaceId: "ws1",
  requestedSessionId: null,
  authSessionGeneration: "login1",
};
const renderComposer = (locked: boolean, notice: ComposerNoticeItem | null) => (
  <>
    {notice ? <p>{notice.text}</p> : null}
    <input aria-label="editor" disabled={locked} />
  </>
);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearAllComposerTransientDrafts();
  vi.restoreAllMocks();
});

it("fences the text consumer until both transfer readers have finished", async () => {
  render(
    <ComposerReferenceTransferBoundary scope={scope}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(true);
  await waitFor(() =>
    expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(false),
  );
});

it("previews a handoff and keeps its bytes and the editor locked", async () => {
  const key = await composerReferenceHandoffKey(scope);
  const raw = JSON.stringify(draft);
  sessionStorage.setItem(key, raw);
  render(
    <ComposerReferenceTransferBoundary scope={scope}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  await screen.findByText("/杜邦分析");
  expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByText("reference_remove_all")).toBeNull();
  expect(sessionStorage.getItem(key)).toBe(raw);
});

it("still previews a valid transient when the separate handoff is unreadable", async () => {
  retainComposerReferenceTransientDraft(composerDraftScopeSignature(scope), {
    ...draft,
    expectedRevision: "rev1",
  });
  const key = await composerReferenceHandoffKey(scope);
  sessionStorage.setItem(key, "{broken");
  render(
    <ComposerReferenceTransferBoundary scope={scope}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  await screen.findByText("/杜邦分析");
  expect(screen.getByText("draft_version_blocked")).toBeTruthy();
  expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(true);
  expect(sessionStorage.getItem(key)).toBe("{broken");
});

it("cannot publish an old scope's asynchronous preview into a new scope", async () => {
  sessionStorage.setItem(await composerReferenceHandoffKey(scope), JSON.stringify(draft));
  const { rerender } = render(
    <ComposerReferenceTransferBoundary scope={scope}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  rerender(
    <ComposerReferenceTransferBoundary scope={{ ...scope, workspaceId: "other" }}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(false),
  );
  expect(screen.queryByText("/杜邦分析")).toBeNull();
});

it("distinguishes unavailable browser storage from an unsupported version", async () => {
  vi.stubGlobal("sessionStorage", { getItem: () => { throw new Error("storage unavailable"); } });
  render(
    <ComposerReferenceTransferBoundary scope={scope}>
      {renderComposer}
    </ComposerReferenceTransferBoundary>,
  );
  await screen.findByText("reference_transfer_read_failed");
  expect(screen.queryByText("draft_version_blocked")).toBeNull();
  expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(true);
});


it("does not accept a valid handoff while the other transfer reader is fenced", async () => {
  const key = await composerReferenceHandoffKey(scope);
  const raw = JSON.stringify(draft);
  sessionStorage.setItem(key, raw);
  vi.spyOn(transient, "readComposerInputTransientDraft").mockImplementation(() => { throw new DOMException("unknown transient", "VersionError"); });
  render(<ComposerReferenceTransferBoundary scope={scope} consumeReferenceTransient allowHandoffAcceptance>{renderComposer}</ComposerReferenceTransferBoundary>);
  await screen.findByText("/杜邦分析");
  expect((screen.getByLabelText("editor") as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "reference_accept_handoff" })).toBeNull();
  expect(sessionStorage.getItem(key)).toBe(raw);
});
