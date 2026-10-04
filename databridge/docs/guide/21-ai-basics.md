---
title: "AI: playground and prompts"
icon: AUTO_AWESOME
permission: use_ai
pages: [ai]
keywords: ai, llm, playground, prompt, template, variable, model, usage, tokens
---
# AI: playground and prompts

The **AI** page has five tabs: Playground, Prompts, Workflows, Models and Usage.

## Playground

1. Pick a **model**. You see only the models you may use; your admin sets the default.
2. Write a **system prompt** (how the model should behave) and a **user prompt** (the request).
3. **Variables:** use `{{ name }}` placeholders; the playground shows an input box for each.
4. Set temperature and max tokens if needed. Tick **JSON output** if you want structured output.
5. Click **Run**. The answer shows the tokens used and the time taken. **Prompt sent** shows exactly what went to the model.

**Save as prompt** stores it in the library.

## Prompts library

- **Versions:** prompts are versioned. Edit the draft, then **Publish** it with a note.
- **Workflows:** use the latest published version, or a pinned one.
- **History:** you can see and restore old versions.

## Usage

**Usage** shows your calls and tokens this month. Admins see everyone's. Your admin may set a monthly token limit.

## Which model should I use?

- **Internal models** run inside the company network, so data doesn't leave it.
- **External models** are cloud providers; personal data is masked before sending.
- **Your own key:** some company models need the key issued to you. See [Company models and your key](guide:company-models).
