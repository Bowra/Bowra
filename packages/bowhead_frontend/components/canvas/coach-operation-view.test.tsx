import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import {
  readCoachOperation,
  type CoachOperationOpen,
  type CoachOperationReceipt,
} from "@/lib/coach-operations";
import { CoachOperationView, operationSummary, operationTarget } from "./coach-operation-view";

vi.mock("@/lib/coach-operations", async (original) => ({
  ...(await original<typeof import("@/lib/coach-operations")>()),
  readCoachOperation: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const pointer: CoachOperationOpen = {
  requestId: "original",
  turnId: "t_original",
  sessionId: "s_original",
  userId: "u_original",
  teamId: "team_original",
  workspaceId: "ws_original",
  reference: { kind: "skill_save", operation_id: `csw_${"a".repeat(64)}` },
};
const receipt: CoachOperationReceipt = {
  operation_ref: pointer.reference,
  domain_result: {
    outcome: "succeeded",
    original_target: { name: "private-method" },
  },
};

it("clears private results after a denied refresh and leaves closing to the tab", async () => {
  vi.mocked(readCoachOperation)
    .mockResolvedValueOnce(receipt)
    .mockRejectedValueOnce(new Error("Revoked"));
  renderIntl(<CoachOperationView pointer={pointer} active />);
  // The Skill slug never becomes the title; a successful save is named “Skill saved”.
  expect(await screen.findByRole("heading", { name: "技能已保存" })).toBeVisible();
  expect(screen.queryByText("private-method")).not.toBeInTheDocument();
  expect(screen.getByTestId("coach-operation-card")).toHaveAttribute("data-intent", "success");
  expect(screen.getByTestId("coach-operation-card")).toHaveTextContent("技能已保存");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("暂时无法查看这个结果");
  expect(screen.queryByTestId("coach-operation-card")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /关闭/ })).toBeNull();
  expect(readCoachOperation).toHaveBeenCalledTimes(2);
});

it("names and marks a failed save by its outcome and keeps the marker while the tab is inactive", async () => {
  vi.mocked(readCoachOperation).mockResolvedValue({
    operation_ref: { kind: "plugin_save", operation_id: `cpw_${"a".repeat(64)}` },
    domain_result: { outcome: "failed", original_target: { name: "Retail data pack" } },
  });
  const onMarker = vi.fn();
  const onLabel = vi.fn();
  const failedPointer = { ...pointer, reference: { kind: "plugin_save" as const, operation_id: `cpw_${"a".repeat(64)}` } };
  function Host() {
    const [active, setActive] = useState(true);
    return <>
      <button type="button" onClick={() => setActive(false)}>Select another tab</button>
      <CoachOperationView pointer={failedPointer} active={active} onMarker={onMarker} onLabel={onLabel} />
    </>;
  }
  renderIntl(<Host />);
  const card = await screen.findByTestId("coach-operation-card");
  expect(card).toHaveAttribute("data-intent", "failure");
  expect(card).toHaveTextContent("插件没能保存");
  expect(screen.getByRole("heading", { name: "插件没能保存" })).toBeVisible();
  await waitFor(() => expect(onMarker).toHaveBeenLastCalledWith({ marker: "failed", statusLabel: "失败" }));
  expect(onLabel).toHaveBeenLastCalledWith("插件没能保存");
  fireEvent.click(screen.getByRole("button", { name: "Select another tab" }));
  expect(screen.queryByTestId("coach-operation-card")).not.toBeInTheDocument();
  // An inactive tab does not read, so it keeps the last known marker and name.
  expect(onMarker).toHaveBeenLastCalledWith({ marker: "failed", statusLabel: "失败" });
  expect(onLabel).toHaveBeenLastCalledWith("插件没能保存");
});

it("fences a late result from the previously mounted target", async () => {
  let settle!: (value: CoachOperationReceipt) => void;
  vi.mocked(readCoachOperation).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        settle = resolve;
      }),
  );
  const first = renderIntl(
    <CoachOperationView pointer={pointer} active />,
  );
  await waitFor(() => expect(readCoachOperation).toHaveBeenCalledOnce());
  first.unmount();
  vi.mocked(readCoachOperation).mockResolvedValue({
    ...receipt,
    domain_result: {
      outcome: "unknown",
      original_target: { name: "second-method" },
    },
  });
  renderIntl(
    <CoachOperationView
      pointer={{ ...pointer, requestId: "second" }}
      active
    />,
  );
  // An unknown outcome keeps the neutral operation name.
  expect(await screen.findByRole("heading", { name: "技能保存" })).toBeVisible();
  await act(async () => settle(receipt));
  expect(screen.queryByRole("heading", { name: "技能已保存" })).not.toBeInTheDocument();
  expect(screen.getByTestId("coach-operation-card")).toHaveTextContent("结果尚不确定");
});

