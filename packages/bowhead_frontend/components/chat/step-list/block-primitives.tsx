/**
 * block-primitives.tsx — shared bits for the agent-block trace:
 * the status glyph and the disclosure chevron. Used by AgentBlock + Step.
 */

import {
  ChevronRight as LucideChevronRight,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";
import { LoaderCircle } from "lucide-react";

export type StepStatus = "done" | "run" | "bail" | "fail" | "part" | "pend";

export function StatusGlyph({ status }: { status: StepStatus }) {
  switch (status) {
    case "done":
      return <span className="g g-done"><CharIcon ch="✓" /></span>;
    case "fail":
      return <span className="g g-fail"><CharIcon ch="✕" /></span>;
    case "bail":
      return <span className="g g-bail"><CharIcon ch="⚠" /></span>;
    case "part":
      return <span className="g g-part"><CharIcon ch="⚠" /></span>;
    case "pend":
      return <span className="g g-pend"><CharIcon ch="○" /></span>;
    case "run":
      return (
        <span className="g g-run">
          <span className="spin"><CharIcon ch="⟳" icon={LoaderCircle} /></span>
        </span>
      );
    default:
      return <span className="g g-pend">·</span>;
  }
}

export function Chevron({ open, className }: { open: boolean; className?: string }) {
  return (
    <LucideGlyph icon={LucideChevronRight} size={12} className={`${className ?? ""}${open ? " open" : ""}`} aria-hidden="true" />
  );
}
