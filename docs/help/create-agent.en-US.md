# Create and manage your own Agent

For a plain-text answer, describe the required format in the Agent instructions and leave the advanced output schema empty. Test the Agent and its complete Chat response separately. You can ask Coach to show an already saved engineering requirement; viewing its receipt does not start connection onboarding or create the missing tool.

Open **Agents** in the main sidebar, then choose **Create Agent** when available. Open **Agents → Manage My Agents** for the existing management views. Switching Hired, Created, Shared, Archived or Team views keeps Agents selected in the sidebar. Creation eligibility is independent from the team administrator role; team governance does not grant access to another author's private drafts.

You can create an Agent for your own use, review its configuration and confirm creation, then adjust and use it from its management page. The creation entry points and options depend on the features your team has enabled, so this guide describes each case by what the page shows. If the page doesn't offer an option, follow what it actually shows. Agent drafts are saved in a Space: without one, **Create Agent** explains why. Team owners and administrators can go straight to creating a Space; members ask an owner or administrator to add them to one.

When coaching is available, you can first ask which tools, Skills or reusable team plugins you can use, without creating a draft. The assistant can inspect available resource instructions and requirements. Finding a resource does not mean it has been tested or added to an Agent. You can then ask for a new Agent draft. Review the saved draft and its suggestions, accept the fields you want, and confirm private creation. If the panel does not open, resume the saved draft instead of requesting a second one.

When draft coaching is available, save your draft and keep it selected in the conversation, then ask for a focused improvement. The assistant can inspect the draft, look for suitable tools, check proposed changes and save a suggestion for review. New suggestions appear in the original draft within a few seconds while draft coaching is connected, without replacing your unsaved edits. Review and accept only the fields you want. If an incomplete configuration blocks a narrow change, Coach should explain what is missing and ask before proposing other fields or tools. A saved suggestion has not changed or created an Agent. If you edit the draft while the assistant works, save your changes and send the request again against the current draft. For an Agent you already own and can edit, the assistant writes its changes directly into the Agent's draft instead of submitting a proposal to accept; see “Let Coach change an existing Agent” below. A validation result describes the checks actually run; it is not automatically an evaluation pass. If an interrupted request has an unknown outcome, recover that request before starting another generation. Where available, Coach can also create or edit your personal Skills; those saves directly change the personal package and have their own recovery receipt. Connecting new tools, publishing Team Skills and running evaluations retain their separate supported actions.

When the right panel lets you switch objects, select the draft before sending an editing request. Switching afterward does not redirect that message. If the conversation cannot edit the selected draft, your message stays in the input and the draft shows a notice. Edit directly on the right, or close the draft before sending a regular message.

Saved display names update the object labels. Equal labels are distinguished by their actual Agent names, and the compact selector shows unsaved status. Closing the selected Agent returns to the most recently selected remaining Agent; a later selection remains active if an earlier close is waiting to save. If the original draft read says it is unavailable, private fields and Chat editing are removed; retry the read or return to your saved drafts.

When Coach evaluation is available, first make sure the draft holds the changes you want and complete the Agent’s Safety Check, then ask Coach to request an evaluation. When **After Coach edits a draft** is set to **Check automatically**, Coach starts one limited release check itself (see below); you can also ask Coach to test a change it just made. With **Ask me first**, or when your review is needed, **Coach evaluation requests** in the release check on the **Release** tab shows the changes to evaluate, the two versions it compares, the maximum model requests and comparisons, how long it may run, and the estimated usage (tokens, model requests and duration only; **No usage estimate yet** when there is no history). Choose **Allow and start evaluation…** and confirm the limits and deadline once more in the dialog. **Not now** only collapses the card; the request keeps waiting until it expires. Opening or refreshing the page does not start evaluation. While it runs you can see the model requests and tokens used; **Stop evaluation…** cancels the running comparison, and requests already sent are not refunded. If a response is lost, refresh the status before continuing. A completed evaluation does not publish the Agent.

The **Release** tab always shows one primary button for the next step: test this change, test again, or release as the new version / submit for review. While the test runs it shows the cases completed; when it finishes it lists the cases that changed (worse first), and you can open a case to compare both versions’ answers. Actions that discard results or used requests, such as stopping a test or withdrawing a submission, ask you to confirm the consequence first.

If your Agent has no evaluation suite yet, ask Coach to propose the first cases and a suite name. Review the tasks, expected results and scoring under **Pending proposals** at the top of the **Configure** tab, then accept the cases you want. Accepting the first cases creates your private suite; writing a proposal alone does not. If someone creates a suite meanwhile, ask Coach to inspect it and prepare a new proposal for that suite. Saved cases have not been run.

To stop an unfinished personal Skill save, ask Coach to stop that exact request. It can confirm that an unapplied change was cancelled, or report that the save already took effect. Stopping does not undo a saved Skill. If newer manual content prevents a reliable answer, retain the original request for recovery. You can resume the request from another conversation in the same Space when you still have access.

