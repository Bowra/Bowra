"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import {
  compilePersonalAppearance,
  PERSONAL_APPEARANCE_REFRESH_EVENT,
  getPersonalAppearance,
  patchPersonalAppearance,
  resetPersonalAppearance,
  undoPersonalAppearance,
  type PersonalAppearanceChange,
  type PersonalAppearanceCompileResult,
  type PersonalAppearanceOverrides,
  type PersonalAppearanceSnapshot,
} from "@/lib/personal-appearance";
import { navigateToPersonalSettings } from "@/lib/personal-settings-paths";
import { getActiveTeamId, TEAM_CHANGED_EVENT } from "@/lib/team-storage";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { toast } from "@/lib/use-toast";
import { randomUuid } from "@/lib/random-id";

export type PersonalAppearanceStatus =
  | "idle"
  | "loading"
  | "saved"
  | "saving"
  | "resetting"
  | "undoing"
  | "conflict"
  | "error";

export type PersonalAppearanceField = keyof PersonalAppearanceOverrides;

interface PatchRequest {
  fields: PersonalAppearanceField[];
  resolve: () => void;
  reject: (error: unknown) => void;
}

interface BufferedPatch {
  changes: PersonalAppearanceChange;
  requests: PatchRequest[];
  generation: number;
  teamId: string;
}

interface PersonalAppearanceContextValue {
  snapshot: PersonalAppearanceSnapshot | null;
  status: PersonalAppearanceStatus;
  errorCode: string | null;
  pendingFields: ReadonlySet<PersonalAppearanceField>;
  failedFields: ReadonlySet<PersonalAppearanceField>;
  load: (force?: boolean) => Promise<PersonalAppearanceSnapshot | null>;
  patch: (changes: PersonalAppearanceChange) => Promise<void>;
  compile: (
    candidate: PersonalAppearanceOverrides,
    candidateId: string,
    signal?: AbortSignal,
  ) => Promise<PersonalAppearanceCompileResult>;
  reset: () => Promise<void>;
  undo: (resetId: string) => Promise<void>;
}

const PersonalAppearanceContext =
  createContext<PersonalAppearanceContextValue | null>(null);

const CHANNEL_NAME = "bowhead-personal-appearance-v3";

