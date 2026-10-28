/**
 * Original Skill editor: domain-specific writes, exact baselines and retained local input.
 * Coach authoring shares the package writer; these tests exercise the original manual editor.
 */

import { createRef, useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";

import { confirmCanvasNavigation, hasCanvasUnsavedChanges, registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { SkillDrawer, splitSkillManifest, type SkillEditorPort, type SkillDrawerTarget } from "./skill-drawer";

const {
  changeSkillFile,
  getTeamSkill,
  getTeamSkillFile,
  putTeamSkillFile,
  putTeamSkillBody,
  fetchSkillDetail,
  fetchSkillFile,
  putSkillFile,
  putSkillBody,
} = vi.hoisted(() => ({
  changeSkillFile: vi.fn(),
  getTeamSkill: vi.fn(),
  getTeamSkillFile: vi.fn(),
  putTeamSkillFile: vi.fn(),
  putTeamSkillBody: vi.fn(),
  fetchSkillDetail: vi.fn(),
  fetchSkillFile: vi.fn(),
  putSkillFile: vi.fn(),
  putSkillBody: vi.fn(),
}));

vi.mock("@/lib/workflow-solidify-api", () => ({
  getTeamSkill,
  getTeamSkillFile,
  putTeamSkillFile,
  putTeamSkillBody,
  copyTeamSkillToPersonal: vi.fn(),
  promoteSkill: vi.fn(),
  withdrawTeamSkill: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  changeSkillFile,
  fetchSkillDetail,
  fetchSkillFile,
  putSkillFile,
  putSkillBody,
}));
vi.mock("@/lib/use-app-router", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-app-router")>(),
  useAppRouter: () => ({ push: vi.fn() }),
}));

const TEAM_ROW = {
  id: "oskill_1",
  team_id: "team_a",
  name: "channel-gm-recipe",
  display_name: "渠道毛利核对",
  description: "渠道毛利核对",
  visibility: "team_wide",
  status: "enabled",
  verdict: "pass",
  size_bytes: 53849,
  created_by: "u_b",
  created_by_name: "同事",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  approval: "approved",
};

const teamTarget = (canEdit: boolean): SkillDrawerTarget => ({
  kind: "team",
  skill: TEAM_ROW as never,
  canManage: canEdit,
  canEdit,
});

const personalTarget = (sourceType: string): SkillDrawerTarget => ({
  kind: "skill",
  skill: {
    name: "dupont",
    content_sha256: REVISION,
    title: "杜邦分析",
    source_type: sourceType,
    enabled: true,
    size_bytes: 4200,
  } as never,
});

const REVISION = "a".repeat(64);

const RAW_MD = "---\nname: channel-gm-recipe\ndescription: 渠道毛利核对\n---\n# 渠道毛利\n步骤...";

function teamDetail() {
  return {
    ...TEAM_ROW,
    content_sha256: REVISION,
    body_md: "# 渠道毛利\n步骤...",
    body_raw: RAW_MD,
    references: [{ name: "metrics.md", size: 29960 }],
    scripts: [],
    evals_count: 0,
    has_evals: false,
  };
}

