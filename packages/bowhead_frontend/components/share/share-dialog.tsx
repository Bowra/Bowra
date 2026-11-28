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
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { addFileShare, fileShareUrl, getFileShares, putFileShareAudience, revokeFileShare } from "@/lib/file-share";
import { copyPermissionedReportLink } from "@/lib/report-share";
import { copyShareLink } from "@/lib/share-link";
import { retryResultsCapabilities, useResultsCapabilities } from "@/lib/use-results-capabilities";
import { DialogShell } from "@/components/ui/dialog";
import {
  KIND_META,
  type Audience,
  type DirectoryGroup,
  type DirectoryUser,
  type ShareKind,
  type ShareGrant,
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
import { withHttpFailureReason } from "@/lib/user-facing-error";
import { getChatSendErrorMessage } from "@/lib/chat-send-errors";

export interface ShareDialogProps {
  kind: ShareKind | "file";
  resourceId: string;
  label?: string;
  onClose: () => void;
  /** fired after any mutation so the host list can refresh audience badges */
  onChanged?: () => void;
  onLinkCopied?: () => void;
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

type DialogGrant = Pick<ShareGrant, "id" | "principal_id" | "principal_name"> & {
  principal_type: ShareGrant["principal_type"] | "workspace" | "team";
};

type DialogShareState = Omit<ShareState, "kind" | "grants"> & {
  grants?: DialogGrant[];
  file?: { id: string; sharingEnabled: boolean };
};

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
  const fileT = useTranslations("file_share");
  const isMobile = useMediaQuery("(max-width: 760px)");
  const ownerId = useOptionalAuthSession()?.me?.id;
  const isFile = props.kind === "file";
  const isReport = props.kind === "report";
  const isDocument = isFile || isReport;
  const meta: LocalizedMeta = props.kind === "file"
    ? { typeLabel: fileT("type"), verb: t("kind_report_verb"), perm: t("perm_view"), icon: "doc" }
    : localizedMeta(props.kind, t);
  const [state, setState] = useState<DialogShareState | null>(null);
  const capabilities = useResultsCapabilities();
  const [serverDisabled, setServerDisabled] = useState(false);
  const sharingAllowed = isFile ? state?.file?.sharingEnabled === true && !serverDisabled
    : !isReport || (capabilities.contentSharing === true && !serverDisabled);
  const sharingNotice = sharingAllowed ? undefined : t(
    serverDisabled || (isFile ? state?.file?.sharingEnabled === false : capabilities.sharingDisabled)
      ? "deployment_disabled" : "capability_unavailable",
  );
  useEffect(() => {
    setServerDisabled(false);
    if (props.kind === "report") void retryResultsCapabilities();
  }, [props.kind, props.resourceId]);

  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [individualRevoke, setIndividualRevoke] = useState<DialogGrant | null>(null);
  const [copying, setCopying] = useState(false);
  // User-intended audience view overrides server-derived value. Empty custom roster derives private server-side, but clicking
  // “指定人·组” must show picker to add first person → view holds custom (otherwise picker unreachable).
  const [view, setView] = useState<Audience | null>(null);

  const load = useCallback(async () => {
    try {
      if (props.kind === "file") {
        const data = await getFileShares(props.resourceId);
        setState({
          resource_id: props.resourceId,
          label: "",
          owner_id: ownerId ?? null,
          audience: data.audience ?? (data.shares.length ? "custom" : "private"),
          can_manage: true,
          file: { id: data.file_id, sharingEnabled: data.sharing_enabled },
          grants: data.shares.map((grant) => ({
            id: `${grant.subject_kind}:${grant.subject_id}`,
            principal_type: grant.subject_kind,
            principal_id: grant.subject_id,
            principal_name: grant.subject_name,
          })),
        });
      } else {
        setState(await getShares(props.kind, props.resourceId));
      }
      setErr(null);
    } catch (e) {
      setState(null);
      setErr(withHttpFailureReason(t("load_failed_safe"), e, { read: true }));
    }
  }, [props.kind, props.resourceId, ownerId, t]);

  useEffect(() => { void load(); }, [load]);

  const fireChanged = props.onChanged;
  const mutate = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); await load(); fireChanged?.(); }
    catch (e) {
      if (e instanceof ApiError && e.code === "content_sharing_disabled") {
        setServerDisabled(true);
        setErr(null);
      } else if (e instanceof ApiError && e.code === "file_share_invalid_recipient") {
        setErr(fileT("invalid_recipient"));
      } else if (e instanceof ApiError && e.code === "agent_trial_file_share_unavailable") {
        // A file made in an Owner draft trial stays the Owner's own (decision TD3).
        setErr(getChatSendErrorMessage(e.code));
      } else setErr(withHttpFailureReason(t("change_failed_safe"), e));
    }
    finally { setBusy(false); }
  }, [load, fireChanged, fileT, t]);

  const updateAudience = useCallback((target: Audience) => {
    const { kind, resourceId } = props;
    return mutate(() => kind === "file" ? putFileShareAudience(resourceId, target) : putAudience(kind, resourceId, target));
  }, [mutate, props.kind, props.resourceId]);

  // team_skill broadcast lock derives live from **current server value** (review P3: static prop becomes stale after in-dialog downgrade;
  // server allows only rows already team_wide+approved to re-pick team_wide). Once current audience differs from
  // team_wide, rebroadcast requires approval state machine. Before opening state arrives, fall back to caller snapshot.
  const teamWideLocked =
    props.kind === "team_skill" ? (state ? state.audience !== "team_wide" : !!props.teamWideLocked) : false;
  // Narrowing an approved team-wide Skill is an admin action; lock the narrower audiences while it is team-wide.
  const downgradeLocked = props.kind === "team_skill" && !!props.downgradeLocked && state?.audience === "team_wide";
  const pickAudience = useCallback((target: Audience) => {
    if (!state || (!sharingAllowed && target !== "private")) return;
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
        void updateAudience("custom");
      }
      return;
    }
    setView(null);  // private / team_wide follow server-derived value.
    if (target === state.audience) return;
    if (target === "private" && hasRoster(state)) { setConfirmRevoke(true); return; }
    void updateAudience(target);
  }, [state, updateAudience, teamWideLocked, downgradeLocked, sharingAllowed, t]);

  function revoke(grant: DialogGrant) {
    const { kind, resourceId } = props;
    if (kind === "file") {
      const principal = grant.principal_type;
      if (principal === "user" || principal === "group") void mutate(() => revokeFileShare(resourceId, principal, grant.principal_id));
    } else {
      void mutate(() => revokeShareGrant(kind, resourceId, grant.id));
    }
  }

  async function copyLink() {
    setCopying(true);
    try {
      const copied = isReport ? await copyPermissionedReportLink(props.resourceId)
        : state?.file ? await copyShareLink(fileShareUrl(state.file.id), fileT("copied")) : false;
      if (copied) props.onLinkCopied?.();
    } finally {
      setCopying(false);
    }
  }

  // Effective rendered audience = user-intent override ?? server-derived value.
  const effectiveAudience: Audience = view ?? state?.audience ?? "private";
  const title = props.label || state?.label || meta.typeLabel;
  const body = (
    <DialogBody
      t={t} meta={meta} state={state} audience={effectiveAudience} err={err} busy={busy} mobile={isMobile}
      teamWideLocked={teamWideLocked}
      downgradeLocked={downgradeLocked}
      sharingAllowed={sharingAllowed}
      sharingNotice={sharingNotice}
      fileT={fileT}
      ownerId={ownerId}
      isDocument={isDocument}
      copying={copying}
      onCopyLink={isReport || state?.file ? () => void copyLink() : undefined}
      onAddGrant={(p) => {
        const { kind, resourceId } = props;
        if (sharingAllowed) void mutate(() => kind === "file"
          ? addFileShare(resourceId, p.principal_type, p.principal_id)
          : addShareGrant(kind, resourceId, p));
      }}
      onRevokeGrant={(grant) => {
        if (isDocument) setIndividualRevoke(grant);
        else revoke(grant);
      }}
      onPickAudience={pickAudience}
    />
  );

  const confirm = individualRevoke ? (
    <RevokeConfirm
      t={t} mobile={isMobile}
      title={t("revoke_title")}
      description={t("revoke_body", { name: individualRevoke.principal_name || fileT("member") })}
      individual
      onCancel={() => setIndividualRevoke(null)}
      onConfirm={() => { setIndividualRevoke(null); revoke(individualRevoke); }}
    />
  ) : confirmRevoke ? (
    <RevokeConfirm
      t={t} mobile={isMobile}
      title={t("revoke_all_title")}
      description={t("revoke_all_body", { n: state?.grants?.length ?? 0 })}
      onCancel={() => setConfirmRevoke(false)}
      onConfirm={() => {
        setView(null);
        setConfirmRevoke(false);
        void updateAudience("private");
      }}
    />
  ) : null;

  if (isMobile) {
    return (
      // Revocation confirmation is nested DialogShell (Esc closes it only); panel unpositioned, absolute position remains relative to backdrop,
      // same location as when directly beneath backdrop.
      <DialogShell
        presentation="center"
        onClose={props.onClose}
        mount={isFile ? "body" : "inline"}
        layer={isFile ? "top" : "base"}
        busy={busy}
        returnFocusRef={props.returnFocusRef}
        label={t("aria_share", { title })}
        backdropClassName="legacy-share-backdrop legacy-share-backdrop--end"
        panelClassName="legacy-share-sheet"
      >
        <div className="legacy-share-grabber" />
        <SheetHeader t={t} title={title} typeLabel={meta.typeLabel} icon={meta.icon} onClose={props.onClose} busy={busy} />
        {body}
        {confirm}
      </DialogShell>
    );
  }

  return (
    <DialogShell
      presentation="center"
      onClose={props.onClose}
      mount={isFile ? "body" : "inline"}
      layer={isFile ? "top" : "base"}
      busy={busy}
      returnFocusRef={props.returnFocusRef}
      label={t("aria_share", { title })}
      backdropClassName="legacy-share-backdrop legacy-share-backdrop--center"
      panelClassName="legacy-share-panel"
      panelStyle={{ width: state && !state.can_manage ? 480 : 560 }}
    >
      <DesktopHeader t={t} title={title} typeLabel={meta.typeLabel} icon={meta.icon} onClose={props.onClose} busy={busy} />
      {body}
      {confirm}
    </DialogShell>
  );
}

