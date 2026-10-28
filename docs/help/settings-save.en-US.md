# How settings are saved

Settings navigation is ordered **Personal → Space → Team**. It shows only entries available to your role and enabled features; empty groups are hidden. Team settings appear only for Team owners and administrators. Space administrators see Space management entries; other Space members see only the readable Space instructions. Opening a direct link does not make an unavailable entry appear in navigation.

Personal, team, and space settings are grouped by topic, one card per group and one row per setting: the name and a short description on the left, and what you can change on the right (stacked on a phone).

Where available, **Open in Canvas** in Space general settings hosts the original general page on the right of Chat. Save the Space name and description with **Save**; the preference controls below still save independently when changed. Use the close button × beside the Canvas tab name, even when only one object is open; the page has no separate close button. If the name or description has unsaved changes, you are asked whether to discard them; a save in progress finishes before the view closes. Closing the view does not undo a saved choice. Ask the Configuration Agent to explain or change supported preferences. A suggestion appears separately; confirm to save. After confirmation the original page reads the saved values while preserving newer manual input. If the page changes while a suggestion is prepared, finish saving or discarding your current input before reviewing it again. Ask for a new suggestion if its original values have changed.

When you try to leave while the Canvas settings page is saving or a confirmation is still in progress, the original page and Space name are shown with a notice. Wait for that operation to finish, then try leaving again. A background result does not switch you away from another editor.

Saving only a Space description leaves its saved name unchanged; saving only its name leaves the description unchanged.

When several pages are open in Canvas, select the page you want to work on. Refresh reopens that selected settings page; save any unsaved text first. Closing a background Agent keeps the visible settings page and your unsent message. Returning from Results restores a page that is still open.

## Two ways settings are saved

- **Switches and single choices take effect immediately**: turning a switch on or off, or picking an option, saves it right away. There is nothing else to press.
- **Text changes are held until you save at the bottom**: after you change a name, a description, a number of days, or a configuration field, a save bar appears at the bottom of the page saying **You have unsaved changes**, with **Discard** and **Save** on the right (some pages say **Save choice policy** or **Review and apply**).
  - **Save** submits all of these changes; the bar disappears when saving finishes.
  - **Discard** restores what was last saved and submits nothing.
  - While saving, the button shows **Saving…** and neither button can be used.

The save bar stays at the bottom of the screen, so you don't have to scroll to the end of a long page to find it. On a phone, the page leaves room for the bar so the last setting isn't covered; when the on-screen keyboard opens, the bar moves above the keyboard and the field you are typing in stays visible.

## Leaving the page

When you have unsaved changes:

- If you reload the page or close the browser tab, the browser asks whether to leave; if you leave, the changes are not saved.
- The Models, Feishu integration, and chart style pages also ask before you switch to another page; on other pages, save or discard before you move on.

## Where it is used

- Personal settings: Account (display name, answer style)
- Team settings: Models (drafts; the bar says **Review and apply**), the Feishu integration connection settings, reference material retention, and the chart style selection
- Space settings: General (space name and description)

## Suggestions in Chat

The Configuration Agent checks current supported fields, choices and limits when it reads settings. You can also ask it to explain or suggest changes to Space instructions; members may read them, while Space administrators and Team managers can change them in an active Space. Review and confirm before saving. Where Team configuration assistance is enabled, Team owners and administrators can also request agent execution, sandbox duration, HTML size and model-retry changes, or suggestions for existing model connections, runtime parameters and assignments. Review the suggestion before saving. Model suggestions open as a draft on the original model page: use its Review/Apply steps and any required risk or index-rebuild confirmation. Existing unsaved input is preserved; save or discard it before loading the suggestion. Stale suggestions require a new proposal.

If Chat says a page opening was requested or a suggestion is ready for review, check the actual page before confirming. A suggestion can remain saved while unsaved input or a closed page prevents review. Save or discard your current edits, open the original page, then review the saved suggestion; this does not apply it automatically.

Try a concrete request such as “Rename this Space to Finance” or “Make my text larger.” You can also select Configuration Agent from the Chat mention menu. Shared Space changes require that Space’s administrator permission. Review the proposed values and confirm; opening the page or displaying a suggestion does not save it.

Where available, ask in Chat or mention **Configuration Agent**. It supports personal appearance choices, a saved answer language, and the current Space’s name, description, week start and data language. Space changes require that Space’s administrator permission. Changing other Agents belongs to Coach. Opening a page does not save settings.

