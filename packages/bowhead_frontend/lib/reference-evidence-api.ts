import { appFetch } from "@/lib/base-path";

const kinds = ["agent", "skill", "capability", "mcp_source", "mcp_tool", "mcp_prompt"] as const;
export type ReferenceKind = typeof kinds[number];

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid usage record");
  return value as Record<string, unknown>;
}

export interface QueuedReferenceEvidence {
  state: "queued" | "cancelled" | "materialized";
  references: { name: string | null; kind: ReferenceKind; hidden: boolean; parameters: string | null }[];
  text: string | null;
  truncated: boolean;
  partial: boolean;
}

export async function fetchQueuedReferenceEvidence(
  workspaceId: string, sessionId: string, submissionId: string, includeContent: boolean, signal: AbortSignal,
): Promise<QueuedReferenceEvidence | null> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  if (includeContent) query.set("include_content", "true");
  const response = await appFetch(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/reference-queue/${encodeURIComponent(submissionId)}/evidence?${query}`, {
    signal, cache: "no-store",
  });
  if (response.status === 404 || response.status === 403) return null;
  if (!response.ok) throw new Error("Queued source unavailable");
  const data = record(await response.json());
  const source = record(data.source_locator);
  const queue = record(data.queue);
  if (data.facts_version !== 1 || source.kind !== "queued_submission" || source.submission_id !== submissionId
    || source.accepted_parent_turn_id !== data.turn_id || !["queued", "cancelled", "materialized"].includes(String(queue.state))
    || !Array.isArray(data.references) || data.references.length > 100) throw new Error("Unsupported queued source");
  const content = includeContent && data.content_included === true;
  let truncated = false;
  const references = data.references.map(raw => {
    const item = record(raw);
    if (!kinds.includes(item.resource_kind as ReferenceKind)) throw new Error("Unsupported queued resource");
    const visible = item.resource_visibility === "visible";
    const parameters = visible && content && item.parameters != null ? JSON.stringify(item.parameters, null, 2) : null;
    if (parameters && parameters.length > 4096) truncated = true;
    return {
      name: visible && typeof item.label === "string" ? item.label.slice(0, 256) : null,
      kind: item.resource_kind as ReferenceKind, hidden: !visible, parameters: parameters?.slice(0, 4096) ?? null,
    };
  });
  const text = content && Array.isArray(source.text_spans) ? source.text_spans.map(raw => {
    const span = record(raw);
    return typeof span.text === "string" ? span.text : "";
  }).join("") : null;
  if (text && text.length > 16384) truncated = true;
  return {
    state: queue.state as QueuedReferenceEvidence["state"], references, text: text?.slice(0, 16384) ?? null, truncated,
    partial: record(data.evidence_completeness).status !== "complete",
  };
}
