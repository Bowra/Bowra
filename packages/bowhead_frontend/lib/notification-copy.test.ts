import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

import { localizeNotification, notificationTypeLabel } from "./notification-copy";

const tEn = createTranslator({ locale: "en-US", messages: en, namespace: "notifications" }) as never;
const tZh = createTranslator({ locale: "zh-CN", messages: zh, namespace: "notifications" }) as never;

const expiring = {
  type: "memory.candidate",
  category: "memory",
  title: "记忆建议即将过期",
  body: "有 3 条记忆建议即将过期（超过 30 天未处理会自动忽略），去看看要不要记住",
  metadata: { expiringCount: 3 },
};

describe("localizeNotification", () => {
  it("uses the same neutral copy for every way an access request stops being active", () => {
    const neutralEn = "This request is no longer active. Request access again if you still need it.";
    const expired = { type: "access_request_expired", title: "x", body: null, metadata: { outcome: "expired" } };
    expect(localizeNotification(expired, tEn, "en-US")).toEqual({ title: neutralEn, body: null });
    expect(localizeNotification(expired, tZh, "zh-CN").title).toBe("这条申请已不再有效，如仍需访问可重新申请。");
    const expansionStale = { type: "access_request_decided", title: "x", body: null, metadata: { outcome: "stale", decidedBy: "manager" } };
    expect(localizeNotification(expansionStale, tEn, "en-US").title).toBe(neutralEn);
    // the legacy flow keeps its own wording
    const legacyStale = { type: "access_request_decided", title: "x", body: null, metadata: { outcome: "stale" } };
    expect(localizeNotification(legacyStale, tEn, "en-US").title).toBe("The requested content no longer exists");
    expect(notificationTypeLabel({ type: "access_request_expired" }, tEn)).toBe("Request no longer active");
  });

  it("shows the requester the manager's note but never who decided", () => {
    const decided = (metadata: Record<string, unknown>, actionUrl: string | null = null) => ({
      type: "access_request_decided",
      title: "x",
      body: "x",
      actionUrl,
      metadata: { requestId: "r1", decidedBy: "manager", ...metadata },
    });
    expect(localizeNotification(decided({ outcome: "rejected", note: "请用对外版" }), tZh, "zh-CN")).toEqual({
      title: "访问申请未通过",
      body: "管理者附言：请用对外版",
    });
    expect(localizeNotification(decided({ outcome: "approved" }, "/?session=s1"), tEn, "en-US")).toEqual({
      title: "Access request approved",
      body: "A manager approved your request. You can open it now.",
    });
    expect(localizeNotification(decided({ outcome: "approved_pending" }), tEn, "en-US")).toEqual({
      title: "Access request approved, taking effect soon",
      body: null,
    });
    const leaky = decided({ outcome: "approved", adminName: "Alice", actorName: "Alice" });
    const copy = localizeNotification(leaky, tEn, "en-US");
    expect(`${copy.title} ${copy.body ?? ""}`).not.toContain("Alice");
  });

  it("localizes escalation, digest and release-failure notices for approvers and owners", () => {
    const meta = { requestId: "r1", requesterName: "Ann", resourceLabel: "Q3" };
    expect(localizeNotification({ type: "access_requested_escalated", title: "x", body: "x", metadata: meta }, tZh, "zh-CN")).toEqual({
      title: "有访问申请等待处理",
      body: "Ann 申请访问「Q3」，负责人 72 小时内未处理",
    });
    expect(localizeNotification({ type: "access_requests_digest", title: "x", body: null, metadata: { count: 4 } }, tEn, "en-US")).toEqual({
      title: "You have 4 access requests to review",
      body: null,
    });
    expect(localizeNotification({ type: "access_request_release_failed", title: "x", body: "x", metadata: meta }, tEn, "en-US")).toEqual({
      title: "An approved access request didn't take effect",
      body: "The release adding Ann to “Q3” didn't go live",
    });
    expect(notificationTypeLabel({ type: "access_requests_digest" }, tEn)).toBe("Access request");
    expect(notificationTypeLabel({ type: "access_request_release_failed" }, tEn)).toBe("Request not applied");
  });

  it("rewrites the expiring memory notice in the viewer's language", () => {
    const copy = localizeNotification(expiring, tEn, "en-US");
    expect(copy.title).toBe("Memory suggestions expiring soon");
    expect(copy.body).toContain("3 memory suggestions are about to expire");
    expect(localizeNotification(expiring, tZh, "zh-CN")).toEqual({ title: expiring.title, body: expiring.body });
  });

  it("tells the new-suggestion notice apart from the expiring one by metadata", () => {
    const copy = localizeNotification(
      { type: "memory.candidate", title: "有新的记忆建议待确认", body: "…", metadata: { createdCount: 1, preview: "Use fiscal year" } },
      tEn,
      "en-US",
    );
    expect(copy).toEqual({
      title: "New memory suggestions to review",
      body: "Learned 1 suggestion from recent conversations. Decide which to keep: Use fiscal year",
    });
  });

  it("uses the admin copy for a team-level model pause and the owner copy per job", () => {
    const base = { type: "scheduled.paused", category: "async_job", title: "定时任务已暂停", body: "…" };
    const meta = { jobName: "Weekly sales", reason: "team_model_unconfigured" };
    expect(localizeNotification({ ...base, subjectType: "team", metadata: meta }, tEn, "en-US").title).toBe(
      "Scheduled tasks paused: no team model configured",
    );
    expect(localizeNotification({ ...base, subjectType: "scheduled_job", metadata: meta }, tEn, "en-US").body).toBe(
      "“Weekly sales” was paused: it needs a team model, which a team admin must configure before it can resume.",
    );
    expect(
      localizeNotification({ ...base, metadata: { jobName: "Weekly sales", reason: "scheduled_recipe_lost" } }, tEn, "en-US").title,
    ).toBe("Scheduled task paused: bound workflow unavailable");
  });

  it("localizes shares and falls back field by field on rows written before metadata carried names", () => {
    expect(
      localizeNotification(
        { type: "content_shared", category: "share", title: "Ann 分享了「Q3」", body: null, metadata: { actorName: "Ann", resourceLabel: "Q3" } },
        tEn,
        "en-US",
      ).title,
    ).toBe("Ann shared “Q3” with you");
    const legacy = { type: "access_requested", title: "有访问申请待处理", body: "Ann 申请访问「Q3」", metadata: { requestId: "r1" } };
    expect(localizeNotification(legacy, tEn, "en-US")).toEqual({ title: "Access request to review", body: legacy.body });
  });

  it("names the Agent in release titles and falls back for rows without a name snapshot", () => {
    const base = {
      type: "agent.release.updated",
      category: "updates",
      title: "An Agent you hired was updated",
      body: "Open the Agent to review the current version and availability.",
    };
    const named = { ...base, metadata: { agent_id: "osa_1", agentName: "Sales Analyst" } };
    expect(localizeNotification(named, tEn, "en-US").title).toBe("Sales Analyst, an Agent you hired, was updated");
    expect(localizeNotification(named, tZh, "zh-CN").title).toBe("你雇用的 Agent「Sales Analyst」已更新");
    expect(
      localizeNotification({ ...named, type: "agent.release.approval_required" }, tEn, "en-US").title,
    ).toBe("Release of Sales Analyst needs review");
    expect(localizeNotification({ ...base, metadata: { agent_id: "osa_1" } }, tEn, "en-US").title).toBe(
      "An Agent you hired was updated",
    );
    expect(localizeNotification({ ...base, metadata: { agent_id: "osa_1" } }, tZh, "zh-CN").title).toBe("你雇用的 Agent 已更新");
  });

  it("tells the Agent Owner what arrived, naming the Agent and counts only", () => {
    const notice = (type: string, metadata: Record<string, unknown>) => ({
      type,
      category: "updates",
      title: "Raw English title",
      body: "Raw English body",
      metadata: { agent_id: "osa_1", agentName: "Sales Analyst", ...metadata },
    });
    expect(localizeNotification(notice("agent.version.proposal_received", { groupCount: 2 }), tEn, "en-US")).toEqual({
      title: "Sales Analyst has a proposal waiting",
      body: "2 proposed changes are waiting for your review.",
    });
    expect(localizeNotification(notice("agent.version.proposal_received", { groupCount: 1 }), tZh, "zh-CN")).toEqual({
      title: "「Sales Analyst」有待处理的提案",
      body: "提案里有 1 项修改等你审阅。",
    });
    expect(localizeNotification(notice("agent.version.result_ready", { readiness: "ready" }), tEn, "en-US").title).toBe(
      "Sales Analyst is ready to release",
    );
    expect(localizeNotification(notice("agent.version.result_ready", { readiness: "ready_with_risks" }), tEn, "en-US").title).toBe(
      "Sales Analyst is ready to release with risks",
    );
    expect(localizeNotification(notice("agent.version.result_blocked", { reasonCount: 1 }), tEn, "en-US").body).toBe(
      "1 check needs attention. Open the Agent to see what failed.",
    );
    expect(localizeNotification(notice("agent.version.result_blocked", {}), tEn, "en-US").body).toBe("Open the Agent to see what failed.");
    expect(localizeNotification(notice("agent.version.candidate_prepared", {}), tZh, "zh-CN").title).toBe("「Sales Analyst」已准备好新版本");
    // Without a name snapshot the persisted English stays, rather than a title with a hole in it.
    const unnamed = { ...notice("agent.version.result_unknown", {}), metadata: { agent_id: "osa_1" } };
    expect(localizeNotification(unnamed, tEn, "en-US")).toEqual({ title: "Raw English title", body: "Raw English body" });
    expect(notificationTypeLabel({ type: "agent.version.result_unknown" }, tEn)).toBe("Agent version");
  });

  it("keeps unknown types verbatim", () => {
    const item = { type: "something.new", title: "Raw", body: "Body" };
    expect(localizeNotification(item, tEn, "en-US")).toEqual({ title: "Raw", body: "Body" });
  });
});

describe("notificationTypeLabel", () => {
  it("never shows a raw category code", () => {
    expect(notificationTypeLabel({ type: "routine.delivered", category: "async_job" }, tEn)).toBe("Scheduled result");
    expect(notificationTypeLabel({ type: "future.job", category: "async_job" }, tEn)).toBe("Scheduled task");
    expect(notificationTypeLabel({ type: "future.share", category: "share" }, tEn)).toBe("Share");
    expect(notificationTypeLabel({ type: "x", category: "unknown_cat" }, tEn)).toBe("Update");
  });
});
