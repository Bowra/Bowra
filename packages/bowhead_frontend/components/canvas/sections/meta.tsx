import type { ReportSection } from "@/lib/types";

type E = Extract<ReportSection, { kind: "meta" }>;

export function MetaSection({ data }: { data: E }) {
  if (!data.pills || !data.pills.length) return null;
  return (
    <div className="v2-report-meta">
      {data.pills.map((p, i) => (
        <span key={i} className="pill">
          {p}
        </span>
      ))}
    </div>
  );
}