Supported personal appearance suggestions can change mode, typography, text size, density, corners or motion; review and confirm before saving. Other appearance controls remain available on the original page. If you explicitly ask to keep using a particular answer language from now on, Chat saves that preference directly. Asking for only this answer or paragraph in another language does not save a preference; interface language is separate.

Interface language controls menus and labels; data language describes stored data; answer language controls generated replies. The Configuration Agent does not currently suggest interface-language changes. Use the original interface-language control with your existing access; opening that page does not save a choice or permit a different setting to substitute for it.

When Chat offers a supported settings change, review the current and suggested values, then confirm to save. Preparing or displaying a suggestion does not save it. Space week-start and data-language suggestions require permission to manage that Space; members without that permission can still ask for an explanation.

If you manually save an affected Space or personal appearance setting after a suggestion is prepared, confirmation or undo can report that the settings changed. Ask for a new suggestion based on the latest values to preserve your later edits. Choices made directly on the original settings page still use its normal save behavior.

When the general page is selected, Chat suggestions cover its name and description. Name is required and can contain up to 120 characters; description can contain up to 2000. Review and confirm to save. A later Space update may cause a conflict: keep your input, reopen the latest saved values and request a new suggestion. Name and description do not offer automatic undo. The other controls keep their own save behavior.

When the suggestion card shows **Couldn’t apply**, your settings weren’t changed; **Retry** applies the same suggestion again. An applied suggestion collapses into one line with **Undo** at the end when the change can be fully restored. Applied name and description changes show no **Undo**; the card explains that you can edit them back.

After refreshing or reopening the original settings page, a pending suggestion remains saved but does not automatically regain its confirmation actions. Choose **Review saved suggestion** to check current access, saved values and local edits; this does not save settings. Confirm after review succeeds. If the baseline changed, request a new suggestion; resolve unsaved input or pending saves first.

If a week-start or data-language suggestion was already applied, reopen its original Space page and choose **Review saved suggestion** to restore **Undo**. Review only reads; Undo uses the current saved values and restores the original choice, including removing a value that previously used the default. Later manual changes or unsaved input can block it. Space name and description do not offer automatic Undo.

When asking for saved settings, distinguish an explicitly saved choice from an effective default. If no choice is saved, the Configuration Agent explains the current default. A default value does not show whether a different choice was saved in the past.

## Notes

- If saving fails, the page says why and your changes are kept; fix them and save again, or choose **Discard**.
- Enabling or disabling a team chart style doesn't clear a chart style selection you haven't saved yet; a style you disable is removed from the selection.
- Forms that you fill in all at once, such as connection setup or inviting members, still use their own submit button.

## When saving or refreshing fails

A save error stays visible so you can act on it. If a response times out, the result may be unconfirmed: check the current settings before sending the change again. A network message means the connection failed; a service or unexpected error does not necessarily mean your network is offline.

Profile changes may have separate outcomes: a saved name stays saved even if the answer-style update cannot be confirmed. Feishu connection settings and personal chart preferences distinguish successful saving from a failed status or appearance refresh. Use **Check again** or **Refresh status** to reread the current state; this does not repeat the saved change.

Team chart publication, revision restoration and selection-policy changes also keep a confirmed change when history or appearance refresh fails. **Retry refresh** reads the current state without publishing or saving again. Google Workspace connection changes keep their confirmed result if status refresh fails; reload to check the latest status. If a change has no confirmed response, check the relevant list or settings before repeating it.

If Chat cannot finish explaining a settings operation, inspect its original page before sending the change again. A saved suggestion still needs your confirmation; a confirmed settings save is not undone by an interrupted reply. Completing the explanation does not repeat the settings change. If the save itself has no confirmed result, check the current saved values first.

## Reset and supported boundaries

Ask to restore a supported appearance choice to its inherited default, or restore all limits in the Team agent-run or sandbox/HTML group. Reset removes the saved override. Setting unlimited steps or automatic model-call duration is a separate choice; omitted settings stay unchanged. A zero retry count disables retries.

Connection credentials, creating or deleting connections, verification runs, integrations, members and access policies still use their original workflows. The assistant lists only currently available configuration domains for your permissions. Settings not listed may still be available manually on their original pages. Model and Team changes do not offer automatic Undo; review a new change to restore previous values.
