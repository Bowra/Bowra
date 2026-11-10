# Manage team model settings

Team owners and administrators manage model services in **Team settings → Models**. The page has three tabs: **Model connections**, **Model assignments**, and **External capabilities**. Add services and maintain their models in the first tab. Use the second tab to choose models for team tasks and review verification evidence. Use the third tab to configure image generation and web search. Review index updates in **Knowledge base → Files**.

**Source descriptions** is an optional knowledge-base model assignment and is disabled by default. Select a declared text model to generate discovery descriptions for new searchable publications and explicit file-level generation requests. It does not backfill files when saved or call a model during source browsing. Clearing the assignment stops new work; existing descriptions remain usable while their sources and permissions remain valid. See [source discovery](knowledge-base.en-US.md#source-discovery-descriptions-and-categories).

## Manage connections and model capabilities

On **Model connections**, add a connection or edit an existing one. Enter the provider address, authentication, required parameters, model IDs, and supported capabilities. Use **Plan verification** before or after saving the connection to review capabilities, request count, and possible cost before running verification. Verification contacts the provider and may consume credits, but it does not automatically save configuration changes.

For each model in the catalog, set its **Input**, **Cached input**, and **Output** credit multipliers. All three default to 1, including for existing models. A cached input multiplier applies only when the service reports cache hits; other input uses the input multiplier. Use values from 0 to 1,000 with at most six decimal places. After saving, each new model call captures the rates then in effect; earlier calls keep their recorded rates, even if the same task continues. Review the multipliers before changing a model's pricing. See [View and allocate credits](credits.en-US.md) for the calculation and usage details.

If a connection is only for image generation or search, you can leave its model catalog empty and save the endpoint and credentials first. Configure its service model under **External capabilities** afterward.

**Context window (tokens)** is the model’s total context capacity, including instructions, conversation, Tool input and room for its reply. Enter an exact value when your service documents one. Otherwise leave the field empty to inherit the connection value, reviewed model catalog value or system default shown on the page. A displayed suggestion is not automatically saved as your own override. Changing this declaration cannot enlarge the provider’s actual limit.

Changing the address, credentials, model ID, or capability parameters can invalidate earlier verification. Deleting a connection opens a confirmation with the affected settings. Choose **Cancel** to keep it or **Confirm deletion** to update the draft: affected settings use suitable available models, or are cleared when none is available. Chat and knowledge-base replacements require valid verification; an embedding replacement also needs a configured dimension. Image generation and search use another configured model for the same capability. Complete **Review and apply** to save the deletion and assignment changes together. Replacing an embedding model still requires index rebuild confirmation. Retrieval may be unavailable during rebuilding because the old connection is being removed; without a replacement, configure an embedding model to restore indexing and retrieval. Other model edits follow the installation’s automatic assignment maintenance policy.

For compatible Chat Completions services hosted on your own infrastructure or a cloud platform, streaming uses the saved service address. When a task requires confirmed usage, the service must return input and output token counts in its streaming response. If it does not, ask the service administrator to enable that support or select a compatible model; Bowra cannot confirm usage from a missing response. A capability verification result is a small connection test, so also try a representative chat task after changing the service.

Custom HTTP chat connection tests allow up to 1,024 output tokens to reduce empty responses caused by a small test allowance. After updating, run verification again for previously failed connections. Saved request limits still apply.

**Model output capacity (tokens)** describes the most the model can generate in one response. The editor shows its exact default and whether it comes from the model, connection, catalog or an unknown-model assumption. Ordinary requests use automatic allowances up to 256,000 tokens, or 32,000 when capacity is unknown; a smaller model capacity takes priority. The actual allowance also leaves room for the request's input. These are maxima, not predicted usage. Enter a documented capacity for custom deployment IDs; **Use inherited capacity** removes your model override. Saving another field does not save an inferred default.

The visible **Request allowance** shows automatic, fixed, capped or omitted values, with separate rows when capabilities differ. Set a request limit in **Request parameters** below; capacity and request policy serve different purposes. An omitted field leaves the server limit unknown. Explicit fixed settings remain fixed and must fit the model's capacity and context.

If a response reaches its output limit, Bowra asks the model for a smaller complete action up to twice, using earlier successful results. It does not execute an incomplete Tool batch or repeat earlier successful writes. The running status shows the correction. If correction still cannot finish, successful earlier results remain available and the failure explains how to request a shorter report or check output settings. Existing time and step limits still apply.

## Automatic context compaction

In a model’s editor, **Auto-compact** is on by default. When an ordinary Agent conversation needs room, Bowra summarizes older context and retains recent original content. Each Agent follows the model it uses. The conversation shows a quiet “Organizing context…” status while this happens; you can continue reading without dismissing a dialog.

Leave the advanced token values on **Automatic** unless you have a specific requirement. The trigger controls when to begin, and recent-content tokens express how much original content to retain. Bowra adjusts these targets to fit the model’s capacity and required input; increasing them cannot bypass a provider limit. Clear a numeric field to restore Automatic, or use the reset action to restore all compaction defaults.

Turning Auto-compact off keeps the saved numbers and any summaries already created. When a request needs more room, Bowra leaves older completed context out of that request without generating a summary. The same fallback is available when Auto-compact is on but a summary is unusable. Saved conversation history stays intact, but the model may no longer remember those older details. The current request and latest Tool results stay available. If the required input alone is too large, compact manually, choose a larger suitable model, or reduce the question and material before resending in the same conversation. Summarization uses model calls and shares the task’s existing time and usage limits. Specialized document generation and other dedicated recovery flows retain their own rules.

When the conversation is idle, use **⋯ → Compact context** to organize it manually without changing Auto-compact. See [conversation actions](conversation-workbench-actions.en-US.md#compact-a-long-conversation).

## Configure request parameters

On **Model assignments**, **Knowledge base → Search result count** defaults to **15 results**. Choose 5, 10, 15, 20, 30 or 50; changing the count does not rebuild the index. More results can improve coverage but use more model context. Access and relevance checks can return fewer results. Existing custom counts between 1 and 50 remain selectable.

Open **Request parameters** in a Chat, Tool or Vision model row and select a parameter profile that matches the provider's documentation for that exact model and API. A profile describes how parameters are sent; it does not prove the provider supports them. Leave it unknown when you have not confirmed the reasoning dialect.

Edit **Model defaults** for shared settings. Use **Capability overrides** only for exceptions, such as Tool thinking On while Chat and Vision use Off. Only edited fields become exceptions. Choosing inheritance removes an exception; **Do not send** removes a request field instead. An always-thinking profile has no Off choice. Thinking budgets must leave room within the output limit, and incompatible sampling settings must be corrected before saving.

Generation controls include temperature, top_p and output limits; parallel tools use an On/Off selector, not a number. **Advanced parameters** accepts exact field names and JSON values where the connection supports them. Do not enter prompts, credentials, headers or runtime-owned controls. Invalid JSON stays in your draft until corrected. Existing capability JSON keeps its scope; **Review conversion** shows supported conversions and needs confirmation before changing the draft.

Choose a capability and **Refresh preview** to inspect a safe synthetic request projection and each value's source. Preview sends no provider request and saves nothing. Unknown advanced values are hidden and remain unverified. Editing makes the prior preview outdated. To test provider acceptance, use the existing **Plan verification** and confirm its request count and possible cost. Excessive fixed budgets are not silently reduced; an untestable item explains the safe limit and recovery choices.

Save the connection, then reopen it to confirm the values. If a review dialog appears, resolve any validation issue or accept the stated risk before applying. Preview or verification alone does not make a draft effective.

## Set production assignments

The **Model assignments** tab groups current selections into conversation and knowledge tasks. Each row shows the actual model, whether it is specified, follows the primary model or is selected automatically, and available verification evidence. Unconfigured and unused roles remain visible. Choose **Edit assignments** to make changes. In the editor, set the primary model and fallback order first; expand the optional lightweight/evaluation and knowledge tasks when needed. The retrieval-count setting is collapsed separately.

Choose **Show verification details** for capability records, times, recent attempts and individual verification actions. Expanding details does not call the provider. Verification may incur charges and does not save configuration. The editor keeps the saved-assignment overview on the parent page; **Review and apply** shows applicable evidence and before/after assignment changes before confirmation. Only applying saves the draft. A successful apply shows **Assignments updated**. Retry settings retain their own **Save retry settings** button and save separately.

**Back to overview** opens the parent **Model assignments** tab, including after a direct visit to the editor. The selected overview tab is preserved when you reload the page.

While editing, choose **Verify draft assignments** to check the models selected in your draft. Review the request count and possible cost before starting. Verification does not apply your assignments or discard the draft. Connections with the same display name show their connection identifiers so you can distinguish the before/after selections.

The evaluation model also scores Agent evaluations, point calibration and Coach checks; by default it is **Auto**, a model different from the main one: a chat model at another provider first, otherwise another chat model in the same connection. The collapsed **Advanced** row names the model Auto picked. If the main model is your team's only chat model, the page says evaluation can only use the main model; add another chat model to a connection to fix that. Agent evaluation never asks for a separate model or address.

Choose **Edit assignments** to set primary, backup, lightweight, and evaluation models, plus models for knowledge vectors, reranking, document vision, and row extraction. Only models in saved connections are available. Verify the capabilities needed by each selected assignment, then confirm the evidence shown on the page.

Assignments refer to the exact model ID in a connection. When automatic maintenance is enabled, an available, freshly verified model can fill an unconfigured assignment after a connection is created or edited, or after verification completes. Existing valid choices stay unchanged. Removing a model or a required capability selects a compatible verified replacement where possible, otherwise clears the affected assignment. Review the changes before applying. An explicitly disabled optional assignment stays off; source descriptions stay off by default. Embedding models never switch automatically: select their replacement and complete the original index-rebuild confirmation. Without automatic maintenance, replace affected assignments explicitly before removing an ID. The model ID must be accepted by the provider; renaming it in Bowra does not change the provider's model catalog.

On **Model assignments**, expand **Show verification details** to view the verification record for each model use. The items that most need attention are shown first; when there are many records, choose **Show N more verification items** to see all of them, and **Show fewer verification items** to return to the short list. Each record shows the use, the model, the capabilities it will use, the verification status and time, and the latest attempt. To verify one item again, choose **Verify** on it, then **Start** to confirm. You can also use **Verify current assignments** to choose several items at once. Verification sends requests to the model provider. **Passed** means the check succeeded. **Recheck recommended** means the previous check passed, but enough time has elapsed that another check is recommended. Network errors or cancellation do not clear previous successful results; a single network error does not mean the configuration is unusable.

## Automatic retries on failure

On **Model assignments**, **Model behavior** has one setting: **Automatic retries on failure**. When a model call fails, for example because it timed out, the connection failed or dropped partway through a reply, or the service had a temporary error, Bowra tries the call again up to this many times. The default is 3 and the allowed range is 0 to 5; enter 0 to turn retries off. Choose **Save retry settings** next to the field; the setting saves on its own and does not change model assignments.

If the connection drops after the model has started replying, the partial reply disappears and the model answers again from the start, so an answer is never shown twice. An interrupted call is not charged to your credits. Every retry counts against the turn's time limit; when too little time is left, Bowra stops retrying and reports the failure. While a retry is pending, the conversation's running status shows, for example, "Model reply timed out, retrying (1/3)". If the page says the configuration changed since it loaded, reload the page and set the value again.

## External capabilities

Choose **External capabilities** in model settings to open its own page and review image generation and web search. Choose **Configure** or **Edit configuration** in the image or search row to open its settings. The return button in either editor opens the external capabilities overview.

Choose a saved connection, enter the service model and request method, then select the model the team will use. Manage its address and credentials under **Model connections**. Review index updates and retry failures from **Knowledge base → Files**. Change knowledge base models under **Model assignments**. A saved connection does not mean it has been assigned to production or is ready for use.

**Save** on a connection validates and stores the change, then returns to the model settings list. If a review dialog appears, resolve any validation issue or accept the stated risk before applying. Other sections use **Review and apply**. Reopen the relevant section to confirm the displayed values. Model choice in a conversation is limited to the options made available by the team.

## Connect through Bowra

If your administrator enables the service, **Use Bowra service** appears beside **Add connection**, including when the team has no connections. Only team owners and administrators can use it. Use any valid HTTP/HTTPS application address, including IP, localhost, intranet names and custom ports. The browser returns to this same address and deployment prefix; no public-entry or callback setting is needed. Keep the same browser and sign-in session until the return completes.

Choose **Use Bowra service**, sign in to Bowra and approve access there. On return, the connection editor contains the imported address, credential and models. A verification plan for Chat and Tool capabilities is prepared automatically. Check its request count and possible cost, then choose **Start** to test it; provider requests never start automatically. Model names and declared capabilities are not verification results. Choose **Save** to store the connection, completing the original risk review if it appears. You can also save first and verify later. Existing model assignments stay unchanged; when automatic maintenance is enabled, freshly verified candidates can fill unconfigured roles.

New Bowra imports automatically append `/v1` to the service address when it is missing, preserving any existing path and avoiding a duplicate `/v1`. Saved connections are not changed automatically. If an existing Bowra connection is missing `/v1`, edit its service address, add `/v1` at the end, then review and apply the change.

If Bowra has no models, no connection is imported. Cancelling authorization leaves existing settings unchanged. Every approved authorization creates a new key in Bowra, even if the exchange fails or you discard the draft. If the result is uncertain, or you abandon an imported draft, find the individual key in Bowra **API Keys** by application name and creation time and disable or delete it if necessary before reconnecting. Do not repeatedly authorize to refresh the model list. Removing a connection here deletes the local credential copy; it does not revoke the Bowra key.

## When verification returns no answer

An HTTP 200 response means the service replied; it does not prove that the model returned a usable answer. An empty answer, thinking content without an answer, or an incompatible response format can produce this result. It does not necessarily mean a connection failure.

1. Choose **View response details** in the verification result. Team owners and administrators can inspect the returned model, stop reason, recognized content types, valid token counts, selected test parameters and the service response.
2. If the service reports a reply length limit, check the output limit and thinking settings before verifying again. If it reports a normal end without an answer, ask the model service administrator to check the model and response format. Missing information is shown as **Not reported by the service**; the page does not guess a cause.
3. Choose **Copy diagnostic details** before saving the connection, closing the result or refreshing the page if you need the details later. Finished results do not reopen automatically. If automatic copying is unavailable, use the manual-copy prompt.

The test parameters show the main model, output and thinking settings where present; they do not include authentication or the full request. The usage summary contains recognized numeric token counts. Other provider fields remain in the response excerpt. JSON is formatted for reading while whitespace inside response strings is preserved. Known credentials are hidden, including values repeated in other fields or stream events; check the contents before sharing. Response excerpts are limited to 64 KiB after credentials are hidden, and copying includes the same excerpt.

Earlier attempts or attempts whose details could not be saved may have no details. Run verification again to collect them, reviewing its request count and possible cost first. Viewing or copying existing details sends no model request and does not save configuration changes.
