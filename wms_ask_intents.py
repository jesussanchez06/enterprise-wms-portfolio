"""Weighted Ask WMS intent classification (zero paid APIs)."""

from __future__ import annotations

import re
from typing import Any

from wms_ask_concepts import concept_hits, extract_entities, normalize_question


INTENT_DEFS: dict[str, dict[str, Any]] = {
    "recommended_actions": {
        "concepts": ["priority", "supervisor"],
        "phrases": [
            "top action", "recommended action", "what should we do", "what should i do",
            "what needs attention", "focus on", "priorit", "next step", "biggest issue",
            "control tower", "action queue", "where should we focus", "what should supervisor",
        ],
        "weight": 6,
    },
    "warehouse_summary": {
        "concepts": ["executive", "orders"],
        "phrases": [
            "warehouse summary", "how is the warehouse", "how are we doing", "executive summary",
            "summarize today", "operations summary", "overall performance", "biggest issue",
            "operational risk", "end to end",
        ],
        "weight": 5,
    },
    "shipping_today": {
        "concepts": ["shipping", "orders"],
        "phrases": [
            "shipped today", "ship today", "today shipment", "went out today", "dispatched today",
            "anything ship", "what did we ship", "completed shipment today", "were dispatched",
            "was dispatched", "orders dispatched", "order dispatched", "dispatched",
        ],
        "weight": 7,
    },
    "orders_open": {
        "concepts": ["orders"],
        "phrases": ["open order", "how many open", "orders open", "pending order", "not completed"],
        "weight": 5,
    },
    "orders_by_status": {
        "concepts": ["orders", "picking", "quality", "blocked"],
        "phrases": [
            "waiting to be picked", "being picked", "picking in progress", "pending verification",
            "ready to ship", "blocked order", "orders placed",
        ],
        "weight": 5,
    },
    "critical_orders": {
        "concepts": ["orders", "critical"],
        "phrases": ["critical order", "critical job"],
        "weight": 6,
    },
    "order_status": {
        "concepts": ["orders"],
        "phrases": ["status of order", "what happened with order", "order status"],
        "weight": 8,
        "requires": ["order_id"],
    },
    "sla_summary": {
        "concepts": ["sla"],
        "phrases": ["sla summary", "service level", "sla compliance", "within sla"],
        "weight": 6,
    },
    "sla_breached": {
        "concepts": ["sla"],
        "phrases": ["breached", "overdue", "late order", "past deadline", "whats late", "what's late"],
        "weight": 7,
    },
    "sla_at_risk": {
        "concepts": ["sla"],
        "phrases": ["at risk", "about to be late", "close to deadline", "least time left"],
        "weight": 7,
    },
    "inventory_summary": {
        "concepts": ["inventory"],
        "phrases": ["inventory summary", "current inventory", "inventory performance", "stock summary"],
        "weight": 5,
    },
    "inventory_value": {
        "concepts": ["inventory"],
        "phrases": ["inventory value", "stock value", "value by warehouse", "total value"],
        "weight": 6,
    },
    "sku_count": {
        "concepts": ["inventory"],
        "phrases": ["how many sku", "sku available", "active sku", "number of sku"],
        "weight": 7,
    },
    "top_inventory_qty": {
        "concepts": ["inventory"],
        "phrases": [
            "most inventory", "highest stock", "top sku", "which part has the most",
            "which sku has the most", "largest inventory", "top part by quantity",
            "top parts by quantity", "by quantity", "most stock",
        ],
        "weight": 8,
    },
    "least_inventory_qty": {
        "concepts": ["inventory"],
        "phrases": ["least inventory", "lowest stock", "smallest inventory", "fewest units"],
        "weight": 8,
    },
    "top_inventory_value": {
        "concepts": ["inventory"],
        "phrases": ["top sku by value", "highest inventory value", "by inventory value"],
        "weight": 8,
    },
    "sku_inventory": {
        "concepts": ["inventory"],
        "phrases": ["how much sku", "parts left", "on hand for", "available for", "stock for"],
        "weight": 8,
        "requires": ["sku"],
    },
    "low_stock": {
        "concepts": ["inventory"],
        "phrases": ["low stock", "running low", "out of stock", "need restock", "skus need attention"],
        "weight": 7,
    },
    "inventory_accuracy": {
        "concepts": ["inventory", "quality"],
        "phrases": ["inventory accuracy", "stock accuracy", "how accurate is inventory"],
        "weight": 7,
    },
    "picking_summary": {
        "concepts": ["picking", "capacity"],
        "phrases": [
            "ready to pick", "picks in progress", "picker", "workboard", "picking productivity",
            "who is picking", "backlog", "work queue",
        ],
        "weight": 6,
    },
    "quality_summary": {
        "concepts": ["quality"],
        "phrases": [
            "quality pass", "pass rate", "quality issue", "quality problem", "quality problems",
            "failed audit", "pending verification", "quality summary", "pick accuracy",
            "any quality", "open quality",
        ],
        "weight": 6,
    },
    "supervisor_summary": {
        "concepts": ["supervisor", "priority"],
        "phrases": [
            "needs my attention", "control tower", "supervisor", "blocked", "escalation",
            "operational risk", "under pressure",
        ],
        "weight": 6,
    },
    "planner_summary": {
        "concepts": ["planner"],
        "phrases": ["planner", "destination", "urgency mix", "planned workload", "release"],
        "weight": 5,
    },
    "help_explain": {
        "concepts": ["help"],
        "phrases": [
            "what can ask wms", "explain", "what does", "how does", "difference between",
            "order workflow", "reset the demo", "what should executives",
        ],
        "weight": 5,
    },
    "write_blocked": {
        "concepts": ["write_request"],
        "phrases": [
            "ship order", "add 100", "add units", "add unit", "delete order", "create order",
            "update inventory", "pass this", "fail this", "reset demo", "adjust inventory",
        ],
        "weight": 9,
    },
}


