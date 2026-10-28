"use client";

/**
 * Shared Skills settings content — complete IA/visual redesign (2026-07-10).
 *
 * Three-level IA (verbatim translation of Claude Design handoff,
 * docs/sprints/skills-page-redesign/design-handoff + design brief §2):
 *   ① “我的 skill”: upload + chat-generated combined (origin downgraded to badge), dual-CTA empty state.
 *   ② “团队技能”: team skills visible to current user (workflow_creator flag off → whole group omitted);
 *                      team governance at /team/settings/workflows, switches here affect current user only.
 *   ③ “内置能力库”: four categories (analysis/documents/design/platform,
 *                      frontmatter category authoritative + slug-prefix fallback), collapsible groups.
 * Enable/disable = per-user switch (update after server confirms; “只影响你自己的会话”); foundational skills
 * (analyst-framework/analyst-sop-output-contract) require confirmation to disable.
 * Removed pagination/per-page bulk/checkbox/mono slug as main title.
 * Details = right drawer (components/skill-drawer.tsx, replacing centered modal).
 * Style scope .sset (styles/skills-settings.css).
 */

import { useCapabilityManagementDestination } from "@/components/capabilities/capability-management-link";
import {
  PageActionButton,
  PageFrameMobileActions,
  PageHeader,
  PageMobileAction,
} from "@/components/shell/page-frame";
import { Plus as LucidePlus, Upload as LucideUpload } from "lucide-react";
import { appPath } from "@/lib/base-path";
import { shouldHandleMenuLinkSelect } from "@/lib/navigation-click";
import { COMPOSER_PREFILL_KEY } from "@/lib/composer-handoff";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import {
  deleteSkill,
  fetchSkills,
  patchSkill,
  patchSkillsBulk,
  previewSkillImpact,
  type SkillResourceChangeImpact,
  type SkillListDTO,
  type SkillSummaryDTO,
} from "@/lib/api";
import {
  deleteTeamSkill,
  listTeamSkills,
  patchTeamSkill,
  previewTeamSkillImpact,
  type TeamSkillPublic,
} from "@/lib/workflow-solidify-api";
import { teamSkillOwnerName } from "@/lib/team-skill-owner";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { ApiError } from "@/lib/api-client";
import {
  isWorkflowCreatorDisabled,
  skillWorkflowErrorPresentation,
} from "@/lib/skill-workflow-error";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { useAppRouter } from "@/lib/use-app-router";
import { teamSkillAudience } from "@/lib/team-skill-audience";
import {
  CATEGORY_ORDER,
  DOCUMENT_SKILL_GROUP_ORDER,
  SKELETON_SLUGS,
  documentSkillGroup,
  skillCategory,
  skillDescription,
  skillMatches,
  humanizeSkillSlug,
  skillTitle,
  type SkillCategory,
} from "@/lib/skills-meta";
import { formatByteSize } from "@/lib/format";

/** Personal skill storage allowance shown in the storage line. */
const SKILL_STORAGE_LIMIT_BYTES = 100 * 1024 * 1024;
import { DialogShell } from "@/components/ui/dialog";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Switch } from "@/components/ui/switch";
import { ErrorNotice } from "@/components/error-notice";
import { CapabilityRepairContext } from "@/components/settings/capability-repair-context";
import { SkillCreateModal } from "@/components/skill-create-modal";
import { SkillUploadModal } from "@/components/skill-upload-modal";
import { SkillDrawer, type SkillDrawerTarget } from "@/components/skill-drawer";
import { clearFeedbackPrefill, peekFeedbackPrefill } from "@/lib/feedback-candidates-api";
import { UsageSummaryInline } from "@/components/settings/resource-usage";
import { ShareDialog } from "@/components/share/share-dialog";
import {
  AlertTriangle,
  ChevronDown,
  Info,
  Package,
  Plus,
  Search,
  Sparkles,
  Trash,
  Upload,
  Users,
  X,
} from "@/components/icons";
import { randomUuid } from "@/lib/random-id";
import { CharIcon, CharIconText } from "@/components/char-icon";
import { AccessRequestPanel } from "@/components/access/access-request-panel";
import { accessRequestsEnabledFrom } from "@/lib/use-access-requests-enabled";

type CatFilter = "all" | SkillCategory | "mine";
type GroupKey = "mine" | "shared" | SkillCategory;

type PendingSkillImpact = {
  scope: "personal" | "team_preference" | "team_asset";
  action: "disable" | "delete";
  skill: SkillSummaryDTO | TeamSkillPublic;
  impact: SkillResourceChangeImpact;
  idempotencyKey: string;
};

function newIdempotencyKey(): string {
  return randomUuid();
}

// skill-team-governance P3: team-skill group restored (removed in terminology batch 2), approved team skills’
// member-discovery entry + per-user enable/disable; team governance (approval/delete/team switch) stays /team/settings/workflows.
const GROUP_ORDER: GroupKey[] = ["mine", "shared", ...CATEGORY_ORDER];

function teamSkillEnabledForUser(skill: TeamSkillPublic): boolean {
  return (
    (skill.approval || "approved") === "approved" &&
    skill.status === "enabled" &&
    skill.my_enabled !== false
  );
}

/* ─────────────────── row primitive: switch ─────────────────── */

function SkillSwitch({
  on,
  busy,
  scope,
  label,
  tooltip,
  scopeTag,
  buttonRef,
  onToggle,
}: {
  on: boolean;
  busy?: boolean;
  scope?: boolean;
  label: string;
  tooltip: string;
  scopeTag?: string;
  buttonRef?: (node: HTMLButtonElement | null) => void;
  onToggle: () => void;
}) {
  const sw = (
    <Switch
      ref={buttonRef}
      className={`sw${on ? " on" : ""}${busy ? " busy" : ""}${scope ? " scope" : ""}`}
      checked={on}
      aria-label={label}
      title={tooltip}
      busy={busy}
      onClick={(e) => e.stopPropagation()}
      onCheckedChange={() => onToggle()}
    >
      <span className="knob">{scope ? <Users size={9} /> : null}</span>
    </Switch>
  );
  if (!scope) return sw;
  return (
    <span className="sw-wrap">
      {sw}
      <span className="sw-scopetag">
        <Users size={9} /> {scopeTag}
      </span>
    </span>
  );
}

/* ─────────────────── page ─────────────────── */

