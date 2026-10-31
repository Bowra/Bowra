# Drag and drop, shortcuts, and hints on the conversation page

This article covers drag and drop, shortcuts, and hints on the conversation page. For the layout of the page itself, see [Conversation page layout](conversation-workbench.en-US.md).

## Compact a long conversation

Open the conversation title’s **⋯** menu and choose **Compact context** when the conversation is idle. Bowhead summarizes eligible older material for the next message and keeps the original conversation visible. A quiet activity line shows progress and the result; the existing Stop control cancels this operation. Your unsent draft stays in the composer.

Manual compaction works even when the model’s **Auto-compact** setting is off and does not change that setting. It may use model credits. If there is nothing useful to reduce, no summary call is made. Wait for active work or pending decisions to finish; dedicated recovery flows may be unavailable.

If the connection is interrupted, Bowhead checks the original request when you reopen the conversation. Select **Compact context** again to check it or explicitly retry the same saved request. A missing or unreadable saved request cannot be resent automatically. Review the operation’s activity before starting another task.

## Choose an Agent, method or result

- When quick references are available, type `@` to choose an Agent for this message, or `/` to choose a Skill, Outcome or tool. Select an available item from the menu to add its tag.
- An **Outcome** describes the result you want delivered. A **Plugin** groups Tools and Skills for an Agent; it is managed with the Agent's resources. Selecting an Outcome does not add a Plugin.
- You can open a selected resource tag to edit its parameters when available. Your choices apply to this message; they do not change the Agent's saved configuration.

## Drag files into the conversation

- Drag files from your computer onto the conversation area (the column with the messages and the composer) and a dashed frame appears. When it says **Drop to add to this message**, release the mouse: the files are added to the composer as attachments and start uploading, just as if you had selected **+** at the bottom left of the composer.
- If the files can't be added, the frame turns red and says why, for example everything you're dragging is a video, audio, or another unsupported type, the message already has too many attachments, or attachments can't be added right now. Releasing then adds nothing.
- When you drag several files at once, you can drop as long as some of them can be added: those upload as usual, and any unsupported file shows up in the composer as an attachment with the reason, which you can remove.
- A message can have up to 5 attachments. Regular files can be up to 2 GiB on deployments with object storage, or 100 MiB on deployments with local storage. Word and PowerPoint packages are limited to 32 MiB, and ZIP archives to 500 MiB. You can add spreadsheets, PDFs, documents, images, text, and other common formats.
- Select a Space before adding files. Wait for uploads to finish before sending. Switching Space, conversation or account stops unfinished uploads; add the files again in the destination conversation. Files already attached belong to their original draft.
- You can also drop files while an answer is running; they're attached to your next message.
- The results area and reports don't accept dropped files. Dragging inside a report still reorders its sections.
- ZIP contents count toward a separate limit of 100 files per message. Split larger archives or remove a bundle before sending. If indexing progress cannot be refreshed, the attachment shows an error that you can remove or retry.
- Before sending a ZIP, open **Bundle Contents** with the down arrow on its attachment. Failed files come first and skipped files say why; more than six indexed files collapse into "N more indexed", and files still indexing are only counted on the attachment. On a phone the list opens from the bottom of the screen; choose **Done** to close it. The list remains available when indexing finishes.
- When an attachment fails to upload, the attachment itself only says **Failed**; the full reason shows below the composer, with **Copy diagnostic information** for your administrator.
- Each attachment shows a line icon for its type, with the extension and size after the name; a long name is shortened first.
- Adding the same file again (same name and size) doesn't create a second attachment: the existing one is briefly outlined, and screen readers announce "This file is already attached".

## Change the width of the conversation and results

- On a computer, drag the divider between the conversation and the results.
- Or use the keyboard: press Tab until the divider is focused, press the left or right arrow key to change the width a little at a time, press Home to make the conversation as narrow as possible, and End to make it as wide as possible. Screen readers announce the share the conversation takes.
- Double-click the divider to go back to the default width.
- The width you choose is remembered in this browser on this computer.

## Shortcut to enlarge the results

- Press ⌘⇧\ (Command + Shift + backslash) on a Mac, or Ctrl + Shift + \ on Windows, to enlarge the results to the whole workspace. Press it again to restore. The tooltip of the enlarge button at the top right of the results also shows the shortcut.
- While enlarged, a narrow strip stays on the left; select it to go back to the conversation. Esc doesn't restore the layout; it closes menus and dialogs.
- ⌘\ or Ctrl + \ without Shift still collapses or expands the navigation sidebar.
- Here \ means the key where the backslash is on a US keyboard. On other keyboard layouts that key may show a different character; in browsers that can read the keyboard layout, the enlarge button's tooltip shows the character on your keyboard, otherwise it says "the \ key on a US keyboard".
- The shortcut doesn't work while a menu, list, dialog, or popover is open; close it first.
- The shortcut works on wide computer screens when there are results. Safari uses ⌘⇧\ for **Show All Tabs**; if pressing it in Safari opens the tab overview, use the enlarge button instead.

