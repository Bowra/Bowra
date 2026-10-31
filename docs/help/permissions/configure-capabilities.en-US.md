# Configure Agent capabilities

In **Capability Center**, open **Tools** or **Team Plugins** to browse their catalogs. Eligible team administrators can choose **Manage Tool Sources** or **Manage Team Plugins** on that page. Use the return link to go back to the catalog; the same resource stays selected in the sidebar. Management opened from the overview's **Team Management** view returns to that view instead. Browsing does not grant execution access.

Use this guide when a team needs to enable a built-in subagent or configure its supported behavior. Member-created Agents are maintained and published by their authors.

## Draft a text-only assistant with AI

When AI draft review is available, explain that the assistant should work only with text you provide, without data queries or web access.

1. Review its purpose and instructions.
2. Check that it needs no tools or skills, then confirm the supplied-text-only scope.
3. Choose **Create agent**. If you edit the draft, save and review it, then confirm again.

If the task needs data queries, files, or scripts and no suitable tool is available, configure the required resources or use manual setup. An empty tool list does not make those tasks possible. If text-only AI drafting is unavailable, follow the manual options offered by your page.

## Create an assistant that works only with supplied text

If manual creation says you can leave tools and skills empty, you can create an assistant for summaries, translation, or text editing:

1. Enter a name, routing description, and work instructions that limit it to content you provide.
2. Leave the tool selection empty and continue to configuration. Skills can also remain empty.
3. Review the configuration and choose **Create agent**. New Agents are private by default.

If the hint is unavailable, follow the requirements shown on your page. Without configured tools, the assistant cannot query data, access external services, or run scripts. Add resources you are authorized to use when those capabilities are needed. Updates to existing Agents continue through the save and publish flow offered by their management page.

## Enable or disable a built-in subagent

Team owners and administrators open **Agents → Manage Built-in Agents**, find the target row, and use its enablement switch. Read any impact confirmation before submitting. Wait for the row to update and check its **Enabled** or **Disabled** label. The list also shows which Agents follow **System defaults** and which are **Customized**.

This page lists only the Agents that ship with the product; manage Agents you created, that were shared with you, or that came from elsewhere on their own management pages. Some Agents provide key team capabilities (for example, the only Agent that can write canvas reports), so disabling them first explains the impact. Disabling affects only the current team, and you can enable the Agent again at any time.

The list includes **Coach** and the **Configuration Agent**. For these two you can only turn them on or off and change their name and description. The configuration dialog doesn't show the other fields, because their model, Skills and instructions are set by the product. A built-in Agent that is turned off no longer appears in the Agent marketplace or the Chat mention list, and the system doesn't hand new work to it. If you try to change a field an Agent doesn't allow, saving fails and the message names that field.

If the configuration version has changed in the meantime, the page reads the latest configuration and applies your enable or disable action again, so other settings saved a moment ago are not overwritten. If the version changes again right away, the page says the configuration was updated; try again.

## Set run limits

Agent creation and configuration use these Team limits; they do not have a separate step setting.

Team owners and Team admins set limits for every agent in the Team, built-in and custom, under **Team settings › Agents › Run limits**:

- **Turn time limit**: how long one conversation turn may run. The default is 30 minutes; you can set 10 minutes to 10 hours. Helpers stop 5 minutes before it so Lead can write the answer.
- **Single model call time**: defaults to **Follow turn time**, using the Agent's remaining time. Choose **At most** for a shorter ceiling of 30–36,000 seconds. It cannot extend the turn, and the model connection's own timeout still applies.
- **Step limit per task**: how many steps one agent may take on one task. The Team default is **Unlimited**. When an effective deployment policy is displayed, the shorter finite Team/deployment ceiling applies to Lead and each helper separately. A step is one model decision; several tool calls in that decision do not count as several model steps. The displayed local stall interval protects waits with no active operation or progress; model and tool waits have their own timeouts.
- **Tool calls per turn**: total tool calls by all agents in one turn. The default is 300; every tool counts the same.

1. Open **Team settings › Agents** and choose the **Run limits** tab.
2. Change the values you need. A value out of range shows the allowed range under the field, and Save stays unavailable.
3. Choose **Save**. The limits apply from the next turn; turns already running are not affected. To go back to the defaults, choose **Restore defaults**.

When a limit is reached, the conversation shows the specific reason and retains known results. A finite step stop also shows used/allowed decisions. Review those results before narrowing the remaining request. Use Continue only when the conversation offers a supported continuation; it is not available for every stop. Restoring Team defaults does not remove a displayed deployment ceiling. Higher limits let a task do more, but you wait longer and use more quota.

