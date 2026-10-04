---
title: Using the REST API
icon: TERMINAL
permission: view
pages: []
keywords: curl, api, python, powershell, x-api-key, ingest, refresh, schedule, uac, n8n, openapi, websocket, wss, stream, streaming, push, subscribe, events
---
# Using the REST API

Everything a consumer or scheduler needs is under `{{api_base}}/api/v1`. Send the key in the `X-API-Key` header. The interactive API reference is at `{{api_base}}/api/docs`.

## Read data

```
curl -H "X-API-Key: <key>" "{{api_base}}/api/v1/data/customer-orders?region=EMEA&page=1"
curl -H "X-API-Key: <key>" "{{api_base}}/api/v1/data/customer-orders?format=csv" -o orders.csv
```

JSON responses include `data`, `page`, `page_size`, `pages`, `total` and the dataset `version`. Request `page=2`, `page=3`, ... up to `pages`.

PowerShell:

```
Invoke-RestMethod -Headers @{ "X-API-Key" = "<key>" } -Uri "{{api_base}}/api/v1/data/customer-orders"
```

## Automate loads (admin keys)

These calls need an API key with access to **all endpoints** (`*`), issued under [API keys](app:keys).

| Call | Does |
|---|---|
| `GET /api/v1/sources` (optional `?name=...`) | Lists sources with their `id` and the call that loads each one |
| `POST /api/v1/ingest/{source_id}` (file upload) | New version of an uploaded source, then map and publish |
| `POST /api/v1/sources/{source_id}/refresh` | Pull fresh data from a connection-backed source |
| `GET /api/v1/runs/{run_id}` | Details of a run |
| `GET /api/v1/runs/{run_id}/rejects` | The reject report (Excel) |

### Finding the source_id

The `source_id` is the number DataBridge gives a source when it is created. It never changes, even when you upload new versions. Two ways to find it:

- In the studio, open [Sources](app:sources). Each source shows **Source id N** under its name, together with the exact call to use.
- From a script, ask by name:

```
curl -H "X-API-Key: <admin key>" "{{api_base}}/api/v1/sources?name=Vendor%20sales"
```

```
[{"id": 1, "name": "Vendor sales", "kind": "upload", "latest_snapshot_id": 9,
  "load_with": "/api/v1/ingest/1"}]
```

The ingest call is only for **uploaded** sources (spreadsheets). A source that reads from a connection is loaded with `refresh`.

### Example: upload a new file

The first version is uploaded in the studio, which saves the sheet profile (header row, skipped rows, types). After that, send each new file as a multipart upload in a field named `file`:

```
curl -X POST "{{api_base}}/api/v1/ingest/1" \
     -H "X-API-Key: <admin key>" \
     -F "file=@vendor_sales_2026-09.xlsx"
```

The call waits until the file is stored and every published mapping that uses the source has republished, then answers:

```
{
  "snapshot_id": 9,
  "rows": 7,
  "skipped": false,
  "drift": {"added": [], "removed": [], "renamed": [], "retyped": [], "changed": false},
  "notes": ["Skipped 2 blank or total rows"],
  "published": [
    {"mapping": "Vendor sales to customer orders", "status": "published",
     "version": 2, "rows": 6, "rejected": 1, "run_id": 6}
  ]
}
```

| Field | Meaning |
|---|---|
| `skipped` | `true` if this exact file was already loaded; nothing was republished |
| `drift.changed` | `true` if columns were added, removed, renamed or changed type |
| `published[].status` | `published`, or `paused` when a mapped column disappeared (fix the mapping, then republish) |
| `published[].rejected` | Rows set aside; download them with `GET /api/v1/runs/{run_id}/rejects` |

Errors: `401`/`403` for a missing key or a key without `*` access, `404` for an unknown source, `400` if the file can't be read with the saved profile, and `413` if it is larger than the upload limit.

PowerShell (7 or later):

