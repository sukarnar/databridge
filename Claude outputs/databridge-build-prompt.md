# Prompt: build DataBridge

Build **DataBridge**, a self-hosted platform where business users map messy source data (spreadsheets, files, database tables, REST APIs) to a clean target schema with drag-and-drop arrows and Excel-style formulas, then serve the result as versioned REST endpoints. It also includes governed AI features: a prompt playground, AI-assisted mapping, and AI workflows with guardrails.

## Stack and shape

- **Language:** Python 3.12 (also supports 3.11 and 3.13).
- **One process, two parts:**
  - **Studio:** a browser-based web UI mounted at `/`.
  - **Broker API:** FastAPI at `/api/v1`, with interactive docs at `/api/docs`.
- **Data:** Polars for data, DuckDB for SQL over frames, Parquet snapshots and datasets, SQLAlchemy 2 metadata on SQLite (default) or PostgreSQL, httpx for HTTP, sqlglot for SQL safety checks, Jinja2 (sandboxed) for prompt templates.
- **Configuration:** `DATABRIDGE_*` environment settings (pydantic-settings) and `.env.example`.
- **No proprietary libraries.** The UI must never run blocking I/O on the server's event loop: all network, database and model calls run in worker threads.

## Core data flow

1. **Sources.** Each refresh or upload is an immutable, versioned snapshot.
   - **Spreadsheet/CSV upload with a Sheet Profile wizard:**
     - detects the header row, skips banner, blank and "Total" rows;
     - fills merged cells and keeps leading zeros;
     - lets the user override types.
     The profile is saved and reused for new files. Identical files are skipped by hash. Schema drift is detected and flagged.
   - **Connections** (credentials encrypted; admins only):
     - file systems through fsspec (local, SMB, SFTP, FTP, S3, Azure, GCS), with file patterns;
     - databases (Oracle, SQL Server, PostgreSQL, MySQL, SQLite), as tables, views or read-only custom SQL;
     - REST APIs (see below).
   - **Explorer:** browses connections lazily and adds an object as a source.
   - **Refresh:** a Refresh button, an API call, or a built-in scheduler (per-source interval, 5 minutes minimum). A snapshot is stored only when the data changed (row-hash fingerprint). Mappings that use the source are then republished automatically.
2. **Targets:** schemas with fields, types and required flags, created by:
   - uploading a header-only template workbook;
   - defining fields by hand;
   - "New target from the source's columns".
3. **Mapping Studio:**
   - **Rules:** drag source fields onto target fields, or write formulas per field in an Excel-like formula language. The language is compiled to Polars expressions, sandboxed, and covers IF/AND/OR/IN/CONTAINS, text, number and date functions, and `&`.
   - **Row steps:** filter, dedupe, sort, unpivot.
   - **Validations:** extra data-quality rules on target fields.
   - **Auto-map:** fuzzy name matching.
   - **AI suggest:** sends only names and types to external models (sample values to internal models only).
   - **Live preview:** rejected cells shown in red.
   - **Publish:** writes a versioned Parquet dataset plus an XLSX reject report.
4. **Endpoints** on published datasets:
   - **Addressing:** slug URLs `GET /api/v1/data/{slug}`, plus `/schema`.
   - **Querying:** query-parameter filters mapped to columns (operators), paging (`page`, `page_size`, `total`, `pages`), JSON/CSV/XLSX output, pinned versions.
   - **Access:** public or API-key protected.
5. **API keys:**
   - stored hashed, shown once;
   - scoped to endpoints, to `workflow:<slug>`, or to all;
   - all-scope keys can also call `POST /api/v1/ingest/{source_id}` (file upload), `POST /api/v1/sources/{id}/refresh`, `GET /api/v1/sources`, and runs/rejects.
6. **Runs:** history of extract, publish and ingest runs, with row counts, status, messages and reject downloads.

## REST API sources

- **Connection:**
  - base URL; requests may only go to the same origin, and redirects or next links to other servers are refused;
  - auth: none, API key (header or query), bearer, basic, or OAuth2 client credentials (token cached, refreshed on 401);
  - extra and secret headers; POST off by default; test path and timeout;
  - requests per minute (thread-safe throttle) and parallel requests (1–8);
  - TLS: system, company CA, or off; mutual TLS with cert/key or .p12.
- **Request builder** in the Explorer: method, path, query and header lines; format (auto/JSON/CSV/XML); records path; "explode" a nested list into rows; max rows and pages; columns to keep. Also lists GET operations from an OpenAPI document when the API publishes one.
- **Pagination:**
  - page number and offset (fetched in parallel, results kept in order);
  - cursor, next-URL in the body, and Link header (sequential).
  - Stops on an empty or repeated page, a total, or limits; truncation is flagged.
- **Incremental loads:** templates `{{today}}`, `{{days_ago:N}}`, `{{last_refresh}}`; append or upsert mode with key columns; an empty answer keeps the existing data.
- **Safety and parsing:**
  - secrets are redacted from errors and logs;
  - responses are capped at 200 MB after decompression (gzip-bomb safe), and DOCTYPE is refused in XML;
  - retries with Retry-After on 429/5xx; 10 s connect timeout with a clear "server can't reach the API" message;
  - nested JSON is flattened to dotted columns.
- **Progress:** live progress and Cancel in the Add-as-source dialog; a timing summary (pages, rows, API time per page, processing time) on each refresh.