def classify_intent(question: str, warehouses: list[str] | None = None, prior_context: dict | None = None) -> dict[str, Any]:
    original = (question or "").strip()
    normalized = normalize_question(original)
    entities = extract_entities(normalized, original, warehouses=warehouses)
    hits = concept_hits(normalized)
    scores: dict[str, float] = {name: 0.0 for name in INTENT_DEFS}

    for intent, definition in INTENT_DEFS.items():
        score = 0.0
        for concept in definition.get("concepts", []):
            score += hits.get(concept, 0) * 1.5
        for phrase in definition.get("phrases", []):
            if phrase in normalized:
                score += definition.get("weight", 5)
        required = definition.get("requires") or []
        if required and all(entities.get(key) for key in required):
            score += 4
        elif required and not all(entities.get(key) for key in required):
            score *= 0.35
        scores[intent] = score

    # Follow-up resolution
    prior = prior_context or {}
    if prior.get("intent") and re.fullmatch(r"(which ones?|list them|show them|those|these)", normalized or ""):
        return {
            "intent": prior.get("intent"),
            "confidence": "high",
            "score": 99,
            "entities": {**prior.get("entities", {}), **entities, "follow_up": True},
            "normalized": normalized,
            "concept_hits": hits,
        }

    if prior.get("sku") and re.search(r"\b(open order|using it|for it|orders using)\b", normalized):
        entities["sku"] = prior["sku"]
        return {
            "intent": "sku_open_orders",
            "confidence": "high",
            "score": 90,
            "entities": entities,
            "normalized": normalized,
            "concept_hits": hits,
        }

    # Write attempts always win — Ask WMS is read-only.
    if hits.get("write_request") or re.search(
        r"\b(ship order|delete order|create order|update inventory|reset demo|add \d+\s+units?)\b",
        normalized,
    ):
        return {
            "intent": "write_blocked",
            "confidence": "high",
            "score": 100,
            "entities": entities,
            "normalized": normalized,
            "concept_hits": hits,
            "scores": scores,
        }

    # Boost combinations
    if hits.get("shipping") and (
        "today" in normalized
        or entities.get("date_label") == "today"
        or re.search(r"\b(dispatched|shipped|went out|shipment)\b", normalized)
    ):
        scores["shipping_today"] += 8
        entities.setdefault("date_label", "today")
    if hits.get("priority") or re.search(r"\btop\s+\d+\s+(action|priorit|recommendation)", normalized):
        scores["recommended_actions"] += 10
    if hits.get("sla") and ("breach" in normalized or "late" in normalized or "overdue" in normalized):
        scores["sla_breached"] += 8
    if hits.get("sla") and "risk" in normalized:
        scores["sla_at_risk"] += 8
    if entities.get("sku") and hits.get("inventory"):
        scores["sku_inventory"] += 10
    if entities.get("order_id") and not hits.get("write_request"):
        scores["order_status"] += 12
    if re.search(r"\b(top|most|highest|largest)\b", normalized) and hits.get("inventory"):
        if "value" in normalized:
            scores["top_inventory_value"] += 10
        elif re.search(r"\b(least|lowest|fewest|smallest)\b", normalized):
            scores["least_inventory_qty"] += 10
        else:
            scores["top_inventory_qty"] += 10

    best_intent = max(scores, key=scores.get)
    best_score = scores[best_intent]
    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    margin = best_score - second

    if best_score < 4:
        confidence = "low"
        intent = "unknown"
    elif best_score < 7 and margin < 2.5:
        confidence = "medium"
        intent = best_intent
    else:
        confidence = "high"
        intent = best_intent

    if not entities.get("limit"):
        if intent == "recommended_actions":
            entities["limit"] = 3
        elif intent in {"top_inventory_qty", "least_inventory_qty", "top_inventory_value"}:
            entities["limit"] = 5 if "top" in normalized else 1

    return {
        "intent": intent,
        "confidence": confidence,
        "score": best_score,
        "entities": entities,
        "normalized": normalized,
        "concept_hits": hits,
        "scores": scores,
    }
