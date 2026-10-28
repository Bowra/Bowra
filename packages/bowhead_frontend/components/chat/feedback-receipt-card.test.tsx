/**
 * agent-feedback-loop batch 5 · suggestion outcome receipt card.
 *
 * Pin down failures that can happen silently:
 *  - Unknown terminal states and invalid timestamps must not expose **raw message keys** in chat.
 *    ★ next-intl **does not throw** in either case (verified in review; do not assume a blank screen):
 *    it falls back to rendering keys such as `feedback_receipt.state_superseded` / `feedback_receipt.meta_mine`
 *    and logs only an `IntlError` to the console. It does not crash, but this is still an incident.
 *  - 「知道了」 must call acknowledge(notification_id), not candidate_id —
 *    acknowledgement targets the **notification**; a wrong ID never marks it read, so the card reappears on every chat visit.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { FeedbackReceipt } from "@/lib/feedback-candidates-api";
import { FeedbackReceiptCard } from "./feedback-receipt-card";

afterEach(cleanup);

function _receipt(overrides: Partial<FeedbackReceipt> = {}): FeedbackReceipt {
  return {
    notification_id: "ntf_1",
    candidate_id: "fbc_1",
    target_label: "Example Data Analyst",
    final_state: "accepted",
    decided_at: "2026-09-11T03:20:00+00:00",
    mine: true,
    ...overrides,
  };
}

describe("FeedbackReceiptCard", () => {
  it("空列表不渲染", () => {
    const { container } = renderIntl(
      <FeedbackReceiptCard items={[]} actingId={null} acknowledge={vi.fn()} />,
    );
    expect(container.textContent).toBe("");
  });

  it("展示目标资产的展示名（服务端已兜底，前端不判空）", () => {
    renderIntl(
      <FeedbackReceiptCard items={[_receipt()]} actingId={null} acknowledge={vi.fn()} />,
    );
    expect(screen.getByText(/Example Data Analyst/)).toBeTruthy();
  });

  it("三种终态各有话说，联署与自提的署名不同", () => {
    const items = [
      _receipt({ notification_id: "n1", final_state: "accepted" }),
      _receipt({ notification_id: "n2", final_state: "rejected" }),
      _receipt({ notification_id: "n3", final_state: "expired", mine: false }),
    ];
    const { container } = renderIntl(
      <FeedbackReceiptCard items={items} actingId={null} acknowledge={vi.fn()} />,
    );
    expect(container.querySelectorAll(".ui-card")).toHaveLength(3);
    expect(container.textContent).toContain("已采纳");
    expect(container.textContent).toContain("未采纳");
    expect(container.textContent).toContain("你联署的");
  });

  it("未知终态被丢掉而不是让整张卡崩掉", () => {
    const { container } = renderIntl(
      <FeedbackReceiptCard
        items={[
          _receipt({ notification_id: "n1", final_state: "superseded" }),
          _receipt({ notification_id: "n2", final_state: "accepted" }),
        ]}
        actingId={null}
        acknowledge={vi.fn()}
      />,
    );
    expect(container.querySelectorAll(".ui-card")).toHaveLength(1);
  });

  it("全是未知终态时整张卡不渲染", () => {
    const { container } = renderIntl(
      <FeedbackReceiptCard
        items={[_receipt({ final_state: "open" })]}
        actingId={null}
        acknowledge={vi.fn()}
      />,
    );
    expect(container.textContent).toBe("");
  });

  it("时间戳解析不出来时，正文照渲，署名整行不出现（不得露裸消息键）", () => {
    // `decided_at` comes from backend `cand.decided_at or cand.updated_at`; malformed historical rows
    // (early writes / migration leftovers / malformed timezone strings) reach this path. next-intl **does not throw** on invalid Date:
    // It renders a raw key instead — users see `feedback_receipt.meta_mine` in chat.
    const { container } = renderIntl(
      <FeedbackReceiptCard
        items={[_receipt({ decided_at: "not-a-date" })]}
        actingId={null}
        acknowledge={vi.fn()}
      />,
    );
    expect(container.textContent).toContain("已采纳");
    expect(container.textContent).not.toContain("feedback_receipt.");
    expect(container.querySelector(".ui-card__row-meta")).toBeNull();
  });

  it("「知道了」传的是 notification_id（签收落点是通知，不是候选）", () => {
    const ack = vi.fn().mockResolvedValue(true);
    renderIntl(
      <FeedbackReceiptCard
        items={[_receipt({ notification_id: "ntf_42", candidate_id: "fbc_9" })]}
        actingId={null}
        acknowledge={ack}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(ack).toHaveBeenCalledWith("ntf_42");
  });

  it("有操作在途时按钮禁用", () => {
    renderIntl(
      <FeedbackReceiptCard items={[_receipt()]} actingId={"ntf_1"} acknowledge={vi.fn()} />,
    );
    expect(screen.getByRole("button").hasAttribute("disabled")).toBe(true);
  });
});
