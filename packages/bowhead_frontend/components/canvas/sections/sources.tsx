"use client";

/** Viewer-safe report sources. Canonical IDs and provider metadata never render here. */

import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";
import { kbCitationHref } from "@/components/chat/step-list/LeadNarrative";
import {
  copyKbFields,
  isKbReportSource,
  isViewerSourceAlias,
  kbRefOfSource,
  type SourceRef,
} from "@/lib/canvas-pm-adapter/citation-types";
import { useKbReportSource } from "@/lib/canvas-pm-adapter/kb-report-source";
import { useSourceAccess } from "@/lib/source-access-registry";

type E = Extract<ReportSection, { kind: "sources" }>;
type Entry = E["entries"][number];

function sourceFromEntry(entry: Entry, index: number): SourceRef | undefined {
  const id = entry.source_alias || entry.id;
  if (!isViewerSourceAlias(id)) return undefined;
  const kept = entry.access_state === "linked_restricted" || entry.access_state === "linked_missing"
    ? entry.access_state
    : null;
  const source: SourceRef = {
    id,
    source_alias: id,
    access_state: entry.access_state === "linked_accessible" ? "linked_accessible" : kept ?? "linked_unavailable",
    generic_type: entry.generic_type ?? "other_source",
    ordinal: entry.ordinal ?? index + 1,
    safe_label: entry.safe_label,
    safe_query: entry.safe_query,
    safe_preview_url: entry.safe_preview_url,
    safe_url: entry.safe_url,
  };
  copyKbFields(entry as unknown as Record<string, unknown>, source);
  return source;
}

/** KB source row (design 08 · D note): accessible → current filename, location, original excerpt, "在知识库中打开";
 *  restricted → "该来源仅对有权限的成员可见"; unavailable → "该来源已失效". Backend resolves states/fields for the viewer;
 *  no artifact probing here (KB sources have no artifact byte exit). */
function KbSourceRow({ source, ordinal }: { source: SourceRef; ordinal: number }) {
  const tStep = useTranslations("step_list");
  const kb = kbRefOfSource(source);
  const { state, location, warning } = useKbReportSource(kb);
  if (!kb) {
    const restricted = source.access_state === "linked_restricted";
    return (
      <li className="v2-sources-item" data-source-access={source.access_state}>
        <span className="v2-sources-tool">[{ordinal}]</span>
        <span className="v2-sources-sep">·</span>
        <span className="v2-sources-query">{restricted ? tStep("cite_kb_restricted") : tStep("cite_state_missing")}</span>
      </li>
    );
  }
  return (
    <li className="v2-sources-item v2-sources-kb" data-source-access="linked_accessible">
      <span className="v2-sources-tool">[{ordinal}]</span>
      <span className="v2-sources-sep">·</span>
      <span className="v2-sources-kb-body">
        <span className="v2-sources-query">{source.safe_label || tStep("cite_kb_prefix")}</span>
        <span className="v2-sources-kb-loc">{location}</span>
        {warning && <span role="note">{warning}</span>}
        {state.status === "ready" ? (
          <span className="v2-sources-kb-quote">{state.content}</span>
        ) : state.status === "empty" ? (
          <span className="v2-sources-kb-note">{tStep("cite_kb_updated")}</span>
        ) : state.status === "error" ? (
          <span className="v2-sources-kb-note">{tStep("cite_kb_unavailable")}</span>
        ) : null}
        <a className="v2-sources-kb-open" href={kbCitationHref(kb)}>{tStep("cite_kb_open")}</a>
      </span>
    </li>
  );
}

function SourceRow({ entry, index }: { entry: Entry; index: number }) {
  const t = useTranslations("sources");
  // 116 dogfood 1.0.95 P3: descriptive means identity-less descriptive source (backend emits this state only for
  // entries lacking a gateable object, with author text); render plain text, outside access registry,
  // without misleading "Source is currently unavailable". ⚠ Semantically distinct from linked_unavailable+
  // safe_label: the latter is stale projection after permission revocation; continue stripping labels
  // (enforced by existing safety tests), so distinguish by access_state rather than label presence.
  const source = entry.access_state === "descriptive" ? undefined : sourceFromEntry(entry, index);
  const kbSource = isKbReportSource(source);
  const access = useSourceAccess(kbSource ? undefined : source);
  const ordinal = entry.ordinal ?? index + 1;
  if (kbSource && source) return <KbSourceRow source={source} ordinal={ordinal} />;
  if (entry.access_state === "descriptive") {
    return (
      <li className="v2-sources-item" data-source-access="descriptive">
        <span className="v2-sources-tool">[{ordinal}]</span>
        <span className="v2-sources-sep">·</span>
        <span className="v2-sources-query">{entry.safe_label || entry.safe_query}</span>
      </li>
    );
  }
  const accessible = access?.status === "linked_accessible";
  const label = accessible
    ? access.source.safe_label || access.source.safe_query || t("generic_source")
    : access?.status === "checking" ? t("checking") : t("unavailable_source");
  return (
    <li className="v2-sources-item" data-source-access={access?.status ?? "linked_unavailable"}>
      <span className="v2-sources-tool">[{ordinal}]</span>
      <span className="v2-sources-sep">·</span>
      <span className="v2-sources-query">{label}</span>
    </li>
  );
}

export function SourcesSection({ data }: { data: E }) {
  const t = useTranslations("sources");
  if (!data.entries?.length) return null;
  return (
    <aside className="v2-sources">
      <div className="v2-sources-head" title={t("readonly_hint")}>
        {data.label || t("head")}
      </div>
      <ol className="v2-sources-list">
        {data.entries.map((entry, index) => (
          <SourceRow key={entry.source_alias || entry.id || index} entry={entry} index={index} />
        ))}
      </ol>
    </aside>
  );
}