## The dot on Results

- While you stay on the conversation and a new report section, data table, or file appears in the results, a small dot appears on **Results** at the top on phones, and on the collapsed **Results** strip on the right on computers. Screen readers read it as "new content".
- The dot goes away once you open the results. If you refresh the page before looking, the dot is still there.

## Share a turn with an Agent's owner as an evaluation case

When a team Agent that someone else owns answered your question, the answer's **⋯** menu, next to **Copy**, offers **Share with the owner as an eval case**. Sharing lets that Agent's owner turn this one turn, your question and the answer you received, into a test case that checks the Agent's future answers. Nothing else from the conversation is shared, and the owner can't browse your other conversations.

- The owner sees your name and the start of your question in their list. They read the answer only when they draft a case from it.
- To withdraw, open the same menu and choose **Stop sharing with the owner**. The turn leaves the owner's list. A case the owner already drafted from it is their own copy and stays in their evaluation suite; ask them directly if you want it removed.
- Sharing also ends on its own if you delete the conversation or leave its Space.
- The menu appears only for team Agents you don't own. Answers from built-in Agents, or from Agents you own, have no menu; your own conversations are already available to you.

## Mark a team Agent's answer as bad

Answers from a team Agent have a thumbs-down button next to **Copy**. Use it when an answer was wrong or unhelpful:

1. Select the thumbs-down. A small dialog opens. Everything in it is optional; submitting without filling anything in still records it.
2. Tick any reasons that apply: **Facts or numbers are wrong**, **Didn't answer my question**, **Left out something important**, **Too brief**, **Too long-winded**, **Hard to read or badly formatted** or **Other**. Add a comment of up to 500 characters about what you expected.
3. When someone else owns the Agent, the dialog also shows **Allow the Agent's owner to use this turn as an eval case (the owner will see the question, the answer and your feedback)**, with the owner's name. It is unchecked unless you already shared this turn. Check it only if you want the owner to see this turn: it is the same consent as **Share with the owner as an eval case** in the **⋯** menu, and the owner then also sees your reasons and comment. Unchecking it for a turn you already shared stops sharing.
4. Select **Submit**. The button stays highlighted and a message confirms it, with **Undo**. Undo takes back the thumbs-down and a share you just added.

- To take the thumbs-down back later, select the highlighted button again. This also stops sharing the turn with the owner; you can share it again from the **⋯** menu.
- Without the checkbox, your thumbs-down stays private to you: the Agent's owner can't see it.
- Answers from built-in Agents have no thumbs-down.

## Why Send isn't available

- When Send can't be used yet, a line of small text under the composer says why, for example **You can send once the attachments finish uploading**. You can send as soon as the reason goes away.
- Screen readers also read the reason on the Send button.
- Only one notice shows below the composer at a time: errors such as a failed send come first, then conflicts that block sending, then general guidance. The rest wait behind **N more notices**, which lists each one with its fix, such as **Remove attachments** or **Send as new request**, **Remove N extra attachments** when there are too many, **Connect** (opens My Connections) when Google Drive isn't connected, and **Retry saving** when the draft couldn't be saved yet. When one is resolved, the next appears.
- After **Revise in Chat** from the canvas, a bar above the text shows **Revising** with the draft's file name and number (for example, **Revising Q2 Review.pptx draft 2**); select the file name to open that draft in the canvas, or select × to stop revising and ask an ordinary question instead.
- Opening or closing Canvas may briefly pause the input while its draft finishes saving. If draft recovery stays paused, keep the page open and select **Retry draft recovery** once local storage is available. This preserves the final edit, completed attachments and selected references; retrying recovery does not send the message. An unsaved edit may be lost if the page is closed or reloaded before storage recovers.

- An attachment refused before your task starts stays in the composer so you can remove or upload it again. Keep the original Agent and methods selected when correcting the attachment. A rejected tool attempt may appear as failed even if a later corrected attempt completes the task.

If another window changes the draft while a file is uploading, the upload can still finish. Review and resolve the draft conflict before sending. Keeping your local draft preserves its completed attachments; choosing the other draft replaces your local input and attachments.

