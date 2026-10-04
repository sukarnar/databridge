---
title: Workflow nodes
icon: HUB
permission: use_ai
pages: []
keywords: workflow node, dataset rows, run input, transform, sql, aggregate, duckdb, router, condition, branch, output dataset, filter, limit, columns, merge
---
# Workflow nodes

Every node except the LLM, with its settings and an example. The LLM node has its own page: [The LLM node](guide:workflow-llm-node). For building and running workflows, see [AI workflows](guide:ai-workflows).

The examples use a source called **Support tickets**:

| ticket_id | customer | email | product | channel | note |
|---|---|---|---|---|---|
| T-1001 | Ann Lee | ann@acme.com | Router X2 | email | Charged twice for my order, please refund ASAP |
| T-1002 | Bob Diaz | bob@beta.io | Cam Mini | chat | Parcel says delivered but nothing arrived |
| T-1003 | Cy Wu | cy@corp.net | Router X2 | phone | |

All formulas use the [formula language](guide:formulas) of the Mapping Studio: columns in `[brackets]`, text in `"quotes"`.

## Dataset rows

**Starts the flow with rows from DataBridge.** It has no input and one exit.

| Setting | What to enter |
|---|---|
| **Rows from** | A **Source** (its latest snapshot) or a **Mapping dataset** (the published output of a mapping). Only published mappings are listed. |
| **Columns** | The columns to keep, comma separated. Blank keeps all. |
| **Filter (formula, optional)** | Only rows where the formula is true. Rows where it's blank are dropped too. |
| **Max rows (0 = all)** | Keep only the first N rows, after the filter. |

Once you pick the data, the node lists its columns and how they are classified. **Classify columns (PII / confidential)** marks columns such as `email` as personal data, so LLM nodes protect them (see [AI guardrails](guide:ai-guardrails)).

**Example:** Rows from *Source · Support tickets*, Columns `ticket_id, product, channel, note, email`, Filter `NOT(ISBLANK([note]))`. T-1003 is dropped, because it has no note.

**Tips:**

- Choose a **mapping dataset** when the data needs cleaning first: the mapping's checks and formulas have already run.
- Keep only the columns you need. Fewer columns mean smaller prompts and less data sent.
- The workflow's *Max input rows per run* (default 1000, in **Settings**) still applies. A bigger input stops the run with a message, so filter or set *Max rows*.

## Run input

**Starts the flow with one row that someone types in, or that an API caller sends.**

| Setting | What to enter |
|---|---|
| **Field** | A column name, e.g. `question`. |
| **Default** | The value used when none is given. |

When you click **Run**, a form asks for each field. API callers send the values as `"input"`:

```
{"input": {"question": "Can I return an opened camera?", "product": "Cam Mini"}}
```

That gives one row with the columns `question` and `product`. Values the API sends for names you didn't list are added as extra columns. Lists and objects arrive as JSON text.

**Example:** fields `question` (no default) and `product` (default `any`). An LLM node can then use `{{ input.question }}`, or simply `{{ data }}` for the whole row.

## Transform

**Adds columns with formulas and/or keeps only some rows.** Nothing is sent anywhere.

| Setting | What to enter |
|---|---|
| **New columns** | **Add column**, then a name and a formula. Columns are added in order, so a later formula can use an earlier new column. A new column with an existing name replaces it. |
| **Keep rows where (formula, optional)** | Only rows where the formula is true go on. It runs after the new columns are added. |

**Examples:**

| Column | Formula | Result for T-1001 |
|---|---|---|
| `short_note` | `LEFT([note], 500)` | the first 500 characters (keeps prompts small) |
| `vip` | `IN([customer], "Ann Lee", "Raj Patel")` | true |
| `contact` | `CONCAT([customer], " via ", [channel])` | Ann Lee via email |

Keep rows where `LEN([note]) >= 10` drops notes too short to classify.

After an LLM node, a Transform can work on the answer, e.g. a column `priority` with `IF([urgency] >= 4, "P1", "P3")`, or keep rows where `[category] <> "other"`.

**Tip:** a new column made from a PII column is treated as PII too. The protection follows the data.

## SQL (aggregate)

**Runs one read-only SQL query over the incoming rows**, which are available as the table **`rows`**. It uses DuckDB's SQL dialect. Use it to **summarize before calling a model**: fewer tokens, and raw rows never leave.

| Setting | What to enter |
|---|---|
| **Query** | One `SELECT` (or `WITH … SELECT`, or a `UNION` of selects) over `rows`. |

**Example:** tickets per product and channel:

```
SELECT product, channel, COUNT(*) AS tickets,
       SUM(CASE WHEN note ILIKE '%refund%' THEN 1 ELSE 0 END) AS refund_requests
FROM rows
GROUP BY product, channel
ORDER BY tickets DESC
```

| product | channel | tickets | refund_requests |
|---|---|---|---|
| Router X2 | email | 41 | 9 |
| Cam Mini | chat | 27 | 3 |

**Rules:**

- **Read-only:** no INSERT, UPDATE, CREATE, COPY, ATTACH or SET.
- **Only the incoming rows:** no other tables, and no file functions such as `read_csv`.
- **Bounded:** a query that runs longer than the AI request timeout is stopped. A result with more rows than *Max input rows per run* stops the run.
- **Classification follows:** a column computed from a PII column counts as PII. When DataBridge can't tell which columns a result came from (subqueries, `UNION`), it assumes the strictest.
- **Review notes:** `SELECT *` keeps the rows' review notes; aggregated rows start without notes.

## Router

**Splits rows in two by a condition.** It has two exits:

- **yes** (green): rows where the condition is true;
- **no** (orange): all others, including rows where the condition is blank.

| Setting | What to enter |
|---|---|
| **Condition (formula)** | e.g. `[urgency] >= 4` |

**Examples:**

| Goal | Condition |
|---|---|
| Urgent tickets | `[urgency] >= 4` |
| Billing questions over email | `AND([category] = "billing", [channel] = "email")` |
| Answers the model wasn't sure about | `OR(ISBLANK([category]), [category] = "other")` |

Connect each exit to a different node: click the green or orange dot, then the target. You don't have to use both exits. Rows leaving by an unconnected exit are simply not used.

**Tip:** routing *before* an LLM node saves tokens. For example, send only `[channel] = "email"` tickets to the model and the rest straight to an output.

## Output dataset

**Saves the rows it receives as a source**, which you can then map, check and serve like any other.

| Setting | What to enter |
|---|---|
| **Dataset name** | The source's name, e.g. `Urgent tickets`. The first run creates it; later runs add snapshots to it. |
| **Include rows flagged by guardrails** | On (default): flagged rows are kept, with an extra **ai_review** column holding the reason. Off: only clean rows are saved. Rejected rows are never in an output. |
| **Columns** | The columns to save, comma separated. Blank saves all. |

**Example:** Dataset name `Urgent tickets`, Columns `ticket_id, product, category, urgency, summary`. The output has those five columns, plus `ai_review` if any row was flagged.

**Tips:**

- The name can't be the name of an existing source that isn't a workflow output.
- Two outputs with the same name would overwrite each other. Give each its own name, or merge the branches into one output.
- PII and confidential markings are carried over to the output source.

## Merging branches

Connect two nodes into the same node and their rows are **stacked**, matched by column name. A column only one branch has is blank for the other branch's rows.

**Example:** after a Router, the *yes* rows go through an LLM node that writes `summary`, and the *no* rows go straight on. Connect both into one Output dataset. Every ticket is saved, and only the urgent ones have a `summary`.
