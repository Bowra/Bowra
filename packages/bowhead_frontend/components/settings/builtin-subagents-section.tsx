"use client";

/**
 * Built-in subagent rows/configuration dialogs at /team/settings/subagents.
 *
 * Built-in administration sprint (docs/sprints/builtin-subagent-admin/).
 *
 * Sole Agent list on team settings; data only from system manifest/team deltas.
 *
 * Irreducible differences (design boundaries, not pending implementation):
 *   - No deletion: code-defined built-ins can only be disabled/reset.
 *   - No visibility scope: inherently team-wide, no owner/audience.
 *   - Cannot edit name / tools / trust_class / output_schema / callable_peers: I2/I3 invariants and payload
 *     contracts would break from team edits.
 *   - Extra Restore defaults action deletes delta row, returning to code defaults.
 *
 * Shared UI components provide controls and dialogs; scoped styles arrange the list and form.
 */

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { PlanIcon, type PlanIconName } from "@/components/chat/plan-card/icons";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { agentKeyFor, agentMetaFor, useBuiltinAgentLabel, type AgentKey } from "@/components/chat/step-list/agent-meta";
import { toast } from "@/lib/use-toast";
import {
  clearBuiltinSubagentOverride,
  getSkillsCatalog,
  listBuiltinSubagents,
  putBuiltinSubagentOverride,
  type BuiltinOverrideField,
  type BuiltinSubagent,
  type SkillsCatalog,
} from "@/lib/team-subagents-api";

/** Match backend MODEL_ALLOWLIST (builtin_subagent_override.py); out-of-range values return 422. */
const MODEL_OPTIONS = ["analyst", "narrative", "modeler", "echo"] as const;

/**
 * Localized label of a model tier. The tier values are routing identifiers (116 showed raw "analyst / narrative /
 * modeler / echo" in the dialog); an unknown future tier falls back to a neutral "System tier" label, never the code.
 */
function useModelTierLabel(): (tier: string | null | undefined) => string {
  const t = useTranslations("settings.subagents.builtin");
  return useCallback(
    (tier: string | null | undefined) =>
      MODEL_OPTIONS.includes(tier as (typeof MODEL_OPTIONS)[number])
        ? t(`model_tier_${tier}` as never)
        : t("model_tier_other"),
    [t],
  );
}
/** Lead prompt currently assigns these user-facing responsibility roles. */
const PRIMARY_CAPABILITY_OPTIONS = [
  "cap_data_query",
  "cap_analysis",
  "cap_compute",
  "cap_presentation",
  "cap_knowledge_search",
  "cap_document",
  "cap_visualize",
] as const;
/** Match peer_block._MAX_SYSTEM_DESC_CHARS; Lead cannot see excess, so reject on write. */
const MAX_DESCRIPTION = 200;
const MAX_DISPLAY_NAME = 40;
const MAX_PROMPT_APPEND = 4000;
const BUILTIN_SUMMARY_KEYS: Partial<Record<AgentKey, string>> = {
  business: "system_business_analyst_summary",
  compliance: "system_compliance_analyst_summary",
  compute: "system_compute_summary",
  consultant: "system_consultant_summary",
  financial: "system_financial_analyst_summary",
  kb: "system_kb_analyst_summary",
  marketing: "system_marketing_analyst_summary",
  configuration: "system_configuration_agent_summary",
  coach: "system_agent_coach_summary",
};

/** Whether the Agent's manifest lets a Team change `field`; an older response without the list allows every field. */
function allowsOverride(item: BuiltinSubagent, field: BuiltinOverrideField): boolean {
  return !item.team_overrides || item.team_overrides.includes(field);
}

/** Row icon: the Agent's own icon when it has one, otherwise the generic bot (polish spec 2d). */
function builtinIcon(name: string): PlanIconName {
  return agentKeyFor(name) === "generic" ? "bot" : agentMetaFor(name).icon;
}

