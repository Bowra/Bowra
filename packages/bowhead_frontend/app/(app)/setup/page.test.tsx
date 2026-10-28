import { BUILD_EDITION, PRODUCT_NAME } from "@/lib/product-identity";
import type { ReactNode } from "react";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { TeamOnboardingApiError, type ReadinessItem, type ReadinessState, type TeamOnboardingStatus } from "@/lib/team-onboarding-api";

const mocks = vi.hoisted(() => {
  const push = vi.fn();
  const replace = vi.fn();
  return {
    fetchStatus: vi.fn(),
    push,
    replace,
    router: { push, replace },
  };
});

vi.mock("@/lib/team-onboarding-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/team-onboarding-api")>("@/lib/team-onboarding-api");
  return {
    ...actual,
    fetchTeamOnboardingStatus: (...args: unknown[]) => mocks.fetchStatus(...args),
  };
});
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => mocks.router,
}));

vi.mock("@/components/shell/page-frame", async () => {
  const actual = await vi.importActual<typeof import("@/components/shell/page-frame")>("@/components/shell/page-frame");
  return { ...actual, PageFrame: ({ children }: { children: ReactNode }) => <main>{children}</main> };
});
vi.mock("@/lib/use-error-diagnostics-capability", () => ({ useErrorDiagnosticsCapability: () => false }));

import SetupPage from "./page";

const installationReadyTitle = BUILD_EDITION === "community" ? "实例已就绪" : "许可证处于可用状态";

function item(
  id: string,
  group: "core" | "conditional" | "recommended",
  state: ReadinessState = "ready",
  overrides: Partial<ReadinessItem> = {},
): ReadinessItem {
  return {
    id,
    group,
    state,
    required: group === "core",
    required_scope: group === "core" ? "core" : group === "conditional" ? "capability" : null,
    satisfied: state === "ready" || state === "not_applicable",
    blocking: group === "core" && state !== "ready" && state !== "warning",
    reason_code:
      {
        license_active: "license_active",
        public_entry_ready: "public_entry_ready",
        llm_primary_configured: "primary_model_configured",
        space_ready: "space_ready",
        first_turn_succeeded: "successful_turn_exists",
        knowledge_base_ready: "kb_embedding_configured",
        datarc_connection_configured: "datarc_connection_missing",
        llm_fallback_configured: "fallback_model_missing",
        email_delivery_configured: "email_not_configured",
        secondary_admin_present: "secondary_admin_missing",
      }[id] ?? "model_state_unavailable",
    required_roles: ["owner"],
    can_act: true,
    observed_at: "2026-07-25T10:00:00Z",
    action:
      id === "license_active"
        ? null
        : {
            kind: "internal_link" as const,
            href: id === "knowledge_base_ready" ? "/knowledge?from=setup" : "/team/settings/models/?from=setup",
          },
    checks: [],
    ...overrides,
  };
}

