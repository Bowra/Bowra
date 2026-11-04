"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { useResultsCapabilitiesSnapshot } from "@/lib/use-results-capabilities";

export type HtmlPreviewMode = "static" | "interactive";

export function useHtmlPreviewMode(sourceKey?: string) {
  const capabilities = useResultsCapabilitiesSnapshot();
  const available = capabilities.interactiveHtml === true;
  const [selection, select] = useState<HtmlPreviewMode>("interactive");
  const [paused, setPaused] = useState(false);
  useEffect(() => setPaused(false), [sourceKey]);
  return {
    available, mode: available ? selection : "static" as HtmlPreviewMode, paused,
    select: (mode: HtmlPreviewMode) => { setPaused(false); select(mode); },
    togglePaused: () => setPaused((value) => !value),
  };
}

export function HtmlPreviewModeControl({ value, onChange }: {
  value: HtmlPreviewMode; onChange: (mode: HtmlPreviewMode) => void;
}) {
  const t = useTranslations("artifact_viewer");
  return <SegmentedControl value={value} onValueChange={onChange} aria-label={t("html_view_mode")}>
    <Segment value="interactive">{t("html_interactive")}</Segment>
    <Segment value="static">{t("html_reading")}</Segment>
  </SegmentedControl>;
}