it("does not equate successful reads or CI authorization with successful execution", () => {
  expect(
    operationSummary({
      ...receipt,
      domain_result: { ok: true, state: "prepared", outcome: "unknown" },
    }).status,
  ).toBe("unknown");
  expect(
    operationSummary({
      operation_ref: {
        kind: "ci_delegation",
        agent_id: "osa_original",
        delegation_id: `acig_${"b".repeat(32)}`,
      },
      domain_result: { ok: true, delegation: { state: "authorized" } },
    }).status,
  ).toBe("authorized");
});

it("shows a stopped CI receipt and the recorded Tool requirement", async () => {
  const stopped: CoachOperationReceipt = {
    operation_ref: {
      kind: "ci_delegation",
      agent_id: "osa_original",
      delegation_id: `acig_${"b".repeat(32)}`,
    },
    domain_result: { ok: true, delegation: { state: "revoked" } },
  };
  vi.mocked(readCoachOperation).mockResolvedValue(stopped);
  renderIntl(
    <CoachOperationView
      pointer={{ ...pointer, reference: stopped.operation_ref }}
      active
    />,
  );
  expect(await screen.findByTestId("coach-operation-card")).toHaveTextContent("评测已停止");
  expect(
    operationSummary({
      operation_ref: {
        kind: "tool_requirement",
        requirement_id: `ctrq_${"c".repeat(32)}`,
        observation_id: null,
      },
      domain_result: {
        requirement: {
          operation: "Read the quarterly dataset",
          reason: "The current connection cannot read the required dataset.",
        },
      },
    }),
  ).toMatchObject({
    status: "requirement_recorded",
    title: "Read the quarterly dataset",
    summary: "The current connection cannot read the required dataset.",
  });
});

it("opens the saved Skill from the receipt card", async () => {
  vi.mocked(readCoachOperation).mockResolvedValue({
    ...receipt,
    domain_result: {
      outcome: "succeeded",
      original_target: { kind: "personal_skill", name: "weekly-summary", id: null, revision: null },
      result: { target: { kind: "personal_skill", name: "weekly-summary", id: "sk_saved", revision: "sha256:abc" } },
    },
  });
  const onOpen = vi.fn();
  renderIntl(<CoachOperationView pointer={pointer} active onOpen={onOpen} />);
  fireEvent.click(await screen.findByRole("button", { name: "打开技能" }));
  expect(onOpen).toHaveBeenCalledWith({
    kind: "resource",
    reference: { kind: "skill", source_kind: "user", id: "sk_saved", revision: "sha256:abc" },
  });
});

it("maps every receipt kind to the action the spec names, and none for Tool requirements", () => {
  expect(operationTarget({
    operation_ref: { kind: "plugin_save", operation_id: `cpw_${"a".repeat(64)}` },
    domain_result: { state: "failed", original_target: { kind: "team_plugin", id: "tcap_1", version: 2, revision: 3 } },
  })).toEqual({ action: "open_plugin", target: { kind: "resource", reference: { kind: "capability", id: "tcap_1", revision: "3" } } });
  expect(operationTarget({
    operation_ref: { kind: "agent_change", agent_id: "osa_a", proposal_id: "acp_1" }, domain_result: {},
  })).toMatchObject({ action: "review_in_agent", target: { kind: "resource", reference: { kind: "agent", id: "osa_a" } } });
  expect(operationTarget({
    operation_ref: { kind: "agent_change", agent_id: "osa_a", proposal_id: "acp_2" }, domain_result: { proposal: { status: "accepted" } },
  })).toMatchObject({ action: "view_in_agent", target: { kind: "resource", reference: { kind: "agent", id: "osa_a" } } });
  expect(operationTarget({
    operation_ref: { kind: "ci_delegation", agent_id: "osa_a", delegation_id: `acig_${"b".repeat(32)}` }, domain_result: {},
  })?.action).toBe("view_in_agent");
  expect(operationTarget({
    operation_ref: { kind: "draft_suggestion", draft_id: "sad_a", suggestion_id: "sas_000000000001" }, domain_result: {},
  })).toEqual({ action: "review_in_draft", target: { kind: "agent_draft", draftId: "sad_a" } });
  expect(operationTarget({
    operation_ref: { kind: "tool_requirement", requirement_id: `ctrq_${"c".repeat(32)}`, observation_id: null }, domain_result: {},
  })).toBeNull();
  expect(operationTarget({ ...receipt, domain_result: { outcome: "failed", original_target: { name: "x", id: null } } })).toBeNull();
});

it("hides the card action when the host cannot open resources", async () => {
  const reference = { kind: "agent_change" as const, agent_id: "osa_a", proposal_id: "acp_1" };
  vi.mocked(readCoachOperation).mockResolvedValue({ operation_ref: reference, domain_result: { proposal: { status: "pending" } } });
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference }} active />);
  await screen.findByTestId("coach-operation-card");
  expect(screen.queryByRole("button", { name: "去智能体中审阅" })).toBeNull();
});
