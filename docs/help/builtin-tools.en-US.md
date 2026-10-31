# Built-in third-party tools

Team owners and team administrators can connect the third-party tools that Bowra supports in **Team settings → Tool Sources**. Tavily, Datarc, and Power BI are currently supported; the picker and the list of configured tools show each service's icon so you can tell them apart.

**Capability Center → Tools** lists operations from enabled connections. Unconfigured or disabled Google Workspace and Power BI connections do not appear there; Google Workspace also requires its deployment feature to be enabled. Administrators can still use **Manage Tool Sources** to configure or re-enable supported connections. Browsing the catalog does not enable a connection or authorize an external account.

## Tavily

Choose **＋ Add built-in third-party tool**, select Tavily, paste the API key, and choose **Verify and save**. Bowra verifies the key first and then stores it encrypted. After saving, you can edit the key or turn the tool on or off at any time.

Once Tavily is enabled, team Agents with web search can use it; when it is disabled, new searches no longer use Tavily. The API key is never shown to team members or Agents.

### Web search

When searching with Tavily, you can state the scope in your question and the Agent filters results accordingly, for example “only news from the past week”, “only government websites”, “exclude a particular site”, or “search more deeply”. News results include their publication date.

### Read web pages and crawl sites

If these capabilities are enabled in your deployment (you can see options such as **Extract Web Pages** under **Add Tool** for an Agent), Tavily also lets Agents read the text of several public web pages at once, list the pages on a website and then choose which to read, or follow links to fetch several pages of a site together. For example: “Read these three links and summarize them”, “What product pages does this website have?”, or “Read all the installation instructions on this documentation site.” A site crawl fetches about 20 pages by default (up to 100), can take two or three minutes, and uses more credits; if you only need a few pages, giving the links directly is faster.

To keep information from being sent to unfamiliar sites, the Agent only reads links you gave in the chat, links from search results, and pages listed or fetched within the same website. Links on those pages that point to other websites are listed but not read. Deep research likewise stays within websites you mentioned, and the Agent removes unfamiliar web addresses from the question first. Internal addresses and pages that require sign-in are outside these capabilities; the Agent opens such pages directly instead.

### Deep research

This is also available only when these capabilities are enabled in your deployment. When you need systematic external research, ask explicitly for deep research, for example “Do deep research on public news about Chinese EV makers expanding overseas over the past six months.” The Agent hands the question to Tavily's research engine and, a few minutes later, gets back a report with source links; the chat shows that the step is in progress meanwhile. Each chat turn can run at most one deep research; the more expensive mode is used only when you explicitly ask for the deepest, most thorough research.

### Turning the capabilities on

Reading web pages, crawling sites, and deep research are turned on separately for each Agent. The built-in data analysis and marketing analysis Agents can read web pages and crawl sites by default, and deep research is available by default only to the marketing analysis Agent. For Agents you create, go to **Configure → What it can use → Add…**, switch to **Add Tool**, and select **Extract Web Pages**, **Map Website**, **Crawl Website**, and **Deep Research**.

### Credits

Every search, read, crawl, and research run uses credits from the team's Tavily account: “deeper” searches and deep reads use twice as much, crawls are charged per page, and deep research uses far more than an ordinary search. When credits run out or the key becomes invalid, the Agent says in its answer that web search is temporarily unavailable and suggests contacting a team administrator. The administrator should then top up or replace the key in the Tavily console and update it on this page. A key with no remaining credits can still be saved and works again once credits are restored.

## Datarc

Choose **＋ Add built-in third-party tool → Datarc** and enter a configuration name, the Endpoint, and the connection key. The Endpoint is the root address of the Datarc service, for example `https://openapi.datarc.cn`; don't enter a specific API path. After verification succeeds, the key is stored encrypted and the list shows the connection as “Datarc - configuration name”.

A team can add several Datarc connections and assign them separately in Agent tool settings. For example, assign “Datarc - Finance” to a finance Agent and “Datarc - Sales” to a sales Agent. Renaming a connection doesn't remove existing assignments; after a connection is disabled or deleted, Agents don't switch to another connection automatically.

