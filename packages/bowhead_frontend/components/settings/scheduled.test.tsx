import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import { ApiError } from "@/lib/api-client";
import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";

const fetchScheduledJobsMock = vi.fn();
const createScheduledJobMock = vi.fn();
const patchScheduledJobMock = vi.fn();
const deleteScheduledJobMock = vi.fn();
const fetchScheduledJobRunsMock = vi.fn();
const runScheduledJobNowMock = vi.fn();
const fetchBindableWorkflowsMock = vi.fn();
const readSpacesMock = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchScheduledJobs: (...args: unknown[]) =>
    fetchScheduledJobsMock(...args),
  createScheduledJob: (...args: unknown[]) =>
    createScheduledJobMock(...args),
  patchScheduledJob: (...args: unknown[]) =>
    patchScheduledJobMock(...args),
  deleteScheduledJob: (...args: unknown[]) =>
    deleteScheduledJobMock(...args),
  fetchScheduledJobRuns: (...args: unknown[]) =>
    fetchScheduledJobRunsMock(...args),
  runScheduledJobNow: (...args: unknown[]) =>
    runScheduledJobNowMock(...args),
  fetchBindableWorkflows: (...args: unknown[]) =>
    fetchBindableWorkflowsMock(...args),
}));

vi.mock("@/lib/use-space", () => ({
  readSpaces: (...args: unknown[]) => readSpacesMock(...args),
}));

import {
  SCHEDULED_ERROR_MESSAGE_KEYS,
  SettingsScheduled,
} from "./scheduled";
import { resetViewerTimeZoneForTests } from "@/lib/time-zone";

const JOB = {
  id: "job_1",
  user_id: "user_1",
  workspace_id: "ws_1",
  name: "Daily review",
  cron: "0 9 * * *",
  tz: "Asia/Shanghai",
  query: "Summarize revenue",
  session_strategy: "fresh" as const,
  notify_channels: {},
  status: "active" as const,
  failure_count: 1,
  next_run_at: "2026-07-31T01:00:00+00:00",
  last_run_at: "2026-07-30T01:00:00+00:00",
  created_at: "2026-07-29T01:00:00+00:00",
  updated_at: "2026-07-30T01:00:00+00:00",
};