function personalDetail() {
  return {
    name: "dupont",
    content_sha256: REVISION,
    title: "杜邦分析",
    source_type: "user_upload",
    enabled: true,
    size_bytes: 4200,
    body_md: "# 杜邦\n原正文",
    body_raw: "---\nname: dupont\ndescription: 杜邦分析\n---\n# 杜邦\n原正文",
    allowed_tools: [],
    scripts: [],
    trigger_examples: [],
    references: [],
    evals_count: 0,
    has_evals: false,
    source_turn_id: null,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("SkillDrawer · 编辑", () => {
  it("个人 Skill 的来源只在元数据行展示一次", async () => {
    fetchSkillDetail.mockResolvedValue(personalDetail());

    renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);

    await screen.findByText(/原正文/);
    expect(screen.getAllByText("上传")).toHaveLength(1);
    expect(document.querySelector(".drawer-badges")).toBeNull();
  });

  it("团队文件视图：可编辑时保存打团队写端点", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    getTeamSkillFile.mockResolvedValue({
      path: "references/metrics.md",
      content: "口径 v1",
      size: 8,
      content_sha256: REVISION,
      mime: "text/markdown",
    });
    putTeamSkillFile.mockResolvedValue({
      path: "references/metrics.md",
      content: "口径 v2",
      size: 8,
      content_sha256: REVISION,
      mime: "text/markdown",
    });
    const onChanged = vi.fn();

    renderIntl(<SkillDrawer target={teamTarget(true)} onClose={() => {}} onTeamStateChanged={onChanged} />);

    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.click(await screen.findByText("metrics.md"));
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));

    const box = screen.getByRole("textbox");
    expect((box as HTMLTextAreaElement).value).toBe("口径 v1"); // Draft starts from current content.
    fireEvent.change(box, { target: { value: "口径 v2" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(putTeamSkillFile).toHaveBeenCalledWith("oskill_1", "references", "metrics.md", "口径 v2", REVISION));
    expect(putSkillFile).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("口径 v2")).toBeTruthy());
    expect(onChanged).toHaveBeenCalled(); // Refetch list (size/description may change).
  });

  it("团队：不可编辑时不出编辑入口（治理门在服务端，入口跟着同一规则显隐）", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    getTeamSkillFile.mockResolvedValue({
      path: "references/metrics.md",
      content: "口径 v1",
      size: 8,
      content_sha256: REVISION,
      mime: "text/markdown",
    });

    renderIntl(<SkillDrawer target={teamTarget(false)} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.click(await screen.findByText("metrics.md"));
    await screen.findByText("口径 v1");
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  });

  it("个人说明 tab：草稿是整份 SKILL.md，保存打个人正文端点，正文当场刷新", async () => {
    const before = personalDetail();
    const after = {
      ...before,
      body_md: "# 杜邦\n新正文",
      body_raw: "---\nname: dupont\ndescription: 杜邦分析\n---\n# 杜邦\n新正文",
    };
    fetchSkillDetail.mockResolvedValueOnce(before).mockResolvedValue(after);
    putSkillBody.mockResolvedValue({
      path: "SKILL.md",
      content: after.body_raw,
      size: 60,
      content_sha256: REVISION,
      mime: "text/markdown",
    });

    renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    // ★ Draft must include frontmatter: saving display-only body_md is necessarily rejected by server.
    // (400 E_SKILL_NAME_INVALID reproduced on real 116).
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(before.body_raw);

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: after.body_raw },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(putSkillBody).toHaveBeenCalledWith("dupont", after.body_raw, REVISION));
    expect(putTeamSkillBody).not.toHaveBeenCalled();
    // Refetch details after saving: display frontmatter-stripped body, not raw saved text.
    await waitFor(() => expect(screen.getByText(/新正文/)).toBeTruthy());
    expect(screen.queryByText(/description: 杜邦分析/)).toBeNull();
  });

  it("内置 skill 不出编辑入口（盘在 git catalog，改了下次升级就没了）", async () => {
    fetchSkillDetail.mockResolvedValue({
      ...personalDetail(),
      source_type: "builtin",
    });

    renderIntl(<SkillDrawer target={personalTarget("builtin")} onClose={() => {}} />);

    await screen.findByText(/原正文/);
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  });

  it("保存失败：留在编辑态、草稿不丢、错误可见", async () => {
    fetchSkillDetail.mockResolvedValue(personalDetail());
    putSkillBody.mockRejectedValue(new Error("boom"));

    renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "写坏的正文" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(putSkillBody).toHaveBeenCalled());
    const box = await screen.findByRole("textbox");
    expect((box as HTMLTextAreaElement).value).toBe("写坏的正文");
  });
});

/** In-app confirmation: find dialog by title, click its button. */
async function answerConfirm(title: RegExp | string, button: string) {
  const dialog = await screen.findByRole("dialog", { name: title });
  fireEvent.click(within(dialog).getByRole("button", { name: button }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: title })).toBeNull());
}

