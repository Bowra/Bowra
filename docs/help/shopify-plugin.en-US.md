# Connect Shopify for analysis and content

A Shopify connection lets selected Agents read your shop's products, orders, current inventory, collections, pages and articles. Customer information is excluded. Available operations depend on the read permissions Shopify actually grants your App.

The Team owner or administrator manages the connection. Members use its Tools through an Agent configured with the Plugin. The Plugin does not expand a member's permissions.

## Connect your shop

1. Create or use a Shopify App in the same Shopify organization that owns your shop. Release its configuration and install it on the shop.
2. Grant the read permissions needed for your task: products, orders, inventory, locations and online store content. Orders older than 60 days also need approved all-orders access. Customer permissions are unnecessary for this Plugin.
3. Open **Settings → Tools**, choose **Shopify**, and enter a connection name, the `store.myshopify.com` domain, Client ID and Client secret.
4. Choose **Verify and save**. The application verifies the shop and stores the credentials encrypted. Saved secrets are never displayed.
5. Choose **Create analysis Plugin** on the connection. In the Agent editor, select that Plugin and use the existing validation and publication flow for the Agent. Each Plugin binds to the selected shop's Tools and the Shopify analysis Skill.

If Shopify is absent from the Tools picker, contact your deployment administrator. This experimental integration is disabled by default in the source preview; inclusion of its source does not enable it.

## Ask operational questions

Examples include “Compare last month's order volume with the previous month,” “Find tracked SKUs with low available stock,” and “Extract product descriptions and shipping-policy copy.” Specify the shop and date range when several connections are available.

Order summaries exclude test orders and, by default, cancelled orders. They use the shop's timezone and current order snapshots, with amounts separated by currency. Current order value and average current order value are **not Shopify native sales or AOV**. Refunded amounts belong to the selected orders' lifetime, not necessarily the requested period.

Each read has bounded pagination. An incomplete list is not a complete total. Summaries refuse totals beyond 1,000 orders or the time budget; use smaller disjoint date ranges when needed. Inventory is current stock; untracked stock is unknown, not zero. ShopifyQL, customer analysis, traffic, conversion, attribution and historical profit are unavailable in this release.

## Improve product and page copy

When available, choose **Create content Plugin** on the connection and select it in the Agent editor. It uses a separate content Skill. The analysis Plugin remains for reading and analysis. Product updates need granted product write access; ordinary page updates need granted online-store page/content write access. After changing the Shopify App's permissions and approving them in Shopify, edit and verify the connection again. Without write access, the Agent can still prepare a draft.

Ask for suggestions to receive copy without saving, or explicitly request an update. Product title, description and SEO title/description are supported; ordinary pages support title and body. Theme layout, publication state, URL handles, page SEO, price and stock are outside this workflow. A page containing shipping text is an ordinary page, not Shopify's dedicated shipping-policy setting.

For an update, review the shop, target and full before/after content in the browser dialog, then choose **Apply these changes** or **Reject update**. HTML appears as text so it can be inspected. Ignoring the dialog lets it expire without updating. Only the requester can decide. Updates require a live browser chat and current member access to its Space. If another editor changes the original content before submission, prepare a fresh review; coordinate when editing together because Shopify cannot make the original-content check atomic with the update.

A saved result is reported only after the content is read back and verified. If the write result is uncertain or verification disagrees, inspect the current Shopify object before proposing a correction; do not repeat the same update blindly. Verification does not establish storefront visibility or improved search/conversion performance.

## Maintain access

Edit the connection to replace a Client secret; leave its field blank to keep the saved secret. Reset secrets in Shopify App administration, then update the Bowra connection. Rotation invalidates old executing requests and page continuations. The shop identity cannot be changed on an existing connection; create a separate connection for another shop.

Disable or delete a connection to stop further access. Agents and Plugins retaining its Tool references cannot use it. Restore access by enabling and verifying the intended connection or updating the Agent's resources. The instance encryption key must remain stable across restarts and backups so saved credentials remain readable.
