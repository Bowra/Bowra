import { apiJson } from "@/lib/api-client";
import type { EmailSendConfirmation } from "@/lib/email-send-confirmation-api";
import type { PersonalMcpConfirmation } from "@/lib/personal-mcp-api";
import type { ShopifyContentConfirmation } from "@/lib/shopify-content-confirmation-api";
import type { TeamToolConfirmation } from "@/lib/team-tool-confirmation-api";
import type {
  ToolConfirmationCategory,
  ToolConfirmationChoice,
  ToolConfirmationKind,
} from "@/lib/sse-events";

/** One pending confirmation owned by the current user (FEATURE_INLINE_TOOL_CONFIRMATION). */
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
  /** Computed by the server; the only countdown anchor besides the event's own value. */
  remaining_seconds: number;
}

interface ToolConfirmationDetailBase extends ToolConfirmationSummary {
  category: ToolConfirmationCategory | null;
  choices: ToolConfirmationChoice[];
}

/** Detail of one confirmation; `item` is exactly what the kind's original list endpoint returns. */
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
