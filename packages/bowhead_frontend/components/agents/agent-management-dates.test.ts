/**
 * Agent management dates use the shared list-date rule, so the value can be relative ("3d ago", "Yesterday") or
 * absolute ("Oct 2"). The templates are "label: value" so both read correctly in every locale — "Archiviert am vor
 * 3 Tagen" / "Archivé le hier" (date-only prepositions) must not come back.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";
import { formatListDate } from "@/lib/locale";

const NOW = new Date("2026-10-09T12:00:00Z");
const CATALOGS = { "en-US": enMessages, "de-DE": deMessages, "fr-FR": frMessages, "zh-CN": zhMessages } as const;

function render(locale: keyof typeof CATALOGS, key: "release_archived_at" | "drafts_updated", iso: string) {
  const messages = CATALOGS[locale];
  const common = createTranslator({ locale, messages, namespace: "common" });
  const t = createTranslator({ locale, messages, namespace: "agents_marketplace" });
  const date = formatListDate(iso, locale, {
    justNow: () => common("list_time_just_now"),
    minutesAgo: (count) => common("list_time_minutes_ago", { count }),
    hoursAgo: (count) => common("list_time_hours_ago", { count }),
    yesterday: () => common("list_time_yesterday"),
    daysAgo: (count) => common("list_time_days_ago", { count }),
  }, NOW, "UTC");
  return t(key, { date: date!.label });
}

describe("agent management date templates", () => {
  it("reads correctly with a relative value", () => {
    expect(render("en-US", "release_archived_at", "2026-10-06T12:00:00Z")).toBe("Archived: 3d ago");
    expect(render("de-DE", "release_archived_at", "2026-10-06T12:00:00Z")).toBe("Archiviert: vor 3 Tagen");
    expect(render("fr-FR", "release_archived_at", "2026-10-06T12:00:00Z")).toBe("Archivé : il y a 3 j");
    expect(render("zh-CN", "release_archived_at", "2026-10-06T12:00:00Z")).toBe("归档：3 天前");
    expect(render("en-US", "release_archived_at", "2026-10-08T12:00:00Z")).toBe("Archived: Yesterday");
    expect(render("fr-FR", "drafts_updated", "2026-10-08T12:00:00Z")).toBe("Dernier enregistrement : Hier");
    expect(render("de-DE", "drafts_updated", "2026-10-09T09:00:00Z")).toBe("Zuletzt gespeichert: vor 3 Std.");
  });

  it("reads correctly with an absolute value", () => {
    expect(render("en-US", "release_archived_at", "2026-09-25T12:00:00Z")).toBe("Archived: Sep 25");
    expect(render("de-DE", "release_archived_at", "2026-09-25T12:00:00Z")).toBe("Archiviert: 25. Sept.");
    expect(render("fr-FR", "release_archived_at", "2026-09-25T12:00:00Z")).toBe("Archivé : 25 sept.");
    expect(render("en-US", "drafts_updated", "2026-09-25T12:00:00Z")).toBe("Last saved: Sep 25");
  });

  it("has no date-only preposition before the value", () => {
    for (const locale of ["de-DE", "fr-FR"] as const) {
      const t = CATALOGS[locale].agents_marketplace;
      expect(`${t.release_archived_at} ${t.drafts_updated}`).not.toMatch(/\b(am|le) \{date\}/);
    }
  });
});
