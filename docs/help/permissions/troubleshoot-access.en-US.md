# Troubleshoot access

When an entry point is missing, a hired Agent cannot run, or a data query is denied, start with the observed symptom and contact the owner of that boundary.

## Confirm these three things first

1. Verify the current account, team, and space.
2. Identify the failed action: discovering a resource, editing, publishing, or running a task.
3. Record the exact message, resource name, and time. Do not send passwords, secrets, or full business data in a support request.

## An Agent is visible but cannot run

Open **My hired Agents**. Re-enable an Agent marked disabled. If access was revoked, ask the author to check its audience and an administrator to check team and space membership. If it is hired but unavailable, inspect the availability notice; the author checks the release and dependencies, while a team administrator checks team connections. Retry in an approved space after the issue is corrected.

## An Agent runs but cannot query Datarc data

1. A team owner or administrator confirms an enabled Datarc connection in **Team settings → Data sources**.
2. The Datarc integration owner confirms that your Bowra user has a valid data identity. Never use another person's account as proof of access.
3. If prompted, choose a Datarc workspace from the list available to your identity. If it is absent, the data-permission owner must update that identity's access.
4. Retry the original query in the correct space and verify the data scope. An empty result can still be a filter, time-range, or data-quality issue.

The Bowra space and a Datarc workspace are different objects. Access to one does not imply access to the other.

A Datarc access problem does not prevent separately authorized Power BI queries, uploaded-file analysis or calculations. Name the source you want to use in your request. A Power BI-only task does not require choosing a Datarc workspace, and file analysis uses your uploaded data rather than silently replacing it with database results.

## Cannot edit or publish

Check that you are the resource author, using the personal version rather than a team detail page, and that the change is saved. A pending approval means colleagues correctly continue using the old version. A team owner or administrator handles approvals and team-wide configuration; a deployment maintainer handles unavailable features or deployment issues.

## A report is visible but attachments or source data are not

Check the report, attachment, and source-data permissions independently. A shared report may contain its generated content without granting the author's private conversation, files, or data identity. Ask the file or report owner about attachments and the data-permission owner about a new query.

If a policy rule appears saved but a call still occurs, confirm the rule's resource and action, then ask the deployment maintainer whether enforcement mode is enabled. Verify the corrected action using the restricted identity.
