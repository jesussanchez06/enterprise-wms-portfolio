"""Ask WMS concept dictionary, normalization, and entity extraction (zero paid APIs)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


CONCEPTS: dict[str, set[str]] = {
    "shipping": {
        "ship", "shipped", "shipping", "shipment", "shipments", "went out", "dispatched",
        "completed shipment", "go out", "wentout", "outbound", "ready to ship", "left the dock",
        "dock", "carrier", "fulfillment complete",
    },
    "orders": {
        "order", "orders", "work", "workload", "jobs", "job", "requests", "request",
        "backlog", "pipeline", "demand", "ticket", "tickets", "open work",
    },
    "inventory": {
        "inventory", "stock", "units available", "available quantity", "on hand", "onhand",
        "stock level", "material", "sku", "skus", "part", "parts", "part number",
        "worth", "value", "valuable", "dollar", "dollars", "money", "on-hand",
        "availability", "qty", "quantity", "bin", "location",
    },
    "quality": {
        "quality", "qc", "audit", "audits", "inspection", "verification", "discrepancy",
        "defect", "damage", "accuracy", "qa", "pass rate", "failed audit", "escalation",
        "escalations", "failed", "fail", "failed inspection", "mistake", "carton",
        "crush", "auditor", "audited", "inspector", "quality issue", "quality history",
        "re-audit", "reaudit",
    },
    "sla": {
        "sla", "deadline", "due", "late", "overdue", "at risk", "atrisk", "breached",
        "service level", "miss", "missing sla", "compliance", "on time", "ontime",
        "healthy", "timer", "past due",
    },
    "otif": {
        "otif", "on time in full", "on-time in-full", "ontime in full", "fill rate",
        "perfect order", "complete and on time",
    },
    "priority": {
        "priority", "priorities", "focus", "attention", "action", "actions",
        "recommendation", "recommendations", "what should we do", "what should i do",
        "what needs attention", "next step", "next steps", "recomendation", "recomendations",
        "what first", "triage",
    },
    "productivity": {
        "productivity", "throughput", "output", "performance", "completed per hour",
        "units per hour", "picks per hour", "efficiency", "rate of work",
    },
    "picking": {
        "pick", "picking", "picker", "pickers", "picked", "fulfillment", "workboard",
        "work queue", "ready to pick", "pick queue", "who picked", "who is picking",
        "active picker", "most orders",
    },
    "capacity": {
        "capacity", "backlog", "queue", "remaining work", "work remaining",
        "left to do", "still open", "outstanding",
    },
    "supervisor": {
        "supervisor", "control tower", "escalation", "escalations", "intervention",
        "bottleneck", "bottlenecks", "ops lead", "floor lead",
    },
    "planner": {
        "planner", "order planning", "procurement", "release", "planned", "destination", "urgency",
        "planning", "demand plan",
    },
    "executive": {
        "executive", "dashboard", "kpi", "kpis", "summary", "overview", "warehouse",
        "management", "ceo", "vp", "leadership", "scorecard", "pulse", "snapshot",
        "how are we", "how is the", "risk", "risks", "warehouse health", "completion rate",
        "needs attention", "attention", "health",
    },
    "ai_analysis": {
        "focus today", "summarize performance", "biggest risk", "top 3 actions",
        "vs yesterday", "versus yesterday", "30 minute", "thirty minute", "review first",
        "below target", "ai analysis", "what should i focus", "first look",
    },
    "warehouse_network": {
        "warehouse", "site", "sites", "facility", "facilities", "warehouse network",
        "distribution center", "dc network", "compare warehouse", "compare sites",
        "which site", "by warehouse", "by site",
    },
    "critical": {"critical", "critcal", "critial", "sev1", "p0"},
    "urgent": {"urgent", "sev2", "p1"},
    "standard": {"standard", "normal", "routine"},
    "blocked": {"blocked", "block", "stuck", "shortage", "shortages", "can't move", "cannot move"},
    "help": {
        "help", "what can you", "what can ask wms", "explain", "how does", "what does",
        "meaning", "difference between", "what is digitech", "ask wms", "digitech wms",
        "how do i use", "capabilities", "readme",
    },
    "kpi_def": {
        "what is sla", "define sla", "what does otif", "kpi definition", "kpi mean",
        "what is otif", "what is inventory accuracy", "define kpi", "meaning of",
    },
    "predictive": {
        "forecast", "forecasting", "predict", "prediction", "will we miss", "next week",
        "next month", "hire", "fire", "terminate", "lay off", "layoff", "headcount cut",
        "who should we fire", "future demand",
    },
    "write_request": {
        "ship order", "add stock", "add units", "add unit", "update inventory",
        "delete order", "create order", "pass audit", "fail audit", "mark completed",
        "reset demo", "adjust inventory", "change status", "move stock", "reassign picker",
        "close issue", "approve order",
    },
    "compare": {
        "compare", "versus", "vs", "difference between warehouses", "which warehouse is",
        "best warehouse", "worst warehouse", "side by side",
    },
    "adjustments": {
        "adjustment", "adjustments", "cycle count", "recount", "inventory correction",
        "qty change", "adjusted",
    },
}

SPELLING_FIXES = {
    "shiped": "shipped",
    "shippd": "shipped",
    "shiiped": "shipped",
    "shiping": "shipping",
    "inventroy": "inventory",
    "inventor": "inventory",
    "inventry": "inventory",
    "invnetory": "inventory",
    "quailty": "quality",
    "qualety": "quality",
    "qality": "quality",
    "critcal": "critical",
    "critial": "critical",
    "recomendation": "recommendation",
    "recomendations": "recommendations",
    "reccomendation": "recommendation",
    "avaliable": "available",
    "availible": "available",
    "quanity": "quantity",
    "qunatity": "quantity",
    "superviser": "supervisor",
    "verificaton": "verification",
    "verifcation": "verification",
    "pendng": "pending",
    "blockd": "blocked",
    "blokced": "blocked",
    "shortge": "shortage",
    "shortaeg": "shortage",
    "yesturday": "yesterday",
    "tommorow": "tomorrow",
    "recieved": "received",
    "completd": "completed",
    "complet": "completed",
    "dashbord": "dashboard",
    "exective": "executive",
    "executve": "executive",
    "priorites": "priorities",
    "priorties": "priorities",
    "actons": "actions",
    "overude": "overdue",
    "overdeu": "overdue",
    "breched": "breached",
    "breachd": "breached",
    "warehosue": "warehouse",
    "warehuse": "warehouse",
    "pikcer": "picker",
    "pickr": "picker",
    "backlg": "backlog",
    "bottlneck": "bottleneck",
    "botleneck": "bottleneck",
    "performace": "performance",
    "performence": "performance",
    "accuracey": "accuracy",
    "accurcy": "accuracy",
    "otfi": "otif",
    "oift": "otif",
    "sumary": "summary",
    "summry": "summary",
    "overveiw": "overview",
    "overivew": "overview",
}


def normalize_question(question: str) -> str:
    text = (question or "").strip().lower()
    text = text.replace("on-hand", "on hand").replace("onhand", "on hand")
    text = text.replace("on-time", "on time").replace("ontime", "on time")
    text = text.replace("in-full", "in full").replace("infull", "in full")
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
            "warehouse", "sku", "part", "recommendation", "job", "request", "site",
            "facility", "kpi", "bottleneck", "adjustment", "escalation", "risk",
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
    # Prefer explicit SKU/part labels; bare digits alone are handled cautiously by callers.
    sku_match = re.search(
        r"\b(?:sku|part|item|pn)\s*#?\s*([0-9]{4,8})\b",
        original or normalized,
        flags=re.I,
    )
    if not sku_match:
        # Only accept bare 4–8 digit tokens when not looking like an order fragment.
        sku_match = re.search(r"(?<![A-Za-z-])\b([0-9]{4,8})\b(?!\s*(?:units?|orders?))", normalized)
        if sku_match and re.search(r"\bord-", original or "", flags=re.I):
            sku_match = None
    if sku_match:
        entities["sku"] = sku_match.group(1)

    # DigiTech demo IDs look like ORD-DT82-0081 (letters + digits), not only ORD-123.
    order_match = re.search(
        r"\b(?:order|ord)\s*#?\s*(ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+|\d{3,})\b",
        original or normalized,
        flags=re.I,
    )
    if not order_match:
        order_match = re.search(
            r"\b(ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\b",
            original or normalized,
            flags=re.I,
        )
    if order_match:
        raw = order_match.group(1)
        entities["order_id"] = raw.upper() if str(raw).upper().startswith("ORD-") else raw

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
            if "san francisco" in lowered and "san francisco" in wh:
                entities["warehouse"] = warehouse
                break
            if ("san bernardino" in lowered or "san bernadino" in lowered) and "bernardino" in wh:
                entities["warehouse"] = warehouse
                break

    return entities
