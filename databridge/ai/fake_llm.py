"""A tiny fake LLM server for tests and offline demos (OpenAI-compatible + Anthropic Messages).

    python -m databridge.ai.fake_llm           # serves http://127.0.0.1:8799/v1
Accepts the key "good-key", keys starting with "user-", or no key (except under /secure/); others get HTTP 401.
Answers the auto-map prompt with name-based matches, and echoes everything else.
"""

import json
import re
from typing import Any

import httpx

MODELS = ["fake-small", "fake-large"]
GOOD_KEY = "good-key"  # also accepted: any key starting with "user-" (individually issued keys)
KEYS_SEEN: list = []  # keys received, for tests


def _norm(s: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    syn = {"cust": "customer", "no": "id", "num": "id", "amt": "amount", "dt": "date", "first": "name",
           "last": "name", "usd": ""}
    return " ".join(syn.get(t, t) for t in tokens if syn.get(t, t))


def _schema_answer(system: str, user: str) -> str:
    """Workflow JSON output. Deterministic, with triggers in the data for guardrail tests:
    __BADJSON__ (not JSON), __LEAK__ (adds an email address), __OUTOFRANGE__ (urgency 9), __INVENT__ (made-up id).
    """
    if "__BADJSON__" in user:
        return "Sorry, I can't answer in JSON."
    low = user.lower()
    data = dict(re.findall(r"^([^:\n<>]{1,60}): (.*)$", user, re.M))
    out: dict[str, Any] = {}
    for name, spec in re.findall(r'^\s*"([^"]+)": (.+?)(?: - .*)?,?$', system, re.M):
        if spec.startswith("one of"):
            options = json.loads(spec[len("one of "):].split(" - ")[0].rstrip(","))
            out[name] = next((o for o in options if str(o).lower() in low), options[-1] if options else "")
        elif spec.startswith("integer"):
            out[name] = 9 if "__OUTOFRANGE__" in user else (5 if ("urgent" in low or "asap" in low) else 2)
        elif spec.startswith("number"):
            out[name] = 1.5
        elif spec.startswith("boolean"):
            out[name] = "urgent" in low
        else:
            out[name] = "INVENTED-999" if "__INVENT__" in user else data.get(name, f"fake {name}")
    if "__LEAK__" in user:
        out["reason"] = "Contact leaked.person@example.org for details"
    return json.dumps(out)


def _answer(system: str, user: str) -> str:
    if "map columns" in system:
        payload = json.loads(user[user.index("{"):])
        matches = []
        for t in payload["target_fields"]:
            tn = _norm(t["name"])
            for s in payload["source_columns"]:
                sn = _norm(s["name"])
                if sn and (sn == tn or sn in tn or tn in sn):
                    matches.append({"source": s["name"], "target": t["name"], "confidence": 0.9,
                                    "reason": "same meaning"})
                    break
        return json.dumps({"matches": matches})
    if "with these fields:" in system:
        return _schema_answer(system, user)
    if "JSON" in system or "json" in system:
        return json.dumps({"echo": user[:200]})
    return f"Echo: {user[:500]}"


def handle(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
    h = {k.lower(): v for k, v in headers.items()}
    anthropic = path.endswith("/messages") or "anthropic-version" in h
    key = h.get("x-api-key") if anthropic else (h.get("authorization", "").removeprefix("Bearer ").strip()
                                                 or h.get("apikey") or None)
    KEYS_SEEN.append(key)
    if key is not None and key != GOOD_KEY and not key.startswith("user-"):
        return 401, {"error": {"message": "invalid api key"}}
    if key is None and "/secure/" in path:  # a gateway that requires every caller's own key
        return 401, {"error": {"message": "missing api key"}}
    if method == "GET" and path.endswith("/models"):
        return 200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]}
    data = json.loads(body or b"{}")
    if data.get("model") not in MODELS:
        return 404, {"error": {"message": f"model {data.get('model')} not found"}}
    if path.endswith("/chat/completions"):
        msgs = data.get("messages", [])
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        user = msgs[-1]["content"] if msgs else ""
        text = _answer(system, user)
        return 200, {"id": "fake-1", "object": "chat.completion", "model": data["model"],
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": len((system + user).split()), "completion_tokens": len(text.split())}}
    if path.endswith("/messages"):
        system = data.get("system", "")
        user = data["messages"][-1]["content"]
        text = _answer(system, user)
        return 200, {"id": "msg_fake", "type": "message", "role": "assistant", "model": data["model"],
                     "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                     "usage": {"input_tokens": len((system + user).split()), "output_tokens": len(text.split())}}
    return 404, {"error": {"message": "not found"}}


def mock_transport() -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        status, payload = handle(request.method, request.url.path, dict(request.headers), request.content)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(_handler)


def asgi_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def any_route(path: str, request: Request):
        status, payload = handle(request.method, "/" + path, dict(request.headers), await request.body())
        return JSONResponse(payload, status_code=status)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(asgi_app(), host="127.0.0.1", port=8799)
