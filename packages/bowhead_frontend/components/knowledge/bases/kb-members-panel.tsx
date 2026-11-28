"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): who can use a knowledge base (PRD §4.2 Members, §4.3 Share; technical
 * design §0 C2/C6/C8/C9 and §3.1 Members; mockups ③ ④ ⑬).
 *
 * `KbAccessEditor` is shared by the Members tab (`KbMembersPanel`) and the Share dialog: the audience ("who can use
 * it"), the list (owner, automatic managers, entries) and the summary. Changes apply immediately; removing an entry
 * shows an Undo toast (at least 8 seconds) that re-adds it with its level. Managers of named knowledge bases change
 * the audience; defaults keep a fixed audience (C6) and My knowledge bases are only yours. Without manage rights the
 * same content is read-only.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { Notice } from "@/components/ui/notice";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { TableFrame } from "@/components/ui/table-frame";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { kbFileError } from "@/lib/kb-file-error";
import { audienceLabelKey } from "@/lib/knowledge-base-display";
import {
  getKnowledgeBase,
  getKnowledgeBaseMembers,
  notifyKnowledgeBasesChanged,
  putKnowledgeBaseMembers,
  updateKnowledgeBase,
  type KnowledgeBaseAudience,
  type KnowledgeBaseDTO,
  type KnowledgeBaseLevel,
  type KnowledgeBaseMemberChange,
  type KnowledgeBaseMemberEntry,
  type KnowledgeBaseMembers,
} from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";
import { toast } from "@/lib/use-toast";

import { KbLevelMenu, KbMemberPicker, memberChangeErrorText, principalKey, type KbPickedPrincipal } from "./kb-member-picker";

type Translator = ReturnType<typeof useTranslations>;

/** One line of the list: the owner, an automatic manager or a member entry. */
interface AccessRow {
  /** React key: a person can be both an automatic manager and a listed entry. */
  key: string;
  /** `principalKey`, used to exclude listed principals from the picker. */
  principal: string;
  kind: "owner" | "automatic" | "entry";
  name: string;
  /** State note (an entry that no longer applies) or what a group / Space entry stands for. */
  note: string | null;
  level: KnowledgeBaseLevel;
  source: string;
  entry: KnowledgeBaseMemberEntry | null;
  /** The entry no longer applies (left the Space or Team, group removed, …): it can only be removed. */
  stale: boolean;
}

/** Audience choices of the Share dialog's radio cards. */
type AudienceChoice = "members" | "space_use" | "space_edit" | "team";

function audienceChoice(members: Pick<KnowledgeBaseMembers, "audience" | "audience_level">): AudienceChoice | null {
  if (members.audience === "members") return "members";
  if (members.audience === "team") return "team";
  if (members.audience === "space") return members.audience_level === "edit" ? "space_edit" : "space_use";
  return null;
}

function choicePatch(choice: AudienceChoice): { audience: KnowledgeBaseAudience; audience_level: "use" | "edit" } {
  if (choice === "space_use") return { audience: "space", audience_level: "use" };
  if (choice === "space_edit") return { audience: "space", audience_level: "edit" };
  return { audience: choice, audience_level: "use" };
}

export interface KbAccessEditorProps {
  kb: KnowledgeBaseDTO;
  /** "panel": the Members tab (③); "dialog": the body of the Share dialog (④ ⑬). */
  variant: "panel" | "dialog";
  onChanged?: () => void;
}

export function KbAccessEditor({ kb, variant, onChanged }: KbAccessEditorProps) {
  const t = useTranslations("knowledge_bases.members");
  const tCommon = useTranslations("knowledge_bases.common");
  const tShare = useTranslations("knowledge_bases.share");
  const tErrors = useTranslations("knowledge_base");
  const meId = useOptionalAuthSession()?.me?.id ?? null;
  const { items: spaces } = useSpaceSelection();
  const narrow = useListRowsLayout();
  const spaceName = kb.home === "space" ? (spaces.find((space) => space.id === kb.home_id)?.name ?? null) : null;
  const spaceLabel = spaceName ?? t("this_space");

  const [members, setMembers] = useState<KnowledgeBaseMembers | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(kb.revision);
  const [pickerOpen, setPickerOpen] = useState(false);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const pickerLabelId = useId();

  useEffect(() => {
    setRevision((current) => Math.max(current, kb.revision));
  }, [kb.revision]);

  // Bumped by every write that sets the list, so a read started earlier never overwrites a newer answer.
  const writeSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = writeSeq.current;
    try {
      const next = await getKnowledgeBaseMembers(kb.id);
      if (seq !== writeSeq.current) return;
      setMembers(next);
      setLoadError(null);
    } catch (caught) {
      if (seq !== writeSeq.current) return;
      const key = kbFileError(caught, "load");
      setLoadError(key === "errors.load_failed" ? t("load_failed_generic") : tErrors(key));
    }
  }, [kb.id, t, tErrors]);

  useEffect(() => {
    setMembers(null);
    setLoadError(null);
    void load();
  }, [load]);

  const written = useCallback((next: KnowledgeBaseMembers) => {
    writeSeq.current += 1;
    setMembers(next);
    notifyKnowledgeBasesChanged();
    onChangedRef.current?.();
  }, []);

  const putChanges = useCallback(
    async (changes: KnowledgeBaseMemberChange[]) => {
      const next = await putKnowledgeBaseMembers(kb.id, changes);
      written(next);
      return next;
    },
    [kb.id, written],
  );

  const shareable = kb.home !== "personal";
  // The server's answer decides; the page's own `can` keeps a just-demoted manager from editing.
  const canManage = kb.can.manage && members?.can_manage === true;
  const editable = canManage && shareable;
  const audienceEditable = editable && !kb.is_default;

  const nameOf = useCallback(
    (name: string | null, fallbackKey: string, userId?: string | null) => {
      const shown = name?.trim() || t(fallbackKey);
      return userId && meId && userId === meId ? t("you_suffix", { name: shown }) : shown;
    },
    [meId, t],
  );

  const rows = useMemo<AccessRow[]>(() => {
    if (!members) return [];
    const out: AccessRow[] = [];
    if (members.owner) {
      out.push({
        key: `owner:${members.owner.user_id}`,
        principal: principalKey("user", members.owner.user_id),
        kind: "owner",
        name: nameOf(members.owner.name, "unnamed_user", members.owner.user_id),
        note: null,
        level: "manage",
        source: t("source_owner"),
        entry: null,
        stale: false,
      });
    }
    for (const manager of members.automatic_managers) {
      out.push({
        key: `automatic:${manager.user_id}`,
        principal: principalKey("user", manager.user_id),
        kind: "automatic",
        name: nameOf(manager.name, "unnamed_user", manager.user_id),
        note: null,
        level: "manage",
        source: t(manager.reason === "space_admin" ? "source_space_admin" : "source_team_admin"),
        entry: null,
        stale: false,
      });
    }
    for (const entry of members.entries) {
      const stale = entry.state !== "active";
      const note = stale
        ? t(`state_${entry.state}`)
        : entry.principal_type === "space"
          ? t("space_entry_note")
          : entry.principal_type === "group"
            ? t(kb.home === "space" ? "group_entry_note_space" : "group_entry_note")
            : null;
      out.push({
        key: `entry:${principalKey(entry.principal_type, entry.principal_id)}`,
        principal: principalKey(entry.principal_type, entry.principal_id),
        kind: "entry",
        name: nameOf(entry.label, `unnamed_${entry.principal_type}`, entry.principal_type === "user" ? entry.principal_id : null),
        note,
        level: entry.level,
        source: t(entry.principal_type === "user" ? "source_direct" : entry.principal_type === "group" ? "source_group" : "source_space"),
        entry,
        stale,
      });
    }
    return out;
  }, [kb.home, members, nameOf, t]);

  const excluded = useMemo(() => new Set(rows.map((row) => row.principal)), [rows]);

  const changeLevel = async (row: AccessRow, level: KnowledgeBaseLevel) => {
    if (!row.entry || level === row.entry.level) return;
    setBusy(true);
    setActionError(null);
    try {
      await putChanges([{ principal_type: row.entry.principal_type, principal_id: row.entry.principal_id, level }]);
    } catch (caught) {
      setActionError(memberChangeErrorText(caught, { name: row.name, home: kb.home }, t, tErrors));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: AccessRow) => {
    const entry = row.entry;
    if (!entry) return;
    setBusy(true);
    setActionError(null);
    try {
      await putChanges([{ principal_type: entry.principal_type, principal_id: entry.principal_id, level: null }]);
      toast({
        kind: "success",
        undo: true,
        text: t("removed_toast", { name: row.name }),
        action: {
          label: t("undo"),
          // Undo re-adds the entry with its level; it runs even after the dialog closed.
          onAction: async () => {
            try {
              await putChanges([{ principal_type: entry.principal_type, principal_id: entry.principal_id, level: entry.level }]);
            } catch (caught) {
              toast({
                kind: "error",
                text: t("undo_failed", { name: row.name, error: memberChangeErrorText(caught, { name: row.name, home: kb.home }, t, tErrors) }),
              });
            }
          },
        },
      });
    } catch (caught) {
      setActionError(memberChangeErrorText(caught, { name: row.name, home: kb.home }, t, tErrors));
    } finally {
      setBusy(false);
    }
  };

  const add = async (principal: KbPickedPrincipal, level: KnowledgeBaseLevel) => {
    setBusy(true);
    setActionError(null);
    try {
      await putChanges([{ principal_type: principal.principal_type, principal_id: principal.principal_id, level }]);
      toast({ kind: "success", text: t("added_toast", { name: principal.name, level: tCommon(`level_${level}`) }) });
    } finally {
      setBusy(false);
    }
  };

  const setAudience = async (audience: KnowledgeBaseAudience, audienceLevel: "use" | "edit") => {
    if (!members || (members.audience === audience && members.audience_level === audienceLevel)) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated = await updateKnowledgeBase(kb.id, { revision, audience, audience_level: audienceLevel });
      writeSeq.current += 1;
      setRevision(updated.revision);
      setMembers((current) => (current ? { ...current, audience: updated.audience, audience_level: updated.audience_level } : current));
      notifyKnowledgeBasesChanged();
      onChangedRef.current?.();
      // The audience changes who can use it: refresh the summary.
      void load();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "knowledge_base_revision_conflict") {
        // Someone changed it meanwhile: show the current settings so the change can be made again.
        try {
          const latest = await getKnowledgeBase(kb.id);
          writeSeq.current += 1;
          setRevision(latest.revision);
          setMembers((current) => (current ? { ...current, audience: latest.audience, audience_level: latest.audience_level } : current));
          onChangedRef.current?.();
          setActionError(t("audience_conflict"));
        } catch (reloadFailure) {
          setActionError(tErrors(kbFileError(reloadFailure, "load")));
        }
      } else {
        const key = kbFileError(caught, "scope");
        setActionError(key === "errors.scope_change_failed" ? t("change_failed_generic") : tErrors(key));
      }
    } finally {
      setBusy(false);
    }
  };

  if (!members) {
    if (loadError) {
      return (
        <div className="kbm-state">
          <Notice tone="error">{t("load_failed", { error: loadError })}</Notice>
          <Button variant="secondary" size="sm" onClick={() => void load()}>
            {tCommon("retry")}
          </Button>
        </div>
      );
    }
    return (
      <p className="kbm-state kbm-muted" role="status">
        {t("loading")}
      </p>
    );
  }

  const levelLabel = (level: KnowledgeBaseLevel) => tCommon(`level_${level}`);
  const rowLevel = (row: AccessRow) =>
    row.entry && editable && !row.stale ? (
      <KbLevelMenu
        value={row.entry.level}
        onChange={(level) => void changeLevel(row, level)}
        label={t("level_for", { name: row.name })}
        disabled={busy}
      />
    ) : (
      <span className="kbm-level-fixed">{levelLabel(row.level)}</span>
    );
  const rowSource = (row: AccessRow) =>
    row.kind === "owner" ? (
      <Badge tone="accent" size="sm">
        {row.source}
      </Badge>
    ) : (
      <span className="kbm-source">{row.source}</span>
    );
  const rowActions = (row: AccessRow) =>
    row.entry && editable ? (
      <RowActionsMenu
        label={tCommon("more_actions_for", { name: row.name })}
        pending={busy}
        actions={[{ id: "remove", label: t("remove"), danger: true, confirms: false, disabled: busy, onSelect: () => void remove(row) }]}
      />
    ) : null;

  const list =
    variant === "dialog" || narrow ? (
      <ListRows label={t("list_title")} className="kbm-rows">
        {rows.map((row) => (
          <ListRow
            key={row.key}
            title={<span title={row.name}>{row.name}</span>}
            subtitle={row.note ?? undefined}
            meta={rowSource(row)}
            status={rowLevel(row)}
            actions={rowActions(row) ?? undefined}
            muted={row.stale}
          />
        ))}
      </ListRows>
    ) : (
      <TableFrame className="kbm-table-frame">
        <table className="ui-table kbm-table" aria-label={t("list_title")}>
          <thead>
            <tr>
              <th scope="col">{t("col_member")}</th>
              <th scope="col">{t("col_level")}</th>
              <th scope="col">{t("col_source")}</th>
              <th scope="col" className="ui-table-actions">
                <span className="kbm-visually-hidden">{t("col_actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className={row.stale ? "kbm-row-stale" : undefined}>
                <td>
                  <span className="kbm-cell-main">{row.name}</span>
                  {row.note ? <span className="kbm-cell-sub">{row.note}</span> : null}
                </td>
                <td>{rowLevel(row)}</td>
                <td>{rowSource(row)}</td>
                <td className="ui-table-actions">{rowActions(row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableFrame>
    );

  const pickerToggleLabel = t(kb.home === "team" ? "add_member_or_space" : "add_member");
  // The dialog shows the search inline on wide screens; phones (⑬) and the Members tab (③) open it from a button.
  const pickerInline = variant === "dialog" && !narrow;
  const pickerShown = editable && (pickerInline || pickerOpen);
  const picker = pickerShown ? (
    <div className="kbm-picker-block">
      {pickerInline ? (
        <span id={pickerLabelId} className="kbm-field-label">
          {pickerToggleLabel}
        </span>
      ) : null}
      <KbMemberPicker
        kb={kb}
        excluded={excluded}
        busy={busy}
        onAdd={add}
        labelledBy={pickerInline ? pickerLabelId : undefined}
        autoFocus={!pickerInline}
      />
    </div>
  ) : null;
  const pickerToggle =
    editable && !pickerInline ? (
      <Button variant="ghost" size="sm" aria-expanded={pickerOpen} onClick={() => setPickerOpen((open) => !open)}>
        {pickerOpen ? tCommon("close") : pickerToggleLabel}
      </Button>
    ) : null;

  const readOnlyNote = !editable && shareable ? <Notice tone="muted">{readOnlyText(kb, members, t, tCommon)}</Notice> : null;
  const errorNotice = actionError ? <Notice tone="error">{actionError}</Notice> : null;

  if (variant === "dialog") {
    return (
      <div className="kbm-editor kbm-editor--dialog">
        <AudienceCards
          kb={kb}
          members={members}
          editable={audienceEditable}
          busy={busy}
          spaceLabel={spaceLabel}
          onPick={(choice) => {
            const patch = choicePatch(choice);
            void setAudience(patch.audience, patch.audience_level);
          }}
        />
        {picker}
        {pickerToggle ? <div className="kbm-dialog-add">{pickerToggle}</div> : null}
        {errorNotice}
        {list}
        <p className="kbm-summary">
          {tShare("summary", { people: members.summary.people, edit: members.summary.edit, manage: members.summary.manage })}
        </p>
        {readOnlyNote}
      </div>
    );
  }

  return (
    <div className="kbm-editor kbm-editor--panel">
      <section className="ui-group" aria-labelledby={`${pickerLabelId}-who`}>
        <div className="ui-group-head">
          <h2 className="ui-group-title" id={`${pickerLabelId}-who`}>
            {t("who_title")}
          </h2>
          <p className="ui-group-desc">{t("who_desc")}</p>
        </div>
        <AudienceRows
          kb={kb}
          members={members}
          editable={audienceEditable}
          busy={busy}
          spaceLabel={spaceLabel}
          onChange={(audience, level) => void setAudience(audience, level)}
        />
        <p className="ui-group-footnote">{t("shared_agents_note")}</p>
      </section>
      <section className="ui-group" aria-labelledby={`${pickerLabelId}-list`}>
        <div className="ui-group-head kbm-list-head">
          <div>
            <h2 className="ui-group-title" id={`${pickerLabelId}-list`}>
              {t("list_title")}
            </h2>
            <p className="ui-group-desc">{listDescription(kb, t)}</p>
          </div>
          {pickerToggle}
        </div>
        {picker}
        {errorNotice}
        {list}
        <p className="kbm-summary">
          {t("summary", { people: members.summary.people, edit: members.summary.edit, manage: members.summary.manage })}
        </p>
        {readOnlyNote}
        <div className="ui-group-footnote kbm-levels">
          <p className="kbm-levels-title">{tCommon("levels_explain_title")}</p>
          <ul>
            <li>{tCommon("levels_explain_use")}</li>
            <li>{tCommon("levels_explain_edit")}</li>
            <li>{tCommon("levels_explain_manage")}</li>
          </ul>
        </div>
      </section>
    </div>
  );
}

function listDescription(kb: KnowledgeBaseDTO, t: Translator): string {
  if (kb.home === "personal") return t("list_desc_personal");
  if (kb.home === "team") return t("list_desc_team");
  return t(kb.is_default ? "list_desc_space_default" : "list_desc_space");
}

function readOnlyText(kb: KnowledgeBaseDTO, members: KnowledgeBaseMembers, t: Translator, tCommon: Translator): string {
  const owner = members.owner?.name?.trim();
  if (owner) return t("read_only_owner", { owner });
  if (kb.home === "team") return t("read_only_team");
  if (kb.home === "space") return t("read_only_space");
  return t("read_only_owner", { owner: tCommon("owner_unknown") });
}

/** Where a knowledge base can be used, by home. */
function whereText(kb: KnowledgeBaseDTO, spaceLabel: string, t: Translator): string {
  if (kb.home === "personal") return t("where_personal");
  if (kb.home === "team") return t("where_team");
  return t("where_space", { space: spaceLabel });
}

/** The fixed audience of a default or My knowledge base, and why it is fixed. */
function fixedAudience(kb: KnowledgeBaseDTO, members: KnowledgeBaseMembers, tCommon: Translator, t: Translator) {
  const label = tCommon(audienceLabelKey(members));
  if (kb.home === "personal") return { label, reason: t("personal_reason") };
  if (kb.is_default) return { label, reason: t("default_reason") };
  return { label, reason: null };
}

/** Members tab (③): "visible to", "they can" (all Space members only) and "where it can be used". */
function AudienceRows({
  kb,
  members,
  editable,
  busy,
  spaceLabel,
  onChange,
}: {
  kb: KnowledgeBaseDTO;
  members: KnowledgeBaseMembers;
  editable: boolean;
  busy: boolean;
  spaceLabel: string;
  onChange: (audience: KnowledgeBaseAudience, level: "use" | "edit") => void;
}) {
  const t = useTranslations("knowledge_bases.members");
  const tCommon = useTranslations("knowledge_bases.common");
  const audienceLabelId = useId();
  const levelLabelId = useId();
  const options: Array<{ value: KnowledgeBaseAudience; label: string }> =
    kb.home === "team"
      ? [
          { value: "members", label: t("audience_option_members") },
          { value: "team", label: t("audience_option_team") },
        ]
      : [
          { value: "members", label: t("audience_option_members") },
          { value: "space", label: t("audience_option_space", { space: spaceLabel }) },
        ];
  const fixed = editable ? null : fixedAudience(kb, members, tCommon, t);
  return (
    <div className="ui-group-card">
      <div className="ui-group-row">
        <div className="ui-group-label">
          <span id={audienceLabelId}>{t("audience_label")}</span>
          <span className="ui-group-hint">{fixed?.reason ?? t("audience_hint")}</span>
        </div>
        <div className="ui-group-control">
          {editable ? (
            <FilterMenuButton<KnowledgeBaseAudience>
              value={members.audience}
              options={options}
              onChange={(audience) => onChange(audience, audience === "space" && members.audience === "space" ? members.audience_level : "use")}
              format={(text) => text}
              menuLabel={t("audience_label")}
              active={false}
              labelledBy={audienceLabelId}
              disabled={busy}
              className="kbm-audience-menu"
              testId="kb-members-audience"
            />
          ) : (
            <span className="kbm-static-value">{fixed?.label}</span>
          )}
        </div>
      </div>
      {editable && members.audience === "space" ? (
        <div className="ui-group-row">
          <div className="ui-group-label">
            <span id={levelLabelId}>{t("default_level_label")}</span>
            <span className="ui-group-hint">{t("default_level_hint")}</span>
          </div>
          <div className="ui-group-control">
            <SegmentedControl<"use" | "edit">
              value={members.audience_level}
              onValueChange={(level) => onChange("space", level)}
              activation="manual"
              busy={busy}
              aria-labelledby={levelLabelId}
            >
              <Segment value="use">{t("default_level_use")}</Segment>
              <Segment value="edit">{t("default_level_edit")}</Segment>
            </SegmentedControl>
          </div>
        </div>
      ) : null}
      {editable && members.audience === "team" ? (
        <div className="ui-group-row">
          <div className="ui-group-label">
            {t("default_level_label")}
            <span className="ui-group-hint">{t("team_level_fixed")}</span>
          </div>
          <div className="ui-group-control">
            <span className="kbm-static-value">{t("default_level_use")}</span>
          </div>
        </div>
      ) : null}
      <div className="ui-group-row">
        <div className="ui-group-label">{t("where_label")}</div>
        <div className="ui-group-control kbm-muted">{whereText(kb, spaceLabel, t)}</div>
      </div>
    </div>
  );
}

/** Share dialog (④ ⑬): "who can use it" radio cards, or the fixed audience with its reason. */
function AudienceCards({
  kb,
  members,
  editable,
  busy,
  spaceLabel,
  onPick,
}: {
  kb: KnowledgeBaseDTO;
  members: KnowledgeBaseMembers;
  editable: boolean;
  busy: boolean;
  spaceLabel: string;
  onPick: (choice: AudienceChoice) => void;
}) {
  const t = useTranslations("knowledge_bases.share");
  const tMembers = useTranslations("knowledge_bases.members");
  const tCommon = useTranslations("knowledge_bases.common");
  const labelId = useId();
  if (!editable) {
    const fixed = fixedAudience(kb, members, tCommon, tMembers);
    return (
      <div className="kbm-field">
        <span className="kbm-field-label">{t("who_label")}</span>
        <div className="kbm-audience-static">
          <span className="kbm-static-value">{fixed.label}</span>
          {fixed.reason ? <span className="kbm-muted">{fixed.reason}</span> : null}
        </div>
      </div>
    );
  }
  const choices: AudienceChoice[] = kb.home === "team" ? ["members", "team"] : ["members", "space_use", "space_edit"];
  return (
    <div className="kbm-field">
      <span className="kbm-field-label" id={labelId}>
        {t("who_label")}
      </span>
      <RadioGroup<AudienceChoice>
        value={audienceChoice(members)}
        onValueChange={onPick}
        activation="manual"
        orientation="both"
        busy={busy}
        aria-labelledby={labelId}
        className="kbm-audience-cards"
      >
        {choices.map((choice) => (
          <Radio key={choice} value={choice} className="kbm-audience-card">
            <span className="kbm-card-title">{t(`card_${choice}`, { space: spaceLabel })}</span>
            <small className="kbm-card-desc">{t(`card_${choice}_desc`)}</small>
          </Radio>
        ))}
      </RadioGroup>
    </div>
  );
}

/** The Members tab of a knowledge base page. */
export function KbMembersPanel({ kb, onChanged }: { kb: KnowledgeBaseDTO; onChanged?: () => void }) {
  return <KbAccessEditor kb={kb} variant="panel" onChanged={onChanged} />;
}
