import { cleanup, screen, waitFor } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { uploadSkill, type SkillDetailDTO } from "@/lib/api";
import { SkillUploadModal } from "./skill-upload-modal";

vi.mock("@/lib/api", () => ({ uploadSkill: vi.fn() }));

const BASE = {
  name: "onc-qbr",
  description: "",
  version: null,
  source_type: "user_upload",
  enabled: true,
  size_bytes: 22_400,
  created_at: null,
  body_md: "",
  allowed_tools: [],
  scripts: [],
  source_turn_id: null,
  trigger_examples: [],
  references: [],
  evals_count: 0,
  has_evals: false,
} as unknown as SkillDetailDTO;

const PENDING: SkillDetailDTO = {
  ...BASE,
  import_status: "pending",
  import_report: {
    grade: "red",
    red_count: 1,
    findings: [
      {
        id: "f_1",
        code: "tool_ref_unresolved",
        severity: "red",
        path: "SKILL.md",
        line: null,
        subject: "data_fetch_skill",
        resolved_against: "tool_registry+skill_catalog",
        message_zh: "声明要用的 data_fetch_skill 在这里不存在",
        message_en: "Declared tool does not exist here",
      },
    ],
  },
};

async function upload(props: Partial<React.ComponentProps<typeof SkillUploadModal>> = {}) {
  const onClose = vi.fn();
  const onUploaded = vi.fn();
  const onEdit = vi.fn();
  renderIntl(<SkillUploadModal open onClose={onClose} onUploaded={onUploaded} onEdit={onEdit} {...props} />);
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File(["x"], "s.zip", { type: "application/zip" })] } });
  fireEvent.click(screen.getByRole("button", { name: "上传" }));
  return { onClose, onUploaded, onEdit };
}

afterEach(cleanup);

describe("上传回执（skill-import 批3）", () => {
  it("清点判红：弹窗停在回执态，列出阻断项，不自动关闭", async () => {
    vi.mocked(uploadSkill).mockResolvedValue(PENDING);
    const { onClose, onUploaded } = await upload();
    await waitFor(() => expect(screen.getByText("已上传，但暂不生效")).toBeTruthy());
    // Still refresh the list: it already belongs to the user, merely not yet effective
    expect(onUploaded).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(/声明要用的 data_fetch_skill/)).toBeTruthy();
    expect(screen.getByText(/1 条阻断项/)).toBeTruthy();
  });

  it("回执态的「去编辑」把这条 skill 交给父层开抽屉", async () => {
    vi.mocked(uploadSkill).mockResolvedValue(PENDING);
    const { onEdit, onClose } = await upload();
    await waitFor(() => expect(screen.getByRole("button", { name: "去编辑" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "去编辑" }));
    expect(onEdit).toHaveBeenCalledWith(PENDING);
    expect(onClose).toHaveBeenCalled();
  });

  it("只有提示（不阻断）时照常关闭，不打断顺利路径", async () => {
    vi.mocked(uploadSkill).mockResolvedValue({ ...BASE, import_status: null });
    const { onClose } = await upload();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(screen.queryByText("已上传，但暂不生效")).toBeNull();
  });
});

describe("回执态的复位（review：此前没有测试覆盖）", () => {
  it("点「完成」即清掉回执——否则下次开弹窗还挂着上一次的结果", async () => {
    vi.mocked(uploadSkill).mockResolvedValue(PENDING);
    const onClose = vi.fn();
    // Parent controls open; keep this mounted: if close()'s reset() failed to clear pending, the receipt would stay on screen
    renderIntl(<SkillUploadModal open onClose={onClose} onUploaded={vi.fn()} onEdit={vi.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["x"], "s.zip", { type: "application/zip" })] } });
    fireEvent.click(screen.getByRole("button", { name: "上传" }));
    await waitFor(() => expect(screen.getByText("已上传，但暂不生效")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    expect(onClose).toHaveBeenCalled();
    expect(screen.queryByText("已上传，但暂不生效")).toBeNull();
    expect(screen.getByRole("button", { name: "上传" })).toBeTruthy();
  });
});

describe("关闭", () => {
  it("Esc 关闭弹窗", () => {
    const onClose = vi.fn();
    renderIntl(<SkillUploadModal open onClose={onClose} onUploaded={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole("dialog", { name: "上传技能" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("上传进行中 Esc 与点遮罩都不关闭", async () => {
    vi.mocked(uploadSkill).mockReturnValue(new Promise(() => undefined));
    const { onClose } = await upload();
    await waitFor(() => expect(screen.getByText("上传中…")).toBeTruthy());
    const dialog = screen.getByRole("dialog", { name: "上传技能" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.click(dialog.parentElement as HTMLElement);
    expect(onClose).not.toHaveBeenCalled();
  });
});
