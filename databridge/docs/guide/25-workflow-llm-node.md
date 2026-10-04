---
title: The LLM node
icon: AUTO_AWESOME
permission: use_ai
pages: []
keywords: llm node, model, prompt, system prompt, user prompt, variables, data, row, rows_table, input, prompt library, per row, batch, columns sent, temperature, max tokens, parallel, retries, json fields, enum, output, guardrails, pii mode, injection, rules, invented values, banned, grounding
---
# The LLM node

The LLM node sends rows to a model and adds its answer to them as new columns. Its settings are grouped as **Model**, **Prompt**, **Calls**, **Output** and **Guardrails**. This page explains each with an example. For the other nodes see [Workflow nodes](guide:workflow-nodes).

The running example classifies support tickets like this one:

| ticket_id | product | channel | note |
|---|---|---|---|
| T-1001 | Router X2 | email | Charged twice for my order, please refund ASAP. Reach me at ann@acme.com |

## Model

**Model** lists the models you may use. Under it you see where the model runs:

- **internal network:** the model runs in your company. Data is sent as is, unless you choose masking.
- **external network:** a cloud provider. Personal data is masked automatically, and columns marked *confidential* can't be sent at all; the run is blocked before any call.

**Example:** choose your company's internal model for tickets that contain customer details, and an external model only for data without personal information, such as product totals. Models are set up under AI > Models (see [Company models](guide:company-models)).

## Prompt

Choose **Write here** or **From the library**.

### Write here

| Field | What goes in it |
|---|---|
| **System prompt** | The model's role and rules. It is the same for every call. **Never put data here.** |
| **User prompt** | The request, with the data. It is filled in for each call. |

**Example:**

System prompt:

```
You triage customer support tickets for an electronics shop.
Use only the ticket text. If you are unsure, choose "other".
```

User prompt:

```
Classify this ticket for the support team.

{{ data }}
```

### Variables

Variables in the user prompt are replaced for each call:

| Variable | Becomes | Example |
|---|---|---|
| `{{ data }}` | The row as `column: value` lines (per-row mode), or the batch as a table (batch mode), inside `<data>` … `</data>` tags | see below |
| `{{ row.note }}` | One value of the row (per-row mode only). For column names with spaces: `{{ row["Order No"] }}`. | `Charged twice for my order…` |
| `{{ rows_table }}` | The batch as a table, without the `<data>` tags | `ticket_id | product …` |
| `{{ rows }}` | The batch as a list, for loops | `{% for r in rows %}- {{ r.note }}{% endfor %}` |
| `{{ input.question }}` | A value from the *Run input* node or the API call | `Can I return it?` |

For the example ticket, `{{ data }}` becomes:

```
<data>
ticket_id: T-1001
product: Router X2
channel: email
note: Charged twice for my order, please refund ASAP. Reach me at [EMAIL_1]
</data>
```

Note how the email address was masked before sending (see *Personal data (PII)* below).

**Rules:**

- **Prefer `{{ data }}`.** It always wraps the data in delimiters, and DataBridge tells the model that whatever is inside is data, never instructions. This is your main protection against text in your data that tries to instruct the model.
- **No data variable in the user prompt?** `{{ data }}` is added at the end automatically.
- **Data variables in the system prompt** are refused when you check or run.
- **Misspelled names fail:** a variable that doesn't exist (e.g. `{{ row.notes }}` when the column is `note`) stops the run with a clear message, rather than sending an empty value.

### From the library

Pick a **Published prompt** from AI > Prompts. Optionally set **Pin version** to use a specific version; blank means the latest published one.

Library prompts are reviewed and versioned, and every run records the prompt name, version and a fingerprint of its text. Use them when several workflows share a prompt, or when prompts need an approval step. The library's *system* and *user* texts use the same variables.

## Calls

| Setting | Default | What it does |
|---|---|---|
| **Mode** | One call per row | *One call per row*: each row gets its own answer. *One call per batch*: up to *Rows per call* rows are sent together, and you get one answer per batch. |
| **Rows per call** | 20 | Batch size (batch mode only). |
| **Columns sent** | all | The columns the model sees, comma separated. The other columns still travel on to the next node, unsent. |
| **Temperature** | model default | 0 to 0.2 for classification and extraction (consistent answers); higher for creative text. |
| **Max reply tokens** | 512 | The longest answer allowed. Keep it small for JSON fields (100 to 200); larger for summaries. |
| **Parallel** | 4 | Calls made at the same time (up to 16). Lower it if the model's server rate-limits you. |
| **Retries** | 1 | When a reply fails a check (wrong JSON, a rule, an invented value), DataBridge asks again up to this many times (0 to 5), telling the model what was wrong. |

**Example, per row:** 200 tickets with *Columns sent* `ticket_id, product, note` means 200 calls. `customer` and `email` are never sent, but are still there afterwards for the output.

**Example, batch:** a SQL node produced 12 rows of totals per region, and you want one summary. Choose *One call per batch* with *Rows per call* `50`: one call, with all 12 rows as a table.

**Batch results replace the rows.** In batch mode the node's output has one row per batch, not per input row:

| batch | rows | summary |
|---|---|---|
| 1 | 12 | • EMEA leads with 41% of revenue… |

So use batch mode to *summarize* a set of rows, and per-row mode to *add answers to* each row.

## Output

### Text

The whole reply goes into one column. **Answer column** names it (default `ai_answer`).

*Example:* Answer column `reply_draft`, prompt *Draft a short, polite reply to this customer.* Each ticket gets a `reply_draft` column.

### JSON fields

The model must answer with a JSON object containing the fields you list. Each field becomes a column.

