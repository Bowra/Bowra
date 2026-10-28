# Connect a personal MCP service

Connect a remote MCP service to let an Agent you own use external tools when you run it. The connection is visible only to you. Team owners and administrators cannot view, configure, or approve its calls.

## Add a connection and select tools

1. Open **Capability Center → My Connections** (`/connections/`).
2. Enter a name and remote MCP address. Choose no authentication, Bearer token, or API key; an API key can use the header name required by the service.
3. Select **Connect and discover tools**, select the tools you need, and save.

Saved credentials cannot be read back. Leave credentials blank while editing to retain them; enter new credentials when changing the authentication method. Bowra supports remote HTTP(S) MCP services, not local commands. Cloud deployments require public HTTPS; internal deployments may use internal addresses. Use trusted services and least-privilege credentials.

## Connect your Slack

When the team administrator has enabled Slack and the deployment has made this capability available, **Connect my Slack** appears in **My Connections**. Complete authorization with your own Slack account. The connection and token belong only to you; team owners and administrators cannot view or use them.

After returning to **My Connections**, the page explains whether the connection succeeded and, on failure, what to check or retry. If it still fails, ask a team administrator to complete the final **Enable personal Slack MCP** step in **Team settings → Integrations → Slack**. It must include the personal OAuth callback URL and required User Token Scopes; Slack may require the app to be reinstalled after scopes are added.

After connecting, select the Slack tools you need and bind them to an Agent you own. This connection is read-only: it can search and read messages, channels, files, and user information, but cannot send messages, create channels, or write Canvases.

## Bind tools to an Agent you own

In **My Connections → My Agent tools**, or in the Agent's tool area, select tools and save the binding. The Agent must have a published version that you own in the current team. You can share the Agent with selected people or the team without removing its personal tool binding. Only your own runs include those tools; other members' runs do not include them and cannot use your connection. When a new version changes the Agent definition, select the personal tools again. A new version that only changes the audience keeps the binding.

## Approve every call

Remote calls require confirmation by default. Review the Agent, connection, tool, operation summary, and full parameters, especially data that will leave Bowra.

- **Allow this call** permits one execution with the current parameters.
- **Always allow in this session** permits later calls to the same tool only in this session; it expires when the session ends, refreshes, or the connection is disabled.
- **Deny and skip** does not run the call.
- Calls expire after two minutes without a decision.

External services can make irreversible changes. Personal ownership does not permit sending team-sensitive data outside the team policy.

## Repair, disable, or delete

Use **Edit connection / Refresh tools** to repair authentication or rediscover tools. Refreshing clears tool selections and requires new confirmation of affected bindings. Disabling or deleting blocks future calls; a started call may finish. Deletion clears credentials but retains minimal call records; before you confirm, the page lists your agents still bound to that connection's tools. Verify an unknown external outcome in the external service before approving the same action again.
