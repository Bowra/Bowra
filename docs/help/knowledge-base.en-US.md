# Use the knowledge base

After you add material to the knowledge base, the assistant can search it for relevant evidence when answering a chat question. You can ask it to search a particular document or topic. Files you only want to store, without being searched in chats, belong in **Library → Files**; you can add any of those to the knowledge base later.

When answering from your knowledge, the assistant can use accessible source names, available section headings and short opening excerpts to find relevant material. It searches when your task needs supporting evidence; a source preview does not mean the entire document has been reviewed.

When you reopen a chat, document references are checked against your current access. Revoked or removed sources no longer provide an accessible reference or file name.

## Source discovery descriptions and categories

Open a ready file's details to see its source discovery description and categories. These help the assistant decide where to search; they are not evidence that the whole file has been reviewed. A permitted file maintainer can write a manual description, assign up to eight categories, or maintain categories and aliases in the file's original scope. Categories have at most two levels. Use category search when the initial list does not include the category you need.

A manual description takes priority. Clearing it and saving restores any still-valid automatic description. When the source changes, review and re-enter an outdated manual description; previous text is not republished automatically. Unsaved drafts remain intact while generation status updates.

Automatic descriptions are optional and disabled by default. A Team owner or administrator can select **Source descriptions** on **Team settings → Models → Assignments**. New searchable source publications can then generate a short description from bounded excerpts. Saving this setting does not process older files. A file maintainer can explicitly choose **Generate description** for a current published file; this consumes the selected model's credits. Failed generation leaves the file searchable. Refresh the file before an explicit retry, especially if the previous model request's outcome is unknown.

In a document matrix, edit a column and set its **Source discovery purpose** to **Description** or **Topics** to reuse eligible existing results. This setting does not run extraction. Reuse remains limited to the current Matrix's permissions and source versions; edited or outdated cells do not qualify. It does not publish the Matrix text as the file's shared manual description.

The assistant receives a small directory limited to the current user, Space and Agent's allowed sources. It can search a paginated source directory when coverage is unclear and read original evidence when the task needs it. Missing matches do not establish that a document or topic is absent. Unrelated tasks have no mandatory knowledge-base step.

## Page layout

The knowledge base page has four kinds of content; click one to switch. They are shown as tabs above the content; in a narrow window, they become a row of buttons you can scroll sideways.

- **Files**: every file in the knowledge base. Upload, find, open, and manage files here.
- **Document matrix**: put several files into one table where each column is a question, and get an answer with its source in every cell.
- **Extracted results**: what was extracted from your files by each extraction table.
- **Templates**: built-in templates, templates you saved, templates in the team library, and extraction tables are all created and managed here.

**Add knowledge** in the page header works from any view; it switches to Files and opens the upload window.

When knowledge bases are enabled, the four tabs become **Knowledge bases / Document matrix / Extraction tables / Templates**; see the next section.

## Organize and share material in knowledge bases

Administrators are rolling this feature out gradually. When knowledge bases are enabled, the first tab on the Knowledge page is **Knowledge bases**. Without that tab, the page works as the other sections of this article describe.

When it is enabled, material is organized in named knowledge bases. Each knowledge base has a name, a description, a place where it lives, who can use it and a member list; except for the Space and Team default knowledge bases, it also has an owner. A file belongs to exactly one knowledge base; to put it somewhere else, move it to another knowledge base.

### Where a knowledge base lives

- **My**: only you can use it, in all of your Spaces. Everyone has a **My knowledge**, and you can create more to organize your own material. My knowledge bases can't be shared; to share material, move the files to a Space or Team knowledge base.
- **Current Space**: used only in this Space's conversations, Agents and document matrices. Every Space has a knowledge base named after the Space.
- **Team**: usable in every Space of the Team. Every Team has a **Team knowledge base**.

My knowledge, the knowledge base named after the Space and the Team knowledge base are default knowledge bases created by the system and marked **Default**. They can't be renamed, given another description, deleted or transferred, and who can use them is fixed: Space members can edit a Space's default knowledge base (viewers can only use it), and everyone in the Team can use the Team knowledge base. People who can manage them can still give individual people more rights on the member list, for example to let a member add files to the Team knowledge base.

