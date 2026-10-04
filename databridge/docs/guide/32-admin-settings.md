---
title: Server settings
icon: SETTINGS
permission: manage_users
pages: []
keywords: environment, settings, env, configuration, deploy, docker, native, vps, backup, proxy, no_proxy
---
# Server settings (admins)

DataBridge is configured with environment variables in the server's `.env` file. It runs with Docker, or natively as a service. Restart the service after changing them.

{{settings}}

## Backups

Back up two things:

- **The database:** PostgreSQL in Docker, or `databridge.db` for SQLite.
- **The data folder:** snapshots, datasets and AI run files.

Keep `DATABRIDGE_SECRET_KEY` safe and unchanged: stored passwords and keys can't be decrypted without it.

## Proxies

If the server reaches the internet through a proxy, add internal hosts (databases, LLM gateways) to `NO_PROXY`. For example: `NO_PROXY=localhost,127.0.0.1,.corp.example`.