The input draft contains your message, attachments and selected references. **Draft saved** in the Agent editor refers to that Agent configuration, not the message you are preparing. Background availability updates do not merge another window's draft for you; review any draft conflict before sending.

## Waiting for a question or document result

If a question is not ready to continue yet, keep your answer and try its original control again shortly. The waiting notice does not mean your answer has been saved. If document planning ends without a complete result, read the known reason and usage status in the document editor. A stopped task may have no open question to answer. No files have been published; editing and sending a new request is a separate action.

Answer a task's question using its original card. When the task can retain completed subtask work, your answer continues the same task without rerunning that saved work. If the original files or access are no longer available, continuation can be refused; check the message before starting another request. Repeated clicks do not replace an answer that has already been saved.

For tasks that retain completed subtask work, if your answer was saved but the task cannot safely continue, reopening the conversation still shows that limitation. Review the work already completed and restore access to required files when possible. Start a new request only after checking the original result; refreshing does not repeat the task or replace your saved answer.

For an available PowerPoint template workflow, attach the template once and identify it in your message. You can give replacement values directly in the message or clarification answer; a separate data file is optional. Review all proposed changes before publishing, especially which title, footer or table value will change.

To create a new presentation instead, say that the attachment is only a style or content reference; this does not preserve its existing slides. If template updating is unavailable, the assistant should explain that limitation, not silently deliver a rebuilt deck. A template draft's PDF comes from its corresponding reviewed presentation, not a separate report.

If you want inspection or a question before editing an uploaded template, say so in your message. Answer any clarification using its original question card, then review the draft in Canvas and confirm it before publishing the editable PPT and corresponding PDF. Check that the assistant followed your requested steps and preserved the template; a generic file or a completed chat reply is not the same as a reviewed template draft. If you only want a summary or a new reference-based presentation, state that instead.

Selecting fewer pages does not remove all undisplayed template content from the editable file. Use a template suitable for sharing.

If the preview reports font substitutions, opening the PPTX on another computer or in another presentation app can change fonts and line breaks. Check the editable file in the app where you intend to use it. The published PDF preserves the layout used in the reviewed preview.

The assistant may automatically correct a failed template edit once. If it still fails, the task stops without publishing. Narrow the requested changes or specify the pages and values in a new request.

If a draft says its changes could not be compared reliably, review every preview page. This does not mean the document has no changes. You can still request a revision in chat before confirming publication.

## Report rewrite drafts and retry

- When you ask AI to rewrite a section or selected text while editing a report and close the instruction box halfway (with ×, by clicking elsewhere, or with Esc), the instruction you wrote is still there the next time you open it on the same section. Instructions are kept separately for each section of each report and are cleared once sent. They aren't kept after you refresh the page.
- If a rewrite fails, the message bar has **Retry**, which rewrites the same text with the same instruction again. If that text was changed in the meantime, the bar simply closes; select the text you want to rewrite again.

## Keyboard use in reports

- While editing a report, select some text and press ⌥⏎ (Option + Return on a Mac, Alt + Enter on Windows) to move focus to the formatting toolbar above it. Use the Left and Right arrow keys to move between buttons, and Enter or Space to use one. Press Esc to go back to the text; the text you selected stays selected. This doesn't work while an input method is still composing text.
- When the section menu is open, use the Up and Down arrow keys to choose, Enter to run the action, and Esc to close it. You can also move onto unavailable items, and screen readers read out why they're unavailable.
- With focus on a citation marker, press Enter to open that source. When focus is in the source card, press Esc to close it and return to the marker. The source line under a chart also opens with Enter or Space and closes with Esc.

## When a turn cannot finish

The failure card explains the recorded reason, such as a model timeout, too many requests, an interrupted response, or an execution limit. Follow the action shown. Model authentication failures need a team administrator to check access; include the copied diagnostics. Credit failures identify whether your allowance or the team's allowance needs attention.

If the card says a system error interrupted the turn, try its **Retry** action. If it happens again, expand **Technical details**, copy the diagnostic information, and send it to your team administrator so they can locate the failed turn.

A completed step does not mean the entire request finished. Review the work already visible before sending again. If the task cannot continue and you choose **Edit and send**, sending starts a new request; it does not resume the original task. If the failure stage was not recorded, the card says so.

If a step has no safe, specific error to display, it says that the step failed. Review the step information shown; if Technical details are available, you can copy the recorded diagnostics. The failure card distinguishes a service interruption from a result that could not be saved completely. A model stage appears only when it was recorded. Share the copied diagnostics with your administrator if the problem recurs.