function snapshot(): TeamOnboardingStatus {
  return {
    schema_version: 1,
    team_id: "team_a",
    milestone: "core_ready",
    partial: false,
    generated_at: "2026-07-25T10:00:00Z",
    core: { satisfied: 5, total: 5 },
    items: [
      item("license_active", "core"),
      item("public_entry_ready", "core"),
      item("llm_primary_configured", "core"),
      item("space_ready", "core"),
      item("first_turn_succeeded", "core"),
      item("knowledge_base_ready", "conditional"),
      item("datarc_connection_configured", "recommended", "missing"),
      item("llm_fallback_configured", "recommended", "missing"),
      item("email_delivery_configured", "recommended", "missing"),
      item("secondary_admin_present", "recommended", "missing"),
    ],
    context: {
      team_name: "Acme",
      current_role: "owner",
      active_spaces: 1,
      active_seats: 1,
      max_seats: 5,
      license_health: "active",
      license_expires_at: null,
      grace_until: null,
      current_public_entry: "https://bowhead.example/",
      account_creation_policy: "invite_only",
      auth_methods: [],
      email_source: "none",
      kb_feature_licensed: true,
      optional_capabilities: {
        image_generation: "disabled",
        web_search: "disabled",
        primary_vision: "enabled",
        primary_think: "enabled",
      },
    },
  };
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("Team setup readiness", () => {
  it("keeps recommendations outside fixed core progress and follows safe actions", async () => {
    mocks.fetchStatus.mockResolvedValue(snapshot());
    renderIntl(<SetupPage />);

    expect((await screen.findByLabelText("核心启用状态")).textContent).toContain("5 / 5");
    expect(screen.getByText("连接北极九章")).toBeTruthy();
    expect(screen.getAllByText("尚未配置", { selector: "span" }).length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("primary-setup-action")).toHaveLength(1);
    expect(screen.getByText("许可证状态")).toBeTruthy();
    expect(screen.getByText(/已配置不等于实时验证/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "能力配置与推荐项" }));

    fireEvent.click(
      screen.getByRole("button", {
        name: /完成知识库配置/,
      }),
    );
    expect(mocks.push).toHaveBeenCalledWith("/knowledge?from=setup");
  });

  it("keeps completed checks and recommendations closed until requested", async () => {
    mocks.fetchStatus.mockResolvedValue(snapshot());
    renderIntl(<SetupPage />);
    const completed = await screen.findByRole("button", { name: /已完成的检查\s*5/ });
    expect(completed.getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText(installationReadyTitle).closest("[hidden]")).not.toBeNull();
    expect(screen.getByText("连接北极九章").closest("[hidden]")).not.toBeNull();
    fireEvent.click(completed);
    expect(screen.getByText(installationReadyTitle).closest("[hidden]")).toBeNull();
    expect(completed.getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps satisfied warnings visible without reducing core progress", async () => {
    const data = snapshot();
    data.items[0] = item("license_active", "core", "warning", { satisfied: true, reason_code: "license_in_grace" });
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);
    const warning = await screen.findByText(installationReadyTitle);
    expect(warning.closest("[hidden]")).toBeNull();
    expect(screen.getByLabelText("核心启用状态").textContent).toContain("5 / 5");
  });

  it("promotes the first unfinished core item as the single primary action", async () => {
    const data = snapshot();
    data.milestone = "core_configuring";
    data.core = { satisfied: 2, total: 5 };
    data.items = data.items.map((entry) => {
      if (entry.id === "llm_primary_configured") {
        return item(entry.id, "core", "missing", {
          reason_code: "primary_model_missing",
          action: {
            kind: "internal_link",
            href: "/team/settings/models/?from=setup",
          },
        });
      }
      if (entry.id === "space_ready" || entry.id === "first_turn_succeeded") {
        return item(entry.id, "core", "missing");
      }
      return entry;
    });
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);

    const suggested = await screen.findByRole("region", {
      name: "主要模型可以运行智能体",
    });
    const primary = screen.getByTestId("primary-setup-action");
    expect(primary.textContent).toContain("配置模型");
    expect(screen.getAllByTestId("primary-setup-action")).toHaveLength(1);
    expect(suggested.textContent).toContain("没有主要模型");
    expect(screen.getAllByText("影响范围").length).toBeGreaterThan(0);
    expect(screen.getAllByText("责任角色").length).toBeGreaterThan(0);
    expect(screen.getAllByText("观察时间").length).toBeGreaterThan(0);

    fireEvent.click(primary);
    expect(mocks.push).toHaveBeenCalledWith("/team/settings/models/?from=setup");
  });

  it("explains owner-only blockers and offers the next action the admin can take", async () => {
    const data = snapshot();
    data.milestone = "core_configuring";
    data.core = { satisfied: 1, total: 5 };
    data.context.current_role = "team_admin";
    data.items = data.items.map((entry) => {
      if (entry.id === "public_entry_ready") {
        return item(entry.id, "core", "missing", {
          can_act: false,
          required_roles: ["owner"],
          reason_code: "no_active_public_entry",
          action: {
            kind: "internal_link",
            href: "/team/settings/public-entries/?from=setup",
          },
        });
      }
      if (entry.id === "llm_primary_configured") {
        return item(entry.id, "core", "missing", {
          reason_code: "primary_model_missing",
          action: {
            kind: "internal_link",
            href: "/team/settings/models/?from=setup",
          },
        });
      }
      return entry;
    });
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);

    expect(await screen.findByText(/这一项需要负责人完成/)).toBeTruthy();
    expect(screen.getByTestId("primary-setup-action").textContent).toContain(`进入 ${PRODUCT_NAME}`);
    fireEvent.click(
      screen.getByRole("button", {
        name: /先完成：主要模型可以运行智能体/,
      }),
    );
    expect(mocks.push).toHaveBeenCalledWith("/team/settings/models/?from=setup");
  });

  it("collapses not-applicable capabilities and never follows unsafe actions", async () => {
    const data = snapshot();
    data.milestone = "core_configuring";
    data.core = { satisfied: 4, total: 5 };
    data.items = data.items.map((entry) => {
      if (entry.id === "knowledge_base_ready") {
        return item(entry.id, "conditional", "not_applicable", {
          reason_code: "kb_not_licensed",
        });
      }
      if (entry.id === "first_turn_succeeded") {
        return item(entry.id, "core", "missing", {
          action: {
            kind: "internal_link",
            href: "https://evil.example/",
          },
          reason_code: "no_successful_turn",
        });
      }
      return entry;
    });
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);

    const collapsed = await screen.findByText("知识库运行时已就绪");
    expect(collapsed.closest("[hidden]")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "开始对话" }));
    expect(mocks.push).not.toHaveBeenCalledWith("https://evil.example/");
  });

  it("preserves the last snapshot when a refresh fails", async () => {
    mocks.fetchStatus.mockResolvedValue(snapshot());
    renderIntl(<SetupPage />);

    await screen.findByText("核心链路已就绪");
    mocks.fetchStatus.mockRejectedValue(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText(/保留上一次成功快照/)).toBeTruthy();
    expect(screen.getByText("核心链路已就绪")).toBeTruthy();
  });

  it("keeps readable items when a single readiness source is partial", async () => {
    const data = snapshot();
    data.partial = true;
    data.items = data.items.map((entry) =>
      entry.id === "datarc_connection_configured"
        ? item(entry.id, "recommended", "unknown", {
            reason_code: "datarc_state_unavailable",
          })
        : entry,
    );
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);

    expect(await screen.findByText(/部分本地状态源暂时不可读/)).toBeTruthy();
    expect(screen.getByText(installationReadyTitle)).toBeTruthy();
    expect(screen.getByText("暂时无法读取北极九章连接状态。")).toBeTruthy();
  });

  it("deduplicates rapid manual refreshes when every core source is unknown", async () => {
    const data = snapshot();
    data.milestone = "core_configuring";
    data.core = { satisfied: 0, total: 5 };
    data.items = data.items.map((entry) =>
      entry.group === "core"
        ? item(entry.id, "core", "unknown", {
            reason_code: entry.id === "license_active" ? "license_state_unavailable" : "model_state_unavailable",
          })
        : entry,
    );
    mocks.fetchStatus.mockResolvedValue(data);
    renderIntl(<SetupPage />);

    await screen.findAllByRole("button", { name: "刷新状态" });
    let resolveRefresh: ((value: TeamOnboardingStatus) => void) | undefined;
    const pending = new Promise<TeamOnboardingStatus>((resolve) => {
      resolveRefresh = resolve;
    });
    mocks.fetchStatus.mockReset();
    mocks.fetchStatus.mockReturnValue(pending);
    const refresh = screen.getByTestId("primary-setup-action");
    const callsBeforeRefresh = mocks.fetchStatus.mock.calls.length;
    act(() => {
      refresh.click();
      refresh.click();
    });
    expect(mocks.fetchStatus).toHaveBeenCalledTimes(callsBeforeRefresh + 1);
    await act(async () => {
      resolveRefresh?.(data);
      await pending;
    });
  });

  it("shows a retryable whole-page error without inventing item states", async () => {
    mocks.fetchStatus.mockRejectedValue(new Error("offline"));
    renderIntl(<SetupPage />);

    expect(await screen.findByText("无法加载启用状态")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
    expect(screen.queryByText("核心运行链路")).toBeNull();
  });

  it("renders a role-specific 403 state without a retry loop", async () => {
    mocks.fetchStatus.mockRejectedValue(new TeamOnboardingApiError(403, "forbidden"));
    renderIntl(<SetupPage />);

    expect(await screen.findByText(/仅对团队负责人和管理员开放/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  });

  it("renders a deployment-disabled state without retrying a missing route", async () => {
    mocks.fetchStatus.mockRejectedValue(new TeamOnboardingApiError(404, "not found"));
    renderIntl(<SetupPage />);

    expect(await screen.findByText(/尚未启用团队引导功能/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  });

  it("sends expired authentication through the protected login handoff", async () => {
    mocks.fetchStatus.mockRejectedValue(new TeamOnboardingApiError(401, "unauthorized"));
    renderIntl(<SetupPage />);

    await waitFor(() => {
      expect(mocks.replace).toHaveBeenCalledWith("/login?next=%2Fsetup");
    });
  });
});

// ─── credential-readiness R8: capability list = decision information (B5–B8) ────────────────────

function withContext(over: Record<string, unknown>): TeamOnboardingStatus {
  const base = snapshot();
  return { ...base, context: { ...base.context, ...over } } as TeamOnboardingStatus;
}

describe("Setup capability list as decision info (R8)", () => {
  it("shows consequences and separates configurable capabilities from primary model changes", async () => {
    mocks.fetchStatus.mockResolvedValue(
      withContext({
        optional_capabilities: {
          image_generation: "disabled",
          web_search: "disabled",
          primary_vision: "disabled",
          primary_think: "enabled",
          kb_rerank: "disabled",
          kb_vision: "invalid",
          kb_row_extractor: "disabled",
        },
      }),
    );
    renderIntl(<SetupPage />);

    // B5: consequence row, more than a Disabled label.
    expect(await screen.findByText("无法获取公开网络信息，只能依据模型已有知识与内部资料")).toBeTruthy();
    expect(screen.getByText("知识库检索缺少精排，相关性下降")).toBeTruthy();
    // B7: primary_vision CTA is Change Primary Model, not a nonexistent independent configuration entry.
    fireEvent.click(screen.getByRole("button", { name: "能力配置与推荐项" }));
    const visionCta = screen.getByTestId("capability-cta-primary_vision");
    expect(visionCta.textContent).toBe("更换主模型");
    // Independently configurable items → Configure; links must use /team/settings/models/.
    const wsCta = screen.getByTestId("capability-cta-web_search");
    expect(wsCta.textContent).toBe("去配置");
    expect(wsCta.getAttribute("href")).toContain("/team/settings/models/");
    // Enabled items have no consequence row.
    expect(screen.queryByText("复杂推理不走深度思考档")).toBeNull();
  });

  it("asks members to contact administrators without configuration links", async () => {
    mocks.fetchStatus.mockResolvedValue(
      withContext({
        current_role: "member",
        optional_capabilities: {
          image_generation: "disabled",
          web_search: "disabled",
          primary_vision: "enabled",
          primary_think: "enabled",
          kb_rerank: "enabled",
          kb_vision: "enabled",
          kb_row_extractor: "enabled",
        },
      }),
    );
    renderIntl(<SetupPage />);

    await screen.findByText("无法生成插画、海报等创意图片");
    expect(screen.queryByTestId("capability-cta-web_search")).toBeNull();
    expect(screen.getAllByText(/需团队管理员开启/).length).toBeGreaterThan(0);
  });

  it("retains all knowledge capability signals independently of readiness conditions", async () => {
    mocks.fetchStatus.mockResolvedValue(
      withContext({
        optional_capabilities: {
          image_generation: "enabled",
          web_search: "enabled",
          primary_vision: "enabled",
          primary_think: "enabled",
          kb_rerank: "disabled",
          kb_vision: "disabled",
          kb_row_extractor: "disabled",
        },
      }),
    );
    renderIntl(<SetupPage />);

    expect(await screen.findByTestId("capability-kb_rerank")).toBeTruthy();
    expect(screen.getByTestId("capability-kb_vision")).toBeTruthy();
    expect(screen.getByTestId("capability-kb_row_extractor")).toBeTruthy();
    expect(screen.getByText("知识库行提取")).toBeTruthy();
  });
});

// ─── credential-readiness R8b: runtime delivery signals (C1, PRD §6.2 tier B) ──────────────

describe("Setup runtime delivery signal (R8b)", () => {
  it("keeps healthy runtime evidence in the team information disclosure", async () => {
    mocks.fetchStatus.mockResolvedValue(
      withContext({
        runtime: {
          llm_refusals_total: 0,
          llm_refusals_by_reason: {},
          observed_since: "2026-07-30T08:00:00Z",
        },
      }),
    );
    renderIntl(<SetupPage />);

    expect((await screen.findByTestId("runtime-signal-ok")).textContent).toContain("未检测到模型送达异常");
    expect(screen.queryByTestId("runtime-signal-degraded")).toBeNull();
  });

  it("keeps runtime failures visible with counts and reasons outside disclosures", async () => {
    mocks.fetchStatus.mockResolvedValue(
      withContext({
        runtime: {
          llm_refusals_total: 3,
          llm_refusals_by_reason: { dreaming: 2, chat: 1 },
          observed_since: "2026-07-30T08:00:00Z",
        },
      }),
    );
    renderIntl(<SetupPage />);

    const degraded = await screen.findByTestId("runtime-signal-degraded");
    expect(degraded.closest("[hidden]")).toBeNull();
    expect(degraded.textContent).toContain("3 次调用未拿到团队模型配置");
    expect(degraded.textContent).toContain("系统缺陷信号");
    expect(degraded.textContent).toContain("dreaming × 2");
    expect(degraded.textContent).toContain("chat × 1");
    // Tier B ignores role, with no configuration CTA (PRD §6.2: configuration is valid; system failed delivery).
    expect(degraded.textContent).not.toContain("去配置");
  });

  it("omits runtime evidence when older snapshots have none", async () => {
    mocks.fetchStatus.mockResolvedValue(snapshot());
    renderIntl(<SetupPage />);

    await screen.findByLabelText("核心启用状态");
    expect(screen.queryByTestId("runtime-signal")).toBeNull();
  });
});
