---
title: REST API sources
icon: HTTP
permission: use_connections
pages: []
keywords: rest, api, http, json, xml, csv, endpoint source, pagination, paging, cursor, oauth, bearer, api key, token, schedule, poll, openapi, swagger, incremental
---
# REST API sources

Any web API that returns **JSON, CSV or XML** can be a source. DataBridge calls it, turns the response into rows and columns, and keeps a versioned snapshot, just like a spreadsheet or a table. You then map it to a target, check it and publish it the same way as any other source.

## 1. Connect the API (admins)

Under [Connections](app:connections), click **New connection** and choose **REST API**:

| Setting | What to enter |
|---|---|
| **Base URL** | The API's address, for example `https://api.example.com`. DataBridge only ever sends requests to this server. Next-page links and redirects to another server are refused, so your credentials can't leak. |
| **Auth type** | **API key** (in a header such as `X-API-Key`, or a query parameter), **Bearer token**, **Basic** (user name and password), or **OAuth2 client credentials** (token URL, client ID, client secret, optional scope and audience). DataBridge fetches the OAuth2 token, reuses it until it expires, and gets a new one if the API rejects it. |
| **Headers JSON / Secret headers** | Extra headers, for example `{"Accept": "application/json"}`. Put anything secret (such as `{"apikey": "..."}`) in *Secret headers*, which are stored encrypted. |
| **Allow POST** | Off by default: sources only read with GET. Turn it on only for APIs that search with POST. |
| **Requests per minute / Timeout** | Keeps DataBridge within the API's limits. Busy answers (HTTP 429, 502 to 504) are retried automatically, honouring `Retry-After`. |
| **Parallel requests** | How many pages DataBridge fetches at the same time with page-number or offset paging (1 to 8, default 4). Set 1 for APIs that don't like concurrent calls. The rate limit still applies. |
| **Certificates** | For company APIs: upload your **company CA** (then choose *Company CA*) and, if the API needs mutual TLS, a **client certificate and key** or a `.p12`/`.pfx` file. The private key is stored encrypted. |

All secrets are encrypted and never shown again. **Save and test** calls the *test path*, or the base URL when the test path is blank.

## 2. Build the request

Open the connection in the [Explorer](app:explorer). If the API publishes an OpenAPI (Swagger) description, its GET endpoints are listed on the left; click one to start. Otherwise click **New request**.

| Field | Example | Notes |
|---|---|---|
| Method, Path | `GET /v1/orders` | The path is relative to the base URL. |
| Query parameters | `status=open` | One per line. |
| Response format | Detect | Or JSON, CSV or XML. |
| Records path | `data.items` | Where the rows are in the JSON. Leave it blank and DataBridge finds the list. For XML, give the record element, for example `.//order`. |
| One row per item of | `lines` | Optional. Turns a nested list into rows. Each order line becomes a row that repeats its order's fields. |
| Pagination | Page number | See below. |
| Max rows / Max pages | 1000000 / 1000 | Safety limits. |

Nested objects become columns with dots, for example `customer.address.city`. Lists that are kept as they are become JSON text. A column that mixes numbers and text becomes text; the mapping converts types anyway.

**Pagination:** choose the style your API uses.

| Style | You set | DataBridge stops when |
|---|---|---|
| Page number | page parameter, optional page size parameter and size, optional *total pages* path | a page is empty or repeats, or the total is reached |
| Offset and limit | offset and limit parameters, page size, optional *total rows* path | a page is empty or short, or the total is reached |
| Cursor | where the next cursor is in the response (for example `meta.next_cursor`) and the parameter to send it in | the cursor is empty or repeats |
| Next-page URL in the body | where the link is (for example `links.next`) | there is no link, or it repeats |
| Link header | nothing | there is no `rel="next"` link |

**Speed.** Most of a refresh is waiting for the API, once per page. To make it faster:

- set the **page size** to the largest the API allows: 10 pages of 1,000 rows are much quicker than 100 pages of 100;
- use page-number or offset paging where the API offers it: DataBridge then fetches several pages at once (**Parallel requests** on the connection). Cursor and next-link paging must go one page at a time, because each page says where the next one is;
- raise **Requests per minute** if the API allows it (0 means no limit);
- for large, growing data, use an incremental request (below), so each refresh only reads what changed.

After each refresh, the Sources page (hover the status) and [Runs](app:runs) show the timing, for example *120 page(s), 200,000 rows in 4.1 s (API about 0.12 s per page, 4 at a time; processing 1.2 s)*. A high per-page API time points at the API or the network; a high processing time at very wide or deeply nested records.

**Incremental loads:** values can use `{{today}}`, `{{yesterday}}`, `{{now}}`, `{{days_ago:7}}` and `{{last_refresh}}`. The last one is the start time of the previous successful refresh; it is empty the first time, and after you edit the request. For example: `updated_since={{last_refresh}}`.

Then set **Each refresh** to *Adds to the data (incremental)*, so the new rows are added to what DataBridge already has instead of replacing it. With **Match rows on** (for example `id`), a changed record replaces its earlier version instead of appearing twice. When the API returns no rows, the data stays as it is.

**Limits:** if a refresh stops at *Max rows* or *Max pages*, the run is marked as a warning and the snapshot notes say it is incomplete. The largest single response DataBridge accepts is 200 MB after decompression.

Click **Preview**. You see the columns and the first rows; the preview reads at most two pages.

## 3. Add it as a source

Click **Add as source**. Untick any columns you don't want to store, and choose how often to **refresh**: off, every 15 minutes, hourly, every 6 hours or daily. DataBridge takes the first snapshot straight away. While it works, the dialog says what it is doing (calling the API, reading page 3, retrying a busy API). **Cancel** stops it at any point, and nothing is added.

A source on its own isn't served anywhere yet. On [Sources](app:sources), a source that no mapping uses says **Not mapped yet**. Click its **Map** button (the arrows icon) and choose **New target from the source's columns**: DataBridge creates a target with one field per column, maps them, and opens the [Mapping Studio](guide:mapping-studio). There you can remove or rename fields, add formulas and checks, and **Publish**. Then create an [endpoint](guide:endpoints) for the mapping, so other systems can fetch the data.

## Refreshing

A REST source refreshes in three ways:

- the **Refresh** button on [Sources](app:sources);
- its **schedule** (clock icon on Sources);
- an external scheduler calling `POST /api/v1/sources/{id}/refresh` (see [Using the REST API](guide:rest-api)).

Each refresh stores a new snapshot **only when the data changed**, so polling often doesn't fill up history. After that, every published mapping that uses the source republishes, and a column change is flagged as drift. The Sources page shows when the source last refreshed, whether that brought new data, no changes or an error, and when it runs next. Every refresh also appears under [Runs](app:runs).

To change the path, parameters, paging or columns later, use **Edit request** (the notes icon) on Sources. The source is refreshed straight away with the new request.

## When it takes too long

- **"Could not connect to …"**: the DataBridge *server* can't reach the API (your browser can, which is why the URL opens fine there). DataBridge gives up after about 10 seconds instead of waiting. Check the server's internet access, firewall, proxy and DNS. With Docker, a common cause is a missing IPv6 route: test from the server with `docker compose exec app python -c "import httpx; print(httpx.get('https://your-api/…').status_code)"`.
- **"No answer from … trying again"**: the API accepted the call but is slow to answer. Each try waits up to the connection's **Timeout**; a refresh tries 4 times, a preview twice.
- **"The API is busy (HTTP 429) …"**: the API is rate limiting. Lower **Requests per minute** on the connection.
- A preview that is quick but a source that is slow means many pages: see *Speed* above.

