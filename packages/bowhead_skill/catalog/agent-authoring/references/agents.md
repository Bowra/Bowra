# Create or edit an Agent

Use this method for Agent configuration and resource bindings. For a complaint
about answer quality, read `evaluation-cases.md` before editing unless the Owner
explicitly requests a prompt change without checking the criterion.

## Resolve the intended object

Use the Agent named in the request, the selected editor from
`agent_authoring_inspect`, or the Agent of the current alignment loop. If none
identifies one and discovery finds multiple owned Agents, ask which before reading
or changing any of them. Do not infer identity from a plausible name.

For an existing Agent, search `kind="agent"` and read its exact returned reference
with `agent_resource_get`. Use its `authoring_target` and saved content. A
working-copy target is not a catalog resource reference. If only that target was
supplied, discover by its exact Agent ID and compare the returned baseline.
`writable=false` for an unbound editor does not decide an explicit target's rights;
each authoring operation checks its own authority.

When no working copy exists, `agent_working_copy_open` can establish one from the
exact current live release. It preserves existing edits and returns the current
baseline, not a recovery receipt for an earlier save. Legacy baseline import stays
in the original Owner flow. Never edit the released version directly.

## Existing Agent writes

`agent_change_validate` checks JSON Patch operations and atomic groups against the
exact target, with separate structure and dependency results. It does not run
Safety Check or evaluation. Use canonical paths:

- Description: `/listing/description`; instructions: `/definition/system_prompt`.
- Resources: `/definition/tool_refs`, `/definition/skill_grants`,
  `/definition/capability_ids`.
- Model/limits/output: `/definition/model`, `/definition/max_iterations`,
  `/definition/output_schema`.

Inspect the baseline before choosing `add`, `replace` or `remove`. Each operation
belongs to one explicit atomic group; dependent groups declare `depends_on`.
For example, a one-operation group is `{"id":"behavior","operation_indexes":[0]}`.
Replacing a selection array must preserve unrelated selections. Exact Skill grants
retain source kind, ID, name, revision, permissions and distribution; do not add
aliases for imported legacy fields.

`agent_change_propose` writes an already-authored patch into the existing draft
as the user. On success, use the receipt's next `target` for further edits in this
Turn. The stored proposal is accepted by that write, not waiting for another
Owner adoption. Read its original receipt with `agent_change_get` after uncertainty
or in a later authorized Turn. Same request and payload recover without another write.

- `agent_edit_permission_required`: nothing was written; explain the suggestion
  in Chat without claiming a saved draft.
- `agent_skill_dependency_missing`: nothing was written. Include the listed
  required Skills in the requested resource change when eligible, otherwise name
  the unavailable dependency. Do not widen an unrelated one-field request.
- `agent_working_copy_version_conflict`: read the current draft and reconsider the
  intended change. Preserve newer user edits; never silently replay the old patch.

Say the change is in the draft, based on the released version returned by the
receipt, and that nothing is released. The Owner can undo changed fields in
Configure. Testing uses the receipt's `test_change` through `evaluation-results.md`.
A requested resource binding is applied by this same draft writer; resource
completion alone is not that binding.

## New Agent drafts and suggestions

`agent_draft_start` establishes an owned creation draft for the stated purpose,
without generating or creating an Agent. Keep its exact returned target. When
`view_opened=true`, its editor is already open; no extra inspection is needed just
to show it after saving a suggestion.

For the admitted creation draft, call `agent_authoring_inspect` without `target`;
the server holds the original baseline. Copy explicit IDs only from actual results.
Inspect required fields before a complete initial configuration. Read exact pending
suggestions with `agent_authoring_proposal_get` before revising their content;
pending content is not the saved draft. `agent_authoring_tools` supplies eligible
resource choices without selecting or executing them.

`agent_authoring_validate` accepts a sparse patch, or an omitted/empty patch for a
check-only goal. Do not manufacture a field edit just to validate. A narrow edit
stays narrow even when an unchanged incomplete field blocks the whole candidate.
`validation_status=invalid` prevents saving a suggestion; `ok=true` only means the
check ran. `agent_authoring_propose` independently validates and saves a pending
suggestion. Owner confirmation cannot make an invalid candidate saveable. Manual
incomplete-draft saving is a separate action, not a bypass for this writer.

When `retry=new_user_turn` is returned, stop writes in that Turn. Explain the actual
changed/unavailable baseline and ask for review of the original editor and a new
send. Save local input only if it exists; a version conflict does not prove unsaved
input, and a new send does not guarantee validation or saving will pass.

Creation suggestions can replace exact `skill_grants` and `capability_ids` arrays.
Preserve unrelated bindings; Owner field adoption, final private creation and first
execution remain separate. Changed dependencies require fresh validation.

## Output and truthful completion

`output_schema` constrains the internal `AnalysisPayload`, not rendered Chat text.
Use `system_prompt` with a null schema for free-text formatting. A closed payload
schema must still allow the actual `delivery_surface` and `exec_summary` fields.
Static validation cannot prove arbitrary conditional behavior or final Chat fidelity.
Write behavior and output shape into instructions, not concrete sample amounts,
names or dates that the Agent may copy into answers.

Report the actual stage throughout the answer:

- Rejected inspection: content could not be read; validation did not run.
- Validation only: the check passed/failed; no save was submitted.
- Writer returned `proposal_invalid`: submission was attempted and rejected;
  nothing was saved, even without a separate validation call.
- Successful writer: use its effect. A creation suggestion awaits review; an
  existing-Agent write is already in the draft. Unknown stays unknown.

Do not call a writer to justify an earlier claim. A saved receipt survives a later
failure. Release is always its separate existing user action.