```
Invoke-RestMethod -Method Post -Uri "{{api_base}}/api/v1/ingest/1" `
  -Headers @{ "X-API-Key" = "<admin key>" } `
  -Form @{ file = Get-Item "C:\drops\vendor_sales_2026-09.xlsx" }
```

Python:

```
import requests

r = requests.post("{{api_base}}/api/v1/ingest/1",
                  headers={"X-API-Key": "<admin key>"},
                  files={"file": open("vendor_sales_2026-09.xlsx", "rb")}, timeout=300)
r.raise_for_status()
result = r.json()
bad = [p for p in result["published"] if p["status"] != "published" or p.get("rejected")]
if bad or result["drift"] and result["drift"]["changed"]:
    raise SystemExit(f"Check DataBridge: {bad or result['drift']}")
```

A scheduler job (Stonebranch UAC, n8n, cron) typically posts the file, then fails or alerts when `drift.changed` is true, a mapping is `paused`, or `rejected` is above zero.

## Streaming API (secure websocket)

Instead of polling, a client can keep one secure websocket open at `wss://<server>/api/v1/stream`. It can do two things:

- **Get told when data changes.** Subscribe to `endpoint:<slug>` to be notified the moment a new version of that endpoint's data is published. Keys with access to all endpoints can also subscribe to `runs` for ingest and publish runs.
- **Stream data in chunks.** Send a `query` and receive the rows in chunks, with the same filters as the REST endpoint.

Authenticate with the same API key as for REST. Scripts send it in the `X-API-Key` header. Browsers can't set headers, so they send `{"type": "auth", "api_key": "..."}` as the first message. Keys are never put in the URL.

Python (`pip install websockets`):

```
import json
from websockets.sync.client import connect

with connect("wss://{{api_host}}/api/v1/stream", additional_headers={"X-API-Key": "<key>"}) as ws:
    print(json.loads(ws.recv()))                        # {"type": "welcome", ...}
    ws.send(json.dumps({"type": "subscribe", "topics": ["endpoint:customer-orders"]}))
    ws.send(json.dumps({"type": "query", "id": "all", "endpoint": "customer-orders",
                        "params": {"region": "EMEA"}, "chunk_size": 5000}))
    for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "rows":
            print("got", len(msg["data"]), "rows")        # save or process each chunk
        elif msg["type"] == "end":
            print("done:", msg["total"], "rows, version", msg["version"])
        elif msg["type"] == "event":                      # a new version was published
            print("new version", msg["data"]["version"], "- query again to refresh")
```

JavaScript in a browser page (the page's address must be allowed by the admin):

```
const ws = new WebSocket("wss://{{api_host}}/api/v1/stream");
ws.onopen = () => ws.send(JSON.stringify({type: "auth", api_key: KEY}));
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "welcome") ws.send(JSON.stringify({type: "subscribe", topics: ["endpoint:customer-orders"]}));
  if (msg.type === "event") refresh();
};
```

| You send | You get |
|---|---|
| `{"type": "subscribe", "topics": [...]}` | `subscribed` with the granted topics and a reason for each denied one |
| `{"type": "query", "id": "q1", "endpoint": "...", "params": {...}, "chunk_size": 1000}` | `rows` messages (`seq` 0, 1, ...), then `end` with `total` and `version` |
| `{"type": "cancel", "id": "q1"}` | `cancelled` |
| `{"type": "ping"}` | `pong` |

The server sends a `heartbeat` every 30 seconds. It checks the key and each subscription every minute: when a key is revoked, the connection closes with code **4401**. Other close codes are **4408** (no auth message within 10 seconds), **4429** (too many connections for this key, 10 by default), **1009** (message larger than 64 KB), **1008** (more than 50 messages in 10 seconds) and **1013** (the client read events too slowly). Reconnect with a short, growing delay.

## AI workflows

`POST /api/v1/workflows/{slug}/run` runs a published AI workflow. See [AI workflows](guide:ai-workflows).
