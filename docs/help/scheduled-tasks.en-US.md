# Create and manage scheduled tasks

Open **Scheduled Tasks** from Capability Center when available. The management page lists existing tasks and their next run.

1. Create a task and give it a recognizable name. Choose the Space where it should run and describe the question, expected output, and data scope. If you select an available Agent or workflow, check that it can use the required sources.
2. Choose a schedule such as **Daily**, **Weekdays**, **Weekly**, or **Manual**. Check the time zone and displayed next run. In Advanced settings, use a five-field cron expression when the presets do not cover the schedule.
3. Choose how the task uses conversations, such as starting a fresh conversation for each run or continuing in its linked conversation. Set optional email delivery and save.

Use the row menu to **Run now**, edit, pause, or delete. Open **Run history** to check outcomes, open the resulting work, and inspect delivery status. If a run is waiting for information, use the original question card. If the task was paused after losing access to a Space, Agent or tool, repair the dependency before enabling it again. [Task alerts and delivery](scheduled-task-alerts.en-US.md)

Each account can have up to 20 active scheduled tasks across its Spaces. At the limit, choose **Go to an active task** and pause an active task to free a slot.
