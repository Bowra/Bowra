# Revise an existing report

When the canvas already has a report, you can keep asking for changes in the same chat, and Bowra revises the existing report instead of writing a new one.

New report attribution uses the Community name, Bowra. Existing saved reports and text supplied by you keep their original content.

Some deployments don't revise reports in place yet: if a new report appears on the canvas after your follow-up request and the original moves into “N earlier reports in this session”, the current deployment writes a new report each time and the approach below doesn't apply. In that case, state the full scope and content you want in your request.

Common requests:

- Change the scope: “Change the report to cover January to June 2026.” The body, key metric cards, and charts are updated to the new scope together, and report and chapter titles that mention the old scope are changed as well.
- Change only one part: “Change only chapter 3 to January to June and leave the rest.” Chapters you didn't mention and the overall conclusion at the end stay as they are; if they no longer match the new scope, the reply tells you where.
- Change a chart: “Keep only the top 10 in the store ranking chart” or “Remove the monthly trend chart.”
- Add content: “Add a section comparing revenue by city.” New content is added after the existing report.
- Write a separate report: “Leave the original report as it is and write a separate one for January to June.” The original stays on the canvas and is not collapsed; later changes apply to the newest report. When you export the canvas, both reports are included.

## Edit text and use AI rewrites

Choose **Edit this report** to edit text directly. Select a passage or open a prose block’s AI action, then choose a rewrite option or enter an instruction such as “Expand the explanation using only these facts.” On a phone, open the prose block’s **More** menu and choose **Edit with AI**.

The highlighted candidate is a preview. Choose **Accept** to replace the original, or **Revert** to keep it. **Stop** cancels generation. Accept or revert before finishing editing, exporting, or leaving the report. One Undo restores the accepted passage. If the selected text changes while rewriting, select it again instead of applying the old candidate.

Select text within one prose block, excluding charts, tables, and code. Interrupted or empty responses preserve your original; retry or close the message and continue editing.

Expansion explains the selected text; it does not fetch new facts or recalculate chart data. If numbers, dates or source references change, a notice asks you to compare the candidate with the original before accepting. Formatting changes can also trigger this notice; its absence does not prove the content is correct. Accepted line breaks remain visible after saving and reopening. To change a chart's data or analysis, ask in Chat or use the chart's **Ask about this chart** action when available.

## Where replaced or removed charts go

When a new chart replaces an old one, or you ask to remove a chart, the old chart is not deleted permanently. It moves to the “charts tucked away” section at the end of the report; expand it to see those charts again.

## Notes

- If an AI rewrite reaches the model output limit, the incomplete rewrite is removed and your original passage is kept. Select a shorter passage and try again.

- Hover, pinned details and ordinary Tables show readable unabridged values: money uses up to two decimal places, other decimals up to four, and declared percentages up to two. Unnecessary trailing zeroes are removed. Small nonzero values remain visible; an explicitly requested integer display rounds to integers. Known monetary scales are converted to the base unit: 0.0009 hundred-million yuan reads as 90,000 yuan, with the Table unit stated in its header. Source downloads retain the supplied data. If an integer is too large to plot exactly, use Table to retain its exact digits.
- You can request an explicit number of decimal places (up to 12), with fixed trailing zeroes or a maximum precision. This changes how available numbers are read; it does not recompute the analysis or recover digits rounded by the source. A missing percent shape or currency remains unspecified rather than being guessed from a column name. Cards use readable magnitudes; axes preserve the differences in their plotted ranges. Saved views keep known precision and currency through normal settings edits and reload.

- Reported events and partial-period qualifications appear only when the source explicitly supplies their dates and applicable measure. Open the remark to check its original values and cited source. Missing rows alone do not imply incomplete reporting.
- Beside an available time window or frame selector, choose **Enter date** to type an existing period. Use the displayed format. An unknown date or reversed range leaves the view unchanged; choose **Apply**, press Enter, or cancel with Escape.
- Eligible Tables can show each entity’s start/end values, absolute change and a small trend. Rows use the same periods and scale; missing endpoints remain missing, and percentage changes are in percentage points. Activate a trend to read that entity’s original rows. Use the existing More menu to switch between **Show original rows** and **Show trends**. Sorting and drilling are temporary; canonical report export uses the authored summary, while CSV follows the displayed table.

- In compositions with supported original-observation bindings, click a mark, Table row or annotation caption to keep that row's original values open. Each measure retains its own unit. Narrow screens use a bottom detail sheet; press Escape or Close to return. Sorting preserves the original row identity; scope or revision changes may close the details.
- Reading steps linked to an existing annotation open the same details. Interval annotations show supplied endpoint values without calculating a period total. Details show the selected measure’s definitions; a remark with an explicit citation shows its cited source text. This does not establish causality. Asking a follow-up remains a deliberate action; browsing or dismissing details does not save a report edit.
- New compositions use equal-width columns for peer analyses. A primary chart with supporting detail can use a wider main column. Narrow canvases stack views in reading order. To change an existing composition, open its chart settings in edit mode and choose the layout; save to apply it.

- For a new presentation from confirmed content, request an editable PPTX and specify the slide count and content. A successful reply includes a downloadable file card; download it there or from Results. An additional Canvas report is not required.

- Standard PPT downloads follow the report content instead of filling a fixed seven-slide sample deck. Long text and tables continue onto additional slides, and chapter headings appear on divider slides. If the standard fallback cannot reproduce a chart, it includes a data table and an explanation; unavailable images or interactive content are marked in the deck. This is a paginated presentation, not a pixel-identical copy of the canvas.

