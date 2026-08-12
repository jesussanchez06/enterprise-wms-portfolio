"""Ask WMS orchestration engine: classify → safe query → deterministic response."""

from __future__ import annotations

from html import escape as html_escape
from typing import Any, Callable

import wms_ask_intents as intents
import wms_ask_queries as queries
import wms_ask_recommendations as recommendations


def compose_response(headline: str, bullets: list[str] | None = None, follow_up: str = "", footer: str = "") -> str:
    html = f"<p class='ask-headline'>{html_escape(headline)}</p>"
    clean_bullets = [b for b in (bullets or []) if str(b).strip()]
    if clean_bullets:
        items = "".join(f"<li>{html_escape(str(item))}</li>" for item in clean_bullets[:6])
        html += f"<ul class='ask-bullets'>{items}</ul>"
    if follow_up:
        html += f"<p class='ask-followup'><strong>Next:</strong> {html_escape(follow_up)}</p>"
    if footer:
        html += f"<p class='ask-followup' style='margin-top:8px;opacity:0.85;'>{html_escape(footer)}</p>"
    return html


HELP_CATEGORIES = [
    "Overview / warehouse summary",
    "Orders & shipping (including today)",
    "SLA healthy / at risk / breached",
    "Inventory counts, rankings, SKU availability",
    "Operations / picking backlog",
    "Quality pass rate and open issues",
    "Supervisor recommended actions",
]


def _clarification(intent: str) -> str:
    prompts = {
        "orders_open": "open orders, completed orders, SLA risk, order status, or today's activity",
        "orders_by_status": "Orders Placed, Picking in Progress, Pending Verification, Blocked, or Completed",
        "inventory_summary": "SKU count, inventory value, low stock, top SKUs, or a specific SKU",
        "quality_summary": "pass rate, pending verification, or open quality issues",
        "sla_summary": "breached orders, at-risk orders, or overall SLA summary",
    }
    options = prompts.get(intent, "orders, inventory, SLA, quality, shipping, or recommended actions")
    return compose_response(
        "I can help with that. Which view do you want?",
        [f"Choose one: {options}."],
        "Example: What are the top three recommended actions?",
    )


