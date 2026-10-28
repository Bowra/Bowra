# View team operations data

Team owners and administrators can open **Settings → Team → Operations → Operations data**. This page shows yesterday, the last 7 complete days, or the last 30 complete days. The daily table contains exact values; the chart shows the selected measure's trend. Today is excluded until its day finishes.

The reporting time zone is shared by the team. Select a time zone and save it to change which hours belong to each day. A change updates the displayed daily grouping; it does not change the underlying activity.

- **Daily login users** counts each person once per day after a successful sign-in. Adding daily values across a week counts a person again on each day they signed in.
- **New sessions** counts ordinary chat sessions created that day. Test and evaluation sessions are excluded.
- **Chat tokens** adds input and output tokens from chat model requests. **Background tokens** shows other attributed model work separately. These are tokens, not credit charges.
- **Skill loads** counts actual Skill reads or invocations. **Tool loads** counts tool definitions supplied to model requests. **Tool calls** counts tool execution attempts, including failed calls.

Older records did not capture every measure. An unrecorded value appears as **0** and may understate earlier activity. Reports show team totals only, without member names, prompts, Skill content, or tool arguments.
