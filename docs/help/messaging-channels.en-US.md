# Connect a team messaging channel

Owners and administrators start from **Team settings → Messaging channels** and open a channel’s setup page. Follow the instructions shown for the current deployment. Configure an address external services can reach when the channel requires a callback. [Configure public access addresses](public-entries.en-US.md)

**Connected** lists channels that are enabled and running. Under **Available to connect**, start or continue setup or check an enabled channel that needs attention. **Coming soon** lists channels whose setup is not yet available.

- **Feishu.** Save the app credentials and choose the supported event connection method. Follow the app console setup and the checks in the Feishu page. Complete its release checks before inviting members to use the bot. Each member confirms their own Bowhead account from the binding card the bot sends when they first message it.
- **Slack.** Configure the Slack app, register the team bot callback and install the bot in the intended Slack workspace. Test a direct message or mention Bowhead in a thread as allowed by your setup. If you also enable personal Slack MCP, follow the separate member authorization instructions.
- **Microsoft Teams.** Follow the Azure bot and app publishing instructions. Set the bot and single sign-on credentials where offered. Verify a personal chat in the configured tenant. Bowhead uses a matching Team member email or the Teams sign-in flow to identify the member.

After setup, test from an eligible member account and check the setup or error status for the specific channel. The integration routes to a Bowhead Team and uses the member’s available access.
