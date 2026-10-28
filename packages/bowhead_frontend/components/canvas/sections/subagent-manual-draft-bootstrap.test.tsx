import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { ManualSubagentDraftDetail } from "@/lib/subagent-drafts-api";

const createDraft = vi.fn();
vi.mock("@/lib/subagent-drafts-api", () => ({
  createManualSubagentDraft: (...args: unknown[]) => createDraft(...args),
}));

import { SubagentManualDraftBootstrap } from "./subagent-manual-draft-bootstrap";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("reserves one server draft before opening manual fields", async () => {
  const onDraft = vi.fn();
  const draft = { draft_id: "sad_1", purpose: "整理文字" } as ManualSubagentDraftDetail;
  createDraft.mockResolvedValue(draft);
  renderIntl(<SubagentManualDraftBootstrap purposeHint=" 整理文字 " onDraft={onDraft} />);

  await waitFor(() => expect(onDraft).toHaveBeenCalledWith(draft));
  expect(createDraft).toHaveBeenCalledOnce();
  expect(createDraft.mock.calls[0][0]).toBe("整理文字");
  expect(createDraft.mock.calls[0][1]).toEqual(expect.any(String));
});

it("retries an uncertain create with the same request identity", async () => {
  createDraft.mockRejectedValueOnce(new Error("offline"));
  createDraft.mockResolvedValueOnce({ draft_id: "sad_1" });
  const onDraft = vi.fn();
  renderIntl(<SubagentManualDraftBootstrap purposeHint={null} onDraft={onDraft} />);

  fireEvent.click(await screen.findByRole("button", { name: "重试创建草稿" }));
  await waitFor(() => expect(onDraft).toHaveBeenCalledOnce());
  expect(createDraft).toHaveBeenCalledTimes(2);
  expect(createDraft.mock.calls[1][1]).toBe(createDraft.mock.calls[0][1]);
});
