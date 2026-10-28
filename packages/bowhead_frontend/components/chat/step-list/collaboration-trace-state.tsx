"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { apiJson } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { getActiveSpaceId, spaceHeader } from "@/lib/space-storage";
import { getActiveTeamId, TEAM_CHANGED_EVENT } from "@/lib/team-storage";
import { useTurn } from "@/lib/turn-store";
import { agentDisplayName } from "./agent-meta";

export interface CollaborationMessage {
  operation_id: string;
  action: "ask" | "reply" | "update" | "resolve" | "expire";
  sender_id: string;
  recipient_id: string;
  body?: string;
  unavailable?: string;
  references?: Array<{ identity: string; version: string }>;
}

export interface CollaborationTopic {
  topic_id: string;
  revision?: number;
  sender_id?: string;
  recipient_id?: string;
  required?: boolean;
  response_status?: "pending" | "answered" | "unavailable" | "cancelled";
  dependency_status?: "open" | "resolved" | "waived" | "blocked" | "cancelled";
  messages: CollaborationMessage[];
}

export interface CollaborationProjection {
  turn_id: string;
  revision: number;
  turn_status?: string;
  participants: Array<{
    execution_id: string;
    profile: string;
    is_owner: boolean;
    status: string;
    waiting: boolean;
  }>;
  topics: CollaborationTopic[];
}

interface TraceState {
  enabled: boolean;
  loading: boolean;
  error: boolean;
  terminal: boolean;
  data: CollaborationProjection | null;
  name: (executionId: string) => string;
  refresh: () => void;
}

const TraceContext = createContext<TraceState | null>(null);

function withoutContent(data: CollaborationProjection | null): CollaborationProjection | null {
  if (!data) return null;
  return { ...data, topics: data.topics.map((topic) => ({ ...topic, messages: topic.messages.map((message) => ({
    operation_id: message.operation_id, action: message.action, sender_id: message.sender_id,
    recipient_id: message.recipient_id, unavailable: "collaboration_material_unavailable",
  })) })) };
}

export function useCollaborationTrace(): TraceState | null {
  return useContext(TraceContext);
}

/** The existing turn log invalidates this authorized reader; it never caches bodies. */
export function CollaborationTraceProvider({ turnId, children }: { turnId: string; children: ReactNode }) {
  const turn = useTurn(turnId);
  const auth = useOptionalAuthSession();
  const [refreshCount, setRefreshCount] = useState(0);
  const request = useRef<AbortController | null>(null);
  const revision = turn?.collaborationRevision ?? 0;
  const workspaceId = getActiveSpaceId();
  const teamId = getActiveTeamId();
  const identity = JSON.stringify([auth?.me?.id, teamId, workspaceId, turnId]);
  const enabled = revision > 0 && (!auth || auth.status === "authenticated");
  // Terminal execution changes also close waits without creating a new message.
  const terminals = JSON.stringify(turn?.peerTerminalById ?? {});
  const finalState = turn?.cardFinalState;
  const [snapshot, setSnapshot] = useState<{
    identity: string; data: CollaborationProjection | null; loading: boolean; error: boolean;
  } | null>(null);
  const refresh = () => {
    request.current?.abort();
    setSnapshot((current) => current ? { ...current, data: withoutContent(current.data), loading: true } : null);
    setRefreshCount((count) => count + 1);
  };

  useEffect(() => {
    const invalidate = () => {
      request.current?.abort();
      setSnapshot((current) => current ? { ...current, data: withoutContent(current.data), loading: true } : null);
      setRefreshCount((count) => count + 1);
    };
    const visible = () => { if (document.visibilityState === "visible") invalidate(); };
    const events = [TEAM_CHANGED_EVENT, "bowhead:workspace-changed", "bowhead:workspaces-invalidated", "focus"];
    for (const event of events) window.addEventListener(event, invalidate);
    document.addEventListener("visibilitychange", visible);
    return () => {
      for (const event of events) window.removeEventListener(event, invalidate);
      document.removeEventListener("visibilitychange", visible);
    };
  }, []);

  useEffect(() => {
    request.current?.abort();
    if (!enabled) { setSnapshot(null); return; }
    const controller = new AbortController();
    request.current = controller;
    setSnapshot((current) => ({ identity, loading: true, error: false, data: current?.identity === identity ? withoutContent(current.data) : null }));
    void apiJson<CollaborationProjection>(`/api/v1/turns/${encodeURIComponent(turnId)}/collaboration`, {
      signal: controller.signal, cache: "no-store", headers: spaceHeader({ workspaceId, teamId }),
    }).then((data) => {
      if (controller.signal.aborted || request.current !== controller) return;
      if (data.turn_id !== turnId) throw new Error("Collaboration turn mismatch");
      setSnapshot({ identity, loading: false, error: false, data });
    }).catch(() => {
      if (!controller.signal.aborted && request.current === controller) {
        setSnapshot({ identity, loading: false, error: true, data: null });
      }
    });
    return () => controller.abort();
  }, [enabled, identity, revision, terminals, finalState, refreshCount, turnId, workspaceId, teamId]);

  const view = enabled && snapshot?.identity === identity ? snapshot : null;
  const data = view?.data ?? null;
  // A confirmed terminal event outranks an older details snapshot. The same
  // Turn store clears finalState on turn.resumed; no separate lifecycle lives here.
  const terminal = finalState != null || ["completed", "failed", "cancelled", "aborted"].includes(data?.turn_status ?? "");
  const names = new Map<string, string>();
  for (const participant of data?.participants ?? []) {
    const entry = Object.entries(turn?.peerAgentAttribution ?? {}).find(([, value]) => value.agentExecutionId === participant.execution_id);
    const displayName = entry ? turn?.peerDisplayNames[entry[0]] ?? entry[1].displayNameSnapshot : undefined;
    names.set(participant.execution_id, agentDisplayName(participant.is_owner ? "lead" : participant.profile, displayName));
  }
  // Distinguish two executions of the same Agent without showing internal IDs.
  const totals = [...names.values()];
  const counts = new Map<string, number>();
  for (const [id, name] of names) {
    if (totals.filter((candidate) => candidate === name).length < 2) continue;
    const index = (counts.get(name) ?? 0) + 1;
    counts.set(name, index);
    names.set(id, `${name} ${index}`);
  }
  return <TraceContext.Provider value={{ enabled, loading: view?.loading ?? enabled, error: view?.error ?? false, terminal, data,
    name: (id) => names.get(id) ?? agentDisplayName(null), refresh,
  }}>{children}</TraceContext.Provider>;
}

export function collaborationTopicState(topic: CollaborationTopic, terminal: boolean) {
  if (topic.dependency_status === "resolved") return "resolved";
  if (topic.dependency_status === "blocked") return "blocked";
  if (topic.dependency_status === "waived") return "waived";
  if (topic.required && (terminal || topic.dependency_status === "cancelled")) return "unresolved";
  if (topic.response_status === "answered") return topic.required ? "verification" : "answered";
  if (topic.response_status === "unavailable") return "unavailable";
  if (topic.response_status === "pending") return terminal ? "unfinished" : "pending";
  if (topic.response_status === "cancelled") return "unfinished";
  return "updated";
}
