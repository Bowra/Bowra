---
name: release-notes
description: "Write/review Bowhead release notes / changelog / GA copy. Use for 'release note', 'changelog', '更新记录', '发布说明', or auditing a release for over-promising."
trigger_examples:
  - "帮我写 v0.62.0 release note"
  - "把这些 PR 整理成发布说明"
  - "看看这版 GA copy 有没有过度承诺"
  - "更新 docs/help/release-notes"
  - "write the changelog for this release"
version: 0.1.0
title: "Release notes writer"
title_zh: "发布说明撰写"
category: platform
---

# Bowhead Release Notes

Use this skill to draft or review Bowhead product release notes. The goal is not to list commits; it is to explain shipped user, admin, and developer impact accurately.

## Read first

When working in the Bowhead repo, inspect these before writing:

- `docs/help/release-notes/index.en-US.md`
- `docs/help/release-notes/index.zh-CN.md`
- Recent files in `docs/help/release-notes/releases/`
- `docs/release-playbook.md`
- The PRs, commits, sprint docs, or user-provided bullet list for this release

Prefer the latest shipped notes as the style source. Older notes may have one-off section names; do not copy a historical exception unless the release is similarly small.

## Workflow

### Step 1 · Establish release truth

Collect the facts before drafting:

- Version, release date, and whether this is `unreleased` or a shipped version.
- What is actually enabled for users at ship time.
- Feature flags, allowlists, staged rollout, dogfood-only behavior, or default-off work.
- Breaking changes, downtime, migrations, admin actions, API/schema/event changes, deprecations, and compatibility notes.
- User-facing fixes and behavior changes, including silent behavior changes.
- Links to docs or guides when the note would otherwise become a tutorial.

If the rollout status is unclear, ask or mark it as a blocker. Do not paper over uncertainty with "now", "available", or "launched".

### Step 2 · Classify changes

Use the standard sections when they apply:

- `Highlights` / `重点更新`: 2-5 user-facing changes with clear impact.
- `Improvements` / `改进`: smaller improvements, polish, new support, or expanded coverage.
- `Fixes` / `修复`: bugs and reliability fixes.
- `Admin Notes` / `管理员说明`: deployment, permissions, organization settings, configuration, compatibility, migrations that admins must know.
- `Developer Notes` / `开发者说明`: API, events, data structures, SDKs, schema, migrations, deprecations.
- `User Impact` / `用户影响`: acceptable for tiny patch releases when the note is mostly fixes and the user-facing consequence deserves a short separate section.

Omit empty sections. For a tiny patch, `Fixes` alone can be enough.

### Step 3 · Draft the pages

Release pages are paired by locale:

- `docs/help/release-notes/releases/vX.Y.Z.en-US.md`
- `docs/help/release-notes/releases/vX.Y.Z.zh-CN.md`

Use this English shape:

```markdown
---
title: vX.Y.Z
summary: One sentence covering the main user/admin/developer impact.
version: X.Y.Z
released: YYYY-MM-DD
---

# vX.Y.Z

Repeat the summary sentence.

## Highlights

- Impact first, then feature name if needed.
```

Use this Chinese shape:

```markdown
---
title: vX.Y.Z
summary: 一句话概括主要用户、管理员或开发者影响。
version: X.Y.Z
released: YYYY-MM-DD
---

# vX.Y.Z

重复 summary 句子。

## 重点更新

- 先写用户影响，再写功能名称。
```

For `unreleased`, use `status: unreleased` and `updated: YYYY-MM-DD` instead of `version` and `released`.

### Locale style

- Keep product names stable: Bowhead, Chat, Canvas, Settings, Skills, Workflows, Subagents, Datarc.
- Chinese notes may naturally keep terms like `workspace`, `turn id`, `access token`, `feature flag`, `light / dark`; follow existing release notes instead of forcing awkward translations.
- Do not make Chinese a literal translation if a shorter, more natural sentence is clearer.

### Index updates

When cutting a shipped release:

- Add both locale pages.
- Insert the version near the top of `docs/help/release-notes/index.*.md`.
- Update the index `updated` date.
- If moving items out of `unreleased`, remove or rewrite the corresponding `unreleased.*` bullets and summary.

## Writing rules

- Lead with what changes for the reader; name internal architecture only when it explains action or risk.
- Release notes are by shipped version, not commit, PR, sprint, or natural date.
- Include only changes that matter to users, administrators, or developers.
- Put breaking changes or deprecations near the top and make the required action explicit.
- Keep bullets concrete and short. Prefer one sentence; use two only when the impact needs a boundary.
- Avoid internal code names, Greek sprint names, trace-only jargon, and raw implementation details in `Highlights`.
- Do not turn release notes into user guides. Link to a guide when instructions become multi-step.

## Accuracy checks

Before finalizing, run this review:

1. **Promise check**: Every "now", "added", "available", or "fixed" statement matches what users will see at ship time.
2. **Rollout check**: Default-off, allowlisted, internal dogfood, or staged work is labeled accurately or omitted until GA.
3. **Silent behavior check**: If behavior changed without a new visible feature, say what users will experience.
4. **Audience check**: Admin-only details are under `Admin Notes`; API/schema/migration details are under `Developer Notes`.
5. **Disclaimers check**: User-facing GA copy should be mostly promises, not "not in scope" disclaimers. Put internal limitations in internal notes or support docs when they are not user-actionable.
6. **Attribution check**: If multiple sprints ship together and feedback needs to map to causes, split bullets so users and support can tell which change did what.
7. **Pairing check**: English and Chinese pages have matching facts, dates, sections, and links.

## Common traps

- Do not say "now directly answers" when the feature flag is still default-off. Say "available in a limited rollout", "behind an admin-controlled flag", or wait for the GA release.
- Do not announce visual or UX unification unless all visible surfaces covered by the wording actually changed.
- Do not let known limitations dominate a public release note. If the main message becomes a disclaimer, the item probably belongs in an internal release note or support wiki.
- Do not hide a user-facing behavior change inside an implementation bullet.
- Do not list test-only scripts, refactors, or infra cleanup unless they change reliability, admin operations, or developer contracts.

## Final response

When returning release-note work, summarize:

- Files changed or drafted.
- Any assumptions or unresolved rollout questions.
- Whether the note is ready to ship, needs PM/admin review, or is blocked by missing facts.