| Setting | What to enter |
|---|---|
| **Field** | Column name, e.g. `category`. Use a name your data doesn't already have: an output field replaces an input column of the same name. |
| **Type** | `string`, `integer`, `number`, `boolean` or `enum` (one of a list). |
| **Options (comma)** | For `enum`: the allowed values, e.g. `billing, shipping, product, other`. |
| **Required** | When ticked, a missing or empty value fails the check. |
| **Hint for the model** | A short description sent with the field, e.g. `1 (low) to 5 (critical)`. |

**Example fields for ticket triage:**

| Field | Type | Options | Required | Hint |
|---|---|---|---|---|
| category | enum | billing, shipping, product, other | yes | |
| urgency | integer | | yes | 1 (low) to 5 (critical) |
| summary | string | | no | one sentence |
| ticket_ref | string | | yes | copy ticket_id from the data |

DataBridge adds the field list to the system prompt and checks every reply:

- **The reply must be JSON.**
- **Types are checked:** `"5"` is accepted for an integer, but `"high"` is not.
- **Enum values must be in the list:** case doesn't matter (`Billing` is stored as `billing`), but `refunds` fails.
- **A failing reply is retried** with the reasons, then handled as set in *When the reply fails a check*.

For T-1001 the node adds: `category` = billing, `urgency` = 5, `summary` = *Customer was charged twice and wants a refund.*, `ticket_ref` = T-1001.

## Guardrails

Each LLM node has its own guardrails. The defaults are safe; adjust them for the data at hand. For how they work overall, see [AI guardrails](guide:ai-guardrails).

### Personal data (PII)

| Choice | What happens | Use it when |
|---|---|---|
| **Auto: mask for external models** (default) | Emails, phone numbers, SSNs, card numbers, IBANs and the like are replaced by tokens such as `[EMAIL_1]` before an *external* model sees them. Internal models get the data as is. | Almost always. |
| **Mask, restore in the answer** | Masked for every model. Tokens in the answer are put back, so a reply mentioning `[EMAIL_1]` shows `ann@acme.com` in the output. | Drafting replies that must address the customer. |
| **Always mask** | Masked for every model; the answer keeps the tokens. | The answer shouldn't contain personal data at all. |
| **Don't send rows with PII** | Rows containing personal data are rejected before any call. | Strict data handling. |
| **Send as is (internal only)** | No masking. Only allowed with internal models. | Internal models where the model needs the real values. |

Columns marked PII on the source are masked as a whole; other text is scanned for personal data.

### Prompt injection in data

Your data may contain text such as *"Ignore all previous instructions and …"*. DataBridge looks for it in every row before sending:

| Choice | Effect |
|---|---|
| **Flag the row for review** (default) | It is sent anyway (inside the data tags) and flagged. |
| **Reject the row (don't send it)** | The row is never sent; it's in the review table. |
| **Stop the run** | The whole run stops. |
| **Don't check** | No check. |

*Example:* for public web forms, choose **Reject**. For internal data, **Flag** is usually enough.

Values from the *Run input* node (`{{ input.… }}`) are checked the same way. Since they apply to every call, a problem there with *Reject* or *Stop* blocks the whole run.

### When the reply fails a check

What happens to a row whose answer fails a check (JSON fields, rules, invented values, banned words, length, unexpected personal data, or a model error) after the retries:

| Choice | Effect |
|---|---|
| **Keep the row, flag for review** (default) | The row goes on, with the problem as its review note. Output datasets keep it with an `ai_review` column, unless they exclude flagged rows. |
| **Move the row to Rejected** | The row leaves the flow and is listed in the review table. |
| **Stop the run** | The first failure stops the run. |

### Rules on the reply (formulas)

Formulas that must be true for each answer. They can use the new fields and the row's columns.

| Must be true | Message |
|---|---|
| `AND([urgency] >= 1, [urgency] <= 5)` | urgency must be 1 to 5 |
| `IF([category] = "billing", [urgency] >= 2, TRUE)` | billing issues are never lowest priority |
| `LEN([summary]) <= 200` | summary too long |

### No invented values

Checks that an answer field only contains values that exist in your data (per-row mode).

| Must match | Means | Example |
|---|---|---|
| **this row's column** | The field must equal that column of the same row. | *Reply field* `ticket_ref`, *Input column* `ticket_id`: the model must echo the right ticket ID. |
| **a value of column** | The field must be one of the values found in that column of the incoming rows. | *Reply field* `product_mentioned`, *Input column* `product`: no made-up product names. |
| **a fixed list** | The field must be in *Allowed (list)*. | `region` in `EMEA, APAC, AMER`. |

The comparison ignores case and surrounding spaces.

### Other checks

| Setting | Example | Effect |
|---|---|---|
| **Banned words or patterns** | `guarantee, legal advice, \bfree\b` | A reply containing one fails the check. Patterns may be regular expressions; case is ignored. |
| **Max reply chars (0 = off)** | `600` | Longer replies fail the check. |
| **Max prompt tokens** | `4000` (default) | Rows whose prompt would be bigger are rejected **before** sending. This catches huge text fields. |
| **Flag replies containing personal data that wasn't in the input** | on (default) | Catches a model inventing or leaking an email address or phone number. |

## Good habits

- **Test first:** run **Test (3 rows)** and read *Prompts and replies*: the prompt as sent, and the reply.
- **Send less:** set *Columns sent*, shorten long text with a Transform (`LEFT([note], 800)`), and aggregate with SQL before summarizing.
- **Ask for structure:** use JSON fields with an `enum` when you need categories, so every answer is one of your values.
- **Prove it:** add a rule or a *No invented values* check for anything you rely on downstream.
- **Keep it steady:** use low temperatures for classification, and pin library prompt versions for production workflows.