When asking Coach to check a change, read which checks actually ran. Configuration and dependency checks do not run the business task. An unavailable tool, or a Skill waiting for re-approval, can block the change even when its fields are valid. To test behavior, use the supported Agent evaluation flow and review its run results; a draft, Skill or Plugin check alone is not a trial.

If sending an editing request is interrupted, keep its original recovery notice and let the page check the result after refresh. Recovery refers to the draft selected when you sent it, even if you opened another draft afterward. A confirmed send means the request was received; review the original draft’s suggestion or save result to see what changed.

You can ask Coach in another conversation in the same Space to check an earlier request using its original receipt. Checking the receipt does not repeat a save or start an evaluation. If the record says the outcome is unknown, keep that request for its supported recovery action; an expired or unavailable receipt cannot prove success.

When Coach offers a Skill editor in the right panel, you can edit it with the same save controls as the Skills page. Switching back to the Agent keeps both editors open. Saving the Skill does not add it to the Agent: adding it is a separate step, which is a binding suggestion to accept on a creation draft, or a change Coach writes into an existing Agent's draft that you can undo. A refreshed page reads saved content; save before leaving if you need to keep local edits. If access has been removed, the panel clears the resource. Selecting a Skill panel alone does not tell Chat which Skill to change; name the intended resource in your request.

## Choose how to create

- **Creation panel on the right**: choosing **Create Agent** in the Agent marketplace or on the management page opens a creation panel to the right of your current work; your conversation and input stay on the left. On narrow screens, switch between the chat and the creation panel.
- **Ask the assistant in a chat**: say in a chat that you want a new Agent and describe what it works on, what it does, and what it should produce. If the purpose is not specific enough, the assistant asks you once for more detail; you can also say "configure manually" or "cancel" at any time. Once the purpose is clear, an AI-generated draft opens on the right, including any name you stated explicitly.
- **AI drafting**: describe the problem to solve in the panel on the right, let AI generate a draft, review it, and confirm creation. You can also switch to filling in the form yourself. If a draft can't be generated right now, switch to manual configuration; the purpose you wrote is carried over.

When the panel offers saved drafts, a chat request first saves and opens one draft, then adds AI suggestions. Select the fields to accept; suggestions do not overwrite your work automatically. If generation fails, continue editing the saved draft or retry suggestions on it. The original request stays as its purpose rather than becoming confirmed work instructions.

Whichever way you choose, you review the configuration and confirm creation yourself.

Saving while you continue typing keeps newer input marked unsaved until it also reaches the server. Drafts save automatically, so closing the tab does not ask; while a save is still running or has failed, you are asked whether to keep editing or discard the unsaved changes. Switching conversation, Team or Space closes the old draft selection without discarding saved content; resume from My Agents in its original Space or open its draft link. If Chat editing is unavailable, edit on the right; reopening the draft does not enable that feature.

## Fill in the configuration

On the creation card, enter the name, purpose, and work instructions, then select tools from the options the page offers; Skills can be selected only when the page explicitly offers them. The last step lists the selected tools and marks the risk category of team tools; review them before confirming. Selecting a Tool or Plugin that changes another system, such as Email, is your permission: the card shows one line saying what it will do and does not ask you to confirm again. When the Agent is used, the user's permissions and connection status are still checked.

- If no tool fits, teams allowed to create text-only assistants can leave tools and Skills empty; other teams must select at least one tool to continue. See the text-only assistant sections in [Configure Agent capabilities](permissions/configure-capabilities.en-US.md).
- When selecting Skills, choose the specific Skills the Agent needs. General-purpose tools that can't limit which Skills are used, such as listing Skills, can't be used for a new Agent.
- Tools, Skills and Plugins: a **Tool** is one action the Agent can take (query data, run Python, send email); a **Skill** is a method or template it follows ("how to write the weekly report"); a **Plugin** is a ready-made set of Tools and Skills. Start with Plugins and add single Tools or Skills only if something is missing.
- Each Plugin says when to use it, gives an example request and lists what it adds, for example "Adds: Run Python · Includes guidance". A Plugin with guidance attaches that guidance automatically. The Team Plugins group appears only when your team has team Plugins. Plugins you can't use are not listed; team administrators also see Plugins the team turned off or hasn't set up, with a link to fix them. A selected Plugin that later becomes unavailable stays selected so you can clear it. When a Plugin is updated, the Agent uses the new contents on its next run.
- When you create an Agent from a description, the draft already picks Plugins for the request and marks them **Recommended**, with the line that explains why. Remove any you don't want.
- On **Configure**, **Add…** opens one search over Plugins, Tools and Skills.
- **Email and Google delivery**: selecting the Email or Google delivery Plugin gives this Agent your permission. Once you save and release it, the Agent sends email or writes to Google without asking each time, including when other members run a shared Agent. The chat shows what it sent or wrote. Lead, the main assistant, still asks you before it sends or writes. A team administrator can turn these Tools off for the whole team at any time. Until the team sets up its mail service, the Email Plugin shows **Not ready** with the reason; owners and administrators can follow **Set it up** to Team settings › Email Service, and other members don't see it.
- When reviewing an AI-generated draft you can add or remove Plugins and Skills in the same place, and when editing an existing Agent (including from the configuration assistant) you can adjust its Plugins; only selections you changed are saved.
- If the Agent will check web pages you give it later, just say at creation that “the user provides the URL each time”; you don't need to enter a link from a particular task. Check that the draft includes the capability to read web pages; web search finds other pages and can't replace reading a specific link. If a page can't be read when the Agent runs, it says it couldn't verify it this time; check that the link is reachable, or paste the page content and try again.
- If the purpose requires the team's business data (such as store revenue or sales details), the draft selects the built-in data query tool, even if the description only says “team data”.
- If a required capability has no tool selected, the configuration check fails and explains which need is missing, why nothing was selected, and how to fix it: select a suitable tool in the tool list as prompted. If the tool is locked, ask a team administrator to enable it. You can't create the Agent until this is fixed.
- If a tool or Skill has become unavailable when you save, the card keeps what you entered and asks you to go back and check, remove, or replace the resource.

