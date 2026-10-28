"use client";

import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";
import { toolLabel } from "@/lib/tool-labels";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { CharIcon } from "@/components/char-icon";

// 2026-09-05: renamed from sql-block.tsx; it has **always** rendered code_block,
// while sql_block was a historical alias of the same component (writers emit only code_block since Sprint ι).
// With the alias retired, only one kind remains here.
type Props = { data: Extract<ReportSection, { kind: "code_block" }> };
type ArtifactRef = { artifact_id: string; filename?: string; mime?: string };

const LANG_LABEL_KEY: Record<string, string> = {
  python: "code_block.lang_label_python",
  sql: "code_block.lang_label_sql",
  shell: "code_block.lang_label_shell",
};

function renderMetaValue(key: string, value: string): string {
  // source values are machine tool names (bi.ask_question, etc.); convert to human-readable text.
  if (key === "source") {
    return toolLabel(value).name;
  }
  return value;
}

function artifactRefs(ids: string[], refs?: ArtifactRef[]): ArtifactRef[] {
  if (refs?.length) return refs.filter((ref) => ref.artifact_id);
  return ids.map((id) => ({ artifact_id: id }));
}

function artifactLabel(ref: ArtifactRef): string {
  return ref.filename || ref.artifact_id;
}

/** Sprint ι: dual-kind component. Renders both legacy `sql_block` (sql field)
 *  and new `code_block` (code field + lang badge + summary + chips). */
export function CodeBlockSection({ data }: Props) {
  const t = useTranslations();
  // Sprint ι: lang prop determines header badge + syntax highlighting.
  // Legacy sql_block: alias to lang="sql".
  const lang: "sql" | "python" | "shell" =
    "lang" in data && data.lang ? data.lang : "sql";
  const codeText = data.code ?? "";
  const meta = data.meta ?? {};
  if (!codeText) return null;

  const sqlLabelOf = (k: string): string => {
    if (k === "source") return t("sql_block.meta_source");
    if (k === "query") return t("sql_block.meta_query");
    return k;
  };
  const metaEntries = Object.entries(meta);
  const langLabel = t(LANG_LABEL_KEY[lang] ?? "code_block.lang_label_sql");

  // Sprint ι (PRD §4.A.3): summary line composed via i18n + summary_payload.
  const summary =
    "summary_payload" in data && data.summary_payload
      ? t("code_block.summary_format", {
          n_lines: data.summary_payload.n_lines,
          statement_summary: data.summary_payload.statement_summary,
          dur_ms: data.summary_payload.dur_ms,
        })
      : "";
  const stdoutPreview =
    "stdout_preview" in data ? data.stdout_preview ?? "" : "";
  const inputIds =
    "input_artifact_ids" in data ? data.input_artifact_ids ?? [] : [];
  const outputIds =
    "output_artifact_ids" in data ? data.output_artifact_ids ?? [] : [];
  const inputArtifacts = artifactRefs(
    inputIds,
    "input_artifacts" in data ? data.input_artifacts : undefined,
  );
  const outputArtifacts = artifactRefs(
    outputIds,
    "output_artifacts" in data ? data.output_artifacts : undefined,
  );

  return (
    <details className={`v2-code-block v2-code-block--${lang} v2-sql-block`} open>
      <summary className="v2-code-block-header v2-sql-block-header">
        <span className="lang-badge">{langLabel}</span>
        {summary && <span className="summary"> · {summary}</span>}
      </summary>
      {metaEntries.length ? (
        <div className="v2-sql-block-meta v2-code-block-meta">
          {metaEntries.map(([k, v]) => (
            <span key={k} className="pill">
              {sqlLabelOf(k)}: {renderMetaValue(k, v)}
            </span>
          ))}
        </div>
      ) : null}
      <pre>
        <code className={`language-${lang}`}>{codeText}</code>
      </pre>
      {stdoutPreview ? (
        <details className="v2-code-block-stdout">
          <summary>{t("code_block.stdout_label")}</summary>
          <pre>
            <code>{stdoutPreview}</code>
          </pre>
        </details>
      ) : null}
      {inputArtifacts.length > 0 || outputArtifacts.length > 0 ? (
        <div className="v2-code-block-chips">
          {inputArtifacts.map((ref, i) => (
            <button
              key={`in-${i}-${ref.artifact_id}`}
              type="button"
              onClick={() => requestCanvasDataFocus(ref.artifact_id, {
                entryPoint: "report",
                reportContext: {
                  label: artifactLabel(ref),
                  sectionId: data.id,
                },
              })}
              className="artifact-chip artifact-chip--input"
              title={ref.filename ? `${ref.filename} · ${ref.artifact_id}` : ref.artifact_id}
            >
              <CharIcon ch="←" /> {artifactLabel(ref)}
            </button>
          ))}
          {outputArtifacts.map((ref, i) => (
            <button
              key={`out-${i}-${ref.artifact_id}`}
              type="button"
              onClick={() => requestCanvasDataFocus(ref.artifact_id, {
                entryPoint: "report",
                reportContext: {
                  label: artifactLabel(ref),
                  sectionId: data.id,
                },
              })}
              className="artifact-chip artifact-chip--output"
              title={ref.filename ? `${ref.filename} · ${ref.artifact_id}` : ref.artifact_id}
            >
              <CharIcon ch="→" /> {artifactLabel(ref)}
            </button>
          ))}
        </div>
      ) : null}
    </details>
  );
}
