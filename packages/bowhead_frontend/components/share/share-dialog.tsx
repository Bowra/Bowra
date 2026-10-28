// components/share/share-dialog.tsx — content-sharing overlay (content-permissions wave 1).
// Faithful translation of the Claude Design handoff (sharing/share-dialog.jsx +
// mobile.jsx): desktop modal + mobile bottom sheet, three-audience segmented,
// directory search → roster, →private confirm, recipient read-only view.
// Wired to lib/content-share-api.ts.

"use client";

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";
import { useMediaQuery } from "@/lib/use-media-query";
import { appPath } from "@/lib/base-path";
import { DialogShell } from "@/components/ui/dialog";
import {
  KIND_META,
  type Audience,
  type DirectoryGroup,
  type DirectoryUser,
  type ShareKind,
  type ShareState,
  addShareGrant,
  fetchTeamDirectory,
  fetchTeamGroups,
  getShares,
  putAudience,
  revokeShareGrant,
} from "@/lib/content-share-api";
import {
  ShAvatar, ShBtn, ShGroupAvatar, ShIcon, ShPermTag, ShQuietState, ShSegmented,
  ShTypeTag, T, avatarColor,
} from "./share-ui";
import { CharIcon } from "@/components/char-icon";

export interface ShareDialogProps {
  kind: ShareKind;
  resourceId: string;
  label?: string;
  onClose: () => void;
  /** fired after any mutation so the host list can refresh audience badges */
  onChanged?: () => void;
  /** skill-team-governance: team_skill broadcast audience locked; visibility changes require approval state machine
   * (server set_audience always returns 422 for rows other than team_wide+approved); caller derives prop from row state. */
  teamWideLocked?: boolean;
  /** team_skill: the viewer may not narrow an approved team-wide Skill (only team owners/admins can; the server rejects it). */
  downgradeLocked?: boolean;
  /** Return focus here on close (Safari clicking button never focuses it, so opening focus misses trigger). Omitted uses opening focus. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}

type Suggestion =
  | { kind: "user"; user: DirectoryUser }
  | { kind: "group"; group: DirectoryGroup };

/** translator handle for the `share` namespace, threaded into child fn-components */
type Tr = ReturnType<typeof useTranslations>;

/** locale-resolved per-kind display strings (KIND_META now carries only icon + perm flavor) */
interface LocalizedMeta {
  typeLabel: string;
  verb: string;
  perm: string;
  icon: "doc" | "bot" | "spark";
}
function localizedMeta(kind: ShareKind, t: Tr): LocalizedMeta {
  const km = KIND_META[kind];
  return {
    typeLabel: t(`kind_${kind}_type`),
    verb: t(`kind_${kind}_verb`),
    perm: km.perm === "view" ? t("perm_view") : t("perm_use"),
    icon: km.icon,
  };
}

export function ShareDialog(props: ShareDialogProps) {
  const t = useTranslations("share");
  const isMobile = useMediaQuery("(max-width: 760px)");
  const meta = localizedMeta(props.kind, t);

  const [state, setState] = useState<ShareState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  // User-intended audience view overrides server-derived value. Empty custom roster derives private server-side, but clicking
  // “指定人·组” must show picker to add first person → view holds custom (otherwise picker unreachable).
  const [view, setView] = useState<Audience | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await getShares(props.kind, props.resourceId));
      setErr(null);
    } catch (e) {
      setErr(t("load_failed_safe"));
    }
  }, [props.kind, props.resourceId, t]);

  useEffect(() => { void load(); }, [load]);

  const fireChanged = props.onChanged;
  const mutate = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); await load(); fireChanged?.(); }
    catch (e) { setErr(t("change_failed_safe")); }
    finally { setBusy(false); }
  }, [load, fireChanged, t]);

  // team_skill broadcast lock derives live from **current server value** (review P3: static prop becomes stale after in-dialog downgrade;
  // server allows only rows already team_wide+approved to re-pick team_wide). Once current audience differs from
  // team_wide, rebroadcast requires approval state machine. Before opening state arrives, fall back to caller snapshot.
  const teamWideLocked =
    props.kind === "team_skill" ? (state ? state.audience !== "team_wide" : !!props.teamWideLocked) : false;
  // Narrowing an approved team-wide Skill is an admin action; lock the narrower audiences while it is team-wide.
  const downgradeLocked = props.kind === "team_skill" && !!props.downgradeLocked && state?.audience === "team_wide";

  const pickAudience = useCallback((target: Audience) => {
    if (!state) return;
    if (target === "team_wide" && teamWideLocked) {
      setErr(t("team_wide_locked_hint"));  // Double guard: segmented already disabled, keyboard/extreme-state fallback.
      return;
    }
    if (target !== "team_wide" && downgradeLocked) {
      setErr(t("team_wide_downgrade_locked_hint"));
      return;
    }
    if (target === "custom") {
      // Hold custom view (show picker) even when empty roster derives private server-side. Switching from team_wide requires
      // one PUT to revoke team grant and downgrade; already custom/private needs no repeated PUT.
      setView("custom");
      if (state.audience !== "custom") {
        void mutate(() => putAudience(props.kind, props.resourceId, "custom"));
      }
      return;
    }
    setView(null);  // private / team_wide follow server-derived value.
    if (target === state.audience) return;
    if (target === "private" && hasRoster(state)) { setConfirmRevoke(true); return; }
    void mutate(() => putAudience(props.kind, props.resourceId, target));
  }, [state, mutate, props.kind, props.resourceId, teamWideLocked, downgradeLocked, t]);

  // Effective rendered audience = user-intent override ?? server-derived value.
  const effectiveAudience: Audience = view ?? state?.audience ?? "private";
  const title = props.label || state?.label || meta.typeLabel;
  const body = (
    <DialogBody
      t={t} meta={meta} state={state} audience={effectiveAudience} err={err} busy={busy} mobile={isMobile}
      teamWideLocked={teamWideLocked}
      downgradeLocked={downgradeLocked}
      onAddGrant={(p) => mutate(() => addShareGrant(props.kind, props.resourceId, p))}
      onRevokeGrant={(gid) => mutate(() => revokeShareGrant(props.kind, props.resourceId, gid))}
      onPickAudience={pickAudience}
    />
  );

  const confirm = confirmRevoke ? (
    <RevokeConfirm
      t={t} state={state} mobile={isMobile}
      onCancel={() => setConfirmRevoke(false)}
      onConfirm={() => { setView(null); setConfirmRevoke(false); void mutate(() => putAudience(props.kind, props.resourceId, "private")); }}
    />
  ) : null;

  if (isMobile) {
    return (
      // Revocation confirmation is nested DialogShell (Esc closes it only); panel unpositioned, absolute position remains relative to backdrop,
      // same location as when directly beneath backdrop.
      <DialogShell
        presentation="center"
        onClose={props.onClose}
        mount="inline"
        returnFocusRef={props.returnFocusRef}
        label={t("aria_share", { title })}
        backdropClassName="legacy-share-backdrop legacy-share-backdrop--end"
        panelClassName="legacy-share-sheet"
      >
        <div className="legacy-share-grabber" />
        <SheetHeader t={t} title={title} typeLabel={meta.typeLabel} icon={meta.icon} />
        {body}
        {confirm}
      </DialogShell>
    );
  }

  return (
    <DialogShell
      presentation="center"
      onClose={props.onClose}
      mount="inline"
      returnFocusRef={props.returnFocusRef}
      label={t("aria_share", { title })}
      backdropClassName="legacy-share-backdrop legacy-share-backdrop--center"
      panelClassName="legacy-share-panel"
      panelStyle={{ width: state && !state.can_manage ? 480 : 560 }}
    >
      <DesktopHeader t={t} title={title} typeLabel={meta.typeLabel} icon={meta.icon} onClose={props.onClose} />
      {body}
      {confirm}
    </DialogShell>
  );
}

