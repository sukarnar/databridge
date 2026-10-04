---
title: Architecture
icon: ACCOUNT_TREE_OUTLINED
permission: view
pages: []
keywords: architecture, diagram, overview, components, how it works, design, data flow, storage, deployment
---
# Architecture

DataBridge is a single Python application. The studio you are using and the REST API that other systems call run in the same process. They share one metadata database and one data folder.

## System overview

![DataBridge system overview](guide/architecture-overview.png)

| Part | What it does |
|---|---|
| **Studio UI** | The pages you use in the browser. It is built with Flet and served by the same server as the API. |
| **Broker API** | REST at `/api/v1`: endpoints that serve data, ingest and refresh triggers, run status and AI workflow runs. Interactive docs are at `/api/docs`. The streaming API at `wss://.../api/v1/stream` pushes publish and run events and streams rows. Both use API keys. |
| **Services** | Ingest (sheet profiles), connectors, the mapping engine (formulas compiled to Polars), publishing, endpoint queries (DuckDB), authentication and audit, and the AI gateway, workflows and guardrails. |
| **Metadata database** | Definitions and history: sources, targets, mappings, endpoints, users, keys, runs and the audit log. PostgreSQL with Docker, or SQLite. Passwords and API keys are stored encrypted. |
| **Data folder** | The data itself: Parquet snapshots of every source version, published dataset versions, reject reports and AI run files. |
| **External systems** | File shares, cloud storage, databases and REST APIs are only read, so use read-only accounts. Company and cloud LLMs are called only through the AI gateway, after the guardrails. |

Everything you publish is **versioned**. A new source version creates a new snapshot. Republishing creates a new dataset version. Endpoints serve the latest published version.

## How data flows

![How data flows from a source to an endpoint](guide/architecture-data-flow.png)

1. **Source.** Upload a spreadsheet, pick a file or table from a [connection](guide:connections), or call a [REST API](guide:rest-sources).
2. **Ingest.** The sheet profile (header row, skipped rows, column types) is applied the same way to every new version. See [Sources](guide:sources).
3. **Snapshot.** Each version is stored as Parquet. If a column appears, disappears or changes type, you see a drift warning. If a file is identical to the last one, it is skipped.
4. **Mapping.** Row steps, then rules and [formulas](guide:formulas), then type conversion, then data checks. See [Mapping Studio](guide:mapping-studio).
5. **Dataset.** Good rows are published. Rows that fail are set aside in the reject report with a reason, and they never block the good rows.
6. **Endpoint.** Consumers fetch the data with filters and paging, as JSON, CSV or Excel. See [Endpoints](guide:endpoints) and the [REST API](guide:rest-api).

Automation uses the same path. A scheduler calls the ingest or refresh API, and every mapping that uses the source republishes on its own. See [Runs](guide:runs).

## How an AI call works

![How an AI call passes the guardrails and the model gateway](guide/architecture-ai.png)

Every AI feature uses the same path: the playground, AI suggestions in the Mapping Studio, and LLM steps in [workflows](guide:ai-workflows).

- **Before sending**, the [guardrails](guide:ai-guardrails) check data classification and network rules, mask personal data, look for prompt injection and enforce row, token and budget limits.
- **The model gateway** checks that your role may use the model, picks the right key (the company key, your issued key or your own cloud key), applies the company certificates, and records usage.
- **After the reply**, the output is checked against the expected format and rules. Each row then passes, is flagged for review, or is rejected with a reason.

Company models and keys are explained in [Company models](guide:company-models).

## Deployment

![Deployment options: Docker or native](guide/architecture-deployment.png)

DataBridge runs on a Linux server, for example a Hostinger VPS, and is deployed from GitHub. You can run it in Docker behind Traefik, or natively as a systemd service behind Nginx. Both use the same application and the same settings, and both check `/health` after every deploy so they can roll back. Server settings are listed under [Settings](guide:admin-settings).

Every connection is encrypted: the studio's websocket and the streaming API only accept `wss://`, and a page from another website can't open them. If there is no proxy, DataBridge can serve `https://` and `wss://` itself. See [HTTPS and secure websockets](guide:admin-security) (admins).
