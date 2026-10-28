"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Field } from "@/components/ui/field";
import { CanvasSection, SectionHeading } from "@/components/ui/section-heading";
import styles from "./space-general.module.css";
import { Notice } from "@/components/ui/notice";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { SaveBar } from "@/components/ui/save-bar";
import { OpenSettingsInCanvas } from "@/components/settings/open-settings-in-canvas";
import { SettingsWorkspaceI18n, type WorkspaceI18nAgentInterface, type WorkspaceI18nLifecycle } from "@/components/settings/workspace-i18n";
import { getWorkspaceMetadata, updateWorkspaceMetadata, type WorkspaceMetadata } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { getActiveTeamId } from "@/lib/team-storage";
import { invalidateSpaces, useSpace } from "@/lib/use-space";
import {
  WORKSPACE_METADATA_SAVED_EVENT,
  notifyWorkspaceMetadataSaved,
  type WorkspaceSettingsSavedTarget,
} from "@/lib/workspace-settings-notifications";
import type { WorkspaceSettingsLifecycle } from "@/lib/workspace-settings-agent";

interface SpaceGeneralProps {
  workspaceId?: string;
  teamId?: string;
  userId?: string;
  hosted?: boolean;
  readOnly?: boolean;
  onLifecycleChange?: (state: WorkspaceSettingsLifecycle) => void;
  onAgentInterfaceChange?: (api: WorkspaceI18nAgentInterface | null) => void;
  onAccessDenied?: () => void;
}

/** Both routes retain this original page; a Canvas supplies an exact Space. */
export function SpaceGeneral(props: SpaceGeneralProps = {}) {
  const t = useTranslations("settings.space_general");
  const space = useSpace();
  const session = useOptionalAuthSession();
  const workspaceId = props.workspaceId ?? space.activeId;
  if (!workspaceId) return <Notice tone="muted">{t("no_active_space")}</Notice>;
  return (
    <SpaceGeneralPage
      key={workspaceId}
      {...props}
      teamId={props.teamId ?? session?.me?.team_id}
      userId={props.userId ?? session?.me?.id}
      workspaceId={workspaceId}
    />
  );
}