describe("SkillDrawer · 文件编辑回归", () => {
  it("未保存文件返回时可留在原草稿；确认离开后说明只编辑 SKILL.md", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    getTeamSkillFile.mockResolvedValue({
      path: "references/metrics.md",
      content: "original reference",
      size: 18,
      content_sha256: REVISION,
      mime: "text/markdown",
    });
    const onClose = vi.fn();
    renderIntl(<SkillDrawer target={teamTarget(true)} onClose={onClose} />);
    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.click(await screen.findByText("metrics.md"));
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "reference draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: /返回文件/ }));
    await answerConfirm("放弃未保存的修改？", "继续编辑");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("reference draft");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await answerConfirm("放弃未保存的修改？", "继续编辑");
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /返回文件/ }));
    await answerConfirm("放弃未保存的修改？", "放弃修改");
    fireEvent.click(screen.getByRole("tab", { name: "说明" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(RAW_MD);
    expect(putTeamSkillBody).not.toHaveBeenCalled();
  });

  it("保存嵌套脚本保留完整相对路径", async () => {
    fetchSkillDetail.mockResolvedValue({
      ...personalDetail(),
      scripts: [{ name: "nested/run.py", size: 4 }],
    });
    fetchSkillFile.mockResolvedValue({
      path: "scripts/nested/run.py",
      content: "x=1",
      size: 4,
      content_sha256: REVISION,
      mime: "text/plain",
    });
    putSkillFile.mockResolvedValue({
      path: "scripts/nested/run.py",
      content: "x=2",
      size: 4,
      content_sha256: REVISION,
      mime: "text/plain",
    });
    renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.click(await screen.findByText("nested/run.py"));
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "x=2" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putSkillFile).toHaveBeenCalledWith("dupont", "scripts", "nested/run.py", "x=2", REVISION));
  });

  it("空 bundle 可以新建 reference，重命名和删除使用明确的文件操作", async () => {
    fetchSkillDetail.mockResolvedValue({
      ...personalDetail(),
      authoring_enabled: true,
    });
    changeSkillFile.mockImplementation(async (_name, input) => ({
      path: `references/${input.target ?? input.filename}`,
      content: input.content ?? "{}",
      size: 2,
      content_sha256: REVISION,
      mime: "application/json",
    }));
    renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.change(screen.getByLabelText("相对文件名"), {
      target: { value: "nested/rules.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "新建文件" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(changeSkillFile).toHaveBeenCalledWith("dupont", {
        operation: "create",
        kind: "references",
        filename: "nested/rules.json",
        content: "{}",
        expected_content_sha256: REVISION,
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "重命名" }));
    fireEvent.change(screen.getByLabelText("相对文件名"), {
      target: { value: "other.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    await waitFor(() =>
      expect(changeSkillFile).toHaveBeenLastCalledWith(
        "dupont",
        expect.objectContaining({
          operation: "rename",
          filename: "nested/rules.json",
          target: "other.json",
        }),
      ),
    );
    await screen.findByText("references/other.json");
    fireEvent.click(screen.getByRole("button", { name: "删除文件" }));
    await answerConfirm("删除 references/other.json？", "删除文件");
    await waitFor(() =>
      expect(changeSkillFile).toHaveBeenLastCalledWith(
        "dupont",
        expect.objectContaining({
          operation: "delete",
          filename: "other.json",
        }),
      ),
    );
  });

  it("团队：收件箱预填交接 → 面板逐行对照，勾选后应用进编辑器草稿，保存打团队正文端点", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    putTeamSkillBody.mockResolvedValue({});
    sessionStorage.setItem("bowhead:feedback-prefill", "stub");
    const base = teamDetail().body_raw as string;
    const edited = base + "\n排名图默认横向条形图。";
    const target: SkillDrawerTarget = {
      ...(teamTarget(true) as Extract<SkillDrawerTarget, { kind: "team" }>),
      prefill: {
        candidate_id: "fbc_sk", target_kind: "team_skill", target_ref: "oskill_1",
        prefill: { kind: "team_skill_body", edited_content: edited, rationale: "" },
        base: { body_sha256: "x" }, banner: { candidate_id: "fbc_sk", proposer_id: "u_wang", evidence_count: 2 },
      },
    };
    renderIntl(<SkillDrawer target={target} onClose={() => {}} onTeamStateChanged={vi.fn()} />);
    await screen.findByTestId("feedback-prefill-panel");
    expect(screen.getByText("此内容由成员反馈起草，来源不可信，保存前逐行审阅")).toBeTruthy();
    expect(screen.getByTestId("feedback-prefill-diff").querySelector('[data-kind="add"]')?.textContent).toContain("横向条形图");
    fireEvent.click(screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
    fireEvent.click(screen.getByRole("button", { name: "应用到编辑器" }));
    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toBe(edited);
    expect(sessionStorage.getItem("bowhead:feedback-prefill")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putTeamSkillBody).toHaveBeenCalledWith("oskill_1", edited, REVISION));
  });

  it("团队：不可编辑时预填面板不出现（治理门在服务端，入口跟同一规则）", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    const target: SkillDrawerTarget = {
      ...(teamTarget(false) as Extract<SkillDrawerTarget, { kind: "team" }>),
      prefill: { candidate_id: "c", target_kind: "team_skill", target_ref: "oskill_1", prefill: { kind: "team_skill_body", edited_content: "x", rationale: "" }, base: {}, banner: { candidate_id: "c", proposer_id: "u", evidence_count: 1 } },
    };
    renderIntl(<SkillDrawer target={target} onClose={() => {}} onTeamStateChanged={vi.fn()} />);
    await screen.findByRole("tab", { name: /文件/ });
    expect(screen.queryByTestId("feedback-prefill-panel")).toBeNull();
  });
});

