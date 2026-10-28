import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ArchivedTurn, TurnRunnerState } from "./types";
import {
  mergeArchivedTurnsHydration,
  mergeCurrentTurnHydration,
  mergePolledEffectDisclosure,
  useTurnRunner,
} from "./use-turn-runner";

describe("useTurnRunner failed-turn hydration", () => {
  it("restores the failed user source and attachments even without a final payload", async () => {
    const attachment = {
      fileId: "art_failed",
      filename: "failed-evidence.zip",
      size: 64,
      mime: "application/zip",
      bundleArtifactIds: ["art_a", "art_b"],
    };
    const hydrate = {
      sessionId: "s_failed",
      sessionTitle: "Failed turn",
      messages: [],
      lastIntent: null,
      lastTurnId: "t_failed",
      lastTerminalStatus: "failed",
      lastErrorReason: {
        source: "turn_failed" as const,
        reasonCode: "lead_internal_error",
      },
      lastUserText: "use the attachment",
      lastUserAttachments: [attachment],
      lastAnswer: "partial answer",
    };

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      hydrate,
      hydrateTick: 1,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_failed"));
    expect(hook.result.current.user).toBe("use the attachment");
    expect(hook.result.current.userAttachments).toEqual([attachment]);
    expect(hook.result.current.answer).toBe("partial answer");
    expect(hook.result.current.errorReason).toEqual(hydrate.lastErrorReason);
    expect(hook.result.current.started).toBe(true);
  });

  it("routes a persisted terminal last turn before its event projection arrives", async () => {
    const hydrate = {
      sessionId: "s_progressive",
      sessionTitle: "Progressive hydrate",
      messages: [],
      lastTurnId: "t_progressive",
      lastTerminalStatus: "completed",
      lastTurnRouted: true,
      lastUserText: "show the report",
      lastAnswer: "The persisted answer is ready.",
    };
    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      hydrate,
      hydrateTick: 1,
    }));

    await waitFor(() =>
      expect(hook.result.current.turnId).toBe("t_progressive"),
    );
    expect(hook.result.current.user).toBe("show the report");
    expect(hook.result.current.answer).toBe("The persisted answer is ready.");
    expect(hook.result.current.started).toBe(true);
    expect(hook.result.current.finalPayload).toBeUndefined();
  });

  it.each(["sse", "mock"] as const)(
    "restores the last turn's start time on refresh so bylines keep their time (%s)",
    async (mode) => {
      const startedAt = Date.parse("2026-09-28T19:22:05+00:00");
      const hydrate = {
        sessionId: "s_time",
        sessionTitle: "Time",
        messages: [],
        lastTurnId: "t_time",
        lastTerminalStatus: "completed",
        lastTurnRouted: true,
        lastUserText: "ask",
        lastAnswer: "answer",
        lastStartedAt: startedAt,
      };
      const hook = renderHook(() => useTurnRunner({ mode, hydrate, hydrateTick: 1 }));
      await waitFor(() => expect(hook.result.current.turnId).toBe("t_time"));
      expect(hook.result.current.startedAt).toBe(startedAt);
      expect(hook.result.current.running).toBe(false);
    },
  );

  it("keeps mock and SSE baseline projection fields aligned", async () => {
    const hydrate = {
      sessionId: "s_mock_projection",
      sessionTitle: "Mock projection",
      messages: [],
      lastTurnId: "t_mock_projection",
      lastTurnRouted: true,
      lastTerminalStatus: "clarification_pending",
      lastUserText: "choose one",
      lastAnswer: "I need one detail.",
      lastSummary: { total: "1s", cost: "", tools: 1, llm: 1 },
      lastClarifications: [{
        clarification_id: "clar_mock_projection",
        candidates: ["A", "B"],
        fallback_text: "Please choose",
      }],
      lastHeadline: "Waiting for input",
      lastCitations: [
        { seq: 1, artifactId: "art_mock", label: null, kind: "data_ref" as const },
      ],
    };
    const hook = renderHook(() => useTurnRunner({
      mode: "mock",
      hydrate,
      hydrateTick: 1,
    }));

    await waitFor(() =>
      expect(hook.result.current.turnId).toBe("t_mock_projection"),
    );
    expect(hook.result.current.summary).toEqual(hydrate.lastSummary);
    expect(hook.result.current.clarificationPending).toMatchObject({
      clarificationId: "clar_mock_projection",
      candidates: ["A", "B"],
    });
    expect(hook.result.current.headline).toBe("Waiting for input");
    expect(hook.result.current.citations).toEqual(hydrate.lastCitations);
  });

  it("enriches the terminal projection and only proof fields for a running turn", () => {
    const current = {
      sessionId: "s_current",
      turnId: "t_current",
      running: false,
      answer: "persisted",
      citations: [],
    } as unknown as TurnRunnerState;
    const patch = {
      sessionId: "s_current",
      turnId: "t_current",
      lastFinalPayload: { inline_artifacts: [] },
      lastResponseKind: "chat" as const,
      lastHeadline: "已补齐",
      lastCitations: [
        { seq: 1, artifactId: "art_1", label: null, kind: "data_ref" as const },
      ],
    };

    const enriched = mergeCurrentTurnHydration(current, patch);
    expect(enriched).not.toBe(current);
    expect(enriched.answer).toBe("persisted");
    expect(enriched.finalPayload).toEqual({ inline_artifacts: [] });
    expect(enriched.headline).toBe("已补齐");
    expect(enriched.citations).toEqual(patch.lastCitations);

    const running = {
      ...current,
      running: true,
      executionGeneration: 2,
    } as TurnRunnerState;
    const runningOverlay = {
      protocol: 2 as const,
      receipt_id: null,
      attempt_count: 1,
      unknown_count: 1,
      counts: {
        accounting: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
        inference_internal: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
        user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 1 },
      },
      proof_digest: "proof_running",
      oldest_unknown_attempt_started_at: "2026-08-14T00:00:00Z",
      oldest_unknown_age_upper_bound_seconds: 1,
      warning_message_keys: ["turn.effect_user_action_unsettled"],
      finalized: false,
    };
    const runningEnriched = mergeCurrentTurnHydration(running, {
      ...patch,
      lastEffectDisclosure: runningOverlay,
      terminalProtocolVersion: 1,
      executionGeneration: 2,
    });
    expect(runningEnriched).not.toBe(running);
    expect(runningEnriched.effectDisclosure).toEqual(runningOverlay);
    expect(runningEnriched.terminalProtocolVersion).toBe(1);
    expect(runningEnriched.executionGeneration).toBe(2);
    expect(runningEnriched.finalPayload).toBeUndefined();
    expect(mergeCurrentTurnHydration(current, {
      ...patch,
      sessionId: "s_stale",
    })).toBe(current);
    expect(mergeCurrentTurnHydration(runningEnriched, {
      ...patch,
      lastEffectDisclosure: null,
      terminalProtocolVersion: 1,
      executionGeneration: 1,
    })).toBe(runningEnriched);
    const staleZero = {
      ...runningOverlay,
      attempt_count: 0,
      unknown_count: 0,
      counts: {
        accounting: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
        inference_internal: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
        user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
      },
      proof_digest: "proof_stale_zero",
    };
    expect(mergeCurrentTurnHydration(runningEnriched, {
      ...patch,
      lastEffectDisclosure: staleZero,
      terminalProtocolVersion: 1,
      executionGeneration: 2,
    }).effectDisclosure).toEqual(runningOverlay);
    expect(mergeCurrentTurnHydration({
      ...runningEnriched,
      running: false,
      terminalStatus: "cancelled",
    }, {
      ...patch,
      lastEffectDisclosure: staleZero,
      terminalProtocolVersion: 1,
      executionGeneration: 2,
    }).effectDisclosure).toEqual(runningOverlay);
    const pendingRead = {
      ...running,
      effectDisclosure: null,
      effectDisclosureDiscoveryPending: true,
    };
    const lateSameGeneration = mergeCurrentTurnHydration(pendingRead, {
      ...patch,
      lastEffectDisclosure: staleZero,
      terminalProtocolVersion: 1,
      executionGeneration: 2,
    });
    expect(lateSameGeneration.effectDisclosureDiscoveryPending).toBe(true);
    expect(mergePolledEffectDisclosure(
      pendingRead,
      "t_current",
      1,
      runningOverlay,
    )).toBe(pendingRead);

    const polled = mergePolledEffectDisclosure(
      running,
      "t_current",
      2,
      runningOverlay,
    );
    expect(polled.effectDisclosure).toEqual(runningOverlay);
    const archived = {
      ...running,
      turnId: "t_followup",
      archivedTurns: [{
        turnId: "t_current",
        executionGeneration: 2,
        effectDisclosure: null,
        effectDisclosureDiscoveryPending: true,
      } as ArchivedTurn],
    };
    expect(
      mergePolledEffectDisclosure(
        archived,
        "t_current",
        2,
        runningOverlay,
      ).archivedTurns?.[0].effectDisclosure,
    ).toEqual(runningOverlay);
    expect(mergeCurrentTurnHydration(current, {
      ...patch,
      turnId: "t_followup",
    })).toBe(current);
  });

  it("merges enriched history without dropping a newer archived Turn", () => {
    const base = {
      turnId: "t_old",
      user: "old question",
      answer: "old answer",
      userAttachments: [],
      traceSteps: [],
      peersDispatched: [],
      beats: [],
      errorReason: null,
      archivedAt: 1,
    } as ArchivedTurn;
    const newer = {
      turnId: "t_new",
      user: "new question",
      answer: "new answer",
      userAttachments: [],
      traceSteps: [],
      peersDispatched: [],
      beats: [],
      errorReason: null,
      archivedAt: 2,
    } as ArchivedTurn;
    const enriched = {
      ...base,
      headline: "enriched",
      citations: [{ seq: 1, artifactId: "art_1", label: null, kind: "data_ref" }],
    } as ArchivedTurn;

    expect(mergeArchivedTurnsHydration([base, newer], [enriched])).toEqual([
      enriched,
      newer,
    ]);
  });
});
