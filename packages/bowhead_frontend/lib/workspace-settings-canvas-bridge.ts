import { ApiError } from "@/lib/api-client";
import type { ConfigLiteProposePayload } from "@/lib/types";
import { settingsCanvasKey, type SettingsCanvasTarget } from "@/lib/settings-canvas-target";
import {
  type WorkspaceSettingsLifecycle,
  settingsFormId,
  performWorkspaceSettingsSuggestionAction,
  stageWorkspaceSettingsSuggestion,
  workspaceSettingsClientInfo,
  WorkspaceSettingsStageError,
  type StagedWorkspaceSettingsSuggestion,
} from "@/lib/workspace-settings-agent";

/** Local recovery pointer only. Only clientInfo enters the admitted settings wire. */
export interface WorkspaceSettingsCanvasBinding {
  controllerId: string;
  clientInfo: string;
}

export interface WorkspaceSettingsCanvasPort {
  controllerId: string;
  target: Readonly<SettingsCanvasTarget>;
  readLifecycle: () => WorkspaceSettingsLifecycle | null;
  onAccessDenied: () => void;
}

interface MountedPort {
  port: WorkspaceSettingsCanvasPort;
  active: boolean;
}

/** Root-local view/operation associations; original page and domain service own all values. */
export class WorkspaceSettingsCanvasBridge {
  private ports = new Map<string, MountedPort>();
  private turns = new Map<string, { mounted: MountedPort; clientInfo: string | null; editGeneration?: number }>();
  private toolTurns = new Set<string>();
  private pendingToolOpens = new Map<string, SettingsCanvasTarget>();
  private reviews = new Map<string, StagedWorkspaceSettingsSuggestion>();
  // Explicit re-review admits one saved proposal, never its original Turn or late results.
  private recovered = new Map<string, { mounted: MountedPort; editGeneration: number }>();
  private staging = new Map<string, Promise<StagedWorkspaceSettingsSuggestion>>();
  private actions = new Map<string, MountedPort>();
  private listeners = new Set<() => void>();
  private revision = 0;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  getSnapshot = () => this.revision;
  hasPendingAction(controllerId: string): boolean {
    return [...this.actions.values()].some((mounted) => mounted.port.controllerId === controllerId);
  }
  private notify() {
    this.revision += 1;
    this.listeners.forEach((listener) => listener());
  }

  register(port: WorkspaceSettingsCanvasPort): () => void {
    // Effect replay keeps the same controller incarnation; a real remount has a new ID.
    const previous = [...this.turns.values()].find((entry) => entry.mounted.port.controllerId === port.controllerId
      && settingsCanvasKey(entry.mounted.port.target) === settingsCanvasKey(port.target));
    const mounted = previous?.mounted ?? { port, active: true };
    mounted.port = port;
    mounted.active = true;
    this.ports.set(port.controllerId, mounted);
    for (const [turnId, target] of this.pendingToolOpens) {
      if (settingsCanvasKey(target) !== settingsCanvasKey(port.target)) continue;
      this.turns.set(turnId, { mounted, clientInfo: null, editGeneration: port.readLifecycle()?.editGeneration });
      this.pendingToolOpens.delete(turnId);
    }
    this.notify();
    return () => {
      if (mounted.port !== port) return;
      mounted.active = false;
      if (this.ports.get(port.controllerId) === mounted) this.ports.delete(port.controllerId);
      this.notify();
    };
  }

  retainScope(scope: { userId: string; teamId: string; workspaceId: string } | null) {
    for (const [turnId, target] of this.pendingToolOpens) {
      if (!scope || target.userId !== scope.userId || target.teamId !== scope.teamId || target.workspaceId !== scope.workspaceId)
        this.pendingToolOpens.delete(turnId);
    }
    for (const [id, mounted] of this.ports) {
      if (!scope || mounted.port.target.userId !== scope.userId
        || mounted.port.target.teamId !== scope.teamId || mounted.port.target.workspaceId !== scope.workspaceId) {
        mounted.active = false;
        this.ports.delete(id);
      }
    }
    for (const [turnId, association] of this.turns)
      if (!association.mounted.active) this.turns.delete(turnId);
    for (const [id, review] of this.reviews)
      if (!this.recovered.get(id)?.mounted.active && !this.turns.has(review.proposal.turn_id)) this.reviews.delete(id);
    this.notify();
  }

  capture(target: SettingsCanvasTarget): WorkspaceSettingsCanvasBinding {
    const mounted = [...this.ports.values()].find((entry) =>
      entry.active && settingsCanvasKey(entry.port.target) === settingsCanvasKey(target));
    const lifecycle = mounted?.port.readLifecycle();
    if (!mounted || !lifecycle) throw new WorkspaceSettingsStageError("page_not_ready");
    return {
      controllerId: mounted.port.controllerId,
      clientInfo: workspaceSettingsClientInfo(target, lifecycle).configuration_settings_target,
    };
  }

