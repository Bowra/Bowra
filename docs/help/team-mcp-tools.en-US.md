# Add a team MCP tool

Team owners and administrators can add an MCP tool in **Capability Center → Tools → Manage tool sources** so team Agents can use selected capabilities from an external service. You add an MCP service and selected tools from it; this does not automatically grant team members access to an account in that service.

If each member should use their own account at the service instead of a shared one, members can sign in on **My Connections**; see [Connect a personal MCP service](personal-mcp.en-US.md).

## Add an MCP service and select tools

1. Open **Capability Center → Tools → Manage tool sources** and, on the **External tool sources** tab, select **Add MCP tool**.
2. Select **MCP server** and enter its address. If the service requires authentication, enter credentials or complete authorization when prompted.
3. Wait for Bowra to load the tools offered by the service, then select the tools your team Agents need.
4. Enter a source name and confirm. You can then view, validate, disable, or edit the source from the tools list.

Saved credentials cannot be read back or displayed. Connect only trusted services and use least-privilege credentials. Cloud deployments require public HTTPS addresses; internal deployments may use internal addresses.
If the service uses OAuth, sign in with an account whose accessible content can be shared with the team. Eligible members’ Agents use that account’s permissions. After authorization, select **Choose Tools** on the source, save the selection, then turn on the source. To narrow a long list, filter by category and select or clear the shown results. If the service rejects the return address, open Bowhead through a verified HTTPS public entry and authorize again.

## Manage added tools

You can validate connectivity, edit a source name or credentials, or disable each source. When a source is disabled, new calls no longer go to the service; an external call already in progress may finish. Deleting a source also removes its saved credentials and cannot be undone.

## Refresh the tool list

When an MCP service adds or retires tools, choose **Refresh Tool List** from the source's actions (in the **⋯** menu on the right of the card). Bowra first reads the tools the service offers now and lists how they differ from the saved list:

- **Added tools**: tools the service now offers. If you selected only some tools when you added the source, new tools aren't imported automatically.
- **Tools removed from the server**: tools the service no longer offers that weren't imported. Removing them doesn't affect Agents.
- **Imported tools that will stop working**: imported tools the service no longer offers. After the update, Agents can't call them. The preview also lists the affected Agents, running tasks, and schedules; affected schedules are paused until their dependencies are confirmed again. If every imported tool was removed, the update turns the source off.
- **Changed tools**: tools the service still offers whose description or parameters changed. Each one shows what changed and whether it's imported. After the update, Agents use the new version the next time they call an imported tool.

Nothing changes until you select **Update tool list**; **Cancel** leaves everything as it was. If the tool list or its usage changes while you review it, Bowra reads it again and asks you to review the new changes. When nothing was added, removed, or changed, the preview says the tool list is up to date.

An MCP service that requires authorization must be authorized before you can refresh its tool list.

## When an authorization ends

