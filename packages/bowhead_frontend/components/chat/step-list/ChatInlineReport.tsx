"use client";

/**
 * The report card under a chat answer (chat running state R2.2): it replaces the appended sentence
 * "完整报告见画布。" for Turns that added or revised report sections.
 *
 * Same shell as a delivered file's card: the report's title, what this Turn changed ("本轮新增 4 节 · 修改 1 节")
 * with the changed sections' titles, and "打开到这一节". The whole card opens the report in Results at the first
 * changed section. Built from `lead.final.payload.report_changes`, so live, reloaded and replayed Turns agree.
 */
import { useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { FileText } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard } from "@/components/ui/card";
import { getCanvasSnapshot, selectReportTitle, subscribeCanvas } from "@/lib/canvas-store";
import { requestCanvasResultOpen } from "@/lib/canvas-workspace-focus";
import { reportChangeGroup, type ReportChangeCounts, type ReportChanges } from "@/lib/report-changes";

/** How many changed sections the card names before it counts the rest. */
const NAMED_SECTIONS = 3;

const reportTitle = () => selectReportTitle(getCanvasSnapshot());

type Translate = ReturnType<typeof useTranslations<"attachment">>;

/** "4 节、2 张图", or "" when only the report's frame changed. */
function counted(counts: ReportChangeCounts, t: Translate): string {
  return [
    counts.sections > 0 ? t("report_card_n_sections", { count: counts.sections }) : null,
    counts.figures > 0 ? t("report_card_n_figures", { count: counts.figures }) : null,
  ].filter((part): part is string => part !== null).join(t("report_card_item_sep"));
}

/** "本轮新增 4 节、2 张图 · 修改 1 节"; "本轮更新了报告" when only the title or sources changed. */
export function reportChangeSummary(changes: ReportChanges, t: Translate): string {
  const added = counted(changes.added, t);
  const updated = counted(changes.updated, t);
  const parts = [
    added ? t("report_card_added", { what: added }) : null,
    updated ? t("report_card_updated", { what: updated }) : null,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? t("report_card_changes", { parts: parts.join(" · ") }) : t("report_card_changed");
}

export function ChatInlineReport({ changes }: { changes: ReportChanges }) {
  const t = useTranslations("attachment");
  const title = useSyncExternalStore(subscribeCanvas, reportTitle, () => "").trim();
  const heading = title ? t("report_card_title", { title }) : t("report_card_untitled");
  const summary = reportChangeSummary(changes, t);
  // Chapter titles only: chart titles and the report's frame read oddly in a list of sections.
  const named = changes.sections
    .filter((section) => reportChangeGroup(section.kind) === "sections")
    .map((section) => section.title)
    .filter(Boolean);
  const total = changes.added.sections + changes.updated.sections;
  const sections = named.length === 0
    ? ""
    : named.length > NAMED_SECTIONS || total > named.length
      ? t("report_card_sections_more", { titles: named.slice(0, NAMED_SECTIONS).join(t("report_card_section_sep")), count: total })
      : t("report_card_sections", { titles: named.join(t("report_card_section_sep")) });
  const open = () => requestCanvasResultOpen({ type: "report", sectionId: changes.firstSectionId }, { entryPoint: "chat" });

  return (
    <ChatCard
      intent="info"
      className="chat-deliverable chat-report-card"
      data-testid="chat-inline-report"
      media={<LucideGlyph icon={FileText} size={20} aria-hidden="true" />}
      title={heading}
      onActivate={open}
      activateLabel={t("report_card_open_label", { title: heading, changes: summary })}
      // The card's own button carries the action for keyboards and screen readers; these words beside it are a pointer
      // target for the same action (116, 1.7.89: a click on them did nothing).
      trailing={
        <span className="chat-report-card__go" aria-hidden="true" data-testid="chat-inline-report-go" onClick={open}>
          {t("report_card_open")}
        </span>
      }
    >
      <span className="ui-card__note">{summary}</span>
      {sections ? <span className="ui-card__note chat-report-card__sections">{sections}</span> : null}
    </ChatCard>
  );
}
