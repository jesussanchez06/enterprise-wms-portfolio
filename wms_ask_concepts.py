"""Ask WMS concept dictionary, normalization, and entity extraction (zero paid APIs)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


CONCEPTS: dict[str, set[str]] = {
    "shipping": {
        "ship", "shipped", "shipping", "shipment", "shipments", "went out", "dispatched",
        "completed shipment", "go out", "wentout",
    },
    "orders": {
        "order", "orders", "work", "workload", "jobs", "job", "requests", "request",
    },
    "inventory": {
        "inventory", "stock", "units available", "available quantity", "on hand", "onhand",
        "stock level", "material", "sku", "skus", "part", "parts", "part number",
    },
    "quality": {
        "quality", "qc", "audit", "audits", "inspection", "verification", "discrepancy",
        "defect", "damage", "accuracy", "qa",
    },
    "sla": {
        "sla", "deadline", "due", "late", "overdue", "at risk", "atrisk", "breached",
        "service level", "miss", "missing sla",
    },
    "priority": {
        "priority", "priorities", "focus", "attention", "action", "actions",
        "recommendation", "recommendations", "what should we do", "what should i do",
        "what needs attention", "next step", "next steps", "recomendation", "recomendations",
    },
    "productivity": {
        "productivity", "throughput", "output", "performance", "completed per hour",
        "units per hour",
    },
    "picking": {
        "pick", "picking", "picker", "pickers", "picked", "fulfillment", "workboard",
    },
    "capacity": {
        "capacity", "backlog", "queue", "remaining work", "work remaining",
    },
    "supervisor": {
        "supervisor", "control tower", "escalation", "escalations", "intervention",
        "bottleneck",
    },
    "planner": {
        "planner", "procurement", "release", "planned", "destination", "urgency",
    },
    "executive": {
        "executive", "dashboard", "kpi", "kpis", "summary", "overview", "warehouse",
        "management", "ceo",
    },
    "critical": {"critical", "critcal", "critial"},
    "urgent": {"urgent"},
    "standard": {"standard"},
    "blocked": {"blocked", "block", "stuck", "shortage", "shortages"},
    "help": {
        "help", "what can you", "what can ask wms", "explain", "how does", "what does",
        "meaning", "difference between",
    },
    "write_request": {
        "ship order", "add stock", "add units", "add unit", "update inventory",
        "delete order", "create order", "pass audit", "fail audit", "mark completed",
        "reset demo", "adjust inventory", "change status",
    },
}

SPELLING_FIXES = {
    "shiped": "shipped",
    "shippd": "shipped",
    "shiiped": "shipped",
    "inventroy": "inventory",
    "inventor": "inventory",
    "quailty": "quality",
    "quality": "quality",
    "critcal": "critical",
    "recomendation": "recommendation",
    "recomendations": "recommendations",
    "avaliable": "available",
    "availible": "available",
    "quanity": "quantity",
    "superviser": "supervisor",
    "verificaton": "verification",
    "pendng": "pending",
    "blockd": "blocked",
    "shortge": "shortage",
    "yesturday": "yesterday",
    "tommorow": "tomorrow",
    "recieved": "received",
    "completd": "completed",
    "dashbord": "dashboard",
    "exective": "executive",
    "priorites": "priorities",
    "actons": "actions",
}


def normalize_question(question: str) -> str:
    text = (question or "").strip().lower()
    text = text.replace("on-hand", "on hand").replace("onhand", "on hand")
    text = text.replace("part number", " part ")
    text = text.replace("part no", " part ")
    text = text.replace("item number", " part ")
    text = re.sub(r"[?!.,;:]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens = []
    for token in text.split():
        tokens.append(SPELLING_FIXES.get(token, token))
    # light plural folding for common ops nouns
    folded = []
    for token in tokens:
        if token.endswith("s") and token[:-1] in {
            "order", "shipment", "action", "priority", "issue", "audit", "picker",
            "warehouse", "sku", "part", "recommendation", "job", "request",
        }:
            folded.append(token[:-1])
        elif token == "skus":
            folded.append("sku")
        else:
            folded.append(token)
    return " ".join(folded)


def concept_hits(normalized: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for concept, terms in CONCEPTS.items():
        score = 0
        for term in terms:
            if " " in term:
                if term in normalized:
                    score += 2
            else:
                if re.search(rf"\b{re.escape(term)}\b", normalized):
                    score += 1
        if score:
            hits[concept] = score
    return hits


def extract_top_n(normalized: str, default: int | None = None) -> int | None:
    match = re.search(r"\b(?:top|first|biggest|largest)\s+(\d{1,2})\b", normalized)
    if match:
        return max(1, min(int(match.group(1)), 20))
    word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}
    match = re.search(r"\b(?:top|first)\s+(one|two|three|four|five|ten)\b", normalized)
    if match:
        return word_map[match.group(1)]
    if re.search(r"\b(three|3)\s+(action|priority|recommendation|issue)s?\b", normalized):
        return 3
    if re.search(r"\b(five|5)\s+(action|priority|recommendation|issue|sku)s?\b", normalized):
        return 5
    return default


def extract_entities(normalized: str, original: str = "", warehouses: list[str] | None = None) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    sku_match = re.search(r"\b(?:sku|part|item|pn)?\s*#?\s*([0-9]{4,8})\b", original or normalized, flags=re.I)
    if not sku_match:
        sku_match = re.search(r"\b([0-9]{4,8})\b", normalized)
    if sku_match:
        entities["sku"] = sku_match.group(1)

    order_match = re.search(
        r"\b(?:order|ord)\s*#?\s*(ORD-\d+|\d{3,})\b",
        original or normalized,
        flags=re.I,
    )
    if order_match:
        entities["order_id"] = order_match.group(1).upper() if str(order_match.group(1)).upper().startswith("ORD-") else order_match.group(1)

    if re.search(r"\bcritical\b", normalized):
        entities["urgency"] = "Critical"
    elif re.search(r"\burgent\b", normalized):
        entities["urgency"] = "Urgent"
    elif re.search(r"\bstandard\b", normalized):
        entities["urgency"] = "Standard"

    top_n = extract_top_n(normalized)
    if top_n is not None:
        entities["limit"] = top_n

    # dates
    now = datetime.now().astimezone() if hasattr(datetime.now(), "astimezone") else datetime.now()
    if "today" in normalized:
        entities["date_label"] = "today"
        entities["date"] = now.date()
    elif "yesterday" in normalized:
        entities["date_label"] = "yesterday"
        entities["date"] = (now - timedelta(days=1)).date()
    else:
        iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", normalized)
        if iso:
            y, m, d = map(int, iso.groups())
            try:
                entities["date"] = datetime(y, m, d).date()
                entities["date_label"] = f"{y:04d}-{m:02d}-{d:02d}"
            except ValueError:
                pass

    if warehouses:
        lowered = normalized
        for warehouse in warehouses:
            wh = warehouse.lower()
            city = wh.split(" warehouse")[0]
            if wh in lowered or city in lowered:
                entities["warehouse"] = warehouse
                break
            if "san diego" in lowered and "san diego" in wh:
                entities["warehouse"] = warehouse
                break
            if ("los angeles" in lowered or " la " in f" {lowered} ") and "los angeles" in wh:
                entities["warehouse"] = warehouse
                break

    return entities
