# Feishu webhook fixtures

`encrypted_webhook_real.json` is intentionally absent until a real Feishu
encrypted webhook callback has been captured and sanitized.

Generate it with:

```bash
uv run --package bowhead-gateway python scripts/feishu_webhook_fixture_tool.py sanitize \
  --input /secure/path/real-feishu-webhook-capture.json \
  --output packages/bowhead_gateway/tests/fixtures/feishu/encrypted_webhook_real.json
```

The committed fixture must use fixture-only token/key values and must be marked:

```json
{
  "derived_from_real_feishu_callback": true
}
```

Never commit production Feishu secrets or raw decrypted payloads.