describe("SkillDrawer verified badge glyph", () => {
  it("renders ✓ before the verified badge label", async () => {
    getTeamSkill.mockResolvedValue(teamDetail());
    const { container } = renderIntl(<SkillDrawer target={teamTarget(false)} onClose={() => {}} />);
    const message = catalogText("zh-CN", "skills_page.badge_verified");
    await screen.findAllByText(message, { exact: false });
    const badge = container.querySelector(".sbadge.verified") as HTMLElement;
    expectRefreshedGlyph(badge, "✓", "start", message);
  });
});


it("sends the exact file baseline and retains local text after a concurrent change", async () => {
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), references: [{ name: "notes.md", size: 3 }] });
  const fileRevision = "b".repeat(64);
  fetchSkillFile.mockResolvedValue({ path: "references/notes.md", content: "Original notes", size: 14, mime: "text/markdown", content_sha256: fileRevision });
  putSkillFile.mockRejectedValue(new ApiError(409, "Conflict", { code: "skill_write_conflict" }));
  renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
  fireEvent.click(await screen.findByText("notes.md"));
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep these local notes" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(putSkillFile).toHaveBeenCalledWith("dupont", "references", "notes.md", "Keep these local notes", fileRevision));
  await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(false));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Keep these local notes");
  expect(putSkillFile).toHaveBeenCalledTimes(1);
  expect(fetchSkillFile).toHaveBeenCalledTimes(1);
});

it("preserves edits instead of submitting without an original content baseline", async () => {
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), content_sha256: undefined });
  renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep local instructions" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByText("请保留尚未保存的内容，重新打开此技能读取当前版本后再保存。")).toBeTruthy();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Keep local instructions");
  expect(putSkillBody).not.toHaveBeenCalled();
});

it("keeps the original edit buffer when acknowledgement readback fails", async () => {
  const initial = personalDetail();
  fetchSkillDetail.mockResolvedValueOnce(initial).mockRejectedValue(new Error("Disconnected"));
  putSkillBody.mockResolvedValue({ path: "SKILL.md", content: "Saved instructions", size: 18, mime: "text/markdown", content_sha256: "b".repeat(64) });
  renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Saved instructions" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(fetchSkillDetail).toHaveBeenCalledTimes(2));
  await waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(false));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Saved instructions");
  expect(putSkillBody).toHaveBeenCalledTimes(1);
  expect(document.querySelector(".fileview-saved")).toBeNull();
});


