"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): the "add members" picker of the Members tab and the Share dialog
 * (PRD §4.2, §4.3; technical design §3.1 Members).
 *
 * Search is by name, never by ID. People come from the Team directory; a Space knowledge base offers only members
 * of its Space (Q2), read from the Space member list and intersected with the directory. Groups come from the
 * groups directory, and the server decides whether a group applies (C8). Spaces are offered only for Team knowledge
 * bases and stand for that Space's members (C9). Both directory endpoints degrade to empty lists while they are
 * unavailable, so an empty unfiltered directory (the caller is always in it) is reported as unavailable.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { Notice } from "@/components/ui/notice";
import { ApiError, apiJson } from "@/lib/api-client";
import { fetchTeamDirectory, fetchTeamGroups, type DirectoryGroup, type DirectoryUser } from "@/lib/content-share-api";
import { kbFileError } from "@/lib/kb-file-error";
import {
  KNOWLEDGE_BASE_LEVELS,
  type KnowledgeBaseDTO,
  type KnowledgeBaseHome,
  type KnowledgeBaseLevel,
  type KnowledgeBasePrincipalType,
} from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";

type Translator = ReturnType<typeof useTranslations>;

export interface KbPickedPrincipal {
  principal_type: KnowledgeBasePrincipalType;
  principal_id: string;
  /** The name shown in the picker, reused in confirmations and error copy. */
  name: string;
}

/** The key of a principal in exclusion sets (`user:usr_1`, `group:grp_1`, `space:ws_1`). */
export function principalKey(type: KnowledgeBasePrincipalType, id: string): string {
  return `${type}:${id}`;
}

export const KB_MEMBER_INVALID_REASONS = [
  "not_in_space",
  "not_in_team",
  "owner",
  "group_inactive",
  "group_not_applicable",
  "space_entries_team_only",
  "space_not_found",
] as const;

export type KbMemberInvalidReason = (typeof KB_MEMBER_INVALID_REASONS)[number];

/** The `reason` of a 422 `knowledge_base_member_invalid` (`detail.error.reason`); null for any other failure. */
export function memberInvalidReason(error: unknown): KbMemberInvalidReason | null {
  if (!(error instanceof ApiError) || error.code !== "knowledge_base_member_invalid") return null;
  const body = error.body as { detail?: { error?: { reason?: unknown } } } | null | undefined;
  const reason = body?.detail?.error?.reason ?? error.details?.reason;
  return KB_MEMBER_INVALID_REASONS.find((known) => known === reason) ?? null;
}

/**
 * Actionable copy for a failed member change: a refused entry names the person, group or Space and what to choose
 * instead; every other failure goes through `kbFileError` (never raw server text).
 * `t` is `knowledge_bases.members`, `tErrors` is `knowledge_base`.
 */
export function memberChangeErrorText(
  error: unknown,
  context: { name: string; home: KnowledgeBaseHome },
  t: Translator,
  tErrors: Translator,
): string {
  const reason = memberInvalidReason(error);
  if (reason === "group_not_applicable") {
    return t(context.home === "space" ? "reason_group_not_applicable_space" : "reason_group_not_applicable_team", { name: context.name });
  }
  if (reason) return t(`reason_${reason}`, { name: context.name });
  const key = kbFileError(error, "scope");
  // The generic scope fallback talks about files; member changes get their own.
  return key === "errors.scope_change_failed" ? t("change_failed_generic") : tErrors(key);
}

/** Level menu used by the picker and by each member row: 可使用 / 可编辑 / 可管理. */
export function KbLevelMenu({
  value,
  onChange,
  label,
  disabled = false,
  testId,
}: {
  value: KnowledgeBaseLevel;
  onChange: (level: KnowledgeBaseLevel) => void;
  /** Accessible name of the control and the menu, e.g. "李华的权限". */
  label: string;
  disabled?: boolean;
  testId?: string;
}) {
  const tCommon = useTranslations("knowledge_bases.common");
  const labelId = useId();
  const options = KNOWLEDGE_BASE_LEVELS.map((level) => ({ value: level, label: tCommon(`level_${level}`) }));
  return (
    <>
      <span id={labelId} className="kbm-visually-hidden">{label}</span>
      <FilterMenuButton<KnowledgeBaseLevel>
        value={value}
        options={options}
        onChange={onChange}
        format={(text) => text}
        menuLabel={label}
        active={false}
        labelledBy={labelId}
        disabled={disabled}
        className="kbm-level"
        testId={testId}
      />
    </>
  );
}

