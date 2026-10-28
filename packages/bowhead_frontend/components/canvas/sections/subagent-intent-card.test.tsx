import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  SubagentDraftApiError,
  type SubagentDraftDetail,
  type ManualSubagentDraftDetail,
} from "@/lib/subagent-drafts-api";

const generateMock = vi.fn();
const generateCanvasMock = vi.fn();
const saveManualMock = vi.fn();
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/subagent-drafts-api")>();
  return {
    ...original,
    generateSubagentDraft: (...args: unknown[]) => generateMock(...args),
    generateCanvasDraftSuggestion: (...args: unknown[]) => generateCanvasMock(...args),
    saveManualSubagentDraft: (...args: unknown[]) => saveManualMock(...args),
  };
});
vi.mock("@/lib/locale", () => ({ getCurrentLocale: () => "zh-CN" }));

import { SubagentIntentCard } from "./subagent-intent-card";

const DRAFT = {
  schema_version: 1,
  kind: "team_subagent_draft_detail",
  draft_id: "sad_1",
  draft_status: "ready",
  authoring_state: "reviewing",
  source_mode: "intent",
  entry_mode: "conversational",
  purpose: "分析销售",
  goal_summary: "分析销售",
  definition: null,
  tools: [],
  skills: [],
  assumptions: [],
  warnings: [],
  clarification: null,
  validation: {
    status: "valid",
    issues: [],
    catalog_fingerprint: "sha256:test",
  },
  mutation_id: "11111111-1111-4111-8111-111111111111",
  version: 1,
  etag: 'W/"sad_1:1"',
  expires_at: "2026-08-01T00:00:00Z",
  result: null,
  actions: {
    clarify: false,
    edit: true,
    validate: true,
    create: true,
    discard: true,
    regenerate: false,
  },
} satisfies SubagentDraftDetail;

const CANVAS_DRAFT = {
  schema_version: 2, kind: "team_subagent_manual_draft_detail", draft_id: "sad_canvas",
  draft_status: "editing", source_mode: "intent", entry_mode: "canvas", workspace_id: "ws_a",
  purpose: "", definition: {}, validation: { status: "unknown", issues: [] }, warnings: [],
  mutation_id: "11111111-1111-4111-8111-111111111111", version: 1, etag: 'W/"sad_canvas:1"',
  expires_at: "2026-10-01T00:00:00Z", result: null,
  actions: { edit: true, validate: true, create: false, discard: true },
} satisfies ManualSubagentDraftDetail;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  generateMock.mockReset();
  generateCanvasMock.mockReset();
  saveManualMock.mockReset();
});

