---
title: Targets
icon: SCHEMA
permission: view
pages: [targets]
keywords: target, schema, template workbook, fields, types, required
---
# Targets

A **target** describes the result you want: its field names, types, whether each is required, and a description.

## Create a target

- **From template workbook**: upload an Excel file that has just the header row (and optionally a few example rows). The fields and types are taken from it.
- **New target**: add fields by hand, or **copy fields from a source** as a starting point.

| Field setting | Meaning |
|---|---|
| Name | The column name consumers will see, e.g. `customer_id` |
| Type | string, integer, decimal, float, boolean, date, timestamp, json, binary |
| Required | Rows without a value are rejected at publish time |
| Description | Shown in the studio and the endpoint schema |

Type conversions happen at publish. A value that can't be converted (e.g. `abc` into a number) becomes a **reject** with a clear reason, rather than silently becoming blank.
