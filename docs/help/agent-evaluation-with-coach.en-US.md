# Improve an Agent with Coach

If an Agent you own keeps getting something wrong, tell Coach in your own words. Coach turns it into an evaluation point, checks with you on a few real answers that it judges the point the way you would, then changes the Agent's draft and reports whether the change helped. You never have to write the point yourself; you only say whether Coach judged each answer right.

## Start

1. Open a conversation with **@Coach**, or choose **Use this to improve the Agent** on a member's thumbs-down in the Agent's **Pending** list.
2. Say what's wrong, for example: "It never says which period the numbers cover." Coach restates it as one point and shows a check card.

If a member gave a thumbs down without a comment, Coach first asks what's wrong with that answer. If the Agent has no answers yet, Coach drafts the point from the Agent's description; confirm it as usual. A point that names a Tool counts as soon as you confirm it; a judgment or data point drafted this way stays informational until it has been checked on real answers.

## Review suggestions without choosing every item

Coach's changes to an existing Agent are already in its draft. Check them in **Configure** and use the existing undo controls if needed; no second acceptance is required.

For a pending proposal in **Configure**, review its purpose and affected fields, then choose **Save these changes to draft**. Expand **Review or adjust changes** only when you want to inspect differences or take part of the proposal.

Suggested tests show their questions and available answer criteria. Choose **Add these tests** to add the pending set, or expand **Review or adjust tests** to edit or select individual items. Adding tests saves coverage; it does not run them or publish the Agent. If the suite changed, Coach must reconsider its suggestions against the current suite.

You can also ask Coach to revise a saved test. The proposal marks it as a change to an existing test. Choose **Update these tests**, or **Save these test changes** when the set also adds tests. Expand **Review or adjust tests** to compare original and proposed content. Adoption keeps the same test and its history; it does not run it. Delete tests on **Evaluate**.

## Manage your test cases

Suite results count runs started from **Run this suite** or **Save and run**. Results from **Test this change** compare versions and remain on **Release**. Earlier answers keep their recorded questions and expectations even after you edit the suite.

On **Evaluate**, choose **Edit Question** beside a saved case to edit it. **Save and run** saves your current cases and runs that saved version; **Save tests** saves without starting a run. If saving fails, no run starts. If the run cannot be confirmed after saving, run again to recover the same request. If the Agent version changed, review the refreshed target before starting again.

An **Execution passed** label confirms the execution checks. Read **Answer criteria** separately: missing or partial grades are shown explicitly, and model judgments are advisory. Choose **View answers and criteria**, then expand a case to read its recorded question, answer and expectations, including passed cases. Calibration disagreement concerns the grading check; it does not prove the Agent answered wrongly.

Short answers can receive a grade too; you do not need to add explanations just to make them longer. Earlier results keep their recorded grades; run the test again to obtain a new assessment.

Automatic reruns are collapsed below the results. Expand them when needed; **Check the baseline with Coach** uses the existing answer-checking conversation. This is not a daily review requirement.

On **Evaluate**, you can remove any case, including the last one, then choose **Save tests**. Saving no cases removes your custom suite from future tests; earlier results stay in history. You can add cases again at any time. A removed suite cannot be run on its own. **Test this change** still runs the basic safety check.

Having no custom cases does not prevent release and does not require an acceptance reason. The page and shared-Agent review state that answer quality has not been evaluated. Other safety checks, actual test failures and shared-Agent review still apply.

## Check Coach's verdicts

The check card lists a few answers. Each row shows the question, part of the answer with the sentence Coach relied on highlighted, and what Coach concluded.

- Leave a row alone if Coach got it right.
- Choose **Wrong** if Coach's verdict is wrong, or **Not sure** if you can't tell.
- Then choose **The rest are right**. When every verdict is right, that's the only click.

One row on each card is an answer Coach edited to take out what the point asks for; it is marked **Edited by Coach**. Judge it like the others.

What happens next:

- **All right:** the point is confirmed and added to the Agent's evaluation suite, then Coach starts changing the draft.
- **One marked Wrong:** Coach suggests a clearer wording and asks about a few more answers. The new card shows the old and new wording; choose **Keep the original, check 2 more** to keep yours.
- **Not sure:** Coach replaces that answer with another one. After three, Coach asks you to say the point differently.
- Coach may ask about one row again on its own; choose your answer on that row and **Save**.