A new Agent is visible only to you. It isn't shared with colleagues automatically and doesn't appear in the Agent marketplace. To share it, check the sharing actions available on its management page.

## Understand which Skills your Agent can use

In an ordinary conversation, built-in execution Agents can read your enabled personal Skills as needed for the task, provided no import issue is pending. You can name a Skill in your request. This does not add the Skill to a custom Agent you create.

For a custom Agent, select the Skills it needs directly or through selected Plugins. Personal Skills can only be added when the Agent is set to **Only me**. If the Agent is shared with colleagues, use built-in Skills or team Skills available to everyone who can use that Agent. If a Skill cannot be selected, read the reason shown next to it and adjust who can use the Agent or choose a suitable team Skill. See [Publish a team Skill](permissions/publish-skill.en-US.md) to submit a personal Skill for team use.

Where **Optional Read-Only Methods** is available, you can configure methods users may explicitly choose for a single task. Listing a method there does not make the Agent use it on every task. Save the configuration and apply or publish the Agent version before relying on the change.

Adding a Skill lets the Agent read it. If the Skill includes scripts, the Agent can also run them in the isolated sandbox; you don't select a separate tool for reading, running or listing Skills. If your deployment hasn't enabled Skill scripts, the editor says so next to the Skill: the Agent can read it but not run its scripts until an administrator enables them, and you don't need to save the Agent again afterwards. An Agent you released earlier keeps its permissions until you release it again. Skills never grant access to data or connections, and sharing an Agent does not share its owner's personal Skills automatically.

The Agent uses the latest approved version of each Skill. When a Skill it uses is updated, the Agent page and the release review show which Skills were updated and recommend evaluating the Agent again. Turning a team Skill off for yourself in Skills doesn't stop Agents that include it; a restriction set for you by a team administrator still applies.

If saving or releasing is blocked, the message lists each blocked Skill or tool with its reason and what to do. Common reasons:

- **Edited and waiting for re-approval**, or **not approved yet**: ask a team administrator to approve the current version, or remove the Skill.
- **Turned off for the team**: ask a team administrator to turn it back on, or remove the Skill.
- **Its files can't be read**, or **its safety check record is missing or damaged**: re-upload the Skill.
- **This name now belongs to a different Skill**, or **it changed after it was added**: remove the Skill and add it again.
- **It bundles its own packages**: a team administrator reviews the release before it goes out.

A mention of commands such as `curl` in a Skill's instructions is shown as a notice in the release review and doesn't block the release; scripts run without network access.

If a Skill can't be used during a run, the step names the Skill, what the Agent tried to do and why, for example that the deployment hasn't enabled Skill scripts, that a team administrator restricted the Skill for you, that it is waiting for re-approval, or that it no longer exists. Follow the action in the message; repeating the same request doesn't help until the cause is fixed.

## Choose a document Skill

When the configuration offers Skills, select the specific document Skill your Agent needs:

- **Word documents** (`bowra-docx`): new editable headings, paragraphs and tables.
- **PDF documents** (`bowra-pdf`): new text/table PDFs and embedded-text inspection; scanned pages need separate OCR.
- **PowerPoint documents** (`bowra-pptx`): new editable title, bullet and table slides.
- **Excel workbooks** (`bowra-xlsx`): value/date worksheets and cell inspection; formulas are not calculated.

These are standard built-in Skills. Selecting one does not grant file access or change an already published Agent. Save and apply the new Agent version through the existing process. These basic Skills do not preserve an uploaded Office template; template-preserving edits are not provided by these Community Skills.

In Bowra Community, a successful document creation appears as a downloadable file in the conversation. A filename mentioned in an answer is not confirmation that a file was created. For PowerPoint, request a new basic text/table deck. If you supply a template to preserve, the Agent should explain the limitation and ask whether a separate new deck is acceptable before generating it. Inspect the downloaded file before sharing; slide rendering and template fidelity are not provided by this Skill.

## Save drafts and continue later

