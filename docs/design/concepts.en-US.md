# Community concepts and stable identifiers

English | [简体中文](概念术语.md)

These naming and isolation rules apply to shared Community code. Preserve stored names even when product wording changes.

## Separate boundaries

| Concept          | Meaning                                                       | Stable identifiers                                        |
| ---------------- | ------------------------------------------------------------- | --------------------------------------------------------- |
| Team             | Membership, administration, and tenant isolation.             | `team_id`, `teams`, `team_members`                        |
| Space            | Collaboration, knowledge, sessions, and access inside a Team. | `workspace_id`, `workspaces`, `workspace_members`, `ws_*` |
| Datarc workspace | External data/query context resolved for the current user.    | `datarc_workspace_id`                                     |

Use Team, Space, and Datarc workspace in visible copy. A third-party workspace includes its product name, such as Slack workspace or Power BI workspace. The Community product name is Bowra; package names and `BOWHEAD_*` configuration remain unchanged.

A Datarc workspace resolves from the session override, then the user's default, then the only accessible workspace when unambiguous. Do not derive it from a Bowra Space. Resolve connections through the current Team's connection registry; there is no deployment, environment, member, or default-Space fallback.

## Shared objects

A User joins a Team and can access its Spaces through membership and resource grants. A Session contains Turns, events, tool calls, approvals, and references. An Agent declares behavior and resource scope; a Tool performs an operation; a Skill packages methods, references, templates, and scripts. Selecting a resource does not grant permission to use it.

An Artifact is the common identity for uploads, tool outputs, deliverables, exports, and captured pages. File management presents these records. A Canvas contains sections and revisions. Sharing grants access to authenticated Team members and does not publish anonymously.

## Registered ID prefixes

| Prefix                              | Meaning                                                                |
| ----------------------------------- | ---------------------------------------------------------------------- |
| `team_`, `usr_`, `om_`              | Team, user, Team membership.                                           |
| `ws_`, `s_`, `t_`, `tl_`            | Space, session, turn, tool call.                                       |
| `plan_`, `ps_`, `apr_`              | Plan, plan step, approval.                                             |
| `art_`, `cv_`, `sec_`, `rv_`        | Artifact, canvas, section, canvas revision.                            |
| `osa_`, `cs_`, `afk_`               | Team Agent, legacy custom Agent, copy lineage.                         |
| `sk_`, `oskill_`, `tcap_`           | Skill instance, Team Skill, Team Capability.                           |
| `dc_`, `mcp_`, `ots_`               | Datarc connection, MCP server, Team tool source.                       |
| `pcm_`, `pmt_`, `pmi_`              | Personal MCP connection, cached tool, invocation.                      |
| `kbf_`, `kbg_`, `kbj_`              | Knowledge file, index generation, rebuild job.                         |
| `sj_`, `sr_`, `led_`, `aud_`, `lu_` | Schedule, scheduled run, credit ledger, audit event, model usage.      |
| `cand_`, `gl_`                      | Memory candidate, personal glossary item.                              |
| `snap_`, `anc_`                     | Memory recovery snapshot, note anchor.                                 |
| `mop_`, `mrg_`, `pdr_`              | Memory governance operation, resource group, privacy deletion receipt. |

Preserve `workspace:{id}`, `X-Workspace-Id`, `kb_ws_*`, and existing stored `workspace_id` fields. The external Datarc context always uses `datarc_workspace_id`. Do not invent new meanings for existing prefixes.

## Invariants

1. Live display, persisted data, and replay must agree.
2. Check Team membership and resource grants at use time; possession of an ID never authorizes access.
3. Preserve the data-context snapshot for executions, approvals, and schedules.
4. Extend Artifact records for file functionality instead of creating another file identity store.
5. Isolate user and Team Skill content; uploaded content never becomes a global same-name built-in.
6. Team definitions and glossary changes require Team-visible approval paths. Personal preferences and one-turn instructions cannot overwrite them.
7. Outbound URL policy follows `BOWHEAD_DEPLOYMENT_MODE`: `local` supports intranet HTTP and nonstandard ports; `saas` requires public HTTPS and blocks private/reserved addresses and restricted ports.

Run `packages/bowhead_core/tests/test_workspace_datarc_namespace_guard.py` after changing naming boundaries or connection resolution.

Coach proposal review groups related Agent changes or suggested tests. The Owner may expand the review to inspect differences or select individual items. Adopting tests does not run them or release an Agent. Direct Coach writes to an existing draft retain their original receipt and do not require a second adoption.