/** Read-only details at the bottom of the configuration dialog: model and the copyable identifier (slug). */
function BuiltinDetails({ item }: { item: BuiltinSubagent }) {
  const t = useTranslations("settings.subagents.builtin");
  const tierLabel = useModelTierLabel();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(item.name);
      toast({ kind: "success", text: t("identifier_copied") });
    } catch {
      // Clipboard can be unavailable (permissions, insecure origin); the identifier stays selectable.
    }
  };
  return (
    <section className="builtin-agent-config-section builtin-agent-details" aria-labelledby="bsa-details-heading">
      <h4 id="bsa-details-heading">{t("detail_section")}</h4>
      <dl>
        <div>
          <dt>{t("detail_model")}</dt>
          <dd>
            {/* Polish spec 2d: the actual model name, with the tier as secondary text; the tier alone when unknown. */}
            {item.model_display_name?.trim() ? (
              <>
                <span>{item.model_display_name}</span>
                <span className="builtin-agent-detail-secondary">{tierLabel(item.model)}</span>
              </>
            ) : tierLabel(item.model)}
          </dd>
        </div>
        <div>
          <dt>{t("detail_identifier")}</dt>
          <dd>
            <code className="builtin-agent-identifier">{item.name}</code>
            <Button size="sm" variant="ghost" onClick={() => void copy()}>{t("copy_identifier")}</Button>
          </dd>
        </div>
      </dl>
    </section>
  );
}

function builtinName(item: BuiltinSubagent, translate: (key: AgentKey) => string): string {
  const override = item.override?.display_name;
  if (override?.trim()) return override;
  if (item.overridden_fields.includes("display_name")) return item.display_name || item.name;
  const key = agentKeyFor(item.name);
  return key === "generic" ? item.display_name || item.name : translate(key);
}

type DraftState = {
  display_name: string;
  description: string;
  model: string;
  primary_capability: string;
  prompt_append: string;
  skills: string[];
};

function draftFrom(item: BuiltinSubagent): DraftState {
  // ⚠ Read item.override raw delta only, not adjacent effective values; effective description is
  // sanitized, so filling and saving it overwrites admin original (review P1-2).
  // Unoverridden fields remain empty, following default shown as placeholder, never filled
  // or opening configuration freezes current default into override, blocking future manifest upgrades.
  const ov = item.override;
  return {
    display_name: ov?.display_name ?? "",
    description: ov?.description ?? "",
    model: ov?.model ?? "",
    primary_capability: ov?.primary_capability ?? "",
    prompt_append: ov?.prompt_append ?? "",
    skills: ov?.skills ?? [],
  };
}

/** Write current delta back unchanged (PUT full final state, not PATCH), modifying only supplied field.
 *  ⚠ Source must be item.override raw delta, never adjacent effective values whose description is
 *  already sanitized; writing it back freezes sanitized output (review P1-2). */
function bodyFrom(item: BuiltinSubagent, patch: { enabled?: boolean } = {}) {
  const ov = item.override;
  // Fields the manifest doesn't allow are never written back (the route rejects them).
  const allowed = <T,>(field: BuiltinOverrideField, value: T | null | undefined): T | null =>
    allowsOverride(item, field) ? value ?? null : null;
  return {
    enabled: patch.enabled ?? item.enabled,
    display_name: allowed("display_name", ov?.display_name),
    description: allowed("description", ov?.description),
    model: allowed("model", ov?.model),
    primary_capability: allowed("primary_capability", ov?.primary_capability),
    prompt_append: allowed("prompt_append", ov?.prompt_append),
    skills: allowed("skills", ov?.skills),
  };
}

