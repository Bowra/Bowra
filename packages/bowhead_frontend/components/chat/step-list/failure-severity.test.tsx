/** Failure severity remains distinct from recorded issue counts and retry activity. */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { createTranslator, NextIntlClientProvider } from "next-intl";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import fr from "@/messages/fr-FR.json";
import zh from "@/messages/zh-CN.json";
import { AgentTrace, stepErrorMessage } from "./AgentTrace";
import { normalizeStepError } from "@/lib/sse-events";
import { StepSummary } from "./StepSummary";
import { isRecoverableStepFailure, countStepIssues } from "./self-healing";
import type { StepView } from "./types";

afterEach(() => cleanup());

function wrap(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      {ui}
    </NextIntlClientProvider>,
  );
}

function step(over: Partial<StepView>): StepView {
  return {
    stepId: "s1",
    state: "done",
    toolName: "python_sandbox",
    ...over,
  } as StepView;
}

describe("Recoverable step severity", () => {
  it("classifies transient failures as recoverable without proving a retry", () => {
    expect(isRecoverableStepFailure(step({ state: "failed", error: { kind: "transient" } as never }))).toBe(true);
  });
  it("retains recoverable user-input failures", () => {
    expect(isRecoverableStepFailure(step({ state: "failed", error: { uiKind: "needs_user_input" } as never }))).toBe(true);
  });
  it("retains partial batch severity", () => {
    expect(isRecoverableStepFailure(step({ state: "partial" }))).toBe(true);
  });
  it("does not classify permanent failures as recoverable", () => {
    expect(isRecoverableStepFailure(step({ state: "failed", error: { kind: "permanent" } as never }))).toBe(false);
  });
  it("does not guess recoverability when failure metadata is absent", () => {
    expect(isRecoverableStepFailure(step({ state: "failed" }))).toBe(false);
  });
  it("does not classify completed steps as failures", () => {
    expect(isRecoverableStepFailure(step({ state: "done" }))).toBe(false);
  });

  // ★ A real counterexample exposed by an existing AgentBlock.test.tsx case:
  // Power BI `pbi_query_result_incomplete` has kind:"transient" but
  // diagnostics.retryable:false — results are incomplete and the system actively stops using these data;
  // users **must** see the explanation. Checking only kind would wrongly collapse it.
  it("honors nonretryable diagnostics over a transient kind", () => {
    expect(
      isRecoverableStepFailure(
        step({
          state: "failed",
          error: {
            kind: "transient",
            diagnostics: { retryable: false },
          } as never,
        }),
      ),
    ).toBe(false);
  });

  it("retains recoverable severity when retry is permitted", () => {
    expect(
      isRecoverableStepFailure(
        step({
          state: "failed",
          error: { kind: "transient", diagnostics: { retryable: true } } as never,
        }),
      ),
    ).toBe(true);
  });
  it("counts failed and partial steps, not attempts", () => {
    expect(
      countStepIssues([
        step({ state: "failed", error: { kind: "transient" } as never }),
        step({ state: "done" }),
        step({ state: "partial" }),
        step({ state: "failed", error: { kind: "permanent" } as never }),
      ]),
    ).toBe(3);
  });
});

describe("StepSummary recorded issues", () => {
  it("shows recorded failed and partial steps without claiming retries", () => {
    wrap(
      <StepSummary
        steps={[
          step({ state: "done" }),
          step({ stepId: "s2", state: "failed", error: { kind: "transient" } as never }),
          step({ stepId: "s3", state: "partial" }),
          step({ stepId: "s4", state: "failed", error: { kind: "permanent" } as never }),
        ]}
        onExpand={() => {}}
      />,
    );
    expect(screen.getByTestId("step-summary-issues").textContent).toBe("3 个步骤遇到问题");
  });

  it("omits the issue count for clean execution", () => {
    wrap(<StepSummary steps={[step({ state: "done" })]} onExpand={() => {}} />);
    expect(screen.queryByTestId("step-summary-issues")).toBeNull();
  });
});

