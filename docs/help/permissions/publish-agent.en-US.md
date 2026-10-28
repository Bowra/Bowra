# Publish an Agent

Use this guide when you created an Agent and want colleagues to use it. Saving a draft, selecting an audience, passing a release check, and publishing a version are separate steps.

## Before you start

Confirm that you are editing an Agent you own, its instructions and dependencies are ready, and any required Skills, models, tools, and data paths are available for the intended audience. Do not claim that an Agent can access data merely because its author can.

Personal MCP tools can remain bound when you share the Agent. They are available only when you run it; colleagues' runs do not include them. Make sure the Agent can still do the work you promise colleagues without those tools.

Having no custom cases does not prevent release and does not require an acceptance reason. The page and shared-Agent review state that answer quality has not been evaluated. Other safety checks, actual test failures and shared-Agent review still apply.

## Set the audience and publish

1. Open **My created → Manage configuration** for the Agent.
2. Choose the intended audience and save the sharing settings. If the Agent has personal tools bound, the audience section reminds you that colleagues cannot use them. Check that the other tools available to colleagues are enough for their tasks.
3. Use the release action offered by the page. Some flows show **Run security checks**; others show **Test this change**, which tests your latest saved draft against the released version in one step. An Agent only you use can be released at any time with **Release as v2** (with an optional release note): the test is information, so a failed, inconclusive or missing test doesn't stop it, and the dialog says in one line what the test showed. Once the test passes, shared Agents use **Submit for review (as v2)**, which also takes an optional release note that reviewers and version history show.
4. Review warnings, complete required changes, and submit the version.
5. If approval is required, a team owner or administrator reviews the candidate in the team workflow and approves or rejects it. The review page highlights the changes, audience and check results, with risks and actions together on the right. Expand **View runtime configuration** for details. To reject, select **Decline this release…**, choose the main reason in the dialog, then select **Confirm decline**. **Continue reviewing** cancels without submitting a decision.

Version evaluation is enabled by default. Choose a Space you can access for the comparison, wait for the results, and resolve any blocking issues. Releasing an Agent only you use is your decision, whatever the test says; publishing for other members requires a passing test and team review. Your administrator can explicitly disable version evaluation.

When reviewing, the evaluation card says **The owner provided no cases** if only the basic safety check ran. That check shows the Agent stays within limits, not how well it answers; if you need more, reject with **Safety or quality evidence is insufficient**.

If a required case fails in both the released version and this change, the test result names the evaluation suite and case and shows how many attempts of this change passed. Open **View case** to compare the answers, then review the case's evaluation requirements, update the draft and test again; an unchanged failure still blocks a shared release, while an Agent only you use can still be released.

After a test finishes, the **Evaluate** tab shows the same result. Missing tests or an infrastructure failure do not mean its evidence is ready; resolve the issue and test again.

## Confirm colleagues can use it

The colleague should find the published Agent from a space and account included in its audience, hire it, and run a permitted task. Confirm the availability state and the result. Hiring or approval does not override team membership, space access, dependency readiness, or the colleague's data identity.

## Update or withdraw

Save and release a new version when behavior changes. Existing colleagues may continue to use the prior published version until the new version is approved and available. Withdraw sharing or disable the Agent when it must no longer be used; review dependent Skills and workflows first.

You do not need to republish after editing a Skill the Agent uses. From its next call, the published Agent uses the latest approved version of a team Skill and the current version of your personal Skill.

Related: [Hire and use an Agent](use-agent.en-US.md) · [Configure Agent capabilities](configure-capabilities.en-US.md)
