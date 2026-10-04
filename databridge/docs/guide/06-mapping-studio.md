---
title: Mapping Studio
icon: COMPARE_ARROWS
permission: view
pages: [mappings, studio]
keywords: map, arrows, drag, formula, auto-map, ai suggest, row steps, filter, dedupe, sort, unpivot, validation, preview, publish, reject
---
# Mapping Studio

The Mapping Studio is where source columns become target fields.

## Create a mapping

Go to [Mappings](app:mappings), click **New mapping**, pick a source and a target, then **Create and open**. Similar names are matched automatically to get you started.

## Map with arrows

- **Link columns:** drag a source column (left) onto a target field (right) to draw an arrow.
- **Edit a field's rule:** click a target field to open its **inspector**. There you can see and edit the rule, a **formula** such as `TRIM([Cust No])` or `[Qty] * [Unit Price]`.
- **Unlink:** **Remove mapping** clears the rule.
- **Auto-map:** matches the remaining fields by name, e.g. `Cust No` to `customer_id`.
- **AI suggest** (designers with AI access): asks a model to match columns by meaning. Suggestions appear as dashed arrows; **Accept all** or **Dismiss**. Column names and types are sent to the model; sample values are sent only to internal models.

Field status colours show what still needs attention: unmapped required fields, formula errors, and fields with rejects in the preview.

## Row steps

The **filter** button in the toolbar opens row steps. They run before the column rules:

| Step | Use it to |
|---|---|
| Filter | Keep rows where a condition is true, e.g. `[Status] <> "Cancelled"` |
| Dedupe | Drop duplicate rows (all columns, or the ones you list) |
| Sort | Order rows by a column |
| Unpivot | Turn month columns (Jan, Feb, ...) into rows |

## Data checks

In the inspector, add checks to a field: **Allowed values**, **Must match pattern (regex)**, **Minimum**, **Maximum** and **Unique**. Rows that fail go to the reject report with the reason; good rows are still published.

## Preview and publish

- **Live preview:** the preview at the bottom updates as you edit. Red cells show the problem when you hover over them.
- **Save draft:** keeps your work without affecting consumers.
- **Publish:** creates a new dataset version. Endpoints serve it immediately. The run shows rows out and rejected. Open rejected rows with **View rejects** in [Runs](guide:runs).

The formula language is described in [Formula reference](guide:formulas).