/* ───────── audience-aware body (shared desktop + mobile) ───────── */
function DialogBody({
  t, meta, state, audience, err, busy, mobile, teamWideLocked, downgradeLocked, onAddGrant, onRevokeGrant, onPickAudience,
}: {
  t: Tr;
  meta: { typeLabel: string; verb: string; perm: string };
  state: ShareState | null; audience: Audience; err: string | null; busy: boolean; mobile: boolean;
  teamWideLocked?: boolean;
  downgradeLocked?: boolean;
  onAddGrant: (p: { principal_type: "user" | "group"; principal_id: string }) => void;
  onRevokeGrant: (grantId: string) => void;
  onPickAudience: (a: Audience) => void;
}) {
  if (err) return <div className={`legacy-share-state-err${mobile ? " is-mobile" : ""}`}>{err}</div>;
  if (!state) return <div className={`legacy-share-state-loading${mobile ? " is-mobile" : ""}`}>{t("loading")}</div>;

  if (!state.can_manage) return <ReadOnlyBody t={t} state={state} mobile={mobile} />;

  const grants = state.grants ?? [];
  const pending = state.pending_requests ?? 0;
  return (
    <div className={`legacy-share-body${mobile ? " is-mobile" : ""}`}>
      <ShSegmented active={audience} onPick={onPickAudience} busy={busy}
        lockedIds={teamWideLocked ? ["team_wide"] : downgradeLocked ? ["custom", "private"] : undefined}
        lockedTitle={
          teamWideLocked ? t("team_wide_locked_hint") : downgradeLocked ? t("team_wide_downgrade_locked_hint") : undefined
        }
        height={mobile ? 42 : 38} fontSize={mobile ? 12.5 : 13} />

      {pending > 0 && (
        <a href={appPath("/settings/access-requests/")} className="legacy-share-pending">
          <ShIcon name="users" size={14} className="legacy-share-flex-none" />
          <span className="legacy-share-grow">{t("pending_requests", { count: pending })}</span>
          <span aria-hidden="true"><CharIcon ch="→" aria-hidden /></span>
        </a>
      )}

      {audience === "private" && (
        <ShQuietState icon="lock" title={t("private_title", { verb: meta.verb, typeLabel: meta.typeLabel })}
          sub={t("private_sub")} />
      )}

      {audience === "custom" && (
        <CustomBody t={t} grants={grants} perm={meta.perm} verb={meta.verb} typeLabel={meta.typeLabel}
          busy={busy} onAddGrant={onAddGrant} onRevokeGrant={onRevokeGrant} />
      )}

      {audience === "team_wide" && (
        <>
          <ShQuietState icon="globe" tint title={t("team_wide_title", { verb: meta.verb })}
            sub={t("team_wide_sub", { typeLabel: meta.typeLabel })} />
          {grants.length > 0 && (
            <div className="legacy-share-roster-kept">
              <ShIcon name="users" size={13} className="legacy-share-roster-kept-icon" />
              <span>{t("roster_kept_note")}</span>
            </div>
          )}
        </>
      )}

      <Footer
        mobile={mobile}
        note={audience === "custom" ? t("footer_notify_note") : undefined}
      />
    </div>
  );
}