beforeEach(() => {
  // Binding defaults to absent backend endpoint/flag off = empty list, keeping existing case DOM unchanged.
  fetchBindableWorkflowsMock.mockResolvedValue({ items: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
});

describe("SettingsScheduled progressive error disclosure", () => {
  it("maps every scheduled backend code to both locale catalogs", () => {
    const backendCodes = [
      "authz_lookup_failed",
      "authz_unavailable",
      "clock_tamper",
      "feature_unlicensed",
      "needs_license",
      "orchestrator_not_ready",
      "over_grace",
      "scheduled_anchor_session_deleted",
      "scheduled_datarc_context_failed",
      "scheduled_invalid_cron",
      "scheduled_job_create_failed",
      "scheduled_job_delete_failed",
      "scheduled_job_limit_reached",
      "scheduled_job_load_failed",
      "scheduled_job_not_found",
      "scheduled_job_run_active",
      "scheduled_job_update_failed",
      "scheduled_owner_lost_datarc_access",
      "scheduled_public_entry_mismatch",
      "scheduled_run_conflict",
      "scheduled_run_dispatch_failed",
      "scheduled_run_interrupted",
      "scheduled_runs_load_failed",
      "scheduled_store_unavailable",
      "scheduled_turn_cancelled",
      "scheduled_turn_failed",
      "scheduled_turn_requires_approval",
      "scheduled_turn_requires_input",
      "scheduled_input_timeout",
      "scheduled_input_superseded",
      "scheduled_recipe_lost",
      "scheduled_recipe_changed",
      "scheduled_recipe_rejected",
      "scheduled_team_model_unconfigured",
      "seat_exceeded",
      "suspended",
      "tamper",
      "team_pool_exhausted",
      "user_credit_exhausted",
      "user_not_active",
      "workspace_archived",
      "workspace_required",
      "workspace_role_unknown",
      "workspace_unavailable",
      "ws_access_denied",
    ] as const;
    const zhErrors = zhMessages.settings.scheduled.errors;
    const enErrors = enMessages.settings.scheduled.errors;

    for (const code of backendCodes) {
      const key = SCHEDULED_ERROR_MESSAGE_KEYS[code];
      expect(key, code).toMatch(/^errors\./);
      const messageKey = key.slice("errors.".length) as keyof typeof zhErrors;
      expect(zhErrors[messageKey], `zh-CN ${code}`).toBeTruthy();
      expect(enErrors[messageKey], `en-US ${code}`).toBeTruthy();
    }
  });

  it("has a message in both locales for every mapped key", () => {
    // backendCodes above is a manually copied contract snapshot; a new backend code missing here would still pass.
    // This starts from **mapping itself**, preventing internal FE drift: any SCHEDULED_ERROR_MESSAGE_KEYS addition
    // must also add Chinese/English wording, or users see keys rather than messages.
    const zhErrors = zhMessages.settings.scheduled.errors as Record<string, string>;
    const enErrors = enMessages.settings.scheduled.errors as Record<string, string>;

    for (const [code, key] of Object.entries(SCHEDULED_ERROR_MESSAGE_KEYS)) {
      const messageKey = key.slice("errors.".length);
      expect(zhErrors[messageKey], `zh-CN missing copy for ${code}`).toBeTruthy();
      expect(enErrors[messageKey], `en-US missing copy for ${code}`).toBeTruthy();
    }
  });

  it("shows the German license message in the German interface, not the Chinese one", async () => {
    // Previous English/other binary branching used Chinese authorization-error catalog for German UI.
    fetchScheduledJobsMock.mockRejectedValue(
      new ApiError(403, "seat limit", { code: "seat_exceeded" }),
    );
    readSpacesMock.mockResolvedValue({ items: [] });

    renderIntl(<SettingsScheduled />, { locale: "de-DE" });

    expect(
      await screen.findByText(deMessages.entitlement_error.codes.seat_exceeded),
    ).toBeTruthy();
    expect(screen.queryByText(zhMessages.entitlement_error.codes.seat_exceeded)).toBeNull();
  });

  it("localizes a typed load error and never renders raw exception text", async () => {
    const requestId = "0123456789abcdef0123456789abcdef";
    const raw =
      "postgresql://internal.example password=upstream-secret";
    fetchScheduledJobsMock.mockRejectedValue(
      new ApiError(503, raw, {
        code: "scheduled_job_load_failed",
        requestId,
        diagnostics: {
          schemaVersion: 1,
          disclosure: "authenticated",
          status: 503,
          code: "scheduled_job_load_failed",
          diagnosticCode: "scheduled_failure",
          requestId,
          safeDetail: "phase=load; failure_class=storage",
          retryable: true,
        },
      }),
    );
    readSpacesMock.mockResolvedValue({ items: [] });

    renderIntl(<SettingsScheduled />);

    expect(
      await screen.findByText(
        "暂时无法加载定时任务，请刷新页面后重试。",
      ),
    ).toBeTruthy();
    expect(screen.getByText("查看技术详情")).toBeTruthy();
    expect(screen.getByText(requestId)).toBeTruthy();
    expect(document.body.textContent).not.toContain(raw);
  });

  it("normalizes a legacy persisted run error before progressive disclosure", async () => {
    const raw = "RuntimeError: token=legacy-secret";
    fetchScheduledJobsMock.mockResolvedValue({ items: [JOB] });
    fetchScheduledJobRunsMock.mockResolvedValue({
      items: [
        {
          id: "run_legacy",
          job_id: JOB.id,
          started_at: "2026-07-30T01:00:00+00:00",
          finished_at: "2026-07-30T01:00:01+00:00",
          status: "failed",
          error: raw,
        },
      ],
    });
    readSpacesMock.mockResolvedValue({ items: [] });

    renderIntl(<SettingsScheduled />);
    expect((await screen.findAllByText("Daily review")).length).toBe(2);
    fireEvent.click(screen.getByTitle("运行历史"));

    expect(
      await screen.findByText(
        "本次运行未能完成，请展开技术详情并携错误码联系支持。",
      ),
    ).toBeTruthy();
    expect(screen.getByText("scheduled_turn_failed")).toBeTruthy();
    expect(screen.getByText("run_legacy")).toBeTruthy();
    expect(screen.queryByText("浏览器观测时间")).toBeNull();
    expect(document.body.textContent).not.toContain(raw);
  });
});

// ════════════════════════════════════════════════════════════════════════════
// scheduled-recipe-runs S1 · binding (PRD FR-3).
// ════════════════════════════════════════════════════════════════════════════

const WORKFLOWS = [
  {
    form: "personal_skill" as const,
    id_or_name: "weekly-report",
    display_name: "周报生成",
    description: "汇总本周经营数据",
    author: null,
    is_own: true,
  },
  {
    form: "team_skill" as const,
    id_or_name: "sk_month",
    display_name: "月度复盘",
    description: "团队共享的月度复盘流程",
    author: "组长",
    is_own: false,
  },
];

const SPACES = { items: [{ id: "ws_1", name: "商超零售", desc: "" }] };

const openNewDrawer = async () => {
  // Empty state/header both show “新建定时任务”; click first (header).
  fireEvent.click((await screen.findAllByText("新建定时任务"))[0]);
};

const fillRequiredFields = () => {
  fireEvent.change(screen.getByPlaceholderText("例如：每日 GMV 复盘"), {
    target: { value: "每周复盘" },
  });
  fireEvent.change(
    screen.getByPlaceholderText("用自然语言描述要跑的分析…"),
    { target: { value: "汇总上周经营情况" } },
  );
};

const workflowSelect = () =>
  document.querySelector<HTMLSelectElement>("select[data-testid='bound-workflow']");

describe("绑定面文案守卫", () => {
  it("绑定/失效态的新键在中英两份词表都有文案", () => {
    // Same purpose as bilingual mapping-key coverage: missing vocabulary makes users see keys instead of messages.
    const keys = [
      "field_workflow",
      "field_workflow_help",
      "workflow_none",
      "workflow_group_personal_skill",
      "workflow_group_team_skill",
      "workflow_group_team_subagent",
      "workflow_author",
      "workflow_clear",
      "workflow_unavailable",
      "rebind",
      "pause_reason_unknown",
    ];
    const zh = zhMessages.settings.scheduled as Record<string, unknown>;
    const en = enMessages.settings.scheduled as Record<string, unknown>;
    for (const key of keys) {
      expect(zh[key], `zh-CN ${key}`).toBeTruthy();
      expect(en[key], `en-US ${key}`).toBeTruthy();
    }
    // Solidification-card entry uses its own namespace (whole card uses create_workflow).
    expect(zhMessages.create_workflow.schedule_it).toBeTruthy();
    expect(enMessages.create_workflow.schedule_it).toBeTruthy();
  });
});

describe("SettingsScheduled 绑定固化流程选择器", () => {
  it("端点 404（flag 关/未部署）时整块不渲染，表单照常提交且不带 recipe_ref", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [] });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockRejectedValue(
      new ApiError(404, "not found", { code: "not_found" }),
    );
    createScheduledJobMock.mockResolvedValue({ ...JOB, id: "job_new" });

    renderIntl(<SettingsScheduled />);
    await openNewDrawer();
    await waitFor(() => expect(fetchBindableWorkflowsMock).toHaveBeenCalled());

    expect(screen.queryByText("绑定固化流程")).toBeNull();
    expect(workflowSelect()).toBeNull();

    fillRequiredFields();
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(createScheduledJobMock).toHaveBeenCalled());
    const payload = createScheduledJobMock.mock.calls[0][0];
    expect(payload.name).toBe("每周复盘");
    expect("recipe_ref" in payload).toBe(false);
  });

  it("列出可绑流程、标注他人作者，提交时带结构化 recipe_ref", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [] });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockResolvedValue({ items: WORKFLOWS });
    createScheduledJobMock.mockResolvedValue({ ...JOB, id: "job_new" });

    renderIntl(<SettingsScheduled />);
    await openNewDrawer();

    const select = await waitFor(() => {
      const el = workflowSelect();
      expect(el).toBeTruthy();
      return el!;
    });
    // Unbound by default.
    expect(select.value).toBe("");

    fireEvent.change(select, { target: { value: "team_skill:sk_month" } });
    // One in option, one in selected-value display.
    expect((await screen.findAllByText("月度复盘")).length).toBe(2);
    expect(screen.getByText("作者：组长")).toBeTruthy();

    fillRequiredFields();
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(createScheduledJobMock).toHaveBeenCalled());
    expect(createScheduledJobMock.mock.calls[0][0].recipe_ref).toEqual({
      form: "team_skill",
      id_or_name: "sk_month",
    });
  });

  it("可清除绑定：新建时清掉即不带 recipe_ref", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [] });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockResolvedValue({ items: WORKFLOWS });
    createScheduledJobMock.mockResolvedValue({ ...JOB, id: "job_new" });

    renderIntl(<SettingsScheduled />);
    await openNewDrawer();
    const select = await waitFor(() => {
      const el = workflowSelect();
      expect(el).toBeTruthy();
      return el!;
    });
    fireEvent.change(select, { target: { value: "personal_skill:weekly-report" } });
    fireEvent.click(await screen.findByText("清除"));

    fillRequiredFields();
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(createScheduledJobMock).toHaveBeenCalled());
    expect("recipe_ref" in createScheduledJobMock.mock.calls[0][0]).toBe(false);
  });

  it("?bind=form:id 自动开新建抽屉并预选该流程，随后清掉 query", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [] });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockResolvedValue({ items: WORKFLOWS });
    window.history.replaceState(
      {},
      "",
      "/team/settings/scheduled/?bind=team_skill%3Ask_month",
    );

    renderIntl(<SettingsScheduled />);

    const select = await waitFor(() => {
      const el = workflowSelect();
      expect(el).toBeTruthy();
      return el!;
    });
    expect(select.value).toBe("team_skill:sk_month");
    expect((await screen.findAllByText("月度复盘")).length).toBe(2);
    expect(window.location.search).toBe("");
  });

  it("列表行显示绑定的流程名（116 dogfood P2-1：此前行上完全看不出绑没绑）", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [{
        ...JOB,
        recipe_ref: JSON.stringify({ form: "team_skill", id_or_name: "sk_month" }),
        bound_display_name: "月度复盘",
      }],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);

    expect(await screen.findAllByText("月度复盘")).toBeTruthy();
  });

  it("他人作者的流程连作者一起标（US-6 借权可见性）", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [{
        ...JOB,
        recipe_ref: JSON.stringify({ form: "team_skill", id_or_name: "sk_month" }),
        bound_display_name: "月度复盘",
        bound_author: "张三",
      }],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);

    expect(await screen.findAllByText("· 张三")).toBeTruthy();
  });

  it("未绑定的行不出绑定标记", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [JOB] });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);

    await screen.findAllByText("Daily review");
    expect(screen.queryByText("月度复盘")).toBeNull();
  });

  it("@manual 任务的启用态不说「运行中」（它什么都没在跑）", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [{ ...JOB, cron: "@manual", next_run_at: null }],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);

    expect(await screen.findAllByText("待手动运行")).toBeTruthy();
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("cron 任务的启用态维持原文案", async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [JOB] });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);

    expect(await screen.findAllByText("运行中")).toBeTruthy();
  });

  it("解绑：编辑已绑定任务、清除后 PATCH 送 {unbind:true}", async () => {
    const bound = {
      ...JOB,
      recipe_ref: JSON.stringify({ form: "team_skill", id_or_name: "sk_month" }),
    };
    fetchScheduledJobsMock.mockResolvedValue({ items: [bound] });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockResolvedValue({ items: WORKFLOWS });
    patchScheduledJobMock.mockResolvedValue(bound);

    renderIntl(<SettingsScheduled />);
    await chooseRowAction("Daily review 的更多操作", "编辑");
    const select = await waitFor(() => {
      const el = workflowSelect();
      expect(el).toBeTruthy();
      return el!;
    });
    expect(select.value).toBe("team_skill:sk_month");

    fireEvent.click(screen.getByText("清除"));
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(patchScheduledJobMock).toHaveBeenCalled());
    expect(patchScheduledJobMock.mock.calls[0][1].recipe_ref).toEqual({ unbind: true });
  });
});

