# Google Workspace

With Google Workspace connected, Agents can read the Google Docs, Sheets and Slides you share, view your calendar, and write results back to Google when you explicitly ask (without a separate confirmation) — all with your own Google access. Each member only ever uses their own authorization; nobody else, including team admins, can read or use your Google data.

## Admins: set up the team connection

Only team owners and team admins can configure the connection. It uses your company's own Google Cloud app; Bowra does not provide a shared one.

1. In the Google Cloud console, create an OAuth client of type "Web application" with user type "Internal" (your organization only).
2. Open **Team settings → Tools → Built-in third-party tools** and choose Google Workspace. Copy the **Authorized redirect URI** shown there into the client's Authorized redirect URIs, and the **Authorized JavaScript origin** into Authorized JavaScript origins. Use a stable HTTPS address accepted by Google. A verified Team public entry is preferred; otherwise the page uses your current access address. Register the complete displayed callback URI with Google.
3. Declare the listed scopes on the OAuth consent screen.
4. Back in Bowra, enter the Client ID, Client Secret and your Google Workspace domain, and turn on the capabilities you need:
   - Basic (required): read and write files members pick in Bowra; create new Docs, Slides and Sheets;
   - Read by link: read any Doc, Sheet or Slides file a member links;
   - Edit existing Docs and Slides, Write to existing Sheets: change existing files the member can edit;
   - Calendar: view the member's own calendar events (off by default).
5. To use "Select from Google Drive" in chats and the knowledge base, create a browser API key in Google Cloud whose website restrictions include your team address and `https://docs.google.com/*`, then enter the API key and project number in Bowra.
6. Click **Authorize with My Google Account**, complete one authorization with your own company account to confirm the setup, then enable the connection.

The card shows how many members have authorized. Changing the Client ID or Client Secret revokes every member's Google authorization; everyone has to reconnect. Bowra never asks to read email or browse the whole Drive.

If your own reverse proxy or security gateway sets a Content Security Policy, allow `accounts.google.com`, `apis.google.com` and `docs.google.com`; otherwise "Select from Google Drive" cannot open.

## Members: connect your Google account

Once your team enables Google, click **Connect Google** in **My Connections** and authorize with the company Google account that matches your Bowra sign-in email. Authorization is refused if the emails differ or the account is outside your company domain.

You can disconnect at any time in My Connections. When your authorization expires, or an admin turns on a new capability, the card asks you to reconnect or update your authorization.

## Use it in a chat

- Read files: paste a link to a Google Doc, Sheet or Slides file, or choose **Select from Google Drive** from the **+** menu in the message box (under **More tools** on narrow screens); picked files are inserted at the cursor as "name link" lines. The Agent reads it with your access; long files are read in parts. A Google Doc link that selects a tab reads that tab; a link without a selected tab reads all tabs, including nested ones.
- View your calendar: just ask, for example "What meetings do I have next week?". Your team must have the Calendar capability on.
- Write back to Google: ask explicitly, for example "Write this analysis up as a Google Doc" or "Append the results to this sheet". The Agent writes directly; when it is unsure which file or section to change, or the target range in a sheet already has data, it asks you first. Writing only happens in web chats. After a write, a card for the file appears in the canvas on the right, showing when it was written: by default it shows a snapshot of the content at write time. Switch between **Snapshot** and **Live preview** on the card to see the current Google view (your browser must be signed in to the matching Google account, and some browsers can’t show it; a line under the preview always explains that if nothing shows, Google may not allow embedding or may need you to sign in, so switch back to the snapshot or open the file in Google), or choose **Open in Google** to open the file in a new tab.

Before writing, Bowra checks that the numbers come from data retrieved in that turn and refuses to write if they don't. If a write request was sent but its outcome is unknown, the answer tells you to check in Google; nothing is retried automatically. You can undo mistakes from the version history in Google Docs or Slides.

For a Google Doc with multiple tabs, share the link to the tab you want to change. If the link does not select a tab, the Agent asks you to choose one before writing. Text replacement affects only that tab. In Sheets, a formula counts as existing data even when its result looks blank; empty values in an agreed overwrite clear the corresponding cells.

## Import into the knowledge base

In the knowledge base, open the upload dialog and click **or Import from Google Drive** under the drop zone, then pick files. Up to 20 files are imported at once; import any others again. Google Docs, Sheets and Slides are converted to the matching Office files; files larger than 10 MB cannot be exported from Google, so split or save them separately first. Imported files are not kept in sync with the original; import again after the original changes.

## Common questions

- No "Connect Google" button: your team has not enabled Google yet. Ask a team admin.
- A file cannot be opened: you may not have access to it, or your team has not turned on "Read by link". Pick it with **Select from Google Drive** instead.
- "Select from Google Drive" does not open: open Bowra through your team's registered address, or ask an admin to check the API key's website restrictions.

Use the browser address shown for JavaScript origins, including a nonstandard port if present. If you access Bowra through several addresses, include each browser origin in the Google application and API Key restrictions. Team public-entry metadata does not block opening the configuration page or initiating authorization; Google still requires its registered callback to match exactly.
