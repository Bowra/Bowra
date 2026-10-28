"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";
import {
  clearAgentRolePreference,
  getAgentRolePreferences,
  setAgentRolePreference,
  type AgentRoleId,
  type AgentRolePreferenceRole,
  type AgentRolePreferences,
} from "@/lib/agents-api";

function CandidateItem({ label, checked, onSelect, disabledReason }: { label: string; checked: boolean; onSelect: () => void; disabledReason?: string }) {
  const item = useMenuItem({ label, checked, role: "menuitemradio", onSelect, disabled: Boolean(disabledReason) });
  const reasonId = useId();
  return (
    <>
      <button type="button" className="ui-filter-menu-item" aria-describedby={disabledReason ? reasonId : undefined} {...item}><span>{label}</span></button>
      {disabledReason ? <MenuItemReason id={reasonId}>{disabledReason}</MenuItemReason> : null}
    </>
  );
}

function ClearItem({ label, onSelect }: { label: string; onSelect: () => void }) {
  const item = useMenuItem({ label, onSelect });
  return <button type="button" className="ui-filter-menu-item" {...item}><span>{label}</span></button>;
}

/** Menu button choosing a role's preferred Agent; picking a candidate saves it (replaces the native select + Save). */
function RolePreferenceMenu({
  role,
  disabled,
  onPick,
  onClear,
}: {
  role: AgentRolePreferenceRole;
  disabled: boolean;
  onPick: (assignmentId: string) => void;
  onClear: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const [open, setOpen] = useState(false);
  const saved = role.preference?.assignment_id ?? "";
  const selectedIndex = role.candidates.findIndex((candidate) => candidate.assignment_id === saved);
  const roleLabel = t(role.label_key as never);
  const menuLabel = t("role_preference_choose", { role: roleLabel });
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", offset: 4, selectedIndex: selectedIndex >= 0 ? selectedIndex : null, sheetTitle: menuLabel });
  const labelId = useId();
  const valueId = useId();
  const candidateLabel = (candidate: AgentRolePreferenceRole["candidates"][number]) =>
    candidate.is_primary ? `${candidate.display_name} · ${t("role_preference_primary")}` : candidate.display_name;
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className="ui-filter-menu-button am-role-preference-menu"
        data-active={saved ? "true" : undefined}
        // The name carries both the purpose and the visible value: "Choose an Agent for Data · Finance Partner".
        aria-labelledby={`${labelId} ${valueId}`}
        disabled={disabled}
        {...menu.getReferenceProps()}
      >
        <span id={labelId} className="sr-only">{menuLabel}</span>
        <span className="ui-filter-menu-button__text">
          <span id={valueId} className="ui-filter-menu-button__value">{role.preference?.display_name ?? t("role_preference_pick")}</span>
        </span>
        <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" className="ui-filter-menu-button__chevron" />
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ className: "ui-filter-menu", "aria-label": menuLabel })}>
          {role.candidates.map((candidate) => (
            <CandidateItem
              key={candidate.assignment_id}
              label={candidateLabel(candidate)}
              checked={candidate.assignment_id === saved}
              disabledReason={role.actions.set ? undefined : t("role_preference_set_unavailable")}
              onSelect={() => { if (candidate.assignment_id !== saved) onPick(candidate.assignment_id); }}
            />
          ))}
          {role.actions.clear ? (
            <>
              <div role="separator" className="ui-row-menu-sep" />
              <ClearItem label={t("role_preference_clear")} onSelect={onClear} />
            </>
          ) : null}
        </div>
      </MenuFloating>
    </>
  );
}

interface Props {
  rosterVersion: number;
}

/** A role worth choosing for: at least two verified candidates for this person (design §7.5). */
export function eligibleRolePreferenceRoles(value: AgentRolePreferences): AgentRolePreferenceRole[] {
  return value.roles.filter((role) => role.availability !== "behavior_unavailable" && role.candidates.length >= 2);
}

export function AgentRolePreferencesPanel({ rosterVersion }: Props) {
  const t = useTranslations("agents_marketplace");
  const [value, setValue] = useState<AgentRolePreferences | null>(null);
  const [pendingRole, setPendingRole] = useState<AgentRoleId | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<"load" | "save" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await getAgentRolePreferences();
      setValue(next);
      setError(null);
    } catch {
      setError("load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load, rosterVersion]);

  const save = async (roleId: AgentRoleId, assignmentId: string, rowVersion: number) => {
    setPendingRole(roleId);
    try {
      const next = await setAgentRolePreference(roleId, assignmentId, rowVersion);
      setValue(next);
      setError(null);
    } catch {
      setError("save");
    } finally {
      setPendingRole(null);
    }
  };

  const clear = async (roleId: AgentRoleId, rowVersion: number) => {
    setPendingRole(roleId);
    try {
      const next = await clearAgentRolePreference(roleId, rowVersion);
      setValue(next);
      setError(null);
    } catch {
      setError("save");
    } finally {
      setPendingRole(null);
    }
  };

  // The whole section stays hidden until it can help: while loading, when unreadable, and while no role
  // has two verified candidates. Nothing else on the page depends on it.
  const roles = value ? eligibleRolePreferenceRoles(value) : [];
  if (value === null || !roles.length) return null;

  return (
    <section className="am-role-preferences" aria-labelledby="am-role-preferences-title" aria-busy={loading}>
      <header>
        <div>
          <h2 id="am-role-preferences-title">{t("role_preferences_title")}</h2>
          <p>{t("role_preferences_body")}</p>
        </div>
      </header>
      {error ? <div className="am-role-preferences-notice" role="alert">{t(error === "load" ? "role_preferences_load_failed" : "role_preferences_save_failed")}</div> : null}
      <div className="am-role-preference-grid">
        {roles.map((role) => {
          const pending = pendingRole === role.role_id;
          const status = role.preference ? role.preference.display_name : t("role_preference_candidates", { count: role.candidates.length });
          return (
            <article key={role.role_id} className="am-role-preference-card" data-availability={role.availability}>
              <div className="am-role-preference-heading">
                <h3>{t(role.label_key as never)}</h3>
                {role.preference ? (
                  <span className="am-role-preference-badge" data-tone="positive">{t("role_preference_assigned_badge")}</span>
                ) : null}
              </div>
              <p className="am-role-preference-status">{status}</p>
              {role.invalid_reason ? <p className="am-role-preference-warning">{t("role_preference_stale")}</p> : null}
              <div className="am-role-preference-controls">
                <RolePreferenceMenu
                  role={role}
                  disabled={pending || loading || (!role.actions.set && !role.actions.clear)}
                  onPick={(assignmentId) => void save(role.role_id, assignmentId, role.row_version)}
                  onClear={() => void clear(role.role_id, role.row_version)}
                />
              </div>
            </article>
          );
        })}
      </div>
      {value.candidates_truncated ? <p className="am-role-preferences-footnote">{t("role_preferences_truncated")}</p> : null}
    </section>
  );
}