function CustomBody({
  t, grants, perm, verb, typeLabel, busy, onAddGrant, onRevokeGrant,
}: {
  t: Tr;
  grants: ShareState["grants"]; perm: string; verb: string; typeLabel: string; busy: boolean;
  onAddGrant: (p: { principal_type: "user" | "group"; principal_id: string }) => void;
  onRevokeGrant: (grantId: string) => void;
}) {
  const rows = grants ?? [];
  return (
    <>
      <div className="legacy-share-search-row">
        <DirectorySearch t={t} busy={busy} existing={new Set(rows.map((g) => g.principal_id))} onPick={onAddGrant} />
      </div>
      {rows.length === 0 ? (
        <div className="legacy-share-roster-empty">
          <ShQuietState icon="users" title={t("empty_roster_title")}
            sub={t("empty_roster_sub", { verb, typeLabel })} />
        </div>
      ) : (
        <div className="legacy-share-roster">
          <div className="legacy-share-roster-head">
            <span className="legacy-share-roster-count">{t("roster_count", { count: rows.length })}</span>
            <span className="legacy-share-grow" />
            <span className="legacy-share-roster-perm">{t("perm_fixed_note", { perm })}</span>
          </div>
          <div className="legacy-share-roster-rows">
            {rows.map((g) => (
              <RosterRow key={g.id} t={t} grant={g} perm={perm} onRevoke={() => onRevokeGrant(g.id)} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function RosterRow({ t, grant, perm, onRevoke }: { t: Tr; grant: NonNullable<ShareState["grants"]>[number]; perm: string; onRevoke: () => void }) {
  const [hover, setHover] = useState(false);
  const isGroup = grant.principal_type === "group";
  // 0708 ⑤: show resolved name; avatar color stays keyed on the id for stability.
  const display = grant.principal_name || grant.principal_id;
  return (
    <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className={`legacy-share-row${hover ? " is-hover" : ""}`}>
        {isGroup ? <ShGroupAvatar size={30} /> : <ShAvatar name={display} color={avatarColor(grant.principal_id)} size={30} />}
        <span className="legacy-share-row-name">{display}</span>
        <ShTypeTag kind={isGroup ? t("tag_group") : t("tag_user")} />
        <span className="legacy-share-grow" />
        <ShPermTag label={perm} />
        <button title={t("remove")} onClick={onRevoke} className={`legacy-share-row-remove${hover ? " is-visible" : ""}`}><ShIcon name="trash" size={11} />{t("remove")}</button>
      </div>
      {isGroup && (
        <div className="legacy-share-row-group-note">
          {t("group_managed_note")}
        </div>
      )}
    </div>
  );
}

/* ───────── directory search + suggestions ───────── */
function DirectorySearch({
  t, busy, existing, onPick,
}: { t: Tr; busy: boolean; existing: Set<string>; onPick: (p: { principal_type: "user" | "group"; principal_id: string }) => void }) {
  const [q, setQ] = useState("");
  const [focus, setFocus] = useState(false);
  // ⑥ (bug 0708): users paginate (infinite scroll), groups are a bounded one-shot.
  // Keep them separate; filter out already-shared principals at render time.
  const [users, setUsers] = useState<DirectoryUser[]>([]);
  const [groups, setGroups] = useState<DirectoryGroup[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Monotonic request id — drop out-of-order responses (a slow page-1 or page-2
  // resolving after the query already changed would otherwise clobber results).
  const reqIdRef = useRef(0);

  // Debounced page-1 fetch on query change (+ groups, refetched each query).
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    // Park pagination while the new query's page 1 is in flight: null cursor makes
    // loadMore a no-op, so a scroll during the debounce window can't fetch the
    // PREVIOUS query's next page at a wrong offset (review nit). page-1 resolution
    // below re-sets the cursor. (Don't clear users here — that would flash empty.)
    setNextCursor(null);
    timer.current = setTimeout(async () => {
      const term = q.trim();
      const myReq = ++reqIdRef.current;
      const [dir, grp] = await Promise.all([fetchTeamDirectory(term || undefined, 0), fetchTeamGroups()]);
      if (myReq !== reqIdRef.current) return; // a newer query superseded this one
      setUsers(dir.items);
      setNextCursor(dir.next_cursor);
      setGroups(grp.items.filter((g) => !term || g.name.includes(term)));
    }, 180);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [q]);

  const loadMore = useCallback(async () => {
    if (nextCursor == null || loadingMore) return;
    setLoadingMore(true);
    const myReq = reqIdRef.current;
    try {
      const dir = await fetchTeamDirectory(q.trim() || undefined, nextCursor);
      if (myReq !== reqIdRef.current) return; // query changed mid-load
      setUsers((prev) => [...prev, ...dir.items]);
      setNextCursor(dir.next_cursor);
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, q]);

  // Merged dropdown list: groups (bounded) first, then users (paginated), both
  // minus already-shared principals. Re-filters when `existing` changes (picking
  // someone) without a refetch.
  const sugg: Suggestion[] = [
    ...groups.filter((g) => !existing.has(g.id)).map((g) => ({ kind: "group", group: g } as Suggestion)),
    ...users.filter((u) => !existing.has(u.id)).map((u) => ({ kind: "user", user: u } as Suggestion)),
  ];

  return (
    // Feishu07.06 #24: dropdown candidates were in-flow placeholders, stretching sharing dialog with more content.
    // position:relative container + absolute dropdown overlay → floats above following content without adding height.
    <div className="legacy-share-search">
      <label className={`legacy-share-search-box${focus ? " is-focus" : ""}`}>
        <ShIcon name="search" size={14} />
        <input value={q} disabled={busy} onChange={(e) => setQ(e.target.value)}
          onFocus={() => setFocus(true)} onBlur={() => setTimeout(() => setFocus(false), 150)}
          placeholder={t("search_placeholder")} className="legacy-share-search-input" />
      </label>
      {focus && sugg.length > 0 && (
        <div
          onScroll={(e) => {
            // ⑥: near the bottom → pull the next page of users.
            const el = e.currentTarget;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) loadMore();
          }} className="legacy-share-suggest">
          {sugg.map((s) => <SuggestRow key={s.kind === "user" ? s.user.id : s.group.id} t={t} s={s} onPick={onPick} />)}
          {loadingMore ? (
            <div className="legacy-share-suggest-more">
              {t("loading_more")}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function SuggestRow({ t, s, onPick }: { t: Tr; s: Suggestion; onPick: (p: { principal_type: "user" | "group"; principal_id: string }) => void }) {
  const [hover, setHover] = useState(false);
  const isUser = s.kind === "user";
  return (
    <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onMouseDown={(e) => { e.preventDefault(); onPick(isUser ? { principal_type: "user", principal_id: s.user.id } : { principal_type: "group", principal_id: s.group.id }); }}
      className={`legacy-share-suggest-row${hover ? " is-hover" : ""}`}>
      {isUser ? <ShAvatar name={s.user.display_name} color={avatarColor(s.user.id)} size={26} /> : <ShGroupAvatar size={26} />}
      <span className="legacy-share-suggest-name">{isUser ? s.user.display_name : s.group.name}</span>
      <span className="legacy-share-suggest-meta">{isUser ? s.user.email_masked : t("member_count", { count: s.group.member_count })}</span>
      {!isUser && <ShTypeTag kind={t("tag_group")} />}
      <span className="legacy-share-grow" />
      {hover && <span className="legacy-share-suggest-add"><ShIcon name="plus" size={11} />{t("add")}</span>}
    </div>
  );
}

/* ───────── recipient read-only ───────── */
function ReadOnlyBody({ t, state, mobile }: { t: Tr; state: ShareState; mobile: boolean }) {
  const tMember = useTranslations("agents_marketplace");
  // Never show the raw owner id; an unresolved owner reads as a generic "Team member".
  const ownerName = state.owner_name?.trim();
  const owner = ownerName && ownerName !== state.owner_id ? ownerName : tMember("team_member");
  const aud = state.audience === "team_wide" ? t("audience_team_wide") : state.audience === "custom" ? t("audience_custom") : t("audience_private");
  const my = state.my_grant;
  const permLabel = my?.permission === "read" ? t("perm_view") : t("perm_use");
  return (
    <div className={`legacy-share-ro-body${mobile ? " is-mobile" : ""}`}>
      <div className="legacy-share-ro-audience">
        <ShIcon name="users" size={14} className="legacy-share-ro-audience-icon" />
        <span className="legacy-share-ro-audience-label">{t("ro_audience_level")}</span>
        <span className="legacy-share-grow" />
        <span className="legacy-share-ro-audience-value">{aud}</span>
      </div>
      <div className="legacy-share-ro-access">
        <div className="legacy-share-ro-access-label">{t("ro_my_access")}</div>
        <div className="legacy-share-ro-access-row">
          <ShAvatar name={t("ro_you")} color={T.accent.replace("var(--c-accent)", "#b04a2a")} size={30} self />
          <div className="legacy-share-ro-access-main">
            <div className="legacy-share-ro-access-name">{t("ro_you")}</div>
            {state.owner_id && <div className="legacy-share-ro-access-by">{t("ro_shared_by", { owner })}</div>}
          </div>
          <ShPermTag label={permLabel} />
        </div>
      </div>
      <div className="legacy-share-ro-note">
        {t("ro_manager_only")}
      </div>
    </div>
  );
}

/* ───────── confirm revoke (→ “仅自己”) ───────── */
function RevokeConfirm({
  t, state, mobile, onCancel, onConfirm,
}: { t: Tr; state: ShareState | null; mobile: boolean; onCancel: () => void; onConfirm: () => void }) {
  const n = state?.grants?.length ?? 0;
  const card = (
    <div className={mobile ? "legacy-share-revoke is-mobile" : "legacy-share-revoke"}>
      <div className="legacy-share-revoke-title">{t("revoke_all_title")}</div>
      <div className="legacy-share-revoke-body">
        {t("revoke_all_body", { n })}
      </div>
      <div className="legacy-share-revoke-foot">
        <ShBtn kind="ghost" h={mobile ? 42 : 31} fs={mobile ? 13.5 : 12.5} onClick={onCancel} style={mobile ? { flex: 1 } : undefined}>{t("cancel")}</ShBtn>
        <ShBtn kind="danger" h={mobile ? 42 : 31} fs={mobile ? 13.5 : 12.5} onClick={onConfirm} style={mobile ? { flex: 1 } : undefined}>{t("stop_access")}</ShBtn>
      </div>
    </div>
  );
  // Mount locally, backdrop restored to absolute: cover sharing dialog only, same as before.
  return (
    <DialogShell
      presentation="center"
      onClose={onCancel}
      mount="inline"
      label={t("revoke_all_title")}
      // FloatingOverlay supplies inline position: fixed; override with inline absolute here (cover sharing dialog only).
      backdropStyle={{ position: "absolute" }}
      backdropClassName={mobile ? "legacy-share-revoke-backdrop is-mobile" : "legacy-share-revoke-backdrop"}
      panelClassName={mobile ? "legacy-share-revoke-panel is-mobile" : "legacy-share-revoke-panel"}
    >
      {card}
    </DialogShell>
  );
}

/* ───────── chrome ─────────
 * Legacy shell appearance (backdrop/panel/header/footer/revoke-access confirmation) is legacy-share-* in styles/ui-controls-legacy.css,
 * appearance-refresh batch 5 in styles/ui-refresh-surfaces.css. */

function DesktopHeader({ t, title, typeLabel, icon, onClose }: { t: Tr; title: string; typeLabel: string; icon: "doc" | "bot" | "spark"; onClose: () => void }) {
  return (
    <div className="legacy-share-head">
      <div className="legacy-share-head-main">
        <div className="legacy-share-eyebrow">
          <ShIcon name="share" size={12} />{t("header_eyebrow")}
        </div>
        <div className="legacy-share-head-row">
          <span className="legacy-share-type-mark"><ShIcon name={icon} size={15} /></span>
          <span className="legacy-share-title">{title}</span>
          <ShTypeTag kind={typeLabel} />
        </div>
      </div>
      <button aria-label={t("aria_close")} onClick={onClose} className="legacy-share-close"><ShIcon name="x" size={14} /></button>
    </div>
  );
}

function SheetHeader({ t, title, typeLabel, icon }: { t: Tr; title: string; typeLabel: string; icon: "doc" | "bot" | "spark" }) {
  return (
    <div className="legacy-share-sheet-head">
      <span className="legacy-share-type-mark"><ShIcon name={icon} size={15} /></span>
      <div className="legacy-share-head-main">
        <div className="legacy-share-sheet-eyebrow">{t("header_eyebrow")}</div>
        <div className="legacy-share-sheet-title">{title}</div>
      </div>
      <ShTypeTag kind={typeLabel} />
    </div>
  );
}

function Footer({ mobile, note }: { mobile: boolean; note?: string }) {
  if (mobile) return null;
  return (
    <div className="legacy-share-foot">
      {note && <span className="legacy-share-foot-note">{note}</span>}
      <span className="legacy-share-grow" />
    </div>
  );
}

function hasRoster(s: ShareState): boolean {
  return s.audience === "custom" && (s.grants?.length ?? 0) > 0;
}