An authorized service keeps working without you: Bowhead renews its access when needed, once for the whole team even when several conversations use the service at the same time. If the service stops accepting the authorization (for example, it expired after a long time unused, someone removed Bowhead's access in the service, or the service changed its sign-in server), the source's card shows **Needs authorization** with an **Authorize** button. Calls from conversations are then not sent, and the Agent tells the user to ask a team administrator.

Select **Authorize** and sign in to the service again; the imported tools stay as they are. While the authorization is pending you can still turn the source off. **Refresh Tool List** is available again once the source is authorized.

## Choose which tools to import

To import tools you skipped when adding the source, or tools a refresh just added, choose **Choose Tools** from the source's actions (in the **⋯** menu on the right of the card). After a refresh adds tools to a source that imports only some of them, the confirmation message also offers **Choose Tools**, with the new tools marked **New**.

The list shows every tool from the last saved tool list, with the imported ones checked. Check the tools Agents should use and select **Save**. Keep at least one tool selected; to stop using every tool, turn the source off instead.

If you uncheck tools that are currently imported, Bowra first shows the impact: the tools that will be removed and the Agents, running tasks, and schedules that use this source. Nothing changes until you select **Confirm and save**; **Back** returns to the list. After saving, Agents can't call the removed tools, and affected schedules are paused until their dependencies are confirmed again.

If the source is used by team Agents, running tasks, or schedules, review the impact and repair those dependencies before disabling or deleting it. External services can perform irreversible actions, so confirm your team's data-use requirements and the service permissions before enabling a tool.

## Confirm each call that can change data

When your deployment turns on call confirmation, Bowhead asks the member who started a task before it calls an MCP tool that may change data. The confirmation appears in the conversation under the waiting step and shows the tool source, the tool, its description, whether the tool writes data, deletes data or has no known category, and the complete arguments that will be sent:

- **Allow This Call** runs this call once with the arguments shown.
- **Always Allow in This Session** also allows later calls to the same tool in the current session. It ends when the session ends or the tool's definition changes.
- **Reject and Skip** does not run the call.
- If nobody decides within three minutes, the call is skipped.

After you decide, the request stays in the conversation as a one-line record of what you decided. If you are on another page, a notice links to the conversation, and the browser tab title shows how many actions are waiting.

Tools the service marks as read-only run without asking. A tool marked both read-only and deleting data counts as unmarked. Tools the service marks as writing or deleting data can then be selected for Agents and are confirmed on every call. Tools with no marking can't be selected for Agents until an owner or administrator confirms their category; when the assistant uses them, every call is confirmed. Each call uses the account the source was connected with, so it acts as that account for every member. Scheduled tasks have nobody to confirm, so their calls to these tools are skipped.

### Confirm a tool's category

Owners and administrators can record what each tool of an MCP source does when the service marks it incorrectly or not at all:

1. Choose **Confirm Tool Categories** from the source's actions (in the **⋯** menu on the right of the card).
2. For each tool, choose **Read only**, **Writes data** or **Deletes data**. Categories below what the service marked are not offered.
3. Select **Save**. The change applies from the next task.

A tool confirmed as read-only runs without asking, and an unmarked tool with a confirmed category can be selected for Agents. If the service later changes a tool's definition, its confirmation stops applying and the list asks you to confirm it again. Confirm **Read only** only for tools that cannot change data.

## Run n8n workflows

When your deployment turns on n8n workflow tools, an MCP source that connects to n8n's instance-level MCP server (an address ending in `/mcp-server/http`) can offer single n8n workflows as tools. Each approved workflow runs its published version in n8n and waits for the result, so it runs the same way every time.

1. In n8n, publish the workflow and turn on **Available in MCP**. Workflows started by a webhook or a schedule can be approved.
2. In Bowhead, choose **n8n Workflows** from the source's actions (in the **⋯** menu on the right of the card). Workflows that aren't published or available in MCP are counted but not listed.
3. Select **Review** next to a workflow. Choose its category by what the workflow does to data, write the description the model reads, and check the input schema: the fields of the webhook request body. The draft lists the fields the published workflow reads; add types and mark the required ones.
4. Select **Approve**. The workflow becomes a tool from the next task.

The assistant can use approved workflows in any task. To give a custom Agent the workflows of one source, create a Plugin from the **n8n workflows** template under **Team settings → Plugins** and select that Plugin in the Agent.

Things to know:

- A run waits up to 5 minutes, or 15 minutes in a scheduled task. A longer run is reported as still running, with a link to the execution in n8n.
- If the workflow is published again in n8n, runs stop with "version changed" until you review and approve the new version.
- Read-only workflows run without asking, also in scheduled tasks. Workflows that write or delete data ask the member who started the task on every run, and need call confirmation to be turned on.
- Workflows run as the n8n account the source was authorized with. When n8n asks, grant only reading workflows, running workflows and reading executions.
- You don't need to import n8n's own tools for approved workflows to run. Importing `execute_workflow` lets the assistant run any workflow that is available in MCP without your approval, so leave it unselected.
- n8n limits MCP requests from one address (100 per 5 minutes by default). Bowhead stays below that limit; an n8n administrator can raise it with `N8N_MCP_SERVER_RATE_LIMIT`.
