"""LLM-assisted features for the rest of DataBridge (phase 1: auto-map suggestions)."""

import json
from dataclasses import dataclass
from typing import Any

from databridge.core.models import User
from databridge.core.types import compatibility
from databridge.services import llm

AUTOMAP_SYSTEM = """You map columns of a source dataset to fields of a target schema for a data integration tool.
Match by meaning, not just spelling (e.g. "Cust No" -> "customer_id", "Amt" -> "amount_usd").
Only propose a match when you are reasonably confident. Each source and each target may be used at most once.
Respond with JSON only, in this exact shape:
{"matches": [{"source": "<source column>", "target": "<target field>", "confidence": 0.0-1.0, "reason": "<short>"}]}"""


@dataclass
class AISuggestion:
    source: str
    target: str
    confidence: float
    reason: str


def suggest_mappings(user: User, model_ref: str, source_fields: list[dict[str, Any]],
                     target_fields: list[dict[str, Any]], already_mapped: set[str] | None = None,
                     min_confidence: float = 0.5) -> list[AISuggestion]:
    """Asks the model for source->target matches. Sample values are only sent to internal models."""
    kind, _, _ = llm.parse_ref(model_ref)
    option = next((o for o in llm.available_models(user) if o.ref == model_ref), None)
    internal = bool(option and option.network == "internal")
    already_mapped = already_mapped or set()
    targets = [t for t in target_fields if t["name"] not in already_mapped]
    if not targets:
        return []
    payload = {
        "source_columns": [{"name": f["name"], "type": f.get("type", "string"),
                            **({"samples": [str(x)[:40] for x in f.get("samples", [])[:3]]} if internal else {})}
                           for f in source_fields],
        "target_fields": [{"name": t["name"], "type": t.get("type", "string"),
                           **({"description": t["description"]} if t.get("description") else {})} for t in targets],
    }
    result = llm.chat(user, model_ref, "Propose matches for:\n" + json.dumps(payload, indent=1),
                      system=AUTOMAP_SYSTEM, purpose="automap", temperature=0, max_tokens=1500, json_mode=True)
    try:
        data = result.json()
    except ValueError as e:
        raise llm.AIError("The model did not return valid JSON; try another model") from e
    matches = data.get("matches", []) if isinstance(data, dict) else data
    src_types = {f["name"]: f.get("type", "string") for f in source_fields}
    tgt_types = {t["name"]: t.get("type", "string") for t in targets}
    used_s, used_t, out = set(), set(), []
    for m in sorted(matches or [], key=lambda x: -float(x.get("confidence", 0) or 0)):
        s, t = str(m.get("source", "")), str(m.get("target", ""))
        conf = float(m.get("confidence", 0) or 0)
        if s not in src_types or t not in tgt_types or s in used_s or t in used_t or conf < min_confidence:
            continue  # ignore invented names and duplicates
        if compatibility(src_types[s], tgt_types[t]) == "incompatible":
            continue
        used_s.add(s)
        used_t.add(t)
        out.append(AISuggestion(s, t, round(conf, 2), str(m.get("reason", ""))[:200]))
    return out
