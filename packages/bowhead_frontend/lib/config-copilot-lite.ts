/** Configuration-copilot lite confirmation — proposal store + server state orchestration (batch 1-a).
 *
 * (tech-design-batch1-draft-store.md §8; batch-0.5 frontend apply orchestration and browser-local
 * overlay moved server-side: terminal authority = status at /api/v1/config-proposals/{id}.)
 * - proposal payload store: module Map (dispatcher writes, Connected reads), rendering source for replay.
 * - Terminal state: memory cache + mount-time hydrate (GET); consistent across F5/devices/private mode.
 * - apply/undo/ignore = forward click via POST, no local write orchestration; 409 state_conflict heals from current server state.
 * - hydrate 404 = server explicitly has no proposal (batch-0.5 dogfood UUID remnants/rollback orphans)
 *   → stale (noninteractive); only network errors return to propose and allow hydrate retries (P2-4 split).
 */

import { ApiError, apiJson } from "@/lib/api-client";
import { notifyPersonalAppearanceSaved } from "@/lib/personal-appearance";
import type { ConfigLiteProposePayload } from "@/lib/types";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { notifyWorkspaceInstructionsSaved, notifyWorkspaceMetadataSaved, notifyWorkspaceSettingsSaved } from "@/lib/workspace-settings-notifications";

export type LiteTerminalState = "applied" | "reverted" | "ignored";
export type LiteServerState = LiteTerminalState | "proposed" | "stale";

export interface LiteActionReceipt {
  proposal_id: string;
  form_id: string;
  status: string;
  workspace_id: string | null;
  replayed?: boolean;
  team_id?: string;
  owner_user_id?: string;
}
type ProposalDto = LiteActionReceipt;

const _proposals = new Map<string, ConfigLiteProposePayload>();
const _states = new Map<string, LiteServerState>();
const _hydrating = new Map<string, Promise<void>>();
const _listeners = new Set<() => void>();
let _version = 0;

function _notify(): void {
  _version += 1;
  _listeners.forEach((fn) => fn());
}

export function subscribeLite(listener: () => void): () => void {
  _listeners.add(listener);
  return () => _listeners.delete(listener);
}

export function liteVersion(): number {
  return _version;
}

/** Dispatcher writes (idempotent replay: same-id overwrite with same payload is harmless). */
export function applyLiteProposal(payload: ConfigLiteProposePayload): void {
  _proposals.set(payload.proposal_id, payload);
  _notify();
}

export function getLiteProposal(proposalId: string): ConfigLiteProposePayload | undefined {
  return _proposals.get(proposalId);
}

/** Read existing normalized events; hosts still resolve authorized review before presentation. */
export function getLiteProposals(): readonly ConfigLiteProposePayload[] {
  return [..._proposals.values()];
}

export function getLiteServerState(proposalId: string): LiteServerState | null {
  return _states.get(proposalId) ?? null;
}

export function resetLiteForTests(): void {
  _proposals.clear();
  _states.clear();
  _hydrating.clear();
  _notify();
}

/** Test-only: clear state cache (simulate hydration after F5); replay reconstructs payload store. */
export function _dropLiteStateCacheForTests(): void {
  _states.clear();
  _hydrating.clear();
}

function _setState(proposalId: string, state: LiteServerState): void {
  _states.set(proposalId, state);
  _notify();
}

function _toState(status: string): LiteServerState {
  return status === "applied" || status === "reverted" || status === "ignored" ? status : "proposed";
}

function _isStructuredNotFound(error: unknown): boolean {
  // Accept only structured server 404 (config_proposal_not_found); FastAPI plain 404 when flag-off route is absent
  // does not qualify (R2 review P2-3): remain unknown, render propose, and allow interaction after flag reopens.
  if (!(error instanceof ApiError) || error.status !== 404) return false;
  const body = error.body as { detail?: { error?: { code?: string } } } | undefined;
  return body?.detail?.error?.code === "config_proposal_not_found";
}

/** Mount-time terminal hydration (deduplicate in-flight requests). Structured 404 → stale (true orphan card);
 *  plain 404 (flag off)/network errors → do not cache (retry on next mount). */
export function hydrateLiteState(proposalId: string): Promise<void> {
  if (_states.has(proposalId)) return Promise.resolve();
  const inflight = _hydrating.get(proposalId);
  if (inflight) return inflight;
  // A late read cannot replace a newer operation receipt or a reset cache's current request.
  const canHydrate = () => _hydrating.get(proposalId) === task && !_states.has(proposalId);
  const task = apiJson<ProposalDto>(`/api/v1/config-proposals/${proposalId}`, {
    cache: "no-store",
  })
    .then((dto) => { if (canHydrate()) _setState(proposalId, _toState(dto.status)); })
    .catch((error: unknown) => {
      if (canHydrate() && _isStructuredNotFound(error)) {
        _setState(proposalId, "stale");
      }
    })
    .finally(() => {
      if (_hydrating.get(proposalId) === task) _hydrating.delete(proposalId);
    });
  _hydrating.set(proposalId, task);
  return task;
}