it("does not apply an old save to a replacement Skill editor", async () => {
  const first = { ...personalTarget("user_upload"), skill: { ...personalTarget("user_upload").skill, id: "sk_first" } } as SkillDrawerTarget;
  const replacement = { ...first, skill: { ...first.skill, id: "sk_replacement" } } as SkillDrawerTarget;
  let select!: (target: SkillDrawerTarget) => void;
  const changed = vi.fn();
  function Workbench() {
    const [target, setTarget] = useState(first);
    select = setTarget;
    return <SkillDrawer target={target} onClose={() => {}} onTeamStateChanged={changed} />;
  }
  let finish!: (value: unknown) => void;
  putSkillBody.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  fetchSkillDetail.mockResolvedValueOnce(personalDetail()).mockResolvedValue({ ...personalDetail(), body_raw: "Replacement instructions", body_md: "Replacement instructions", content_sha256: "c".repeat(64) });
  renderIntl(<Workbench />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Original save" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  act(() => select(replacement));
  await screen.findByText("Replacement instructions");
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Newer local input" } });
  await act(async () => finish({ path: "SKILL.md", content: "Original save", size: 13, mime: "text/markdown", content_sha256: "b".repeat(64) }));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Newer local input");
  expect(fetchSkillDetail).toHaveBeenCalledTimes(2);
  expect(changed).not.toHaveBeenCalled();
});

it("does not reset edits when the same Skill receives updated labels", async () => {
  let relabel!: () => void;
  function Workbench() {
    const [target, setTarget] = useState(personalTarget("user_upload"));
    relabel = () => setTarget({ ...target, skill: { ...target.skill, title: "Updated title" } } as SkillDrawerTarget);
    return <SkillDrawer target={target} onClose={() => {}} />;
  }
  fetchSkillDetail.mockResolvedValue(personalDetail());
  renderIntl(<Workbench />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep this edit" } });
  act(() => relabel());
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Keep this edit");
  expect(fetchSkillDetail).toHaveBeenCalledTimes(1);
});

it("keeps input that changes while an older discard decision is pending", async () => {
  fetchSkillDetail.mockResolvedValue(personalDetail());
  const close = vi.fn();
  renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={close} />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "First draft" } });
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  // A queued input update belongs to the original controller, after the leave request.
  fireEvent.change(input, { target: { value: "Newer draft" } });
  await answerConfirm("放弃未保存的修改？", "放弃修改");
  expect(close).not.toHaveBeenCalled();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Newer draft");
});

it("keeps an empty new file until its creation is explicitly discarded", async () => {
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), authoring_enabled: true });
  renderIntl(<SkillDrawer target={personalTarget("user_upload")} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
  fireEvent.change(screen.getByLabelText("相对文件名"), { target: { value: "planned.md" } });
  fireEvent.click(screen.getByRole("button", { name: "新建文件" }));
  fireEvent.click(screen.getByRole("button", { name: /返回文件/ }));
  await answerConfirm("放弃未保存的修改？", "继续编辑");
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  expect(screen.getByText("references/planned.md")).toBeTruthy();
  expect(changeSkillFile).not.toHaveBeenCalled();
});

