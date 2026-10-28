import type { ReportSection } from "@/lib/types";
import { Markdown } from "@/lib/md";
import { UnverifiedNumbersNote } from "../unverified-numbers-note";

type E = Extract<ReportSection, { kind: "narrative" }>;

/** Sprint κ Phase C: render typewriter cursor when this narrative section is
 * currently streaming (per PRD r2 §4.4.2 — only the most recently-streaming
 * section blinks; others render as static once done). */
export function NarrativeSection({
  data, isStreaming = false,
}: { data: E; isStreaming?: boolean }) {
  if (!data.markdown && !isStreaming) return null;
  return (
    <div className="v2-report-section">
      <Markdown markdown={data.markdown ?? ""} enableCardCitations />
      {isStreaming ? (
        <span
          aria-hidden
          className="kappa-cursor"
          style={{
            display: "inline-block",
            width: 2,
            height: "1.1em",
            background: "currentColor",
            verticalAlign: "text-bottom",
            marginLeft: 1,
            animation: "kappa-cursor-blink 1s step-end infinite",
          }}
        />
      ) : null}
      <UnverifiedNumbersNote numbers={data.unverified_numbers} />
    </div>
  );
}
