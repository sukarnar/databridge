---
title: AI guardrails
icon: SHIELD
permission: use_ai
pages: []
keywords: guardrail, pii, personal data, mask, confidential, injection, validation, rules, invented, leak, limit, budget, safety
---
# AI guardrails

Every LLM node checks what is sent to the model and what comes back. You set the guardrails in the node's **Guardrails** section.

## What is sent

| Guardrail | How it works |
|---|---|
| **Data classification** | Columns marked *PII* or *confidential* on the source (shield icon) are protected. The marking follows the data through renames, formulas and SQL. |
| **Network rules** | Internal models receive everything. External models get personal data masked. Confidential columns **block the run** before any call. |
| **PII masking** | Emails, phones, SSNs, card numbers and similar are replaced with tokens like `[EMAIL_1]`. *Mask, restore in the answer* puts the originals back in the output. |
| **Prompt injection** | Text like "ignore previous instructions" in your data is detected; the row is flagged, rejected (never sent) or the run stops. Data is always wrapped so the model treats it as data. |
| **Limits** | Max rows per run and max prompt size. Large runs ask for confirmation; runs are blocked over the token or monthly budget (workflow **Settings**). |

## What comes back

| Guardrail | How it works |
|---|---|
| **Schema check** | JSON fields must have the right types and allowed values; invalid replies are retried |
| **Rules** | Formulas that must be true, e.g. `AND([urgency] >= 1, [urgency] <= 5)` |
| **No invented values** | A reply field must match an input column or a fixed list |
| **Content** | Replies with personal data that wasn't in the input are flagged; banned words; max length |

**When a check fails**, each guardrail can do one of three things:

- **Flag:** the row stays, with an `ai_review` note.
- **Reject:** the row goes to the review table.
- **Stop:** the run stops.

Rows that fail are listed in the run's **review** table with the reason, so one bad row never silently disappears.
