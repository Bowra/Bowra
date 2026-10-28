# Author resources and return to the original Agent

Use only the resource path needed for the request. A personal Skill edit needs no
Agent draft, report, shared Plugin or evaluation. A single-Agent binding normally
needs no shared Plugin change. Use exact resource discovery when a dependency is
unknown, and stop searching when the needed fact is established.

## Personal Skills

Inspect by stable name with `agent_skill_inspect`. An absent package has null ID
and revision; never guess an existing identity. Inspection can read disabled
personal Skills without enabling them. Read only needed text with `agent_skill_read`
and its character offsets; paged files must retain the same target revision.
Preserve unrelated text and binary assets.

Author focused `{path, content}` changes, then `agent_skill_validate` and
`agent_skill_save`. Supported writable paths are `SKILL.md`, `references/`,
`scripts/` and `evals/`; null deletes a file but the manifest cannot be deleted.
For a text-only method, write the requested package directly rather than reading
multiple whole frameworks for style. A draft-only request stops before saving.

Saving directly changes the personal package, preserving ownership/preferences
and requiring import inspection of the new bytes. It does not publish a Team
Skill, bind an Agent, grant execution or prove automatic selection/business success.
Return the original save receipt and bounded inspection findings, including
warnings, failures and unread-finding pointers. Do not copy the package through
Chat again merely to prove it was saved.

Unknown saves recover through `agent_skill_operation_get(recover=true)` on the
original operation, including from a later owned Chat in the same Team/Space.
Current authority and original hashes still govern recovery; conflicts preserve
newer content and recovery data. Inspect the current package before another edit:
a successful historical receipt is not a current baseline or current inspection.

For an explicit stop request, use `agent_skill_cancel` on the original operation.
`cancelled` means unapplied work was cancelled; `already_applied` reconciles a
completed save and does not undo it; `already_terminal` retains an earlier result.
Unknown/conflicting outcomes require diagnosis, not package deletion, ledger reset
or a new save. Closing a view is not cancellation.

## Shared Plugins

A Team Plugin is an optional shared selection of Tools and exact non-personal
Skills. Search `kind="capability"`, then use `agent_plugin_inspect` for its exact
target and shared-consumer impact. Discuss shared impact within the actual request;
one Agent's change must not silently alter other consumers.

For creation use a null target, a name and nonempty resources. For editing retain
the target and `expected_impact`; supply only changed fields. Null fields are
omitted, while arrays replace their selections. Preserve unrelated entries and
exact Skill grant identity, revision, permissions and distribution.

`agent_plugin_validate` checks current dependencies, including implied wrappers.
Personal/creator-private Skills cannot enter a shared Plugin. Read permission is
the conservative choice; invocation needs an invokable package and eligible Tools.
Only the current Team administrator with write entitlement can
`agent_plugin_save`, with unchanged version/impact and rechecked dependencies.
Compilation is not execution, Safety Check, a consumer grant or release permission.

Keep the original operation ID for `agent_plugin_operation_get`. Same request and
payload recover the save; later use requires current inspection/validation. Saving
a Plugin does not bind an Agent or rewrite existing released/running snapshots.

## Missing Tools

After scoped discovery, retain a real gap with `agent_tool_requirement_create`:
operation, input/output contract, reason, acceptable alternatives and acceptance
cases. Attach the exact original parent when serving an Agent draft, existing
Agent, personal Skill or Plugin. Independent resource work needs no dummy Agent.
Never put credentials in the requirement or Skill files.

Use OpenAPI/hosted MCP onboarding only for supported remote connections. Native
implementation, local transport or unsupported authentication requires engineering
handoff. `agent_tool_requirement_open` requests the existing administrator
connection UI; `agent_operation_open` shows the saved requirement receipt.
If onboarding returns `tool_engineering_delivery_required`, follow its read-only
`view_action` instead of creating another requirement. A member's handoff does not
grant administration, and event delivery does not prove a connection exists.
Credentials and OAuth stay in the authenticated connection UI.

After external completion, discover the actual Tool and call
`agent_tool_requirement_reconcile` with its exact reference. Retain the returned
observation ID; recover it with `agent_tool_requirement_get`. Matching names,
callbacks or user-supplied IDs are not proof of availability. Current authorization
and original parent/Space still apply after interruptions or revocation.

Compare the actual schema with the requirement. An observation proves selectable
metadata, not creation provenance, accepted binding or a successful business task.
Do not retry unknown connection creation or expand Coach's frozen tool scope.

## Original editors and parent bindings

`agent_authoring_inspect` returns the selected editor and exact authoring baseline.
Unsaved input, pending saves or a changed baseline may block admission. Later tab
switches do not retarget this request. Use `agent_resource_open` with an actual
discovered reference and original parent to show the existing editor; opening is
neither saving nor proof that the user saw it. Resources without an admitted Coach
writer retain their original governance/editor, including Team Skill publication.

When resource work serves an Agent, preserve `original_parent`. Read that same
parent's current baseline and the child's current reference. Read `agents.md` for
the requested binding, passing optional `resource_context` with exact resources
and readable original save/observation references. An externally created resource
may have no readable private receipt: leave `source_operation` null rather than
claiming creation provenance.

The binding uses the parent's actual writer: a new Agent gets a pending suggestion;
an existing Agent's successful `agent_change_propose` saves the draft binding.
The resource save alone does neither. Preserve newer parent input, reconsider
changed dependencies, and keep both original receipts for recovery. For a shared
Plugin parent, reread impact and use its own administrator-only writer instead.
