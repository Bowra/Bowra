# Start an analysis

Use this guide when you want Bowra to analyse material, frame a question, or produce a report. You do not need to understand every Agent, Skill, or permission setting before you begin.

## Finish the team setup

Team owners and administrators can open **Team Readiness** from the user menu. The page highlights the next core configuration to complete. Follow its action to the relevant settings page, then use **Return to readiness** to come back and refresh the status.

Completed checks, capabilities and recommended setup, and team information are collapsed by default. Expand them to review configuration, permissions, and check details. Warnings that need attention remain visible, even if the core checks are complete. Recommended settings do not reduce core progress; some capabilities still require their own configuration before use.

When all five core checks are satisfied, the page shows **Your team is ready**. You can enter Bowra or return later to review the configuration. A configured connection is not proof that an external service is currently reachable.

## Before you start

- Confirm that you are signed in with your own account and have selected the right team and space.
- Prepare a clear outcome, audience, time range, and decision criteria.
- Provide only files, fields, and context needed for this work. Never put passwords, tokens, or other secrets in a conversation or attachment.

## Start the work

### Create a basic presentation

Use the Bowra PPTX Skill to create a new text/table presentation from authorized facts. It does not preserve an uploaded PowerPoint template, its charts, masters or layout. If you supply a template, confirm whether a separate basic presentation is acceptable before generating it. Review the downloaded file before sharing.

1. Choose **New Work** in the left navigation.
2. Check the team and space at the top. If colleagues will collaborate, make sure they belong to this space.
3. State the expected result, then add context, scope, and criteria. For example: “Compare this quarter's East China sales with last quarter, explain the main drivers, and provide three recommendations for leadership.”
4. Add only relevant files and say how each should be used.
5. Send the request and review the process and result. Continue in the same work to refine conditions or correct scope; create a new work item only for a new question.

## Answer questions during analysis

In a regular conversation, an Agent may show a question card to confirm a period, metric definition, or other condition. Answer each question, add your own answer where needed, and submit to continue. The card means the analysis is waiting for your input; it does not mean the analysis is complete. If no card appears, use the question shown in the conversation to provide more detail.

When an Agent selected with `@` asks after reading its Skill, answer the original card to continue with that Agent and its retained reading results. If that Agent then prepares a document, the same task receives your saved answers. Asking the question does not write its waiting summary into your report.

- With a single single-choice question, selecting an answer submits it. To give a specific number or text, type it in **Type your own answer…** on the card and choose **Send**.
- With several questions or multiple choices, answer each and choose **Confirm and continue**.
- If you can't answer a question yet, choose **Skip this question**, or choose **Skip all, let the model decide** to let the model decide from context. Skipping may mean the result covers only part of the question.
- To change your original question, choose **Rephrase →**. The card closes and the original question returns to the composer so you can edit and send it again.

After answering, you can refresh the page to review the questions, your answers, and the results that follow.

## When “Send status unresolved” appears

While a message is sending normally, you only see a waiting state. If the network drops after sending and the page can't tell whether the task has started, **Send status unresolved** appears next to the composer. Editing is paused and your content is kept.

- Choose **Check and retry** to check the result of the original send. If sending needs to continue, it reuses the same submission so the task isn't created twice.
- If you don't want to wait, choose **Abandon and keep editing**. If the original task had in fact started, the page takes you to that conversation; only if it hadn't started does your draft become editable again.
- If the check fails for now, try again later. Your content is not cleared.

A draft with `@` or `/` references may also show a read-only copy. Choose **View references and parameters** to review it, or use **Copy text** or **Copy full draft** to keep the content. While the send status is still unresolved, use the actions next to the composer first.

## Select an Agent, method, or tool

When the composer shows a shortcut menu, type `@` to select an Agent and `/` to select an available method, tool, or capability. Disabled entries are unavailable; use the explanation in the menu. Selected resources become chips in the composer. Select a chip, or focus it with Tab and press Enter, to fill required parameters. **Apply parameters** updates the draft; you still need to send the task.

Use the source label to distinguish resources with the same name. An unavailable resource keeps its explanation visible. You can continue choosing from available results if another category fails to load, or choose **Retry** to load that category again.

