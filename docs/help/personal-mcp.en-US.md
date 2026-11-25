# Connect a personal MCP service

Connect a remote MCP service to let an Agent you own use external tools when you run it. The connection is visible only to you. Team owners and administrators cannot view, configure, or approve its calls.

## Add a connection and select tools

1. Open **Capability Center → My Connections** (`/connections/`).
2. Enter a name and remote MCP address. Choose no authentication, Bearer token, or API key; an API key can use the header name required by the service.
3. Select **Connect and discover tools**, select the tools you need, and save.

Saved credentials cannot be read back. Leave credentials blank while editing to retain them; enter new credentials when changing the authentication method. Bowra supports remote HTTP(S) MCP services, not local commands. Cloud deployments require public HTTPS; internal deployments may use internal addresses. Use trusted services and least-privilege credentials.

## Sign in to a service with your own account

Some MCP services ask you to sign in with your own account instead of giving you a token, for example Notion, Linear, Sentry or n8n. When your deployment offers it, choose **Sign in to the service** under **Authentication**. If you leave **No authentication** and the service asks for a sign-in, the dialog switches to it and names where you will sign in.

1. Enter a name and the remote MCP address, then choose **Continue to Sign In**.
2. Sign in at the service and approve the access it asks for. Some services let you narrow what Bowhead may do (for example read only); the tools that work later follow that choice.
3. Back in **My Connections**, select the tools you need and save. The connection shows where you signed in.

Bowhead keeps your sign-in for this connection only and renews it when needed; it survives restarts and is never shown to anyone. If the service ends the sign-in (for example after a long time unused, or because Bowhead's access was removed there), the connection shows that your sign-in has ended: choose **Sign In Again**. Your tool selections and Agent bindings are kept; tools whose definition changed need to be selected again. Until then your Agents keep running with their other tools, and a call to this connection's tools is not sent: the Agent tells you to sign in again.

A sign-in that was cancelled or not finished shows **Sign-in not finished** with **Continue Sign-in**. Editing a signed-in connection changes its name only; to use another address, add a new connection. Deleting it makes Bowhead forget your sign-in; to remove Bowhead's access at the service too, remove it from the service's connected apps.

Some services only accept apps registered by their administrator (for example GitHub, Google and Microsoft). Bowhead cannot sign in to those yet; use a personal token or API key if the service offers one.

| Situation                                                           | Where to connect                        | Whose account the service sees                  |
| ------------------------------------------------------------------- | --------------------------------------- | ----------------------------------------------- |
| The team shares one service account managed by an administrator     | Team settings → Tool Sources            | The administrator's sign-in, shared by the team |
| Each person should act as themselves and the service offers sign-in | My Connections → Sign in to the service | Your own                                        |
| The service gives you a personal token or API key                   | My Connections → Bearer Token / API Key | Your own                                        |
| Slack                                                               | My Connections → Connect My Slack       | Your own, through the team's Slack app          |

## Connect your Slack

When the team administrator has enabled Slack and the deployment has made this capability available, **Connect my Slack** appears in **My Connections**. Complete authorization with your own Slack account. The connection and token belong only to you; team owners and administrators cannot view or use them.

After returning to **My Connections**, the page explains whether the connection succeeded and, on failure, what to check or retry. If it still fails, ask a team administrator to complete the final **Enable personal Slack MCP** step in **Team settings → Integrations → Slack**. It must include the personal OAuth callback URL and required User Token Scopes; Slack may require the app to be reinstalled after scopes are added.

After connecting, select the Slack tools you need and bind them to an Agent you own. This connection is read-only: it can search and read messages, channels, files, and user information, but cannot send messages, create channels, or write Canvases.

## Bind tools to an Agent you own

If there are no tools in **My Agent tools**, add a connection or manage an existing one and enable the tools you need. Then expand the Agent you want to configure.

In **My Connections → My Agent tools**, or in the Agent's tool area, select tools and save the binding. The Agent must have a published version that you own in the current team. You can share the Agent with selected people or the team without removing its personal tool binding. Only your own runs include those tools; other members' runs do not include them and cannot use your connection. When a new version changes the Agent definition, select the personal tools again. A new version that only changes the audience keeps the binding.

## Approve every call

Remote calls require confirmation by default. The request appears in the conversation under the waiting step. Review the Agent, connection, tool, operation summary, and full parameters, especially data that will leave Bowra.

- **Allow this call** permits one execution with the current parameters.
- **Always allow in this session** permits later calls to the same tool only in this session; it expires when the session ends, refreshes, or the connection is disabled.
- **Deny and skip** does not run the call.
- Calls expire after three minutes without a decision.

After you decide, the request stays in the conversation as a one-line record of what you decided. If you are on another page, a notice links to the conversation, and the browser tab title shows how many actions are waiting.

External services can make irreversible changes. Personal ownership does not permit sending team-sensitive data outside the team policy.

## Repair, disable, or delete

Use **Edit connection / Refresh tools** to repair authentication or rediscover tools. When the service rejects a connection's credential, the connection shows that its authentication expired; your Agents keep running with their other tools, and a call to this connection's tools is not sent: the Agent tells you to update the credential first. Refreshing clears tool selections and requires new confirmation of affected bindings. Disabling or deleting blocks future calls; a started call may finish. Deletion clears credentials but retains minimal call records; before you confirm, the page lists your agents still bound to that connection's tools. Verify an unknown external outcome in the external service before approving the same action again.
