# Use the knowledge base

After you add material to the knowledge base, the assistant can search it for relevant evidence when answering a chat question. You can ask it to search a particular document or topic. Files you only want to store, without being searched in chats, belong in **Library → Files**; you can add any of those to the knowledge base later.

When answering from your knowledge, the assistant can use accessible source names, available section headings and short opening excerpts to find relevant material. It searches when your task needs supporting evidence; a source preview does not mean the entire document has been reviewed.

## Page layout

The knowledge base page has four kinds of content; click one to switch. They are shown as tabs above the content; in a narrow window, they become a row of buttons you can scroll sideways.

- **Files**: every file in the knowledge base. Upload, find, open, and manage files here.
- **Document matrix**: put several files into one table where each column is a question, and get an answer with its source in every cell.
- **Extracted results**: what was extracted from your files by each extraction table.
- **Templates**: built-in templates, templates you saved, templates in the team library, and extraction tables are all created and managed here.

**Add knowledge** in the page header works from any view; it switches to Files and opens the upload window.

## Choose who can see a file

Pick a visibility scope when you upload:

- **Personal**: only you can see and search it.
- **Space**: members of the current space can search it. After you switch spaces, you see that space's material.
- **Team**: everyone in the team can search it. Only team administrators can upload to the team scope.

Being an administrator of one Space does not grant Team-wide upload permission. Ask a Team administrator to add Team knowledge.

## Upload files

1. Go to **Library → Knowledge Base** and click **Add knowledge** in the top right.
2. The upload window checks whether the Team has an active embedding model. If it reports that none is configured, a Team administrator can use **Configure embedding model**; other members should ask a Team administrator. Return to the window and click **Check again** after configuration. Upload is available once the check succeeds.
3. Choose a scope, then drop or select one file at a time. To add several files, upload them individually or put them in a ZIP archive. PDF, Word, PowerPoint, Excel, CSV, Markdown, TXT, images, and ZIP are supported, up to 200 MB per file. Unsupported files inside a ZIP are skipped, with the reason shown in the import results; folders are not imported as files.
4. A message confirms the upload, and the file appears in the list and starts processing. Once its status is **Available**, chats can search it.

If you choose an empty file or one over 200 MB, the upload dialog explains what to do. Choose another file to continue.

For Excel and CSV files, only the first 5,000 rows of each file are searchable (all sheets combined). Rows beyond that are not added to the knowledge base, and answers that cite such a table say that only the first 5,000 rows were used. If you need all the data, split the table into several files before uploading.

## Add files from Files

On **Library → Files**, the **Knowledge base** column shows where each file stands in the knowledge base:

- **Added · status**: the file is in the knowledge base; see "File status" below for what each status means. Click **View in knowledge base**, **View progress**, or **Retry in knowledge base** to open the file in the knowledge base.
- **Suggested for knowledge base** or **Not added**: click **Add to knowledge base**, choose who can search it (Personal, the current space, or Team), and confirm. After it's added, **View progress** in the message takes you straight to the file.

Adding a file to the knowledge base doesn't move or copy it. It stays in Files.

The **Add to knowledge base** window checks for an active Team embedding model before you confirm. If it is missing, follow the same configuration guidance as in the upload window above; the file remains in Files.

Uploading to Files or attaching a file in chat only stores the file; classification as long-term material does not add it to knowledge. Choose **Add to knowledge base**, upload from the Knowledge page, or explicitly ask the Agent to add an exact file and specify its visibility. The Agent checks your original request and current permissions; ambiguous requests need clarification. Processing must finish before the file is searchable. Chat ZIP attachments stay as one archive and are not unpacked or indexed; use the Knowledge page to import documents inside a ZIP. **Remove from knowledge base** keeps the original file. An old addition request cannot add it again after removal; a new explicit addition can.

## File status

After the team changes its embedding model, the Files view shows a team index notice while the index is updating or if the latest update failed. This is separate from each file's processing status. On failure, a team owner or administrator can choose **Review and retry** to inspect and confirm the target; other members should contact a team administrator. The notice is hidden when no index update is pending.