describe("SettingsScheduled 失效态", () => {
  const pausedJob = (pause_reason: string) => ({
    ...JOB,
    id: `job_${pause_reason}`,
    status: "paused" as const,
    failure_count: 0,
    pause_reason,
  });

  it.each([
    ["scheduled_recipe_lost", zhMessages.settings.scheduled.errors.recipe_lost],
    ["scheduled_recipe_changed", zhMessages.settings.scheduled.errors.recipe_changed],
    ["scheduled_recipe_rejected", zhMessages.settings.scheduled.errors.recipe_rejected],
  ])("%s 渲染人话而非原始 code，并给「重新绑定」", async (code, copy) => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [pausedJob(code)] });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);
    expect(await screen.findByText(copy)).toBeTruthy();
    expect(document.body.textContent).not.toContain(code);
    expect(screen.getByText("重新绑定")).toBeTruthy();
  });

  it("非绑定类 pause_reason 也出人话、且不给「重新绑定」", async () => {
    // Backend writes bare code without scheduled_ prefix; fallback must still translate it.
    fetchScheduledJobsMock.mockResolvedValue({
      items: [pausedJob("team_model_unconfigured")],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);
    expect(
      await screen.findByText(
        zhMessages.settings.scheduled.errors.team_model_unconfigured,
      ),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("team_model_unconfigured");
    expect(screen.queryByText("重新绑定")).toBeNull();
  });

  it("完全没见过的 pause_reason 落到兜底文案，绝不显示 code", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [pausedJob("something_we_never_shipped")],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);
    expect(
      await screen.findByText(zhMessages.settings.scheduled.pause_reason_unknown),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("something_we_never_shipped");
  });

  it("「重新绑定」打开该任务的编辑抽屉并聚焦选择器", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [pausedJob("scheduled_recipe_changed")],
    });
    readSpacesMock.mockResolvedValue(SPACES);
    fetchBindableWorkflowsMock.mockResolvedValue({ items: WORKFLOWS });

    renderIntl(<SettingsScheduled />);
    fireEvent.click(await screen.findByText("重新绑定"));

    // One each in eyebrow and title.
    expect((await screen.findAllByText("编辑定时任务")).length).toBe(2);
    await waitFor(() => {
      const el = workflowSelect();
      expect(el).toBeTruthy();
      expect(document.activeElement).toBe(el);
    });
  });

  it("active 的任务即便库里留着旧 pause_reason 也不显示失效行", async () => {
    fetchScheduledJobsMock.mockResolvedValue({
      items: [{ ...JOB, pause_reason: "scheduled_recipe_lost" }],
    });
    readSpacesMock.mockResolvedValue(SPACES);

    renderIntl(<SettingsScheduled />);
    expect((await screen.findAllByText("Daily review")).length).toBe(2);
    expect(
      screen.queryByText(zhMessages.settings.scheduled.errors.recipe_lost),
    ).toBeNull();
  });
});

