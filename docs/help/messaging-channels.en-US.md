# Connect a team messaging channel

Owners and administrators start from **Team settings → Messaging channels** and open a channel’s setup page. Follow the instructions shown for the current deployment. Configure an address external services can reach when the channel requires a callback. [Configure public access addresses](public-entries.en-US.md)

**Connected** lists channels that are enabled and running. Under **Available to connect**, start or continue setup or check an enabled channel that needs attention. **Coming soon** lists channels whose setup is not yet available.

- **Feishu.** Save the app credentials and choose the supported event connection method. Follow the app console setup and the checks in the Feishu page. Complete its release checks before inviting members to use the bot. Each member confirms their own Bowhead account from the binding card the bot sends when they first message it.
- **Slack.** Configure the Slack app, register the team bot callback and install the bot in the intended Slack workspace. Test a direct message or mention Bowhead in a thread as allowed by your setup. If you also enable personal Slack MCP, follow the separate member authorization instructions.
- **Microsoft Teams.** Follow the Azure Bot setup, register the full external HTTPS messaging endpoint including the deployment prefix, and publish the app for personal chats in your tenant.

After setup, test from an eligible member account and check the setup or error status for the specific channel. The integration routes to a Bowhead Team and uses the member’s available access.

For data questions, check that your linked account can access the intended Datarc workspace. If a clarification directs you to Bowhead Web, open the original conversation there to continue.

## Using Teams personal chat

1. Send the bot a message. If prompted, sign in to Teams. Open **Connect Teams**, sign in to Bowhead with your matching work account, and confirm. The link lasts ten minutes. Members previously matched by email confirm once after the upgrade. Return to Teams and send your question.
2. Select the Space for your question. Send `/space` to change it, then send your next question. Each Space keeps its own conversation. If a result is waiting in your previous Space, switch back to receive it.
3. When a question card offers **Submit and continue**, select the choices or enter the requested text, then submit. The answer continues the original task. If you already answered on the web, that answer stands. Cards that direct you to Bowhead Web can be completed in the original conversation there.