When you submit a card, it sends Coach a short **@Coach** message saying what was decided, so Coach carries on in the same conversation. The collapsed card then says where the point stands and what happens next, for example **Point confirmed. Next: Coach suggests test cases from it**. If you were typing a message of your own, your draft stays and the card offers **Send to Coach** to send that message when you're ready. In a later conversation turn you can also just write to **@Coach**; it picks up the check you have open with it.

To change the point's wording yourself, choose **Reword the point** on the card. The card starts over; nothing is written to the suite until the point is confirmed. If the card says it is out of date, the point changed elsewhere: choose **Check again**.

Only the Agent's owner can answer a check card. Anyone else who opens the conversation sees a line saying so.

## Read the result

After Coach changes the draft and a check runs, Coach shows a result card:

- **What you raised:** how many cases with your point met it before and after the change.
- **Questions Coach hasn't seen:** the held-out cases (below), before and after.
- **Got worse:** other cases that passed before and fail now. Choose **Fix the one that got worse** to ask Coach to fix it.
- Two fresh answers to read.

With fewer than three cases the card shows counts, not percentages. If the Team's evaluation model changed during the check, the card says the result doesn't count; choose **Rerun with the current model** and let Coach run a new check.

## Held-out cases

When Coach adds cases for a point, one of them is held out. Coach can't see a held-out case's question or answers; it only learns whether the case passed. If a change also passes the held-out case, the Agent really improved, rather than only getting better at the questions Coach saw.

On the Agent's **Evaluate** tab, each case has a **Held out** switch with one line under it, "Coach can't see this case, so it checks whether a change really helped." The question mark beside it opens the details. You can:

- Turn it on for another case of the same point; the held-out flag moves there.
- Turn it off. If it is the point's only held-out case, you are asked to confirm, and Coach's later reports say the change couldn't be checked independently for that point.

Only you, the owner, can change held-out cases; Coach can't. Changes take effect when you save the suite. Release checks always run every case, held out or not.

Tests always use the latest saved version of each active suite: once you save a suite, confirm a point or accept suggested cases, the next test includes them. When cases are saved after a test finished, its result is marked out of date; choose **Test again** to include them. Earlier results stay in the history. When a shared Agent is already submitted for review, the reviewer approves it with the cases it was submitted with; cases saved during review are checked from the next test. To have them checked first, withdraw the submission and test again.

## Test a change

On **Evaluate** or **Release**, choose **Test this change**. It is one step: your latest saved draft runs the same cases as the released version. If Configure still has unsaved edits, the page says they won't be included in this test. You can leave the page while the test runs; you'll be notified when the result is ready, and you can choose **Stop test** if needed.

- **Test passed**: choose **Release as v2** (shared Agents: **Submit for review (as v2)**). The dialog takes an optional release note, shown in version history and, for shared Agents, to reviewers.
- **Cases got worse**: the result lists the cases that got worse and the points they missed. Choose **Ask Coach to fix it**, or change the draft on Configure and test again.
- **A required case failed**: nothing got worse, but a required case fails on this change (often it fails on the released version too). The page says so; choose **See the failed cases** for the answers and the points they missed, fix the draft, then test again.
- **Couldn't run**: the page names the cause and the fix, for example an unavailable evaluation model or a test that went over its time limit. Fix it, then choose **Test again**. For a time limit, the page lists the cases that timed out, so you can split them into smaller tasks; a run that times out drops only itself, and the other finished runs of that case still count. If a Tool a case requires can't run during evaluation yet, the page names the Tool and the cases. Testing again won't change that, so you can change the point to a judgment criterion for now. Data queries (such as **Query Data**) can't run during evaluation yet either: when an answer depends on one, the page says the case can't be tested in evaluation and names the Tool and the cases. Such a case doesn't count as failed, better, unchanged or worse, and testing again won't change it.
- **Data cases on live Datarc data**: unless live data reads in evaluation have been disabled for your deployment, data queries (**Query Data**, **View Data Fields**, **Clarify Data Term**) read your Team's real Datarc data by default instead of being skipped, so data cases get a normal result. They read with the Agent Owner's Datarc access and only read: rows stay inside the test run, and no Data entry or file is saved. Only the Agent's Owner can start such a test; if someone else starts it, data cases stop and ask the Owner to start it. The card states once whose access was used, because members with different Datarc access may see different data. Because the data is live, a case whose data changed between the two versions during the test isn't compared: the page says **The data changed during the test**, and you can choose **Test again**. When Datarc itself is the problem, the page names the cause and the fix: the Team has no Datarc connection or its key was rejected (team administrators get **Open Data Sources settings**), your email isn't verified, no Datarc workspace is selected, your account isn't a member of the workspace, your Datarc account can't read the data a case needs, or Datarc was unreachable, busy or too slow. **Test again** is offered only for the last three. A data case that hits its time limit says so; give it a longer time limit or split it. Your answers view also shows each run's Datarc calls and how many rows it read.
- **Cases stopped before a result**: each reason names the cases and what to do. The evaluation model failed or isn't set up; the Agent's model was rate-limited, timed out, was unavailable or rejected its credentials or the request; a Tool failed (the page names it); a run was stopped (for example the service restarted); or a platform error, which comes with a diagnostic reference to send to a team administrator. When the cause is a missing model or rejected credentials, testing again gives the same result: team administrators get **Open Models settings**, everyone else is asked to contact one, and **Test again once fixed** stays available for after the fix.
- **The evaluation can't use a model**: if the team has no model for the Agent's main model or the evaluation model, the test is refused before anything runs and the page names which one. Set it under **Team settings › Models › Model assignments**, then test again.
- **Release refused**: if something changed after the test (the draft, the released version or the test itself), or, for a shared Agent, a risk still needs your note, the release dialog says which and what to do.
- **View a case's answers**: each run's answer is laid out as in Chat (headings, tables). A run that failed lists every point it missed and why, for example "It didn't call “Run Code”", and any other check it failed.
- **Result out of date**: the draft changed after the test, or the released version changed. Choose **Test again**.
- **Only the basic safety check**: without cases of your own, a test can only confirm the Agent stays within limits. Choose **Add cases** to add a few questions you care about; the card updates as soon as you save them. You can still choose **Test this change** without cases.