describe("SettingsScheduled 行尾「⋯」（外观段第四批）", () => {
  const setup = () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [JOB] });
    readSpacesMock.mockResolvedValue(SPACES);
    runScheduledJobNowMock.mockResolvedValue({ run_id: "run_1", status: "running" });
    fetchScheduledJobRunsMock.mockResolvedValue({ items: [] });
    deleteScheduledJobMock.mockResolvedValue(undefined);
    renderIntl(<SettingsScheduled />);
  };

  it("orders the menu Run now, Edit, Delete; Edit opens the edit drawer", async () => {
    setup();
    const menu = await openRowMenu("Daily review 的更多操作");
    expect(menuItemNames(menu)).toEqual(["立即运行", "编辑", "删除…"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "编辑" }));
    expect((await screen.findAllByText("编辑定时任务")).length).toBeGreaterThan(0);
    expect(deleteScheduledJobMock).not.toHaveBeenCalled();
    expect(runScheduledJobNowMock).not.toHaveBeenCalled();
  });

  it("offers Run now and Edit on the phone card; Edit opens the drawer without running", async () => {
    setup();
    const openMobileMenu = async () => {
      const mobileTrigger = (await screen.findAllByRole("button", { name: "Daily review 的更多操作", hidden: true }))
        .find((el) => el.closest(".r-row-mobile"));
      expect(mobileTrigger).toBeTruthy();
      fireEvent.click(mobileTrigger!);
      return screen.getByRole("menu", { name: "Daily review 的更多操作", hidden: true });
    };
    expect(menuItemNames(await openMobileMenu())).toEqual(["立即运行", "编辑"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "编辑" }));
    expect((await screen.findAllByText("编辑定时任务")).length).toBeGreaterThan(0);
    expect(runScheduledJobNowMock).not.toHaveBeenCalled();
  });

  it("only runs the job from Run now on the phone card", async () => {
    setup();
    const mobileTrigger = (await screen.findAllByRole("button", { name: "Daily review 的更多操作", hidden: true }))
      .find((el) => el.closest(".r-row-mobile"));
    fireEvent.click(mobileTrigger!);
    fireEvent.click(screen.getByRole("menuitem", { name: "立即运行" }));
    await waitFor(() => expect(runScheduledJobNowMock).toHaveBeenCalledWith("job_1"));
    expect(deleteScheduledJobMock).not.toHaveBeenCalled();
  });

  it("only runs the job from Run now", async () => {
    setup();
    await chooseRowAction("Daily review 的更多操作", "立即运行", { danger: false });
    await waitFor(() => expect(runScheduledJobNowMock).toHaveBeenCalledWith("job_1"));
    expect(deleteScheduledJobMock).not.toHaveBeenCalled();
  });

  it("marks Delete as danger and confirms first; cancel keeps the job", async () => {
    setup();
    await chooseRowAction("Daily review 的更多操作", "删除…", { danger: true });
    await expectConfirmGate({
      action: deleteScheduledJobMock,
      dialogName: "删除这个定时任务？",
      confirmName: "删除",
      reopen: () => chooseRowAction("Daily review 的更多操作", "删除…"),
    });
    await waitFor(() => expect(deleteScheduledJobMock).toHaveBeenCalledWith("job_1"));
  });
});