describe("AgentTrace failure severity", () => {
  function trace(err: Record<string, unknown> | undefined, state = "failed") {
    return wrap(
      <AgentTrace
        steps={[
          {
            stepId: "s_x",
            parentStepId: null,
            concurrencyGroupId: null,
            stepType: "tool_run",
            toolName: "python_sandbox",
            headerZh: "运行代码",
            state,
            error: err,
          } as never,
        ]}
        peerNames={{}}
        peerDisplayNames={{}}
        peerProse={{}}
      />,
    );
  }

  it("keeps unclassified exception prose out of a rendered persisted step", () => {
    trace({ kind: "permanent", message: "tool_exception: secret provider response" });
    expect(screen.queryByText(/secret provider response/)).toBeNull();
    expect(screen.getByText(zh.step_list.step_failed_generic)).toBeTruthy();
  });

  it("transient 失败 → .stp.is-part（琥珀 ⚠），不是 is-fail（红 ✕）", () => {
    const { container } = trace({ kind: "transient", message: "查询超时" });
    expect(container.querySelector(".stp.is-part")).not.toBeNull();
    expect(container.querySelector(".stp.is-fail")).toBeNull();
  });

  it("permanent 失败 → 仍是 .stp.is-fail（红），终态必须显眼", () => {
    const { container } = trace({ kind: "permanent", message: "配置错误" });
    expect(container.querySelector(".stp.is-fail")).not.toBeNull();
    expect(container.querySelector(".stp.is-part")).toBeNull();
  });

  it("transient 但 retryable:false → 按终态染红（后端说不会再试了）", () => {
    const { container } = trace({
      kind: "transient",
      message: "结果不完整",
      diagnostics: { retryable: false },
    });
    expect(container.querySelector(".stp.is-fail")).not.toBeNull();
    expect(container.querySelector(".stp.is-part")).toBeNull();
  });
});

describe("step failure sentence (no raw codes in the UI)", () => {
  const tStep = ((key: string) => (key === "step_failed_generic" || key === "skill_unavailable" ? zh.step_list[key] : key)) as never;
  const tReasons = ((key: keyof typeof zh.error_messages.reasons) => zh.error_messages.reasons[key]) as never;
  const message = (error: Record<string, unknown>) => stepErrorMessage(error as never, tStep, tReasons);

  it("maps a public reason code, given as the message or the code, to its sentence", () => {
    expect(message({ kind: "permanent", message: "wallclock_exceeded" })).toBe(zh.error_messages.reasons.wallclock_exceeded);
    expect(message({ kind: "permanent", message: "boom", code: "llm_provider_timeout" })).toBe(zh.error_messages.reasons.llm_provider_timeout);
  });

  it("replaces an unknown bare code with the generic sentence", () => {
    expect(message({ kind: "permanent", message: "some_internal_code" })).toBe(zh.step_list.step_failed_generic);
    expect(message({ kind: "permanent", message: "" })).toBe(zh.step_list.step_failed_generic);
    expect(zh.step_list.step_failed_generic).toContain("这一步执行失败");
    expect(zh.step_list.step_failed_generic).not.toContain("原因暂时无法确定");
  });

  it("shows the server's Skill denial sentence, which names the Skill and the fix", () => {
    const wire = normalizeStepError({
      type: "skill_unavailable",
      message_zh: "Skill “Sales report” is waiting for re-approval by a team admin.",
      is_transient: false,
    });
    expect(wire?.code).toBe("skill_unavailable");
    expect(message(wire as never)).toBe("Skill “Sales report” is waiting for re-approval by a team admin.");
  });

  it("uses the localized Skill copy for older Skill denials and an empty Skill denial", () => {
    for (const type of ["assigned_skill_unavailable", "skill_scope_denied"]) {
      const wire = normalizeStepError({ type, message_zh: "The Agent's exact Skill source is unavailable for this operation.", is_transient: false });
      expect(message(wire as never)).toBe(zh.step_list.skill_unavailable);
    }
    expect(message({ kind: "permanent", code: "skill_unavailable", message: "" })).toBe(zh.step_list.skill_unavailable);
    expect(message({ kind: "permanent", code: "skill_unavailable", message: "skill_unavailable" })).toBe(zh.step_list.skill_unavailable);
  });

  it.each(["Configuration failed", "tool_exception: RuntimeError: private detail", "Call subagent_return before continuing"]) ("uses a bounded explanation for unclassified prose: %s", (text) => {
    expect(message({ kind: "permanent", message: text })).toBe(zh.step_list.step_failed_generic);
  });
});