function SpaceGeneralPage({
  workspaceId,
  teamId,
  userId,
  hosted,
  readOnly,
  onLifecycleChange,
  onAgentInterfaceChange,
  onAccessDenied,
}: SpaceGeneralProps & { workspaceId: string }) {
  const t = useTranslations("settings.space_general");
  const [metadata, setMetadata] = useState<WorkspaceMetadata | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const [preferences, setPreferences] = useState<WorkspaceI18nLifecycle | null>(null);
  const [originalTeamId] = useState(() => teamId ?? getActiveTeamId());
  const mounted = useRef(false);
  const accessLost = useRef(false);
  const ticket = useRef(0);
  const editGeneration = useRef(0);
  const pending = useRef(false);
  const saved = useRef<WorkspaceMetadata | null>(null);
  const local = useRef({ name: "", description: "" });
  const refreshNeeded = useRef(false);
  const deny = useCallback(() => {
    accessLost.current = true;
    ticket.current += 1;
    saved.current = null;
    setMetadata(null);
    setName("");
    setDescription("");
    setPreferences(null);
    onAccessDenied?.();
  }, [onAccessDenied]);
  const install = (value: WorkspaceMetadata) => {
    saved.current = value;
    local.current = { name: value.name, description: value.description ?? "" };
    setMetadata(value);
    setName(value.name);
    setDescription(value.description ?? "");
  };
  const refresh = useCallback(async () => {
    if (accessLost.current || !mounted.current || (originalTeamId && getActiveTeamId() && originalTeamId !== getActiveTeamId())) return;
    if (pending.current) {
      refreshNeeded.current = true;
      return;
    }
    refreshNeeded.current = false;
    const originalGeneration = editGeneration.current;
    const readTicket = ++ticket.current;
    try {
      const value = await getWorkspaceMetadata(workspaceId);
      if (!mounted.current || accessLost.current || readTicket !== ticket.current) return;
      const dirty =
        saved.current && (local.current.name.trim() !== saved.current.name || local.current.description.trim() !== (saved.current.description ?? ""));
      if (pending.current || originalGeneration !== editGeneration.current || dirty) {
        refreshNeeded.current = true;
        // Keep the original write revision for newer input rather than silently rebasing it.
        if (saved.current && value.revision !== saved.current.revision) setError(t("save_failed"));
        return;
      }
      if (!saved.current || value.revision >= saved.current.revision) install(value);
    } catch (reason) {
      if (mounted.current && readTicket === ticket.current) {
        setError(t("load_failed"));
        if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) deny();
      }
    } finally {
      if (mounted.current && readTicket === ticket.current) setLoading(false);
    }
  }, [workspaceId, originalTeamId, t, deny]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const onSaved = (event: Event) => {
      const target = (event as CustomEvent<WorkspaceSettingsSavedTarget>).detail;
      if (target?.workspaceId === workspaceId && target.teamId === originalTeamId) void refresh();
    };
    window.addEventListener(WORKSPACE_METADATA_SAVED_EVENT, onSaved);
    return () => {
      mounted.current = false;
      ticket.current += 1;
      window.removeEventListener(WORKSPACE_METADATA_SAVED_EVENT, onSaved);
    };
  }, [workspaceId, originalTeamId, refresh]);
  useEffect(() => {
    onAgentInterfaceChange?.({
      requestReadback: async (receipt) => {
        if (
          !mounted.current ||
          accessLost.current ||
          receipt.form_id !== "workspace_metadata" ||
          receipt.status !== "applied" ||
          receipt.workspace_id !== workspaceId ||
          !receipt.owner_user_id ||
          (userId && receipt.owner_user_id !== userId) ||
          receipt.team_id !== originalTeamId
        )
          return false;
        await refresh();
        return true;
      },
    });
    return () => onAgentInterfaceChange?.(null);
  }, [workspaceId, originalTeamId, userId, refresh, onAgentInterfaceChange]);
  const dirtyFields = metadata
    ? [...(metadata.name !== name.trim() ? ["name"] : []), ...((metadata.description ?? "") !== description.trim() ? ["description"] : [])]
    : [];
  useEffect(() => {
    onLifecycleChange?.({
      workspaceId,
      savedValues: metadata && !accessLost.current ? { name: metadata.name, description: metadata.description ?? "" } : null,
      localValues: { name, description },
      revision: metadata?.revision,
      unconfirmedFields: [...dirtyFields, ...(preferences?.unconfirmedFields ?? [])],
      editGeneration: generation + (preferences?.editGeneration ?? 0),
      pendingWrites: Number(saving) + (preferences?.pendingWrites ?? 0),
      error,
    });
  }, [workspaceId, metadata, name, description, generation, saving, preferences, error, onLifecycleChange]);
  const edit = (key: "name" | "description", value: string) => {
    local.current[key] = value;
    editGeneration.current += 1;
    setGeneration(editGeneration.current);
    (key === "name" ? setName : setDescription)(value);
  };
  const save = async () => {
    const original = saved.current;
    if (
      !original ||
      !local.current.name.trim() ||
      pending.current ||
      accessLost.current ||
      readOnly ||
      (originalTeamId && getActiveTeamId() && originalTeamId !== getActiveTeamId())
    )
      return;
    pending.current = true;
    ticket.current += 1;
    setSaving(true);
    setError(null);
    const originalGeneration = editGeneration.current;
    try {
      const updated = await updateWorkspaceMetadata(workspaceId, {
        ...(local.current.name.trim() !== original.name ? { name: local.current.name.trim() } : {}),
        ...(local.current.description.trim() !== (original.description ?? "") ? { description: local.current.description.trim() || null } : {}),
        expected_revision: original.revision,
      });
      if (!mounted.current || accessLost.current || (originalTeamId && getActiveTeamId() && originalTeamId !== getActiveTeamId())) return;
      saved.current = updated;
      setMetadata(updated);
      if (originalGeneration === editGeneration.current) install(updated);
      invalidateSpaces();
      if (originalTeamId) notifyWorkspaceMetadataSaved({ teamId: originalTeamId, workspaceId });
    } catch (reason) {
      if (mounted.current) {
        setError(t("save_failed"));
        if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) deny();
      }
    } finally {
      pending.current = false;
      if (mounted.current) {
        setSaving(false);
        if (refreshNeeded.current) void refresh();
      }
    }
  };
  if (accessLost.current) return <Notice tone="error">{t("load_failed")}</Notice>;
  return (
    <div className={hosted ? styles.hostedStack : styles.page}>
      {/* Hosted in Canvas the object header (C4) replaces the page header. */}
      {!hosted ? (
        <header className="ui-page-head legacy-space-general-head">
          <div className={styles.headingCopy}>
            <h1 className="ui-page-title legacy-space-general-title">{t("title")}</h1>
            <p className="ui-page-desc legacy-space-general-desc">{t("subtitle")}</p>
          </div>
          {metadata ? (
            <OpenSettingsInCanvas
              workspaceId={workspaceId}
              page="space_general"
              disabled={dirtyFields.length > 0 || saving || Boolean(preferences?.pendingWrites || preferences?.unconfirmedFields.length)}
            />
          ) : null}
        </header>
      ) : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
      {loading ? (
        <LoadingSkeleton variant="form" rows={2} label={t("loading")} />
      ) : metadata ? (
        <>
          <GroupShell hosted={Boolean(hosted)} title={t("group_title")}>
              <Field id="space-general-name" label={t("name_label")}>
                <input
                  id="space-general-name"
                  className="settings-input"
                  value={name}
                  maxLength={120}
                  disabled={readOnly}
                  onChange={(event) => edit("name", event.target.value)}
                />
              </Field>
              <Field id="space-general-description" label={t("description_label")}>
                <textarea
                  id="space-general-description"
                  className="settings-input"
                  rows={4}
                  value={description}
                  maxLength={2000}
                  disabled={readOnly}
                  onChange={(event) => edit("description", event.target.value)}
                />
              </Field>
          </GroupShell>
          <SaveBar
            dirty={dirtyFields.length > 0}
            saving={saving}
            saveDisabled={!name.trim() || readOnly}
            onSave={() => void save()}
            onDiscard={() => {
              if (metadata) {
                editGeneration.current += 1;
                setGeneration(editGeneration.current);
                install(metadata);
                if (refreshNeeded.current) void refresh();
              }
            }}
          />
          <SettingsWorkspaceI18n workspaceId={workspaceId} workspaceName={metadata.name} hosted={hosted ? "section" : false} onLifecycleChange={setPreferences} onAccessDenied={deny} />
        </>
      ) : null}
    </div>
  );
}

/** The settings page card group, or a Canvas section with labels above the fields when hosted (M6, C5). */
function GroupShell({ hosted, title, children }: { hosted: boolean; title: string; children: React.ReactNode }) {
  if (hosted) {
    return (
      <CanvasSection labelledBy="space-general-group-title">
        <SectionHeading id="space-general-group-title" title={title} />
        {children}
      </CanvasSection>
    );
  }
  return (
    <section className="ui-group ui-space-general-group" aria-labelledby="space-general-group-title">
      <div className="ui-group-head">
        <h2 id="space-general-group-title" className="ui-group-title">
          {title}
        </h2>
      </div>
      <div className="ui-group-card">{children}</div>
    </section>
  );
}