describe("SettingsScheduled default time zone", () => {
  const stubBrowserZone = (zone: string | undefined) => {
    resetViewerTimeZoneForTests();
    const real = Intl.DateTimeFormat.prototype.resolvedOptions;
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockImplementation(function (
      this: Intl.DateTimeFormat,
    ) {
      return { ...real.call(this), timeZone: zone } as Intl.ResolvedDateTimeFormatOptions;
    });
  };

  afterEach(() => {
    vi.restoreAllMocks();
    resetViewerTimeZoneForTests();
  });

  const createWithDefaultZone = async () => {
    fetchScheduledJobsMock.mockResolvedValue({ items: [] });
    readSpacesMock.mockResolvedValue(SPACES);
    createScheduledJobMock.mockResolvedValue({ ...JOB, id: "job_new" });
    renderIntl(<SettingsScheduled />);
    await openNewDrawer();
    fillRequiredFields();
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(createScheduledJobMock).toHaveBeenCalled());
    return createScheduledJobMock.mock.calls[0][0].tz as string;
  };

  it("creates a new job in the viewer's browser zone", async () => {
    stubBrowserZone("Europe/Berlin");
    expect(await createWithDefaultZone()).toBe("Europe/Berlin");
  });

  it("falls back to UTC when the browser reports no zone", async () => {
    stubBrowserZone(undefined);
    expect(await createWithDefaultZone()).toBe("UTC");
  });
});
