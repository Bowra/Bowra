"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";

import {
  setSubagentAudience,
  TeamSubagentApiError,
  type TeamSubagentOwnerManagerDetail,
} from "@/lib/team-subagents-api";
import {
  fetchTeamDirectory,
  fetchTeamGroups,
  type Audience,
  type DirectoryGroup,
  type DirectoryUser,
} from "@/lib/content-share-api";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import {
  ShAvatar,
  ShGroupAvatar,
  ShIcon,
  ShSegmented,
  ShTypeTag,
  T,
  avatarColor,
} from "@/components/share/share-ui";

type PrincipalType = "user" | "group";

interface DraftPrincipal {
  type: PrincipalType;
  id: string;
  name: string;
  meta?: string;
}

function principalKey(type: PrincipalType, id: string): string {
  return `${type}:${id}`;
}

function rosterFromDetail(
  detail: TeamSubagentOwnerManagerDetail,
): Map<string, DraftPrincipal> {
  return new Map(
    detail.audience.roster.map((principal) => {
      const type = principal.principal_type;
      return [
        principalKey(type, principal.principal_id),
        {
          type,
          id: principal.principal_id,
          name: principal.principal_name || principal.principal_id,
        },
      ];
    }),
  );
}

function sameRoster(
  detail: TeamSubagentOwnerManagerDetail,
  selected: Map<string, DraftPrincipal>,
): boolean {
  const current = new Set(
    detail.audience.roster.map((principal) =>
      principalKey(principal.principal_type, principal.principal_id)),
  );
  return (
    current.size === selected.size &&
    [...selected.keys()].every((key) => current.has(key))
  );
}

