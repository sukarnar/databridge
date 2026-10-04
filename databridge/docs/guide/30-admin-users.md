---
title: Users and security
icon: MANAGE_ACCOUNTS
permission: manage_users
pages: [users]
keywords: user, account, role, password, reset, unlock, lockout, audit, sign out everywhere, admin
---
# Users and security (admins)

## Accounts

Go to [Users](app:users) and click **New user**. Choose the role (see [Getting started](guide:getting-started)). A temporary password is generated. Share it securely; the user must change it at first sign-in.

On a user you can:

- **Change role** or **disable** the account.
- **Reset password:** gives a new temporary password.
- **Unlock:** after too many failed sign-ins, the account locks for a while.
- **Sign out everywhere:** ends all of that user's sessions.

Deleting a user also deletes their personal and company API keys. At least one active admin always remains.

## Audit log

The **Audit log** records sign-ins, failed attempts, changes to connections, endpoints, keys, users and AI settings, and denied actions. It never records passwords or keys.

## Security

The **Security** tab shows whether the studio and API run over HTTPS and secure websockets (wss://). It lists any setup problems with how to fix them, shows open connections, and shows refused connections such as other websites trying to open the studio's websocket. See [HTTPS and secure websockets](guide:admin-security).

## Good practice

- **Least privilege:** give people the viewer role unless they build mappings.
- **Read-only database accounts:** use them for connections.
- **One API key per consumer:** revoke keys that are no longer used.
