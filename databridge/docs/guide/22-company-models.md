---
title: Company models and your key
icon: DNS
permission: use_ai
pages: []
keywords: company model, my key, api key, issued key, gateway, local llm, add my key, personal key
---
# Company models and your key

Go to **AI > Models**. The page has two sections.

## Company models

These are the LLMs your company provides, for example the company AI gateway. Each card shows:

- **Health:** whether the model is up, how fast it answers, and its recent history.
- **Key:** which key it uses.
  - **company key:** nothing to do; it just works.
  - **your own key:** the company issued you a personal key for it. Click **Add my key**, paste it and save. It is tested, stored encrypted, used only for your calls and your workflows, and not even admins can read it.
- **Models available to you:** only the models an admin approved for your role.

If several company models share a gateway, one key covers all of them. **Replace key** and **Remove** are on the card.

## My API keys

These are your own keys for public cloud providers (OpenAI, Anthropic, Gemini, ...), if your company allows them. They can only call public `https` addresses.

## If a model fails

| Message | Meaning |
|---|---|
| *add yours under AI > Models* | That model needs your issued key |
| *the API key was rejected* | Your key expired or was revoked: ask for a new one and use **Replace key** |
| *not approved for your role* | Ask an admin to approve the model for your role |
| *Monthly token limit reached* | Your admin set a limit; ask for more |