| Status     | Meaning                                                                                                                                                                |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Uploading  | The file is still being transferred                                                                                                                                    |
| Queued     | Waiting to be processed; this can take a while when many files arrive                                                                                                  |
| Processing | Reading and organizing the content; large or scanned files take longer                                                                                                 |
| Available  | Processing is finished and chats can search it                                                                                                                         |
| Failed     | Processing did not finish. Hover over the status to see why; if you can manage the file, use the retry button next to it, or open the file to see the reason and retry |

**Available** means the basic content can be searched; table field extraction results may appear later. It does not guarantee that the original file's preview can be opened right now. When the page shows source status, the list shows **Paused for new tasks**, **Source check unavailable**, or **Source not checked** first; hover over the status to see when the source was last checked.

When a file fails, follow the message:

- **This file format is not supported**: convert it to one of the formats above and upload it again.
- **No searchable text was found**: check whether the file is empty or contains only images without text.
- **Processing was interrupted** or **Processing timed out**: click **Retry**. For very large files, split them before uploading.
- **The processing service is temporarily unavailable**: try again later. If it keeps failing, contact a team administrator.
- **The original file was not found during processing**: ask the uploader or an administrator to restore the original first. If you can manage the file, after it is restored use the retry button next to the status, click **Retry parse** in the row's more menu, or click **Retry** in the file details. Bowra first checks that the original can be found; if it is still missing, processing does not start and you are asked to restore the original or upload it again. After a successful retry, earlier failure messages for this file are dismissed.

## Find files

- Use the scope above the list to switch between Personal, Space, and Team. The scope is the **Scope** filter in the toolbar (on a phone, under **Filters**). Space shows the current space name, with a short note on who can search that scope.
- With the **Scope** filter, if there is an **All** option, it lists the personal, current space, and team files you can search at once, and the **Scope** column in the table shows where each file belongs. The page remembers the scope you chose last time; the first time, it shows the current space.
- Type in the search box to find files by name.
- Turn on **Deep search** and press Enter to search file content within the current scope. The note next to the search box says how many files were found and in which scope; each result shows the matching passage, labeled with where it is (page, section, or paragraph).
- If deep search finds nothing, try other words, use the button to search another scope, or click **Search by file name** to turn deep search off.
- Filter by status, type, and upload date, and change the sort order when the page offers it. Each filter button shows what you selected; to undo a whole group at once, open its menu and choose **Clear status** or **Clear type**.
- When two or more conditions are selected (the keyword, each status, each type, and the date each count as one), they are listed under the toolbar. Remove them one at a time with ×, or choose **Clear All**. Scope is not listed there.
- On a phone, **Filters** opens a panel from the bottom with Scope, Status, Type, and Date groups. Choices apply right away; choose **Show N files** to return to the list. Sorting and **Deep search** stay in the toolbar.
- After an upload succeeds, the list switches to the scope you uploaded to (it stays on **All** if you are viewing **All**), clears search and filters, and shows the first page sorted by newest upload, so you don't have to look for the new file. If files from a ZIP are still processing, click **Back to file list** to follow their status there. Once processing finishes, click **Done**; the list refreshes the same way.

If the page address changes as you search and filter, it remembers the search, filters, sort order, page, and the file you have open: the browser's Back button returns to the previous results, and refreshing keeps you in place. Sending the address to a colleague only shows these files to people who already have access; it does not grant access.

When the page remembers the address this way, the list also supports full keyboard use: press Tab to reach a filter button and Enter to open it, use the arrow keys to choose an option, Space to select it, and Esc to close the menu. Selecting and opening are separate actions: press Space on a row to select the file, and press Enter on the file name to open its details.

## File details

Click a file in the list to open its details: status, visibility, uploader, upload time, and size.

- When a file is available, click **Use in chat** to start a new chat with the file attached. This option is offered for files you uploaded in the current space; other files are still searched automatically in chats.
- If a file has been cited in chats, the number of citations appears under its name.
- When you open a file from deep search results, the matching passage is shown first and highlighted.
- In a document matrix, **View source** on a cell opens the file details right on the page with the cited passage shown first; close it to return to the matrix.
- A file that is still processing shows its progress; you can close the details and processing continues. If processing failed, the details explain why and you can **Retry** right there.
- If you have permission, you can also change visibility, extract the file to a table, or delete the file from the details.
- If the preview can't be opened, the details explain why. When **Retry Preview** is shown, you can retry without closing the details. If the file content can be read but can't be displayed right now, you can download the original instead. If you don't have access or the file is no longer reachable, check your access or contact the person who manages the file.