You can select an available team or system Agent without adding a method. A Skill already included in that Agent can be referenced as ordinary instructions without a separate parameter form. If no compatible Agent is available, choose one that includes the Skill or ask its owner to add it. Selecting instructions does not grant permission to run scripts. When a supported task shows a question card, answer on that card to continue with the original Agent and selected methods. You do not need to select them again. Sending a new message, including one edited through **Rephrase**, starts a separate task rather than answering the pending card. If access or task materials have changed, continuation may be unavailable; follow the message shown on the card.

If a task needs a Datarc workspace, choose from the workspace list when prompted. Your selected workspace name appears while the task continues. The original task and its attachments remain available while you choose. A withdrawn file, changed permission, or expired task material can prevent continuation.

On a workspace selection card, check **Remember my choice** to make it the default for future tasks. Leave it unchecked to use the selection only in the current Chat.

The analysis agent can also list available Datarc workspaces and use these same switching tools when you request another source. Ordinary data questions keep using your selected workspace.

Say “switch to …” to change the source for
new work in the current run. Helpers already working keep their original
source. A temporary choice belongs to this Chat; asking to remember it changes
your default for future tasks without replacing another Chat's temporary choice.
Check the result: a switch can work for the current run even if saving fails or
cannot be confirmed. Upgrades preserve an older workspace choice when its original Team and connection can be confirmed. If that source is ambiguous, choose it once again; a newer saved choice is kept. If the task's original connection or access has changed, start a new task
with an explicit source instead of continuing the old helper under a new source.

Available built-in Agents also support these selections and question cards. You can select one alone or pair it with an available method.

If other Agents help with your task, they can still use their own methods. A method you selected for the main Agent is not automatically shared with them.

You can also queue a task with an Agent or method while another task runs. When the queued task starts, it keeps your selection. If it asks a question, answer on that task’s card to continue it. You can cancel a queued task before it starts.

When an Agent asks for a missing value after reading a method, answer on the question card. Supported text files, extracted PDF text, XLSX previews and PNG, JPEG or WebP images stay with the original task; you do not need to upload them again. If a file has changed or is no longer accessible, the task may be unable to continue. PDFs use extractable text; provide images or data separately when scanned pages or charts are needed. Excel previews show only the first 30 non-empty rows per sheet. Analysis of the complete workbook requires an Agent with the necessary file-reading tools.

For Word or PowerPoint, finish uploading the DOCX or PPTX and explain how you want to use it. An Agent configured to read attachments can read its text when needed and keep the read result while asking you a question. Reply on that question card to continue. Reading text does not interpret charts, images or page layout, and does not create a document from a template. A changed, deleted or inaccessible file cannot be silently substituted during continuation.

When image creation is available in the `/` menu, select it and describe the image you need. You can also queue this request while another task runs. If it asks about style or another missing detail, answer on its question card to continue with the original selection. The result must include a generated image; a text description alone does not complete an image request.

When a reviewed team tool or tool source is available in `/`, select a team Agent with `@` and fill in the requested parameters. You can send the task immediately or queue it. If it asks for missing information before or after a supported tool read, answer on the original question card; the Agent and tool parameters remain selected. Saved successful reads are reused when the task continues. If those results have expired or access has changed, continuation may be unavailable. Selecting a source makes its available tools usable as needed. Selecting a specific tool requires that tool to complete successfully. A failed task does not mean the tool never ran; check the task result before sending again.

If **ROE DuPont attribution** is available in the method menu, select it to break profitability into margin, asset turnover, and leverage. Supply the company, period, revenue, net income, average assets, and average equity when known. Missing required figures may be requested on a question card. The method can also explain five-factor DuPont or value creation when the additional data is available. Selecting a method does not enable extra scripts or data connections.

When **MCP prompt templates** are available in `/`, choose a team Agent with `@`, select one to three templates and fill in their parameters. You can send immediately or queue the task with attachments. Templates provide reference material after sending; filling in parameters does not contact the service. Answer missing-information questions on the original card to keep the Agent, parameters and supported attachments. Saved template material and completed method reads are reused when the task continues. If permissions change or material expires, continuation may be unavailable. Prompt tasks cannot currently mix other resource labels or use plan mode; the Agent's existing methods remain available.