export function PersonalAppearanceProvider({
  children,
}: {
  children: ReactNode;
}) {
  const t = useTranslations("settings.appearance.saveState");
  const [snapshot, setSnapshot] = useState<PersonalAppearanceSnapshot | null>(
    null,
  );
  const [status, setStatus] = useState<PersonalAppearanceStatus>("idle");
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [pendingFields, setPendingFields] = useState<
    ReadonlySet<PersonalAppearanceField>
  >(new Set());
  const [failedFields, setFailedFields] = useState<
    ReadonlySet<PersonalAppearanceField>
  >(new Set());
  const snapshotRef = useRef<PersonalAppearanceSnapshot | null>(null);
  const statusRef = useRef<PersonalAppearanceStatus>("idle");
  const loadControllerRef = useRef<AbortController | null>(null);
  const broadcastChannelRef = useRef<BroadcastChannel | null>(null);
  const generationRef = useRef(0);
  const mutationChainRef = useRef<Promise<void>>(Promise.resolve());
  const localChangesRef = useRef<PersonalAppearanceChange>({});
  const pendingFieldCountsRef = useRef(
    new Map<PersonalAppearanceField, number>(),
  );
  const patchBufferRef = useRef<BufferedPatch | null>(null);
  const patchTimerRef = useRef<number | null>(null);
  const undoRef = useRef<(resetId: string) => Promise<void>>(async () => {});

  const updateStatus = useCallback((next: PersonalAppearanceStatus) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  const commitSnapshot = useCallback((next: PersonalAppearanceSnapshot) => {
    snapshotRef.current = next;
    setSnapshot(applyLocalChanges(next, localChangesRef.current));
  }, []);

  const updatePendingFields = useCallback(() => {
    setPendingFields(new Set(pendingFieldCountsRef.current.keys()));
  }, []);

  const addPendingFields = useCallback(
    (fields: PersonalAppearanceField[]) => {
      for (const field of fields) {
        pendingFieldCountsRef.current.set(
          field,
          (pendingFieldCountsRef.current.get(field) ?? 0) + 1,
        );
      }
      updatePendingFields();
    },
    [updatePendingFields],
  );

  const removePendingFields = useCallback(
    (fields: PersonalAppearanceField[]) => {
      for (const field of fields) {
        const next = (pendingFieldCountsRef.current.get(field) ?? 1) - 1;
        if (next > 0) pendingFieldCountsRef.current.set(field, next);
        else pendingFieldCountsRef.current.delete(field);
      }
      updatePendingFields();
    },
    [updatePendingFields],
  );

  const mergeLocalChanges = useCallback(
    (changes: PersonalAppearanceChange) => {
      localChangesRef.current = {
        ...localChangesRef.current,
        ...changes,
      };
      const confirmed = snapshotRef.current;
      if (confirmed) {
        setSnapshot(applyLocalChanges(confirmed, localChangesRef.current));
      }
    },
    [],
  );

  const clearMatchingLocalChanges = useCallback(
    (changes: PersonalAppearanceChange) => {
      for (const field of appearanceChangeFields(changes)) {
        if (
          sameAppearanceValue(
            localChangesRef.current[field],
            changes[field],
          )
        ) {
          delete localChangesRef.current[field];
        }
      }
      const confirmed = snapshotRef.current;
      if (confirmed) {
        setSnapshot(applyLocalChanges(confirmed, localChangesRef.current));
      }
    },
    [],
  );

  const load = useCallback(
    async (force = false): Promise<PersonalAppearanceSnapshot | null> => {
      const existing = snapshotRef.current;
      if (existing && !force) return existing;
      loadControllerRef.current?.abort();
      const controller = new AbortController();
      loadControllerRef.current = controller;
      const generation = generationRef.current;
      updateStatus("loading");
      setErrorCode(null);
      try {
        const next = await getPersonalAppearance({
          etag: existing?.etag,
          signal: controller.signal,
        });
        if (controller.signal.aborted || generation !== generationRef.current) {
          return null;
        }
        if (next) commitSnapshot(next);
        setFailedFields(new Set());
        updateStatus("saved");
        return next ?? existing;
      } catch (error) {
        if (controller.signal.aborted || generation !== generationRef.current) {
          return null;
        }
        const code = appearanceErrorCode(error);
        setErrorCode(code);
        updateStatus(
          code === "personal_theme_conflict" ? "conflict" : "error",
        );
        throw error;
      } finally {
        if (loadControllerRef.current === controller) {
          loadControllerRef.current = null;
        }
      }
    },
    [commitSnapshot, updateStatus],
  );

  const applyCommittedSnapshot = useCallback(
    async (
      next: PersonalAppearanceSnapshot,
      generation: number,
      teamId: string,
      markSaved = true,
    ): Promise<boolean> => {
      if (generation !== generationRef.current) return false;
      commitSnapshot(next);
      setErrorCode(null);
      const applied = await refreshEffectiveTheme(teamId || undefined).catch(
        () => false,
      );
      if (generation !== generationRef.current) return false;
      broadcastChannelRef.current?.postMessage({
        kind: "personal-appearance-changed",
        teamScope: teamId,
        revisionOrRowVersion: next.resource.rowVersion,
      });
      if (applied) {
        if (markSaved) {
          setFailedFields(new Set());
          updateStatus("saved");
        }
        return true;
      } else {
        setErrorCode("personal_theme_runtime_refresh_failed");
        updateStatus("error");
        toast({
          kind: "error",
          text: t("runtimeFailed"),
          action: {
            label: t("reopen"),
            onAction: () => navigateToPersonalSettings("appearance"),
          },
          durationMs: 8000,
        });
        return false;
      }
    },
    [commitSnapshot, t, updateStatus],
  );

  const enqueueMutation = useCallback(
    (run: () => Promise<void>): Promise<void> => {
      const queued = mutationChainRef.current.catch(() => undefined).then(run);
      mutationChainRef.current = queued;
      return queued;
    },
    [],
  );

  const handleMutationFailure = useCallback(
    (error: unknown) => {
      const code = appearanceErrorCode(error);
      setErrorCode(code);
      updateStatus(
        code === "personal_theme_conflict" ? "conflict" : "error",
      );
      toast({
        kind: code === "personal_theme_conflict" ? "warning" : "error",
        text:
          code === "personal_theme_conflict"
            ? t("conflictToast")
            : t("failedToast"),
        action: {
          label: t("reopen"),
          onAction: () => navigateToPersonalSettings("appearance"),
        },
        durationMs: 8000,
      });
    },
    [t, updateStatus],
  );

  const flushPatchBuffer = useCallback(() => {
    if (patchTimerRef.current !== null) {
      window.clearTimeout(patchTimerRef.current);
      patchTimerRef.current = null;
    }
    const batch = patchBufferRef.current;
    patchBufferRef.current = null;
    if (!batch) return;

    const operation = enqueueMutation(async () => {
      if (batch.generation !== generationRef.current) {
        clearMatchingLocalChanges(batch.changes);
        return;
      }
      updateStatus("saving");
      setErrorCode(null);
      try {
        const current = snapshotRef.current ?? (await load());
        if (!current || batch.generation !== generationRef.current) {
          clearMatchingLocalChanges(batch.changes);
          return;
        }
        const next = await patchPersonalAppearance(
          current.etag,
          batch.changes,
        );
        await applyCommittedSnapshot(
          next,
          batch.generation,
          batch.teamId,
          false,
        );
        clearMatchingLocalChanges(batch.changes);
        setFailedFields((currentFields) => {
          const nextFields = new Set(currentFields);
          for (const field of appearanceChangeFields(batch.changes)) {
            nextFields.delete(field);
          }
          return nextFields;
        });
      } catch (error) {
        clearMatchingLocalChanges(batch.changes);
        if (batch.generation === generationRef.current) {
          setFailedFields((currentFields) => {
            const nextFields = new Set(currentFields);
            for (const field of appearanceChangeFields(batch.changes)) {
              nextFields.add(field);
            }
            return nextFields;
          });
          handleMutationFailure(error);
        }
        throw error;
      }
    });

    void operation
      .then(
        () => {
          for (const request of batch.requests) request.resolve();
        },
        (error: unknown) => {
          for (const request of batch.requests) request.reject(error);
        },
      )
      .finally(() => {
        for (const request of batch.requests) {
          removePendingFields(request.fields);
        }
        if (
          pendingFieldCountsRef.current.size === 0 &&
          statusRef.current === "saving"
        ) {
          updateStatus("saved");
        }
      });
  }, [
    applyCommittedSnapshot,
    clearMatchingLocalChanges,
    enqueueMutation,
    handleMutationFailure,
    load,
    removePendingFields,
    updateStatus,
  ]);

  const patch = useCallback(
    (changes: PersonalAppearanceChange) => {
      const fields = appearanceChangeFields(changes);
      if (!fields.length) return Promise.resolve();
      const generation = generationRef.current;
      const teamId = getActiveTeamId();
      addPendingFields(fields);
      mergeLocalChanges(changes);
      setFailedFields((currentFields) => {
        const nextFields = new Set(currentFields);
        for (const field of fields) nextFields.delete(field);
        return nextFields;
      });
      updateStatus("saving");

      return new Promise<void>((resolve, reject) => {
        const request = { fields, resolve, reject };
        const buffered = patchBufferRef.current;
        if (
          buffered &&
          buffered.generation === generation &&
          buffered.teamId === teamId
        ) {
          buffered.changes = { ...buffered.changes, ...changes };
          buffered.requests.push(request);
        } else {
          if (buffered) flushPatchBuffer();
          patchBufferRef.current = {
            changes: { ...changes },
            requests: [request],
            generation,
            teamId,
          };
        }
        if (patchTimerRef.current !== null) {
          window.clearTimeout(patchTimerRef.current);
        }
        patchTimerRef.current = window.setTimeout(flushPatchBuffer, 120);
      });
    },
    [
      addPendingFields,
      flushPatchBuffer,
      mergeLocalChanges,
      updateStatus,
    ],
  );

  const compile = useCallback(
    async (
      candidate: PersonalAppearanceOverrides,
      candidateId: string,
      signal?: AbortSignal,
    ) => {
      flushPatchBuffer();
      await mutationChainRef.current.catch(() => undefined);
      const current = snapshotRef.current ?? (await load());
      if (!current) throw new Error("Personal appearance is unavailable");
      return compilePersonalAppearance(
        current.etag,
        candidate,
        candidateId,
        signal,
      );
    },
    [flushPatchBuffer, load],
  );

  const undo = useCallback(
    (resetId: string) => {
      flushPatchBuffer();
      return enqueueMutation(async () => {
        const generation = generationRef.current;
        const teamId = getActiveTeamId();
        const current = snapshotRef.current ?? (await load());
        if (!current || generation !== generationRef.current) return;
        updateStatus("undoing");
        setErrorCode(null);
        try {
          const next = await undoPersonalAppearance(
            current.etag,
            resetId,
          );
          await applyCommittedSnapshot(next, generation, teamId);
        } catch (error) {
          handleMutationFailure(error);
          throw error;
        }
      });
    },
    [
      applyCommittedSnapshot,
      enqueueMutation,
      flushPatchBuffer,
      handleMutationFailure,
      load,
      updateStatus,
    ],
  );
  undoRef.current = undo;

  const reset = useCallback(
    () => {
      flushPatchBuffer();
      return enqueueMutation(async () => {
        const generation = generationRef.current;
        const teamId = getActiveTeamId();
        const current = snapshotRef.current ?? (await load());
        if (!current || generation !== generationRef.current) return;
        updateStatus("resetting");
        setErrorCode(null);
        try {
          const result = await resetPersonalAppearance(
            current.etag,
            createIdempotencyKey(),
          );
          await applyCommittedSnapshot(result.snapshot, generation, teamId);
          if (result.undo && generation === generationRef.current) {
            const resetId = result.undo.resetId;
            toast({
              kind: "success",
              undo: true,
              text: t("resetDone"),
              action: {
                label: t("undo"),
                onAction: () => undoRef.current(resetId),
              },
              durationMs: 10 * 60 * 1000,
            });
          }
        } catch (error) {
          handleMutationFailure(error);
          throw error;
        }
      });
    },
    [
      applyCommittedSnapshot,
      enqueueMutation,
      flushPatchBuffer,
      handleMutationFailure,
      load,
      t,
      updateStatus,
    ],
  );

  useEffect(() => {
    const onTeamChanged = () => {
      generationRef.current += 1;
      loadControllerRef.current?.abort();
      if (patchTimerRef.current !== null) {
        window.clearTimeout(patchTimerRef.current);
        patchTimerRef.current = null;
      }
      const buffered = patchBufferRef.current;
      patchBufferRef.current = null;
      if (buffered) {
        for (const request of buffered.requests) request.resolve();
      }
      localChangesRef.current = {};
      pendingFieldCountsRef.current.clear();
      setPendingFields(new Set());
      setFailedFields(new Set());
      snapshotRef.current = null;
      setSnapshot(null);
      setErrorCode(null);
      updateStatus("idle");
    };
    window.addEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
    return () => {
      window.removeEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
      loadControllerRef.current?.abort();
      if (patchTimerRef.current !== null) {
        window.clearTimeout(patchTimerRef.current);
      }
    };
  }, [updateStatus]);

  useEffect(() => {
    const onSaved = (event: Event) => {
      const teamId = (event as CustomEvent<{ teamId?: string }>).detail?.teamId;
      if (!teamId || teamId !== getActiveTeamId()) return;
      if (["saving", "resetting", "undoing"].includes(statusRef.current)) return;
      void load(true).catch(() => undefined);
    };
    window.addEventListener(PERSONAL_APPEARANCE_REFRESH_EVENT, onSaved);
    return () => window.removeEventListener(PERSONAL_APPEARANCE_REFRESH_EVENT, onSaved);
  }, [load]);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(CHANNEL_NAME);
    broadcastChannelRef.current = channel;
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const data = event.data;
      if (!isAppearanceBroadcast(data)) return;
      if (data.teamScope !== getActiveTeamId()) return;
      if (["saving", "resetting", "undoing"].includes(statusRef.current)) {
        return;
      }
      void load(true).catch(() => undefined);
    };
    return () => {
      if (broadcastChannelRef.current === channel) {
        broadcastChannelRef.current = null;
      }
      channel.close();
    };
  }, [load]);

  const value = useMemo<PersonalAppearanceContextValue>(
    () => ({
      snapshot,
      status,
      errorCode,
      pendingFields,
      failedFields,
      load,
      patch,
      compile,
      reset,
      undo,
    }),
    [
      compile,
      errorCode,
      failedFields,
      load,
      patch,
      pendingFields,
      reset,
      snapshot,
      status,
      undo,
    ],
  );

  return (
    <PersonalAppearanceContext.Provider value={value}>
      {children}
    </PersonalAppearanceContext.Provider>
  );
}

