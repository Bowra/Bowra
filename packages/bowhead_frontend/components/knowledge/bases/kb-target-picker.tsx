"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): the target picker of "Add to" (upload, Add from Files) and "Move to another
 * knowledge base" (PRD §4.6, §4.7; mockups ⑦ and ⑧).
 *
 * One radio group, grouped My / the current Space / Team, each option with its name, the Default badge and its audience.
 * - `purpose="add"`: every knowledge base in the directory; those you cannot add to (`!can.add_files`) stay visible,
 *   disabled, with the reason and whom to ask.
 * - `purpose="move"`: only knowledge bases you can edit ("only those you can edit are listed"); the one the files are in
 *   now (`excludeId`) stays visible, marked as current and not selectable.
 *
 * Keyboard: the shared RadioGroup (arrows move and select, wrap and skip disabled options, Home/End, one Tab stop).
 */
import { useId } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import type { KbScope } from "@/lib/kb-api";
import { audienceLabelKey, homeLabel } from "@/lib/knowledge-base-display";
import {
  KNOWLEDGE_BASE_HOMES,
  directoryKnowledgeBases,
  type KnowledgeBaseDirectory,
  type KnowledgeBaseDTO,
  type KnowledgeBaseHome,
} from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";

import { useKnowledgeBaseNamer } from "./use-knowledge-bases";

export interface KnowledgeBaseTargetPickerProps {
  directory: KnowledgeBaseDirectory | null;
  /** First directory load in flight: a skeleton instead of the options. */
  loading?: boolean;
  value: string | null;
  onChange: (id: string) => void;
  purpose: "add" | "move";
  /** Move: the knowledge base the files are in now (shown as current, not selectable). */
  excludeId?: string | null;
  /** ID of the visible label of the group. */
  labelledBy?: string;
}

type Translator = (key: string, values?: Record<string, string | number>) => string;

/** Whether files can be added to (or moved into) this knowledge base. */
export function canAddToKnowledgeBase(kb: Pick<KnowledgeBaseDTO, "can"> | null | undefined): boolean {
  return Boolean(kb?.can.add_files);
}

/** The options of one home, in directory order. */
export function knowledgeBaseTargetOptions(
  directory: KnowledgeBaseDirectory | null | undefined,
  home: KnowledgeBaseHome,
  purpose: "add" | "move",
  excludeId?: string | null,
): KnowledgeBaseDTO[] {
  const all = directory?.sections[home] ?? [];
  return purpose === "add" ? all : all.filter((kb) => canAddToKnowledgeBase(kb) || kb.id === excludeId);
}

/**
 * The default "Add to" target (PRD §4.7): the preferred knowledge base (the one you are in, or `?add_to_kb=`) when you can
 * add to it, else the current Space's default when you can add to it, else My knowledge, else the first one you can add to.
 */
export function defaultKnowledgeBaseTarget(
  directory: KnowledgeBaseDirectory | null | undefined,
  preferredId?: string | null,
): string | null {
  const all = directoryKnowledgeBases(directory);
  const editable = (kb: KnowledgeBaseDTO | undefined): kb is KnowledgeBaseDTO => Boolean(kb && canAddToKnowledgeBase(kb));
  const preferred = preferredId ? all.find((kb) => kb.id === preferredId) : undefined;
  if (editable(preferred)) return preferred.id;
  const spaceDefault = directory?.sections.space.find((kb) => kb.is_default);
  if (editable(spaceDefault)) return spaceDefault.id;
  const myDefault = directory?.sections.personal.find((kb) => kb.is_default);
  if (editable(myDefault)) return myDefault.id;
  return all.find((kb) => canAddToKnowledgeBase(kb))?.id ?? null;
}

/** The upload scope of a target: its home (My → the user, Space → that Space, Team → the Team) and the home's ID. */
export function knowledgeBaseUploadScope(kb: Pick<KnowledgeBaseDTO, "home" | "home_id">): { scope: KbScope; scope_id: string } {
  return { scope: kb.home, scope_id: kb.home_id };
}

/** Why files cannot be added, and whom to ask: the owner by name, else the home's managers. */
export function cannotAddReason(kb: Pick<KnowledgeBaseDTO, "home" | "is_default" | "owner">, t: Translator): string {
  if (!kb.is_default && kb.owner?.name) return t("reason_use_only_owner", { name: kb.owner.name });
  if (kb.home === "space") return t("reason_use_only_space");
  if (kb.home === "team") return t("reason_use_only_team");
  return t("reason_use_only");
}

export function KnowledgeBaseTargetPicker({
  directory,
  loading = false,
  value,
  onChange,
  purpose,
  excludeId = null,
  labelledBy,
}: KnowledgeBaseTargetPickerProps) {
  const t = useTranslations("knowledge_bases.target");
  const tCommon = useTranslations("knowledge_bases.common");
  const nameOf = useKnowledgeBaseNamer();
  const { items: spaces } = useSpaceSelection();
  const baseId = useId();

  if (!directory) {
    return loading ? <LoadingSkeleton rows={3} label={tCommon("loading")} /> : null;
  }

  const spaceName = directory.space_id ? spaces.find((space) => space.id === directory.space_id)?.name ?? null : null;
  const groups = KNOWLEDGE_BASE_HOMES.map((home) => ({ home, options: knowledgeBaseTargetOptions(directory, home, purpose, excludeId) }))
    .filter((group) => group.options.length > 0);
  const selectable = groups.some((group) => group.options.some((kb) => canAddToKnowledgeBase(kb) && kb.id !== excludeId));

  const empty = selectable ? null : (
    <p className="kb-target-empty" role="status" data-testid="kb-target-empty">
      {t(purpose === "move" ? "empty_move" : "empty_add")}
    </p>
  );
  if (groups.length === 0) return empty;

  return (
    <>
      <RadioGroup<string>
        className="kb-target-picker"
        orientation="both"
        aria-labelledby={labelledBy}
        value={value}
        onValueChange={onChange}
        data-testid="kb-target-picker"
      >
        {groups.map(({ home, options }) => {
          const headingId = `${baseId}-${home}`;
          return (
            <div key={home} role="group" aria-labelledby={headingId} className="kb-target-group">
              <div id={headingId} className="kb-target-group__label">
                {homeLabel(home, tCommon, spaceName)}
              </div>
              {options.map((kb) => {
                const current = kb.id === excludeId;
                const reason = current ? t("reason_current") : canAddToKnowledgeBase(kb) ? null : cannotAddReason(kb, t);
                return (
                  <Radio
                    key={kb.id}
                    value={kb.id}
                    disabled={reason !== null}
                    className="kb-target-option"
                    data-testid={`kb-target-${kb.id}`}
                  >
                    <span className="kb-target-option__dot" aria-hidden="true" />
                    <span className="kb-target-option__text">
                      <span className="kb-target-option__name">
                        <span className="kb-target-option__title">{nameOf(kb)}</span>
                        {kb.is_default ? <Badge tone="outline" size="sm">{tCommon("default_badge")}</Badge> : null}
                        {current ? <Badge tone="accent" size="sm">{t("current_badge")}</Badge> : null}
                      </span>
                      <span className="kb-target-option__meta">{tCommon(audienceLabelKey(kb))}</span>
                      {reason ? <span className="kb-target-option__reason">{reason}</span> : null}
                    </span>
                  </Radio>
                );
              })}
            </div>
          );
        })}
      </RadioGroup>
      {empty}
    </>
  );
}
