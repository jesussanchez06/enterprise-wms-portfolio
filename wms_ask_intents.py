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
            "what first", "triage", "what should ops", "give me priorities", "biggest priorities",
            "top recomend", "recommend",
        ],
        "weight": 6,
    },
    "warehouse_summary": {
        "concepts": ["executive", "orders"],
        "phrases": [
            "warehouse summary", "how is the warehouse", "how are we doing", "executive summary",
            "summarize today", "operations summary", "overall performance", "biggest issue",
            "operational risk", "end to end", "kpi overview", "scorecard", "pulse check",
            "ops snapshot", "management summary", "how is digitech", "warehouse performing",
            "how are operations", "risk overview", "remaining risk", "warehouse health",
            "completion rate", "top kpi", "executive overview",
        ],
        "weight": 5,
    },
    "kpi_snapshot": {
        "concepts": ["executive"],
        "phrases": [
            "the kpi", "today kpi", "todays kpi", "important kpi", "current kpi",
            "show kpi", "show me kpi", "give me kpi", "what are the kpi", "what are kpi",
            "list kpi", "kpi today", "kpi status", "our kpi", "warehouse kpi",
            "key performance", "performance indicator", "show me today kpi",
            "give me the important kpi", "what kpi",
        ],
        "weight": 8,
    },
    "remaining_work": {
        "concepts": ["capacity", "orders"],
        "phrases": [
            "remaining work", "work remaining", "what's left", "what is left", "left to do",
            "outstanding work", "still open", "how much work left", "open workload",
            "work still open", "what's outstanding",
        ],
        "weight": 7,
    },
    "warehouse_count": {
        "concepts": ["warehouse_network"],
        "phrases": [
            "how many warehouse", "warehouse count", "how many site", "list warehouse",
            "list my warehouse", "list the warehouse", "which warehouse do", "which warehouse have",
            "warehouse do i have", "warehouse do you have", "number of warehouse",
            "configured warehouse", "warehouse network", "site do i have", "show me the warehouse",
            "show my warehouse", "what warehouse do", "which site do",
        ],
        "weight": 8,
    },
    "warehouse_compare": {
        "concepts": ["warehouse_network", "compare"],
        "phrases": [
            "compare warehouse", "compare site", "warehouse vs", "which warehouse has",
            "best warehouse", "worst warehouse", "side by side warehouse", "by warehouse",
            "inventory by warehouse", "value by site", "site comparison", "warehouse comparison",
            "which site is", "across warehouse",
        ],
        "weight": 7,
    },
    "shipping_today": {
        "concepts": ["shipping", "orders"],
        "phrases": [
            "shipped today", "ship today", "today shipment", "went out today", "dispatched today",
            "anything ship", "what did we ship", "completed shipment today", "were dispatched",
            "was dispatched", "orders dispatched", "order dispatched", "dispatched",
            "how many shipped", "shipments today", "outbound today",
        ],
        "weight": 7,
    },
    "shipping_summary": {
        "concepts": ["shipping"],
        "phrases": [
            "shipping summary", "ready to ship", "pending ship", "awaiting ship",
            "shipping queue", "outbound summary", "what is ready to ship", "ship pending",
            "orders ready for shipping", "shipping status", "shipping performance",
            "shipping volume", "shipping sla", "shipping trend", "late shipment",
        ],
        "weight": 6,
    },
    "orders_open": {
        "concepts": ["orders"],
        "phrases": [
            "open order", "how many open", "order open", "order are open", "orders are open",
            "pending order", "not completed", "how many order", "order currently open",
            "active order", "unfinished order", "how many pending",
        ],
        "weight": 5,
    },
    "orders_created_today": {
        "concepts": ["orders"],
        "phrases": [
            "created today", "received today", "orders today", "new order today",
            "how many order today", "orders placed today", "demand today", "incoming today",
            "order created today", "order received today",
        ],
        "weight": 7,
    },
    "orders_completed": {
        "concepts": ["orders", "shipping"],
        "phrases": [
            "completed order", "order completed", "how many completed", "orders completed",
            "finished order", "order finished", "closed order", "done order", "how many finished",
        ],
        "weight": 6,
    },
    "orders_oldest": {
        "concepts": ["orders", "capacity"],
        "phrases": [
            "oldest order", "oldest open", "longest waiting", "aging order", "aging backlog",
            "age of backlog", "which order is oldest", "stale order", "first in queue",
            "longest open",
        ],
        "weight": 7,
    },
    "orders_by_status": {
        "concepts": ["orders", "picking", "quality", "blocked"],
        "phrases": [
            "waiting to be picked", "being picked", "picking in progress", "pending verification",
            "ready to ship", "blocked order", "orders placed", "by status", "status breakdown",
            "orders by status", "stuck order", "partial pick", "partially picked",
        ],
        "weight": 5,
    },
    "critical_orders": {
        "concepts": ["orders", "critical"],
        "phrases": [
            "critical order", "critical job", "critical open", "sev1 order", "p0 order",
            "most urgent order",
        ],
        "weight": 6,
    },
    "order_status": {
        "concepts": ["orders"],
        "phrases": [
            "status of order", "what happened with order", "order status", "where is order",
            "tell me about order", "look up order", "details for order", "info on order",
        ],
        "weight": 8,
        "requires": ["order_id"],
    },
    "sla_summary": {
        "concepts": ["sla"],
        "phrases": [
            "sla summary", "service level", "sla compliance", "within sla", "sla health",
            "sla overview", "how is sla", "sla status", "compliance rate",
        ],
        "weight": 6,
    },
    "sla_breached": {
        "concepts": ["sla"],
        "phrases": [
            "breached", "overdue", "late order", "past deadline", "whats late", "what's late",
            "missed sla", "past due", "late?", "anything late", "who is late",
        ],
        "weight": 7,
    },
    "sla_at_risk": {
        "concepts": ["sla"],
        "phrases": [
            "at risk", "about to be late", "close to deadline", "least time left",
            "nearly late", "approaching deadline", "sla risk",
        ],
        "weight": 7,
    },
    "sla_healthy": {
        "concepts": ["sla"],
        "phrases": [
            "healthy sla", "within window", "on track sla", "orders that are healthy",
            "sla healthy", "still healthy",
        ],
        "weight": 6,
    },
    "otif_summary": {
        "concepts": ["otif", "sla", "shipping"],
        "phrases": [
            "otif", "on time in full", "on time and in full", "perfect order rate",
            "fill rate and on time", "otif rate", "otif performance",
        ],
        "weight": 8,
    },
    "inventory_summary": {
        "concepts": ["inventory"],
        "phrases": [
            "inventory summary", "current inventory", "inventory performance", "stock summary",
            "inventory overview", "stock overview", "inventory?", "stock?",
        ],
        "weight": 5,
    },
    "inventory_value": {
        "concepts": ["inventory"],
        "phrases": [
            "inventory value", "stock value", "value by warehouse", "total value",
            "how much is inventory worth", "dollar value of stock",
        ],
        "weight": 6,
    },
    "sku_count": {
        "concepts": ["inventory"],
        "phrases": ["how many sku", "sku available", "active sku", "number of sku", "sku count"],
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
        "phrases": [
            "how much sku", "parts left", "on hand for", "available for", "stock for",
            "where is sku", "location of", "qty for", "quantity for",
        ],
        "weight": 8,
        "requires": ["sku"],
    },
    "sku_open_orders": {
        "concepts": ["orders", "inventory"],
        "phrases": [
            "open order using", "open order for", "order using", "order for sku",
            "open demand for", "which order use", "order requesting", "order that use",
            "using it", "for it", "demand for sku",
        ],
        "weight": 8,
        "requires": ["sku"],
    },
    "low_stock": {
        "concepts": ["inventory"],
        "phrases": [
            "low stock", "running low", "out of stock", "need restock", "skus need attention",
            "stockout", "zero stock", "below threshold", "overstock", "over stock",
            "inventory warning", "stock warning", "units on hand",
        ],
        "weight": 7,
    },
    "inventory_accuracy": {
        "concepts": ["inventory", "quality"],
        "phrases": [
            "inventory accuracy", "stock accuracy", "how accurate is inventory",
            "location accuracy", "bin accuracy",
        ],
        "weight": 7,
    },
    "inventory_adjustments": {
        "concepts": ["inventory", "adjustments"],
        "phrases": [
            "inventory adjustment", "stock adjustment", "adjust transaction", "recent adjustment",
            "cycle count", "qty correction", "inventory correction", "adjusted stock",
        ],
        "weight": 7,
    },
    "picker_count": {
        "concepts": ["picking"],
        "phrases": [
            "how many picker", "active picker", "list picker", "picker roster",
            "who are the picker", "configured picker", "picker do you have",
            "pickers do you have", "number of picker", "how many active picker",
            "picker?", "who picks",
        ],
        "weight": 8,
    },
    "picking_summary": {
        "concepts": ["picking", "capacity"],
        "phrases": [
            "ready to pick", "picks in progress", "workboard", "picking productivity",
            "who is picking", "backlog", "work queue", "picker workload", "picking backlog",
            "pick queue", "ops queue", "picking summary", "pipeline", "waiting verification",
            "recently finished", "active pickers", "most orders picker", "blocked shortages",
        ],
        "weight": 6,
    },
    "productivity_limits": {
        "concepts": ["productivity"],
        "phrases": [
            "productivity", "units per hour", "picks per hour", "throughput rate",
            "picker productivity", "how productive", "efficiency rate", "output rate",
            "avg daily volume", "average daily volume", "fill rate", "workload by responsibility",
            "productivity trend", "daily volume",
        ],
        "weight": 7,
    },
    "quality_summary": {
        "concepts": ["quality"],
        "phrases": [
            "quality pass", "pass rate", "quality summary", "pick accuracy",
            "quality audit pass rate", "audit pass rate", "qc status",
            "any quality", "open quality", "quality?", "quality problem", "quality problems",
        ],
        "weight": 6,
    },
    "quality_failed_detail": {
        "concepts": ["quality"],
        "phrases": [
            "which audit failed", "audit failed", "failed inspection", "order failed inspection",
            "why did the audit fail", "why did audit fail", "why fail", "what discrepancy",
            "discrepancy was found", "carton damage", "carton crush", "corner crush",
            "quality issue", "which order has a quality", "order with the mistake",
            "the mistake", "with the mistake", "failed audit", "failed quality",
            "which order failed", "inspection fail", "what went wrong with quality",
            "quality verification fail", "damage found",
        ],
        "weight": 9,
    },
    "quality_audits_list": {
        "concepts": ["quality"],
        "phrases": [
            "show failed audit", "failed audits", "list failed audit", "quality history",
            "today's audit", "todays audit", "audits today", "show quality issue",
            "list quality issue", "quality issues", "show audits", "audit history",
            "recent audit", "all failed",
        ],
        "weight": 8,
    },
    "quality_auditor": {
        "concepts": ["quality"],
        "phrases": [
            "who audited", "which auditor", "who was the auditor", "auditor?",
            "who inspected", "which inspector", "who did the audit", "audited by",
        ],
        "weight": 9,
    },
    "context_picker": {
        "concepts": ["picking"],
        "phrases": [
            "who picked it", "who picked that", "who picked the order", "which picker",
            "who was the picker", "picker for it", "who picked", "picked by whom",
        ],
        "weight": 9,
    },
    "context_sku": {
        "concepts": ["inventory", "orders"],
        "phrases": [
            "what sku was involved", "which sku was involved", "what sku", "which sku",
            "sku involved", "what part was involved", "which part was on", "lines on it",
            "what was the sku",
        ],
        "weight": 8,
    },
    "context_shipped": {
        "concepts": ["shipping", "orders"],
        "phrases": [
            "was it shipped", "did it ship", "has it shipped", "was that shipped",
            "did that order ship", "shipped yet", "is it completed", "did it complete",
        ],
        "weight": 9,
    },
    "kpi_attention": {
        "concepts": ["executive", "priority"],
        "phrases": [
            "which kpi needs attention", "kpi needs attention", "which kpi is off",
            "kpi attention", "what kpi is off", "which metric needs", "kpi below target",
        ],
        "weight": 8,
    },
    "ai_briefing": {
        "concepts": ["ai_analysis", "executive", "priority"],
        "phrases": [
            "focus today", "what should i focus", "summarize performance", "biggest risk",
            "kpis below target", "vs yesterday",
            "versus yesterday", "compared to yesterday", "30 minute review", "thirty minute",
            "review first", "inventory attention", "warehouse attention", "why productivity",
            "ai analysis", "first look",
        ],
        "weight": 8,
    },
    "avg_completion_time": {
        "concepts": ["orders", "productivity", "executive"],
        "phrases": [
            "average completion time", "avg completion time", "avg completion",
            "mean completion time", "how long to complete", "average cycle time",
            "average order time", "completion time average",
        ],
        "weight": 9,
    },
    "orders_urgent_mix": {
        "concepts": ["orders", "urgent", "critical", "blocked"],
        "phrases": [
            "urgent order", "count urgent", "how many urgent", "urgent critical blocked",
            "blocked waiting inventory", "missed sla", "longest order",
            "fastest order", "completed today list", "list completed today",
        ],
        "weight": 6,
    },
    "supervisor_summary": {
        "concepts": ["supervisor"],
        "phrases": [
            "needs my attention", "control tower", "control tower status", "supervisor summary",
            "supervisor view", "supervisor", "escalation", "operational risk", "under pressure",
            "floor issues", "supervisor status", "escalations", "dashboard summary",
            "healthy urgency", "urgency mix supervisor",
        ],
        "weight": 7,
    },
    "planner_summary": {
        "concepts": ["planner"],
        "phrases": [
            "planner", "order planning", "destination", "urgency mix", "planned workload", "release",
            "planner summary", "order planning summary", "planning view", "workload by destination",
        ],
        "weight": 5,
    },
    "cross_ops_risk": {
        "concepts": ["sla", "inventory", "quality", "shipping", "supervisor"],
        "phrases": [
            "inventory causing sla", "stock delaying", "quality blocking ship",
            "quality delaying shipping", "what is the bottleneck", "what's the bottleneck",
            "bottleneck", "cross functional risk", "inventory vs sla", "quality vs shipping",
            "what's blocking fulfillment", "root cause of delay", "blocking fulfillment",
        ],
        "weight": 8,
    },
    "kpi_definitions": {
        "concepts": ["help", "kpi_def"],
        "phrases": [
            "what is sla", "define sla", "what is otif", "define otif", "what does kpi",
            "kpi definition", "meaning of sla", "meaning of otif", "what is inventory accuracy",
            "explain sla", "explain otif", "what do these kpi", "define healthy at risk",
            "define ",
        ],
        "weight": 9,
    },
    "help_explain": {
        "concepts": ["help"],
        "phrases": [
            "what can ask wms", "explain", "what does", "how does", "difference between",
            "order workflow", "reset the demo", "what should executives", "digitech wms",
            "ask wms", "what can you answer", "help?", "capabilities", "how do i use ask",
            "is this read only", "read-only", "readonly",
        ],
        "weight": 5,
    },
    "unsupported_predictive": {
        "concepts": ["predictive"],
        "phrases": [
            "forecast", "predict", "will we miss", "next week", "next month",
            "who should we fire", "hire more", "lay off", "layoff", "terminate picker",
            "future demand", "headcount",
        ],
        "weight": 9,
    },
    "write_blocked": {
        "concepts": ["write_request"],
        "phrases": [
            "ship order", "add 100", "add units", "add unit", "delete order", "create order",
            "update inventory", "pass this", "fail this", "reset demo", "adjust inventory",
            "mark completed", "reassign picker", "move stock", "change status",
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
    "inventory_adjustments",
}

SHORT_FORM_MAP = {
    "late": "sla_breached",
    "overdue": "sla_breached",
    "breached": "sla_breached",
    "priority": "recommended_actions",
    "priorities": "recommended_actions",
    "action": "recommended_actions",
    "actions": "recommended_actions",
    "inventory": "inventory_summary",
    "stock": "inventory_summary",
    "quality": "quality_summary",
    "qc": "quality_summary",
    "audits": "quality_audits_list",
    "auditor": "quality_auditor",
    "discrepancy": "quality_failed_detail",
    "damage": "quality_failed_detail",
    "mistake": "quality_failed_detail",
    "shipping": "shipping_summary",
    "shipments": "shipping_today",
    "blocked": "orders_by_status",
    "sla": "sla_summary",
    "otif": "otif_summary",
    "picker": "picker_count",
    "pickers": "picker_count",
    "help": "help_explain",
    "backlog": "remaining_work",
    "bottleneck": "cross_ops_risk",
    "risks": "warehouse_summary",
    "risk": "warehouse_summary",
    "kpis": "kpi_snapshot",
    "kpi": "kpi_snapshot",
    "status": "warehouse_summary",
}


def intent_family_for(intent: str | None) -> str | None:
    if intent in INVENTORY_RANKING_INTENTS:
        return "inventory_ranking"
    if intent in {"picking_summary", "picker_count", "productivity_limits", "context_picker"}:
        return "picking"
    if intent in {
        "warehouse_count",
        "warehouse_summary",
        "warehouse_compare",
        "remaining_work",
        "kpi_snapshot",
        "kpi_attention",
        "ai_briefing",
        "avg_completion_time",
    }:
        return "warehouse_network"
    if intent in {"sla_summary", "sla_breached", "sla_at_risk", "sla_healthy", "otif_summary"}:
        return "sla"
    if intent in {
        "orders_open",
        "orders_by_status",
        "critical_orders",
        "order_status",
        "shipping_today",
        "shipping_summary",
        "orders_created_today",
        "orders_completed",
        "orders_oldest",
        "orders_urgent_mix",
        "context_shipped",
        "context_sku",
    }:
        return "orders"
    if intent in {
        "quality_summary",
        "quality_failed_detail",
        "quality_audits_list",
        "quality_auditor",
    }:
        return "quality"
    if intent in {"sku_inventory", "sku_open_orders"}:
        return "sku"
    if intent in {"recommended_actions", "supervisor_summary", "cross_ops_risk"}:
        return "supervisor"
    if intent in {"help_explain", "kpi_definitions"}:
        return "help"
    return None


def _short_form_intent(normalized: str) -> str | None:
    token = (normalized or "").strip()
    if not token:
        return None
    # Ultra-short: one or two tokens, often recruiter one-word prompts.
    words = token.split()
    if len(words) == 1:
        return SHORT_FORM_MAP.get(words[0])
    if len(words) == 2 and words[0] in {"any", "whats", "what's", "show", "list", "todays", "today"}:
        return SHORT_FORM_MAP.get(words[1])
    if len(words) <= 4 and "kpi" in words and not any(
        w in words for w in ("define", "definition", "meaning", "explain", "calculated")
    ):
        return "kpi_snapshot"
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
    prior_entities = prior.get("entities") or {}

    if prior_intent and re.fullmatch(
        r"(which ones?|list them|show them|those|these|the critical ones?|critical ones?)",
        normalized or "",
    ):
        follow_intent = prior_intent
        merged = {**prior_entities, **entities, "follow_up": True}
        if "critical" in normalized:
            merged["urgency"] = "Critical"
            follow_intent = "critical_orders" if prior_family == "orders" else prior_intent
        return {
            "intent": follow_intent,
            "confidence": "high",
            "score": 99,
            "entities": merged,
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": intent_family_for(follow_intent),
        }

    # "its warehouse" / "which warehouse" after SKU turn
    if (prior.get("sku") or prior_entities.get("sku")) and re.search(
        r"\b(its warehouse|which warehouse|where is it|what warehouse|location)\b",
        normalized,
    ):
        entities["sku"] = prior.get("sku") or prior_entities.get("sku")
        return {
            "intent": "sku_inventory",
            "confidence": "high",
            "score": 96,
            "entities": {**prior_entities, **entities, "follow_up": True},
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": "sku",
        }

    # Order follow-up: SLA / warehouse / picker for that order
    if (prior.get("order_id") or prior_entities.get("order_id")) and re.search(
        r"\b(its (sla|warehouse|status|picker)|for that order|that order|sla for it|is it late|at risk)\b",
        normalized,
    ):
        entities["order_id"] = prior.get("order_id") or prior_entities.get("order_id")
        return {
            "intent": "order_status",
            "confidence": "high",
            "score": 96,
            "entities": {**prior_entities, **entities, "follow_up": True},
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": "orders",
        }

    # Quality / order cross-module follow-ups (picker / SKU / shipped).
    prior_order = prior.get("order_id") or prior_entities.get("order_id")
    prior_family_is_quality = prior_family == "quality" or prior_intent in {
        "quality_failed_detail",
        "quality_audits_list",
        "quality_auditor",
        "quality_summary",
        "order_status",
    }
    if prior_order and (
        prior_family_is_quality
        or prior.get("picker")
        or prior_entities.get("sku")
        or prior.get("sku")
    ):
        if re.search(r"\b(who picked|which picker|picked (it|that)|picker for)\b", normalized):
            entities["order_id"] = prior_order
            if prior.get("picker") or prior_entities.get("picker"):
                entities["picker"] = prior.get("picker") or prior_entities.get("picker")
            return {
                "intent": "context_picker",
                "confidence": "high",
                "score": 98,
                "entities": {**prior_entities, **entities, "follow_up": True},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "picking",
            }
        if re.search(
            r"\b(what sku|which sku|sku (was |is )?involved|what part|which part|lines? on it)\b",
            normalized,
        ):
            entities["order_id"] = prior_order
            if prior.get("sku") or prior_entities.get("sku"):
                entities["sku"] = prior.get("sku") or prior_entities.get("sku")
            return {
                "intent": "context_sku",
                "confidence": "high",
                "score": 98,
                "entities": {**prior_entities, **entities, "follow_up": True},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "orders",
            }
        if re.search(
            r"\b(was it shipped|did it ship|has it shipped|shipped yet|did it complete|is it completed)\b",
            normalized,
        ):
            entities["order_id"] = prior_order
            return {
                "intent": "context_shipped",
                "confidence": "high",
                "score": 98,
                "entities": {**prior_entities, **entities, "follow_up": True},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "orders",
            }
        if re.search(r"\b(who audited|which auditor|auditor|who inspected)\b", normalized):
            entities["order_id"] = prior_order
            return {
                "intent": "quality_auditor",
                "confidence": "high",
                "score": 97,
                "entities": {**prior_entities, **entities, "follow_up": True},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "quality",
            }
        if re.search(
            r"\b(why (did )?(it |the audit )?fail|what discrepancy|carton|damage|mistake)\b",
            normalized,
        ):
            entities["order_id"] = prior_order
            return {
                "intent": "quality_failed_detail",
                "confidence": "high",
                "score": 97,
                "entities": {**prior_entities, **entities, "follow_up": True},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "quality",
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
                "entities": {**prior_entities, **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }
        if least_follow and short_follow:
            return {
                "intent": "least_inventory_qty",
                "confidence": "high",
                "score": 94,
                "entities": {**prior_entities, **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }
        if most_follow and short_follow and not value_follow:
            return {
                "intent": "top_inventory_qty",
                "confidence": "high",
                "score": 93,
                "entities": {**prior_entities, **entities, "follow_up": True, "limit": entities.get("limit") or 1},
                "normalized": normalized,
                "concept_hits": hits,
                "intent_family": "inventory_ranking",
            }

    if prior.get("sku") and re.search(
        r"\b(open order|using it|for it|order using|order for|critical ones?)\b",
        normalized,
    ):
        entities["sku"] = prior["sku"]
        if "critical" in normalized:
            entities["urgency"] = "Critical"
        return {
            "intent": "sku_open_orders",
            "confidence": "high",
            "score": 90,
            "entities": entities,
            "normalized": normalized,
            "concept_hits": hits,
            "intent_family": intent_family_for("sku_open_orders"),
        }

    # Predictive / HR unsupported always wins over normal ops intents.
    if hits.get("predictive") or re.search(
        r"\b(forecast|predict|next week|next month|fire|lay ?off|terminate|hire more pickers?)\b",
        normalized,
    ):
        return {
            "intent": "unsupported_predictive",
            "confidence": "high",
            "score": 100,
            "entities": entities,
            "normalized": normalized,
            "concept_hits": hits,
            "scores": scores,
            "intent_family": "unsupported",
        }

    # Write attempts always win — Ask WMS is read-only.
    if hits.get("write_request") or re.search(
        r"\b(ship order|delete order|create order|update inventory|reset demo|add \d+\s+units?|mark completed|reassign picker)\b",
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
            "intent_family": "write",
        }

    # Ultra-short one-word / two-word routing
    short_intent = _short_form_intent(normalized)
    if short_intent and short_intent in INTENT_DEFS:
        if short_intent == "orders_by_status" and normalized in {"blocked", "any blocked", "show blocked", "list blocked"}:
            entities["status_hint"] = "Blocked"
        scores[short_intent] += 18

    # Boost combinations
    if hits.get("orders") and re.search(
        r"\b(how many order|order are open|order open|open order|pending order|not completed)\b",
        normalized,
    ) and not entities.get("sku"):
        scores["orders_open"] += 8
    if hits.get("orders") and re.search(r"\b(created today|received today|new order today|order today)\b", normalized):
        scores["orders_created_today"] += 12
    if hits.get("orders") and re.search(r"\b(oldest|longest waiting|aging|stale)\b", normalized):
        scores["orders_oldest"] += 12
    if hits.get("orders") and re.search(r"\b(completed order|how many completed|finished order)\b", normalized) and "today" not in normalized:
        scores["orders_completed"] += 10
    if hits.get("capacity") or re.search(r"\b(remaining work|work remaining|what('?s| is) left|outstanding work)\b", normalized):
        scores["remaining_work"] += 12
    if hits.get("shipping") and (
        "today" in normalized
        or entities.get("date_label") == "today"
        or re.search(r"\b(dispatched|shipped|went out|shipment)\b", normalized)
    ):
        scores["shipping_today"] += 8
        entities.setdefault("date_label", "today")
    if hits.get("shipping") and re.search(r"\b(ready to ship|pending ship|shipping summary|shipping queue)\b", normalized):
        scores["shipping_summary"] += 10
    if hits.get("priority") or re.search(r"\btop\s+\d+\s+(action|priorit|recommendation)", normalized):
        scores["recommended_actions"] += 10
    if re.search(r"\b(about to be late|close to deadline|nearly late|approaching deadline|at risk)\b", normalized):
        scores["sla_at_risk"] += 14
    elif hits.get("sla") and ("breach" in normalized or "late" in normalized or "overdue" in normalized):
        scores["sla_breached"] += 8
    if hits.get("sla") and "risk" in normalized:
        scores["sla_at_risk"] += 8
    if hits.get("sla") and "healthy" in normalized:
        scores["sla_healthy"] += 8
    if hits.get("otif") or "otif" in normalized or "on time in full" in normalized:
        # Definitions win over live OTIF metric questions.
        if re.search(r"\b(what is|define|meaning of|explain)\b", normalized):
            scores["kpi_definitions"] += 18
        else:
            scores["otif_summary"] += 14
    if hits.get("compare") or re.search(
        r"\b(compare|versus|vs|by warehouse|across warehouse|which warehouse has|site comparison)\b",
        normalized,
    ):
        scores["warehouse_compare"] += 12
        # Prefer site comparison over SKU value ranking when warehouse is explicit.
        if re.search(r"\b(warehouse|site)\b", normalized) and re.search(r"\b(value|worth|inventory)\b", normalized):
            scores["warehouse_compare"] += 8
            scores["top_inventory_value"] -= 6
    if hits.get("adjustments") or re.search(r"\b(adjustment|cycle count|qty correction)\b", normalized):
        scores["inventory_adjustments"] += 10
    if hits.get("productivity") or re.search(r"\b(units per hour|picks per hour|productivity)\b", normalized):
        scores["productivity_limits"] += 12
    if hits.get("kpi_def") or re.search(r"\b(what is|define|meaning of)\s+(sla|otif|kpi|accuracy)\b", normalized):
        scores["kpi_definitions"] += 14
    if re.search(r"\bdefine\b", normalized) and re.search(r"\b(sla|otif|kpi|accuracy)\b", normalized):
        scores["kpi_definitions"] += 16
        scores["otif_summary"] -= 8
    # Live KPI values (not definitions): "what are the KPIs?", "today's KPIs", etc.
    if re.search(r"\bkpi\b", normalized) and not re.search(
        r"\b(what is|define|definition|meaning of|how is .* calculated|explain)\b",
        normalized,
    ):
        scores["kpi_snapshot"] += 16
        scores["warehouse_summary"] += 4
        scores["kpi_definitions"] -= 10
    if re.search(r"\bkpi\b", normalized) and re.search(r"\b(definition|define|meaning)\b", normalized):
        scores["kpi_definitions"] += 18
        scores["kpi_snapshot"] -= 20
    if re.search(
        r"\b(inventory.*(sla|late|delay)|quality.*(ship|shipping)|bottleneck|blocking fulfillment)\b",
        normalized,
    ):
        scores["cross_ops_risk"] += 16
        scores["supervisor_summary"] -= 4
    if re.search(r"\b(control tower|needs my attention|supervisor summary|supervisor view)\b", normalized):
        scores["supervisor_summary"] += 12
        scores["recommended_actions"] -= 4
    if re.search(r"\b(stuck|shortage)\b", normalized) and re.search(r"\b(order|job)\b", normalized):
        scores["orders_by_status"] += 12
        entities["status_hint"] = "Blocked"
    if re.search(r"\b(aging|oldest|longest waiting|stale)\b", normalized):
        scores["orders_oldest"] += 12
        scores["remaining_work"] -= 4
    if re.search(r"\b(order completed|completed order|how many completed|finished order)\b", normalized):
        scores["orders_completed"] += 12
    if entities.get("sku") and re.search(
        r"\b(open order|order using|order for|order request|demand for|which order use)\b",
        normalized,
    ):
        scores["sku_open_orders"] += 14
    elif entities.get("sku") and hits.get("inventory"):
        scores["sku_inventory"] += 10
    if entities.get("order_id") and not hits.get("write_request"):
        scores["order_status"] += 12

    # Quality family routing — failed detail / lists / auditor beat generic summary.
    qualityish = bool(hits.get("quality")) or bool(
        re.search(
            r"\b(audit|quality|qc|inspection|discrepanc|damage|carton|mistake|auditor)\b",
            normalized,
        )
    )
    if qualityish:
        if re.search(
            r"\b(who audited|which auditor|auditor\b|who inspected|which inspector)\b",
            normalized,
        ):
            scores["quality_auditor"] += 22
            scores["quality_summary"] -= 8
        elif re.search(
            r"\b(show failed|failed audit|list failed|quality history|audits today|today.?s audit|"
            r"list quality|show quality issue|audit history|recent audit)\b",
            normalized,
        ):
            scores["quality_audits_list"] += 20
            scores["quality_summary"] -= 6
            scores["quality_failed_detail"] -= 4
        elif re.search(
            r"\b(any quality|open quality|pass rate|quality summary|qc status|quality\?|quality problem)\b",
            normalized,
        ) or normalized in {"quality", "quality issue", "quailty issue"}:
            scores["quality_summary"] += 18
            scores["quality_failed_detail"] -= 12
        elif re.search(
            r"\b(failed|fail|mistake|discrepanc|carton|crush|damage|which audit|"
            r"why.*(fail|audit)|failed inspection|order failed)\b",
            normalized,
        ) or re.search(
            r"\b(which order).*(quality|inspection|mistake)\b",
            normalized,
        ) or re.search(r"\b(order with the mistake|the mistake)\b", normalized):
            scores["quality_failed_detail"] += 20
            scores["quality_summary"] -= 8
            scores["orders_by_status"] -= 6

    # Standalone follow-up-style questions without prior still map to intents (engine asks for context).
    if re.search(r"\b(who picked( it| that)?|which picker)\b", normalized):
        scores["context_picker"] += 16
        scores["picker_count"] -= 6
    if re.search(r"\b(what sku was involved|which sku was involved|sku involved)\b", normalized):
        scores["context_sku"] += 18
        scores["top_inventory_qty"] -= 10
        scores["sku_inventory"] -= 6
    if re.search(r"\b(was it shipped|did it ship|has it shipped|shipped yet)\b", normalized):
        scores["context_shipped"] += 18
        scores["shipping_today"] -= 10

    if re.search(r"\b(which kpi|kpi needs attention|kpi attention|metric needs)\b", normalized):
        scores["kpi_attention"] += 16
        scores["recommended_actions"] -= 4
    if hits.get("ai_analysis") or re.search(
        r"\b(focus today|summarize performance|biggest risk|vs yesterday|30 minute|thirty minute|"
        r"below target|first look|ai analysis|review first)\b",
        normalized,
    ):
        scores["ai_briefing"] += 16
        if re.search(r"\btop\s+(3|three)\s+actions?\b", normalized) and "recommend" not in normalized:
            scores["ai_briefing"] += 4
    if re.search(r"\b(average|avg|mean)\b", normalized) and re.search(
        r"\b(completion|cycle)\s+time\b", normalized
    ):
        scores["avg_completion_time"] += 20

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
            scores["top_inventory_qty"] += 4

    if hits.get("picking") and re.search(
        r"\b(how many|list|active|roster|configured|do you have|available)\b",
        normalized,
    ) and re.search(r"\bpicker\b", normalized):
        scores["picker_count"] += 12

    # Warehouse network listing/count — do not steal SKU/order/picker/inventory "how many" questions.
    warehouse_networkish = bool(hits.get("warehouse_network")) or bool(
        re.search(r"\b(warehouse|site|facility)\b", normalized)
    )
    count_or_list = bool(
        re.search(
            r"\b(how many|list|number of|configured|do (i|you) have|show (me |my )?the warehouse|show my warehouse)\b",
            normalized,
        )
    ) or bool(re.search(r"\b(which|what)\s+warehouse\b", normalized) and re.search(
        r"\b(do i|do you|have|are there)\b", normalized
    ))
    steals_count = bool(
        re.search(
            r"\b(sku|order|picker|part|unit|shipment|inventory|stock|value|accuracy|shipped|compare)\b",
            normalized,
        )
    )
    summaryish = bool(
        re.search(r"\b(summary|performing|performance|how is|how are we|overview|kpi)\b", normalized)
    )
    if warehouse_networkish and count_or_list and not steals_count and not summaryish:
        scores["warehouse_count"] += 12

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

    # Bare ambiguous words with medium confidence → keep intent but flag clarify later via engine.
    if short_intent and intent == short_intent and len(normalized.split()) <= 2:
        confidence = "high"
        if short_intent == "orders_by_status":
            entities.setdefault("status_hint", "Blocked")

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