- For ordinary PPTX template requests, upload the PPTX and data in chat, identify the template and describe the changes. Where available, the **+** (Open…) menu in the Canvas tab bar opens current and earlier drafts in the common Canvas for page review, question instructions and explicit publication confirmation. Closing only closes the view; reopen from the same menu. Published source/PDF files use the ordinary Results controls. Saved drafts and published files are retained. No generic replacement deck should be delivered instead. Complex charts and embedded objects are not supported. There is no default-template setup in Personal Settings → Data Export; attaching a PPT does not automatically make it the report's export template. A template-derived PDF comes from its PPTX, while report PDF downloads remain separate report snapshots.

- You can identify the template by filename or upload order, for example: “The first file is this quarter's data; the second is the template. Keep its layout and prepare PPTX and PDF for review.” When several files are grouped together, name the exact template instead of relying on an ambiguous number. To change templates, upload the replacement and explicitly request a new draft. Confirming a draft publishes the files you reviewed; it does not generate a different presentation.
- After publication, download the editable PPTX or corresponding PDF using the file controls. Repeating a download does not regenerate the document. If a download fails, follow the displayed advice before retrying; a removed or changed source may make its output unavailable. Check your browser's download list to confirm the file was saved.
- Before confirming a template draft, check both the change summary and every preview page. The PPTX keeps its font settings, but PDF conversion may use a replacement when a font is unavailable; open the font notice to see the substitutions. Do not confirm if the appearance or numbers are wrong. Previewing or answering a clarification question does not publish the draft.
- For files already attached to this chat, identify the template by name and restate the changes in your current message; you do not need to number old attachments. “Accepted” means the request was received, not that the files are ready. A definite refusal of a new request creates no draft. A conflict with an existing request does not replace that request. Do not submit another request merely because the status is uncertain or the review controls are missing.
- The reply says honestly what changed in this round and which charts were tucked away. If an old chart is still in the report, or a key metric card still has a number that wasn't updated to the new scope, the reply tells you that too.
- When you change only one part, the summary conclusion at the end isn't updated. If you want the conclusion updated to the new scope as well, say so explicitly.
- If the part you name doesn't match the report (for example, you ask to change chapter 5 of a four-part report), Bowra asks which part you mean instead of picking one on its own. Naming the chapter is the most reliable, for example "change only the category chapter".

- Where a document draft offers an edit action, selecting it puts the draft target beside your instruction in the input. Check the target before sending. Existing unsent content is kept and must be handled before another target can be added. Removing the target keeps your text; the next message is then an ordinary request. Closing the draft view does not cancel or publish it. If sending is awaiting recovery, check that result before starting another request. The input may show the saved instruction and exact draft identifier while editing is locked. Switching the open Canvas does not change this target.

- To revise a private document, open the draft from the **+** menu in the tab bar, then choose **Revise in Chat** when available. Describe your changes in the existing input and send. Switching or closing document Tabs does not change the input target. The new draft still needs review and separate publication confirmation. After you send, a receipt under your message shows which draft the new version is based on; when it is ready it reads **Draft N is ready** with **Review in Canvas**. The original draft is always kept.

- When supported by the document workflow, a ready draft opens in the same Canvas after an access check. If you are viewing another object or writing the next message, the draft stays in the background. Select it when ready, or use the **+** menu in the tab bar to reopen it. Opening or reconnecting never confirms publication.

- If a template request asks a clarification question in chat, answer through that question's existing controls. The same request continues using the retained template and data; you do not need to upload them again. If information is still missing after that answer, the task stops and says so without publishing files. A ready draft still requires your separate review and confirmation. Skipping the question ends this request; it will no longer wait for an answer. If access or the required model configuration changes, the request stops without replacing its saved files.

- State the selected title or value clearly when answering; you may include an explanation in the same answer. In the draft, check that only the intended text appears in each field and that every requested change is present. A valid source reference does not guarantee the right business interpretation. If the template has no place for a requested field, clarify its destination instead of approving a draft that omits it.

- You can ask to inspect the template before asking you a question or preparing a draft. If that inspection cannot complete, the request stops instead of skipping ahead. A written claim that inspection finished is not proof that a draft exists. Answer an actual question through its existing controls; answering does not repeat a completed inspection or publish files.

- If Bowra cannot determine how to use your PPTX, say whether you want read-only analysis or edits that preserve its layout, then send again. If the failure says the model's routing response could not be validated, retry the request; if it happens again, give your admin the diagnostic reference. A routing failure does not execute the proposed file action or publish a draft.

- If the original request or attachments become unavailable during this check, check your access to the files and attach them again if needed before resending. The old proposed file action will not continue on changed inputs.

When requesting a new PPTX, specify the points for each slide. Supplied list items remain separate editable bullet paragraphs in the presentation.

## Read on a phone and verify values

A link to a report, file or code result opens Results directly on a phone. Choose Conversation to return to the chat. In compact report reading, use **More → History** for earlier revisions; desktop History remains beside the report.

- On a narrow chart, fewer dates or numeric labels may be printed to leave room for series identity. All observations remain available through the existing exact-value details and Table. A visible legend identifies lines when endpoint names cannot fit.
- Shared time-window controls appear before the charts they affect. The range explanation states which views change and what the export includes.
- When source values use a different scale, the table explains its displayed unit and the source unit. For example, a source value in ten-thousand yuan may appear as a full yuan amount in Table. Source inspection preserves the supplied values; a missing conversion note does not prove that unspecified units are equivalent.
- Use **Show all rows**, then **Collapse**, to return from a long table without fetching the same rows again. Interactive remarks offer **Details**; keyboard instructions appear when an interactive chart receives keyboard focus.
- In manual editing, **Done** saves pending input, including text that still has focus. A failed save keeps editing available. Browser refresh/close protection also includes pending input and saving; wait for **Saved** before leaving.
