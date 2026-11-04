# Grant space access

Use this guide when a colleague needs to collaborate in a space. You need permission to manage the target space; being a team administrator does not by itself make you its space administrator.

## Before you start

Confirm the correct team and space, the colleague's account, and the work they need to do. Choose the least powerful role that covers that work. Adding someone to a space does not grant them access to external business data or private resources.

## Add a member

1. Open the target space and choose **Space settings → Members**.
2. Select **Add member**, find the colleague, and choose a role.
3. Save the change.
4. Ask the colleague to refresh, select the space, and confirm that the intended entry points are visible.

Use **Collaborator** for ordinary work, **Viewer** for read-only review, and **Space administrator** only for someone who must manage membership or supported space settings.

## Change or remove access

Open the member list, select the existing member, change the role or remove access, then save and reopen the list to confirm. Removal ends future access through this space. It does not delete the person's own work or automatically revoke unrelated team, resource, or data permissions.

## Manage spaces in team settings

Team owners and administrators can create spaces in **Team settings → Spaces** and see each space's member count, administrator count, and status. Content inside a space is still available only to members with the matching space access.

- With many spaces, search by name or internal ID, filter by all, active, or archived, and page through the list at the bottom. **Per page** can show 20, 50, or 100 spaces; changing the search, status, or page size returns to the first page.
- If the list fails to load, the page shows an error and a **Retry** button. A failed search doesn't mean no space matches; choose **Search** again to retry with the same conditions.
- From a space's row you can manage its administrators, archive it, or restore it. Archiving runs immediately and shows **Undo** at the top of the screen, which restores the space right away. Archiving pauses new work and member changes in the space, and also pauses its scheduled tasks. When restoring a space that has no active space administrator, you must first choose an active team member; restoring turns the scheduled tasks that archiving paused back on. They follow their schedule from the moment of restoring, and runs missed while the space was archived are not made up; tasks a member paused themselves stay paused. If the task owner already has the maximum number of active scheduled tasks, that task stays paused and shows why.

If you do not see the member-management entry, ask a current space administrator. If the colleague can enter the space but cannot use an Agent or query data, follow [Troubleshoot access](troubleshoot-access.en-US.md).

## Handle a space assignment notification

The notification names the member requesting a space, using their account name when no display name is set. Team owners and administrators can click it to open that member's space permissions in **Team settings → Members**, including when the member is on another list page. Older notifications also use this destination after upgrading.

Check the member's identity and existing permissions, choose **Add space**, search for the target space, select a role, and choose **Grant**. Opening the notification does not grant access. After a successful grant, the pending notification is resolved and the requester receives a space-assigned notification. Ask them to refresh their space list. If the notification came from another team, switch to that team and reopen it; the grant dialog stays closed in a different team. If the member has left the team or you no longer have management permission, check the current team and membership before continuing.

## Leave a space yourself

Open the space switcher at the top and choose **More Space Settings → Leave Current Space…**. Check the space name and consequences, then confirm **Leave Current Space**. Follow any unsaved-content prompt before continuing; canceling does not remove your access.

Members, viewers, and administrators with another active administrator can leave. The last active administrator sees a disabled action and an explanation. Assign another active team member as administrator in **Space Access** before leaving. The server checks again when you confirm, so simultaneous departures cannot leave the space without an administrator.

After leaving, you return home and switch to another available space. If none remain, the existing no-space page lets you request access yourself; leaving does not send a request automatically. Your team account and existing space content remain. Rejoining requires an administrator to grant access again.

The submenu shows the current space name. Space administrators can also open **General**, **AI Work Instructions**, and **Space Access** there; ordinary members and viewers do not see these management links. If eligibility cannot be checked, choose **Retry Check** or refresh the space list. For an archived space, ask an administrator to check its status.
