# Manage team OpenAPI access

Team owners and administrators can open **Team settings → OpenAPI** when the feature is available.

1. In **Manage keys**, add a key with a name, the scopes the integration needs, and an expiration. A blank expiration uses the 30-day default. Select the Agent CI preset if the external pipeline needs to propose and evaluate Agent changes.
2. Copy the token when it appears and store it with the calling system’s credentials. The full token is displayed only once.
3. In **API documentation**, find the base path, exact endpoint, required scope, and example requests. The calling system sends the key as a Bearer token. Use the service address that the calling system can reach, including the deployment prefix. You can download the reference as Markdown.
4. Check **Call logs** for the key, action and result if a call fails. Compare the requested endpoint with the active key’s scopes and expiration. The log shows safe summaries.

Disable or revoke a key to stop its access, including a pipeline using it. Regenerating a key replaces the old token. Update the caller before relying on it again. Revocation keeps the historical call log.
