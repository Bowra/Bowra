# Initialize a Bowra Community installation

English | [简体中文](community-setup.zh-CN.md)

Community runs without a commercial license key, renewal or built-in user limit. Supply your own infrastructure and model credentials. Team permissions, account suspension and configured credit budgets still apply.

## First start

1. Use a Community application build and a new persistent data directory. Preserve the generated `BOWHEAD_FERNET_KEY` and `BOWHEAD_JWT_SECRET` in the deployment configuration. Changing the encryption root can make saved credentials unreadable.
2. Start the application using its packaged startup command. It checks installation provenance before running the immutable database migrations, then records the new Community installation. Existing unknown or Business databases are refused before migration; use the original Business build with those databases.
3. On the application host, retrieve the one-time first-owner proof:

   ```sh
   docker compose exec app /app/.venv/bin/python -m bowhead_gateway.installation setup-token
   ```

   For a local Python checkout, use `uv run python -m bowhead_gateway.installation setup-token` with the same database and environment. The command prints the proof only while first-owner setup is pending. Do not publish it in an issue, screenshot or log.

4. Open the application's `/activate/` page, enter the proof, Team name, owner email and a strong password. Only one submission can claim the installation; repeated or concurrent submissions cannot create another owner.
5. Sign in using the created account, configure Team model credentials, and complete the normal setup checklist. Add members through the supported Team administration flow. Invitation/recovery email requires configured email delivery and an appropriate public entry.

The proof derives from the persistent encryption root and installation identity. It stops working after setup completes. Ordinary API writes and account creation are unavailable until setup finishes. The public setup-status API never reveals the proof or owner credentials.

## Restart, upgrades and recovery

Restarting a pending or initialized Community installation preserves its identity. Back up the complete database/object data and persistent keys together. If the very first migration is interrupted before provenance is recorded, admission refuses the resulting unknown database. Inspect and restore the paired pre-start backup; never label an unknown existing database Community just to bypass admission.

To upgrade a completed Community installation to Business, stop all Community application and worker processes, back up the recovery set, preserve all keys/data, and use the same or a newer Business application version. The first Business admission records the edition transition. Sign in as an existing Team owner or administrator and upload a valid signed license for the existing Team ID through Business administration. This preserves users, ownership and resources; the public first-owner activation route cannot create a replacement administrator. Business seat, expiry and grace rules apply after the upgrade. Automatic Business-to-Community conversion is unsupported, including after a license expires. Mixed Community and Business processes against one database are unsupported.

For manual development startup, run the packaged preparation before starting Uvicorn:

```sh
uv run python -m bowhead_gateway.installation prepare --migration-command .venv/bin/bowhead
```

This requires the migration-managed `bowhead.db` path and processes all eight database targets. `BOWHEAD_DEPLOYMENT_MODE` continues to control outbound URL policy; it does not select the edition or bypass installation admission.