describe("per-reason Skill denial copy", () => {
  const SKILL_REASONS = [
    "not_granted",
    "script_runtime_disabled",
    "command_disabled",
    "disabled_by_team",
    "not_approved",
    "pending_reapproval",
    "not_visible",
    "use_denied",
    "source_changed",
  ] as const;
  const SERVER_SENTENCE = "Skill 'sales-report' was edited and is waiting for re-approval by a team admin.";
  const translators = (locale: string, messages: Record<string, unknown>) => ({
    tStep: createTranslator({ locale, messages, namespace: "step_list" }) as never as (key: string, values?: Record<string, string>) => string,
    tReasons: createTranslator({ locale, messages, namespace: "error_messages.reasons" }) as never,
  });
  // The step payload is persisted as emitted, so live and replayed steps parse the same wire error.
  const wire = (reason: string, skill?: string | null) =>
    normalizeStepError({
      type: "skill_unavailable",
      code: `skill_unavailable_${reason}`,
      message_zh: SERVER_SENTENCE,
      is_transient: reason === "source_changed",
      ...(skill === undefined ? {} : { skill }),
    });

  it.each([
    ["zh-CN", zh],
    ["en-US", en],
    ["de-DE", de],
    ["fr-FR", fr],
  ] as const)("%s names the Skill and the reason for every reason in its own language", (locale, messages) => {
    const { tStep, tReasons } = translators(locale, messages);
    for (const reason of SKILL_REASONS) {
      const error = wire(reason, "sales-report");
      expect(error?.skill).toBe("sales-report");
      const text = stepErrorMessage(error as never, tStep as never, tReasons);
      expect(text).toBe(tStep(`skill_unavailable_reason_${reason}`, { skill: tStep("skill_unavailable_named", { name: "sales-report" }) }));
      expect(text).toContain("sales-report");
      expect(text).not.toMatch(/[{}]|step_list\./);
      expect(text).not.toBe(SERVER_SENTENCE);
    }
  });

  it("says “This Skill” when the step carries no usable Skill name", () => {
    const { tStep, tReasons } = translators("en-US", en);
    for (const skill of [undefined, null, "", "../etc/passwd", "a b"]) {
      const error = wire("use_denied", skill);
      expect(error?.skill).toBeUndefined();
      expect(stepErrorMessage(error as never, tStep as never, tReasons)).toBe(
        en.step_list.skill_unavailable_reason_use_denied.replace("{skill}", en.step_list.skill_unavailable_unnamed),
      );
    }
  });

  it("falls back to the server sentence for a reason this page doesn't know", () => {
    const { tStep, tReasons } = translators("zh-CN", zh);
    expect(stepErrorMessage(wire("quota_exhausted", "sales-report") as never, tStep as never, tReasons)).toBe(SERVER_SENTENCE);
    expect(
      stepErrorMessage({ kind: "permanent", code: "skill_unavailable_quota_exhausted", message: "" } as never, tStep as never, tReasons),
    ).toBe(zh.step_list.skill_unavailable);
  });

  it("keeps a Skill name only on Skill denials", () => {
    const error = normalizeStepError({ type: "tool_exception", code: "llm_provider_timeout", skill: "sales-report", message_zh: "", is_transient: true });
    expect(error?.skill).toBeUndefined();
  });
});

describe("agent messaging and new-presentation failures name the cause", () => {
  // 116 s_beffa22dce1f showed "this step failed without a specific error" for both.
  const CASES = {
    collaboration_coordination_reply_only: "collab_errors.format",
    collaboration_unresolved: "collab_errors.waiting",
    collaboration_reply_closed: "collab_errors.closed",
    collaboration_operation_limit: "collab_errors.limit",
    collaboration_material_unavailable: "collab_errors.forbidden",
    collaboration_recipient_unavailable: "collab_errors.unavailable",
    deck_render_not_admitted: "deck_errors.not_admitted",
    deck_storage_unavailable: "deck_errors.storage_unavailable",
    deck_validation_failed: "deck_errors.invalid",
    deck_delivery_missing: "deck_errors.missing",
  } as const;

  it.each([
    ["zh-CN", zh],
    ["en-US", en],
    ["de-DE", de],
    ["fr-FR", fr],
  ] as const)("renders the localized sentence in %s for the wire code", (locale, messages) => {
    const tStep = createTranslator({ locale, messages, namespace: "step_list" }) as never as (key: string) => string;
    const tReasons = createTranslator({ locale, messages, namespace: "error_messages.reasons" }) as never;
    for (const [code, key] of Object.entries(CASES)) {
      const error = normalizeStepError({ type: "tool_returned_error", code, message_zh: "tool 返回 ok=False", is_transient: true });
      expect(error?.code).toBe(code);
      const text = stepErrorMessage(error as never, tStep as never, tReasons);
      expect(text).toBe(tStep(key));
      expect(text).not.toBe(tStep("step_failed_generic"));
      expect(text).not.toMatch(/step_list\.|collab_errors|deck_errors/);
    }
  });
});
