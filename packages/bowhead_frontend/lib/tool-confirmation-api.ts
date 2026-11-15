import { apiJson } from "@/lib/api-client";
import type { PersonalMcpConfirmation } from "@/lib/personal-mcp-api";
import type {
  ToolConfirmationCategory,
  ToolConfirmationChoice,
  ToolConfirmationKind,
} from "@/lib/sse-events";

/** An email waiting for its requester: the full message and attachment labels. */
export interface EmailSendConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  to: string;
  subject: string;
  body: string;
  attachments?: {
    artifact_id: string;
    filename: string;
    mime: string;
    size: number;
    /** File versions: the attached file has a newer version (the requester decides). */
    newer_version?: { id: string; version: number };
  }[];
  expires_at: string;
}

/** A Shopify content update waiting for its requester, with per-field before/after. */
export interface ShopifyContentConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  team_id: string;
  shop_domain: string;
  object_id: string;
  resource: "product" | "page";
  title: string;
  handle: string;
  state: string;
  edits: { field: string; before: string | null; after: string }[];
  expires_at: string;
}

/** A non-read Team MCP call waiting for the requester (FEATURE_TEAM_TOOL_CONFIRMATION). */
export interface TeamToolConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  team_id: string;
  source_name: string;
  tool_ref: string;
  tool_name: string;
  description: string;
  /** Effective category of the tool; older servers omit it, which reads as unknown. */
  category?: "external_write" | "destructive" | "unknown";
  arguments: Record<string, unknown>;
  expires_at: string;
}

/** One pending confirmation owned by the current user. */
export interface ToolConfirmationSummary {
  id: string;
  kind: ToolConfirmationKind;
  session_id: string;
  session_title: string | null;
  turn_id: string;
  step_id: string | null;
  title: string;
  source_label: string | null;
  agent_name: string | null;
  expires_at: string;
  /** Computed by the server; the card's only countdown anchor (never the local clock). */
  remaining_seconds: number;
}

interface ToolConfirmationDetailBase extends ToolConfirmationSummary {
  category: ToolConfirmationCategory | null;
  choices: ToolConfirmationChoice[];
}

/** Detail of one confirmation; `item` is the kind's own pending entry, including its preview. */
export type ToolConfirmationDetail =
  | (ToolConfirmationDetailBase & { kind: "personal_mcp"; item: PersonalMcpConfirmation })
  | (ToolConfirmationDetailBase & { kind: "email_send"; item: EmailSendConfirmation })
  | (ToolConfirmationDetailBase & { kind: "shopify_content"; item: ShopifyContentConfirmation })
  | (ToolConfirmationDetailBase & { kind: "team_mcp"; item: TeamToolConfirmation });

export const TOOL_CONFIRMATION_NOT_FOUND = "tool_confirmation_not_found";
export const TOOL_CONFIRMATION_UNAVAILABLE = "tool_confirmation_unavailable";

export function toolConfirmationApi<T>(path: string, method = "GET", body?: unknown) {
  return apiJson<T>(`/api/v1/tool-confirmations${path}`, {
    method,
    cache: "no-store",
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
}

export async function listToolConfirmations(): Promise<ToolConfirmationSummary[]> {
  const data = await toolConfirmationApi<{ items?: ToolConfirmationSummary[] }>("");
  return Array.isArray(data?.items) ? data.items : [];
}

export function getToolConfirmation(id: string): Promise<ToolConfirmationDetail> {
  return toolConfirmationApi<ToolConfirmationDetail>(`/${encodeURIComponent(id)}`);
}

export function decideToolConfirmation(id: string, decision: ToolConfirmationChoice): Promise<{ ok: true }> {
  return toolConfirmationApi<{ ok: true }>(`/${encodeURIComponent(id)}/decision`, "POST", { decision });
}