As before, document matrices accept files from Space and Team knowledge bases only, not from My knowledge bases. Once your administrator enables matrices visible to only you, your own such matrices also accept files from your My knowledge bases; see “Matrices visible to only you”.

### Who can use it, and the three access levels

You can use a knowledge base because its **Who can use it** setting includes you, because you are on its member list, or because you are one of its automatic managers. When several apply, the highest level counts.

- **Who can use it**: a My knowledge base is only for you. A Space knowledge base is either **Members only** (only the people on the list) or **All Space members**; for the latter, you also choose whether Space members can use or edit it by default. A Team knowledge base is either **Members only** or **Everyone in the Team** (use only).
- **Member list**: you can add people and groups; a Team knowledge base can also list Spaces, meaning the members of that Space. A Space knowledge base accepts only members of that Space; to share material across Spaces, create a Team knowledge base. When someone leaves a Space, their entries on that Space's knowledge bases stop applying, and apply again if they rejoin.
- **Automatic managers**: Space admins can manage every knowledge base in their Space. The Team owner and Team admins can manage every Team knowledge base and, once they join a Space, that Space's default knowledge base. Automatic managers can't be removed from the list. The Team owner and Team admins don't see a Space's members-only knowledge bases because of their Team role.

The three access levels:

- **Can use**: search and cite in conversations, Agents and document matrices; view, preview and download files; check sources.
- **Can edit**: also add files, remove or delete the files they added, maintain those files' descriptions and categories, and retry their parsing.
- **Can manage**: also manage all files, the member list, who can use it, and the name and description.

The owner is the one manager accountable for the knowledge base and can't be removed from the list; Space and Team default knowledge bases have no owner and are managed by their automatic managers. Only the owner and automatic managers can transfer ownership or delete the knowledge base. Space viewers can at most use Space knowledge bases: they can search, view and download, but not add or delete files.

### Find knowledge bases and files

The **Knowledge bases** tab lists the knowledge bases you can use, grouped under My, the current Space and Team. Each row shows the name, description, who can use it, your level, the number of files (with failed files flagged) and the last update. The Team index notice still appears above the list, with the same rules.