## Set sandbox and HTML limits

On **Team settings › Agents › Run limits**, Team owners and admins can also configure:

- **Sandbox computation**: default execution time (120 seconds initially) and maximum execution time (600 seconds initially). The default cannot exceed the maximum. The displayed deployment ceiling is a hard boundary; increasing it requires the deployment maintainer.
- **HTML reports**: maximum size of each embedded HTML file, including inline images and data. The initial value is 25 MiB; the maximum is 50 MiB. Larger files and complex tables need more browser memory and processing time. Existing saved Team values and deployment defaults still apply: raise this field and save if an older limit blocks a report. This controls the whole file, not the model’s token output.

The page shows the deployment defaults and allowed ranges. Edit the values and choose **Save sandbox and HTML limits**. To inherit the deployment defaults again, choose **Use deployment defaults**, review the values, then save. Task runtime and sandbox/HTML changes are saved separately. If another administrator saved first, your edits stay visible; discard and reload the current settings before editing again.

Sandbox changes apply to new tasks; running tasks keep their initial limits. HTML previews use the report’s Team setting the next time they load. Raising a duration helps slow computations, but does not fix an unavailable executor. An unknown execution result should be checked before retrying.

## Configure responsibility and automatic Skills

1. Choose **Configure** for the target built-in subagent. The form groups settings into **Name and Purpose**, **Execution**, and **Team Guidance**; **Cancel** and **Save** remain visible while you scroll.
2. Update the display name, routing description, or team guidance where supported.
3. Choose an available primary responsibility and any Skills the Agent should read automatically.
4. If needed, adjust **Model tier** (Analysis, Writing, Data modeling, or Light tasks; it affects only this built-in Agent). Steps, time and tool calls are set for all agents on the **Run limits** tab; see the next section.
5. Save, reopen the configuration, and confirm the values.
6. Test an in-scope task with an account that has access to the target space and data.

To undo the team's changes to a built-in Agent, open **⋯ → Reset to default** on its customized row. To check an Agent's identifier and model without changing anything, open **⋯ → Details** on its row; this is available to everyone who can see the list.

If saving fails: when the configuration version has changed, retry on the latest configuration; when the team configuration isn't ready or is unavailable, ask a system administrator to check the service upgrade status and retry once it has recovered; when the built-in Agent has been removed, its row disappears from the list and there is nothing more to do. If the page can't reliably read the team settings, it doesn't allow changes, so product defaults are never mistaken for the team's current configuration.

Responsibility and Skill choices help the system select an Agent. They do not grant members data permissions or guarantee an external tool is configured. Product-defined tools, output contracts, and peer calls cannot be arbitrarily replaced in this configuration.

## Find your saved workflows

Open **Capability Center → Skills and Workflows → Mine** to find workflows saved for yourself. The personal-workflow link on the team workflow page opens this same filtered list. Your switches affect your own sessions; team administration is a separate action.

## Choose a document Skill

Under **Documents & export**, the built-in Skills are separated by purpose:

| Group                      | Use it for                                                                                              |
| -------------------------- | ------------------------------------------------------------------------------------------------------- |
| Editing and writing guides | Methods for reading, editing and writing files; actual operations depend on the available capabilities. |
| Create basic documents     | New text, list and table files, not edits to your uploaded templates.                                   |
| Export existing reports    | Download a report already on the Canvas as Word, PDF, PowerPoint or Excel.                              |

The group does not grant extra access or enable missing functionality. Your own and team Skills keep their existing groups and names.

In the Trace, **Create New Presentation** means a new basic editable PPTX is being created from supplied content. Exporting an existing Canvas report is a separate operation. Community does not provide the private PowerPoint template-draft Plugin.

## Find the owner for other enablement

| Need                                             | Owner                                                                                                            |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| Team model, search, or image service             | Team owner or administrator in **Team settings → Models**                                                        |
| External tool source or connection               | Team owner or administrator in **Capability Center → Tools → Manage Tool Sources**                               |
| Datarc connection                                | Team owner or administrator in **Team settings → Data sources**; members still need their own data authorization |
| A member-created Agent's instructions or Skills  | That Agent's author                                                                                              |
| A platform capability not offered by the product | Product maintainer                                                                                               |

An Outcome describes the result an Agent promises and may show evidence or availability. A team Plugin groups Tools and Skills for Agent authors to select. Neither replaces feature, tool, or data authorization.
