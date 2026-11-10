// @vitest-environment node
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

import { localizeNotification, notificationTypeLabel } from "./notification-copy";
import { NOTIFICATION_TYPES } from "./notification-types.generated";

const translators = {
  "en-US": createTranslator({ locale: "en-US", messages: en, namespace: "notifications" }) as never,
  "zh-CN": createTranslator({ locale: "zh-CN", messages: zh, namespace: "notifications" }) as never,
};

// Every parameter some type's copy reads, so each type can render its localized branch.
const metadata = {
  toolName: "crm_update",
  riskLevel: "high",
  jobName: "Daily sales",
  agentName: "Forecaster",
  requesterName: "Alice",
  resourceLabel: "Q3 report",
  actorName: "Bob",
  adminName: "Carol",
  skillLabel: "Pricing",
  decision: "approved",
  outcome: "approved",
  count: 3,
  createdCount: 2,
  finalState: "accepted",
  targetKind: "agent_def",
  candidateId: "fbc_1",
  decidedAt: "2026-11-08T09:30:00+00:00",
  level: "attention",
};

describe("registered notification types", () => {
  const types = Object.keys(NOTIFICATION_TYPES);

  it("covers the backend registry", () => {
    expect(types.length).toBeGreaterThan(0);
    expect(NOTIFICATION_TYPES["skill_approval_submitted"]?.actionRequired).toBe(true);
  });

  it.each(Object.entries(translators))("has localized copy and a type label for every type in %s", (_locale, t) => {
    const missingCopy: string[] = [];
    const missingLabel: string[] = [];
    for (const type of types) {
      const raw = { type, category: NOTIFICATION_TYPES[type]?.category, title: "RAW_TITLE", body: null, metadata };
      if (localizeNotification(raw, t, "en-US").title === "RAW_TITLE") missingCopy.push(type);
      const label = notificationTypeLabel(raw, t);
      if (label === (t as (key: string) => string)("type_update")) missingLabel.push(type);
    }
    // A type registered without a branch in notification-copy.ts would show its stored snapshot.
    expect(missingCopy).toEqual([]);
    expect(missingLabel).toEqual([]);
  });
});