## Check a file's source and restore the original

When the file details offer **Source check**, click **Check source** to see the current state of the original and its index. The check does not repeat on its own; click **Check again** for a fresh result. The result has three parts:

- **Original file**: if the original was found, that still doesn't prove it matches the indexed content exactly. If the check ran normally but the original wasn't found, contact the uploader or an administrator. If the original can't be checked right now, there may be a storage, network, or access problem; this alone doesn't mean the file is lost.
- **Index**: the number of chunks matches the record and a sample is readable. This doesn't guarantee that the full text is complete or that every citation can be checked.
- **Source correspondence**: if it says the index has not been verified against the current original, the index can't yet be proven to come from this original. Check each citation against the source text.

Checking does not reprocess the file; check again after the file changes. The page may also say whether the file can be used for new tasks. If the original is temporarily unavailable, results use the previously verified index and include a notice. If the file is paused for new tasks, follow the explanation in the details first instead of rerunning the matrix.

If you can manage the file and the page offers recovery actions:

- **Restore original**: choose a file with exactly the same content as the original. It is checked again after restoring; it is not reprocessed and uses no model credits.
- If the content is different, upload it as a new file when prompted. The old file and its earlier citations are kept.
- **Reparse and verify**: processes the original again and rebuilds searchable content. This can use model credits; read the explanation on the page first.

If a source changes while a task is running, related new results may be paused. Your manual edits and checked marks are kept. If the original is briefly unreachable but previously verified content can still be used, results carry a notice. The notice does not mean the source text has been checked.

## Rebuild several files

When the page offers **Rebuild selected files…**, select the files you can manage on the current page of the file list, then choose it. Review the number of files, their size, and the model usage note, then confirm. Each batch holds up to 20 files and 100 MB in total, processed one at a time. This rebuilds searchable content only; it does not re-extract tables.

Progress is shown in one place on the page instead of a message for every file. After refreshing, you can still see your tasks in the current team and space. If a task is interrupted, restore access or fix the source problem first, then click **Check access and resume**; Bowra does not keep retrying on its own. After **Stop remaining files**, a file that has already started may still finish, and model requests already sent may use credits.

Earlier matrix values and citations are not recalculated automatically when a source has problems. If you lose access to a file, its name, source text, and matrix values are hidden; they reappear when access is restored.

## Ask the same questions across many files with a document matrix

1. Click **Document matrix**, then **New matrix**, and pick the files to include. You can also select several files under Files and click **Open as matrix**.
2. Add a question column (for example "What is the contract amount?"), choose the answer type, and run it. Each row is a file, and each cell is that file's answer to the question.
3. Click a cell to see the answer and its source. You can edit the answer or mark it as checked; **View source** opens the file details on the same page with that passage first.
4. Runs use credits based on document length; you see an estimate before a run starts.

### Read the statuses in a matrix

- **Not run**: the question hasn't been processed yet.
- **Processing**: the task is in progress.
- **Failed**: open the cell to see why, then decide whether to retry.
- **No value extracted**: the question was processed but no result was extracted. Check the source text; this doesn't prove the answer is missing from the file.

A matrix groups its tasks in one task summary; expand **Run history** to see each task. Reopening a finished matrix doesn't show old success messages again. If something failed, the summary keeps the number of items to handle; expand it and choose **View failed**. For long column names, click or focus the column header with the keyboard to read the full question; the full question also appears when you open a cell. On a phone, tap a cell to see its details and close them to return to the matrix.

After you switch spaces, the matrix checks your access in the current space again. If the matrix isn't available in that space, go back to the list and choose another matrix, or switch back to the original space; once access is restored, click **Retry**.

A set of question columns you use often can be **saved as a template** from the matrix column settings, then brought in with **Use as matrix** under Templates to add all the columns at once. Templates you create can be sent to the team library with **Publish to team library**, so members of other spaces in your team can use **Copy to my space**. Only the question columns are published; files and answers never leave your space.

## Turn fields in your files into a table with extraction tables

When you have a set of similar files (for example monthly income statements or contracts), create an **extraction table** that lists the fields to pull out of them (for example revenue and net profit). Each file added to the table is extracted into a row with those fields. You can view the results in the knowledge base and query them directly in chat.