In the creation panel on the right, starting manual creation first creates a draft and then shows the form; choosing AI drafting also creates a draft you can come back to. If the page can't create the draft, choose **Retry creating draft**; retrying continues the same creation task.

- After you pause typing, changes are saved automatically; you can also choose **Save draft**. Changes are saved in the draft only when you see the “Draft saved” message. If automatic saving fails, your input stays in the panel; retry saving as prompted.
- The draft in Canvas saves automatically; the toolbar shows **Saved** or **Saving**. Only while a save is still running or has failed does closing or switching page, conversation, or space ask you to **Keep editing** or **Discard unsaved changes**. Discarding drops only the unsaved changes; the saved draft remains. If saving fails, choose **Retry save** in the toolbar.
- If another browser tab saved the same draft first, the page shows, field by field, the value before editing, your input, and the latest saved value. Choose for each field and then save, so changes made elsewhere aren't overwritten.
- From the general creation entry, you can continue an unfinished draft in the current space or explicitly start a new one. The draft list shows each draft's name, source, status, last saved time, and expiry, so you can tell drafts apart; continuing a manual draft takes you straight back to the manual editor on the right.
- Under **Created by me** in your Agents, you can also find unfinished AI and manual drafts that can still be continued. The **Continue creating** section appears only when there are drafts to continue. Once a draft is open, the page address can be used to refresh and keep viewing it.
- If the draft can't be found, check the current space, choose **View my drafts** to find other drafts, or start again. Expired drafts can't be continued. If loading fails temporarily, retry on the card.

## Draft with AI

1. Choose AI drafting, enter the goal, and generate suggestions. While generation runs, you can stop waiting and fill in fields yourself; the results appear later in the same draft.
2. Compare the current content with the AI suggestions field by field. Select only the fields you want to adopt, or ignore a suggestion entirely; your own changes are never replaced automatically. If AI needs more information, answer on the right, or change the goal and generate again.
3. If the recommended tools don't suit your task, open the tool catalog under **Capabilities and tools**, search for and select other tools you're allowed to use, or clear the original suggestions. The page marks the tools you added and explains why some can't be selected; when you're done, choose **Save and check**. If the catalog can't be opened, what you've entered and selected is kept; try again later. If saving reports that a tool is no longer available, remove or replace it as prompted. Whether Skills can be added here depends on the options the page offers.
4. Review and confirm creation.

To switch to filling in the form yourself, choose **Configure manually**. Changes you made on the right are saved in the same draft, the original goal is carried into the work instructions with a short version in the routing description, and a name you stated explicitly in the chat is kept. After switching you can still continue from the original address or from **Created by me**. If a version conflict is reported, review and reload first. If a request fails, the page checks the draft's status again; if the draft can no longer be generated, you can switch to manual configuration, and your original goal and extra answers are carried over. Changing the name or work instructions doesn't clear the original reasons for recommended tools.

## Edit a draft from the chat

When your team has chat editing for drafts, AI-generated drafts under review and saved manual drafts are connected to the current chat. When the right side shows that the draft is connected to this chat, describe the fields to change on the left; after the change is saved, the right side refreshes, and you review and confirm creation. If the right side asks you to edit there, edit directly on the right.

- If the right side has unsaved changes, a new message stays in the composer and you're reminded to save or discard the changes on the right first; then send again.
- Changes from the chat never overwrite what you're typing; if there is a version conflict, reload as prompted.
- A manual card that isn't connected must be edited on the right; replies on the left don't change it.

## After creation

In the Canvas editor, the object Tab holds its name and close control. After creation, the same Tab shows the saved configuration and the available next action. Reopening may first show “Created Agent” while its name and configuration load. If reading fails, choose **Reload configuration**; this only reloads the existing Agent and does not create another one. Closing its Tab only closes the view.

Creating an Agent releases it as v1 right away. By default only you can use it: the receipt says **Released v1 · Only you can use it** (or **Shared with selected people** when you chose people at creation) and gives two next steps. Try it first: choose **Start using** when the receipt offers it; otherwise type @ in a chat, pick the Agent, and ask your question. The receipt also lists what the Agent can use by kind, for example **1 Plugin · 3 Tools**; a Plugin counts once, not as the Tools it brings. For more confidence, add a few cases on **Evaluate**; to share it, change who can use it under **Configure › Who can use it**. Team-wide access may need administrator approval.

After the Agent is created, choose **Manage Agent** to open it directly and review or adjust its configuration, or find it later under your Agents. Being added to your personal list doesn't mean you have hired it. Older Agents open their original configuration page; Agents under version management open the version workbench.

The version workbench has three tabs: **Configure**, **Evaluate** and **Release**; only the tabs your team has enabled appear. The former **Change proposals**, **Validate and release** and **Version history** tabs are merged into these, and old links take you to the matching place automatically.

