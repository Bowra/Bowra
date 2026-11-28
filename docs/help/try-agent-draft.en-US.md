# Try an Agent's draft in a chat

Availability depends on this deployment. If the agent draft trials entry is not offered, ask your administrator whether it is enabled.

As an Agent's owner you can chat with its saved draft before you release it, the way members will use it: ask follow-up questions, let it run Tools, and look at its charts and files. Members keep using the released version; only you see the trial.

## Start a trial

1. Open the Agent you own and make your changes in **Configure**.
2. Choose **Try the draft** on the release bar, or **New trial** under **Trials** on **Test cases**. While a submitted version waits for review, the button reads **Try the version in review**.
3. A new chat opens with the Agent fixed in the input, and the input starts empty: an unsent message left in a new chat from earlier is discarded. The band and the input name what you try, the draft or the version in review. On a wide screen the Agent's editor opens beside the chat, so you can edit and ask in turn; on a narrow screen choose **Edit the draft** to open it full screen. Instead of the home page's suggestions, up to two questions from your own test cases are offered; choosing one fills the input without sending it.

Before the trial starts, unsaved edits are saved. If only **Instructions** changed, they are saved without asking. If other fields changed too, the page names them and asks first: **Save and try** saves them into the draft (they can't be discarded afterwards, and the released version doesn't change), and **Use only the saved draft** tries what you saved earlier.

If the draft can't be tried yet, the page names the failed check, for example the safety check, and a button takes you to the field to fix. If an unsaved edit can't be saved yet, for example **Who can use it** is set to specific members but nobody is picked, the trial doesn't offer to save it: the page names the problem and links to the section that fixes it.

## Chat with the draft

- A violet band at the top says it's a trial: only you can see it, Tools run for real and credits are charged as in any chat.
- Each answer is labelled with the draft it used and when that draft was taken for the trial, such as **Draft as of 14:32**. When you save the draft between two questions, a line says the answers below use the new draft.
- Sending saves the editor first by the same rule, so the next message always uses your newest saved draft. A line above the input says when that will happen. If a test of the change is running, the trial keeps using the draft under test and says so.
- Under each answer, **Save as case** adds the question to the Agent's **Test cases**; follow **Write its points** to finish it there. Where Agents can be started from their page, **Ask vN the same question** opens an ordinary chat with the released version and the question filled in.

## What runs for real

A trial is not a sandbox. Queries, web searches, code and Tools that write to other systems really run. Actions that need your confirmation still ask, the same as in any chat, with a reminder that confirming really runs them.

## What isn't available in a trial

Plan mode, MCP tool and prompt references, optional read-only methods, adding to a running answer, handing the task to another copy of the same Agent and template document tools are unavailable in a trial, and the page says why. Trial answers can't be rated or shared with others, files made in a trial can't be shared by link, and a trial chat can't become a scheduled task.

## Find a trial again

Trial chats don't appear in the sidebar. Open **Test cases** on the Agent's page: **Trials** lists your latest ten, with the first question, the time of the last message, the number of turns and how many drafts were used. **Back to the Agent** only leaves the chat; you can reopen it from **Trials**.

A trial ends when its draft is released, at once when you release it from the editor beside the chat. The conversation stays readable. **Start using vN** opens an ordinary chat with the released version; where it isn't offered, mention the Agent with @ in a new chat. To try later changes, start a new trial.

A trial also can't continue when the draft it used was withdrawn or replaced and the saved draft is now the same as the released version. Reopening it says so at the top: change the draft in **Configure**, then start a new trial from the Agent's page.