type Candidate =
  | { kind: "space"; id: string; name: string }
  | { kind: "group"; group: DirectoryGroup }
  | { kind: "user"; user: DirectoryUser };

function candidatePrincipal(candidate: Candidate, t: Translator): KbPickedPrincipal {
  if (candidate.kind === "space") return { principal_type: "space", principal_id: candidate.id, name: candidate.name };
  if (candidate.kind === "group") return { principal_type: "group", principal_id: candidate.group.id, name: candidate.group.name };
  return { principal_type: "user", principal_id: candidate.user.id, name: userName(candidate.user, t) };
}

function userName(user: DirectoryUser, t: Translator): string {
  return user.display_name?.trim() || user.email_masked || t("unnamed_user");
}

function matches(text: string, term: string): boolean {
  return !term || text.toLocaleLowerCase().includes(term.toLocaleLowerCase());
}

/** People per page shown before "load more"; a Space knowledge base reads more directory pages to fill it. */
const PEOPLE_TARGET = 8;
const MAX_AUTO_PAGES = 5;
/** Groups and Spaces shown while nothing is typed. */
const IDLE_LIMIT = 5;
const SEARCH_DEBOUNCE_MS = 180;

type SpaceMembers = { status: "idle" | "loading" | "ready" | "failed"; ids: ReadonlySet<string> };

export interface KbMemberPickerProps {
  kb: Pick<KnowledgeBaseDTO, "id" | "home" | "home_id">;
  /** Principals already listed (entries, owner, automatic managers), as `principalKey`s. */
  excluded: ReadonlySet<string>;
  /** A member change is being saved elsewhere in the editor. */
  busy?: boolean;
  /** Saves the entry; rejects with the API error, which the picker maps to actionable copy. */
  onAdd: (principal: KbPickedPrincipal, level: KnowledgeBaseLevel) => Promise<void>;
  /** Id of a visible label for the search box. */
  labelledBy?: string;
  autoFocus?: boolean;
}