describe("SubagentIntentCard", () => {
  it("binds AI to the existing Canvas draft and lets manual editing continue while generation finishes", async () => {
    const saved = { ...CANVAS_DRAFT, purpose: "每周分析门店销售并生成趋势", version: 2, etag: 'W/"sad_canvas:2"' };
    saveManualMock.mockResolvedValue(saved);
    generateCanvasMock.mockImplementation(() => new Promise(() => {}));
    const onManual = vi.fn();
    const onCanvasDraftSaved = vi.fn();
    renderIntl(<SubagentIntentCard canvasDraft={CANVAS_DRAFT} onCanvasDraftSaved={onCanvasDraftSaved}
      onDraft={vi.fn()} onManual={onManual} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("目标描述"), {
      target: { value: "每周分析门店销售并生成趋势" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    await waitFor(() => expect(generateCanvasMock).toHaveBeenCalledTimes(1));
    expect(saveManualMock).toHaveBeenCalledWith(CANVAS_DRAFT, saved.purpose, expect.any(Object), false);
    expect(generateCanvasMock.mock.calls[0][0]).toEqual(saved);
    expect(onCanvasDraftSaved).toHaveBeenCalledWith(saved);
    expect(generateMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "停止等待，手动填写" }));
    expect(onManual).toHaveBeenCalledWith(saved.purpose);
  });
  it("is a modal dialog on its own and a non-modal region when embedded in the canvas", () => {
    const onClose = vi.fn();
    const { unmount } = renderIntl(
      <SubagentIntentCard onDraft={vi.fn()} onManual={vi.fn()} onClose={onClose} />,
    );
    const dialog = screen.getByRole("dialog", { name: "描述你想要的智能体" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(screen.getByLabelText("目标描述"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();

    renderIntl(
      <SubagentIntentCard embedded onDraft={vi.fn()} onManual={vi.fn()} onClose={onClose} />,
    );
    const region = screen.getByRole("region", { name: "描述你想要的智能体" });
    expect(region).not.toHaveAttribute("aria-modal");
    fireEvent.keyDown(screen.getByLabelText("目标描述"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("generates a server-owned draft from the user's goal", async () => {
    generateMock.mockResolvedValue(DRAFT);
    const onDraft = vi.fn();
    renderIntl(
      <SubagentIntentCard
        onDraft={onDraft}
        onManual={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("目标描述"), {
      target: { value: "每周分析门店销售异常并生成趋势图" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));

    await waitFor(() => expect(generateMock).toHaveBeenCalledTimes(1));
    expect(generateMock.mock.calls[0][0]).toBe(
      "每周分析门店销售异常并生成趋势图",
    );
    await waitFor(() => expect(onDraft).toHaveBeenCalledWith(DRAFT));
  });

  it("keeps manual configuration as an explicit secondary path", () => {
    const onManual = vi.fn();
    renderIntl(
      <SubagentIntentCard
        initialPurpose="分析销售"
        onDraft={vi.fn()}
        onManual={onManual}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "手动配置" }));
    expect(onManual).toHaveBeenCalledWith("分析销售");
    expect(generateMock).not.toHaveBeenCalled();
  });

  it("explains a model timeout, preserves the purpose, and starts a new retry attempt", async () => {
    generateMock
      .mockRejectedValueOnce(
        new SubagentDraftApiError(
          503,
          "subagent_draft_model_timeout",
          "Something went wrong",
        ),
      )
      .mockResolvedValueOnce(DRAFT);
    const onDraft = vi.fn();
    const onManual = vi.fn();
    renderIntl(
      <SubagentIntentCard
        initialPurpose="每周分析门店销售异常"
        onDraft={onDraft}
        onManual={onManual}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    expect(await screen.findByText(/AI 起草超过 2 分半还没完成/)).toBeTruthy();
    expect(
      (screen.getByLabelText("目标描述") as HTMLTextAreaElement).value,
    ).toBe("每周分析门店销售异常");

    fireEvent.click(screen.getByRole("button", { name: "手动配置" }));
    expect(onManual).toHaveBeenCalledWith("每周分析门店销售异常");

    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    await waitFor(() => expect(onDraft).toHaveBeenCalledWith(DRAFT));
    expect(generateMock).toHaveBeenCalledTimes(2);
    expect(generateMock.mock.calls[0][1].idempotencyKey).not.toBe(
      generateMock.mock.calls[1][1].idempotencyKey,
    );
  });

  it("keeps the request key after a local polling timeout", async () => {
    generateMock
      .mockRejectedValueOnce(
        new SubagentDraftApiError(
          408,
          "subagent_draft_generation_timeout",
          "Draft generation is taking longer than expected",
        ),
      )
      .mockResolvedValueOnce(DRAFT);
    renderIntl(
      <SubagentIntentCard
        initialPurpose="分析销售"
        onDraft={vi.fn()}
        onManual={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    expect(await screen.findByText(/AI 起草超过 2 分半还没完成/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    await waitFor(() => expect(generateMock).toHaveBeenCalledTimes(2));
    expect(generateMock.mock.calls[0][1].idempotencyKey).toBe(
      generateMock.mock.calls[1][1].idempotencyKey,
    );
  });

  it("explains a rejected generated credential and uses a new key for retry", async () => {
    generateMock
      .mockRejectedValueOnce(new SubagentDraftApiError(503, "subagent_draft_secret_pattern_rejected", "操作失败"))
      .mockResolvedValueOnce(DRAFT);
    renderIntl(
      <SubagentIntentCard
        initialPurpose="核对公开产品公告"
        onDraft={vi.fn()}
        onManual={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));
    expect(await screen.findByText(/生成结果疑似含有敏感凭据/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生成可审阅草稿" }));

    await waitFor(() => expect(generateMock).toHaveBeenCalledTimes(2));
    expect(generateMock.mock.calls[0][1].idempotencyKey).not.toBe(
      generateMock.mock.calls[1][1].idempotencyKey,
    );
  });
});
