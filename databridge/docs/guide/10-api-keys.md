---
title: API keys
icon: KEY
permission: manage_keys
pages: [keys]
keywords: api key, revoke, scope, access, consumer
---
# API keys (admins)

Other systems identify themselves with an API key.

1. Go to [API keys](app:keys) and click **New API key**.
2. Name it after who will use it, e.g. "Finance dashboard".
3. Choose its scope:
   - **Specific endpoints:** read-only access to those endpoints.
   - **Specific AI workflows:** may run those workflows.
   - **All endpoints, plus ingest and refresh:** an admin key for schedulers.
4. Copy the key. **It is shown only once.** DataBridge stores only a hash of it.

**Revoke** stops a key immediately. Give each consumer its own key, so you can revoke one without affecting the others.