  acceptTurn = (turnId: string, binding: WorkspaceSettingsCanvasBinding) => {
    // An exact cached receipt cannot transfer an existing Turn to a new page instance.
    if (this.turns.has(turnId)) return;
    const mounted = this.ports.get(binding.controllerId);
    if (!mounted) return;
    this.turns.set(turnId, { mounted, clientInfo: binding.clientInfo });
    this.notify();
  };

  /** A server Tool pointer joins the first mounted original page, once per Turn. */
  acceptToolOpen(turnId: string, target: SettingsCanvasTarget, source: "live" | "replay" = "live") {
    if (this.toolTurns.has(turnId)) return;
    this.toolTurns.add(turnId);
    if (source === "live" && !this.turns.has(turnId)) {
      const mounted = [...this.ports.values()].find((entry) => entry.active
        && settingsCanvasKey(entry.port.target) === settingsCanvasKey(target));
      if (mounted) this.turns.set(turnId, { mounted, clientInfo: null, editGeneration: mounted.port.readLifecycle()?.editGeneration });
      else this.pendingToolOpens.set(turnId, { ...target });
    }
    this.notify();
  }

  isToolTurn(turnId: string) { return this.toolTurns.has(turnId); }

  pageChanged(controllerId: string) {
    const mounted = this.ports.get(controllerId);
    if (!mounted?.active) return;
    for (const association of this.turns.values()) {
      if (association.mounted === mounted && association.clientInfo === null && association.editGeneration === undefined)
        association.editGeneration = mounted.port.readLifecycle()?.editGeneration;
    }
    this.notify();
  }

  denyBinding = (binding: WorkspaceSettingsCanvasBinding) => {
    const mounted = this.ports.get(binding.controllerId);
    if (!mounted?.active) return;
    this.invalidate(mounted);
  };

  routesTo(proposal: ConfigLiteProposePayload, controllerId: string): boolean {
    const mounted = this.recovered.get(proposal.proposal_id)?.mounted ?? this.turns.get(proposal.turn_id)?.mounted;
    if (!mounted?.active || mounted.port.controllerId !== controllerId) return false;
    const page = mounted.port.target;
    // Keep a blocked suggestion visible on its original page; staging still checks input.
    return proposal.form_id === settingsFormId(page) && proposal.team_id === page.teamId && proposal.workspace_id === page.workspaceId;
  }

  assertOriginal(proposal: ConfigLiteProposePayload) { this.original(proposal); }

  hasOriginal(proposal: ConfigLiteProposePayload) {
    try { this.original(proposal); return true; } catch { return false; }
  }
  isActionPending(proposalId: string) { return this.actions.has(proposalId); }

  private original(proposal: ConfigLiteProposePayload): MountedPort {
    const recovered = this.recovered.get(proposal.proposal_id);
    if (recovered) {
      const page = recovered.mounted.port.readLifecycle();
      if (!recovered.mounted.active || !page) throw new WorkspaceSettingsStageError("page_not_ready");
      if (page.editGeneration !== recovered.editGeneration) throw new WorkspaceSettingsStageError("local_changes");
      if (this.reviews.get(proposal.proposal_id)?.proposal.turn_id !== proposal.turn_id)
        throw new WorkspaceSettingsStageError("target_mismatch");
      return recovered.mounted;
    }
    const association = this.turns.get(proposal.turn_id);
    const lifecycle = association?.mounted.port.readLifecycle();
    if (!association?.mounted.active || !lifecycle)
      throw new WorkspaceSettingsStageError("page_not_ready");
    const target = proposal.settings_target;
    const page = association.mounted.port.target;
    if (proposal.form_id !== settingsFormId(page) || proposal.team_id !== page.teamId || proposal.workspace_id !== page.workspaceId)
      throw new WorkspaceSettingsStageError("target_mismatch");
    if (association.clientInfo === null) {
      if (target) throw new WorkspaceSettingsStageError("target_mismatch");
      if (association.editGeneration !== lifecycle.editGeneration) throw new WorkspaceSettingsStageError("local_changes");
      return association.mounted;
    }
    const sent = JSON.parse(association.clientInfo);
    if (!target
      || target.form_id !== settingsFormId(page)
      || target.user_id !== page.userId || target.team_id !== page.teamId
      || target.workspace_id !== page.workspaceId
      || target.edit_generation !== sent.edit_generation)
      throw new WorkspaceSettingsStageError("target_mismatch");
    return association.mounted;
  }

