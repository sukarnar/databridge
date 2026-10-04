"""Auto-map: suggest source -> target field pairs by name similarity, type and values."""

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from databridge.core.types import compatibility

SYNONYMS = {
    "cust": "customer", "cus": "customer", "client": "customer",
    "qty": "quantity", "amt": "amount", "val": "value", "price": "amount",
    "no": "id", "num": "id", "nbr": "id", "number": "id", "code": "id", "key": "id",
    "dt": "date", "desc": "description", "addr": "address", "st": "state",
    "fname": "first name", "lname": "last name", "tel": "phone", "mobile": "phone",
    "usd": "", "total": "amount", "sku": "product id", "cat": "category",
}


def normalize(name: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)  # camelCase -> camel Case
    tokens = re.split(r"[^A-Za-z0-9]+", s.lower())
    out = [SYNONYMS.get(t, t) for t in tokens if t]
    return " ".join(t for t in out if t).strip()


@dataclass
class Suggestion:
    source: str
    target: str
    score: float
    reason: str


def suggest(source_fields: list[dict[str, Any]], target_fields: list[dict[str, Any]],
            threshold: float = 70.0) -> list[Suggestion]:
    """Greedy one-to-one assignment by descending score. Scores are 0-100."""
    candidates: list[Suggestion] = []
    for t in target_fields:
        tn = normalize(t["name"])
        for s in source_fields:
            sn = normalize(s["name"])
            name_score = max(fuzz.token_sort_ratio(sn, tn), fuzz.token_set_ratio(sn, tn) * 0.95)
            if sn == tn:
                name_score = 100
            compat = compatibility(s.get("type", "string"), t.get("type", "string"))
            type_adj = {"ok": 0, "lossy": -8, "incompatible": -40}[compat]
            score = max(0.0, min(100.0, name_score + type_adj))
            if score >= threshold:
                reason = f"name {name_score:.0f}%" + ("" if compat == "ok" else f", type {compat}")
                candidates.append(Suggestion(s["name"], t["name"], round(score, 1), reason))
    candidates.sort(key=lambda c: c.score, reverse=True)
    used_s, used_t, picked = set(), set(), []
    for c in candidates:
        if c.source in used_s or c.target in used_t:
            continue
        used_s.add(c.source)
        used_t.add(c.target)
        picked.append(c)
    return picked
