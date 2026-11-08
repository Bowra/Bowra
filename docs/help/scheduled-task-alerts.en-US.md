# Task alerts and delivery

These notification options are available when enabled by your deployment administrator.

A scheduled task runs while you are doing something else. Its Agent can flag a run for you when the result needs a look now, and you can choose to receive results only for those runs.

## How a task alerts you

When a run finds something worth your attention, such as a number crossing the level the task watches or data that is missing, the task's Agent raises an alert. It chooses one of three levels:

- **Note**: appears in your notifications.
- **Flagged**: also pops up in an open Bowhead tab, and as a browser notification while the tab is in the background (see [Notifications](notification-center.en-US.md)).
- **Urgent**: also sends one email to your account address.

Only the task's owner receives its alerts. The alert names the task; open it to see the run, where the Agent explains what it found. A task sends at most one alert per run and at most five a day.

## Choose when results are delivered

1. Open **Team settings → Scheduled tasks** and edit the task.
2. Under **Notifications**, turn on **Email a summary when it finishes**.
3. Under **When to deliver**, choose:
   - **Every run** to receive the result after each successful run (the default).
   - **Only when flagged** to receive the result only when the task's Agent flagged that run. Other runs still appear in the task's run history.
4. Select **Save**.

## Notes

- Describe in the task's instructions what should count as worth flagging, for example "alert me if daily revenue drops more than 20%".
- The urgent email goes only to your own account address, and only when you are an active member of the task's team and the team's mail service is set up.
- If alerts are switched off for your Bowhead deployment, every run is delivered as before, whatever you chose.
