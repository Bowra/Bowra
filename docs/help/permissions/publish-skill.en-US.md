# Publish a team Skill

A Skill is a reusable method. Personal editing, team submission, approval, and each member's enablement are different actions.

## Use a personal Skill or configure it for an Agent

Open **Skills and workflows → My Skills** to manage your personal version. Once enabled with no pending import issues, it can be read as needed by built-in execution Agents in your ordinary conversations; you can also ask for it by name. Saving or enabling it does not mean it will be selected on every task, and reading its instructions does not enable script execution.

Custom Agents need a separate Skill configuration, directly, through a selected Plugin, or as an allowed read-only method the user selects for a task. A personal Skill requires the Agent to be set to **Only me**. To configure it for an Agent shared with colleagues, submit it to the team using the steps below. Once approved, enabled and available to the entire team, choose the team version in the Agent's configuration, then save and apply or publish the Agent version. Team approval does not add the Skill to Agents automatically.

## First submission

1. Open **My Skills** and finish editing and saving your personal version.
2. Choose **Submit to team** when the entry is available.
3. Review the name, description, instructions, dependencies, and intended use, then submit.
4. Where approval is required, a team owner or administrator reviews the submission in the team workflow.

After approval, members can discover the team version. A team-wide availability setting and each person's personal enablement still affect whether it can be used in a session.

Reading a Skill on its own does not grant permission to run its scripts; adding a Skill with scripts to an Agent lets that Agent run them in the sandbox (see [Create an Agent](../create-agent.en-US.md#understand-which-skills-your-agent-can-use)). If it is disabled, its execution permission is revoked, or its import needs resolution while inputs are being prepared, execution stops before starting. Correct the availability or permission issue before trying again; disabling a Skill is not a cancellation control for an already running script.

## Update a Skill

Edit your published Skill, save, and submit the update. Until the update is approved and released, colleagues normally continue using the existing published version. Do not assume saving an author draft changes the team's version.

After the update is approved, Agents that use the Skill pick up the new version at their next run; their owners see that a Skill the Agent uses was updated, with a recommendation to evaluate the Agent again. If the team version itself is edited directly and returned to review, Agents that include it can't use it, and can't be saved or released with it, until a team owner or administrator approves it again.

## Rejection, cancellation, and direct administration

Read rejection feedback, revise the personal version, and submit again when ready. Cancel a pending submission only when the workflow offers that action. An administrator's direct team edit changes the team version; coordinate with the original author so future updates do not overwrite each other.

If **Submit to team** is absent, check that this is your saved personal version and that the team feature is available. A Skill's usage list shows only Agents and resources you are allowed to view; viewing it grants no edit, disable, or delete permission.

Related: [Publish an Agent](publish-agent.en-US.md) · [Roles and actions reference](../permissions-reference.en-US.md)

When saving instructions or managing files, the editor checks that the package still matches the version you opened. If another window or Coach has saved it, your local text stays in the editor. If a save reports that the Skill changed, copy the text you want to keep and reopen its current files before submitting another edit. A conflicting save does not establish which version is current; keep your unsaved text until you have compared it.

Saving content and completing its import check are separate results. If a check fails or becomes stale, an existing pending review stays pending. Retry inspection of the current files; a failed check does not establish that earlier blockers were resolved.

When Coach Skill authoring is available, you can ask in Chat to create or edit your personal Skill. Describe the files or behavior to change. Coach preserves the other package files and your enabled preference. A successful save changes your personal version directly; import inspection and a business test are separate. If saving is interrupted, ask to recover the original operation in the same chat before requesting another edit. A conflict requires comparing the current files. Team submission and approval still follow the steps above.

When Chat reports a saved Skill, also read its inspection notes. A yellow note such as missing trigger examples may need improvement; a blocker or an incomplete check needs resolution before relying on the Skill. Saved and enabled does not mean automatic selection or business results have been tested. If the Skill changed after the reported save, inspect its current version before continuing. A request for a draft only leaves your saved Skill unchanged.