export function usePersonalAppearance(): PersonalAppearanceContextValue {
  const value = useContext(PersonalAppearanceContext);
  if (!value) {
    throw new Error(
      "usePersonalAppearance must be used inside PersonalAppearanceProvider",
    );
  }
  return value;
}

function appearanceErrorCode(error: unknown): string {
  return error instanceof ApiError && error.code
    ? error.code
    : "personal_theme_unknown_error";
}

const APPEARANCE_FIELDS: PersonalAppearanceField[] = [
  "mode",
  "accent",
  "background",
  "typographyProfile",
  "typeScale",
  "density",
  "radius",
  "motion",
];

function appearanceChangeFields(
  changes: PersonalAppearanceChange,
): PersonalAppearanceField[] {
  return APPEARANCE_FIELDS.filter((field) =>
    Object.prototype.hasOwnProperty.call(changes, field),
  );
}

function sameAppearanceValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function applyLocalChanges(
  snapshot: PersonalAppearanceSnapshot,
  changes: PersonalAppearanceChange,
): PersonalAppearanceSnapshot {
  const overrides = { ...snapshot.resource.overrides };
  const effective = { ...snapshot.resource.effective };
  const sources = { ...snapshot.resource.sources };
  const inheritedSource = (
    field: PersonalAppearanceField,
  ): "product" | "team_default" =>
    snapshot.resource.team.revisionNumber > 0 &&
    ["accent", "background", "typographyProfile", "density", "radius"].includes(
      field,
    )
      ? "team_default"
      : "product";

  if (Object.prototype.hasOwnProperty.call(changes, "mode")) {
    if (changes.mode === null) {
      delete overrides.mode;
      sources.mode = inheritedSource("mode");
    } else if (changes.mode !== undefined) {
      overrides.mode = changes.mode;
      effective.mode = changes.mode;
      sources.mode = "user";
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "accent")) {
    if (changes.accent === null) {
      delete overrides.accent;
      sources.accent = inheritedSource("accent");
    } else if (changes.accent !== undefined) {
      const accent = changes.accent;
      overrides.accent = accent;
      sources.accent = "user";
      if (effective.colorScheme) {
        const registryItem =
          accent.kind === "preset"
            ? snapshot.resource.registries.accent.items.find(
                (item) => item.id === accent.id,
              )
            : null;
        const lightAccent =
          accent.kind === "custom"
            ? accent.seed
            : registryItem?.swatch.light?.primary;
        const darkAccent =
          accent.kind === "custom"
            ? accent.seed
            : registryItem?.swatch.dark?.primary;
        effective.colorScheme = {
          ...effective.colorScheme,
          light: {
            ...effective.colorScheme.light,
            ...(lightAccent
              ? { accent: lightAccent, focus: lightAccent }
              : {}),
            ...(registryItem?.swatch.light?.onPrimary
              ? { accentInk: registryItem.swatch.light.onPrimary }
              : {}),
          },
          dark: {
            ...effective.colorScheme.dark,
            ...(darkAccent ? { accent: darkAccent, focus: darkAccent } : {}),
            ...(registryItem?.swatch.dark?.onPrimary
              ? { accentInk: registryItem.swatch.dark.onPrimary }
              : {}),
          },
        };
      }
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "background")) {
    if (changes.background === null) {
      delete overrides.background;
      sources.background = inheritedSource("background");
    } else if (changes.background !== undefined) {
      const background = changes.background;
      overrides.background = background;
      sources.background = "user";
      if (effective.colorScheme) {
        const registryItem =
          background.kind === "preset"
            ? snapshot.resource.registries.background.items.find(
                (item) => item.id === background.id,
              )
            : null;
        const light: {
          primary: string;
          secondary?: string;
          onPrimary?: string;
        } | undefined =
          background.kind === "custom"
            ? { primary: background.light }
            : registryItem?.swatch.light;
        const dark: {
          primary: string;
          secondary?: string;
          onPrimary?: string;
        } | undefined =
          background.kind === "custom"
            ? { primary: background.dark }
            : registryItem?.swatch.dark;
        effective.colorScheme = {
          ...effective.colorScheme,
          light: {
            ...effective.colorScheme.light,
            ...(light?.primary ? { background: light.primary } : {}),
            ...(light?.secondary ? { surface: light.secondary } : {}),
            ...(light?.onPrimary ? { ink: light.onPrimary } : {}),
          },
          dark: {
            ...effective.colorScheme.dark,
            ...(dark?.primary ? { background: dark.primary } : {}),
            ...(dark?.secondary ? { surface: dark.secondary } : {}),
            ...(dark?.onPrimary ? { ink: dark.onPrimary } : {}),
          },
        };
      }
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "typographyProfile")) {
    if (changes.typographyProfile === null) {
      delete overrides.typographyProfile;
      sources.typographyProfile = inheritedSource("typographyProfile");
    } else if (changes.typographyProfile !== undefined) {
      overrides.typographyProfile = changes.typographyProfile;
      effective.typographyProfile = changes.typographyProfile;
      sources.typographyProfile = "user";
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "typeScale")) {
    if (changes.typeScale === null) {
      delete overrides.typeScale;
      sources.typeScale = inheritedSource("typeScale");
    } else if (changes.typeScale !== undefined) {
      overrides.typeScale = changes.typeScale;
      effective.typeScale = changes.typeScale;
      sources.typeScale = "user";
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "density")) {
    if (changes.density === null) {
      delete overrides.density;
      sources.density = inheritedSource("density");
    } else if (changes.density !== undefined) {
      overrides.density = changes.density;
      effective.density = changes.density;
      sources.density = "user";
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "radius")) {
    if (changes.radius === null) {
      delete overrides.radius;
      sources.radius = inheritedSource("radius");
    } else if (changes.radius !== undefined) {
      overrides.radius = changes.radius;
      effective.radius = changes.radius;
      sources.radius = "user";
    }
  }
  if (Object.prototype.hasOwnProperty.call(changes, "motion")) {
    if (changes.motion === null) {
      delete overrides.motion;
      sources.motion = inheritedSource("motion");
    } else if (changes.motion !== undefined) {
      overrides.motion = changes.motion;
      effective.motion = changes.motion;
      sources.motion = "user";
    }
  }

  return {
    ...snapshot,
    resource: {
      ...snapshot.resource,
      overrides,
      effective,
      sources,
    },
  };
}

function createIdempotencyKey(): string {
  return `personal-reset-${randomUuid()}`;
}

function isAppearanceBroadcast(value: unknown): value is {
  kind: "personal-appearance-changed";
  teamScope: string;
  revisionOrRowVersion: number;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const raw = value as Record<string, unknown>;
  return (
    raw.kind === "personal-appearance-changed" &&
    typeof raw.teamScope === "string" &&
    typeof raw.revisionOrRowVersion === "number"
  );
}