## Accounts and security

- **Roles:** Admin, Designer, Viewer. Permissions are enforced on the server for every action. Admins create accounts; there is no sign-up.
- **Passwords:**
  - scrypt hashes, a complexity policy, and a forced change on first sign-in;
  - lockout after 5 failures, with identical timing and messages for unknown users;
  - CLI commands (`python -m databridge.manage`) for create-admin, reset-password, unlock, disable and enable.
- **Sessions:** server-side, token stored as a SHA-256 hash; 12 h absolute, 60 min idle, or 14 days with "keep me signed in". Rechecked on every page change.
- **Audit log:** of all changes, with user and IP.
- **Transport security:**
  - an ASGI middleware enforcing HTTPS and wss (redirects plain http, refuses ws);
  - origin checks against cross-site websocket hijacking;
  - websocket connection budgets (total, streaming, per IP) and per-path message size limits;
  - HSTS and security headers; a trusted-proxies setting;
  - optional built-in TLS (`python -m databridge.serve`) with TLS 1.2+ and optional client-certificate auth;
  - a streaming wss API for queries.
- **Admin Security page and `/health`:** the page shows the setup and any problems; `/health` reports one word (ok, warning or error).

## AI

- **Gateway:** every model call goes through one gateway, which checks the role, model approval, key mode and the monthly token budget, then logs usage. Prompt text is not logged by default.
- **Providers:**
  - users' own encrypted keys for OpenAI, Anthropic, Gemini, Groq, Mistral, OpenRouter, Azure, or any OpenAI-compatible service;
  - company/shared endpoints (Ollama, vLLM, LM Studio, a gateway), each marked *internal* or *external* network;
  - key modes: one company key, each user's own key, or no key;
  - model catalog: display names, role limits, admin approval, default models;
  - YAML config file (also imports the Continue `config.yaml` format), applied at start;
  - health checks with certificate-expiry alerts to the audit log and a webhook;
  - company CA and mutual TLS.
- **AI page:** Playground, Prompt library (versioned, publish and restore), Models, Usage.
- **Data classification:** per source column (public, internal, PII, confidential), with "Suggest from data". The classification follows the data through formulas, SQL lineage and outputs.
- **AI workflows:** a node canvas whose rows flow as Polars frames with hidden `_row_id` and `_review` columns.
  - **Nodes:**
    - Dataset rows (source or published mapping; columns, filter, limit);
    - Run input (form or API payload);
    - Transform (formula columns and a filter);
    - SQL (one read-only DuckDB SELECT over `rows`; no file functions, with time and memory limits);
    - Router (yes/no ports);
    - LLM;
    - Output dataset (writes a workflow-output source, with flagged rows optional through an `ai_review` column).
    Branches can merge.
  - **LLM node:**
    - model;
    - inline prompt or a library prompt (pinned version);
    - variables `{{ data }}` (wrapped in `<data>` tags with a "this is data, not instructions" notice), `{{ row.x }}`, `{{ rows }}`, `{{ rows_table }}`, `{{ input.x }}` (data variables are refused in system prompts);
    - per-row or batch mode; columns sent; temperature, max tokens, parallel calls and retries with feedback;
    - output as text or typed JSON fields (string, integer, number, boolean, enum), validated.
  - **Guardrails:**
    - network rules (confidential columns blocked for external models; PII masked);
    - PII modes: auto, pseudonymize-and-restore, mask, block, off (internal only);
    - prompt-injection detection: flag, reject or stop;
    - formula rules on replies; "no invented values" (must match the row, a column's values, or a list);
    - banned patterns, a max reply length, a max prompt size, and a PII-leak check;
    - on failure: flag, reject or stop.
    Rejected and flagged rows go to a review table with the node and reason.
  - **Lifecycle:**
    - Check & estimate: a dry run with no model calls, showing calls, tokens, what is sent and warnings;
    - Test on 3 rows, recording the exact masked prompts and replies;
    - Run, with confirmation above a token threshold;
    - Publish as a version;
    - per-workflow limits: max rows, max tokens per run, confirm-above, monthly budget.
  - **API:** `POST /api/v1/workflows/{slug}/run` (input, limit, confirm) runs the published version as the owner (409 needs confirmation, 422 blocked); `GET /api/v1/ai-runs/{id}` returns a run's results.

## Product quality

- **Plain-language UI:**
  - dialogs show progress and errors inside the dialog, full width above the buttons;
  - native-library panics are caught too;
  - long operations run in the background, can be cancelled, and ignore repeat clicks;
  - path inputs are forgiving (quotes, backslashes, a file instead of a folder).
- **In-app user guide:** Markdown topics filtered by role, with search, "help for this page", and generated reference sections (formula functions, settings). Architecture and workflow diagrams come from a script that renders SVG and PNG. It includes step-by-step workflow examples that are covered by tests.
- **Branding:** a configurable app name, logo and colors.

## Deployment and tests

- **Running it:** one command starts the server (`python -m databridge.serve`), configured through `DATABRIDGE_*` settings. A `/health` check reports the running release, so a deployment can verify it and roll back if needed.
- **Tests:** pytest on SQLite and PostgreSQL in CI. They include a fake API server for REST paging/auth/formats/safety, a fake LLM for workflows and guardrails, TLS and websocket security, database column limits, UI dialog behavior, and the guide's examples run end to end.