export function SubagentAudienceDialog({
  detail: initialDetail,
  onClose,
  onSaved,
  onOpenReleaseManager,
}: {
  detail: TeamSubagentOwnerManagerDetail;
  onClose: () => void;
  onSaved: (detail: TeamSubagentOwnerManagerDetail) => void;
  onOpenReleaseManager: () => void;
}) {
  const t = useTranslations("settings.subagents");
  const [detail, setDetail] = useState(initialDetail);
  const [mode, setMode] = useState<Audience>(initialDetail.audience.mode);
  const [selected, setSelected] = useState(
    () => rosterFromDetail(initialDetail),
  );
  const [query, setQuery] = useState("");
  const [users, setUsers] = useState<DirectoryUser[]>([]);
  const [groups, setGroups] = useState<DirectoryGroup[]>([]);
  const [directoryBusy, setDirectoryBusy] = useState(false);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [releaseManaged, setReleaseManaged] = useState(false);
  const saveGuard = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setDirectoryBusy(true);
      setDirectoryError(null);
      const term = query.trim();
      try {
        const [directory, groupDirectory] = await Promise.all([
          fetchTeamDirectory(term || undefined, 0),
          fetchTeamGroups(),
        ]);
        if (cancelled) return;
        setUsers(directory.items);
        setGroups(
          groupDirectory.items.filter((group) =>
            !term ||
            group.name.toLocaleLowerCase().includes(
              term.toLocaleLowerCase(),
            )),
        );
        setSelected((current) => {
          const resolved = new Map(current);
          for (const user of directory.items) {
            const key = principalKey("user", user.id);
            if (resolved.has(key)) {
              resolved.set(key, {
                type: "user",
                id: user.id,
                name: user.display_name,
                meta: user.email_masked,
              });
            }
          }
          for (const group of groupDirectory.items) {
            const key = principalKey("group", group.id);
            if (resolved.has(key)) {
              resolved.set(key, {
                type: "group",
                id: group.id,
                name: group.name,
                meta: t("audience_group_members", {
                  count: group.member_count,
                }),
              });
            }
          }
          return resolved;
        });
      } catch {
        if (cancelled) return;
        setUsers([]);
        setGroups([]);
        setDirectoryError(t("audience_directory_failed"));
      } finally {
        if (!cancelled) setDirectoryBusy(false);
      }
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, t]);

  const selectedRows = useMemo(
    () => [...selected.values()].sort((a, b) =>
      a.type.localeCompare(b.type) || a.name.localeCompare(b.name)),
    [selected],
  );
  const suggestions = useMemo(
    () => [
      ...groups
        .filter((group) => !selected.has(principalKey("group", group.id)))
        .map((group) => ({
          type: "group" as const,
          id: group.id,
          name: group.name,
          meta: t("audience_group_members", {
            count: group.member_count,
          }),
        })),
      ...users
        .filter((user) => !selected.has(principalKey("user", user.id)))
        .map((user) => ({
          type: "user" as const,
          id: user.id,
          name: user.display_name,
          meta: user.email_masked,
        })),
    ].slice(0, 12),
    [groups, selected, t, users],
  );
  const effectiveSelected = mode === "custom" ? selected : new Map();
  const customInvalid = mode === "custom" && selected.size === 0;
  const tooLarge = mode === "custom" && selected.size > 200;
  const dirty = (
    detail.audience.mode !== mode ||
    (
      mode === "custom" &&
      !sameRoster(detail, selected)
    )
  );

  const addPrincipal = (principal: DraftPrincipal) => {
    setSelected((current) => {
      if (current.size >= 200) return current;
      const next = new Map(current);
      next.set(principalKey(principal.type, principal.id), principal);
      return next;
    });
    setQuery("");
    setError(null);
  };

  const removePrincipal = (principal: DraftPrincipal) => {
    setSelected((current) => {
      const next = new Map(current);
      next.delete(principalKey(principal.type, principal.id));
      return next;
    });
    setError(null);
  };

  const save = async () => {
    if (
      saveGuard.current ||
      customInvalid ||
      tooLarge ||
      !dirty
    ) return;
    saveGuard.current = true;
    setSaving(true);
    setError(null);
    setReleaseManaged(false);
    const audience = {
      mode,
      user_ids: [...effectiveSelected.values()]
        .filter((principal) => principal.type === "user")
        .map((principal) => principal.id),
      group_ids: [...effectiveSelected.values()]
        .filter((principal) => principal.type === "group")
        .map((principal) => principal.id),
    };
    try {
      const updated = await setSubagentAudience(detail.id, audience);
      onSaved(updated);
    } catch (cause) {
      if (
        cause instanceof TeamSubagentApiError
        && cause.code === "agent_revision_writer_required"
      ) {
        setReleaseManaged(true);
        setError(t("audience_release_managed"));
        return;
      }
      setError(
        t("audience_save_failed", {
          error: t("audience_unknown_reason"),
        }),
      );
    } finally {
      saveGuard.current = false;
      setSaving(false);
    }
  };

  return (
    <Dialog
      title={t("audience_title", {
        name: detail.display_name || detail.name,
      })}
      onClose={onClose}
      width={620}
      busy={saving}
      footer={(
        <>
          <span
            style={{
              flex: 1,
              color: T.ink4,
              fontSize: "0.71875rem",
              lineHeight: 1.5,
            }}
          >
            {t("audience_atomic_note")}
          </span>
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={saving}
          >
            {t("audience_cancel")}
          </Button>
          <Button
            variant="primary"
            onClick={() => void save()}
            disabled={
              saving ||
              customInvalid ||
              tooLarge ||
              !dirty
            }
          >
            {saving
              ? t("audience_saving")
              : t("audience_save")}
          </Button>
        </>
      )}
    >
      <p
        style={{
          margin: "0 0 14px",
          color: T.ink3,
          fontSize: "0.78125rem",
          lineHeight: 1.65,
        }}
      >
        {t("audience_description")}
      </p>
      <ShSegmented
        active={mode}
        onPick={(nextMode) => {
          setMode(nextMode);
          setError(null);
        }}
        disabled={saving}
      />

      {error ? (
        <Notice tone={releaseManaged ? "info" : "error"} style={{ marginTop: 14 }}>
          {error}
          {releaseManaged ? (
            <Button
              variant="secondary"
              onClick={onOpenReleaseManager}
              style={{ marginLeft: 12 }}
            >
              {t("audience_open_release_manager")}
            </Button>
          ) : null}
        </Notice>
      ) : null}
      {directoryError ? (
        <Notice tone="error" style={{ marginTop: 14 }}>
          {directoryError}
        </Notice>
      ) : null}

      {mode === "private" ? (
        <AudienceExplanation
          icon="lock"
          title={t("audience_private_title")}
          body={t("audience_private_body", {
            count: detail.audience.roster.length,
          })}
        />
      ) : null}

      {mode === "team_wide" ? (
        <AudienceExplanation
          icon="globe"
          title={t("audience_team_title")}
          body={t("audience_team_body", {
            count: detail.audience.roster.length,
          })}
        />
      ) : null}

      {mode === "custom" ? (
        <div style={{ marginTop: 16 }}>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              height: 42,
              padding: "0 13px",
              border: `1px solid ${T.line}`,
              borderRadius: 10,
              background: T.surface,
              color: T.ink4,
            }}
          >
            <ShIcon name="search" size={14} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("audience_search_placeholder")}
              disabled={saving}
              style={{
                flex: 1,
                border: 0,
                outline: 0,
                background: "transparent",
                color: T.ink,
                font: "inherit",
                fontSize: "0.8125rem",
              }}
            />
            {directoryBusy ? (
              <span style={{ fontSize: "0.6875rem", color: T.ink4 }}>
                {t("audience_searching")}
              </span>
            ) : null}
          </label>

          {suggestions.length > 0 ? (
            <div
              style={{
                marginTop: 8,
                border: `1px solid ${T.line}`,
                borderRadius: 10,
                background: T.bg,
                padding: 5,
                maxHeight: 196,
                overflowY: "auto",
              }}
            >
              {suggestions.map((principal) => (
                <PrincipalRow
                  key={principalKey(principal.type, principal.id)}
                  principal={principal}
                  actionLabel={t("audience_add")}
                  onAction={() => addPrincipal(principal)}
                />
              ))}
            </div>
          ) : null}

          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 10,
              margin: "16px 2px 7px",
            }}
          >
            <strong style={{ fontSize: "0.78125rem", color: T.ink2 }}>
              {t("audience_selected_count", {
                count: selected.size,
              })}
            </strong>
            <span style={{ flex: 1 }} />
            <span style={{ fontSize: "0.71875rem", color: T.ink4 }}>
              {t("audience_limit")}
            </span>
          </div>

          {selectedRows.length ? (
            <div
              style={{
                border: `1px solid ${T.line}`,
                borderRadius: 10,
                overflow: "hidden",
                maxHeight: 250,
                overflowY: "auto",
              }}
            >
              {selectedRows.map((principal, index) => (
                <div
                  key={principalKey(principal.type, principal.id)}
                  style={{
                    borderTop: index ? `1px solid ${T.line2}` : 0,
                  }}
                >
                  <PrincipalRow
                    principal={principal}
                    actionLabel={t("audience_remove")}
                    onAction={() => removePrincipal(principal)}
                  />
                </div>
              ))}
            </div>
          ) : (
            <Notice tone="muted">
              {t("audience_custom_empty")}
            </Notice>
          )}
        </div>
      ) : null}
    </Dialog>
  );
}

