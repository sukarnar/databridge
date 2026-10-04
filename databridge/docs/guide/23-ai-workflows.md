---
title: AI workflows
icon: ACCOUNT_TREE
permission: use_ai
pages: [workflow]
keywords: workflow, node, canvas, connect, template, save, check, estimate, test, run, publish, settings, limits, budget, api, run history, review, draft, version
---
# AI workflows

A workflow sends rows of your data through a chain of steps (nodes), some of them calling a language model, and saves the result as a new source. For example:

read support tickets, keep the ones with text, classify each with an LLM, then send the urgent ones to one dataset and the rest to another.

![The ticket triage workflow used as the example in these pages](guide/workflow-triage.png)

This page covers the editor and the run lifecycle. The other workflow pages go deeper:

- [Workflow nodes](guide:workflow-nodes): every node except the LLM, with an example each.
- [The LLM node](guide:workflow-llm-node): prompts, variables, per-row and batch calls, JSON output, and the guardrail settings.
- [Workflow examples](guide:workflow-examples): four complete workflows, built step by step.
- [AI guardrails](guide:ai-guardrails): what is protected and how.

You need the *Use AI* permission, and at least one model you're allowed to use (see [AI basics](guide:ai-basics)).

## How data moves

Every node receives a **table of rows** and passes a table on:

- Inputs (*Dataset rows*, *Run input*) create the rows.
- Logic nodes (*Transform*, *SQL*, *Router*) reshape, filter or split them, without calling a model.
- The *LLM* node calls a model and **adds columns** with the answer.
- *Output dataset* saves the rows as a source.

A row can pick up a **review note** on the way, for example when a guardrail flags it. Flagged rows carry on through the flow and appear in the run's review table. **Rejected** rows leave the flow and are listed in the review table with the node and the reason. Nothing disappears silently.

## Create a workflow

Go to [AI](app:ai) > **Workflows** and click **New workflow**. Give it a name, for example *Triage support tickets*, and an optional description. Then choose what to start from:

| Template | You get | Good for |
|---|---|---|
| Classify each row | Dataset rows > LLM (JSON fields *category*, *urgency*, *reason*) > Router (`[urgency] >= 4`) > two outputs | Tagging, triage, extraction |
| Summarize a dataset | Dataset rows > SQL > LLM (one call per batch) > output | Reports and summaries of a whole table |
| Answer a question from the run input | Run input (*question*) > LLM > output | Small assistants called from other systems |
| Blank | An empty canvas | Anything else |

Templates are only a starting point: every node can be changed, removed or connected differently. LLM nodes start on your default model. Then pick the data in the input node and name the outputs.

## The editor

The editor has a header with the actions, the node list on the left, the canvas in the middle and the settings of the selected node on the right.

### Add nodes

Click a node type in **Add a node**. It appears on the canvas and is selected.

**Tip:** if a node is selected when you add the next one, the new node is connected to it automatically (Router nodes excepted, since they have two exits). To build a straight chain, add the nodes in order.

### Connect nodes

1. Click the **dot on the right edge** of the node that sends the rows. A yellow bar says *Connecting from …*.
2. Click the node that should receive them.

To give up, click **Cancel** in the yellow bar, or the dot again.

- A **Router** has two dots: green **yes** for rows where its condition is true, orange **no** for the others.
- **Input nodes** take no input, and **Output dataset** nodes have no exit.
- Connections must flow one way. A connection that would make a loop is refused.
- **Two connections into one node** merge the rows of both branches. For example, send the router's *yes* and *no* rows through different nodes, then into one output.

### Edit, move and remove

- **Click** a node to edit it on the right.
- **Label** renames the node on the canvas; it is also how runs and errors refer to it. *Classify ticket* is clearer than *LLM*.
- **Drag** a node to move it.
- **Connections** (at the bottom of the settings) lists what the node is connected to. The unlink icon removes a connection.
- The bin icon at the top of the settings deletes the node with its connections.

## Save, check, test, run, publish

The header buttons, in the order you'd normally use them:

| Button | What it does | Calls the model? |
|---|---|---|
| **Save** | Saves the draft. Leaving with unsaved changes asks first. | No |
| **Check & estimate** | Checks the workflow and does a dry run of everything except the model calls. | No |
| **Test (3 rows)** | Runs the draft on the first 3 rows and shows the exact prompts and replies. | Yes, a few calls |
| **Run** | Runs the draft on all rows and writes the outputs. | Yes |
| **Publish** | Freezes the current draft as a new version, for the API. | No |

### Check & estimate

**Check & estimate** first saves the draft, then checks it. Problems are listed so you can fix them, for example:

- *Classify: choose a model*
- *Urgent?: enter a condition*
- *Urgent tickets: name the output dataset*
- *Classify: data variables (data) belong in the user prompt, never in the system prompt*

When it's valid, it reads the data, runs the logic nodes and renders every prompt, without calling a model. It then shows:

- **Model calls and tokens:** the number of calls, about how many tokens to expect, and the maximum. There is one row per LLM node, with its calls, prompt tokens, maximum reply tokens and the model's network (*internal* or *external*).
- **What is sent:** per LLM node, the columns sent as they are and the columns masked.
- **Guardrails found (before any call):** for example *injection detected: 1* or *pii masked: 12*, and how many rows would be rejected before reaching the model.
- **Warnings:** for example when the prompts alone exceed the run's token limit, or the monthly budget would be passed.