1. In the knowledge base, click **Templates**, then **New template**, and choose **Extraction table (extract on upload)**. Describe the fields in a sentence, review them, and save.
2. Click **Extract to table** in the file details or in the list's more menu, and answer "Which table should this file be extracted to?". You can also select several files and extract them together. When uploading an Excel file, you can pick the table under "Extraction table" right away.
3. When extraction finishes, open **Extracted results**. Every cell links back to the source text.
4. To stop extracting a file, choose **Don't extract** under **Extract to table**. The file stays available to full-text search.

Extraction is queued within a daily quota, so it can take a while when there are many files. "Template" only means a reusable set of question columns in a matrix: save a matrix as a template, then apply it to a new matrix.

## See sources in answers and reports

When an answer or report uses documents from the knowledge base, the related sentence carries a citation number. Open it to check the source text.

### In a chat

Where direct knowledge reading is available, the assistant handling your task can look up documents and continue its other work. You can still choose a research Agent explicitly. If repeated searches find no new evidence, the assistant stops searching and explains the gap; this does not mean the knowledge base is empty.

When a chat answer uses knowledge base content, the related sentence gets a citation marker with a small document icon. Its number continues the same sequence as data citations.

- Click the marker to expand a source card under the sentence: file name, location (page, section, or paragraph), and the passage, with the part related to the answer highlighted. Click the marker or **×** again to close it. A marker inside a table opens its card below the table. Source cards can be opened once the answer has finished.
- Click **Open in knowledge base** to open the file details with the cited passage shown first. If the file was deleted or you don't have access to its space, the knowledge base page shows a notice that the file couldn't be opened.
- If the file was processed again and the cited passage no longer exists, the card says the source has been updated; open the knowledge base to see the latest content.
- If you don't have access to the file, the marker is dimmed and only says the source is restricted, without the file name or content. If the file was deleted, it says the source is no longer available.
- If the file was processed again but the cited passage can still be read, the card marks it as coming from an earlier processing result. It is not replaced with a new passage automatically.

### In a report

- Hover over a citation number in the report body to see the file name, location, and passage. Click **Open in knowledge base** to go to the file with the cited passage shown first.
- In the **Data sources** section at the end of the report, knowledge base entries also show the file name, location, an excerpt, and a link to open the file.
- What you can see depends on your own access to the file, not on the access of the person who wrote the report. When a report is shared, colleagues without access to the file see only **Restricted source** and "Only members with access can see this source", without the file name or text.

### When a source can't be checked right now

When an answer or report carries a notice that some originals are temporarily unavailable, the result uses content that was verified earlier; check the conclusions once the originals are reachable again. Such notices in a report body are saved with the report; keep them when you export the full report. Reopening a citation reads the source text with your current access; without access you can't view the source text through a citation.

## Delete files

Deleting from the file details takes effect as soon as you confirm and cannot be undone. When you delete selected files from the list, or use the row menu, a message with **Undo** appears at the top of the screen after you confirm and stays for at least 8 seconds; choose **Undo** and the files are not deleted and stay in the list. Deleted material is no longer searched in chats.

## Problems when uploading

The upload dialog tells you why a file was not uploaded. The dialog stays open and keeps the scope you chose.

If either window cannot confirm that the knowledge base is ready, click **Check again**. If the check still fails, ask a Team administrator to review the Team model settings. No file is uploaded or added while the check is unresolved.

- **"Can't be added to the knowledge base"**: the file format is not supported. The dialog lists the supported formats; you can export the file to PDF and upload that instead.
- **"The upload was cut off in transit"**: the connection was dropped partway through the transfer, usually because your company's network gateway limits upload size. If small files work but large ones always fail, ask the administrator who runs the deployment to raise the limit. If it happened only once, click **Retry** to upload the same file again.
- **"Network connection failed"**: the upload failed before any of the file was sent. Check your network connection and click **Retry**.

You can click **Cancel upload** to stop an upload in progress.

If an upload cannot confirm your sign-in information, the dialog shows a failure instead of loading indefinitely. Choose **Reload sign-in information** to read it again. Upload stays unavailable until your identity is confirmed; this message alone does not mean you lack Space access.

If no relevant evidence is found after a bounded search, the answer explains what remains unverified. This does not mean the knowledge base is empty. You can provide a document name or a more specific question to narrow the search.