- **Configure**: one page with three collapsible sections. **What it does** holds the name, description, instructions, built-in tool guidance and model. The optional **Main responsibility** sits under **Advanced**, folded at the bottom; private Agents usually don't need it, and the folded line shows it when one is set. For shared Agents with a main responsibility or a review step, Advanced starts open and first says what reviewers see. **What it can use** holds Plugins, Skills, Tools and connections; **Add…** opens the catalog in a side drawer. Counts there and in the save card include the Tools and Skills your selected Plugins bring, so an Agent that gets everything from a Plugin is not shown as having nothing added. **Who can use it and how it's listed** holds the audience, optional methods, listing information, copy policy and output contract, and is folded by default. When something in a section blocks release, its header turns red and states the reason. At the top, **Pending proposals** lists changes from external pipelines and user feedback for you to review and merge; it is hidden when empty, and the **Configure** tab shows how many are pending. Each change shows its current value next to the proposed one; when you haven't saved a draft since the last release, it is compared with the released version, and merging creates the draft from it. If **Merge selected changes** is unavailable, the card says why, for example when no change is selected. Above it, a list of thumbs-downs to review shows one row per turn where a member gave a thumbs-down and also shared the turn with you: a **Thumbs-down** label, the start of the question, then who, when, the reasons they picked and their comment (**No comment** when they left none). **Use this to improve the Agent** opens a new chat with Coach and the question, reasons and comment staged in the composer; nothing is sent until you send it. **Dismiss** removes the item, with an Undo. Either way the item leaves the list and no longer counts toward the **Configure** tab, and the turn stays available under **Draft from conversations**. An item also disappears when the member stops sharing the turn or takes back the thumbs-down. A thumbs-down the member didn't share never reaches you, and a turn shared from the **⋯** menu without a thumbs-down appears only under **Draft from conversations**; there are no inbox notifications for these items.
- **Evaluate**: manage evaluation suites (formerly under **Validate and release**).
- **Release**: opens with the **This change** card, which lists what changed per section, tagged by who changed it (Coach, Manual, External pipeline or Feedback), the evaluation suites and the release-check state, each with a link to the matching tab. When nothing differs, it says the draft matches the released version. The release check and its actions follow, and the version history is at the bottom, where you can choose **Create draft from this release**.
- **External pipelines**: external pipelines are managed with Team OpenAPI keys, not from the workbench; their proposals appear under **Pending proposals** as **External pipeline proposal**. To stop an external pipeline, disable or revoke its key in Team settings › OpenAPI.

- **Start using**: when the page offers **Start using**, choosing it opens a new chat with this Agent selected, where you enter the task. Available Agents on the details page and in the management list have the same entry. If the current version or permissions have changed, the page asks you to go back to the management page; a name typed as text is never treated as a selected Agent. Any unsent draft is kept.
- **Change the configuration**: adjust it on the configuration page and select **Save draft**; saving doesn't change the released version people use. A newly created Agent is already released as v1, so the first time you open the version workbench there may be no draft yet. If the page says your changes are kept only in the browser, save first, then continue to the **Release** tab.
- **Built-in tool guidance**: under **What it does** on the **Configure** tab of the version workbench, turn this on if the Agent should also follow Bowra's guidance for the tools it can use, for example the rules for querying business data. It is off by default. Like other settings, it takes effect only after you save the draft and release the new version; turning it on or off creates a new version that reviewers see as a change.
- **Apply a new version**: for an Agent only you use, choose **Apply new version** after verification; the new version applies only to you and isn't listed in the marketplace. For versions meant for colleagues, follow the page: **Activate new version** changes the current version and who can see it, which is not the same as listing it in the marketplace (the marketplace currently shows Agents according to team visibility); when approval is required, the page shows **Submit for administrator approval**. Review the audience and impact first; see [Publish an Agent](permissions/publish-agent.en-US.md).
- **Who can use it and how it's listed**: this folded section of the **Configure** tab sets the audience, optional methods, listing information such as description and category, copy policy and output contract.
- **Evaluations**: when the page offers the **Evaluate** tab and a result shows **Failed** or **Incomplete**, choose **See why it did not pass**. If it reports an evaluation environment fault, this result doesn't say anything about the Agent's quality; try again later and contact an administrator if it persists. If it says the evaluation went over its time limit, some case runs were stopped before they finished; nothing is broken, so choose **Test again**, and if it keeps happening, split the cases that time out into smaller tasks. Failed test cases mean you should check the task and the expected result. Don't conclude that the configuration is wrong just because zero cases passed. Release checks follow the latest saved test cases automatically. If the page says the evaluation policy was updated, test cases were saved or added after the page loaded; the page switches to the current policy, so choose **Test this change** again. If you saved cases or edited the draft after a test, its result is marked out of date: choose **Test again** to test your latest draft and cases; a release, risk decision or review submission waits for that test. If a test case has expired, the message names the case, its suite and the expiry date: remove that case from the suite on the **Evaluate** tab (editing it keeps the expiry date; add it again as a new case if you still need it), save, then try again.
- **Expected points in results**: when you open a case's answers, each run lists the case's points with **Met**, **Not met** or **Not graded** and the sentence from the answer the verdict relied on; met points are folded under the run. If a verdict is wrong in either direction, choose **Judged wrongly**. This records the answer so later checks of that point must judge it the other way, and may show Coach's suggested rewording; it doesn't change this comparison's result, so update the point and test again. A point that failed its check of good and bad answers is shown as for reference only, with the reason. When a release check lists "A point the model graded wasn't met" as a risk, the list of unmet points and their sentences appears with the risk, each with **Judged wrongly**; you can record the risk and release, mark the verdict, or both.

