/**
 * KpiVerifiedTick — verified tick corner badge for KPI values (design D1, report-refactor P1).
 * KPI values use formatInlineTokens' plain-string path (not citation Decoration); the component
 * renders the tick based on hasVerifiedNumMarker(raw val).
 */

import {
  Check as LucideCheck,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

export function KpiVerifiedTick() {
  return (
    <LucideGlyph icon={LucideCheck} size={11} className="rc-kpi-tick" aria-hidden />
  );
}