## Personal settings

Personal settings are direct pages. Open **Settings** from the account menu, or use the relevant address: Account `/settings/profile/`, Security `/settings/security/`, Appearance `/settings/appearance/`, Memory `/settings/memory/`, or Export `/settings/export/`. You can bookmark these addresses or open them directly from another page.

When a memory suggestion notification asks you to review an item, select it to open **Settings → Personal → Memory**. Review the pending suggestions there before they expire.

## Find settings and capabilities

Open **Settings** in the account menu to find your personal preferences and the team or Space settings you can manage. Existing page addresses still work.

The Capability Center overview is one page with a section per resource: Plugins, Skills, Tools, connections and scheduled tasks, and Agents. Each section shows the real items and their state, and **View all** opens the full list; team owners and administrators also find the matching management pages there. Turn on **Only mine** to keep just the Plugins you turned off and your own Skills, connections and scheduled tasks. To create an Agent when eligible, open **Agents → Create Agent**; being a team administrator is not required. Seeing a resource in the catalog does not give you permission to run it or change its configuration.

Authorized administrators can open **Administration Console** from the account menu and switch between operational traces/approvals, resource governance and the available administration pages in its sidebar. Publication reviews and your personal connection confirmations stay in their original pages. Use **Back to Work** to return to your task.

## Decide whether a result is usable

An Agent finishing its part does not mean the whole task has finished. Check the task's final status and answer. Unavailable usage or cost information is unknown, not zero.

An unknown delivery status means the service has not confirmed the call's outcome. It does not mean the call failed or was free. Check the latest task record before resending; a late response can update the record after the task stops.

Before sharing a completed result, check that it answers the question, uses the requested period and filters, connects key numbers to conclusions, and has appropriate sharing for the report, attachments, and source data. A shared report does not grant colleagues access to your private conversation, every attachment, or your data identity.

**My Reports** in the sidebar shows 50 reports at a time. The right end of the toolbar shows how many match the current search and filters (“N found” while you search). The bottom of the list reads “Showing N of M”; select **Load more** for the next batch. Reports already shown and the **Shared with me** section below stay as they are. Once everything is shown, the bottom reads “All N reports shown”. Changing the search, Space, sort, or the empty-report toggle starts again from the first batch. If loading fails, select **Retry**; the reports already shown stay.

## Next guides

- [Grant space access](permissions/space-access.en-US.md)
- [Publish an Agent](permissions/publish-agent.en-US.md) or [Hire and use an Agent](permissions/use-agent.en-US.md)
- [Troubleshoot access](permissions/troubleshoot-access.en-US.md)
- [Security and offline sandbox](security-sandbox.en-US.md)
- [Revise an existing report](revise-report.en-US.md) or [Send email](email.en-US.md)

If your environment supports methods in Planning mode, select a team Agent and available read-only methods, describe the task, then review the plan before confirming it. You can edit the steps before confirmation; a changed plan must be confirmed again. Main steps keep the selected Agent and methods, while helpers use their own resources. Required main steps cannot be skipped or switched to another Agent; start a new request to change that selection. This initial path does not accept attachments or other selected resource types. If access changes or task materials are cleared while the plan waits, it cannot continue with replacement materials.

Confirming a plan keeps the language of its original task when your answer preference is automatic or follows your input. An explicit answer-language preference or team requirement still applies. An answer-only plan returns its result in the conversation; a plan requesting a report produces a report.

Open **Agents** from the main sidebar, after Library, to browse the market or enter Manage My Agents. All Agent management Tabs, details and editors keep that primary entry selected. Capability Center lists Overview, Skills and Workflows, My Connections and Scheduled Tasks; Tool and Team Plugin catalogs and configuration remain under Overview. Skill category/My filters stay inside the Skills page. Tools, Team Plugins, Skills and Workflows, and Agents offer their corresponding management actions when you have permission, with links back to the resource page.