A check Coach starts after its edit shows the same **Testing** state, marked **Started after Coach's edit**.

### Releasing an Agent only you use

For an Agent only you can use, the test tells you how the change did; it never stops you from releasing. **Release as v2** is offered next to the result whether the test passed, has no verdict, couldn't run or hasn't been run, and no written reason is needed. The release dialog says in one line what the test showed, for example "This change's test has no verdict: 1 case gave inconsistent results" or "This change hasn't been tested yet". The optional release note stays.

- When a required case got worse, or the change failed the Safe execution contract check, the dialog says so plainly and the button reads **Release as v2 anyway**.
- While a test runs, stop it or wait for the result before releasing.
- Version history shows what the test said at release, for example **Released when its test had no verdict**.
- Agents shared with other people keep the review: a test without a verdict has to be tested again before you submit it.

**Inconsistent results.** Each case runs several times on both versions, usually 3 times. Most runs decide the result: when 2 of 3 runs pass, the case passes, and when only 1 passes, it fails. Under **Comparison details** you still see how the runs went, for example "This change passed 2 of 3 runs and failed 1; v1 passed all 3 runs". A case is unstable only when its runs don't reach a majority, for example 1 passed, 1 failed and 1 hit its time limit. Then the test has no verdict, even if it is the only such case, and the result names the case with how its runs went on both versions. Cases marked **Critical case** (the Safe execution contract check) are stricter: they must pass every run, so any disagreement leaves the test without a verdict. Choose **View answers** to compare the runs. Testing again often settles it; if a case keeps disagreeing, make its points more specific.

## Where points come from, and Coach's advice

On the **Evaluate** tab, a point confirmed with Coach shows the words it came from and how many answers you checked, with **View** for the details. A point that started from a member's thumbs-down shows **From member feedback**; the member's comment isn't shown there.

When you add or change a point, Coach reads it and, only if there is a problem, adds one line under it: too vague to judge, two things in one point, a figure copied from one answer, or the same as or contradicting another point. Choose **Use this rewrite** to take the suggested wording, **Split in two** to replace a point that checks two things with two points, or **Keep as is**; Coach won't mention it again for that wording. The suite may also show **Coach reviewed this suite** with missing coverage and cases that ask the same thing; **View side by side** shows the two cases so you can keep both or remove one, which takes effect when you save the suite.

A point's **⋯** menu has **Check on Answers**, which asks Coach to check the point with you on real answers, and, for a saved point, **View Calibration**, which lists the examples it was calibrated on, how each should and did grade, and the evaluation model used. All of this is advice: it never blocks saving or release, and nothing appears if Coach is unavailable.