function AudienceExplanation({
  icon,
  title,
  body,
}: {
  icon: "lock" | "globe";
  title: string;
  body: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 12,
        marginTop: 16,
        padding: "16px",
        border: `1px solid ${T.line}`,
        borderRadius: 12,
        background: T.bg,
      }}
    >
      <span
        style={{
          width: 34,
          height: 34,
          flex: "0 0 34px",
          borderRadius: 10,
          display: "grid",
          placeItems: "center",
          color: T.accent,
          background: T.accentTint,
        }}
      >
        <ShIcon name={icon} size={16} />
      </span>
      <div>
        <div style={{ fontSize: "0.84375rem", fontWeight: 650, color: T.ink }}>
          {title}
        </div>
        <div
          style={{
            marginTop: 3,
            fontSize: "0.75rem",
            color: T.ink3,
            lineHeight: 1.6,
          }}
        >
          {body}
        </div>
      </div>
    </div>
  );
}

function PrincipalRow({
  principal,
  actionLabel,
  onAction,
}: {
  principal: DraftPrincipal;
  actionLabel: string;
  onAction: () => void;
}) {
  const t = useTranslations("settings.subagents");
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        minHeight: 46,
        padding: "6px 9px",
      }}
    >
      {principal.type === "group" ? (
        <ShGroupAvatar size={28} />
      ) : (
        <ShAvatar
          name={principal.name}
          color={avatarColor(principal.id)}
          size={28}
        />
      )}
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            color: T.ink,
            fontSize: "0.8125rem",
            fontWeight: 600,
          }}
        >
          {principal.name}
        </div>
        {principal.meta ? (
          <div
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              color: T.ink4,
              fontSize: "0.6875rem",
            }}
          >
            {principal.meta}
          </div>
        ) : null}
      </div>
      <ShTypeTag
        kind={
          principal.type === "group"
            ? t("audience_type_group")
            : t("audience_type_user")
        }
      />
      <Button
        size="sm"
        variant="ghost"
        onClick={onAction}
      >
        {actionLabel}
      </Button>
    </div>
  );
}
