---
title: Endpoints
icon: API
permission: view
pages: [endpoints]
keywords: endpoint, rest, api, filter, parameter, json, csv, xlsx, page, version, public
---
# Endpoints

An **endpoint** serves a mapping's published data at a URL, as JSON, CSV or Excel.

## Create an endpoint (designers)

1. Go to [Endpoints](app:endpoints) and click **New endpoint**.
2. Choose the mapping it **serves**, a name and a URL slug. The URL becomes `{{api_base}}/api/v1/data/<slug>`.
3. **Filters:** let callers narrow the data. Each filter is a query parameter tied to a target column, with an operator:

   | Operator | Example call |
   |---|---|
   | equals / not equal | `?region=EMEA` |
   | `>` `>=` `<` `<=` | `?min_amount=1000` |
   | contains | `?name=smith` |
   | in list | `?status=Open,Pending` |

4. **Formats and page size:** choose the formats and the page size.
5. **Access:** **Public** endpoints need no key. Leave it off for anything sensitive.

## Test it

**Test** on an endpoint card lets you fill in filters and see the response as a consumer would.

## Versions

Every publish creates a new dataset version. Callers get the latest version unless they ask for an older one, e.g. `?version=3`.

See [Using the REST API](guide:rest-api) for call examples, and [API keys](guide:api-keys) for access.
