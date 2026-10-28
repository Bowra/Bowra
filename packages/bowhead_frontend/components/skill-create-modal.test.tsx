import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { SkillCreateModal } from "./skill-create-modal";
const { generateSkill } = vi.hoisted(() => ({ generateSkill: vi.fn() }));
vi.mock("@/lib/api", () => ({ generateSkill }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
it("新建从输入生成完整清单，保存失败保留输入，成功后打开创建结果", async () => {
  const created = { name: "new-skill" };
  generateSkill.mockRejectedValueOnce(new Error("failed")).mockResolvedValueOnce(created);
  const onCreated = vi.fn();
  const onClose = vi.fn();
  renderIntl(<SkillCreateModal onClose={onClose} onCreated={onCreated} presentError={() => ({ message: "保存失败" })} />);
  fireEvent.change(screen.getByLabelText(/标识名称/), {
    target: { value: "new-skill" },
  });
  fireEvent.change(screen.getByLabelText("什么时候使用这个技能"), {
    target: { value: "条件: 门店比较" },
  });
  fireEvent.change(screen.getByLabelText("执行步骤与输出要求"), {
    target: { value: "## 步骤\n比较门店" },
  });
  fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
  await screen.findByText("保存失败");
  expect(onClose).not.toHaveBeenCalled();
  expect((screen.getByLabelText(/标识名称/) as HTMLInputElement).value).toBe("new-skill");
  fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
  expect(generateSkill).toHaveBeenLastCalledWith('---\nname: new-skill\ndescription: "条件: 门店比较"\n---\n## 步骤\n比较门店');
  expect(onClose).toHaveBeenCalledTimes(1);
});
it("有未保存输入时 Esc 先问是否放弃；确认框里的 Esc 只关确认框", async () => {
  const onClose = vi.fn();
  renderIntl(<SkillCreateModal onClose={onClose} onCreated={vi.fn()} presentError={() => ({ message: "" })} />);
  fireEvent.change(screen.getByLabelText(/标识名称/), { target: { value: "draft" } });
  fireEvent.keyDown(screen.getByRole("dialog", { name: "新建技能" }), { key: "Escape" });
  fireEvent.keyDown(await screen.findByRole("dialog", { name: "放弃未保存的修改？" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "放弃未保存的修改？" })).toBeNull());
  // The outer dialog is aria-hidden while confirmation is open; after closing, it must be discoverable by role again.
  expect(screen.getByRole("dialog", { name: "新建技能" })).toBeTruthy();
  expect(onClose).not.toHaveBeenCalled();
});