  private read = (mounted: MountedPort) => () => mounted.active ? mounted.port.readLifecycle() : null;
  private invalidate(mounted: MountedPort) {
    // A late denial from a disposed controller cannot remove a refreshed/reopened page.
    if (!mounted.active) return;
    mounted.active = false;
    if (this.ports.get(mounted.port.controllerId) === mounted) this.ports.delete(mounted.port.controllerId);
    for (const [id, review] of this.reviews)
      if (this.recovered.get(id)?.mounted === mounted || this.turns.get(review.proposal.turn_id)?.mounted === mounted) this.reviews.delete(id);
    mounted.port.onAccessDenied();
    this.notify();
  }
  private deny(mounted: MountedPort, error: unknown) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.invalidate(mounted);
  }

  private recoveryPage(proposal: ConfigLiteProposePayload): MountedPort | undefined {
    return [...this.ports.values()].find(({ active, port }) => active && port.readLifecycle() !== null
      && settingsFormId(port.target) === proposal.form_id && port.target.teamId === proposal.team_id
      && port.target.workspaceId === proposal.workspace_id
      && (!proposal.settings_target || proposal.settings_target.user_id === port.target.userId));
  }

  canReviewSaved(proposal: ConfigLiteProposePayload): boolean {
    return !this.hasOriginal(proposal) && !this.actions.has(proposal.proposal_id)
      && !this.staging.has(proposal.proposal_id) && Boolean(this.recoveryPage(proposal));
  }

  /** User-triggered, authorized GET; it never saves or redirects an old in-flight operation. */
  reviewSaved(proposal: ConfigLiteProposePayload): Promise<StagedWorkspaceSettingsSuggestion> {
    const mounted = this.recoveryPage(proposal);
    if (!mounted || this.actions.has(proposal.proposal_id) || this.staging.has(proposal.proposal_id))
      return Promise.reject(new WorkspaceSettingsStageError("page_not_ready"));
    const task = stageWorkspaceSettingsSuggestion(proposal.proposal_id, mounted.port.target, this.read(mounted), "saved_review")
      .then((review) => {
        if (!mounted.active || !mounted.port.readLifecycle()) throw new WorkspaceSettingsStageError("page_not_ready");
        if (review.proposal.turn_id !== proposal.turn_id) throw new WorkspaceSettingsStageError("target_mismatch");
        this.reviews.set(proposal.proposal_id, review);
        this.recovered.set(proposal.proposal_id, { mounted, editGeneration: review.editGeneration });
        return review;
      })
      .catch((error) => { this.deny(mounted, error); throw error; })
      .finally(() => { this.staging.delete(proposal.proposal_id); this.notify(); });
    this.staging.set(proposal.proposal_id, task);
    return task;
  }

  stage(proposal: ConfigLiteProposePayload): Promise<StagedWorkspaceSettingsSuggestion> {
    let mounted: MountedPort;
    try { mounted = this.original(proposal); } catch (error) { return Promise.reject(error); }
    const existing = this.reviews.get(proposal.proposal_id);
    if (existing) return existing.proposal.turn_id === proposal.turn_id
      ? Promise.resolve(existing) : Promise.reject(new WorkspaceSettingsStageError("target_mismatch"));
    const pending = this.staging.get(proposal.proposal_id);
    if (pending) return pending.then((review) => {
      if (this.original(proposal) !== mounted || review.proposal.turn_id !== proposal.turn_id)
        throw new WorkspaceSettingsStageError("target_mismatch");
      return review;
    });
    const task = stageWorkspaceSettingsSuggestion(proposal.proposal_id, mounted.port.target, this.read(mounted))
      .then((review) => {
        // The event is a routing hint; the authorized reader must confirm its original Turn.
        if (this.original(proposal) !== mounted || review.proposal.turn_id !== proposal.turn_id)
          throw new WorkspaceSettingsStageError("target_mismatch");
        this.reviews.set(proposal.proposal_id, review);
        return review;
      })
      .catch((error) => { this.deny(mounted, error); throw error; })
      .finally(() => { this.staging.delete(proposal.proposal_id); if (!this.hasOriginal(proposal)) this.notify(); });
    this.staging.set(proposal.proposal_id, task);
    return task;
  }

  async perform(proposal: ConfigLiteProposePayload, op: "apply" | "undo") {
    const mounted = this.original(proposal);
    if (this.actions.has(proposal.proposal_id)) throw new Error("settings_action_pending");
    this.actions.set(proposal.proposal_id, mounted);
    this.notify();
    try {
      const review = await this.stage(proposal);
      const receipt = await performWorkspaceSettingsSuggestionAction(review, this.read(mounted), op);
      if (receipt && (receipt.proposal_id !== proposal.proposal_id || receipt.form_id !== settingsFormId(mounted.port.target)
        || receipt.owner_user_id !== mounted.port.target.userId || receipt.team_id !== mounted.port.target.teamId
        || receipt.workspace_id !== mounted.port.target.workspaceId))
        throw new Error("settings_receipt_target_mismatch");
      // performLiteAction already issued canonical invalidation for the actual receipt.
      return receipt;
    } catch (error) {
      this.deny(mounted, error);
      throw error;
    } finally {
      this.actions.delete(proposal.proposal_id);
      this.notify();
    }
  }
}
