"use client";

/**
 * Shared list date (Agents & Reports Polish Spec §3a, D8): relative within seven days ("3h ago", "Yesterday",
 * "3d ago"), then month and day, adding the year only when it differs from the current one. The calendar and the
 * absolute text use the viewer's time zone (next-intl `useTimeZone()`); hover / long press shows the full date and
 * time through `title`, and `<time dateTime>` carries the machine-readable value.
 */

import { useLocale, useTimeZone, useTranslations } from "next-intl";

import { formatListDate, type ListDate } from "@/lib/locale";

/** Formatter for callers that need the text inside other copy (for example "Archived {date}"). */
export function useListDate(): (value: Date | string | null | undefined) => ListDate | null {
  const t = useTranslations("common");
  const locale = useLocale();
  const timeZone = useTimeZone();
  return (value) =>
    formatListDate(
      value,
      locale,
      {
        justNow: () => t("list_time_just_now"),
        minutesAgo: (count) => t("list_time_minutes_ago", { count }),
        hoursAgo: (count) => t("list_time_hours_ago", { count }),
        yesterday: () => t("list_time_yesterday"),
        daysAgo: (count) => t("list_time_days_ago", { count }),
      },
      new Date(),
      timeZone,
    );
}

export function ListTime({
  value,
  className,
  empty = "—",
}: {
  value: Date | string | null | undefined;
  className?: string;
  /** Shown when the value is missing or unparseable. */
  empty?: string;
}) {
  const format = useListDate();
  const date = format(value);
  if (!date) return <span className={className}>{empty}</span>;
  return (
    <time className={className} dateTime={date.dateTime} title={date.title}>
      {date.label}
    </time>
  );
}
