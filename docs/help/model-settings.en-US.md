# Manage team model settings

Team owners and administrators manage model services in **Team settings → Models**. The page has three tabs: **Model connections**, **Model assignments**, and **External capabilities**. Add services and maintain their models in the first tab. Use the second tab to choose models for team tasks and review verification evidence. Use the third tab to configure image generation and web search. Review index updates in **Knowledge base → Files**.

## Manage connections and model capabilities

On **Model connections**, add a connection or edit an existing one. Enter the provider address, authentication, required parameters, model IDs, and supported capabilities. Save a connection first, then use **Plan verification** to review capabilities, request count, and possible cost before running verification. Verification contacts the provider and may consume credits, but it does not automatically save configuration changes.

For each model in the catalog, set its **Input**, **Cached input**, and **Output** credit multipliers. All three default to 1, including for existing models. A cached input multiplier applies only when the service reports cache hits; other input uses the input multiplier. Use values from 0 to 1,000 with at most six decimal places. After saving, each new model call captures the rates then in effect; earlier calls keep their recorded rates, even if the same task continues. Review the multipliers before changing a model's pricing. See [View and allocate credits](credits.en-US.md) for the calculation and usage details.

If a connection is only for image generation or search, you can leave its model catalog empty and save the endpoint and credentials first. Configure its service model under **External capabilities** afterward.

**Context window (tokens)** is the model’s total context capacity, including instructions, conversation, Tool input and room for its reply. Enter an exact value when your service documents one. Otherwise leave the field empty to inherit the connection value, reviewed model catalog value or system default shown on the page. A displayed suggestion is not automatically saved as your own override. Changing this declaration cannot enlarge the provider’s actual limit.

Changing the address, credentials, model ID, or capability parameters can invalidate earlier verification. Before removing a model or connection that is still used, replace it in production assignments or external capabilities.

For compatible Chat Completions services hosted on your own infrastructure or a cloud platform, streaming uses the saved service address. When a task requires confirmed usage, the service must return input and output token counts in its streaming response. If it does not, ask the service administrator to enable that support or select a compatible model; Bowra cannot confirm usage from a missing response. A capability verification result is a small connection test, so also try a representative chat task after changing the service.

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

The evaluation model also scores Agent evaluations, point calibration and Coach checks; by default it is **Auto**, a model different from the main one: a chat model at another provider first, otherwise another chat model in the same connection. The collapsed **Advanced** row names the model Auto picked. If the main model is your team's only chat model, the page says evaluation can only use the main model; add another chat model to a connection to fix that. Agent evaluation never asks for a separate model or address.

Choose **Edit assignments** to set primary, backup, lightweight, and evaluation models, plus models for knowledge vectors, reranking, document vision, and row extraction. Only models in saved connections are available. Verify the capabilities needed by each selected assignment, then confirm the evidence shown on the page.

Assignments refer to the exact model ID in a connection. When replacing the only conversational model in a connection, its assignments move to the new ID with the draft; review those changes before applying. If a connection has several conversational models, replace affected assignments explicitly before removing an ID. The model ID must be accepted by the provider; renaming it in Bowra does not change the provider's model catalog.

On **Model assignments**, **Production assignments and verification evidence** shows the verification record for each model use. The items that most need attention are shown first; when there are many records, choose **Show N more verification items** to see all of them, and **Show fewer verification items** to return to the short list. Each record shows the use, the model, the capabilities it will use, existing verification evidence, and the latest attempt. To verify one item again, choose **Verify** on it, then **Start** to confirm. You can also use **Verify current assignments** to choose several items at once. Verification sends requests to the model provider. Results are for troubleshooting; a single network error does not mean the configuration is unusable.

## Automatic retries on failure

On **Model assignments**, **Model behavior** has one setting: **Automatic retries on failure**. When a model call fails before the model has replied at all, for example because it timed out, the connection failed, or the service had a temporary error, Bowra tries the call again up to this many times. The default is 3 and the allowed range is 0 to 5; enter 0 to turn retries off. Choose **Save** next to the field; the setting saves on its own and does not change model assignments.

Retries only happen while the model has not started replying, so an answer is never sent twice. Every retry counts against the turn's time limit; when too little time is left, Bowra stops retrying and reports the failure. While a retry is pending, the conversation's running status shows, for example, "Model reply timed out, retrying (1/3)". If the page says the configuration changed since it loaded, reload the page and set the value again.

## External capabilities

Choose **External capabilities** in model settings to open its own page and review image generation and web search. Choose **Configure** or **Edit configuration** in the image or search row to open its settings. The return button in either editor opens the external capabilities overview.

Choose a saved connection, enter the service model and request method, then select the model the team will use. Manage its address and credentials under **Model connections**. Review index updates and retry failures from **Knowledge base → Files**. Change knowledge base models under **Model assignments**. A saved connection does not mean it has been assigned to production or is ready for use.

**Save** on a connection validates and stores the change, then returns to the model settings list. If a review dialog appears, resolve any validation issue or accept the stated risk before applying. Other sections use **Review and apply**. Reopen the relevant section to confirm the displayed values. Model choice in a conversation is limited to the options made available by the team.

## Connect through Bowra

If your administrator enables the service, **Use Bowra service** appears beside **Add connection**, including when the team has no connections. Only team owners and administrators can use it. Open this application through an HTTPS website domain on port 443; Bowra cannot return authorization to HTTP, localhost or an IP address.

Choose **Use Bowra service**, sign in to Bowra and approve access there. When you return, the model service address, credential and model names are imported into an OpenAI-compatible draft. The configuration is not effective yet. Review the connection and its intended Chat/Tool capabilities; the names returned by Bowra do not prove those capabilities. Use the existing verification actions when needed. For a team without a main model, a single returned model is preselected; with multiple models, choose the main model. Existing main-model assignments stay unchanged. Choose **Review and apply** and complete any required confirmation to make the configuration effective.

New Bowra imports automatically append `/v1` to the service address when it is missing, preserving any existing path and avoiding a duplicate `/v1`. Saved connections are not changed automatically. If an existing Bowra connection is missing `/v1`, edit its service address, add `/v1` at the end, then review and apply the change.

If Bowra has no models, no connection is imported. Cancelling authorization leaves existing settings unchanged. Every approved authorization creates a new key in Bowra, even if the exchange fails or you discard the draft. If the result is uncertain, or you abandon an imported draft, find the individual key in Bowra **API Keys** by application name and creation time and disable or delete it if necessary before reconnecting. Do not repeatedly authorize to refresh the model list. Removing a connection here deletes the local credential copy; it does not revoke the Bowra key.
