---
title: Sources and uploads
icon: TABLE_CHART
permission: view
pages: [sources]
keywords: upload, spreadsheet, excel, csv, sheet profile, header, version, drift, snapshot, classify, pii
---
# Sources and uploads

A **source** is anything that produces rows: an uploaded spreadsheet, a file on a connection, a database table, or a SQL query. Every upload or refresh is kept as a **snapshot**, so you can always see what came in and when.

## Upload a spreadsheet

1. Go to [Sources](app:sources) and click **Upload spreadsheet**. Excel (.xlsx, .xls), CSV and similar files are supported.
2. The **import wizard** shows the raw rows. Blue marks the header and grey marks skipped rows. Adjust these settings if needed:
   - **Sheet**: which worksheet to read.
   - **Header row** and **Header spans**: where the column names are, and how many rows they take (for two-row headers).
   - **Skip rows starting with**: ignore totals, notes or blank-ish rows (e.g. `Total, Note`).
3. Check the **columns and detected types**. Change a type if the guess is wrong, e.g. keep an ID as text so leading zeros survive.
4. Give the source a name and save.

DataBridge remembers these settings as the source's **sheet profile**. Every later version of the file is read the same way.

## Map the source

Each source card says which mappings use it, or **Not mapped yet**. Click the **Map** button (the arrows icon) to start a mapping with this source already chosen. Pick an existing target, or **New target from the source's columns** to get a target with one field per column, which you can then trim or rename. The [Mapping Studio](guide:mapping-studio) opens with the fields matched.

## New versions and schema changes

- **Upload new version** reads the new file with the saved profile.
- Mappings that use this source are **republished automatically**.
- If columns were added, removed or renamed, the source shows **schema changed**. The version history explains what changed, so you can fix the mapping.
- An identical file (same content) is recognised and not stored twice.

Sources from a connection have **Refresh** instead of Upload.

## Classify columns for AI

Designers and admins can click the **shield** icon on a source to mark columns as *public*, *internal*, *personal data (PII)* or *confidential*. **Suggest from data** finds emails, phone numbers, SSNs, card numbers, IBANs and IP addresses. AI features then protect those columns: see [AI guardrails](guide:ai-guardrails).

## Automating uploads

Schedulers such as Stonebranch UAC or n8n can post new files directly with an admin API key:

```
curl -X POST -H "X-API-Key: <key>" -F "file=@sales_2026-09.xlsx" \
     {{api_base}}/api/v1/ingest/<source id>
```

The source card shows its id. See [Using the REST API](guide:rest-api).