/* ───────── audience-aware body (shared desktop + mobile) ───────── */
function DialogBody({
  t, meta, state, audience, err, busy, mobile, teamWideLocked, downgradeLocked, sharingAllowed, sharingNotice,
  fileT, ownerId, isDocument, copying, onCopyLink, onAddGrant, onRevokeGrant, onPickAudience,
}: {
  t: Tr;
  meta: { typeLabel: string; verb: string; perm: string };
  state: DialogShareState | null; audience: Audience; err: string | null; busy: boolean; mobile: boolean;
  teamWideLocked?: boolean;
  downgradeLocked?: boolean;
  sharingAllowed: boolean;
  sharingNotice?: string;
  fileT: Tr;
  ownerId?: string;
  isDocument: boolean;
  copying: boolean;
  onCopyLink?: () => void;
  onAddGrant: (p: { principal_type: "user" | "group"; principal_id: string }) => void;
  onRevokeGrant: (grant: DialogGrant) => void;
  onPickAudience: (a: Audience) => void;
}) {
  if (!state && err) return <div role="alert" className={`legacy-share-state-err${mobile ? " is-mobile" : ""}`}>{err}</div>;
  if (!state) return <div className={`legacy-share-state-loading${mobile ? " is-mobile" : ""}`}>{t("loading")}</div>;

  if (!state.can_manage) return <ReadOnlyBody t={t} state={state} mobile={mobile} />;

  const grants = state.grants ?? [];
  const pending = state.pending_requests ?? 0;
  const directSharing = audience === "custom" || (isDocument && audience === "private");
  return (
    <div className={`legacy-share-body${mobile ? " is-mobile" : ""}`}>
      {sharingNotice && <p role="status">{sharingNotice}</p>}
      {err ? <div role="alert" className="legacy-share-state-err">{err}</div> : null}
      <ShSegmented active={audience} onPick={onPickAudience} busy={busy}
        lockedIds={!sharingAllowed ? ["custom", "team_wide"] : teamWideLocked ? ["team_wide"] : downgradeLocked ? ["custom", "private"] : undefined}
        lockedTitle={
          sharingNotice ?? (teamWideLocked ? t("team_wide_locked_hint") : downgradeLocked ? t("team_wide_downgrade_locked_hint") : undefined)
        }
        height={mobile ? 42 : 38} fontSize={mobile ? 12.5 : 13} />
      {pending > 0 && (
        <a href={appPath("/settings/access-requests/")} className="legacy-share-pending">
          <ShIcon name="users" size={14} className="legacy-share-flex-none" />
          <span className="legacy-share-grow">{t("pending_requests", { count: pending })}</span>
          <span aria-hidden="true"><CharIcon ch="→" aria-hidden /></span>
        </a>
      )}

      {audience === "private" && !directSharing && (
        <ShQuietState icon="lock" title={t("private_title", { verb: meta.verb, typeLabel: meta.typeLabel })}
          sub={t("private_sub")} />
      )}

      {directSharing && (
        <CustomBody t={t} grants={grants} perm={meta.perm} verb={meta.verb} typeLabel={meta.typeLabel}
          busy={busy} sharingAllowed={sharingAllowed} onAddGrant={onAddGrant} onRevokeGrant={onRevokeGrant}
          fileT={fileT} ownerId={ownerId}
        />
      )}

      {audience === "team_wide" && (
        <>
          <ShQuietState icon="globe" tint title={t("team_wide_title", { verb: meta.verb })}
            sub={state.file ? fileT("team_wide_sub") : t("team_wide_sub", { typeLabel: meta.typeLabel })} />
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
        note={[
          directSharing && sharingAllowed ? t("footer_notify_note") : null,
          state.file ? fileT("latest_version") : null,
        ].filter(Boolean).join(" ")}
        copyLabel={t("copy_link")}
        onCopyLink={onCopyLink}
        busy={busy || copying}
      />
    </div>
  );
}

function CustomBody({
  t, grants, perm, verb, typeLabel, busy, sharingAllowed, onAddGrant, onRevokeGrant, fileT, ownerId,
}: {
  t: Tr; fileT: Tr;
  grants: DialogGrant[]; perm: string; verb: string; typeLabel: string; busy: boolean;
  ownerId?: string;
  sharingAllowed: boolean;
  onAddGrant: (p: { principal_type: "user" | "group"; principal_id: string }) => void;
  onRevokeGrant: (grant: DialogGrant) => void;
}) {
  const existing = new Set(grants.map((grant) => grant.principal_id));
  if (ownerId) existing.add(ownerId);
  return (
    <>
      {sharingAllowed && <div className="legacy-share-search-row">
        <DirectorySearch t={t} busy={busy} existing={existing} onPick={onAddGrant} />
      </div>}
      {grants.length === 0 ? (
        <div className="legacy-share-roster-empty">
          <ShQuietState icon="users" title={t("empty_roster_title")}
            sub={t("empty_roster_sub", { verb, typeLabel })} />
        </div>
      ) : (
        <div className="legacy-share-roster">
          <div className="legacy-share-roster-head">
            <span className="legacy-share-roster-count">{t("roster_count", { count: grants.length })}</span>
            <span className="legacy-share-grow" />
            <span className="legacy-share-roster-perm">{t("perm_fixed_note", { perm })}</span>
          </div>
          <div className="legacy-share-roster-rows">
            {grants.map((grant) => (
              <RosterRow key={grant.id} t={t} fileT={fileT} grant={grant} perm={perm} busy={busy} onRevoke={() => onRevokeGrant(grant)} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function RosterRow({ t, fileT, grant, perm, busy, onRevoke }: {
  t: Tr; fileT: Tr; grant: DialogGrant; perm: string; busy: boolean; onRevoke: () => void;
}) {
  const [hover, setHover] = useState(false);
  const isGroup = grant.principal_type === "group";
  const scope = grant.principal_type === "workspace" || grant.principal_type === "team" ? grant.principal_type : null;
  // 0708 ⑤: show resolved name; avatar color stays keyed on the id for stability.
  const display = grant.principal_name || (scope ? fileT(scope) : grant.id.startsWith("user:") ? fileT("member") : grant.principal_id);
  return (
    <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className={`legacy-share-row${hover ? " is-hover" : ""}`}>
        {isGroup || scope ? <ShGroupAvatar size={30} /> : <ShAvatar name={display} color={avatarColor(grant.principal_id)} size={30} />}
        <span className="legacy-share-row-name">{display}</span>
        {!scope ? <ShTypeTag kind={isGroup ? t("tag_group") : t("tag_user")} /> : null}
        <span className="legacy-share-grow" />
        <ShPermTag label={perm} />
        {!scope ? <button type="button" disabled={busy} title={t("remove")} onClick={onRevoke} className={`legacy-share-row-remove${hover ? " is-visible" : ""}`}><ShIcon name="trash" size={11} />{t("remove")}</button> : null}
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
}: {
  t: Tr;
  busy: boolean;
  existing: Set<string>;
  onPick: (p: { principal_type: "user" | "group"; principal_id: string }) => void;
}) {
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
    <div className="legacy-share-search" onFocus={() => setFocus(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocus(false);
      }}>
      <label className={`legacy-share-search-box${focus ? " is-focus" : ""}`}>
        <ShIcon name="search" size={14} />
        <input value={q} disabled={busy} onChange={(e) => setQ(e.target.value)}
          aria-label={t("search_placeholder")}
          placeholder={t("search_placeholder")} className="legacy-share-search-input" />
      </label>
      {focus && sugg.length > 0 && (
        <div
          onScroll={(e) => {
            // ⑥: near the bottom → pull the next page of users.
            const el = e.currentTarget;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) loadMore();
          }} className="legacy-share-suggest">
          {sugg.map((s) => <SuggestRow key={s.kind === "user" ? s.user.id : s.group.id} t={t} s={s} busy={busy} onPick={onPick} />)}
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

function SuggestRow({ t, s, busy, onPick }: { t: Tr; s: Suggestion; busy: boolean; onPick: (p: { principal_type: "user" | "group"; principal_id: string }) => void }) {
  const [hover, setHover] = useState(false);
  const isUser = s.kind === "user";
  return (
    <button type="button" disabled={busy} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => onPick(isUser ? { principal_type: "user", principal_id: s.user.id } : { principal_type: "group", principal_id: s.group.id })}
      className={`legacy-share-suggest-row${hover ? " is-hover" : ""}`}>
      {isUser ? <ShAvatar name={s.user.display_name} color={avatarColor(s.user.id)} size={26} /> : <ShGroupAvatar size={26} />}
      <span className="legacy-share-suggest-name">{isUser ? s.user.display_name : s.group.name}</span>
      <span className="legacy-share-suggest-meta">{isUser ? s.user.email_masked : t("member_count", { count: s.group.member_count })}</span>
      {!isUser && <ShTypeTag kind={t("tag_group")} />}
      <span className="legacy-share-grow" />
      {hover && <span className="legacy-share-suggest-add"><ShIcon name="plus" size={11} />{t("add")}</span>}
    </button>
  );
}

/* ───────── recipient read-only ───────── */
function ReadOnlyBody({ t, state, mobile }: { t: Tr; state: DialogShareState; mobile: boolean }) {
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
  t, title, description, individual = false, mobile, onCancel, onConfirm,
}: {
  t: Tr; title: string; description: string; individual?: boolean; mobile: boolean;
  onCancel: () => void; onConfirm: () => void;
}) {
  const card = (
    <div className={mobile ? "legacy-share-revoke is-mobile" : "legacy-share-revoke"}>
      <div className="legacy-share-revoke-title">{title}</div>
      <div className="legacy-share-revoke-body">
        {description}
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
      label={title}
      role={individual ? "alertdialog" : "dialog"}
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

function DesktopHeader({ t, title, typeLabel, icon, onClose, busy }: {
  t: Tr; title: string; typeLabel: string; icon: "doc" | "bot" | "spark"; onClose: () => void; busy: boolean;
}) {
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
      <button type="button" aria-label={t("aria_close")} disabled={busy} onClick={onClose} className="legacy-share-close"><ShIcon name="x" size={14} /></button>
    </div>
  );
}

function SheetHeader({ t, title, typeLabel, icon, onClose, busy }: {
  t: Tr; title: string; typeLabel: string; icon: "doc" | "bot" | "spark"; onClose: () => void; busy: boolean;
}) {
  return (
    <div className="legacy-share-sheet-head">
      <span className="legacy-share-type-mark"><ShIcon name={icon} size={15} /></span>
      <div className="legacy-share-head-main">
        <div className="legacy-share-sheet-eyebrow">{t("header_eyebrow")}</div>
        <div className="legacy-share-sheet-title">{title}</div>
      </div>
      <ShTypeTag kind={typeLabel} />
      <button type="button" aria-label={t("aria_close")} disabled={busy} onClick={onClose} className="legacy-share-close"><ShIcon name="x" size={14} /></button>
    </div>
  );
}

function Footer({ mobile, note, copyLabel, onCopyLink, busy }: {
  mobile: boolean; note?: string; copyLabel: string; onCopyLink?: () => void; busy: boolean;
}) {
  if (mobile && !onCopyLink) return null;
  return (
    <div className={`legacy-share-foot${mobile ? " is-mobile" : ""}`}>
      {note && <span className="legacy-share-foot-note">{note}</span>}
      <span className="legacy-share-grow" />
      {onCopyLink ? <ShBtn kind="ghost" h={mobile ? 42 : 32} onClick={onCopyLink} disabled={busy}>{copyLabel}</ShBtn> : null}
    </div>
  );
}

function hasRoster(s: DialogShareState): boolean {
  return s.audience === "custom" && (s.grants?.length ?? 0) > 0;
}
