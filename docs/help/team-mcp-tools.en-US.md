# Add a team MCP tool

Team owners and administrators can add an MCP tool in **Team settings → Tool Sources** so team Agents can use selected capabilities from an external service. You add an MCP service and selected tools from it; this does not automatically grant team members access to an account in that service.

## Add an MCP service and select tools

1. Open **Team settings → Tool Sources** and select **Add MCP tool**.
2. Select **MCP server** and enter its address. If the service requires authentication, enter credentials or complete authorization when prompted.
3. Wait for Bowra to load the tools offered by the service, then select the tools your team Agents need.
4. Enter a source name and confirm. You can then view, validate, disable, or edit the source from the tools list.

Saved credentials cannot be read back or displayed. Connect only trusted services and use least-privilege credentials. Cloud deployments require public HTTPS addresses; internal deployments may use internal addresses.

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

## Choose which tools to import

To import tools you skipped when adding the source, or tools a refresh just added, choose **Choose Tools** from the source's actions (in the **⋯** menu on the right of the card). After a refresh adds tools to a source that imports only some of them, the confirmation message also offers **Choose Tools**, with the new tools marked **New**.

The list shows every tool from the last saved tool list, with the imported ones checked. Check the tools Agents should use and select **Save**. Keep at least one tool selected; to stop using every tool, turn the source off instead.

If you uncheck tools that are currently imported, Bowra first shows the impact: the tools that will be removed and the Agents, running tasks, and schedules that use this source. Nothing changes until you select **Confirm and save**; **Back** returns to the list. After saving, Agents can't call the removed tools, and affected schedules are paused until their dependencies are confirmed again.

If the source is used by team Agents, running tasks, or schedules, review the impact and repair those dependencies before disabling or deleting it. External services can perform irreversible actions, so confirm your team's data-use requirements and the service permissions before enabling a tool.