- Type in the search box to find knowledge bases or files by name. Different knowledge bases can have the same name; use their group and description to tell them apart.
- Turn on **Deep search** and press Enter to search content across every knowledge base you can use. The results are a file table with an extra **Knowledge base** column showing where each file is.
- The **⋯** menu at the end of a row offers **Share** (**Who has access** if you can't manage it), **Settings** and **Delete knowledge base**, depending on your level.

The page header has **New knowledge base** and **Add files** (called **Add knowledge** when knowledge bases are not enabled). Extraction tables and their results are both under **Extraction tables**; **Templates** holds only document matrix templates.

### Create a knowledge base

1. On the Knowledge page, click **New knowledge base**.
2. Enter a name. The description is optional, but say what the material is about: the assistant uses it to decide when to search.
3. Choose where it lives: My, the current Space or Team. Places you can't choose are still listed, with the reason. Everyone can create in My; Space admins and Space members can create in the Space, viewers can't; only the Team owner and Team admins can create in the Team.
4. Choose who can use it. The options depend on where it lives; with **All Space members**, the default is use. Click **Create**.

The new, empty knowledge base opens, and you can add files right away or choose them from Files.

### A knowledge base's page

Click a knowledge base's name to open its page. The header shows where it lives, its name and description, who can use it, your level and the last update. If you can edit it, you see **Add files**; if you can manage it, you see **Share**, and everyone else sees **Who has access**.

The page has its own address, which you can bookmark or send to colleagues. Opening the address grants no access: people without access see "This knowledge base can't be found", exactly as for a knowledge base that doesn't exist.

The page has three tabs:

- **Files**: the files in this knowledge base. They work as described under "Find files" and "File details" below: search by name, deep search, filter by status, type and date, sort, page through, and select several files for bulk actions. There is no Scope filter or Scope column, and there is an **Added by** column.
- **Members**: the "Who can use it" group shows who can use it, the Space members' default level and where it can be used. The list below shows each member's level and source (owner, automatic, added directly, group or Space). The three levels are explained at the bottom.
- **Settings**: the name, the description, the owner (**Transfer ownership…**) and **Delete knowledge base…**.

When a knowledge base has no files yet, people who can edit it see ways to add files or choose them from Files, and others see whom they can ask. If you can only use it, **Add files** and **Share** are not shown, and the page says whom to contact when material needs to be added or changed.

### Share and manage members

If you can manage a knowledge base, click **Share**:

1. Under "Who can use it", choose the audience.
2. Under "Add members or Spaces", search by name for people, groups or Spaces (only Team knowledge bases can list Spaces), choose a level and click **Add**.
3. In the list, change someone's level or remove them. Removing shows a message with **Undo** that stays for at least 8 seconds.

Changes apply immediately; the next search follows them. The bottom of the dialog says how many people can use it, how many of them can edit and how many can manage. **Copy link** copies the knowledge base's address only and gives no one access.

Everyone else can open **Who has access** to see the same information without changing it. My knowledge bases can't be shared.

### Rename, transfer and delete

- **Rename or change the description**: people who can manage it change them under **Settings** and save.
- **Transfer ownership**: the owner or an automatic manager can transfer ownership to someone who can already manage the knowledge base. The previous owner keeps managing it while they still qualify. Default and My knowledge bases can't be transferred.
- **Delete the knowledge base**: the owner or an automatic manager clicks **Delete knowledge base…**; the confirmation shows how many files it holds. After deletion, those files leave knowledge and are no longer searched, and their originals stay in Files. Default knowledge bases can't be deleted. If some files can't leave, the knowledge base is kept and the message explains why.

### Add files

When you click **Add files** on a knowledge base's page, **Add to** in the upload window defaults to that knowledge base; from the list of knowledge bases, it defaults to the current Space's default knowledge base. The choices are grouped under My, the current Space and Team. Knowledge bases you can't add files to are listed but can't be chosen, with the reason. The embedding model check, supported formats and sizes, ZIP, Google Drive, cancelling and error explanations are the same as under "Upload files" below.

### Move files to another knowledge base

1. In a file's details, click **Move to another knowledge base…**, or select files on the **Files** tab and choose **Move to another knowledge base…**.
2. Choose the target. The list is grouped under My, Space and Team and shows who can use each one; you can only choose knowledge bases you can edit.
3. The window then says how many people gain access and how many lose it after the move. Click **Move** to confirm.

To move a file you added, you need to be able to edit the knowledge base it is in; to move someone else's file, you need to be able to manage it. The original, its versions, citations, document matrices and extraction results are all kept, and access is then computed from the new knowledge base. People who lose access no longer see the file's name or content, and citations show them a restricted source.

### Add from Files or from a conversation

On **Library → Files**, **Add to knowledge base** now asks for a knowledge base (with the same rules as **Add to** above) instead of a scope. **View in knowledge base** opens the knowledge base the file is in, with the file selected.

When you ask the assistant in a conversation to add a file to knowledge, say which knowledge base. If you don't, or if you can't edit that knowledge base, the assistant asks you before adding anything.

### File details and citations

In file details, the visibility is replaced by **Knowledge base** (a link to it) and **Who has access** (a one-line summary, with **View members** to open its Members tab). **Change visibility** is replaced by **Move to another knowledge base…**. Everything else stays the same.

Conversations search every knowledge base you can use in the current Space: your My knowledge bases, the current Space's knowledge bases and the Team knowledge bases. You don't choose a knowledge base in the conversation. Citation cards and report sources follow the same rules as before, and people without access see only a restricted source. **Open in knowledge base** opens the knowledge base the file is in, with the file selected. Global search's Knowledge group likewise lists only files from knowledge bases you can use.

### Notices and phones

- The first time you open the Knowledge page after knowledge bases are enabled, a one-time notice says your material was organized by its old scopes: personal files are in My knowledge, Space files in the knowledge base named after the Space and Team files in the Team knowledge base. Who can see what has not changed.
- Space viewers also see a notice that they can now only use Space knowledge bases and can no longer add or delete files in them; they can ask a Space admin when needed.
- On a phone, the list of knowledge bases and the file table become rows, header buttons wrap, and tabs scroll sideways. Share and dialogs with several fields open from the bottom of the screen.

If an administrator later turns this feature off again, files in knowledge bases created in a Space or the Team can't be searched or opened until it is turned back on. Files in default knowledge bases and in My knowledge bases stay available.

## Choose who can see a file

When knowledge bases are enabled, you choose a knowledge base under **Add to** when you upload instead, and that knowledge base decides who can see the file; see the previous section.

Pick a visibility scope when you upload:

- **Personal**: only you can see and search it.
- **Space**: members of the current space can search it. After you switch spaces, you see that space's material.
- **Team**: everyone in the team can search it. Only team administrators can upload to the team scope.

Being an administrator of one Space does not grant Team-wide upload permission. Ask a Team administrator to add Team knowledge.

Viewers of a Space can read and search its knowledge but cannot add files to it, or change or delete files in it, including files they added earlier. Their personal files are not affected. To add material, ask a Space administrator to change your role.

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

When knowledge bases are enabled, **Add to knowledge base** asks for a knowledge base and **View in knowledge base** opens the knowledge base the file is in; see "Add from Files or from a conversation" above.

The **Add to knowledge base** window checks for an active Team embedding model before you confirm. If it is missing, follow the same configuration guidance as in the upload window above; the file remains in Files.

Uploading to Files or attaching a file in chat only stores the file; classification as long-term material does not add it to knowledge. Choose **Add to knowledge base**, upload from the Knowledge page, or explicitly ask the Agent to add an exact file and specify its visibility. The Agent checks your original request and current permissions; ambiguous requests need clarification. Processing must finish before the file is searchable. Chat ZIP attachments stay as one archive and are not unpacked or indexed; use the Knowledge page to import documents inside a ZIP. **Remove from knowledge base** keeps the original file. An old addition request cannot add it again after removal; a new explicit addition can.

When a file is revised in **Files**, the knowledge base keeps the version you previously added and indicates when a newer one is available. In **Files**, choose **Add version N** to add the new version to the intended scope. Check that it is ready before removing the older entry if you no longer want it returned in search.

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

When knowledge bases are enabled, the actions below happen on the **Files** tab of each knowledge base's page, which has no Scope filter. To search across knowledge bases, use the search and deep search on the list of knowledge bases.

- Use the scope above the list to switch between Personal, Space, and Team. The scope is the **Scope** filter in the toolbar (on a phone, under **Filters**). Space shows the current space name, with a short note on who can search that scope.
- With the **Scope** filter, if there is an **All** option, it lists the personal, current space, and team files you can search at once, and the **Scope** column in the table shows where each file belongs. The page remembers the scope you chose last time; the first time, it shows the current space.
- Type in the search box to find files by name.
- Turn on **Deep search** and press Enter to search file content within the current scope. The note next to the search box says how many files were found and in which scope; each result shows the matching passage, labeled with where it is (page, section, or paragraph). On a phone, the list has no room for the passage, so each result names where the match is, for example “Page 3 · Search hit”.
- If deep search finds nothing, try other words, use the button to search another scope, or click **Search by file name** to turn deep search off.
- Filter by status, type, and upload date, and change the sort order when the page offers it. Each filter button shows what you selected; to undo a whole group at once, open its menu and choose **Clear status** or **Clear type**.
- When two or more conditions are selected (the keyword, each status, each type, and the date each count as one), they are listed under the toolbar. Remove them one at a time with ×, or choose **Clear All**. Scope is not listed there.
- On a phone, **Filters** opens a panel from the bottom with Scope, Status, Type, and Date groups. Choices apply right away; choose **Show N files** to return to the list. Sorting and **Deep search** stay in the toolbar.
- After an upload succeeds, the list switches to the scope you uploaded to (it stays on **All** if you are viewing **All**), clears search and filters, and shows the first page sorted by newest upload, so you don't have to look for the new file. If files from a ZIP are still processing, click **Back to file list** to follow their status there. Once processing finishes, click **Done**; the list refreshes the same way.

If the page address changes as you search and filter, it remembers the search, filters, sort order, page, and the file you have open: the browser's Back button returns to the previous results, and refreshing keeps you in place. Sending the address to a colleague only shows these files to people who already have access; it does not grant access.

When the page remembers the address this way, the list also supports full keyboard use: press Tab to reach a filter button and Enter to open it, use the arrow keys to choose an option, Space to select it, and Esc to close the menu. Selecting and opening are separate actions: press Space on a row to select the file, and press Enter on the file name to open its details.

## File details

When knowledge bases are enabled, see "File details and citations" above for what changes in the details.

Click a file in the list to open its details: status, visibility, uploader, upload time, and size.

- To add a shared document to the conversation you are writing, choose **+ → Choose from Library → Team materials** or **Current Space**. The selected version is copied to your draft as an attachment. You can also ask the assistant to search the knowledge base. In a file's details, **Use in chat** starts a new chat for files you uploaded in the current Space.
- If a file has been cited in chats, the number of citations appears under its name.
- When you open a file from deep search results, the matching passage is shown first and highlighted.
- In a document matrix, **View source** on a cell opens the file details right on the page with the cited passage shown first; close it to return to the matrix.
- A file that is still processing shows its progress; you can close the details and processing continues. If processing failed, the details explain why and you can **Retry** right there.
- If you have permission, you can also change visibility, extract the file to a table, or delete the file from the details. Changing the visibility of a file someone else added asks for a reason of at least 10 characters.
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
3. Click a cell to see the answer and its source. You can edit the answer or mark it as checked; **View source** opens the file details on the same page with that passage first. The source line also says where the passage is: the page for PDFs and presentations, and the paragraph for Word, Excel, Markdown and other files without fixed pages. No location is shown when the quoted text is not found in the file. Cells from earlier runs may still show a page estimated by AI; rerun them to locate the page in the source.
4. Runs use credits based on document length; you see an estimate before a run starts.

By default, a matrix can have up to 20 columns and 500 files; if you go over, the message shows the actual limit and the current count. A column name cannot be empty or repeat another column's name in the same matrix; names that differ only in leading or trailing spaces, or in full-width and half-width characters, count as the same name.

The **Document matrix** list shows one matrix per row, with where its rows come from, files × columns, the last run and who created it. Click a matrix name to open it.

### Change a matrix after you create it

Do this on a computer; on a phone you can only view a matrix.

- **Add files**: click **Add files** below the table and search by file name. Files are grouped under "This Space" and "Team". Files already in the matrix, personal files and files still being processed are listed with the reason they can't be added; personal files can only go into matrices visible to only you (your administrator enables them; see “Matrices visible to only you” below), where you can check them like any other file. You can add up to 100 files at a time. New files appear at the end of the table as "Not run"; **Run** then processes only cells that haven't run or have failed, and existing results are kept.
- **Remove files**: point at a file's row and choose **⋯** → **Remove from Matrix…**, or select several rows and click **Remove from matrix**, up to 100 at a time. The confirmation says how many cells are deleted and how many of them were edited by hand or verified. The files stay in the knowledge base and can be added again later; runs in progress skip removed files.
- **Rename**: click the matrix name at the top, or choose **⋯** → **Rename**. Type the new name and press Enter to save, or press Esc to keep the current name. The name can't be empty.
- **Delete the matrix**: choose **⋯** → **Delete Matrix…**. The confirmation says how many cells' results are deleted; runs in progress stop, and credits already used are not refunded. The files themselves aren't affected.

Space members can add files, rename the matrix and remove files with no edited or verified cells. Removing files with edited or verified cells, and deleting the matrix, takes the matrix creator, a Space admin or a Team admin. You can also undo those edits and verifications in the cells first, then remove the files.

### Matrices visible to only you

Your administrator turns this feature on step by step. Once it's on, the step where you click **Create** for a new matrix asks **Who can see it**:

- **Space members** (the default): as before, members of the current Space see the matrix in the list and on Home and can open it; each row still follows their own access to the file.
- **Only me**: only you can see and use the matrix, not even Space or Team admins. Your personal files are added to the files you can choose, and the list and the top of the matrix page show **Only me**.

For an existing matrix, its creator chooses **⋯** → **Make Visible to Only Me** or **Make Visible to Space Members** on a computer and confirms.

- When you make it visible to only you, the confirmation names the members who edited or verified cells or ran the matrix: their edits and verifications stay, but they can no longer open it. While other members' runs are still in progress you can't change it; wait until they finish. If a column has a source discovery purpose, set it to "None" first.
- To make it visible to Space members, the matrix can't hold personal files. The message lists them; remove them from the matrix and try again.
- Matrices visible to only you can't have a source discovery purpose, and saving one as a template can't include personal files; the message lists the files to remove first. Their runs count toward the Space's limit of runs at the same time.
- When you delete your account, your matrices visible to only you are deleted with their cells, runs and history; matrices visible to Space members stay.
- If your administrator later turns the feature off, existing matrices visible to only you stay visible to you only and keep their personal files, but you can no longer create them or change who can see a matrix.

### Run and rerun

**Run** processes every unfinished cell, meaning cells that haven't run or have failed; existing results are not rerun. Click **▾** next to **Run** to choose what to run; the first three items show how many cells they cover:

- **Run Unfinished Only**: the same as clicking **Run**.
- **Rerun Stale Only**: cells whose result is out of date because the column's question changed or the file has a new version.
- **Rerun Failed Only**: only the failed cells.
- **Try on 3 Files First…**: only the first 3 files that still have unfinished cells. Check the results and adjust the questions if needed before you run the rest. The task summary marks this run as a trial.
- **Rerun All…**: extracts every cell again but keeps verified and hand-edited cells; the menu says how many it keeps. To rerun a verified cell, undo the verification first, or open the cell and click **Rerun cell**.

**Run This Column** and **Rerun Whole Column…** in a column header's menu work the same way for that column.

Before a run starts, the confirmation shows:

- How many cells each column runs and its estimated credits. Reading the documents is shared by the columns and has its own line. A cross-checked column reads every file again, which often makes it the largest cost; the confirmation says so.
- The total, your balance, the estimated time (you can leave the page while it runs) and the longest file's share of the estimate.
- **Try on N files first**: when checked, only the first few files run and the cost shown is the trial's.

In an open cell, **Rerun cell** and **Retry cell** show about how many credits they use. A single-cell rerun starts right away without a confirmation and replaces a hand-edited or verified value.

Cells that fail because of a timeout, a temporary model service error or an interruption are retried once automatically before the run ends, after a wait of half a minute to a minute. Failed attempts use no credits, and a successful retry is charged as usual. If most calls fail, which points to a temporary model service outage, nothing is retried automatically and the task summary says why; try **Rerun Failed Only** again later. Cells whose model output couldn't be read have already used credits and are not retried automatically; rerun them yourself.

### Check the results

The toolbar above the table helps you check the AI's answers cell by cell:

- **Verified x / y**, on the right of the toolbar, shows how many of the cells you can view are verified.
- **Search in table** finds rows by file name or answer.
- **Status** shows only rows with cells that are Not Verified, Edited, Low Confidence, Cross-Check Mismatch, Failed or Stale. Low Confidence means a confidence below 70%, or an answer whose quoted source text can't be found in the file; Cross-Check Mismatch means the two extractions of a cross-checked column differ.
- **Add filter** filters by the values of one column. Text and option columns offer "is" and "contains"; number columns offer "is", "greater than" and "less than", and date columns "is", "after" and "before"; every column offers "is empty". Click a filter to change or remove it.
- **Sort** orders the rows by file name or by any column; rows without a value come last.
- Search, filters and sort are written into the page address, so going back or reloading keeps them, and a colleague who opens the link sees the same view. Click **Clear filter** or press Esc to show every row again. Rows of files you can't view are hidden while you filter.

**Mark Column Verified…** in a column header's menu marks every finished, current cell in that column as verified at once, hand-edited cells included; unfinished, failed and stale cells are not marked. The confirmation says how many cells are marked, and rows hidden by the filters are included. When it's done, the message says which cells were not marked and why.

When you open a cell, the side panel shows the column's full question at the top. Below it, **History** lists the latest 50 entries: who edited the value, kept it as text, verified it or removed the verification, and when; and when the AI extracted, reran or retried it. History records what was done, not the cell's earlier values; changes made before this feature was available have no history. Removing a file from the matrix also removes its cells' history.

Marking a column verified takes a Space member; viewers can see the progress, filters, sort and history. On a phone, only the progress is shown.

### Export to Excel

Click **Export to Excel** at the top of the matrix to download it as an Excel workbook named after the matrix, with sheet names and headings in your interface language. **Answers** has one row per file and one column per question; numbers, dates and yes/no answers are real spreadsheet values you can sort, filter and add up, verified answers are green, and answers edited by hand that aren't verified yet are yellow. **Sources** has one row per cell with the file, the column and its question, the answer, its status, the AI's confidence, the page or paragraph when known, and the quoted source text. **About** shows the matrix name, when and by whom it was exported, how many cells were hidden, and what each status means. The file holds only what you can see in the matrix: files you can't view are listed as "(No access)" without their values. Text is always saved as text, so content that looks like a formula isn't run, and content longer than 32,000 characters is cut and marked "(truncated)". Every Space role, viewers included, can export; do it on a computer, as phones don't show the button. The file shows the matrix as it was when you exported it and doesn't update later.

### Read the statuses in a matrix

- **Not run**: the question hasn't been processed yet.
- **Processing**: the task is in progress.
- **Failed**: open the cell to see why, then decide whether to retry. Temporary failures such as timeouts were already retried once automatically.
- **No value extracted**: the question was processed but no result was extracted. Check the source text; this doesn't prove the answer is missing from the file.

A matrix groups its tasks in one task summary; expand **Run history** to see each task. Reopening a finished matrix doesn't show old success messages again. If something failed, the summary keeps the number of items to handle; expand it and choose **View failed**, which sets the toolbar's Status to Failed. Column headers show the full column name over two lines; point at, click or focus a header with the keyboard to read its full question. The full question also appears when you open a cell. On a phone, tap a cell to see its details and close them to return to the matrix.

After you switch spaces, the matrix checks your access in the current space again. If the matrix isn't available in that space, go back to the list and choose another matrix, or switch back to the original space; once access is restored, click **Retry**.

A set of question columns you use often can be saved with **Save as template** at the top of the matrix, then brought in with **Use as matrix** under Templates to add all the columns at once. Templates you create can be sent to the team library with **Publish to team library**, so members of other spaces in your team can use **Copy to my space**. Only the question columns are published; files and answers never leave your space.

## Turn fields in your files into a table with extraction tables

When you have a set of similar files (for example monthly income statements or contracts), create an **extraction table** that lists the fields to pull out of them (for example revenue and net profit). Each file added to the table is extracted into a row with those fields. You can view the results in the knowledge base and query them directly in chat.

1. In the knowledge base, click **Templates**, then **New template**, and choose **Extraction table (extract on upload)**. Describe the fields in a sentence, review them, and save.
2. Click **Extract to table** in the file details or in the list's more menu, and answer "Which table should this file be extracted to?". You can also select several files and extract them together. When uploading an Excel file, you can pick the table under "Extraction table" right away.
3. When extraction finishes, open **Extracted results**. Every cell links back to the source text, and its location is shown the same way as in a document matrix.
4. To stop extracting a file, choose **Don't extract** under **Extract to table**. The file stays available to full-text search.

You only see extracted results for files you can read, so results from other members' personal files never appear in the knowledge base or in chat.

Extraction is queued within a daily quota, so it can take a while when there are many files. The **Templates** view holds two kinds of items: templates are reusable sets of question columns for a matrix (save a matrix as a template, then apply it to a new matrix); extraction tables are sets of fields extracted automatically on upload, with their results under **Extracted results**. When knowledge bases are enabled, extraction tables and their results are both under **Extraction tables**, where **New extraction table** creates one, and **Templates** holds only matrix templates.

## See sources in answers and reports

For a ready file you uploaded in the current Space, open its details and choose **Use in chat**. Bowra checks the original file and prepares its attachment before opening the new chat. If the file is no longer available, restore the original and try again.

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

In larger collections, recent sources with maintained descriptions or categories receive priority in the assistant’s initial source hints. The assistant can use the source directory to find additional files by category name; a missing hint does not mean a file is unavailable.

## When sources change during a conversation

If a source is deleted, re-parsed or your access to it changes, later questions in the same conversation still continue. New searches use only the sources that are still available. Content the assistant already read earlier in the conversation may still be taken into account, and earlier answers stay in the conversation. Check any fact that came from the changed source, and start a new conversation if that content must be left out entirely.

## When sources change during a paused task

If a task waiting for your answer reports **Knowledge sources changed**, a source that task read has changed. Start a new conversation and ask again using the currently available sources. Changes to a source, its description or your access can make the earlier context unusable. The assistant checks current sources before continuing with them. The same source-change status is shown after refreshing or replaying the chat.
