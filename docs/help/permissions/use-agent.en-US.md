# Hire and use an Agent

Use a published Agent that is shared with you from the Agent directory. Hiring adds it to your personal working set; it does not make you the Agent author or grant extra data access.

In the sidebar, choose **Agents → Marketplace** to find Agents. Choose **Agents → My Agents** to manage them. Its main tabs are **Hired by me**, **Created by me**, **Shared with me**, **Archived**, and **Team governance** when authorized. Each tab has its own address that you can bookmark or reopen; browser Back and Forward return between views. Clicking **Agents** opens **Hired by me**.

## Hire an Agent

1. Open **Agents → Marketplace** from a space where you are allowed to work.
2. Review the Agent's purpose, availability, dependencies, and audience.
3. Choose **Hire**. Confirm the Agent appears under **My hired Agents**.
4. In a new or existing task, select the Agent and provide the goal and relevant material.

For a member-created Agent to work, it must have a current released version, the Agent must be enabled, you must be in its current audience, and your own hiring must be active. If any of these changes, you cannot use it until the condition is restored. Hiring does not grant extra team, space, resource, or data permissions. Built-in system Agents marked **System included** do not require personal hiring.

## Pause or remove an Agent

Open **My hired Agents** and choose **Disable** when you do not want it used in your sessions. Re-enable it later if it remains shared and available. Choose **Remove** or **Unhire** to remove it from your working set. These actions affect your own use, not the published Agent or other members.

After unhiring an Agent you created, open **Manage my Agents → Created by me** and choose **Hire** on its row. You do not need to open its details. The button appears only when the Agent is currently hireable; if it is missing, check the row's availability reason and confirm the Agent itself is enabled with an active release.

## When an Agent shows “Unavailable”

In the Agent marketplace, the **Hired by me** list, and the Agent details page, an Agent that can't work for you right now shows **Unavailable** with a one-line reason. Only the single most important reason is shown at a time. If your hiring remains active, restoring the release, enabled state, or audience can restore access automatically. If you unhire the Agent or its hiring access was revoked, hire it again when the **Hire** button is available.

| Reason                                                   | What to do                                                                                                          |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Turned off or archived                                   | Its owner turned it off or archived it, or left the team. Contact the owner, or unhire it under **Hired by me**     |
| Restricted by an administrator                           | A team administrator restricted your use of this Agent. Contact the administrator if you have questions             |
| Needs attention from its owner                           | Ask its owner or a team administrator to check the released version and safety status                               |
| Not shared with you                                      | The Agent has not been shared with you                                                                              |
| Not in this space                                        | You are not a member of the current space. Switch to a space you belong to, or ask a space administrator for access |
| Hiring required                                          | You haven't hired it yet; choose **Hire**. You can use it when the other conditions also hold                       |
| You turned it off                                        | You paused it under **Hired by me**. Choose **Enable** (on the details page, **Enable again**) to restore it        |
| A dependency is restricted, or No access to a dependency | A resource it needs is restricted for you, or you don't have access to it. Contact an administrator                 |
| A dependency is unavailable                              | A resource it needs is currently unavailable; its owner or an administrator needs to fix it                         |

If it shows **Can't confirm**, Bowra couldn't confirm right now whether the Agent can be used. Try again later with **Retry** in the **Hired by me** list or on the details page. This is the only case where waiting helps; the other reasons don't resolve on their own.

### Partly limited

If the Agent still works but one of the tools it uses is currently unavailable, **Partly limited** appears next to the normal status, naming the item (or saying “A dependency is unavailable” when the name can't be shown). Capabilities that rely on that tool don't work; everything else does. The owner or an administrator needs to fix the tool. You can keep using an Agent you have already hired.

These statuses reflect your own situation, so colleagues may see a different status. You can always unhire an Agent you no longer need under **Hired by me**, whatever the reason.

## Team defaults

Team owners and administrators can configure supported default Agents at the team level. For an eligible member, a team default creates a personal hiring record that the member can disable or unhire; it does not override current audience or data authorization. Built-in system Agents marked **System included** do not require personal hiring. If an Agent is visible but cannot run, use [Troubleshoot access](troubleshoot-access.en-US.md).

For built-in analysis Agents, state whether you want a specific fact or a full analysis. A factual question can receive a concise answer with its source; request a table, chart, file or report explicitly when you need that deliverable. Risk reviews and investment assessments still require their supporting evidence. Missing evidence is reported as a limitation.

For marketing materials, say whether you want a new image, a text change to an existing poster, or an export of its current version. Identify the existing design when several are available. For a new presentation, say whether your supplied content is final or needs further analysis. Editing an uploaded presentation while preserving its layout still uses its document draft and review process.

If the model produces neither an answer nor an action before any operation runs, Bowra makes one recovery attempt within the task’s original limits. If it still cannot proceed, the task shows an empty-response or response-limit error. Use **Retry**; if it happens again, copy the diagnostics for your administrator. You do not need to add irrelevant dates or dimensions to a complete request.

When a knowledge-base question has no supporting matches, the Agent tries a limited number of relevant reformulations and then states what remains unverified. It preserves any facts already supported by sources. “No relevant evidence found” does not mean the knowledge base is empty; provide a document name or more precise subject if you want a new search.

To create a new presentation from confirmed content, give the desired page count, slide titles, text and colors. The Consultant preserves that scope and provides an editable PPTX after rendering and download verification. If file registration fails during execution, recovery can reuse the rendered content without rendering again. Editing an existing template still uses its separate draft and review steps.
