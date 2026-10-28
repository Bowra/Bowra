# Use team Plugins

A Plugin is a reusable selection of Tools and Skills. Team admins can give it a name and description, then choose from available resources. For example, a “Research” Plugin might combine web search tools with a research Skill.

Built-in names default to English and use the available Chinese labels when your interface language is Chinese. Names you give your own Tools, Skills, Plugins, and Agents stay as you entered them.

## Browse and use

Members can open **Capability Center → Plugins** to see enabled Plugins. **Built-in Plugins** are Tool bundles that ship with Bowra, such as Data analysis, Reports and canvas, and Web research; each name shows its version. You can choose built-in Plugins for your own Agents, including Email and Google delivery. Settings Plugins (appearance, answer language and similar) are used only by the system settings assistant. You can turn them off like other Plugins; the settings assistant then no longer changes that kind of setting for you. You cannot select them for your own Agents.

**Team Plugins** shows the team's enabled selections and their contents; the section appears only when your team has team Plugins. When creating or editing your own Agent, open **What it can use**, select a team Plugin, and save. You can also select individual Tools and Skills.

Agents use the latest contents on their next run. A run already in progress keeps the selection it started with. If a Plugin is turned off, Agents that use it keep running without the Tools and Skills it provided; Tools you selected individually are not affected.

A Plugin helps select resources. Whether a Tool or Skill can actually run still depends on the current user, Agent, and resource permissions and availability.

## Turn off Plugins and built-in Tools

- **Plugins**: each Plugin has a **Me** switch that applies only to you; the notice at the bottom lets you undo it. Team owners and team admins also see a **Team** switch. Before a team-wide change takes effect, Bowra lists the affected Agents (including built-in Agents) and scheduled tasks; the change applies from their next run. When the team turns a Plugin off, members cannot turn it back on for themselves.
- **Built-in Tools**: **Capability Center → Tools** groups built-in Tools by purpose. Team owners and team admins can turn each one on or off. Turning a Tool off removes it from every Agent, including Agents that get it through a Plugin. Turning on a Tool that writes or sends content outside Bowra, such as Send Email or writing to Google Docs, asks you to confirm the risk first. Select **Details** on a Tool to see what it does and, for owners and admins, which Agents and scheduled tasks use it. There is no **Always confirm** setting: once access is granted, nothing asks again.
- Turning something off never changes an Agent's configuration; turning it back on restores it automatically.

## Manage Plugins

Team owners and admins first see the existing list by choosing **Manage Team Plugins** in **Capability Center → Plugins**. This manager includes disabled team Plugins so administrators can enable them again. If there are no team Plugins yet, the page shows an empty state; select **Create Plugin** in the upper right to add one. Select a Plugin name to edit it, or select **Cancel** to return to the list. Enter a name and description, select Tools and Skills, then save.

Tools are separated into **Added Tools** and **System Tools**. Added Tools include enabled built-in third-party connections and team Tool Sources. Each operation shows its connection, so you can select only the ones the Plugin needs. Disabled or unavailable operations cannot be added. If a Tool already in a Plugin becomes unavailable, clear its selection while editing before saving.

Editing an existing Plugin updates the resources used by Agents that selected it on their next run. Before disabling one, tell affected Agent owners to update their configuration.

To remove a Plugin permanently, select **Delete** in its list entry, review the number of affected Agents, and confirm. Its Tools and Skills are removed from those Agents' future runs immediately; runs already in progress keep what they started with. Agent drafts also lose the selection. Historical versions remain viewable, but restoring an old version does not reselect a deleted Plugin.

An **Outcome** describes a result an Agent promises users, such as “write a report.” It is separate from a Plugin resource selection.
