# Edit Word files in Results

Availability depends on this deployment. If the word editing entry is not offered, ask your administrator whether it is enabled.

When you view a Word (.docx) file in **Results**, you see its laid-out pages. When you can change the file, **Edit** appears above the preview, and you can change the text of body paragraphs and table cells without downloading it.

## View a Word file

- The first time a version is opened, its pages have to be created. Until they are ready, the reading view shows with “Rendering the pages…” above it; the pages then replace it.
- If the pages can't be created, the reading view stays, and you can still read and download the file.

## Who sees Edit

**Edit** only appears when you can save a new version of the file:

- you own this conversation and can write in its Space;
- the file was delivered in this conversation, or you uploaded it. Template documents have no **Edit**.

If the button is missing, editing is not available on your deployment yet, or you cannot change files in this conversation. For editing on a phone, see “Edit on a phone” below.

## The editor layout

**Edit** opens the editor in the full view.

- **Top bar**: the file name, the save status (such as “Editing · based on version 2”), **Discard changes**, **Save as new version** (**Save as a new file** for an upload) and **Stop editing**; after a save, **Keep editing** appears too.
- **Toolbar**: undo, redo, zoom (Fit page / 100% / 150%), what is selected, what you can do with it (such as **Undo this change** or **Download file**) and a short hint.
- **Outline on the left**: the document's headings, each with its level and page; a dot marks a heading with unsaved changes below it. Select a heading to go there. In a narrow window with a panel open, the outline is hidden; **Outline** in the toolbar opens it again.
- **Icon bar on the right** with three panels, closed by default. An open panel makes room next to the pages instead of covering them:
  - **Changes**: every change, by page, each with its own undo.
  - **On this page**: the page's paragraphs, table cells and items that can't be edited here, in Tab order. With a keyboard or screen reader you can pick items from this list.
  - **Shortcuts**: the keys you can use while editing, with tips.

Where Word files can be edited, the PowerPoint editor on a computer uses the same layout, with a list of slides on the left instead of the outline: the save buttons are in the top bar, the change list is in **Changes**, what you can edit on the slide is in **On this page**, and hints are in the toolbar and **Shortcuts**. On a phone, the PowerPoint save bar says “N unsaved · View changes”; tap it to open **Changes**. For the steps of editing a presentation, see [Conversation page layout](conversation-workbench.en-US.md).

## Change text

1. Click a paragraph or table cell once to select it, then click it again, press Enter or just type (an input method such as Chinese Pinyin works too) to change it right where it is on the page. Tab moves through the paragraphs and cells on the pages in order, continuing on the next page.
2. In a paragraph, Enter finishes it, Shift+Enter adds a line break inside it, and Esc keeps your change and returns to the selection; press Esc again to deselect. A new paragraph can't be started here.
3. A short change shows inside the paragraph right away. When a change adds a line, the paragraph is marked “Unsaved · number · adds a line” and the new text sits in a card in the right margin; saving lays out the pages again, and the text below moves down. In a narrow window or with a panel open, only the number stays and the text is in **Changes**.
4. If some paragraphs can't be found on the pages, the editor says so, and you change them in **On this page**.
5. Press ⌘Z (Ctrl+Z on Windows) to undo the latest change and ⇧⌘Z (Ctrl+Y on Windows) to redo it; Page Up / Page Down (fn+↑ / fn+↓ on a Mac laptop) change pages; ⌘S (Ctrl+S on Windows) saves.

Text you are changing is shown in your browser's font, which differs slightly from the final layout; the new pages after saving are what counts. Generated content in a paragraph (a page number, contents or a reference) can't be removed or added.

## What can't be edited here

The following can't be changed here. When you select one, the toolbar marks it “Can't be edited here” and says why, and **On this page** gives the reason too; **Download file** in the toolbar lets you change it in Word:

- headers and footers;
- footnotes and endnotes;
- paragraphs with tracked changes (accept or reject them in Word first);
- generated content that runs across several paragraphs;
- text in a form control;
- text in a text box;
- the parts of a merged cell after the first (edit the merged cell's first part).

If the whole document has Track Changes on, restricts editing or is digitally signed, none of it can be edited here. The editor explains why and offers three ways on: **Download file** and edit it in Word (with Track Changes on, this keeps the tracked changes); **Reference the whole file** and say in the chat what to change, so the AI changes it; or **Stop editing**.

## Save

- Select **Save as new version**. Each save creates one version, and earlier versions can be downloaded from **Version History**. Each file keeps its latest 3 versions; when a save will stop keeping an older one, the save status says which before you save.
- After saving, the pages are created again from the new version; until they are ready, your changes show as they are. Select **Keep editing** to continue on the new version, or **Stop editing** to return to the page you were on.
- Saving an uploaded file creates a new file named “original name (edited).docx” and leaves the upload unchanged; later saves add versions to the new file. If such an edited file already exists (made by you or the AI), the editor tells you when it opens, and you can keep editing that one.
- **Discard changes** clears every unsaved change: a single change goes at once, and the notice offers **Undo**; with more than one, it asks you first.
- With unsaved changes, leaving in any way (Stop editing, opening another conversation or reloading the page) first asks “Stop editing?” and says how many changes are not saved.
- After you save, the AI reads the version as you edited it and knows the changes are yours.

## When the AI saved a newer version first

If the AI (or another editing window) saves a newer version of the file while you are editing, your save writes nothing and overwrites nothing, and your changes are all kept. **Changes** opens and lists which changes can be **re-applied** to the newer version. Where the newer version changed the same text, choose **Use mine** (your text) or **Keep version N** (drop your change). Re-applied changes are still unsaved; check them, then save again. You can also select **Copy my text** and handle it yourself.

While the AI is working in this conversation, the editor reminds you that it may save a new version.

## Reference a paragraph or pages in the chat

To have the AI read or change only part of a file, reference that part in the chat, then say in the message box what to do:

- **Reference this page**: under each page in the **Results** page preview, and in the editor's toolbar when nothing is selected. Select it again to remove the page. You can reference up to 10 pages of one file; the file's card above the message box says which (such as “Pages 2, 3”), and you can remove pages one by one from it.
- **Reference this paragraph** / **Reference this cell**: in the editor's toolbar when a paragraph or table cell is selected.
- One file carries one kind of reference at a time: referencing a paragraph replaces the file's referenced pages, and referencing a page replaces a referenced paragraph; the notice then offers **Undo**.
- A reference points to the version you were looking at. The AI reads the text of those pages or that paragraph in that version (headers and footers are part of a page's text), not the whole file. Page numbers are those of the pages you saw; Word may break pages slightly differently.
- If a referenced paragraph or page no longer exists when you send (for example because the file has a newer version), the message is not sent and the draft is kept; open the file, reference it again on its pages, and send.

## Edit on a phone

On a phone, **Edit** shows the pages one under another:

1. Under each page are **Edit page N** (with how many items can be edited on it) and **Reference this page**. **Edit page N** lists the paragraphs and cells you can change on that page; pick one to change it. You can also tap a paragraph on the page. Where paragraphs are close together, the one nearest your finger is chosen.
2. A paragraph is changed in a panel that opens from the bottom: the keyboard's return key adds a line break inside the paragraph, **Done** finishes it, **Undo this change** drops its change, and **Reference this paragraph** references it in the chat. When a change adds a line, the page marks the place with a number for now, and the pages are laid out again on save.
3. Under each page you see what changed on it. The save bar at the bottom says “N unsaved · View changes”; tap it to open **Changes**, where you can undo each change or save. The save bar also has **Discard changes** and **Save as new version**.
4. Tapping something that can't be edited here, such as a header or footer, explains why and offers **Reference this page** (or **Reference this paragraph**) and **Download file**.
5. After saving, the save bar says which version was saved; tap **Keep editing** to continue on the new version.
