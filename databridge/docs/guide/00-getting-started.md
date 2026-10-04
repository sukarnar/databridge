---
title: Getting started
icon: ROCKET_LAUNCH
permission: view
pages: [dashboard]
keywords: overview, sign in, roles, menu, workflow, what is
---
# Getting started

DataBridge turns spreadsheets, files and database tables into clean, consistent data that other systems can fetch over REST. You describe what the result should look like, which is the **target**. Then you draw arrows from your **source** columns to it, and publish.

## The typical flow

1. **Get the data in.** Upload a spreadsheet under [Sources](app:sources), or pick a table or file from a [connection](guide:connections).
2. **Describe the result.** Create a [target](guide:targets): the fields, types and required columns you want.
3. **Map.** Open the [Mapping Studio](guide:mapping-studio), drag source columns onto target fields, add formulas and checks, and watch the live preview.
4. **Publish.** Publishing creates a versioned dataset. Rows that fail checks are set aside in a reject report.
5. **Serve.** Create an [endpoint](guide:endpoints) and issue an [API key](guide:api-keys). Other systems then call the endpoint.

When a new version of the file arrives, upload it (or let a scheduler call the API). DataBridge maps and republishes it automatically, and tells you if the columns changed.

## Roles

| Role | Can do |
|---|---|
| **Viewer** | Look at sources, targets, mappings, endpoints and runs; test endpoints |
| **Designer** | Everything a viewer can, plus create and publish sources, targets, mappings and endpoints; use connections; use AI features |
| **Admin** | Everything, plus users, connections, API keys, company AI models and the audit log |

Menu items you can't use are hidden. This guide shows only the topics that apply to your role.

## The menu

| Menu item | What it's for |
|---|---|
| Home | Quick start checklist and recent runs |
| Connections | Shared folders, SFTP, cloud storage and databases |
| Explorer | Browse a connection's tables and files and add them as sources |
| Sources | Data coming in, each with its version history |
| Targets | The shapes you map to |
| Mappings | Source-to-target mappings; opens the Mapping Studio |
| Endpoints | REST endpoints that serve published data |
| AI | Playground, prompts, workflows, models and usage |
| API keys | Keys that other systems use to call endpoints |
| Runs | History of ingests, refreshes and publishes |
| Users | Accounts and roles (admins) |
| Documentation | This guide |

> **Tip:** press **Documentation** while you are on any page. The guide opens at the topic for that page.

## Your account

Click your initials at the bottom of the menu to change your password or sign out. Sessions end after a period of inactivity. Signing in with "Keep me signed in" keeps you signed in on that device.
