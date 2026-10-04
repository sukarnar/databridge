---
title: Setting up company LLMs
icon: HUB
permission: manage_models
pages: []
keywords: config.yaml, continue, upload config, endpoint, gateway, ollama, vllm, certificate, ca, mtls, catalog, approve, default model, health, alert, webhook, discovery key
---
# Setting up company LLMs (admins)

Company models live under **AI > Models > Company models**.

## Fastest: upload the company's config.yaml

If the company distributes a Continue `config.yaml` (models with `apiBase`, `requestOptions`, `headers`):

1. Click **Upload config.yaml** and choose the file. Copy/paste damage (mixed quotes, uneven indentation, pasted links) is repaired and listed.
2. Check the **mapping**: one endpoint per server, with its models, auth header and TLS.
3. Upload the **CA certificate** named in `caBundlePath` once. Every endpoint that uses it then trusts it.
4. Choose the key handling:
   - **Each user adds the key issued to them** (recommended when the file has a placeholder like `putapikeyhere`).
   - **The key in the file as the company key.** It is stored encrypted and removed from the kept file.
   - **No key.**
5. Click **Apply**.

Uploading a newer file, or clicking **New version**, updates the same endpoints and disables removed models. **View file** shows the kept file and the generated configuration.

## Or add an endpoint by hand

Click **Add endpoint** and fill in:

- **Type preset:** Ollama, vLLM, LM Studio, a gateway, ...
- **Base URL:** e.g. `https://host/v1`.
- **API style.**
- **Key mode:**
  - *one company key*;
  - *each user's own key*, with an optional **discovery key** used only to list models and for health checks;
  - *no key*.
- **Network:** internal or external.
- **Roles** that may use it.
- **Certificates:** your provider's CA, or a client certificate for mutual TLS (.pem, or .p12/.pfx).

Then **Save and test**.

## Approve models and set defaults

- **Model catalog:** the **checklist** icon on a card. Enable models, give them friendly names and descriptions, and restrict some to roles. With *Only models an admin enabled*, newly discovered models stay off until you enable them.
- **Default models:** the default for the Playground, AI suggest and new workflows.

## Keep it healthy

Endpoints are checked every few minutes: reachability, speed, and certificate expiry.

- **On the page:** failing endpoints and certificates close to expiry show a red banner. Alerts go to the audit log, and to Slack or Teams if a webhook is set.
- **Check now:** **Check health now** runs the checks immediately.
- **Configuration as code:** **Export config** / **Paste config** move the setup between servers as YAML, without secrets.

If the server uses a web proxy, internal LLM hosts must be listed in `NO_PROXY`. See [Server settings](guide:admin-settings).