/** An original settings editor saved this proposal; re-read the authoritative receipt. */
export async function refreshLiteState(proposalId: string): Promise<void> {
  await _hydrating.get(proposalId);
  _states.delete(proposalId);
  await hydrateLiteState(proposalId);
}

function _conflictStatus(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const body = error.body as { detail?: { error?: { code?: string; details?: { status?: string } } } } | undefined;
  const inner = body?.detail?.error;
  if (inner?.code !== "proposal_state_conflict") return null;
  return typeof inner.details?.status === "string" ? inner.details.status : null;
}

function liteErrorCode(error: ApiError): string | undefined {
  const body = error.body as { detail?: { code?: string; error?: { code?: string } } } | undefined;
  return error.code ?? body?.detail?.error?.code ?? body?.detail?.code;
}

export function isLiteFormUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && liteErrorCode(error) === "form_unavailable";
}

export function isLiteBaselineConflict(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  return ["configuration_baseline_changed", "config_version_conflict", "agent_run_limits_conflict", "execution_limits_conflict"].includes(liteErrorCode(error) ?? "");
}

/** Apply/undo: forward POST for server execution (write orchestration, audit, and state CAS all server-side).
 *  Cache response status on success; 409 state_conflict heals from current server state; propagate other errors to caller
 *  (LiteConfirm failure state supports retries; server guarantees idempotency/replay semantics). */
export async function performLiteAction(proposal: ConfigLiteProposePayload, op: "apply" | "undo", shouldNotify?: (receipt: LiteActionReceipt) => boolean): Promise<LiteActionReceipt | null> {
  let receipt: LiteActionReceipt;
  try {
    const dto = await apiJson<ProposalDto>(`/api/v1/config-proposals/${proposal.proposal_id}/${op}`, { method: "POST" });
    if (dto.proposal_id !== proposal.proposal_id || dto.form_id !== proposal.form_id || dto.status !== (op === "apply" ? "applied" : "reverted")) {
      throw new Error("The settings save outcome could not be verified.");
    }
    receipt = dto;
    _setState(proposal.proposal_id, _toState(dto.status));
  } catch (error) {
    const conflictStatus = _conflictStatus(error);
    if (conflictStatus) {
      // Cross-device healing: other device already undid/ignored; immediately render correct current state here.
      _setState(proposal.proposal_id, _toState(conflictStatus));
      return null;
    }
    throw error;
  }
  // Refresh after both apply and undo (R2 review P1-1: batch-0.5 baseline did both; undoing appearance must immediately revert screen).
  if (proposal.form_id === "user_appearance") {
    void refreshEffectiveTheme();
  }
  if (proposal.form_id === "personal_appearance" && proposal.team_id) {
    notifyPersonalAppearanceSaved(proposal.team_id);
    void refreshEffectiveTheme(proposal.team_id).catch(() => undefined);
  }
  if (proposal.form_id === "workspace_i18n" && receipt.team_id && receipt.workspace_id && receipt.owner_user_id && (shouldNotify?.(receipt) ?? true)) {
    notifyWorkspaceSettingsSaved({ teamId: receipt.team_id, workspaceId: receipt.workspace_id, receipt });
  }
  if (proposal.form_id === "workspace_metadata" && receipt.team_id && receipt.workspace_id && receipt.owner_user_id && (shouldNotify?.(receipt) ?? true)) {
    notifyWorkspaceMetadataSaved({ teamId: receipt.team_id, workspaceId: receipt.workspace_id, receipt });
  }
  if (proposal.form_id === "space_instructions" && receipt.team_id && receipt.workspace_id) {
    notifyWorkspaceInstructionsSaved({ teamId: receipt.team_id, workspaceId: receipt.workspace_id, receipt });
  }
  return receipt;
}

/** Ignore: forward POST; failure silently retains propose (no side effects, click again). */
export function ignoreLiteProposal(proposalId: string): void {
  void apiJson<ProposalDto>(`/api/v1/config-proposals/${proposalId}/ignore`, {
    method: "POST",
  })
    .then((dto) => _setState(proposalId, _toState(dto.status)))
    .catch((error: unknown) => {
      const conflictStatus = _conflictStatus(error);
      if (conflictStatus) _setState(proposalId, _toState(conflictStatus));
    });
}
