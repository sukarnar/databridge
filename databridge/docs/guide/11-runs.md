---
title: Runs and troubleshooting
icon: HISTORY
permission: view
pages: [runs]
keywords: run, history, failed, warning, reject, reject report, rejected rows, error, troubleshoot, problem, not refreshing
---
# Runs and troubleshooting

[Runs](app:runs) lists every ingest, refresh and publish, with its status, rows in and out, and rejected rows.

| Status | Meaning |
|---|---|
| ok | Everything went through |
| warning | Published, but some rows were rejected: click **View rejects** to see each row and its reason |
| failed | Nothing was published; the message says why |

## Common problems

| Symptom | What to do |
|---|---|
| Source shows **schema changed** | The new file has different columns. Open the mapping, relink the renamed columns and publish. |
| Many rejects saying *cannot convert to number* | The column holds text like `N/A`. Clean it with a formula, e.g. `IF([Amount] = "N/A", "", [Amount])`, or change the target type. |
| *required value missing* | Add a default with `COALESCE([Col], "unknown")`, or make the field optional. |
| Leading zeros lost | Set the column type to text in the source's import wizard. |
| Endpoint returns 401 / 403 | The key is missing, revoked, or not allowed for that endpoint. |
| Connection test fails | Check host, port and firewall from the DataBridge server. For databases, check the driver hint in the connection dialog. |

If something on screen looks stale, reload the browser page. Your work in dialogs is saved only when you press Save.
