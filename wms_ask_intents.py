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
        "phrases": [
            "open order", "how many open", "order open", "order are open", "orders are open",
            "pending order", "not completed", "how many order", "order currently open",
        ],
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
            "top parts by quantity", "by quantity", "most stock", "highest quantity",
        ],
        "weight": 8,
    },
    "least_inventory_qty": {
        "concepts": ["inventory"],
        "phrases": [
            "least inventory", "lowest stock", "smallest inventory", "fewest units",
            "which sku has the lowest", "which part has the lowest", "which has the lowest",
            "which sku has the least", "which has the least", "the lowest", "the least",
            "lowest inventory", "least stock", "lowest quantity", "least quantity",
        ],
        "weight": 8,
    },
    "top_inventory_value": {
        "concepts": ["inventory"],
        "phrases": [
            "top sku by value", "highest inventory value", "by inventory value",
            "worth the most", "worth most", "most valuable", "highest value",
            "by value", "in terms of value", "inventory value ranking", "worth the most money",
            "most money", "highest dollar", "by dollar", "by money",
        ],
        "weight": 8,
    },
    "sku_inventory": {
        "concepts": ["inventory"],
        "phrases": ["how much sku", "parts left", "on hand for", "available for", "stock for"],
        "weight": 8,
        "requires": ["sku"],
    },
    "sku_open_orders": {
        "concepts": ["orders", "inventory"],
        "phrases": [
            "open order using", "open order for", "order using", "order for sku",
            "open demand for", "which order use", "order requesting", "order that use",
            "using it", "for it",
        ],
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
    "picker_count": {
        "concepts": ["picking"],
        "phrases": [
            "how many picker", "active picker", "list picker", "picker roster",
            "who are the picker", "configured picker", "picker do you have",
            "pickers do you have", "number of picker", "how many active picker",
        ],
        "weight": 8,
    },
    "picking_summary": {
        "concepts": ["picking", "capacity"],
        "phrases": [
            "ready to pick", "picks in progress", "workboard", "picking productivity",
            "who is picking", "backlog", "work queue", "picker workload", "picking backlog",
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

INVENTORY_RANKING_INTENTS = {
    "top_inventory_qty",
    "least_inventory_qty",
    "top_inventory_value",
    "inventory_summary",
    "inventory_value",
    "sku_count",
    "low_stock",
}


def intent_family_for(intent: str | None) -> str | None:
    if intent in INVENTORY_RANKING_INTENTS:
        return "inventory_ranking"
    if intent in {"picking_summary", "picker_count"}:
        return "picking"
    if intent in {"sla_summary", "sla_breached", "sla_at_risk"}:
        return "sla"
    if intent in {"orders_open", "orders_by_status", "critical_orders", "order_status", "shipping_today"}:
        return "orders"
    return None


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
    prior_intent = prior.get("intent")
    prior_family = prior.get("intent_family") or intent_family_for(prior_intent)

    if prior_intent and re.fullmatch(r"(which ones?|list them|show them|those|these)", normalized or ""):
        return {
            "intent": prior_intent,
            "confidence": "high",
            "score": 99,
            "entities": {**prior.get("entities", {}), **entities, "follow_up": True},
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": intent_family_for(prior_intent),
        }

    # Inventory ranking continuity: short reframes after a SKU/qty ranking turn.
    if prior_family == "inventory_ranking" or prior_intent in INVENTORY_RANKING_INTENTS:
        value_follow = bool(
            re.search(r"\b(value|worth|money|dollar|dollars|valuable|priced?)\b", normalized)
        )
        least_follow = bool(
            re.search(r"\b(lowest|least|fewest|smallest|minimum|bottom)\b", normalized)
        )
        most_follow = bool(
            re.search(r"\b(most|highest|largest|greatest|top|biggest)\b", normalized)
        )
        short_follow = len(normalized.split()) <= 8
        if value_follow and (short_follow or most_follow or "in terms" in normalized):
            return {
                "intent": "top_inventory_value",
                "confidence": "high",
                "score": 95,
                "entities": {**prior.get("entities", {}), **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }
        if least_follow and short_follow:
            return {
                "intent": "least_inventory_qty",
                "confidence": "high",
                "score": 94,
                "entities": {**prior.get("entities", {}), **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }
        if most_follow and short_follow and not value_follow:
            return {
                "intent": "top_inventory_qty",
                "confidence": "high",
                "score": 93,
                "entities": {**prior.get("entities", {}), **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }

    if prior.get("sku") and re.search(
        r"\b(open order|using it|for it|order using|order for)\b",
        normalized,
    ):
        entities["sku"] = prior["sku"]
        return {
            "intent": "sku_open_orders",
            "confidence": "high",
            "score": 90,
            "entities": entities,
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": intent_family_for("sku_open_orders"),
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
    if hits.get("orders") and re.search(
        r"\b(how many order|order are open|order open|open order|pending order|not completed)\b",
        normalized,
    ) and not entities.get("sku"):
        scores["orders_open"] += 8
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
    if entities.get("sku") and re.search(
        r"\b(open order|order using|order for|order request|demand for|which order use)\b",
        normalized,
    ):
        scores["sku_open_orders"] += 14
    elif entities.get("sku") and hits.get("inventory"):
        scores["sku_inventory"] += 10
    if entities.get("order_id") and not hits.get("write_request"):
        scores["order_status"] += 12

    inventoryish = bool(hits.get("inventory")) or bool(
        re.search(r"\b(sku|part|stock|inventory|units?)\b", normalized)
    )
    which_sku = bool(re.search(r"\b(which|what)\b", normalized)) and bool(
        re.search(r"\b(sku|part|item|stock|inventory)\b", normalized)
    )
    valueish = bool(re.search(r"\b(value|worth|dollar|dollars|money|valuable|priced?)\b", normalized))
    leastish = bool(re.search(r"\b(least|lowest|fewest|smallest|minimum|bottom)\b", normalized))
    mostish = bool(re.search(r"\b(most|highest|largest|greatest|top|biggest)\b", normalized))
    if inventoryish or which_sku:
        if valueish and (mostish or which_sku or "in terms" in normalized):
            scores["top_inventory_value"] += 14
        elif leastish:
            scores["least_inventory_qty"] += 14
        elif mostish:
            scores["top_inventory_qty"] += 12
        elif which_sku and not valueish:
            # "which sku has the ..." without an explicit polarity still leans quantity ranking.
            scores["top_inventory_qty"] += 4

    if hits.get("picking") and re.search(
        r"\b(how many|list|active|roster|configured|do you have|available)\b",
        normalized,
    ) and re.search(r"\bpicker\b", normalized):
        scores["picker_count"] += 12

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
        "intent_family": intent_family_for(intent),
    }