### Build evaluation cases

- **Draft from conversations.** On the **Evaluate** tab, choose **Draft from conversations** and pick up to 32 questions you asked this Agent. Each becomes a case whose task is your question as you asked it, and Coach suggests what a good answer must include. The list shows your own finished conversations, plus single turns that members chose to share with you from an answer's **⋯** menu, marked **Shared by** and their name. You never see members' other conversations, and a shared turn's answer is shown only once you draft a case from it. A member can stop sharing at any time; the turn then leaves your list, but a case you already drafted from it is your copy and stays in your suite. Review the drafted cases, then save the suite; nothing runs until you start an evaluation.
- **Earlier default settings.** Cases saved before October 15, 2026 may still allow no Tool calls, so an Agent that queries data can fail them for doing its job. The suite then shows how many cases are affected and **Update to current defaults**. Updating saves a new suite revision that removes that setting and the old strict number check; earlier results stay as they were.
- **Figures without a direct source.** A case's answers may show a hint listing figures that don't appear in the data the Agent read or in the task. Figures calculated from the data, such as totals, shares or rounded values, appear there too, so the hint never fails a case or blocks a release. Use it to spot a figure worth checking; to require a specific figure, add a data point to the case.
- **Read a release check.** Above the per-case changes, the **Release** tab shows how many cases ran and how often the candidate and the released version passed. A version shows "no verdict yet" when none of its cases reached a result, for example because they were inconclusive.

### Let Coach change an existing Agent

Name an Agent you own in the chat and describe what to change. You can also select **Ask Coach to change this** beside the version workbench tabs, or **Ask Coach to fix it** when a release check fails: a new chat opens with **@Coach** and the request staged in the composer. Nothing is sent until you complete the request and send it. If you can edit that Agent (the same right you need to save its draft by hand), Coach writes the change directly into its draft; there is nothing to accept field by field. The Agent's editor opens on the right. If you can't edit the Agent, Coach only replies with suggestions in the chat and changes nothing.

You can also start with Coach yourself: type **@** in the composer and choose **Coach**, or find Coach in the Agent marketplace. Coach is never picked automatically for other requests. If your team turned Coach off, it doesn't appear in either place.

- The change goes into the draft only; nothing is released or applied. The new version still takes effect only after the release check on the **Release** tab.
- If the canvas has unsaved edits, your message isn't sent to Coach and a banner appears above the composer: **Save and continue** saves the canvas and then sends the same message; **Discard edits** drops the canvas edits so you can send again.
- Each time Coach writes the draft, a receipt row under the answer shows how many changes went into the draft; **View in canvas** opens the Agent.
- A Skill Coach adds must come with the Skills it requires. If any are missing, Coach writes nothing and tells you which Skills are needed.
- After Coach writes the draft, an open editor for that Agent refreshes, unless it holds unsaved input.
- If a tool or Skill is invalid or you lack permission, fix it under **What it can use** first.

The Agent editor in the canvas shows, from top to bottom, **Coach's changes**, the three configuration sections (What it does, What it can use, Who can use it and how it's listed), and the release-check card, where you can start a check and release.

- Each row of **Coach's changes** is one field. **Undo** restores only what Coach wrote and never touches your own edits; an undone row offers **Reapply**. **Undo all** undoes every row.
- If you also changed a field after Coach did, its row shows **You changed this too** and **View conflict**. The dialog shows the value before Coach, the value Coach wrote and the current value. You can choose:
  - **Edit in Configure**: change the field yourself in the configuration.
  - **Keep current**: keep your value. The row leaves **Coach's changes** and counts as a manual change from then on; the draft isn't changed.
  - **Restore value before Coach**: after you confirm, an ordinary save replaces your current value with the value from before Coach.
- While any field is in conflict, **Undo all** undoes nothing and offers **Undo the other N** instead.
- While the canvas has unsaved edits, the undo controls wait until you save or discard them.

#### After Coach edits a draft

In **Personal Settings → Account → Agents**, or in the canvas editor's **⋯** menu, choose what happens after Coach edits a draft. The setting applies only to changes you ask Coach to make.

- **Check automatically** (default): after Coach writes the draft, it starts one release check on that change with a fixed limit of up to 200 model requests, 1 comparison and 30 minutes. If the estimate may exceed 200 requests, the check doesn't start; you're asked to review and start it yourself. A newer automatic check replaces an older one that is still running, and withdraws that older check's candidate if it was only prepared. A candidate already submitted for review is never withdrawn: the check then doesn't start, and you're asked to withdraw the candidate on the **Release** tab. Requests already used stay counted.
- **Ask me first**: Coach only requests the check. Review the usage and start it from the release-check card, as with Coach evaluation requests above.

