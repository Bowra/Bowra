# Use the Administration Console

Open **Administration Console** from the account menu. The sidebar shows the pages available under your Team role and deployment.

- **Approvals.** Open a pending item and check the requester, Space, action and proposed arguments before approving or rejecting. Use the history to check the decision and any execution result. An in-chat confirmation requested from a member uses its card in that member’s conversation.
- **Turn traces.** Find the turn ID in the diagnostic information and enter it in the lookup. Review the status and step outputs you can access. Give the ID and failing step to support if you need help.
- **Usage and credits.** Open a member’s entry to check their balance and ledger. Use the available export or credit adjustment actions. [View and allocate credits](credits.en-US.md)
- **Access control.** Select the correct Team and Space when managing groups, members or a resource grant. Find the resource and recipient by name, choose the permission, effect and optional expiry, then check the grant summary before saving. Use the exact identifier if the authorized directory does not contain the target. Use the access explanation to check an existing decision before changing a rule. [Permissions reference](permissions-reference.en-US.md)
- **Audit log.** Where available in Business, filter Team events by action. To find a permission change, look under **Access and Sharing** for **Resource Grant Added or Updated** or **Resource Grant Revoked**. Expand a record to see the event code and any permission details.

A Team role can authorize an administrative action. Access to the underlying Space and content is checked separately.

## Distribute a report or HTML file to a group

1. To manage a roster manually, a Team owner or administrator can create a group in **Access control**, leave the Space field empty and add existing Team members by member ID. For a Store Managers group, remove the outgoing manager and add the replacement when responsibilities change. Department and title changes do not automatically update a local roster.
2. Open the report or eligible HTML result, choose **Share**, select the group and confirm. A new member can use the existing link. Removing someone from the group takes away the access they received through it on their next request. A separate personal, group or Team-wide grant can still give them access.
3. To maintain groups automatically in Business, use **Directory sync (SCIM 2.0)** when available. Create a provisioning credential. Copy the **Tenant URL** and the one-time **Secret Token** into your identity provider. Choose only the users and groups to sync. For Entra, enable both mappings in the enterprise application's **Provisioning** settings, test the connection, then start provisioning. Use an externally reachable HTTPS Team entry. [Microsoft Entra provisioning setup](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups)
4. For an existing account, confirm the person's verified Team member ID and link it before provisioning. Refresh the directory list and choose **Publish for sharing** on the intended group. Use its directory ID to distinguish similar names. Maintain its direct members at the provider. A transfer takes effect after Bowhead receives and commits that update. Changing a job title alone does not change a group roster.
5. Rotate the credential before its displayed expiration and update the provider. Disabling sync stops further updates and leaves the last confirmed membership effective. To withdraw material immediately, revoke the group's grant in the material's sharing dialog. A previously downloaded copy remains with its recipient.

## Review knowledge base grants

Administrators are rolling knowledge bases out gradually. When knowledge bases are enabled, **Access control** also shows the read-only **Knowledge base grants** section to the Team owner and Team admins. It lists every resource grant on a knowledge scope or a knowledge file:

- **Applies to:** the person, group, Space members or everyone in the Team, by name. People who left the Team, removed groups and deleted Spaces are marked.
- **Target:** a Space's or the Team's knowledge files, or one file and the knowledge base it is in. A file you can't open, a file in someone else's My knowledge and a Space's members-only knowledge bases are not named. Deleted files and Spaces are marked.
- **What it restricts:** an allow grant turns its target into an allow list, so everyone without an allow grant on it is blocked; a deny grant blocks its recipient. The section shows whether this deployment enforces grants or only logs them.
- **Knowledge base members (suggestion):** which level the recipient would need on which knowledge bases to keep the same access, or that keeping someone out would need a members-only knowledge base.

These grants keep applying after the knowledge base rules: they can only block access, never grant it. The section changes nothing. To change or revoke a grant, use **Resource Grants** above it; a grant whose target or recipient no longer exists can be revoked.
