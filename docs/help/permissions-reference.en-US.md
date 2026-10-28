# Permissions reference: roles and actions

Use this page to quickly determine the owner of an action. Team, space, resource, platform, and data permissions are separate checks.

## Four rules to remember

1. A team role governs team-wide administration; it does not automatically grant space membership or business-data access.
2. A space role controls collaboration in that space; it does not make someone the author of every Agent or Skill.
3. An Agent's audience and release state control who can discover and use it; editing remains with the author or a designated manager.
4. Data is queried using the current member's own data identity. Sharing a report or Agent never delegates that identity.

## Who manages what?

| Object or action                               | Primary owner                                            | Notes                                                                                      |
| ---------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Team membership, team services, approval       | Team owner or administrator                              | This does not transfer another member’s private data identity.                             |
| Space membership and space settings            | Space administrator                                      | Team administrators do not automatically join every space.                                 |
| Member-created Agent content and versions      | Agent author                                             | The author selects an audience and publishes; approval may be required.                    |
| Built-in Agent enablement and configuration    | Team owner or administrator                              | Product-defined tools and output contracts are not freely replaced.                        |
| Personal Skill editing and personal enablement | The Skill author or member                               | A personal switch only changes that member's use.                                          |
| Team Skill approval or team-wide state         | Team owner or administrator                              | A member cannot override a team-wide disablement.                                          |
| Outcome definitions                            | Product or deployment maintainer                         | Team roles configure supported features; they do not create unsupported Outcome contracts. |
| Team Plugins                                   | Team owner or administrator                              | Members can browse and select these Tool and Skill bundles for Agents they own.            |
| External business data                         | Data-system administrator and the member's data identity | Check the data source and the person's authorization separately.                           |

## Configure built-in Agents

Team owners and administrators can open **Agents → Built-in Agents**. Each row shows a localized purpose summary; choose **Configure** for that Agent to review its full routing description and team overrides. Changing the list language does not rewrite your saved configuration.

If the skill choices fail to load, choose **Retry Skills** in the dialog. Selected skills and other unsaved fields are kept. An empty catalog has its own message, and existing selections remain visible. Review the selections before saving; **Cancel** leaves the team configuration unchanged.

## Agent, Plugin, Outcome, and Skill

An **Agent** packages a work role and its dependencies. A **Plugin** groups reusable Tools and Skills for Agents and is managed by team owners and admins. An **Outcome** describes the kind of result an Agent promises and may show evidence or availability. Neither grants permission. A **Skill** is a reusable method. Saving a draft, publishing a version, approving a team release, and enabling a method for one person's session are different actions with different owners.

When an action is denied, identify the exact resource and operation, then check the team, space, resource, capability, and data boundaries in that order. See [Troubleshoot access](permissions/troubleshoot-access.en-US.md) for the diagnostic flow.