Either way, a passing check doesn't release or apply the Agent by itself.

## Describe what a good answer does

On the **Evaluate** tab, each test case can list expected points: plain-language statements of what a good answer does. There are three kinds:

- **Judgment criterion**: something you can see in the answer, such as “compares with the same month last year”.
- **Data fact**: a figure the answer should state, such as “states East China revenue for last quarter”. It is checked against the data the Agent actually read in that run.
- **Required action**: a Tool the Agent must call before answering, such as querying sales data. Pick it from the Agent's own Tools, listed by the names Configure shows; with several picked, calling any one of them meets the point.

**Suggest test cases with AI** drafts a few points for each case, and Coach can suggest points when it proposes cases. A suggested point shows where it came from. It doesn't count until you choose **Confirm**; points you write yourself are confirmed when you save the suite. Choose **Save quality suite** to keep your changes.

When the released version has already answered a case's question, choose **See how they're judged on the released version's answer** under the case's points to see that answer and how each suggested point would be judged on it, with the sentence it relied on. If a verdict looks wrong, reword the point before confirming it. Choose **Mark as good answer** when that answer is one you'd accept; it helps check that the point tells good answers from bad ones.

Every confirmed point carries a badge: **Blocks release** means a required action (a point that names a Tool) blocks a release when it isn't met; **For reference** means a data fact or judgement criterion is shown for reference only. The status also says when a point is being calibrated or grading for its kind is still being validated. The add-point form shows the same badge for the kind you pick. Add a point with **Add point** under the case, and edit or delete one from its **⋯** menu; editing can also change its kind and its Tool, so you don't need to delete and add it again; choose **Collapse** when you finish editing a question. Changes to cases and points are used in tests only after **Save tests** in the save bar at the bottom, which says how many changes aren't saved yet. Tips: write one thing per point, describe something a reader can find in the answer, and avoid exact figures from today's data, which change between runs.

To turn a complaint into a point without writing it yourself, ask Coach and check its verdicts on real answers; see [Improve an Agent with Coach](agent-evaluation-with-coach.en-US.md), which also explains the **Held out** switch on each case.

## Translate the Agent's listing

If the Agent management page offers listing translations, choose the original language and a different translation language: Simplified Chinese, English, German, or French. Edit the translated name and description yourself, or ask AI to draft them. Review the draft, save it, and then confirm the translation. Confirmed text takes effect with the next release, through the same checks and approval process as the original listing.

Once a translation exists, the original language is locked. If the original text changes, review and save the translation again before confirming it. Translation changes the public name and description; it does not change the Agent's instructions or permissions. Only the Agent's owner can manage its translations.

## Use the Agent in a chat

The lead Agent brings it into the conversation when the task fits and current permissions and resources allow. **Typing only the Agent's name in the composer doesn't guarantee that it handles the task.** To check whether it was used, expand the run process of that answer and look at the Agents that took part.

When the composer offers quick references:

- Type `@` to choose the Agent for this task; type `/` to choose a Skill. If you select all existing input and then type `@` or `/`, the input is replaced and the matching list opens; pasting these characters doesn't open the list. If you choose only a Skill, Bowra tries to assign a suitable Agent that is currently available. Check the selected names and attachments before sending.
- If the Agent asks a clarifying question before or during the work, answer on the question card in the current chat. If an attachment was withdrawn or changed, submit the task again; to switch Agent or Skill, also send it as a new task.
- Tab completes ordinary text from history; `@` and `/` names in earlier messages aren't restored as selected references by Tab. Select the resources from the list again and check the **References** count.
- When you choose **Edit and send** on an earlier message, the original question fills an empty composer, and an existing draft isn't overwritten. The `@` and `/` names filled in are plain text; the original references and attachments aren't restored. Select the Agents, Skills, and attachments you need again and check the **References** count before sending. If you're told team permissions have changed, select them again first. Clarification cards from the original task should still be answered on the card; no need to resend.
- If a Skill in the `/` list is gray, read the reason under it. “This Skill has no read-only content for quick references yet” means you can't select it in the composer for now, even if the Agent can normally read that Skill; the Skill's maintainer needs to add content that can be referenced first.
- An Agent can also be unavailable when a Skill it depends on has changed or you no longer have access. Its owner must update the dependency and apply the new Agent version through the usual validation process; selecting the Agent again does not update its configuration.

If you need a formula calculated step by step, say so in your question. When the selected Agent provides a substitution that can be checked, the chat answer shows the formula and result typeset; wide formulas can be scrolled horizontally. Formulas that can't be checked aren't filled in automatically.

## If creation fails

If creation fails, the card keeps what you entered so you can edit it or try again. To change tools or work instructions after creation, go to the Agent's management page; whether the change affects the version in use depends on the current status shown on the page.

When a needed tool is missing and Coach handoff is available, ask it to retain the requirement and the Agent draft it belongs to. A team owner or administrator can use the existing connection page to preview the connection, choose tools and enter credentials. Keep credentials out of Chat. After setup, ask Coach to check the actual tool and return to the original draft. An opened page or available tool is not a successful business test. If you edited the draft while waiting, review a fresh suggestion; recover the original requirement after an interruption instead of repeating connection creation.

