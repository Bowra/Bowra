import { apiJson } from "./api-client";

export interface HomeSourceIssue {
  source: string;
  code: string;
}

export interface HomeAttentionItem {
  id: string;
  source: "notification";
  nativeType: string;
  category?: string;
  priority: string;
  title: string;
  body: string | null;
  metadata?: Record<string, unknown>;
  actionUrl: string;
  subjectType: string | null;
  subjectId: string | null;
  createdAt: string;
}

export interface HomeProgress {
  completed: number;
  total: number;
  failed: number;
  etaSeconds: number | null;
}

export interface HomeActiveItem {
  id: string;
  engine: "chat" | "routine" | "matrix";
  nativeStatus: string;
  title: string;
  subtitle: string | null;
  progress: HomeProgress | null;
  actionUrl: string;
  sessionId: string | null;
  startedAt: string | null;
  updatedAt: string | null;
  metadata: Record<string, unknown>;
}

export interface HomeOutputItem {
  id: string;
  kind: "report" | "deliverable";
  title: string;
  format: string | null;
  actionUrl: string;
  sessionId: string | null;
  createdAt: string;
  updatedAt: string;
  metadata: Record<string, unknown>;
}

export interface HomeSnapshot {
  version: "home.snapshot.v1";
  generatedAt: string;
  scope: {
    workspaceId: string;
    workspaceName: string;
  };
  attentionScope: "user";
  attention: HomeAttentionItem[];
  active: HomeActiveItem[];
  outputs: HomeOutputItem[];
  counts: {
    attention: number;
    active: number;
    outputs: number;
  };
  partial: boolean;
  issues: HomeSourceIssue[];
}

export type HomeProposalKind = "analyze" | "create" | "act" | "automate";

export interface HomeProposalItem {
  id: string;
  kind: HomeProposalKind;
  title: string;
  reason: string;
  prompt: string;
  capabilityRefs: string[];
  contextRefs: string[];
}

export interface HomeProposalResponse {
  version: "home.proposals.v1";
  generatedAt: string;
  strategy: "llm" | "deterministic";
  cached: boolean;
  generationId: string;
  items: HomeProposalItem[];
}

function arrayOf<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((item) => item && typeof item === "object") as T[]) : [];
}

function countOf(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/**
 * Home data is additive: a partial or older response (missing lists, counts or scope) must render as
 * empty sections, never crash the home page. Normalize at the parse boundary so every reader can rely
 * on the declared shape.
 */
export function normalizeHomeSnapshot(raw: unknown, workspaceId: string): HomeSnapshot {
  const body = (raw && typeof raw === "object" ? raw : {}) as Partial<HomeSnapshot>;
  const attention = arrayOf<HomeAttentionItem>(body.attention);
  const active = arrayOf<HomeActiveItem>(body.active);
  const outputs = arrayOf<HomeOutputItem>(body.outputs);
  const counts = (body.counts && typeof body.counts === "object" ? body.counts : {}) as Partial<HomeSnapshot["counts"]>;
  const scope = (body.scope && typeof body.scope === "object" ? body.scope : {}) as Partial<HomeSnapshot["scope"]>;
  return {
    version: "home.snapshot.v1",
    generatedAt: typeof body.generatedAt === "string" ? body.generatedAt : "",
    scope: {
      workspaceId: typeof scope.workspaceId === "string" ? scope.workspaceId : workspaceId,
      workspaceName: typeof scope.workspaceName === "string" ? scope.workspaceName : "",
    },
    attentionScope: "user",
    attention,
    active,
    outputs,
    counts: {
      attention: countOf(counts.attention, attention.length),
      active: countOf(counts.active, active.length),
      outputs: countOf(counts.outputs, outputs.length),
    },
    partial: Boolean(body.partial),
    issues: arrayOf<HomeSourceIssue>(body.issues),
  };
}

export function normalizeHomeProposals(raw: unknown): HomeProposalResponse {
  const body = (raw && typeof raw === "object" ? raw : {}) as Partial<HomeProposalResponse>;
  return {
    version: "home.proposals.v1",
    generatedAt: typeof body.generatedAt === "string" ? body.generatedAt : "",
    strategy: body.strategy === "llm" ? "llm" : "deterministic",
    cached: Boolean(body.cached),
    generationId: typeof body.generationId === "string" ? body.generationId : "",
    items: arrayOf<HomeProposalItem>(body.items).filter(
      (item) => typeof item.id === "string" && typeof item.title === "string" && typeof item.prompt === "string",
    ),
  };
}

export async function fetchHomeSnapshot(workspaceId: string, signal?: AbortSignal): Promise<HomeSnapshot> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return normalizeHomeSnapshot(await apiJson<unknown>(`/api/v1/home/snapshot?${query.toString()}`, { signal }), workspaceId);
}

export async function fetchHomeProposals(workspaceId: string, locale: string, signal?: AbortSignal): Promise<HomeProposalResponse> {
  const query = new URLSearchParams({ workspace_id: workspaceId, locale });
  return normalizeHomeProposals(await apiJson<unknown>(`/api/v1/home/proposals?${query.toString()}`, { signal }));
}