it("retains independent hosted Skill buffers and routes close through the original editor", async () => {
  fetchSkillDetail.mockResolvedValue(personalDetail());
  const firstPort = createRef<SkillEditorPort>();
  const close = vi.fn();
  let select!: (value: boolean) => void;
  function Workbench() {
    const [first, setFirst] = useState(true);
    select = setFirst;
    return <>
      <div hidden={!first}><SkillDrawer hosted controllerRef={firstPort} target={personalTarget("user_upload")} onClose={close} /></div>
      <div hidden={first}><SkillDrawer hosted target={{ ...personalTarget("user_upload"), skill: { ...personalTarget("user_upload").skill, name: "second" } } as SkillDrawerTarget} onClose={() => {}} /></div>
    </>;
  }
  renderIntl(<Workbench />);
  // Hosted in Canvas the Skill body is directly editable (D10): no separate Edit step.
  await screen.findByRole("textbox");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "First retained text" } });
  expect(hasCanvasUnsavedChanges()).toBe(true);
  expect(screen.queryByRole("dialog")).toBeNull();
  act(() => select(false));
  // Hosted in Canvas the Skill body is directly editable (D10): no separate Edit step.
  await screen.findByRole("textbox");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Second retained text" } });
  act(() => select(true));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("First retained text");
  act(() => firstPort.current?.requestClose());
  await answerConfirm("放弃未保存的修改？", "继续编辑");
  expect(close).not.toHaveBeenCalled();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("First retained text");
  const identifiers = [...document.querySelectorAll("[id]")].map((node) => node.id);
  expect(new Set(identifiers).size).toBe(identifiers.length);
  act(() => select(false));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Second retained text");
});

it("refuses a hosted same-name replacement and sends original instance preconditions on save", async () => {
  const target = { ...personalTarget("user_upload"), skill: { ...personalTarget("user_upload").skill, id: "sk_original" } } as SkillDrawerTarget;
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), id: "sk_recreated" });
  renderIntl(<SkillDrawer hosted target={target} onClose={() => {}} />);
  await waitFor(() => expect(fetchSkillDetail).toHaveBeenCalled());
  await screen.findByRole("alert");
  expect(screen.queryByRole("textbox")).toBeNull();
  cleanup();
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), id: "sk_original" });
  putSkillBody.mockRejectedValue(new Error("Conflict"));
  renderIntl(<SkillDrawer hosted target={target} onClose={() => {}} />);
  // Hosted in Canvas the Skill body is directly editable (D10): no separate Edit step.
  await screen.findByRole("textbox");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Original instance edit" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  // Only the instructions are edited; the original manifest header is re-attached on save.
  const front = splitSkillManifest(personalDetail().body_raw ?? "").front;
  await waitFor(() => expect(putSkillBody).toHaveBeenCalledWith("dupont", `${front}Original instance edit`, REVISION, "sk_original"));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Original instance edit");
});

it("retains a Skill buffer when another editor blocks an approved page departure", async () => {
  fetchSkillDetail.mockResolvedValue(personalDetail());
  renderIntl(<SkillDrawer hosted target={personalTarget("user_upload")} onClose={() => {}} />);
  // Hosted in Canvas the Skill body is directly editable (D10): no separate Edit step.
  await screen.findByRole("textbox");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep until the page actually leaves" } });
  const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved: () => true, confirmLeave: async () => false });
  try {
    let departure!: Promise<boolean>;
    act(() => { departure = confirmCanvasNavigation(); });
    await answerConfirm("放弃未保存的修改？", "放弃修改");
    expect(await departure).toBe(false);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Keep until the page actually leaves");
  } finally { unregister(); }
});

it("edits only the instructions in Canvas and never shows the manifest header", async () => {
  fetchSkillDetail.mockResolvedValue({ ...personalDetail(), body_raw: "---\nname: dupont\ndescription: Weekly summary\n---\nRead the weekly sales.\n" });
  renderIntl(<SkillDrawer hosted target={personalTarget("user_upload")} onClose={() => {}} />);
  const field = await screen.findByRole("textbox", { name: "步骤说明" });
  expect((field as HTMLTextAreaElement).value).toBe("Read the weekly sales.\n");
  expect(screen.queryByText(/name: dupont/)).toBeNull();
  // The four fixed segments of a hosted Skill (D10).
  expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["说明", "触发示例", expect.stringMatching(/^文件/), "评估"]);
});

it("splits a SKILL.md manifest into its header and instructions", () => {
  expect(splitSkillManifest("---\nname: a\n---\nBody\n")).toEqual({ front: "---\nname: a\n---\n", body: "Body\n" });
  expect(splitSkillManifest("No header")).toEqual({ front: "", body: "No header" });
});