function ConfigDialog({
  item,
  returnFocusRef,
  onClose,
  onSaved,
}: {
  item: BuiltinSubagent;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onSaved: (next: BuiltinSubagent) => void;
}) {
  const t = useTranslations("settings.subagents.builtin");
  const agentLabels = useBuiltinAgentLabel();
  const tierLabel = useModelTierLabel();
  const [draft, setDraft] = useState<DraftState>(() => draftFrom(item));
  const [catalog, setCatalog] = useState<SkillsCatalog | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [catalogRetry, setCatalogRetry] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savingRef = useRef(false); // State disables button next render; same-tick double click bypasses if(busy).
  const allows = (field: BuiltinOverrideField) => allowsOverride(item, field);
  const skillsAllowed = allows("skills");
  const showExecution = allows("primary_capability") || allows("model");
  const showGuidance = skillsAllowed || allows("prompt_append");

  useEffect(() => {
    // Skills are platform policy for some Agents; don't load a picker the dialog won't show.
    if (!skillsAllowed) return;
    let alive = true;
    setCatalog(null);
    setCatalogError(false);
    getSkillsCatalog()
      .then((c) => {
        if (alive) setCatalog(c);
      })
      .catch(() => {
        if (alive) setCatalogError(true);
      });
    return () => {
      alive = false;
    };
  }, [catalogRetry, skillsAllowed]);

  const toggleSkill = (name: string) => {
    setDraft((d) => ({
      ...d,
      skills: d.skills.includes(name) ? d.skills.filter((s) => s !== name) : [...d.skills, name],
    }));
  };

  const save = async () => {
    if (savingRef.current) return;
    savingRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const next = await putBuiltinSubagentOverride(
        item.name,
        {
          enabled: item.enabled,
          display_name: allows("display_name") ? draft.display_name.trim() || null : null,
          description: allows("description") ? draft.description.trim() || null : null,
          model: allows("model") ? draft.model || null : null,
          primary_capability: allows("primary_capability") ? draft.primary_capability || null : null,
          prompt_append: allows("prompt_append") ? draft.prompt_append.trim() || null : null,
          skills: skillsAllowed && draft.skills.length > 0 ? draft.skills : null,
        },
        item.etag,
      );
      onSaved(next);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      savingRef.current = false;
      setBusy(false);
    }
  };

  // During catalog load, selected skills must remain visible/removable; loading must not block edit-time removal.
  const known = new Set([...(catalog?.builtin ?? []), ...(catalog?.team_skills ?? [])].map((s) => s.name));
  const extraSkills = draft.skills.filter((s) => !known.has(s));
  const supportedResponsibilities = PRIMARY_CAPABILITY_OPTIONS.filter(
    (capability) => item.primary_capability_options?.includes(capability) ?? capability === item.defaults.primary_capability,
  );
  const unsupportedResponsibility =
    draft.primary_capability && !supportedResponsibilities.some((value) => value === draft.primary_capability);
  const defaultPrimaryCapability = PRIMARY_CAPABILITY_OPTIONS.find(
    (capability) => capability === item.defaults.primary_capability,
  );
  const defaultPrimaryCapabilityLabel = defaultPrimaryCapability
    ? t(`primary_capability_${defaultPrimaryCapability}` as never)
    : item.defaults.primary_capability
      ? t("primary_capability_other")
      : "—";

  return (
    <Dialog
      title={t("config_title", { name: builtinName(item, agentLabels) })}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      busy={busy}
      width={680}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {t("cancel")}
          </Button>
          <Button variant="primary" onClick={() => void save()} disabled={busy}>
            {t("save")}
          </Button>
        </>
      }
    >
      {error ? <Notice tone="error">{error}</Notice> : null}
      <div className="builtin-agent-config">
        <p className="builtin-agent-config-intro">
          {t("config_intro")}
        </p>

        <section className="builtin-agent-config-section" aria-labelledby="bsa-identity-heading">
          <h4 id="bsa-identity-heading">{t("section_identity")}</h4>
          {allows("display_name") ? (
          <Field id="bsa-display-name" label={t("field_display_name")} hint={t("hint_display_name")}>
            <input
              id="bsa-display-name"
              aria-describedby={fieldDescribedBy("bsa-display-name", { hint: true })}
              value={draft.display_name}
              maxLength={MAX_DISPLAY_NAME}
              placeholder={builtinName(item, agentLabels)}
              onChange={(e) => setDraft((d) => ({ ...d, display_name: e.target.value }))}
              className="ui-input"
            />
          </Field>
          ) : null}

          {allows("description") ? (
          <Field
            id="bsa-description"
            label={t("field_description")}
            hint={t("hint_description", { max: MAX_DESCRIPTION })}
          >
            <textarea
              id="bsa-description"
              aria-describedby={fieldDescribedBy("bsa-description", { hint: true })}
              value={draft.description}
              maxLength={MAX_DESCRIPTION}
              rows={3}
              // The system description is model-facing routing text (mixed languages, internal terms); it stays the
              // default without being shown, so the placeholder only says what an empty field means.
              placeholder={t("placeholder_description")}
              onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
              className="ui-input builtin-agent-textarea"
            />
          </Field>
          ) : null}

        </section>
        {showExecution ? (
        <section className="builtin-agent-config-section" aria-labelledby="bsa-execution-heading">
          <h4 id="bsa-execution-heading">{t("section_execution")}</h4>
          {allows("primary_capability") ? (
          <Field
            id="bsa-primary-capability"
            label={t("field_primary_capability")}
            hint={t("hint_primary_capability")}
          >
            <select
              id="bsa-primary-capability"
              aria-describedby={fieldDescribedBy("bsa-primary-capability", { hint: true })}
              value={draft.primary_capability}
              onChange={(e) => setDraft((d) => ({ ...d, primary_capability: e.target.value }))}
              className="ui-input"
            >
              <option value="">
                {t("primary_capability_default", { capability: defaultPrimaryCapabilityLabel })}
              </option>
              {unsupportedResponsibility && (
                <option value={draft.primary_capability} disabled>
                  {t("primary_capability_unsupported")}
                </option>
              )}
              {supportedResponsibilities.map((capability) => (
                <option key={capability} value={capability}>
                  {t(`primary_capability_${capability}` as never)}
                </option>
              ))}
            </select>
          </Field>
          ) : null}
          <div className="builtin-agent-config-pair">
            {allows("model") ? (
            <Field id="bsa-model" label={t("field_model")} hint={t("hint_model")}>
              <select
                id="bsa-model"
                aria-describedby={fieldDescribedBy("bsa-model", { hint: true })}
                value={draft.model}
                onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
                className="ui-input"
              >
                <option value="">{t("model_default", { model: tierLabel(item.defaults.model) })}</option>
                {MODEL_OPTIONS.map((m) => (
                  <option key={m} value={m}>
                    {tierLabel(m)}
                  </option>
                ))}
              </select>
            </Field>
            ) : null}

          </div>

        </section>
        ) : null}
        {showGuidance ? (
        <section className="builtin-agent-config-section" aria-labelledby="bsa-guidance-heading">
          <h4 id="bsa-guidance-heading">{t("section_guidance")}</h4>
          {skillsAllowed ? (
          <Field id="bsa-skills" label={t("field_skills")} hint={t("hint_skills")}>
            <div
              id="bsa-skills" role="group" aria-label={t("field_skills")} aria-describedby={fieldDescribedBy("bsa-skills", { hint: true })} aria-busy={catalog === null && !catalogError} className="builtin-agent-skills"
            >
              {extraSkills.map((name) => (
                <label key={name} className="builtin-agent-skill">
                  <input type="checkbox" checked onChange={() => toggleSkill(name)} />
                  <code className="bh-mono builtin-agent-skill-name">
                    {name}
                  </code>
                </label>
              ))}
              {catalogError ? (
                <Notice tone="error" className="builtin-agent-skills-error">
                  <p>{t("skills_load_failed")}</p>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => setCatalogRetry(value => value + 1)}>{t("skills_retry")}</Button>
                </Notice>
              ) : catalog === null ? (
                <div className="builtin-agent-skills-loading" role="status">{t("loading")}</div>
              ) : catalog.builtin.length === 0 && catalog.team_skills.length === 0 ? (
                <p className="builtin-agent-skills-loading">{t("skills_empty")}</p>
              ) : (
                [
                  { key: "team", label: t("skills_group_team"), entries: catalog.team_skills },
                  { key: "builtin", label: t("skills_group_builtin"), entries: catalog.builtin },
                ].map((group) =>
                  group.entries.length === 0 ? null : (
                    <div key={group.key}>
                      <div className="builtin-agent-skills-heading">
                        {group.label}
                      </div>
                      {group.entries.map((s) => (
                        <label
                          key={s.name} className="builtin-agent-skill"
                        >
                          <input
                            type="checkbox"
                            checked={draft.skills.includes(s.name)}
                            onChange={() => toggleSkill(s.name)}
                          />
                          <span className="builtin-agent-skill-name">
                            {s.display_name?.trim() || s.name}
                          </span>
                          {/* Team skills carry the description their authors wrote; built-in skill descriptions are
                              model-facing SKILL.md text, so built-ins show only their name. */}
                          {group.key === "team" && s.description ? (
                            <span className="builtin-agent-skill-description">{s.description}</span>
                          ) : null}
                        </label>
                      ))}
                    </div>
                  ),
                )
              )}
            </div>
          </Field>
          ) : null}

          {allows("prompt_append") ? (
          <Field id="bsa-prompt-append" label={t("field_prompt_append")} hint={t("hint_prompt_append")}>
            <textarea
              id="bsa-prompt-append"
              aria-describedby={fieldDescribedBy("bsa-prompt-append", { hint: true })}
              value={draft.prompt_append}
              maxLength={MAX_PROMPT_APPEND}
              rows={4}
              onChange={(e) => setDraft((d) => ({ ...d, prompt_append: e.target.value }))}
              className="ui-input builtin-agent-textarea"
            />
          </Field>
          ) : null}
        </section>
        ) : null}
        <BuiltinDetails item={item} />
      </div>
    </Dialog>
  );
}