export function SkillsSettingsContent({
  onNavigateAway,
  initialScope,
}: {
  onNavigateAway?: (href: string) => void;
  initialScope?: "mine" | "all";
} = {}) {
  const t = useTranslations("skills_page");
  const ti = useTranslations("skill_import");
  const te = useTranslations("errors");
  const ownerFallback = useTranslations("agents_marketplace")("team_member");
  const router = useAppRouter();
  const navigateAway = useCallback(
    (href: string) => {
      if (onNavigateAway) {
        onNavigateAway(href);
        return;
      }
      router.push(href);
    },
    [onNavigateAway, router],
  );
  const presentError = useCallback(
    (error: unknown) =>
      skillWorkflowErrorPresentation(error, {
        surface: "settings",
        copy: {
          generic: t("error_generic"),
          transport: {
            requestTimedOut: te("transport.requestTimedOut"),
            serviceUnreachable: te("transport.serviceUnreachable"),
            retry: te("transport.retry"),
          },
        },
      }),
    [t, te],
  );

  const [data, setData] = useState<SkillListDTO | null>(null);
  const [teamSkills, setTeamSkills] = useState<TeamSkillPublic[]>([]);
  const [teamImpactGuard, setTeamImpactGuard] = useState(false);
  const [me, setMe] = useState<MeUser | null>(null);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const [loading, setLoading] = useState(true);
  // Access requests: `/skills/?skill=<id>` for a team skill the viewer cannot see
  // shows the request panel; missing and hidden skills look the same.
  const [teamLoaded, setTeamLoaded] = useState(false);
  const [skillParam, setSkillParam] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [cat, setCat] = useState<CatFilter>(initialScope ?? "all");
  useEffect(() => {
    if (initialScope) setCat(initialScope);
  }, [initialScope]);
  const [onlyEnabled, setOnlyEnabled] = useState(false);
  const [collapsed, setCollapsed] = useState<Partial<Record<GroupKey, boolean>>>(() => ({
    analysis: true,
    documents: true,
    design: true,
    platform: true,
  }));

  const [busyRows, setBusyRows] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState<Partial<Record<GroupKey, "enable" | "disable">>>({});

  const [drawerTarget, setDrawerTarget] = useState<SkillDrawerTarget | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const managementDestination = useCapabilityManagementDestination({
    resource: "skills",
    me,
    origin: cat === "mine" ? "my_skills" : undefined,
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SkillSummaryDTO | null>(null);
  const [teamDeleteTarget, setTeamDeleteTarget] = useState<TeamSkillPublic | null>(null);
  const [skeletonConfirm, setSkeletonConfirm] = useState<SkillSummaryDTO | null>(null);
  const [shareTarget, setShareTarget] = useState<TeamSkillPublic | null>(null);
  const [pendingImpact, setPendingImpact] = useState<PendingSkillImpact | null>(null);
  const [flashName, setFlashName] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rowSwitchRefs = useRef(new Map<string, HTMLButtonElement>());
  const bindRowSwitch = useCallback((key: string, node: HTMLButtonElement | null) => {
    if (node) rowSwitchRefs.current.set(key, node);
    else rowSwitchRefs.current.delete(key);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const r = await fetchSkills();
      setData(r);
      setError(null);
    } catch (e) {
      setError(presentError(e));
    } finally {
      setLoading(false);
    }
  }, [presentError]);

  const refreshTeam = useCallback(async () => {
    // Flag off → route 404 silently empty (group omitted); other failures diagnosable.
    try {
      const r = await listTeamSkills();
      setTeamSkills(r.items);
      setTeamImpactGuard(Boolean(r.impact_guard_enabled));
    } catch (e) {
      setTeamSkills([]);
      setTeamImpactGuard(false);
      if (!isWorkflowCreatorDisabled(e)) {
        setError(presentError(e));
      }
    } finally {
      setTeamLoaded(true);
    }
  }, [presentError]);

  useEffect(() => {
    void refresh();
    void refreshTeam();
    fetchMe()
      .then(setMe)
      .catch((e) => setError(presentError(e)));
  }, [presentError, refresh, refreshTeam]);


  useEffect(
    () => () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    [],
  );

  const openSkillImpact = useCallback(
    async (
      scope: PendingSkillImpact["scope"],
      action: PendingSkillImpact["action"],
      skill: SkillSummaryDTO | TeamSkillPublic,
    ) => {
      const key = scope === "personal"
        ? skill.name
        : "id" in skill && skill.id
          ? String(skill.id)
          : skill.name;
      setBusyRows((current) => new Set(current).add(key));
      try {
        const impact = scope === "team_asset"
          ? await previewTeamSkillImpact(String((skill as TeamSkillPublic).id), {
              action: action === "delete" ? "delete" : "patch",
              proposed_change: action === "disable" ? { status: "disabled" } : {},
            })
          : await previewSkillImpact(skill.name, {
              action: action === "delete" ? "delete" : "patch",
              proposed_change: action === "disable" ? { enabled: false } : {},
              ...(scope === "team_preference" && "id" in skill
                ? { team_skill_id: String(skill.id) }
                : {}),
            });
        if (impact.blocking || !impact.impact_token) {
          setError({
            message: t(
              action === "delete" && impact.reason_codes.includes("resource_delete_has_live_dependencies")
                ? "impact.delete_blocked"
                : "impact.unavailable",
            ),
          });
          return false;
        }
        setPendingImpact({
          scope,
          action,
          skill,
          impact,
          idempotencyKey: newIdempotencyKey(),
        });
        setError(null);
        return true;
      } catch (e) {
        setError(presentError(e));
        return false;
      } finally {
        setBusyRows((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        });
      }
    },
    [presentError, t],
  );

  const confirmSkillImpact = useCallback(async () => {
    const pending = pendingImpact;
    const impactToken = pending?.impact.impact_token;
    if (!pending || !impactToken) return;
    const guard = {
      impactToken,
      idempotencyKey: pending.idempotencyKey,
    };
    const skillId = "id" in pending.skill && pending.skill.id
      ? String(pending.skill.id)
      : "";
    setLoading(true);
    try {
      if (pending.scope === "team_asset") {
        if (pending.action === "delete") await deleteTeamSkill(skillId, guard);
        else await patchTeamSkill(skillId, { status: "disabled" }, guard);
        await refreshTeam();
      } else if (pending.action === "delete") {
        await deleteSkill(skillId, guard);
        setDrawerTarget(null);
        await refresh();
      } else {
        await patchSkill(
          pending.skill.name,
          {
            enabled: false,
            ...(pending.scope === "team_preference"
              ? { team_skill_id: skillId }
              : {}),
          },
          guard,
        );
        if (pending.scope === "team_preference") await refreshTeam();
        else await refresh();
      }
      setPendingImpact(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setPendingImpact(null);
        const refreshed = await openSkillImpact(
          pending.scope,
          pending.action,
          pending.skill,
        );
        if (refreshed) setError({ message: t("impact.changed") });
      } else {
        setError(presentError(e));
      }
    } finally {
      setLoading(false);
    }
  }, [openSkillImpact, pendingImpact, presentError, refresh, refreshTeam, t]);

  /* ── per-user toggles: refresh after server confirms, avoiding briefly misreporting dangerous state ── */
  const applyToggle = useCallback(
    async (name: string, enabled: boolean, focusKey?: string) => {
      const restoreFocus = focusKey
        ? document.activeElement === rowSwitchRefs.current.get(focusKey)
        : false;
      setBusyRows((s) => new Set(s).add(name));
      try {
        await patchSkill(name, { enabled });
        await refresh();
        return true;
      } catch (e) {
        setError(presentError(e));
        return false;
      } finally {
        setBusyRows((s) => {
          const n = new Set(s);
          n.delete(name);
          return n;
        });
        if (restoreFocus && focusKey) {
          window.requestAnimationFrame(() => rowSwitchRefs.current.get(focusKey)?.focus());
        }
      }
    },
    [presentError, refresh],
  );

  const onToggle = useCallback(
    (skill: SkillSummaryDTO, enabled: boolean, focusKey?: string) => {
      if (!enabled && SKELETON_SLUGS.has(skill.name)) {
        setSkeletonConfirm(skill); // Foundational-skill disable guard.
        return;
      }
      if (!enabled && data?.impact_guard_enabled) {
        void openSkillImpact("personal", "disable", skill);
        return;
      }
      void applyToggle(skill.name, enabled, focusKey);
    },
    [applyToggle, data?.impact_guard_enabled, openSkillImpact],
  );

  /* ── group bulk (enable/disable all) ── */
  const runBulk = useCallback(
    async (group: GroupKey, skills: SkillSummaryDTO[], enabled: boolean) => {
      const targets = skills.filter((s) => s.enabled !== enabled).map((s) => s.name);
      if (!targets.length) return;
      if (!enabled && data?.impact_guard_enabled) {
        setError({ message: t("impact.bulk_disable") });
        return;
      }
      setBulkBusy((b) => ({ ...b, [group]: enabled ? "enable" : "disable" }));
      try {
        const result = await patchSkillsBulk(targets.map((name) => ({ name, enabled })));
        await refresh();
        if (result.errors.length) {
          setError({ message: t("bulk_failed", { count: result.errors.length }) });
        }
      } catch (e) {
        await refresh();
        setError(presentError(e));
      } finally {
        setBulkBusy((b) => ({ ...b, [group]: undefined }));
      }
    },
    [data?.impact_guard_enabled, presentError, refresh, t],
  );

  /* ── team skill: per-user enable/disable (skill-team-governance P3, own user only) ──
     Write = team leg of personal PATCH (skill_instances team_shared rows); team-level toggles stay
     /team/settings/workflows (admin governance surface). Refresh after server confirms. */
  const onTeamPerUserToggle = useCallback(
    async (s: TeamSkillPublic, enabled: boolean, focusKey?: string) => {
      if (!enabled && data?.impact_guard_enabled) {
        return openSkillImpact("team_preference", "disable", s);
      }
      const restoreFocus = focusKey
        ? document.activeElement === rowSwitchRefs.current.get(focusKey)
        : false;
      setBusyRows((prev) => new Set(prev).add(s.id));
      try {
        await patchSkill(s.name, { enabled, team_skill_id: s.id });
        await refreshTeam();
        return true;
      } catch (e) {
        setError(presentError(e));
        return false;
      } finally {
        setBusyRows((prev) => {
          const n = new Set(prev);
          n.delete(s.id);
          return n;
        });
        if (restoreFocus && focusKey) {
          window.requestAnimationFrame(() => rowSwitchRefs.current.get(focusKey)?.focus());
        }
      }
    },
    [data?.impact_guard_enabled, openSkillImpact, presentError, refreshTeam],
  );

  const performDelete = useCallback(
    async (skillId: string) => {
      if (data?.impact_guard_enabled && deleteTarget) {
        setDeleteTarget(null);
        await openSkillImpact("personal", "delete", deleteTarget);
        return;
      }
      try {
        await deleteSkill(skillId);
        setDeleteTarget(null);
        setDrawerTarget(null);
        await refresh();
      } catch (e) {
        setDeleteTarget(null);
        setError(presentError(e));
      }
    },
    [data?.impact_guard_enabled, deleteTarget, openSkillImpact, presentError, refresh],
  );

  const performTeamDelete = useCallback(async () => {
    if (!teamDeleteTarget) return;
    if (teamImpactGuard) {
      const target = teamDeleteTarget;
      setTeamDeleteTarget(null);
      await openSkillImpact("team_asset", "delete", target);
      return;
    }
    try {
      await deleteTeamSkill(teamDeleteTarget.id);
      setTeamDeleteTarget(null);
      setDrawerTarget(null);
      await refreshTeam();
    } catch (e) {
      setTeamDeleteTarget(null);
      setError(presentError(e));
    }
  }, [openSkillImpact, presentError, teamDeleteTarget, teamImpactGuard, refreshTeam]);

  /* ── derivation: grouping + filtering ── */
  const searching = query.trim().length > 0;
  const mineAll = useMemo(() => (data ? [...data.user_upload, ...data.user_generated] : []), [data]);
  const builtinByCat = useMemo(() => {
    const out: Record<SkillCategory, SkillSummaryDTO[]> = { analysis: [], documents: [], design: [], platform: [] };
    for (const s of data?.builtin ?? []) out[skillCategory(s)].push(s);
    return out;
  }, [data]);

  const filterRows = useCallback(
    (rows: SkillSummaryDTO[]) => rows.filter((s) => skillMatches(s, query) && (!onlyEnabled || s.enabled)),
    [query, onlyEnabled],
  );
  const mine = useMemo(() => filterRows(mineAll), [filterRows, mineAll]);
  const shared = useMemo(() => {
    const q = query.trim().toLowerCase();
    return teamSkills.filter(
      (s) =>
        (!q ||
          s.name.toLowerCase().includes(q) ||
          (s.display_name || "").toLowerCase().includes(q) ||
          (s.description || "").toLowerCase().includes(q)) &&
        (!onlyEnabled || teamSkillEnabledForUser(s)),
    );
  }, [teamSkills, query, onlyEnabled]);

  const groupVisible = useCallback(
    (g: GroupKey): boolean => {
      if (g === "shared") return cat === "all";
      if (cat === "mine") return g === "mine";
      if (cat !== "all") return g === cat;
      return true;
    },
    [cat],
  );

  const visibleRowTotal =
    (groupVisible("mine") ? mine.length : 0) +
    (groupVisible("shared") ? shared.length : 0) +
    CATEGORY_ORDER.reduce((acc, c) => acc + (groupVisible(c) ? filterRows(builtinByCat[c]).length : 0), 0);

  const totalSize = mineAll.reduce((a, s) => a + (s.size_bytes || 0), 0);
  const canManageTeam = useCallback(
    (s: TeamSkillPublic) => !!me && (s.created_by === me.id || me.is_team_admin),
    [me],
  );
  /* Whether team-skill body editable directly (drawer edit entry). Same backend
     team_skill_content_admin_required rule: submitted/approved broadcast surface editable by admins only;
     author uses “复制为我的 → edit → resubmit”; creator_only skips approval, author edits directly. */
  const canEditTeam = useCallback(
    (s: TeamSkillPublic) => {
      if (!canManageTeam(s)) return false;
      const broadcast = (s.visibility ?? "team_wide") === "team_wide";
      const governed = (s.approval ?? "approved") === "approved" || s.approval === "pending";
      return broadcast && governed ? !!me?.is_team_admin : true;
    },
    [canManageTeam, me],
  );

  // agent-feedback-loop FE batch C: adopted skill suggestion deep-links /skills/?skill=<id>&feedback_prefill=1.
  // Handoff peek/clear lives on this unconditionally mounted page (drawer conditional; see two composer-handoff incidents),
  // wait for team list and me before resolving: peek deletes mismatched target keys, never try with empty list.
  const feedbackDeepLinkRef = useRef(false);
  useEffect(() => {
    if (feedbackDeepLinkRef.current || typeof window === "undefined" || !me) return;
    const params = new URLSearchParams(window.location.search);
    const skillId = params.get("skill");
    if (!skillId) return;
    if (params.get("feedback_prefill") !== "1") {
      clearFeedbackPrefill();
      return;
    }
    const row = teamSkills.find((s) => s.id === skillId);
    if (!row) return;
    feedbackDeepLinkRef.current = true;
    const handoff = peekFeedbackPrefill(skillId);
    if (handoff?.prefill?.kind !== "team_skill_body") return;
    setDrawerTarget({ kind: "team", skill: row, canManage: Boolean(me.is_team_admin), canEdit: canEditTeam(row), prefill: handoff });
  }, [me, teamSkills, canEditTeam]);

  const accessRequestsEnabled = accessRequestsEnabledFrom(me);
  useEffect(() => {
    if (!accessRequestsEnabled || typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("feedback_prefill") === "1") return;
    setSkillParam(params.get("skill"));
  }, [accessRequestsEnabled]);
  const skillDeepLinkRef = useRef<string | null>(null);
  useEffect(() => {
    if (!accessRequestsEnabled || !skillParam || !me || skillDeepLinkRef.current === skillParam) return;
    const row = teamSkills.find((s) => s.id === skillParam);
    if (!row) return;
    skillDeepLinkRef.current = skillParam;
    setDrawerTarget({ kind: "team", skill: row, canManage: canManageTeam(row), canEdit: canEditTeam(row) });
  }, [accessRequestsEnabled, skillParam, me, teamSkills, canManageTeam, canEditTeam]);
  const noAccessSkillId =
    accessRequestsEnabled && skillParam && teamLoaded && !teamSkills.some((s) => s.id === skillParam) ? skillParam : null;
  const leaveSkillDeepLink = useCallback(() => {
    setSkillParam(null);
    router.replace("/skills/");
  }, [router]);

  /* ─────────────────── renderers ─────────────────── */

  const renderSkillRow = (s: SkillSummaryDTO, group: GroupKey): ReactNode => {
    const isMine = s.source_type !== "builtin";
    const badges: ReactNode[] = [];
    // skill-import batch 3: pending row must explain itself; switch shows on but skill ineffective,
    // user has no further hint after closing upload receipt (review P1-3; backend DTO specifies same composite state).
    if (s.import_status === "pending")
      badges.push(
        <span key="imp" className="sbadge skeleton">
          {ti("pending_badge")}
        </span>,
      );
    if (s.source_type === "user_upload")
      badges.push(
        <span key="src" className="sbadge upload">
          <Upload size={9} />
          {t("source.user_upload")}
        </span>,
      );
    if (s.source_type === "user_generated")
      badges.push(
        <span key="src" className="sbadge gen">
          <Sparkles size={9} />
          {t("source.user_generated")}
        </span>,
      );
    if (group === "analysis")
      badges.push(
        <span key="auto" className="sbadge auto">
          {t("badge_auto")}
        </span>,
      );
    if (SKELETON_SLUGS.has(s.name))
      badges.push(
        <span key="skel" className="sbadge skeleton">
          <AlertTriangle size={9} />
          {t("badge_skeleton")}
        </span>,
      );
    const meta = [s.version ? `v${s.version}` : null, s.size_bytes ? formatByteSize(s.size_bytes) : null]
      .filter(Boolean)
      .join(" · ");
    const title = skillTitle(s);
    const openDrawer = () => setDrawerTarget({ kind: "skill", skill: s });
    const openUsage = () => setDrawerTarget({ kind: "skill", skill: s, initialTab: "usage" });
    return (
      <div
        key={s.name}
        className={`srow${s.enabled ? "" : " off"}${flashName === s.name ? " flash" : ""}`}
      >
        <button
          type="button"
          className="srow-main"
          aria-label={`${t("detail_btn")} ${title}`}
          onClick={openDrawer}
        >
          <div className="srow-titlerow">
            <span className="srow-title">{title}</span>
            {/* A title derived from the slug must not repeat it; the slug stays visible in the drawer. */}
            {title !== s.name && title !== humanizeSkillSlug(s.name) ? <span className="srow-slug">{s.name}</span> : null}
            {badges}
            {meta ? <span className="srow-meta">{meta}</span> : null}
          </div>
          <div className="srow-desc">{skillDescription(s)}</div>
        </button>
        <div className="srow-usage">
          <UsageSummaryInline summary={s.usage_summary} onOpen={openUsage} />
        </div>
        <div className="srow-actions">
          {isMine ? (
            <RowActionsMenu
              label={t("more_actions_for", { name: title })}
              actions={[{
                id: "delete",
                label: t("delete_title"),
                danger: true,
                onSelect: () => {
                  if (data?.impact_guard_enabled) void openSkillImpact("personal", "delete", s);
                  else setDeleteTarget(s);
                },
              }]}
            />
          ) : null}
        </div>
        <div className="srow-switch-right">
          <SkillSwitch
            on={s.enabled}
            busy={busyRows.has(s.name)}
            label={t("toggle_aria", { name: title })}
            tooltip={t("scope_user_tooltip")}
            buttonRef={(node) => bindRowSwitch(`skill:${s.name}`, node)}
            onToggle={() => onToggle(s, !s.enabled, `skill:${s.name}`)}
          />
        </div>
      </div>
    );
  };

  const importProvenanceTitle = (
    p: NonNullable<TeamSkillPublic["import_provenance"]>,
    tr: ReturnType<typeof useTranslations>,
  ): string => {
    const lines = p.findings.slice(0, 5).map((f) => `· ${f.subject || f.code || ""}${f.message_zh ? ` — ${f.message_zh}` : ""}`);
    if (p.truncated || (typeof p.red_count === "number" && p.red_count > p.findings.length)) lines.push(tr("import_overridden_more"));
    return [tr("import_overridden_hint"), ...lines].join("\n");
  };

  const renderSharedRow = (s: TeamSkillPublic): ReactNode => {
    const manage = canManageTeam(s);
    const appr = s.approval || "approved";
    const isApproved = appr === "approved";
    const teamOff = s.status !== "enabled";
    const perUserOn = s.my_enabled !== false;
    const enabledForUser = teamSkillEnabledForUser(s);
    const audience = teamSkillAudience(s);
    const title = s.display_name?.trim() || humanizeSkillSlug(s.name);
    const openDrawer = () => setDrawerTarget({ kind: "team", skill: s, canManage: manage, canEdit: canEditTeam(s) });
    const openUsage = () =>
      setDrawerTarget({
        kind: "team",
        skill: s,
        canManage: manage,
        canEdit: canEditTeam(s),
        initialTab: "usage",
      });
    return (
      <div
        key={s.id}
        className={`srow${enabledForUser ? "" : " off"}`}
      >
        <button
          type="button"
          className="srow-main"
          aria-label={`${t("detail_btn")} ${title}`}
          onClick={openDrawer}
        >
          <div className="srow-titlerow">
            <span className="srow-title">{title}</span>
            {title !== s.name && title !== humanizeSkillSlug(s.name) ? <span className="srow-slug">{s.name}</span> : null}
            <span className="sbadge from">{t("badge_from", { name: teamSkillOwnerName(s, ownerFallback) })}</span>
            {isApproved ? (
              <span className="sbadge from">
                {audience === "team_wide"
                  ? t("audience.team_wide")
                  : audience === "custom"
                    ? t("audience.custom", { count: s.share_count ?? 0 })
                    : t("audience.private")}
              </span>
            ) : null}
            {/* Approval badge priority (review P2-2): suppress verified badge for unapproved rows. */}
            {!isApproved ? (
              appr === "pending" ? (
                <span className="sbadge skeleton">{t("badge_approval_pending")}</span>
              ) : (
                <span className="sbadge unverified is-rejected" title={s.rejected_reason || undefined}>
                  {t("badge_approval_rejected")}
                </span>
              )
            ) : s.verdict === "pass" ? (
              <span className="sbadge verified"><CharIconText ch="✓">{t("badge_verified")}</CharIconText></span>
            ) : (
              <span className="sbadge unverified">{t("badge_unverified")}</span>
            )}
            {isApproved && s.candidate_state === "pending" ? (
              <span className="sbadge skeleton">{t("badge_candidate_pending")}</span>
            ) : null}
            {/* ⑨: evidence of author knowingly enabling. Say “作者明知有 N 条阻断项仍然启用”, not “有 N 条错误”:
                reviewer must weigh person's choice, whereas latter sounds like platform quality judgment. */}
            {s.import_provenance ? (
              <span className="sbadge unverified" title={importProvenanceTitle(s.import_provenance, t)}>
                {typeof s.import_provenance.red_count === "number" && s.import_provenance.red_count > 0
                  ? t("badge_import_overridden", { count: s.import_provenance.red_count })
                  : t("badge_import_overridden_unknown")}
              </span>
            ) : null}
            {isApproved && teamOff ? <span className="sbadge unverified">{t("shared_ro_off")}</span> : null}
            {s.size_bytes ? <span className="srow-meta">{formatByteSize(s.size_bytes)}</span> : null}
          </div>
          <div className="srow-desc">{skillDescription(s)}</div>
        </button>
        <div className="srow-usage">
          <UsageSummaryInline summary={s.usage_summary} onOpen={openUsage} />
        </div>
        <div className="srow-actions">
          {manage ? (
            <>
              <RowActionsMenu
                label={t("more_actions_for", { name: title })}
                actions={[
                  ...(isApproved ? [{ id: "share", label: t("share_title"), onSelect: () => setShareTarget(s) }] : []),
                  {
                    id: "delete",
                    label: t("delete_title"),
                    danger: true,
                    onSelect: () => {
                      if (teamImpactGuard) void openSkillImpact("team_asset", "delete", s);
                      else setTeamDeleteTarget(s);
                    },
                  },
                ]}
              />
            </>
          ) : null}
        </div>
        {isApproved ? (
          // Per-user switch (own user only); team-level toggles at /team/settings/workflows (admin governance).
          <div className="srow-switch-right">
            <SkillSwitch
              on={perUserOn}
              busy={busyRows.has(s.id)}
              label={t("toggle_aria", { name: title })}
              tooltip={t("scope_user_tooltip")}
              buttonRef={(node) => bindRowSwitch(`team:${s.id}`, node)}
              onToggle={() => void onTeamPerUserToggle(s, !perUserOn, `team:${s.id}`)}
            />
          </div>
        ) : (
          <div className="srow-status-right">
            <span className={`ro-stat ${appr === "pending" ? "on" : "off"}`}>
              <span className="d" />
              {appr === "pending" ? t("badge_approval_pending") : t("badge_approval_rejected")}
            </span>
          </div>
        )}
      </div>
    );
  };

  const renderGroup = (opts: {
    group: GroupKey;
    name: string;
    rows: ReactNode[];
    enabledCount: number;
    totalCount: number;
    hint?: string;
    hintBanner?: string;
    bulkSkills?: SkillSummaryDTO[];
  }): ReactNode => {
    const { group, name, rows, enabledCount, totalCount, hint, hintBanner, bulkSkills } = opts;
    // Search and a selected category are answer states: never hide their matching rows
    // behind a remembered/default collapsed group.
    const isCollapsed = !searching && cat === "all" && !!collapsed[group];
    const busy = bulkBusy[group];
    const countText = searching
      ? t("group.hit_count", { count: totalCount })
      : t("group.enabled_count", { enabled: enabledCount, total: totalCount });
    return (
      <section key={group} className="sgroup">
        <div className={`sgroup-head${isCollapsed ? " collapsed" : ""}`}>
          <button
            type="button"
            className="sgroup-toggle"
            aria-expanded={!isCollapsed}
            onClick={() => setCollapsed((c) => ({ ...c, [group]: !isCollapsed }))}
          >
            <span className="caret">
              <ChevronDown size={15} />
            </span>
            <span className="gname">{name}</span>
            <span className="gcount">{countText}</span>
            {hint ? <span className="ghint">{hint}</span> : null}
          </button>
          {bulkSkills ? (
            <span className="gbulk">
              <button
                type="button"
                className={busy === "enable" ? "busy" : ""}
                disabled={!!busy}
                onClick={() => {
                  void runBulk(group, bulkSkills, true);
                }}
              >
                {busy === "enable" ? <span className="spin" /> : null}
                {t("group.bulk_enable")}
              </button>
              <span className="bdot" />
              <button
                type="button"
                className={busy === "disable" ? "busy" : ""}
                disabled={!!busy}
                onClick={() => {
                  void runBulk(group, bulkSkills, false);
                }}
              >
                {busy === "disable" ? <span className="spin" /> : null}
                {t("group.bulk_disable")}
              </button>
            </span>
          ) : null}
        </div>
        {!isCollapsed ? (
          <div className="sgroup-list">
            {hintBanner ? (
              <div className="ghint-banner">
                <Info size={13} /> {hintBanner}
              </div>
            ) : null}
            {rows}
          </div>
        ) : null}
      </section>
    );
  };

  /* ── assemble visible groups ── */
  const groups: ReactNode[] = [];
  if (data) {
    for (const g of GROUP_ORDER) {
      if (!groupVisible(g)) continue;
      if (g === "mine") {
        if (mineAll.length === 0 && !searching && !onlyEnabled) {
          // ① empty state: new-user default (dual-CTA card).
          groups.push(
            <section key="mine" className="sgroup">
              <div className="sgroup-head legacy-skills-group-head--static">
                <span className="caret">
                  <ChevronDown size={15} />
                </span>
                <span className="gname">{t("group.mine")}</span>
                <span className="gcount">{t("group.enabled_count", { enabled: 0, total: 0 })}</span>
              </div>
              <div className="mine-empty">
                <div className="me-ico">
                  <Package size={22} />
                </div>
                <div className="me-t">{t("mine_empty_text")}</div>
                <div className="me-cta">
                  <button type="button" className="cta-card" onClick={() => setUploadOpen(true)}>
                    <span className="cc-t">
                      <Upload size={15} /> {t("mine_empty_upload")}
                    </span>
                    <span className="cc-s">{t("mine_empty_upload_sub")}</span>
                  </button>
                  <button type="button" className="cta-card" onClick={() => navigateAway("/")}>
                    <span className="cc-t">
                      <Sparkles size={15} /> {t("mine_empty_chat")}
                    </span>
                    <span className="cc-s">{t("mine_empty_chat_sub")}</span>
                  </button>
                </div>
              </div>
            </section>,
          );
        } else if (mine.length > 0) {
          groups.push(
            renderGroup({
              group: "mine",
              name: t("group.mine"),
              hint: t("group.mine_hint"),
              rows: mine.map((s) => renderSkillRow(s, "mine")),
              enabledCount: mine.filter((s) => s.enabled).length,
              totalCount: mine.length,
              bulkSkills: mine,
            }),
          );
        }
      } else if (g === "shared") {
        if (shared.length > 0) {
          groups.push(
            renderGroup({
              group: "shared",
              name: t("group.shared"),
              rows: shared.map(renderSharedRow),
              enabledCount: shared.filter(teamSkillEnabledForUser).length,
              totalCount: shared.length,
              hint: t("group.shared_hint"),
            }),
          );
        }
      } else {
        const rows = filterRows(builtinByCat[g]);
        if (rows.length > 0) {
          groups.push(
            renderGroup({
              group: g,
              name: t(`group.${g}` as Parameters<typeof t>[0]),
              rows: g === "documents"
                ? DOCUMENT_SKILL_GROUP_ORDER.flatMap((key) => {
                    const members = rows.filter((s) => documentSkillGroup(s.name) === key);
                    if (!members.length) return [];
                    const label = t(`document_groups.${key}.title`);
                    return [
                      <section key={key} className="document-skill-group" aria-label={label}>
                        <div className="ghint-banner">
                          <div className="document-skill-heading">
                            <h3>{label}</h3>
                            <p>{t(`document_groups.${key}.hint`)}</p>
                          </div>
                        </div>
                        {members.map((s) => renderSkillRow(s, g))}
                      </section>,
                    ];
                  })
                : rows.map((s) => renderSkillRow(s, g)),
              enabledCount: rows.filter((s) => s.enabled).length,
              totalCount: rows.length,
              hint: g === "analysis" ? t("group.analysis_hint") : undefined,
              bulkSkills: rows,
            }),
          );
        }
      }
    }
  }

  const managementAction = managementDestination
    ? {
        id: "manage-team-skills",
        label: managementDestination.label,
        href: appPath(managementDestination.href),
        onSelect: (event: React.MouseEvent<HTMLElement>) => {
          if (!shouldHandleMenuLinkSelect(event)) return;
          event.preventDefault();
          router.push(managementDestination.href);
        },
      }
    : null;
  const mobileMoreActions = [
    ...(data?.authoring_enabled ? [{ id: "create-skill", label: t("create.title"), onSelect: () => setCreateOpen(true) }] : []),
    ...(managementAction ? [managementAction] : []),
  ];

  const catChips: { key: CatFilter; label: string }[] = [
    { key: "all", label: t("filter.all") },
    { key: "analysis", label: t("group.analysis") },
    { key: "documents", label: t("group.documents") },
    { key: "design", label: t("group.design") },
    { key: "platform", label: t("group.platform") },
    { key: "mine", label: t("filter.mine") },
  ];

  return (
    <>
      <div className="sset">
        {/* PageHeader (Header, Language & Paging Spec §2c, D6): primary Upload, secondary Create (authoring only),
            and the team-management link in ⋯; phones put Upload in the top bar and Create first in its ⋯. */}
        <PageHeader
          title={t("title")}
          description={t("subtitle")}
          moreActions={managementAction ? (
            <RowActionsMenu className="sset-header-more" label={t("more_actions")} actions={[managementAction]} />
          ) : null}
          secondary={data?.authoring_enabled ? (
            <PageActionButton variant="secondary" icon={LucidePlus} onClick={() => setCreateOpen(true)}>
              {t("create.title")}
            </PageActionButton>
          ) : null}
          primary={(
            <PageActionButton icon={LucideUpload} onClick={() => setUploadOpen(true)}>
              {t("upload_btn")}
            </PageActionButton>
          )}
        />
        <PageFrameMobileActions>
          {mobileMoreActions.length ? (
            <RowActionsMenu className="sset-mobile-more" label={t("more_actions")} actions={mobileMoreActions} />
          ) : null}
          <PageMobileAction label={t("upload_btn")} icon={LucideUpload} onClick={() => setUploadOpen(true)} />
        </PageFrameMobileActions>

        <CapabilityRepairContext kind="skill" onDependency={setQuery} />

        {error ? (
          <ErrorNotice
            message={error.message}
            action={error.action}
            diagnostics={error.diagnostics}
            surface="settings"
            onAction={() => {
              void refresh();
              void refreshTeam();
            }}
            diagnosticsEnabled
          />
        ) : null}

        {loading ? (
          <div className="page-loading">{t("loading")}</div>
        ) : noAccessSkillId ? (
          <AccessRequestPanel
            kind="team_skill"
            resourceId={noAccessSkillId}
            onBack={leaveSkillDeepLink}
            onOpen={() => void refreshTeam()}
          />
        ) : data ? (
          <>
            <div className="filterbar">
              <div className="searchbox">
                <span className="si">
                  <Search size={15} />
                </span>
                <input
                  type="search"
                  name="skill_search"
                  autoComplete="off"
                  value={query}
                  placeholder={t("search_placeholder")}
                  aria-label={t("search_placeholder")}
                  onChange={(e) => setQuery(e.target.value)}
                />
                {query ? (
                  <button type="button" className="clr" aria-label={t("clear_search")} onClick={() => setQuery("")}>
                    <X size={13} />
                  </button>
                ) : null}
              </div>
              <div className="chips">
                {catChips.map(({ key, label }) => (
                  <button
                    key={key}
                    type="button"
                    className={`fchip${cat === key ? " on" : ""}`}
                    aria-pressed={cat === key}
                    onClick={() => setCat(key)}
                  >
                    {label}
                  </button>
                ))}
                <span className="sep" />
                <button
                  type="button"
                  className={`fchip toggle${onlyEnabled ? " on" : ""}`}
                  aria-pressed={onlyEnabled}
                  onClick={() => setOnlyEnabled((v) => !v)}
                >
                  <span className="tk">{onlyEnabled ? <CharIcon ch="✓" /> : ""}</span>
                  {t("filter.only_enabled")}
                </button>
              </div>
            </div>

            {searching && visibleRowTotal === 0 ? (
              <div className="noresult">
                <div className="nr-ico">
                  <Search size={18} />
                </div>
                <div className="nr-t">{t.rich("no_result", { kw: query.trim(), b: (chunks) => <b>{chunks}</b> })}</div>
                <button type="button" className="btn sec sm" onClick={() => setQuery("")}>
                  {t("clear_search")}
                </button>
              </div>
            ) : (
              groups
            )}

            {mineAll.length > 0 && totalSize > 0 ? (
              <div className="storage">
                <div className="storage-top">
                  <span className="st-lab">{t("storage.label")}</span>
                  <span className="st-val">{t("storage.value", { used: formatByteSize(totalSize), limit: formatByteSize(SKILL_STORAGE_LIMIT_BYTES) })}</span>
                </div>
                <div className="storage-bar">
                  <div
                    className="fill"
                    style={{ width: `${Math.max(0.5, Math.min(100, (totalSize / SKILL_STORAGE_LIMIT_BYTES) * 100))}%` }}
                  />
                </div>
                <div className="storage-note">{t("storage.note")}</div>
              </div>
            ) : null}
          </>
        ) : null}

        {/* ── overlays (all obtain tokens within .sset scope) ── */}
        {createOpen ? (
          <SkillCreateModal presentError={presentError} onClose={() => setCreateOpen(false)} onCreated={(skill) => {
            void refresh();
            setDrawerTarget({kind: "skill", skill});
          }} />
        ) : null}
        <SkillUploadModal
          open={uploadOpen}
          onClose={() => setUploadOpen(false)}
          onUploaded={(skill) => {
            void refresh();
            setFlashName(skill.name);
            if (flashTimer.current) clearTimeout(flashTimer.current);
            flashTimer.current = setTimeout(() => setFlashName(null), 2200);
          }}
          // skill-import batch 3: receipt “去编辑” directly opens skill drawer (inventory status area there too).
          onEdit={(skill) => setDrawerTarget({ kind: "skill", skill })}
        />

        {drawerTarget ? (
          <SkillDrawer
            target={drawerTarget}
            meId={me?.id ?? null}
            workflowCreatorEnabled={Boolean(me?.capabilities?.workflow_creator)}
            coachEnabled={Boolean(me?.capabilities?.feedback_loop)}
            teamRow={
              drawerTarget.kind === "skill" ? (teamSkills.find((x) => x.name === drawerTarget.skill.name) ?? null) : null
            }
            onClose={() => setDrawerTarget(null)}
            onToggle={(name, enabled) => {
              const all = [...(data?.builtin ?? []), ...mineAll];
              const s = all.find((x) => x.name === name);
              if (s) {
                if (!enabled && SKELETON_SLUGS.has(s.name)) {
                  setSkeletonConfirm(s);
                  return;
                }
                if (!enabled && data?.impact_guard_enabled) {
                  setDrawerTarget(null);
                  void openSkillImpact("personal", "disable", s);
                  return;
                }
                void (async () => {
                  if (!(await applyToggle(s.name, enabled))) return;
                  setDrawerTarget((cur) =>
                    cur && cur.kind === "skill"
                      ? { ...cur, skill: { ...cur.skill, enabled } }
                      : cur,
                  );
                })();
              }
            }}
            onTeamPerUserToggle={(s, enabled) => {
              void (async () => {
                if (!(await onTeamPerUserToggle(s, enabled))) return;
                setDrawerTarget((cur) =>
                  cur && cur.kind === "team"
                    ? { ...cur, skill: { ...cur.skill, my_enabled: enabled } }
                    : cur,
                );
              })();
            }}
            onTeamStateChanged={() => {
              void refreshTeam();
              void refresh();
            }}
            onNavigateAway={navigateAway}
            onEditInChat={(name) => {
              // “用对话修改” (PRD §3.8 decision 4: chat is editing): one-time sessionStorage handshake,
              // chat Workbench consumes on mount and prefills composer (see chat-workbench.tsx).
              try {
                sessionStorage.setItem(COMPOSER_PREFILL_KEY, t("drawer.edit_in_chat_prefill", { name }));
              } catch {
                /* Storage unavailable: navigate only. */
              }
              navigateAway("/");
            }}
            onDelete={(skill) => {
              if (data?.impact_guard_enabled) void openSkillImpact("personal", "delete", skill);
              else setDeleteTarget(skill);
            }}
          />
        ) : null}

        {skeletonConfirm ? (
          <DialogShell
            onClose={() => setSkeletonConfirm(null)}
            label={t("skeleton_confirm.title", {
              name: skillTitle(skeletonConfirm),
            })}
            mount="inline"
            backdropClassName="scrim"
            panelClassName="modal"
          >
            <span className="modal-ico plat">
              <AlertTriangle size={18} />
            </span>
            <h3 className="modal-title">{t("skeleton_confirm.title", { name: skillTitle(skeletonConfirm) })}</h3>
            <p className="modal-body">{t("skeleton_confirm.body")}</p>
            <div className="modal-foot">
              <button type="button" className="btn pri sm" onClick={() => setSkeletonConfirm(null)}>
                {t("skeleton_confirm.keep")}
              </button>
              <button
                type="button"
                className="btn sec sm"
                onClick={() => {
                  if (data?.impact_guard_enabled) {
                    void openSkillImpact("personal", "disable", skeletonConfirm);
                  } else {
                    void applyToggle(skeletonConfirm.name, false);
                  }
                  setSkeletonConfirm(null);
                }}
              >
                {t("skeleton_confirm.disable_anyway")}
              </button>
            </div>
          </DialogShell>
        ) : null}

        {deleteTarget ? (
          <DialogShell
            onClose={() => setDeleteTarget(null)}
            label={t("delete_confirm.title", {
              name: skillTitle(deleteTarget),
            })}
            mount="inline"
            backdropClassName="scrim"
            panelClassName="modal"
          >
            <span className="modal-ico danger">
              <Trash size={18} />
            </span>
            <h3 className="modal-title">{t("delete_confirm.title", { name: skillTitle(deleteTarget) })}</h3>
            <p className="modal-body">{t("delete_confirm.body")}</p>
            <div className="modal-foot">
              <button type="button" className="btn ghost sm" onClick={() => setDeleteTarget(null)}>
                {t("cancel")}
              </button>
              <button type="button" className="btn danger sm" disabled={!deleteTarget.id} onClick={() => deleteTarget.id && void performDelete(deleteTarget.id)}>
                <Trash size={13} /> {t("delete_confirm.confirm")}
              </button>
            </div>
          </DialogShell>
        ) : null}

        {teamDeleteTarget ? (
          <DialogShell
            onClose={() => setTeamDeleteTarget(null)}
            label={t("delete_confirm.title", {
              name:
                teamDeleteTarget.display_name || teamDeleteTarget.name,
            })}
            mount="inline"
            backdropClassName="scrim"
            panelClassName="modal"
          >
            <span className="modal-ico danger">
              <Trash size={18} />
            </span>
            <h3 className="modal-title">
              {t("delete_confirm.title", { name: teamDeleteTarget.display_name || teamDeleteTarget.name })}
            </h3>
            <p className="modal-body">
              {t(
                teamSkillAudience(teamDeleteTarget) === "team_wide"
                  ? "delete_confirm.team_wide_body"
                  : teamSkillAudience(teamDeleteTarget) === "custom"
                    ? "delete_confirm.custom_body"
                    : "delete_confirm.private_body",
              )}
            </p>
            <div className="modal-foot">
              <button type="button" className="btn ghost sm" onClick={() => setTeamDeleteTarget(null)}>
                {t("cancel")}
              </button>
              <button type="button" className="btn danger sm" onClick={() => void performTeamDelete()}>
                <Trash size={13} /> {t("delete_confirm.confirm")}
              </button>
            </div>
          </DialogShell>
        ) : null}

        {pendingImpact ? (
          <DialogShell
            onClose={() => setPendingImpact(null)}
            label={t(`impact.${pendingImpact.action}_title`, {
              name: "display_name" in pendingImpact.skill
                ? pendingImpact.skill.display_name || pendingImpact.skill.name
                : skillTitle(pendingImpact.skill),
            })}
            mount="inline"
            backdropClassName="scrim"
            panelClassName="modal"
          >
            <span className="modal-ico danger">
              <AlertTriangle size={18} />
            </span>
            <h3 className="modal-title">
              {t(`impact.${pendingImpact.action}_title`, {
                name: "display_name" in pendingImpact.skill
                  ? pendingImpact.skill.display_name || pendingImpact.skill.name
                  : skillTitle(pendingImpact.skill),
              })}
            </h3>
            <p className="modal-body">
              {t(`impact.${pendingImpact.action}_lead`)}
            </p>
            <div className="impact-facts" aria-label={t("impact.scope_label")}>
              <span>{t("impact.current_agents", { n: pendingImpact.impact.summary.current_agent_count })}</span>
              <span>{t("impact.draft_agents", { n: pendingImpact.impact.summary.draft_agent_count })}</span>
              <span>{t("impact.running_tasks", { n: pendingImpact.impact.summary.running_task_count })}</span>
              <span>{t("impact.schedules", { n: pendingImpact.impact.summary.scheduled_binding_count })}</span>
            </div>
            <p className="modal-body">{t("impact.runtime_note")}</p>
            {pendingImpact.impact.required_follow_up.length ? (
              <p className="modal-body">{t("impact.schedules_note")}</p>
            ) : null}
            <div className="modal-foot">
              <button type="button" className="btn ghost sm" onClick={() => setPendingImpact(null)}>
                {t("cancel")}
              </button>
              <button type="button" className="btn danger sm" disabled={loading} onClick={() => void confirmSkillImpact()}>
                {pendingImpact.action === "delete" ? <Trash size={13} /> : null}
                {t(`impact.${pendingImpact.action}_confirm`)}
              </button>
            </div>
          </DialogShell>
        ) : null}

        {shareTarget ? (
          <ShareDialog
            kind="team_skill"
            resourceId={shareTarget.id}
            label={shareTarget.display_name || shareTarget.name}
            teamWideLocked={!((shareTarget.approval || "approved") === "approved" && shareTarget.visibility === "team_wide")}
            downgradeLocked={!me?.is_team_admin}
            onClose={() => setShareTarget(null)}
            onChanged={() => {
              void refreshTeam();
            }}
          />
        ) : null}
      </div>
    </>
  );
}