def answer_question(
    conn,
    question: str,
    *,
    prior_context: dict | None = None,
    warehouses: list[str] | None = None,
    source_warehouse: str = "San Diego Warehouse 100",
    low_stock_threshold: int = 25,
    shortage_issue_type: str = "Inventory Shortage",
    picker_roster: list[str] | None = None,
    now_pt: Callable,
    parse_order_datetime: Callable,
    normalize_urgency: Callable,
    calculate_sla_status: Callable,
    sku_description: Callable[[str], str],
    debug: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (html_answer, snapshot_like_meta, context_for_followups)."""
    classification = intents.classify_intent(question, warehouses=warehouses, prior_context=prior_context)
    intent = classification["intent"]
    confidence = classification["confidence"]
    entities = classification.get("entities") or {}
    now = now_pt()
    footer = "Based on current WMS operational data."
    rec_footer = "Recommendation based on SLA status, inventory availability, workflow stage, and quality exceptions."
    roster = [str(name).strip() for name in (picker_roster or []) if str(name).strip()]

    status_map = queries.count_orders_by_status(conn)
    inv = queries.inventory_snapshot(conn)
    quality = queries.quality_snapshot(conn)
    sla = queries.build_sla_buckets(
        conn,
        parse_order_datetime=parse_order_datetime,
        normalize_urgency=normalize_urgency,
        calculate_sla_status=calculate_sla_status,
        now=now,
    )
    snapshot = {
        "orders_total": sum(status_map.values()),
        "orders_placed": status_map.get("Orders Placed", 0),
        "picking": status_map.get("Picking in Progress", 0),
        "pending_verification": status_map.get("Pending Verification", 0),
        "completed": status_map.get("Completed", 0),
        "blocked": status_map.get("Blocked", 0),
        "quality_issue": status_map.get("Quality Issue", 0),
        "open_quality_issues": quality["open_quality_issues"],
        "sku_count": inv["sku_count"],
        "units_on_hand": inv["units_on_hand"],
        "inventory_value": inv["inventory_value"],
    }

    meta = {
        "intent": intent,
        "confidence": confidence,
        "entities": entities,
        "normalized": classification.get("normalized"),
    }
    if debug:
        meta["debug"] = {
            "score": classification.get("score"),
            "scores": classification.get("scores"),
            "concept_hits": classification.get("concept_hits"),
        }

    context = {
        "intent": intent,
        "intent_family": classification.get("intent_family") or intents.intent_family_for(intent),
        "entities": entities,
        "sku": entities.get("sku"),
        "order_id": entities.get("order_id"),
        "urgency": entities.get("urgency"),
    }

    # Read-only enforcement
    if intent == "write_blocked" or any(
        phrase in (classification.get("normalized") or "")
        for phrase in ("ship order", "add units", "delete order", "create order", "update inventory")
    ):
        answer = compose_response(
            "Ask WMS is read-only and cannot modify warehouse records.",
            [
                "Use Planner to create orders.",
                "Use Inventory to adjust/restock stock.",
                "Use Operations/Quality/Supervisor workflows for picks, audits, and releases.",
            ],
            "Ask for status or recommendations instead.",
            footer="Safety rule: natural language never executes write operations.",
        )
        return answer, snapshot, context

    if confidence == "low" or intent == "unknown":
        answer = compose_response(
            "I can answer operational warehouse questions from this demo session.",
            HELP_CATEGORIES,
            "Try: What are the top three recommended actions?",
            footer,
        )
        return answer, snapshot, context

    if confidence == "medium" and intent in {
        "orders_open",
        "orders_by_status",
        "inventory_summary",
        "quality_summary",
        "sla_summary",
    }:
        return _clarification(intent), snapshot, context

    limit = int(entities.get("limit") or 3)

    if intent == "recommended_actions" or (
        intent == "supervisor_summary" and "action" in (classification.get("normalized") or "")
    ):
        actions = recommendations.build_recommendations(
            conn,
            limit=limit,
            parse_order_datetime=parse_order_datetime,
            normalize_urgency=normalize_urgency,
            calculate_sla_status=calculate_sla_status,
            now=now,
            low_stock_threshold=low_stock_threshold,
            shortage_issue_type=shortage_issue_type,
        )
        bullets = [f"{idx}. {item['title']} — {item['why']}" for idx, item in enumerate(actions, start=1)]
        if len(actions) == 1 and str(actions[0].get("title", "")).startswith("No urgent"):
            headline = "No urgent interventions detected right now."
        else:
            headline = f"Here are the top {len(actions)} recommended action(s) right now."
        answer = compose_response(
            headline,
            bullets,
            "Open Supervisor or Operations to execute the first priority.",
            rec_footer,
        )
        context["intent"] = "recommended_actions"
        return answer, snapshot, context

    if intent == "shipping_today":
        day = entities.get("date") or now.date()
        label = entities.get("date_label") or "today"
        count, sample = queries.count_shipped_on_date(conn, day)
        bullets = [f"Filter: status=Completed, date={label}"]
        if sample:
            bullets.append("Examples: " + ", ".join(sample))
        answer = compose_response(
            f"{count} order(s) shipped/completed on {label}.",
            bullets,
            "Ask which critical orders still have not shipped.",
            footer,
        )
        return answer, snapshot, context

    if intent == "warehouse_summary":
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        answer = compose_response(
            (
                f"Warehouse snapshot: {open_orders} open order(s), {snapshot['completed']} completed, "
                f"{snapshot['blocked']} blocked, inventory value ${snapshot['inventory_value']:,.2f}."
            ),
            [
                f"Pipeline: {snapshot['orders_placed']} placed, {snapshot['picking']} picking, {snapshot['pending_verification']} pending QA.",
                f"SLA on open work: {sla['healthy_count']} healthy, {sla['at_risk_count']} at risk, {sla['breached_count']} breached.",
                f"Quality: pass rate {quality['pass_rate']}% with {quality['open_quality_issues']} open issue(s).",
                f"Inventory: {snapshot['sku_count']} active SKUs / {snapshot['units_on_hand']:,} units.",
            ],
            "Ask for the top three recommended actions.",
            footer,
        )
        return answer, snapshot, context

    if intent == "orders_open":
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        urgency = entities.get("urgency")
        if urgency:
            rows = [r for r in queries.list_orders(conn, urgency=urgency, limit=50) if r["status"] != "Completed"]
            answer = compose_response(
                f"{len(rows)} {urgency.lower()} open order(s).",
                [f"{r['order_number']} — {r['status']}" for r in rows[:8]] or ["None found."],
                "Ask which ones are at SLA risk.",
                footer,
            )
            context["intent"] = "critical_orders" if urgency == "Critical" else "orders_open"
            context["entities"] = {"urgency": urgency, "order_ids": [r["order_number"] for r in rows]}
            return answer, snapshot, context
        answer = compose_response(
            f"{open_orders} order(s) are open (not completed).",
            [
                f"Orders Placed: {snapshot['orders_placed']}",
                f"Picking in Progress: {snapshot['picking']}",
                f"Pending Verification: {snapshot['pending_verification']}",
                f"Blocked: {snapshot['blocked']}",
                f"Quality Issue: {snapshot['quality_issue']}",
            ],
            "Ask for blocked orders or SLA risk.",
            footer,
        )
        return answer, snapshot, context

    if intent == "critical_orders":
        rows = [r for r in queries.list_orders(conn, urgency="Critical", limit=50) if r["status"] != "Completed"]
        answer = compose_response(
            f"{len(rows)} critical open order(s).",
            [f"{r['order_number']} — {r['status']}" for r in rows[:10]] or ["No critical open orders."],
            "Ask which critical orders are at SLA risk.",
            footer,
        )
        context["entities"] = {"urgency": "Critical", "order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "orders_by_status":
        normalized = classification.get("normalized") or ""
        status = None
        if "blocked" in normalized:
            status = "Blocked"
        elif "pending verification" in normalized or "waiting for qc" in normalized or "quality" in normalized:
            status = "Pending Verification"
        elif "picking" in normalized or "being picked" in normalized:
            status = "Picking in Progress"
        elif "placed" in normalized or "ready to pick" in normalized or "waiting to be picked" in normalized:
            status = "Orders Placed"
        elif "completed" in normalized or "ready to ship" in normalized:
            status = "Completed"
        if not status:
            return _clarification("orders_by_status"), snapshot, context
        rows = queries.list_orders(conn, status=status, limit=12)
        answer = compose_response(
            f"{len(rows) if len(rows) < 12 else status_map.get(status, len(rows))} order(s) currently in {status}.",
            [f"{r['order_number']} ({r['urgency']})" for r in rows] or ["None found."],
            "Ask for recommended actions on this queue.",
            footer,
        )
        context["entities"] = {"status": status, "order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "order_status":
        order_id = entities.get("order_id")
        detail = queries.get_order_detail(conn, str(order_id)) if order_id else None
        if not detail:
            answer = compose_response(
                f"No order matching '{order_id}' was found in this demo session.",
                [],
                "Ask how many orders are open.",
                footer,
            )
            return answer, snapshot, context
        lines = [f"{line['sku']}: {line['quantity']} units" for line in detail["lines"][:8]]
        answer = compose_response(
            (
                f"Order {detail['order_number']} is {detail['status']} "
                f"(urgency {detail['urgency']}, owner {detail['responsibility']})."
            ),
            [
                f"Created: {detail['date']}",
                f"Route: {detail['source']} → {detail['destination']}",
                f"Picked {detail['picked_quantity']} of {detail['expected_quantity']} expected units",
                *([f"Lines: {', '.join(lines)}"] if lines else []),
            ],
            "Ask whether this order is at SLA risk.",
            footer,
        )
        context["order_id"] = detail["order_number"]
        return answer, snapshot, context

    if intent == "sla_summary":
        total_open = sla["healthy_count"] + sla["at_risk_count"] + sla["breached_count"]
        answer = compose_response(
            (
                f"SLA on {total_open} open order(s): {sla['healthy_count']} healthy, "
                f"{sla['at_risk_count']} at risk, {sla['breached_count']} breached."
            ),
            [
                "Critical SLA window: 2 business hours.",
                "Urgent SLA window: 4 business hours.",
                "Standard SLA window: 2 business days.",
            ],
            "Ask which orders breached SLA.",
            footer,
        )
        return answer, snapshot, context

    if intent == "sla_breached":
        rows = sla["breached"]
        answer = compose_response(
            f"{len(rows)} order(s) currently breached SLA.",
            [f"{r['order_number']} ({r['urgency']}, {r['status']})" for r in rows[:10]] or ["None found."],
            "Ask for recommended actions.",
            footer,
        )
        context["entities"] = {"order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "sla_at_risk":
        rows = sla["at_risk"]
        urgency = entities.get("urgency")
        if urgency:
            rows = [r for r in rows if r.get("urgency") == urgency]
        answer = compose_response(
            f"{len(rows)} order(s) are at SLA risk"
            + (f" for {urgency} urgency." if urgency else "."),
            [f"{r['order_number']} ({r['urgency']}, {r['status']})" for r in rows[:10]] or ["None found."],
            "Ask for the top recommended actions.",
            footer,
        )
        context["entities"] = {"order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "sku_count":
        answer = compose_response(
            f"There are {inv['sku_count']} active SKUs available in this demo warehouse.",
            [
                f"Total units on hand: {inv['units_on_hand']:,}",
                f"Inventory value: ${inv['inventory_value']:,.2f}",
            ],
            "Ask which part number has the most inventory.",
            footer,
        )
        return answer, snapshot, context

    if intent in {"top_inventory_qty", "least_inventory_qty", "top_inventory_value"}:
        mode = {
            "top_inventory_qty": "most_qty",
            "least_inventory_qty": "least_qty",
            "top_inventory_value": "most_value",
        }[intent]
        rows = queries.rank_skus(conn, mode=mode, limit=max(limit, 5))
        if not rows:
            return compose_response("No positive on-hand inventory was found.", [], footer=footer), snapshot, context
        lead = rows[0]
        if mode == "least_qty":
            headline = f"SKU {lead['sku']} has the least inventory with {lead['qty']:,} unit(s)."
        elif mode == "most_value":
            headline = f"SKU {lead['sku']} has the highest inventory value at ${lead['value']:,.2f}."
        else:
            headline = f"SKU {lead['sku']} has the most inventory with {lead['qty']:,} unit(s)."
        show_n = limit if limit > 1 else min(4, len(rows))
        bullets = [f"{lead['sku']}: {sku_description(lead['sku'])}"]
        if mode == "most_value":
            bullets.extend(
                f"#{idx}. SKU {row['sku']}: ${row['value']:,.2f} ({row['qty']:,} units)"
                for idx, row in enumerate(rows[:show_n], start=1)
            )
        else:
            bullets.extend(
                f"#{idx}. SKU {row['sku']}: {row['qty']:,} units"
                for idx, row in enumerate(rows[:show_n], start=1)
            )
        answer = compose_response(headline, bullets, "Ask for low-stock SKUs.", footer)
        return answer, snapshot, context

    if intent == "sku_inventory":
        sku = str(entities.get("sku") or "")
        data = queries.sku_availability(conn, sku, source_warehouse)
        bullets = [
            f"Source warehouse ({source_warehouse}): {data['source_qty']:,} unit(s)",
            f"Network on-hand: {data['network_qty']:,} unit(s)",
            f"Description: {sku_description(sku)}",
        ]
        if data["locations"]:
            bullets.append(
                "Locations: "
                + ", ".join(
                    f"{row['warehouse']}/{row['location']}={row['quantity']}"
                    for row in data["locations"][:4]
                )
            )
        answer = compose_response(
            f"SKU {sku} has {data['source_qty']:,} unit(s) available at {source_warehouse}.",
            bullets,
            "Ask if any open orders are using this SKU.",
            footer,
        )
        context["sku"] = sku
        return answer, snapshot, context

    if intent == "sku_open_orders":
        sku = str(entities.get("sku") or (prior_context or {}).get("sku") or "")
        rows = queries.sku_open_order_demand(conn, sku) if sku else []
        answer = compose_response(
            f"{len(rows)} open order(s) currently request SKU {sku}.",
            [f"{r['order_number']}: {r['quantity']} units ({r['status']}, {r['urgency']})" for r in rows]
            or ["No open demand found for this SKU."],
            "Ask whether available inventory can cover that demand.",
            footer,
        )
        context["sku"] = sku
        return answer, snapshot, context

    if intent == "low_stock":
        rows = queries.low_stock_skus(conn, threshold=low_stock_threshold, limit=8)
        answer = compose_response(
            f"{len(rows)} SKU(s) are below the {low_stock_threshold}-unit low-stock threshold.",
            [f"SKU {r['sku']}: {r['qty']} units" for r in rows] or ["No low-stock SKUs right now."],
            "Ask for inventory value by warehouse.",
            footer,
        )
        return answer, snapshot, context

    if intent == "inventory_value":
        rows = queries.inventory_by_warehouse(conn)
        answer = compose_response(
            f"Total inventory value is ${inv['inventory_value']:,.2f}.",
            [f"{r['warehouse']}: ${r['value']:,.2f} ({r['units']:,} units)" for r in rows],
            "Ask which SKU has the highest inventory value.",
            footer,
        )
        return answer, snapshot, context

    if intent == "inventory_accuracy":
        acc = queries.inventory_accuracy(conn)
        answer = compose_response(
            f"Inventory accuracy is {acc['accuracy_pct']}% "
            f"({acc['valid_skus']} of {acc['total_skus']} active SKUs fully located).",
            [
                "Accuracy here means positive-qty SKUs with valid warehouse and location assignment.",
            ],
            "Ask for low-stock SKUs.",
            footer,
        )
        return answer, snapshot, context

    if intent == "inventory_summary":
        answer = compose_response(
            (
                f"Inventory summary: {inv['sku_count']} active SKUs, "
                f"{inv['units_on_hand']:,} units, value ${inv['inventory_value']:,.2f}."
            ),
            [
                f"Low-stock threshold: {low_stock_threshold} units",
                f"Inventory accuracy: {queries.inventory_accuracy(conn)['accuracy_pct']}%",
            ],
            "Ask which part number has the most inventory.",
            footer,
        )
        return answer, snapshot, context

    if intent == "picker_count":
        pickers = queries.picker_workload(conn)
        workload = {p["picker"]: p["picks"] for p in pickers}
        if roster:
            bullets = [
                f"{name}"
                + (f" - {workload[name]} pick event(s)" if name in workload else " - no pick events yet")
                for name in roster
            ]
            answer = compose_response(
                f"This demo has {len(roster)} configured picker(s) on the operations roster.",
                bullets,
                "Ask about picking backlog or recommended actions for the next picker.",
                footer,
            )
        else:
            answer = compose_response(
                "No picker roster is configured in this session.",
                [f"{p['picker']}: {p['picks']} pick event(s)" for p in pickers]
                or ["No pick transactions recorded yet."],
                "Ask about picking backlog or ready-to-pick orders.",
                footer,
            )
        return answer, snapshot, context

    if intent == "picking_summary":
        pickers = queries.picker_workload(conn)
        bullets = [f"{p['picker']}: {p['picks']} pick event(s)" for p in pickers]
        if not bullets and roster:
            bullets = [
                f"Configured roster ({len(roster)}): " + ", ".join(roster[:6])
                + ("…" if len(roster) > 6 else ""),
                "No pick transactions recorded yet in this demo session.",
            ]
        elif not bullets:
            bullets = ["No pick transactions recorded yet in this demo session."]
        answer = compose_response(
            (
                f"Operations queue: {snapshot['orders_placed']} ready/placed, "
                f"{snapshot['picking']} picking in progress, {snapshot['blocked']} blocked."
            ),
            bullets,
            "Ask how many pickers are configured, or what the next picker should work on.",
            footer,
        )
        return answer, snapshot, context

    if intent == "quality_summary":
        issues = queries.open_quality_issues(conn, limit=8)
        answer = compose_response(
            (
                f"Quality summary: pass rate {quality['pass_rate']}% "
                f"({quality['audits_passed']}/{quality['audits_total']} audits), "
                f"{quality['pending_verification']} pending verification, "
                f"{quality['open_quality_issues']} open quality case(s)."
            ),
            [f"{i['issue_id']} on {i['order_number']} ({i['issue_type']})" for i in issues]
            or ["No open quality/supervisor issues."],
            "Ask for recommended supervisor actions.",
            footer,
        )
        return answer, snapshot, context

    if intent == "supervisor_summary":
        actions = recommendations.build_recommendations(
            conn,
            limit=3,
            parse_order_datetime=parse_order_datetime,
            normalize_urgency=normalize_urgency,
            calculate_sla_status=calculate_sla_status,
            now=now,
            low_stock_threshold=low_stock_threshold,
            shortage_issue_type=shortage_issue_type,
        )
        answer = compose_response(
            (
                f"Control tower: {snapshot['blocked']} blocked, {sla['at_risk_count']} at risk, "
                f"{sla['breached_count']} breached, {quality['open_quality_issues']} open quality cases."
            ),
            [f"{idx}. {item['title']}" for idx, item in enumerate(actions, start=1)],
            "Ask for the top three recommended actions with reasons.",
            rec_footer,
        )
        return answer, snapshot, context

    if intent == "planner_summary":
        mix = queries.planner_mix(conn)
        answer = compose_response(
            f"Planner view: {snapshot['orders_total']} total order(s), {snapshot['orders_placed']} currently placed.",
            [
                "Destinations: "
                + (
                    ", ".join(f"{d['destination']}: {d['count']}" for d in mix["destinations"][:4])
                    or "none"
                ),
                "Urgency: "
                + (", ".join(f"{u['urgency']}: {u['count']}" for u in mix["urgency"][:4]) or "none"),
            ],
            "Ask whether a SKU can support a requested quantity.",
            footer,
        )
        return answer, snapshot, context

    if intent == "help_explain":
        normalized = classification.get("normalized") or ""
        if "workflow" in normalized or "planner to shipping" in normalized:
            answer = compose_response(
                "Orders move Planner -> Operations pick -> Quality verification -> Completed/shipped, with Supervisor handling escalations.",
                [
                    "Planner creates demand against source-warehouse inventory.",
                    "Operations picks and can block on shortage.",
                    "Quality verifies and may escalate discrepancies.",
                    "Executive dashboard monitors SLA, productivity, and inventory KPIs.",
                ],
                "Ask what the Supervisor Control Tower is for.",
                footer="Application documentation (not a live metric query).",
            )
        elif "sla" in normalized:
            answer = compose_response(
                "SLA states are Healthy, At Risk, and Breached based on urgency windows already configured in this WMS.",
                [
                    "Critical: 2 business hours.",
                    "Urgent: 4 business hours.",
                    "Standard: 2 business days.",
                ],
                "Ask for the current SLA summary.",
                footer="Application documentation + live SLA engine rules.",
            )
        elif "reset" in normalized:
            answer = compose_response(
                "Use Reset Demo in the top navigation to restore this visitor's private demo warehouse to the shared sample baseline.",
                ["This clears that visitor's orders/picks/adjustments and Ask WMS chat for the session."],
                "Ask how the warehouse is performing today.",
                footer="Application documentation.",
            )
        else:
            answer = compose_response(
                "Ask WMS answers live operational questions from this visitor's demo database without a paid AI API.",
                HELP_CATEGORIES,
                "Try: How is the warehouse performing today?",
                footer="Ask WMS uses intent recognition, controlled queries, and deterministic rules.",
            )
        return answer, snapshot, context

    # Fallback
    answer = compose_response(
        "I can answer that area, but need a bit more detail.",
        HELP_CATEGORIES,
        "Try: What needs supervisor attention right now?",
        footer,
    )
    return answer, snapshot, context
