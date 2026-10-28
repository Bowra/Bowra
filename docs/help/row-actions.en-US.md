# More actions in lists

Use the **⋯** menu at the end of a row or card for actions such as edit, download, remove, or delete. Rows without available actions do not show a menu.

## Open an action

1. Find the “⋯” at the end of the row you want. On a computer, the “⋯” in table rows (for example team members, members under Space Access, and Access in the Administration Console) appears when you hover over the row or move to it with the keyboard; in other lists it is always shown. On phones and tablets the “⋯” is always shown. On cards (such as public access entries, OpenAPI keys, and model connections), the “⋯” is in the top-right corner and always shown.
2. Select “⋯” and choose the action you want.
3. Red actions at the bottom of the menu, below a divider, delete content, remove access, or immediately invalidate credentials that are in use (such as delete, remove, revoke, regenerate a key, or disable a public access entry). Their names end in “…”. Each asks for confirmation first, and the confirmation explains what will happen; choosing **Cancel** or pressing Esc changes nothing. Deletions that can be undone (**Move to Trash** for a memory, **Delete** on the Files page) don't end in “…” and don't ask for confirmation; you get **Undo** afterwards. Actions you can reverse at any time, such as disabling or enabling an OpenAPI key, are not red and don't ask for confirmation; archiving a space is not red either: it runs immediately and offers **Undo**; see [Mobile dialogs and Undo](mobile-dialogs-undo.en-US.md).

State controls, such as enable switches, and primary actions, such as opening a report, can remain directly on a row or card.

## Common examples

- Team settings: members and invitations, spaces, model and data connections, tools, built-in Agents, workflows and scheduled tasks.
- Space settings: members and Space Instructions.
- Personal settings: signed-in devices, memories and recently cleared items.
- Library and reports: files, extraction tables, report links and deletion.
- Agents and Skills: supported sharing, management and personal enablement actions.

Your permissions and the item's state determine the available actions. Seeing a resource does not grant permission to modify it.

## Phones and destructive actions

On a phone, management tables become compact rows with the name, useful details and state. Larger action menus may open from the bottom of the screen. Credit rows open the member's usage details when available; Space Access rows show the person's identity and role.

Irreversible actions ask for confirmation. Supported reversible actions, such as moving a memory to trash or archiving a space, can offer **Undo** after completion. A disabled item explains the reason, for example that the only space administrator cannot remove themselves. Do not retry an action while its progress indicator is active.

## Notes

- On a phone, a “⋯” menu with more than 6 actions (for example the team member menu an owner sees) opens from the bottom of the screen; others open next to the button. For general menu behavior, see [Use menus](menus.en-US.md).
- A gray item you can't select can't be used right now, and the reason is shown under it, for example that the owner's role can only change through an ownership transfer, that you can't remove yourself as the only administrator of a space, or that another action on the page hasn't finished yet.
- While revoking another signed-in device or restoring a recent clear, the row's “⋯” shows that the action is in progress, and returns to normal when it finishes.
- **Approve** and **Reject** in the approvals and feedback inboxes are outcomes, so they still appear side by side in the row.
- Revoking or regenerating an OpenAPI key, removing a DAX template, removing a member from a group, and revoking a resource grant ask for confirmation. After regeneration the old key stops working immediately, so update the key wherever it is used. To stop an external pipeline, disable or revoke its key in Team settings › OpenAPI. Revoking a “deny” grant lifts a restriction, so the person may regain access through other grants. When the action succeeds and the row disappears from the list, keyboard focus moves to the list's heading.