When Coach is available, you can ask it to inspect a Team Plugin or help an administrator compose one from eligible Tools and Skills. Review its shared impact: changing a Plugin can affect other Agents that use it later. For a change limited to one Agent, keep the change in that Agent’s resource selection. A saved Plugin still needs to be selected for the Agent and checked for the intended task.

When Coach suggests Skills or Plugins for a creation draft, review and accept those fields explicitly. Later manual saves preserve the selections, and you can remove them in the Tools step. Creation checks their current versions and permissions again. If a resource changes, review and validate the draft again; if it adds external write effects, confirm them before creating. A saved or privately created Agent has not yet completed a business trial.

A missing tool may also belong to a shared Plugin. Ask Coach to retain that Plugin and its original version. After the tool is available, an administrator reviews the current shared impact before saving the binding; recording a requirement does not change the Plugin.

When Coach evaluation suggestions are available, you can ask for additional test cases without changing your Agent. Set up a quality test suite on the **Evaluate** tab first if none exists. Review suggestions under **Pending proposals** at the top of the Agent’s **Configure** tab, edit the task, expected results and scoring, then accept or dismiss the cases you choose. If someone changes the suite, ask Coach for a fresh proposal. Saving or accepting cases does not run tests or prove that the Agent passes them.

You can also ask Coach to inspect an evaluation run you own. It can explain failed checks, missing coverage and incomplete results without changing the Agent or starting another test. A historical pass may no longer apply after settings or access change. Unknown cost remains unknown, and raw outputs are not included in this summary.

When Coach offers an operation result, open it in Canvas to check the original save, proposal or evaluation request. Other editors keep their input. The tab names the operation as its outcome allows: “Skill saved” after a successful save, “Plugin couldn’t be saved” after a failure, and just the operation name (“Skill save”) while the outcome is unknown; the result card shows its status at the time; later changes aren’t reflected there. Buttons on the card such as **Open Skill**, **Open Plugin** or **Review in Agent** open the matching editor or draft beside it; a change Coach already wrote into an Agent's draft shows **View in Agent**. **Refresh** in the toolbar reads the current status; closing the result with the tab’s × leaves ongoing work running. An unknown result still needs its supported recovery action. If access is no longer available, the view clears its content.

When Coach opens a Skill or Plugin in Canvas, you can keep it alongside your Agent draft. Returning to the draft keeps unsaved input in both editors. Save each resource separately, then review the Agent binding change. Plugin changes require a Team administrator and may affect other Agents that use it; changing its name does not upgrade its selected Skills. A save conflict keeps your input so you can compare it with the current version.

If an Agent you own has not been edited yet, Coach can establish its editing draft from the current published version before writing its changes. Existing draft edits are preserved. You can undo Coach's changes row by row under **Coach's changes**. Asking Coach to check a saved creation draft requires no field change and does not run a business test.

When Coach opens an existing Agent in Canvas, edit and save its configuration there. Switching to another open object keeps your input. If the Agent changed elsewhere, review the save conflict before retrying. The editor shows **Coach's changes**, the three configuration sections and the release-check card (including Coach evaluation requests), from top to bottom. For evaluation suites, automation, version history and personal Tool connections, use **⋯ › Open in Management** on the toolbar.

To discuss the selected Skill, Plugin or Agent, save or discard its local changes, then send a message in ordinary Chat. That message keeps its original target even if you switch tabs. If its saved version or access changed, review the original editor and send again. Undo Agent changes Coach wrote under **Coach's changes**, and confirm evaluation budgets through their existing controls; asking about a resource does not publish or run it.

If someone else changed the saved resource, choose **Reload saved version** in its editor. You can keep editing or discard only that editor’s local changes; other open objects retain their input.

After opening a draft, narrowing or widening the window preserves unsaved editor input and unsent Chat text. If saving fails, follow the saved-result check or retry shown by the page; resizing does not mean the draft was saved. An explicitly requested name should stay unchanged. Interface language does not automatically determine the Agent's future answer language; review its generated instructions before adopting them.

For short answers with one item per line, ask for the desired line format in the instructions. Chat preserves paragraph line breaks during generation and when you reopen the conversation.

## Knowledge sources

When the editor offers Knowledge sources, choose readable knowledge in the current context or selected files in the original configuration view. Select the knowledge-reading Plugin or relevant Tools first; choosing sources does not add Tools or grant file permissions. Each user of a shared Agent still needs access to its selected files.

Search and select the files to include. Removing the last file means no knowledge files may be read; explicitly choosing current-context reading broadens the scope. Optionally set the number of search results, or leave it empty for the Team default. Save the draft and review it through the original release flow. Unavailable sources appear as numbered placeholders; restore access or revise the selection before saving.

These settings govern knowledge reading and do not add uploaded task attachments to the knowledge base. Task attachments retain their original authorization.