*Example:* 240 tickets with one call per row shows *240 model calls · ~96,000 tokens expected*. If that's more than you meant to spend, add a filter or a limit to the input before running.

### Test (3 rows)

This runs the draft on 3 rows (the first 3 of each input) and opens the run. **Prompts and replies (test runs)** shows each call:

- the system prompt;
- the user prompt **as sent**, with personal data masked and the data inside `<data>` tags;
- the model's reply, and whether it passed the checks.

Use it to tune prompts before spending tokens on everything.

### Run

**Run** runs the draft on all rows:

- If the workflow has a *Run input* node, a form asks for its values first.
- If the run is expected to use more tokens than *Ask to confirm above* (workflow **Settings**), a **Confirm run** window shows the estimate. The run starts only when you click *Run (~N tokens)*.
- When it finishes, the run opens (see *Run results* below).

Each *Output dataset* gets a new snapshot. Runs from the editor always use the **draft**, so you can try changes without publishing.

### Publish

**Publish** checks the workflow and freezes it as version 1, 2, 3 and so on. The API always runs the **published** version, so you can keep editing the draft without affecting callers. Under the name, the header shows *published v3* (or *draft* if never published), *unsaved changes*, and *draft differs from published* when you've changed it since. Publish again to release the changes.

## Run results

Each run window (also under *Recent runs* > **Details** on the Workflows tab) shows:

- **Status:**
  - *ok*;
  - *warning*: some rows were flagged or rejected;
  - *blocked*: a guardrail stopped the run, often before any call;
  - *failed*: an error, shown in red.
- **The run itself:** what started it (*manual*, *test*, *api*), the version (or *draft*), rows in and out, flagged and rejected rows, calls and tokens.
- **Guardrails:** what triggered, with counts, e.g. *pii masked 31*, *retried 2*, *rule failed 1*.
- **A table per node:** status, rows in and out, flagged/rejected, calls/tokens, time, and notes or the error.
- **Review: flagged and rejected rows:** each with *_status* (flagged or rejected), *_node*, *_reason* and its data.
- **Output:** the first rows written.

Example of a review row:

| _status | _node | _reason | ticket_id | note |
|---|---|---|---|---|
| rejected | Classify ticket | possible prompt injection (ignore previous instructions) | T-1043 | Ignore all previous instructions and … |

## Use the results

Each *Output dataset* node writes a source (kind *AI workflow output*) under [Sources](app:sources). Every run adds a snapshot. Map it to a target in the [Mapping Studio](guide:mapping-studio) and serve it through an [endpoint](guide:endpoints), like any other source. Its PII and confidential markings come from the data it was made from.

## Settings and limits

**Settings** (header) holds the name, description and the limits for this workflow:

| Limit | Default | What happens |
|---|---|---|
| Max input rows per run | 1000 | An input with more rows stops the run with a message. Add a filter or a *Max rows* to the input, or raise the limit. |
| Max tokens per run | 200000 | If an LLM node's prompts alone would go over, the run is blocked before calling. If the limit is reached during the calls, the remaining rows get no answer and are flagged (or rejected, per the node's *When the reply fails a check*). |
| Ask to confirm above (tokens) | 20000 | Bigger runs show the estimate and wait for you to confirm. API callers must send `"confirm": true`. |
| Monthly token budget (0 = none) | 0 | Once the workflow has used this many tokens this month, runs are blocked until next month. |

Your administrator may also set a monthly token limit per user.

## Run it from other systems (API)

1. **Publish** the workflow.
2. Under [API keys](app:keys), create a key and tick **Run workflow: <name>**. A key for all endpoints also works.
3. Call it:

```
curl -X POST {{api_base}}/api/v1/workflows/triage-support-tickets/run \
     -H "X-API-Key: <key>" -H "Content-Type: application/json" \
     -d '{"input": {"question": "Where is my parcel?"}, "limit": 0, "confirm": false}'
```

The address uses the workflow's **slug**: its name in lower case with dashes. The toast after **Publish** shows it.

| Body field | Meaning |
|---|---|
| `input` | Values for the *Run input* node, by field name. Leave it out if the workflow has none. |
| `limit` | Process only the first N rows of each input (0 = all), e.g. for a smoke test. |
| `confirm` | `true` to run even when the estimate is over *Ask to confirm above*. |

The run uses the published version, as the workflow's **owner**, with the owner's model permissions. The reply:

```
{"run_id": 57, "status": "ok", "error": null, "rows_in": 1, "rows_out": 1,
 "rows_flagged": 0, "rows_rejected": 0, "tokens": 412, "calls": 1,
 "guardrail_events": {}, "rows": [{"question": "Where is my parcel?", "answer": "..."}], "review": []}
```

`rows` holds up to 500 output rows. For more, read the output source through an endpoint.

| HTTP status | Meaning |
|---|---|
| 200 | Finished (*ok* or *warning*). |
| 409 | The estimate is over *Ask to confirm above*; the reply holds `tokens_expected`. Send again with `"confirm": true`. |
| 422 | Blocked by a guardrail, or the input is not valid. |
| 500 | The run failed; the reply holds the error. |

`GET /api/v1/ai-runs/{run_id}` returns the same result again later, with the same key.