/**
 * Read-only details for viewers who cannot manage built-ins: the purpose, model and identifier stay visible to
 * them (polish spec 2d moved these out of the row, it did not change who can read them).
 */
function BuiltinDetailsDialog({
  item,
  summary,
  returnFocusRef,
  onClose,
}: {
  item: BuiltinSubagent;
  summary: string;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const common = useTranslations("common");
  const agentLabels = useBuiltinAgentLabel();
  return (
    <Dialog
      title={builtinName(item, agentLabels)}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      width={520}
      footer={<Button variant="secondary" onClick={onClose}>{common("close")}</Button>}
    >
      <div className="builtin-agent-config">
        <p className="builtin-agent-config-intro">{summary}</p>
        <BuiltinDetails item={item} />
      </div>
    </Dialog>
  );
}

/**
 * Settings row (polish spec 2d): icon · display name · one-line purpose · skill count · ⋯. The slug and model live
 * in the configuration dialog's details; enablement is a badge, and its actions sit in the row menu.
 */
function BuiltinAgentRow({
  item,
  manageable,
  busy,
  onConfigure,
  onToggle,
  onReset,
}: {
  item: BuiltinSubagent;
  manageable: boolean;
  busy: boolean;
  onConfigure: (item: BuiltinSubagent, trigger: HTMLElement) => void;
  onToggle: (item: BuiltinSubagent) => void;
  onReset: (item: BuiltinSubagent) => void;
}) {
  const t = useTranslations("settings.subagents.builtin");
  const agentLabels = useBuiltinAgentLabel();
  const listing = useTranslations("agents_marketplace");
  const changed = item.overridden_fields.length > 0;
  const name = builtinName(item, agentLabels);
  const summaryKey = BUILTIN_SUMMARY_KEYS[agentKeyFor(item.name)];
  const summary = summaryKey ? listing(summaryKey as never) : t("summary_generic");
  const rowRef = useRef<HTMLLIElement>(null);
  const nameRef = useRef<HTMLButtonElement>(null);
  const detailsReturnRef = useRef<HTMLElement | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  // Focus returns to the row's ⋯ after the dialog closes (the menu item itself unmounts with the menu).
  const menuTrigger = () => rowRef.current?.querySelector<HTMLElement>(".ui-row-more") ?? rowRef.current ?? document.body;
  return (
    <li ref={rowRef} data-testid={`builtin-row-${item.name}`} className="builtin-agent-row" data-enabled={item.enabled ? "true" : "false"}>
      <span className="builtin-agent-symbol" aria-hidden="true">
        <PlanIcon name={builtinIcon(item.name)} size={16} />
      </span>
      <div className="builtin-agent-content">
        <div className="builtin-agent-title-row">
          <h3>
            {manageable ? name : (
              // Viewers open read-only details from the name; managers use Configure in the row menu.
              <button
                ref={nameRef}
                type="button"
                className="builtin-agent-name-button"
                onClick={() => {
                  detailsReturnRef.current = nameRef.current;
                  setDetailsOpen(true);
                }}
              >
                {name}
              </button>
            )}
          </h3>
          {item.enabled ? null : <span className="builtin-agent-badge" data-tone="muted">{t("badge_disabled")}</span>}
          {changed ? <span className="builtin-agent-badge">{t("badge_changed")}</span> : null}
        </div>
        <p className="builtin-agent-description">{summary}</p>
      </div>
      {allowsOverride(item, "skills") ? (
        <span className="builtin-agent-skill-count" data-empty={item.skills.length === 0 ? "true" : undefined}>
          {t("row_meta", { skillCount: item.skills.length })}
        </span>
      ) : null}
      <RowActionsMenu
        label={t("more_actions_for", { name })}
        busy={busy}
        actions={[
          ...(manageable
            ? [{ id: "configure", label: t("action_configure"), onSelect: () => onConfigure(item, menuTrigger()) }]
            : []),
          // Read-only identifier and model for everyone who can see the list (not only managers).
          { id: "details", label: t("action_details"), onSelect: () => {
            detailsReturnRef.current = menuTrigger();
            setDetailsOpen(true);
          } },
          ...(manageable
            ? [
                ...(allowsOverride(item, "enabled")
                  ? [
                      item.enabled
                        ? { id: "disable", label: t("action_disable"), onSelect: () => onToggle(item) }
                        : { id: "enable", label: t("action_enable"), onSelect: () => onToggle(item) },
                    ]
                  : []),
                ...(changed ? [{ id: "reset", label: t("action_reset"), onSelect: () => onReset(item) }] : []),
              ]
            : []),
        ]}
      />
      {detailsOpen ? (
        <BuiltinDetailsDialog item={item} summary={summary} returnFocusRef={detailsReturnRef} onClose={() => setDetailsOpen(false)} />
      ) : null}
    </li>
  );
}

/**
 * Built-in Agent list and configuration/confirmation dialogs; page owns heading/count/empty state.
 */
export function BuiltinSubagentCards({
  items,
  manageable,
  onItemChanged,
  onItemRemoved,
}: {
  items: BuiltinSubagent[];
  manageable: boolean;
  onItemChanged: (next: BuiltinSubagent) => void;
  onItemRemoved: (name: string) => void;
}) {
  const t = useTranslations("settings.subagents.builtin");
  const agentLabels = useBuiltinAgentLabel();
  const [configuring, setConfiguring] = useState<BuiltinSubagent | null>(null);
  const configTriggerRef = useRef<HTMLElement | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<BuiltinSubagent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const writeEnabled = useCallback(
    async (item: BuiltinSubagent, enabled: boolean) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      try {
        let next: BuiltinSubagent;
        try {
          next = await putBuiltinSubagentOverride(item.name, bodyFrom(item, { enabled }), item.etag);
        } catch (e) {
          if (!(typeof e === "object" && e !== null && "code" in e && e.code === "builtin_override_version_conflict")) {
            throw e;
          }
          // Enable/disable is idempotent intent. Stale list refetches raw delta and writes once with latest ETag, preventing
          // overwriting another admin's new config without asking user to interpret 409.
          const latest = (await listBuiltinSubagents()).items.find((candidate) => candidate.name === item.name);
          if (!latest) {
            onItemRemoved(item.name);
            return;
          }
          next = latest.enabled === enabled
            ? latest
            : await putBuiltinSubagentOverride(item.name, bodyFrom(latest, { enabled }), latest.etag);
        }
        onItemChanged(next);
        setError(null);
      } catch (e) {
        if (typeof e === "object" && e !== null && "code" in e && e.code === "builtin_subagent_removed") {
          onItemRemoved(item.name);
        }
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        busyRef.current = false;
        setBusy(false);
        setConfirmDisable(null);
      }
    },
    [onItemChanged, onItemRemoved],
  );

  const onToggle = (item: BuiltinSubagent) => {
    if (busyRef.current) return;
    // Disabling built-in with risk warning must show consequences before admin acts.
    if (item.enabled && item.disable_warning) {
      setConfirmDisable(item);
      return;
    }
    void writeEnabled(item, !item.enabled);
  };

  const onReset = async (item: BuiltinSubagent) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      onItemChanged(await clearBuiltinSubagentOverride(item.name));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  if (items.length === 0) return null;

  return (
    <>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <ul className="builtin-agents-list">
        {items.map((item) => (
          <BuiltinAgentRow
            key={item.name}
            item={item}
            manageable={manageable}
            busy={busy}
            onConfigure={(next, trigger) => {
              configTriggerRef.current = trigger;
              setConfiguring(next);
            }}
            onToggle={onToggle}
            onReset={(x) => void onReset(x)}
          />
        ))}
      </ul>

      {configuring ? (
        <ConfigDialog
          key={`${configuring.name}:${configuring.etag}`}
          item={configuring}
          returnFocusRef={configTriggerRef}
          onClose={() => setConfiguring(null)}
          onSaved={onItemChanged}
        />
      ) : null}

      {confirmDisable ? (
        <ConfirmDialog
          title={t("disable_confirm_title", { name: builtinName(confirmDisable, agentLabels) })}
          description={t(`disable_warning_${confirmDisable.disable_warning}` as never)}
          confirmLabel={t("action_disable")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setConfirmDisable(null)}
          onConfirm={() => void writeEnabled(confirmDisable, false)}
        />
      ) : null}
    </>
  );
}