export function KbMemberPicker({ kb, excluded, busy = false, onAdd, labelledBy, autoFocus = false }: KbMemberPickerProps) {
  const t = useTranslations("knowledge_bases.members");
  const tErrors = useTranslations("knowledge_base");
  const { items: spaces } = useSpaceSelection();
  const isSpace = kb.home === "space";
  const isTeam = kb.home === "team";

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<KbPickedPrincipal | null>(null);
  const [level, setLevel] = useState<KnowledgeBaseLevel>("use");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [people, setPeople] = useState<DirectoryUser[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [searching, setSearching] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [directoryUnavailable, setDirectoryUnavailable] = useState(false);
  const [groups, setGroups] = useState<DirectoryGroup[]>([]);
  const [spaceMembers, setSpaceMembers] = useState<SpaceMembers>({ status: isSpace ? "loading" : "idle", ids: new Set() });
  const requestRef = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();
  const optionIdPrefix = useId();

  // Groups are a bounded one-shot list; a Space knowledge base also needs its Space's member IDs (Q2).
  useEffect(() => {
    let alive = true;
    fetchTeamGroups()
      .then((result) => {
        if (alive) setGroups(result.items);
      })
      // A failed read leaves no groups to offer; people and Spaces stay searchable.
      .catch(() => undefined);
    if (isSpace) {
      setSpaceMembers({ status: "loading", ids: new Set() });
      apiJson<{ members?: Array<{ user_id: string }> }>(`/api/v1/workspaces/${encodeURIComponent(kb.home_id)}/members`)
        .then((result) => {
          if (alive) setSpaceMembers({ status: "ready", ids: new Set((result.members ?? []).map((member) => member.user_id)) });
        })
        .catch(() => {
          if (alive) setSpaceMembers({ status: "failed", ids: new Set() });
        });
    }
    return () => {
      alive = false;
    };
  }, [isSpace, kb.home_id]);

  const spaceIds = spaceMembers.ids;
  // Where people come from: the directory, nothing (the Space member list failed), or not yet known.
  const peopleSource: "directory" | "none" | "pending" = !isSpace
    ? "directory"
    : spaceMembers.status === "ready"
      ? "directory"
      : spaceMembers.status === "failed"
        ? "none"
        : "pending";

  /** Reads directory pages from `cursor`; a Space knowledge base keeps reading until enough Space members match. */
  const readPeople = useCallback(
    async (term: string, cursor: number) => {
      let items: DirectoryUser[] = [];
      let next: number | null = cursor;
      let raw = 0;
      for (let page = 0; page < MAX_AUTO_PAGES && next != null; page += 1) {
        const result = await fetchTeamDirectory(term || undefined, next);
        raw += result.items.length;
        items = items.concat(isSpace ? result.items.filter((user) => spaceIds.has(user.id)) : result.items);
        next = result.next_cursor;
        if (!isSpace || items.length >= PEOPLE_TARGET) break;
      }
      return { items, next, raw };
    },
    [isSpace, spaceIds],
  );

  // Debounced first page per query; out-of-order answers are dropped.
  useEffect(() => {
    if (peopleSource === "pending") return;
    setNextCursor(null);
    if (peopleSource === "none") {
      setPeople([]);
      setSearching(false);
      return;
    }
    const term = query.trim();
    setSearching(true);
    const timer = setTimeout(async () => {
      const request = ++requestRef.current;
      // The directory answers an empty page when it is unavailable; a network failure reads the same way.
      const result = await readPeople(term, 0).catch(() => ({ items: [] as DirectoryUser[], next: null, raw: 0 }));
      if (request !== requestRef.current) return;
      setPeople(result.items);
      setNextCursor(result.next);
      setSearching(false);
      // The caller is an active Team member, so an empty unfiltered directory means it is unavailable.
      if (!term) setDirectoryUnavailable(result.raw === 0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, readPeople, peopleSource]);

  const loadMore = useCallback(async () => {
    if (nextCursor == null || loadingMore) return;
    setLoadingMore(true);
    const request = requestRef.current;
    try {
      const result = await readPeople(query.trim(), nextCursor);
      if (request !== requestRef.current) return;
      setPeople((previous) => [...previous, ...result.items]);
      setNextCursor(result.next);
    } catch {
      // Keep what is listed; "Load more" stays available to try again.
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, nextCursor, query, readPeople]);

  const term = selected ? "" : query.trim();
  const candidates = useMemo<Candidate[]>(() => {
    const limit = term ? Number.POSITIVE_INFINITY : IDLE_LIMIT;
    const spaceCandidates: Candidate[] = isTeam
      ? spaces
          .filter((space) => !excluded.has(principalKey("space", space.id)) && matches(space.name, term))
          .slice(0, limit)
          .map((space) => ({ kind: "space", id: space.id, name: space.name }))
      : [];
    const groupCandidates: Candidate[] = groups
      .filter((group) => !excluded.has(principalKey("group", group.id)) && matches(group.name, term))
      .slice(0, limit)
      .map((group) => ({ kind: "group", group }));
    const userCandidates: Candidate[] = people
      .filter((user) => !excluded.has(principalKey("user", user.id)))
      .map((user) => ({ kind: "user", user }));
    return [...spaceCandidates, ...groupCandidates, ...userCandidates];
  }, [excluded, groups, isTeam, people, spaces, term]);

  useEffect(() => {
    setActiveIndex((index) => Math.min(index, Math.max(0, candidates.length - 1)));
  }, [candidates.length]);

  const choose = (candidate: Candidate) => {
    const principal = candidatePrincipal(candidate, t);
    setSelected(principal);
    setQuery(principal.name);
    setOpen(false);
    setError(null);
  };

  const add = async () => {
    if (!selected || adding || busy) return;
    setAdding(true);
    setError(null);
    try {
      await onAdd(selected, level);
      setSelected(null);
      setQuery("");
      setLevel("use");
      inputRef.current?.focus();
    } catch (caught) {
      setError(memberChangeErrorText(caught, { name: selected.name, home: kb.home }, t, tErrors));
    } finally {
      setAdding(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (!candidates.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((index) => (index + step + candidates.length) % candidates.length);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (open && candidates[activeIndex]) choose(candidates[activeIndex]);
      else if (selected) void add();
      return;
    }
    if (event.key === "Escape" && open) {
      // Close the results only; the dialog stays open.
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    }
  };

  const placeholder = t(isTeam ? "picker_placeholder_team" : "picker_placeholder_space");
  const showResults = open && !selected;
  const activeId = showResults && candidates[activeIndex] ? `${optionIdPrefix}-${activeIndex}` : undefined;
  const peopleUnavailable = peopleSource === "none";
  const searchPending = searching || peopleSource === "pending";
  // With nothing typed and no directory, the unavailable notice below explains the empty list.
  const emptyText =
    !candidates.length && !searchPending && !(directoryUnavailable && !term)
      ? t(isSpace ? "picker_no_results_space" : "picker_no_results_team")
      : null;

  const groupLabel = (kind: Candidate["kind"]) =>
    kind === "space" ? t("picker_group_spaces") : kind === "group" ? t("picker_group_groups") : t("picker_group_people");

  return (
    <div
      className="kbm-picker"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <div className="kbm-picker-row">
        <div className="kbm-picker-search">
          <input
            ref={inputRef}
            type="search"
            className="ui-input kbm-picker-input"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={showResults}
            aria-controls={listboxId}
            aria-activedescendant={activeId}
            aria-labelledby={labelledBy}
            aria-label={labelledBy ? undefined : placeholder}
            placeholder={placeholder}
            value={query}
            autoFocus={autoFocus}
            disabled={adding}
            onFocus={() => setOpen(true)}
            onChange={(event) => {
              setQuery(event.target.value);
              setSelected(null);
              setOpen(true);
              setActiveIndex(0);
              setError(null);
            }}
            onKeyDown={onKeyDown}
          />
          {showResults ? (
            <div id={listboxId} role="listbox" aria-label={t("picker_results_label")} className="kbm-results">
              {candidates.map((candidate, index) => {
                const principal = candidatePrincipal(candidate, t);
                const previous = candidates[index - 1];
                const header = !previous || previous.kind !== candidate.kind;
                const meta =
                  candidate.kind === "space"
                    ? t("picker_space_meta")
                    : candidate.kind === "group"
                      ? t("picker_group_members", { count: candidate.group.member_count })
                      : candidate.user.email_masked;
                return (
                  <div key={principalKey(principal.principal_type, principal.principal_id)} role="presentation">
                    {header ? (
                      <div role="presentation" className="kbm-results-group">
                        {groupLabel(candidate.kind)}
                      </div>
                    ) : null}
                    <div
                      id={`${optionIdPrefix}-${index}`}
                      role="option"
                      aria-selected={index === activeIndex}
                      className={`kbm-result${index === activeIndex ? " is-active" : ""}`}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => choose(candidate)}
                    >
                      <span className="kbm-result-name">{principal.name}</span>
                      {meta ? <span className="kbm-result-meta">{meta}</span> : null}
                    </div>
                  </div>
                );
              })}
              {searchPending && !candidates.length ? (
                <div role="presentation" className="kbm-results-note">
                  {t("picker_searching")}
                </div>
              ) : null}
              {emptyText ? (
                <div role="presentation" className="kbm-results-note">
                  {emptyText}
                </div>
              ) : null}
              {nextCursor != null && !peopleUnavailable ? (
                <div role="presentation" className="kbm-results-more">
                  <Button
                    variant="plain"
                    size="sm"
                    loading={loadingMore}
                    disabled={loadingMore}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => void loadMore()}
                  >
                    {loadingMore ? t("picker_loading_more") : t("picker_load_more")}
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
        <KbLevelMenu value={level} onChange={setLevel} label={t("picker_level_menu")} disabled={adding} testId="kb-member-picker-level" />
        <Button variant="secondary" onClick={() => void add()} disabled={!selected || adding || busy} loading={adding}>
          {adding ? t("picker_adding") : t("picker_add")}
        </Button>
      </div>
      <p className="kbm-picker-hint">{t(isSpace ? "picker_hint_space" : "picker_hint_team")}</p>
      {directoryUnavailable ? <Notice tone="muted">{t("picker_directory_unavailable")}</Notice> : null}
      {peopleUnavailable ? <Notice tone="muted">{t("picker_space_members_unavailable")}</Notice> : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
    </div>
  );
}
