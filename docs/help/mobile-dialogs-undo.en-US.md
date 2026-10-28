# Mobile dialogs and undo

This guide covers two things: how windows where you fill in information open from the bottom of the screen on a phone (or any narrow screen), and which actions can be undone right after you take them. On a computer, dialogs open in the middle of the screen.

## Windows you fill in open from the bottom

Windows with two or more fields, a list, or long content (for example inviting members, adding a connection, editing a memory, or configuring a tool) open on a phone as a panel that slides up from the bottom:

- Most panels have a “×” close button at the top; you can also choose **Cancel** at the bottom. **Cancel**, **Save**, and similar buttons stay at the bottom, so you don't have to scroll to the end to reach them.
- You can also close the panel by dragging the short bar at the top downward; if you only drag it a little and let go, the panel returns to place.
- While something is being saved or submitted, the panel can't be closed until the action finishes.
- When the keyboard opens, the panel moves above it, so the field you are typing in and the bottom buttons stay visible.
- While the panel is open, the page behind it can't be used and doesn't scroll.

Windows that only ask you to confirm one thing (such as “Delete this report?”) or to change a single name still appear in the middle of the screen.

Some windows (such as inviting members) ask whether to discard your input if you close them after filling something in; choose **Cancel** to go back and keep filling it in.

## Actions you can undo

After the actions below, a message with **Undo** on the right appears at the top of the screen. It stays for at least 8 seconds. Hovering over it or focusing a button inside it pauses the countdown; moving the pointer and keyboard focus away resumes the remaining time. Choose **Undo** to restore the item; the list refreshes.

| Action                                                                                                              | Where                                        | After undo                                                                      |
| ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------- |
| Delete or archive a conversation                                                                                    | The conversation's “⋯” in the sidebar        | The conversation returns to the list                                            |
| Delete knowledge base files (selected in the list or from the row menu); the undo message appears after you confirm | Knowledge base file list                     | The files are not deleted and stay in the list                                  |
| Delete a file                                                                                                       | Files page                                   | The file returns from the trash to the list                                     |
| Move a memory to the trash                                                                                          | **Settings › Memory**                        | The memory returns to the saved list                                            |
| Clear saved memories                                                                                                | **Settings › Memory** › **Data & privacy**   | Memories are restored as they were                                              |
| Restore personal appearance defaults                                                                                | **Appearance** in personal settings          | Your previous appearance settings return                                        |
| Archive a space                                                                                                     | The “⋯” on a row in team settings **Spaces** | The space is active again, and scheduled tasks paused by archiving turn back on |

Notes:

- Finish or stop running work before deleting or archiving its conversation. If deletion or Undo is temporarily blocked, follow the reason shown in the message; a failed deletion keeps the conversation in the list.
- If you miss the undo message, deleted files and memories moved to the trash can be restored from the trash within 30 days, and cleared memories can be restored from **Recent clears**.
- Moving a memory to the trash and deleting a file on the Files page don't ask for confirmation; they run immediately and show **Undo**. A file that a report links to still shows a warning first that downloads in the report will stop working.
- Archiving a space doesn't ask for confirmation; it runs immediately and shows **Undo**. If you miss the undo message, choose **Restore** in the space list. Archiving pauses the scheduled tasks in that space; undoing or restoring turns them back on (they follow their schedule from the moment of restoring), and tasks you paused yourself stay paused. Members may need to choose the space again the next time they open it.
- Permanent deletion, deleting a report, a skill, or a scheduled task, and deleting a file from the knowledge base file details can't be undone; they ask for confirmation first.

## Close a message

Messages disappear on their own, or you can choose “×” on the right to close one. On a phone, you can also swipe a message up, left, or right to close it. After you close a message that has **Undo**, that action can no longer be undone (you can still restore items from the trash and similar places as described above).