When you create or edit a connection, Bowra reads the key's API permissions, the workspaces it may access, and its expiry date, and shows the results in the edit window grouped by permissions, available tools, workspaces, and validity. The workspace list is collapsed by default and can be expanded; choose **Check again** to refresh the scope. An older connection provides no tools to Agents until its first successful check, after its key fails verification, or after the key expires.

Some older Datarc versions don't report key permissions. For such instances, Bowra verifies the key by reading the workspaces it can access, shows the permissions as **All permissions**, and provides all three capabilities; whether a query actually works still depends on the key's permissions in Datarc. If the connection key fails verification, check that the key is correct and not expired. If you're told the Endpoint doesn't provide the Datarc API, make sure you entered the root address of the Datarc service.

Depending on the key's permissions, Bowra provides **List workspaces**, **Inspect data schema**, and **Query data**. These are capabilities within one connection and don't need to be configured one by one in an Agent.

When a workspace name matches several choices, select one of the listed workspaces to continue the same question, including work delegated to an analyst. This workspace card uses listed choices; ordinary question cards can still accept your own text. Clear **Remember as default** to keep the choice within the current Chat. If a choice cannot be accepted, the card stays open with an explanation; choose an available option and continue. If access has changed, choose another option or ask your Team administrator to check the connection and permissions. Skipping the card does not confirm a workspace switch.

## Power BI

Choose **＋ Add built-in third-party tool → Power BI**, name the connection, and enter the Microsoft Entra Tenant ID, Client ID, and client secret. You can provide a workspace ID and semantic model ID, or leave them blank to select an accessible model automatically. Bowra verifies query access before saving. The secret is stored encrypted and is never shown again. A team can add several connections, such as separate finance and sales models.

An organization administrator must register the application in Microsoft Entra, create its client secret, allow its service principal's security group to use the Power BI API, add the service principal to the target workspace, and grant Build permission on the semantic model. If verification fails, check the tenant, workspace, model, and Build permission.

Each connection has its own DAX templates. Team owners and administrators can add or remove them from the connection card. Disabling or deleting a connection does not make an Agent switch to another one. Deleting a connection also deletes its templates.

In the Agent's **Configure → What it can use** section, select the Power BI connection it should use and save. Each connection is listed separately, and Agents can use only those assigned to them. Existing service-principal connections are migrated to Tool Sources. An older connection that used delegated authorization must be configured again with a service principal. Then select the connection in the Agent settings and save. Power BI checks permissions for the team's service principal rather than for the individual chat user.

## Assign to Agents

Open **Agent management → Created by me → Configure → What it can use**, choose **Add…** and then **Add Tool**. Team tools appear as connection cards:

- The card uses the configuration name, for example “Datarc - Finance”.
- Selecting a card once adds the whole connection.
- Expand **View included capabilities** to see which tools the connection provides.
- Search matches both connection names and capability names.
- **Manage team Tool connections** takes you to the tools page in team settings.

When you save the Agent, Bowra records the specific tools based on the connection's most recently saved check. It doesn't re-check or widen the scope on every call; the scope is refreshed from the connection only when you save the Agent again. Permissions added to the external key later are not added to existing Agents automatically, and if an API becomes unavailable on the external side, calls to it return an error.

For example, suppose the team has two connections:

```text
Datarc - Finance
  List workspaces, Inspect data schema, Query data

Datarc - Sales
  List workspaces, Query data
```

The Agent page shows just two connection cards instead of five similarly named tools. Each tool stays tied to its own connection, so the two connections never share an Endpoint or key.

When querying data, the actual scope is limited by the connection key, the current user's membership permissions in Datarc, and the tools saved with the Agent. If only one workspace is accessible, it is selected automatically; with several, the Agent asks the user to choose. If you're told no Datarc account was found, make sure your Bowra sign-in email has been created in that Datarc instance and added to the target workspace.

Connection keys are never shown to team members or Agents. Private deployments can use an internal company Endpoint; SaaS deployments accept only public HTTPS addresses.

A Datarc connection or member lookup failure does not prevent you from starting or continuing unrelated work, such as writing or sending an email. If a task needs Datarc, Bowra explains the data-access problem instead of querying without verified access. If the connection key has expired or been revoked, ask a team administrator to update it in **Team settings → Tool Sources**. Failed account synchronization after sign-in does not invalidate your Bowra login.
