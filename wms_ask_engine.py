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
    "Overview / warehouse summary / remaining work / risks / KPI attention",
    "Orders & shipping (open, created today, oldest, completed, urgent mix)",
    "SLA healthy / at risk / breached + OTIF when measurable",
    "Inventory counts, value, rankings, adjustments, SKU lookup",
    "Operations / picking backlog (productivity limits explained)",
    "Quality: failed audits, discrepancy reasons, auditors, history, pass rate",
    "Follow-ups after a quality order: who picked / which SKU / was it shipped",
    "Supervisor recommended actions / bottleneck signals / AI briefing",
    "KPI definitions and DigiTech WMS / Ask WMS help",
]

KPI_DEFINITIONS = [
    "SLA Healthy / At Risk / Breached — time left vs urgency window (Critical 2h, Urgent 4h, Standard 2 business days).",
    "OTIF — Completed orders finished on/before SLA deadline AND picked_quantity >= expected_quantity (needs completion timestamps).",
    "Inventory accuracy — share of positive-qty SKUs with valid warehouse + location.",
    "Quality audit pass rate — Passed audits ÷ total quality audits × 100 (not a separate pick-accuracy metric).",
    "Low stock — SKUs below the configured unit threshold (demo default 25).",
]


def _clarification(intent: str) -> str:
    prompts = {
        "orders_open": "open orders, completed orders, SLA risk, order status, or today's activity",
        "orders_by_status": "Orders Placed, Picking in Progress, Pending Verification, Blocked, or Completed",
        "inventory_summary": "SKU count, inventory value, low stock, top SKUs, or a specific SKU",
        "quality_summary": "pass rate, pending verification, or open quality issues",
        "sla_summary": "breached orders, at-risk orders, healthy orders, or overall SLA summary",
        "shipping_summary": "shipped today, ready/pending verification, or completed count",
        "warehouse_summary": "executive snapshot, remaining work, recommended actions, or warehouse compare",
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
    calculate_sla_deadline: Callable | None = None,
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
        "picker": entities.get("picker") or (prior_context or {}).get("picker"),
    }

    def _attach_quality_context(order_number: str | None) -> None:
        if not order_number:
            return
        bundle = queries.quality_context_bundle(conn, order_number)
        context["order_id"] = bundle.get("order_id") or order_number
        if bundle.get("sku"):
            context["sku"] = bundle["sku"]
        if bundle.get("picker"):
            context["picker"] = bundle["picker"]
        merged = dict(context.get("entities") or {})
        merged["order_id"] = context["order_id"]
        if context.get("sku"):
            merged["sku"] = context["sku"]
        if context.get("picker"):
            merged["picker"] = context["picker"]
        if bundle.get("skus"):
            merged["skus"] = bundle["skus"]
        context["entities"] = merged

    # Read-only enforcement
    if intent == "write_blocked" or any(
        phrase in (classification.get("normalized") or "")
        for phrase in ("ship order", "add units", "delete order", "create order", "update inventory")
    ):
        answer = compose_response(
            "Ask WMS is read-only and cannot modify warehouse records.",
            [
                "Use Order Planning to create orders.",
                "Use Inventory to adjust/restock stock.",
                "Use Operations/Quality/Supervisor workflows for picks, audits, and releases.",
            ],
            "Ask for status or recommendations instead.",
            footer="Safety rule: natural language never executes write operations.",
        )
        return answer, snapshot, context

    if intent == "unsupported_predictive":
        answer = compose_response(
            "Ask WMS does not forecast demand or recommend HR actions (hiring/firing).",
            [
                "Supported: live orders, SLA, inventory, quality, shipping, and deterministic priorities.",
                "Missing for predictions: future demand models, labor plans, and external signals.",
            ],
            "Ask for remaining work, SLA risk, or recommended actions instead.",
            footer="Unsupported predictive / personnel request.",
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
        "shipping_summary",
        "warehouse_summary",
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

    if intent == "warehouse_count":
        network = [str(name).strip() for name in (warehouses or []) if str(name).strip()]
        if not network:
            # Fall back to warehouses that currently hold inventory in this session.
            network = [
                str(r["warehouse"]).strip()
                for r in queries.inventory_by_warehouse(conn)
                if str(r.get("warehouse") or "").strip()
            ]
        if network:
            named = ", ".join(network)
            answer = compose_response(
                f"You have {len(network)} warehouse(s): {named}.",
                [
                    "These sites are the configured WMS warehouse network for this demo.",
                ],
                "Ask for inventory value by warehouse.",
                footer,
            )
        else:
            answer = compose_response(
                "No warehouses are configured in this demo session.",
                [],
                "Ask for a warehouse summary or inventory value.",
                footer,
            )
        return answer, snapshot, context

    if intent in {"warehouse_summary", "kpi_snapshot"}:
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        accuracy = queries.inventory_accuracy(conn)
        shipped_today, _ = queries.count_shipped_on_date(conn, now.date())
        created_today = int(queries.count_orders_created_on_date(conn, now.date()) or 0)
        headline = (
            f"Current DigiTech WMS KPIs: {snapshot['orders_total']} total order(s), "
            f"{open_orders} pending/open, {snapshot['completed']} completed."
            if intent == "kpi_snapshot"
            else (
                f"Warehouse snapshot: {open_orders} open order(s), {snapshot['completed']} completed, "
                f"{snapshot['blocked']} blocked, inventory value ${snapshot['inventory_value']:,.2f}."
            )
        )
        bullets = [
            f"Orders received/created today: {created_today}",
            f"Orders shipped/completed today: {shipped_today}",
            f"Orders pending (not completed): {open_orders}",
            f"Quality audit pass rate: {quality['pass_rate']}% "
            f"({quality['audits_passed']} passed of {quality['audits_total']} audits)",
            f"Inventory accuracy: {accuracy['accuracy_pct']}% "
            f"({accuracy['valid_skus']} of {accuracy['total_skus']} SKUs fully located)",
            f"Total inventory value: ${snapshot['inventory_value']:,.2f} across {snapshot['sku_count']} SKUs",
            f"SLA on open work: {sla['healthy_count']} healthy, {sla['at_risk_count']} at risk, "
            f"{sla['breached_count']} breached",
            f"Pipeline: {snapshot['orders_placed']} placed, {snapshot['picking']} picking, "
            f"{snapshot['pending_verification']} pending QA, {snapshot['blocked']} blocked",
        ]
        answer = compose_response(
            headline,
            bullets,
            "Ask for OTIF, top recommended actions, or a specific KPI definition.",
            footer,
        )
        return answer, snapshot, context

    if intent == "remaining_work":
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        answer = compose_response(
            f"{open_orders} order(s) remain open across the network.",
            [
                f"Ready/placed: {snapshot['orders_placed']}",
                f"Picking: {snapshot['picking']}",
                f"Pending verification: {snapshot['pending_verification']}",
                f"Blocked: {snapshot['blocked']} | Quality Issue: {snapshot['quality_issue']}",
                f"SLA pressure: {sla['at_risk_count']} at risk, {sla['breached_count']} breached.",
            ],
            "Ask for the oldest open orders or recommended actions.",
            footer,
        )
        return answer, snapshot, context

    if intent == "warehouse_compare":
        rows = queries.inventory_by_warehouse(conn)
        network = [str(name).strip() for name in (warehouses or []) if str(name).strip()]
        bullets = [f"{r['warehouse']}: {r['units']:,} units / ${r['value']:,.2f}" for r in rows]
        if network and len(rows) < len(network):
            present = {r["warehouse"] for r in rows}
            missing = [w for w in network if w not in present]
            if missing:
                bullets.append("No positive inventory currently at: " + ", ".join(missing))
        answer = compose_response(
            f"Warehouse comparison across {len(rows) or len(network)} site(s) with on-hand stock.",
            bullets or ["No inventory rows found for comparison."],
            "Ask for low-stock SKUs or inventory value totals.",
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

    if intent == "orders_created_today":
        day = entities.get("date") or now.date()
        label = entities.get("date_label") or "today"
        count = queries.count_received_on_date(conn, day)
        answer = compose_response(
            f"{count} order(s) were created/received on {label}.",
            [
                f"Filter: order_header.date on {label}",
                f"Open now (all dates): {max(snapshot['orders_total'] - snapshot['completed'], 0)}",
            ],
            "Ask how many shipped today.",
            footer,
        )
        return answer, snapshot, context

    if intent == "orders_completed":
        answer = compose_response(
            f"{snapshot['completed']} order(s) are Completed in this demo session.",
            [
                f"Still open: {max(snapshot['orders_total'] - snapshot['completed'], 0)}",
                f"Pending verification (pre-ship): {snapshot['pending_verification']}",
            ],
            "Ask how many shipped today or for OTIF.",
            footer,
        )
        return answer, snapshot, context

    if intent == "orders_oldest":
        rows = queries.list_oldest_open_orders(conn, limit=max(limit, 8))
        answer = compose_response(
            f"Oldest open order backlog ({len(rows)} shown).",
            [f"{r['order_number']} — {r['status']} / {r['urgency']} (created {r['date']})" for r in rows]
            or ["No open orders."],
            "Ask which of these breached SLA.",
            footer,
        )
        context["entities"] = {"order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "orders_by_status":
        normalized = classification.get("normalized") or ""
        status = entities.get("status_hint")
        if not status:
            if "blocked" in normalized or "stuck" in normalized or "shortage" in normalized:
                status = "Blocked"
            elif "pending verification" in normalized or "waiting for qc" in normalized or (
                "quality" in normalized and "issue" not in normalized
            ):
                status = "Pending Verification"
            elif "quality issue" in normalized:
                status = "Quality Issue"
            elif "partial" in normalized or "being picked" in normalized or "picking" in normalized:
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
        sla_note = "SLA n/a (completed)"
        if detail["status"] != "Completed":
            try:
                order_time = parse_order_datetime(detail["date"])
                sla_key, sla_label = calculate_sla_status(
                    order_time, normalize_urgency(detail["urgency"], "Standard"), now
                )
                sla_note = f"SLA: {sla_label} ({sla_key})"
            except Exception:
                sla_note = "SLA: unable to evaluate from order date"
        answer = compose_response(
            (
                f"Order {detail['order_number']} is {detail['status']} "
                f"(urgency {detail['urgency']}, owner {detail['responsibility']})."
            ),
            [
                f"Created: {detail['date']}",
                f"Route: {detail['source']} → {detail['destination']}",
                f"Picked {detail['picked_quantity']} of {detail['expected_quantity']} expected units",
                sla_note,
                *([f"Lines: {', '.join(lines)}"] if lines else []),
            ],
            "Ask whether this order is at SLA risk, or for recommended actions.",
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
        bullets = [
            f"{r['order_number']} ({r['urgency']}, {r['status']}) — {queries.sla_breach_cause_hint(r['status'])}"
            for r in rows[:10]
        ] or ["None found."]
        answer = compose_response(
            f"{len(rows)} order(s) currently breached SLA.",
            bullets,
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

    if intent == "sla_healthy":
        rows = sla["healthy"]
        answer = compose_response(
            f"{len(rows)} open order(s) are currently SLA healthy.",
            [f"{r['order_number']} ({r['urgency']}, {r['status']})" for r in rows[:10]] or ["None found."],
            "Ask which orders are at risk.",
            footer,
        )
        context["entities"] = {"order_ids": [r["order_number"] for r in rows]}
        return answer, snapshot, context

    if intent == "otif_summary":
        if calculate_sla_deadline is None:
            answer = compose_response(
                "OTIF cannot be computed in this session wiring.",
                ["Need SLA deadline calculation plus completion timestamps for Completed orders."],
                "Ask for SLA summary or completed order counts instead.",
                footer="Unsupported OTIF metric in current configuration.",
            )
            return answer, snapshot, context
        otif = queries.otif_snapshot(
            conn,
            parse_order_datetime=parse_order_datetime,
            normalize_urgency=normalize_urgency,
            calculate_sla_deadline=calculate_sla_deadline,
        )
        if not otif["supported"]:
            answer = compose_response(
                "OTIF is only partially supported here — no Completed orders had recoverable completion timestamps.",
                [
                    otif["definition"],
                    f"Completed orders in session: {otif['completed_total']}",
                    f"Missing completion timestamps: {otif['missing_completion']}",
                    "Completion time is inferred from quality Pass audit_time or PICK/PICK_CLOSE transactions.",
                ],
                "Ask for SLA on open orders or shipped-today counts instead.",
                footer="Honest OTIF gap: completion timestamps are not stored on order_header.",
            )
            return answer, snapshot, context
        bullets = [
            otif["definition"],
            f"Measured Completed orders: {otif['measured']} (missing timestamps: {otif['missing_completion']})",
            f"On-time: {otif['on_time_count']} | In-full: {otif['in_full_count']} | OTIF: {otif['otif_count']}",
        ]
        for miss in otif["samples_missed"][:4]:
            flags = []
            if not miss["on_time"]:
                flags.append("late")
            if not miss["in_full"]:
                flags.append("short")
            bullets.append(f"Miss example: {miss['order_number']} ({', '.join(flags) or 'flagged'})")
        answer = compose_response(
            f"OTIF on measured Completed orders is {otif['otif_pct']}%.",
            bullets,
            "Ask for breached SLA on open work.",
            footer,
        )
        return answer, snapshot, context

    if intent == "shipping_summary":
        day = entities.get("date") or now.date()
        label = entities.get("date_label") or "today"
        shipped_today, sample = queries.count_shipped_on_date(conn, day)
        answer = compose_response(
            (
                f"Shipping pulse: {shipped_today} completed on {label}, "
                f"{snapshot['pending_verification']} pending verification, "
                f"{snapshot['completed']} completed total."
            ),
            [
                f"Ready-ish pre-ship queue = Pending Verification ({snapshot['pending_verification']})",
                f"Blocked from shipping: {snapshot['blocked']} blocked + {snapshot['quality_issue']} quality issue",
                *(["Examples completed: " + ", ".join(sample)] if sample else []),
            ],
            "Ask how many shipped today or for quality issues delaying shipping.",
            footer,
        )
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

    if intent == "inventory_adjustments":
        sku = entities.get("sku") or (prior_context or {}).get("sku")
        rows = queries.inventory_adjustments(conn, limit=10, sku=str(sku) if sku else None)
        if not rows:
            answer = compose_response(
                "No ADJUST inventory transactions were found"
                + (f" for SKU {sku}." if sku else " in this demo session."),
                [
                    "Adjustments appear when Inventory corrections are posted (tx_code=ADJUST).",
                    "This answer never invents adjustment history.",
                ],
                "Ask for inventory accuracy or low-stock SKUs.",
                footer,
            )
            return answer, snapshot, context
        answer = compose_response(
            f"{len(rows)} recent inventory adjustment(s)"
            + (f" for SKU {sku}." if sku else "."),
            [
                f"{r['tx_time']}: SKU {r['sku']} {r['qty_change']:+d} @ {r['warehouse']}/{r['location']}"
                for r in rows[:8]
            ],
            "Ask for on-hand quantity of a SKU.",
            footer,
        )
        if sku:
            context["sku"] = sku
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

    if intent == "productivity_limits":
        pickers = queries.picker_workload(conn)
        shipped_today, _ = queries.count_shipped_on_date(conn, now.date())
        from datetime import timedelta as _td

        shipped_yesterday, _ = queries.count_shipped_on_date(conn, now.date() - _td(days=1))
        mix = queries.planner_mix(conn)
        bullets = [
            "Units/hour and picks/hour are not fully supported — no shift-level time-on-task clock.",
            f"Configured roster size: {len(roster) if roster else 0}",
            f"Completed volume today: {shipped_today} | yesterday: {shipped_yesterday}",
            "Fill rate proxy: Completed picked_quantity vs expected (see OTIF in-full component).",
            "Urgency/workload mix: "
            + (", ".join(f"{u['urgency']}={u['count']}" for u in mix["urgency"][:4]) or "none"),
        ]
        if pickers:
            bullets.append(
                "Observed pick events: "
                + ", ".join(f"{p['picker']}={p['picks']}" for p in pickers[:5])
            )
        answer = compose_response(
            "Productivity: live volume and pick events are available; rate metrics have honest limits.",
            bullets,
            "Ask for OTIF, picking backlog, or average completion time.",
            footer="Honest limit: no shift-level productivity clock in schema.",
        )
        return answer, snapshot, context

    if intent == "avg_completion_time":
        avg = queries.average_completion_minutes(conn, parse_order_datetime=parse_order_datetime)
        if not avg["supported"]:
            answer = compose_response(
                "Average completion time is not available — no Completed orders had recoverable timestamps.",
                [
                    "Completion time is inferred from Pass audit_time or PICK/PICK_CLOSE transactions.",
                    f"Missing completion timestamps: {avg['missing']}",
                ],
                "Ask for OTIF or shipped-today counts instead.",
                footer="Honest gap when completion timestamps cannot be recovered.",
            )
            return answer, snapshot, context
        answer = compose_response(
            (
                f"Average create→completion time is about {avg['avg_hours']} hour(s) "
                f"({avg['avg_minutes']} minutes) across {avg['measured']} measured Completed order(s)."
            ),
            [
                f"Orders missing recoverable completion timestamps: {avg['missing']}",
                "Derived from order_header.date → Pass audit or pick-close time (not a dedicated cycle-time column).",
            ],
            "Ask for OTIF or longest open orders.",
            footer,
        )
        return answer, snapshot, context

    if intent == "quality_failed_detail":
        order_id = entities.get("order_id") or (prior_context or {}).get("order_id")
        failed = queries.primary_failed_audit(conn, order_id=str(order_id) if order_id else None)
        if not failed:
            # Fall back to open Quality Issue status orders if no Failed audit rows.
            qi_orders = queries.list_orders(conn, status="Quality Issue", limit=8)
            if qi_orders:
                lead = qi_orders[0]
                _attach_quality_context(lead["order_number"])
                answer = compose_response(
                    f"Order {lead['order_number']} is currently in Quality Issue status.",
                    [f"{r['order_number']} ({r['urgency']})" for r in qi_orders],
                    "Ask who picked it, what SKU was involved, or whether it shipped.",
                    footer,
                )
                return answer, snapshot, context
            answer = compose_response(
                "No failed quality audits were found in this demo session.",
                ["Ask WMS never invents discrepancies — only quality_audits / supervisor_quality_issues rows."],
                "Ask for the quality pass rate or today's audits.",
                footer,
            )
            return answer, snapshot, context

        note = failed.get("note") or failed.get("discrepancy_type") or "quality discrepancy"
        issue_status = failed.get("issue_status") or "Quality Issue"
        dtype = failed.get("discrepancy_type") or failed.get("issue_type") or "Quality Issue"
        # Recruiter-facing narrative (real seed: carton corner crush on ORD-DT82-0081).
        headline = (
            f"Order {failed['order_number']} failed quality verification due to {note}"
            f"{'' if note.endswith('.') else '.'} "
            f"Audit recorded as Quality Issue"
            + (f" ({issue_status})." if issue_status else ".")
        )
        bullets = [
            f"Failed audit: {failed['audit_id']} at {failed['audit_time']}",
            f"Discrepancy type: {dtype}",
            f"Auditor role: {failed.get('auditor_role') or 'Quality'}",
            f"Order status now: {failed.get('order_status') or 'n/a'}",
        ]
        if failed.get("picker_name"):
            bullets.append(f"Picker on issue record: {failed['picker_name']}")
        if failed.get("part_snapshot"):
            bullets.append(f"Part snapshot: {failed['part_snapshot']}")
        _attach_quality_context(failed["order_number"])
        answer = compose_response(
            headline,
            bullets,
            "Ask who picked it, what SKU was involved, or whether it shipped.",
            footer,
        )
        return answer, snapshot, context

    if intent == "quality_audits_list":
        normalized = classification.get("normalized") or ""
        day = entities.get("date")
        label = entities.get("date_label")
        if day is not None and (
            "today" in normalized or "audit" in normalized
        ):
            rows = queries.list_audits_on_date(conn, day, limit=12)
            answer = compose_response(
                f"{len(rows)} quality audit(s) recorded on {label or 'that date'}.",
                [
                    f"{r['order_number']}: {r['result']}"
                    + (f" — {r['note']}" if r.get("note") and r["result"] == "Failed" else "")
                    for r in rows
                ]
                or ["No audits found for that date."],
                "Ask which audit failed, or for the quality pass rate.",
                footer,
            )
            if rows:
                # Prefer a failed row for follow-up context when present.
                failed_row = next((r for r in rows if r.get("result") == "Failed"), rows[0])
                _attach_quality_context(failed_row["order_number"])
            return answer, snapshot, context

        if "history" in normalized or "quality issue" in normalized:
            history = queries.list_quality_history(conn, limit=10)
            answer = compose_response(
                f"Quality history: {len(history)} quality issue record(s) (excluding inventory shortages).",
                [
                    f"{h['order_number']}: {h['issue_type']} — {h['issue_status']}"
                    + (f" ({h['root_cause_notes']})" if h.get("root_cause_notes") else "")
                    for h in history
                ]
                or ["No quality issue history in this session."],
                "Ask why the audit failed, or who audited.",
                footer,
            )
            if history:
                _attach_quality_context(history[0]["order_number"])
            return answer, snapshot, context

        failed_rows = queries.list_failed_audits(conn, limit=10)
        answer = compose_response(
            f"{len(failed_rows)} failed quality audit(s) in this demo session.",
            [
                f"{r['order_number']}: {r.get('note') or r.get('discrepancy_type') or 'Failed'}"
                f" ({r.get('issue_status') or 'Quality Issue'})"
                for r in failed_rows
            ]
            or ["No failed audits found."],
            "Ask why the audit failed, who picked it, or what SKU was involved.",
            footer,
        )
        if failed_rows:
            _attach_quality_context(failed_rows[0]["order_number"])
        return answer, snapshot, context

    if intent == "quality_auditor":
        order_id = entities.get("order_id") or (prior_context or {}).get("order_id")
        failed = queries.primary_failed_audit(conn, order_id=str(order_id) if order_id else None)
        if not failed:
            # Any recent audit auditor role
            day_audits = queries.list_audits_on_date(conn, now.date(), limit=5)
            if day_audits:
                lead = day_audits[0]
                answer = compose_response(
                    f"Auditor role on recent audit {lead['audit_id']}: {lead.get('auditor_role') or 'Quality'}.",
                    [f"{r['order_number']}: {r['result']} by {r.get('auditor_role') or 'Quality'}" for r in day_audits],
                    "Ask which audit failed.",
                    footer,
                )
                _attach_quality_context(lead["order_number"])
                return answer, snapshot, context
            answer = compose_response(
                "No auditor information is available in this demo session.",
                ["Auditor is stored as auditor_role on quality_audits (demo uses role 'Quality')."],
                "Ask for failed audits.",
                footer,
            )
            return answer, snapshot, context
        answer = compose_response(
            (
                f"Order {failed['order_number']} was audited by role "
                f"'{failed.get('auditor_role') or 'Quality'}' "
                f"(audit {failed['audit_id']})."
            ),
            [
                "Person-level auditor names are not stored — only auditor_role on quality_audits.",
                f"Failed at {failed.get('audit_time')} — {failed.get('note') or failed.get('discrepancy_type')}",
            ],
            "Ask what discrepancy was found, or who picked the order.",
            footer,
        )
        _attach_quality_context(failed["order_number"])
        return answer, snapshot, context

    if intent == "context_picker":
        order_id = entities.get("order_id") or (prior_context or {}).get("order_id")
        if not order_id:
            answer = compose_response(
                "I need an order context to identify the picker.",
                ["Ask which order failed quality first, or name an order like ORD-DT82-0081."],
                "Example follow-up after a quality answer: Who picked it?",
                footer,
            )
            return answer, snapshot, context
        info = queries.get_order_picker(conn, str(order_id))
        _attach_quality_context(str(order_id))
        if not info.get("picker"):
            answer = compose_response(
                f"Picker is not tracked for order {order_id} in available fields.",
                [
                    "Checked supervisor_quality_issues.picker_name and inventory_transactions pick events.",
                    "Ask WMS does not invent picker names.",
                ],
                "Ask what SKU was involved or whether it shipped.",
                footer,
            )
            return answer, snapshot, context
        answer = compose_response(
            f"Order {order_id} was picked by {info['picker']}.",
            [f"Source: {info.get('source') or 'ops data'}"],
            "Ask what SKU was involved, or whether it shipped.",
            footer,
        )
        context["picker"] = info["picker"]
        return answer, snapshot, context

    if intent == "context_sku":
        order_id = entities.get("order_id") or (prior_context or {}).get("order_id")
        prior_sku = entities.get("sku") or (prior_context or {}).get("sku")
        if not order_id and prior_sku:
            answer = compose_response(
                f"Prior context SKU is {prior_sku}.",
                [f"Description: {sku_description(str(prior_sku))}"],
                "Ask for on-hand quantity of that SKU.",
                footer,
            )
            context["sku"] = prior_sku
            return answer, snapshot, context
        if not order_id:
            answer = compose_response(
                "I need an order context to list involved SKUs.",
                ["Ask which order has the quality issue first."],
                "Example: What SKU was involved?",
                footer,
            )
            return answer, snapshot, context
        lines = queries.order_lines_for(conn, str(order_id))
        _attach_quality_context(str(order_id))
        if not lines:
            answer = compose_response(
                f"No order lines are recorded for {order_id}.",
                ["Ask WMS does not invent SKUs."],
                "Ask whether the order shipped.",
                footer,
            )
            return answer, snapshot, context
        lead = lines[0]
        answer = compose_response(
            f"Order {order_id} involved SKU {lead['sku']} ({lead['quantity']} unit(s)).",
            [
                f"SKU {line['sku']}: {line['quantity']} units — {sku_description(line['sku'])}"
                for line in lines[:8]
            ],
            "Ask who picked it, or whether it shipped.",
            footer,
        )
        context["sku"] = lead["sku"]
        return answer, snapshot, context

    if intent == "context_shipped":
        order_id = entities.get("order_id") or (prior_context or {}).get("order_id")
        if not order_id:
            answer = compose_response(
                "I need an order context to check ship/completion status.",
                ["Ask which order failed quality first, or name the order id."],
                "Example: Was it shipped?",
                footer,
            )
            return answer, snapshot, context
        ship = queries.order_ship_status(conn, str(order_id))
        _attach_quality_context(str(order_id))
        if not ship.get("found"):
            answer = compose_response(
                f"No order matching '{order_id}' was found in this demo session.",
                [],
                "Ask how many orders shipped today.",
                footer,
            )
            return answer, snapshot, context
        if ship.get("shipped"):
            when = ship.get("ship_or_completion_time") or ship.get("date") or "timestamp not stored"
            answer = compose_response(
                f"Yes — order {ship['order_number']} is Completed (shipped/closed).",
                [
                    f"Completion/ship signal: {when}",
                    "Dedicated carrier ship-date column is not in schema; Completed status is the ship proxy.",
                ],
                "Ask who picked it or what SKU was involved.",
                footer,
            )
        else:
            answer = compose_response(
                f"Not yet — order {ship['order_number']} is currently {ship.get('status')}.",
                ["It has not reached Completed status in this session."],
                "Ask for SLA status or recommended actions.",
                footer,
            )
        return answer, snapshot, context

    if intent == "kpi_attention":
        accuracy = queries.inventory_accuracy(conn)
        pressures = []
        if sla["breached_count"]:
            pressures.append(
                (sla["breached_count"] * 3 + sla["at_risk_count"], "SLA breaches / at-risk open work")
            )
        if snapshot["blocked"]:
            pressures.append((snapshot["blocked"] * 4, "Blocked orders (inventory shortages)"))
        if quality["open_quality_issues"] or snapshot["quality_issue"]:
            pressures.append(
                (
                    (quality["open_quality_issues"] + snapshot["quality_issue"]) * 3,
                    "Open quality issues / Quality Issue status",
                )
            )
        if quality["pass_rate"] < 98:
            pressures.append((int(100 - quality["pass_rate"]), "Quality audit pass rate below 98% target"))
        if accuracy["accuracy_pct"] < 99:
            pressures.append((int(100 - accuracy["accuracy_pct"]), "Inventory accuracy"))
        pressures.sort(key=lambda item: item[0], reverse=True)
        if not pressures:
            headline = "No KPI is flashing critical right now — queues look controlled."
            bullets = [
                f"SLA: {sla['breached_count']} breached / {sla['at_risk_count']} at risk",
                f"Quality pass rate: {quality['pass_rate']}%",
                f"Blocked: {snapshot['blocked']}",
            ]
        else:
            headline = f"KPI needing attention: {pressures[0][1]}."
            bullets = [f"{idx}. {label}" for idx, (_, label) in enumerate(pressures[:4], start=1)]
        answer = compose_response(
            headline,
            bullets,
            "Ask for recommended actions or an executive summary.",
            rec_footer,
        )
        return answer, snapshot, context

    if intent == "ai_briefing":
        normalized = classification.get("normalized") or ""
        from datetime import timedelta as _td

        shipped_today, _ = queries.count_shipped_on_date(conn, now.date())
        shipped_yesterday, _ = queries.count_shipped_on_date(conn, now.date() - _td(days=1))
        created_today = queries.count_orders_created_on_date(conn, now.date())
        created_yesterday = queries.count_orders_created_on_date(conn, now.date() - _td(days=1))
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
        signals = queries.cross_ops_signals(conn, sla)
        if re.search(r"\b(vs yesterday|versus yesterday|compared to yesterday)\b", normalized):
            answer = compose_response(
                (
                    f"Vs yesterday (session data): completed {shipped_today} today vs "
                    f"{shipped_yesterday} yesterday; created {created_today} vs {created_yesterday}."
                ),
                [
                    "Comparison uses order_header dates in this visitor DB only — not a multi-week trend model.",
                    f"Open now: {max(snapshot['orders_total'] - snapshot['completed'], 0)} | "
                    f"SLA breached: {sla['breached_count']}",
                ],
                "Ask which KPI needs attention.",
                footer,
            )
            return answer, snapshot, context
        bullets = [
            f"Focus signal: {signals['bottleneck']}",
            f"Today: {created_today} created, {shipped_today} completed/shipped",
            f"SLA pressure: {sla['at_risk_count']} at risk, {sla['breached_count']} breached",
            f"Quality pass rate: {quality['pass_rate']}% | open quality cases: {quality['open_quality_issues']}",
        ]
        for idx, item in enumerate(actions[:3], start=1):
            bullets.append(f"Action {idx}: {item['title']} — {item['why']}")
        answer = compose_response(
            "30-minute ops review: start with the top risks and actions below.",
            bullets,
            "Ask for OTIF, failed audits, or warehouse comparison.",
            rec_footer,
        )
        return answer, snapshot, context

    if intent == "orders_urgent_mix":
        normalized = classification.get("normalized") or ""
        if "completed today" in normalized or (
            "completed" in normalized and entities.get("date_label") == "today"
        ):
            day = entities.get("date") or now.date()
            count, sample = queries.count_shipped_on_date(conn, day)
            answer = compose_response(
                f"{count} order(s) completed/shipped today.",
                (["Examples: " + ", ".join(sample)] if sample else ["None completed today."]),
                "Ask for OTIF or longest open orders.",
                footer,
            )
            return answer, snapshot, context
        if "longest" in normalized or "oldest" in normalized:
            rows = queries.list_oldest_open_orders(conn, limit=8)
            answer = compose_response(
                "Longest-open (aging) orders:",
                [f"{r['order_number']} — {r['status']} / {r['urgency']} ({r['date']})" for r in rows]
                or ["No open orders."],
                "Ask which breached SLA.",
                footer,
            )
            return answer, snapshot, context
        if "fastest" in normalized:
            answer = compose_response(
                "Fastest-order ranking is not fully supported — no dedicated cycle-time leaderboard.",
                [
                    "Ask for average completion time (derived) or oldest open orders instead.",
                    "Per-order start/stop labor clocks are not stored as a ranked metric.",
                ],
                "Ask for average completion time.",
                footer="Honest limit: no fastest-order leaderboard in schema.",
            )
            return answer, snapshot, context
        urgent = [r for r in queries.list_orders(conn, urgency="Urgent", limit=50) if r["status"] != "Completed"]
        critical = [r for r in queries.list_orders(conn, urgency="Critical", limit=50) if r["status"] != "Completed"]
        blocked = queries.list_orders(conn, status="Blocked", limit=20)
        answer = compose_response(
            (
                f"Urgency mix (open): {len(critical)} critical, {len(urgent)} urgent; "
                f"{len(blocked)} blocked waiting on inventory/workflow."
            ),
            [
                "Critical: " + (", ".join(r["order_number"] for r in critical[:6]) or "none"),
                "Urgent: " + (", ".join(r["order_number"] for r in urgent[:6]) or "none"),
                "Blocked: " + (", ".join(r["order_number"] for r in blocked[:6]) or "none"),
                f"SLA missed/breached now: {sla['breached_count']}",
            ],
            "Ask for critical order details or recommended actions.",
            footer,
        )
        return answer, snapshot, context

    if intent == "quality_summary":
        issues = queries.open_quality_issues(conn, limit=8)
        failed_rows = queries.list_failed_audits(conn, limit=5)
        bullets = [f"{i['issue_id']} on {i['order_number']} ({i['issue_type']})" for i in issues]
        if not bullets and failed_rows:
            bullets = [
                f"Historical fail: {r['order_number']} — {r.get('note') or r.get('discrepancy_type')}"
                for r in failed_rows
            ]
        if not bullets:
            bullets = ["No open quality/supervisor issues."]
        answer = compose_response(
            (
                f"Quality summary: audit pass rate {quality['pass_rate']}% "
                f"({quality['audits_passed']}/{quality['audits_total']} audits passed), "
                f"{quality['pending_verification']} pending verification, "
                f"{quality['open_quality_issues']} open quality case(s), "
                f"{quality['audits_failed']} failed audit(s) on record."
            ),
            bullets,
            "Ask which audit failed, or for quality history.",
            footer,
        )
        if failed_rows:
            _attach_quality_context(failed_rows[0]["order_number"])
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
        mix = queries.planner_mix(conn)
        answer = compose_response(
            (
                f"Control tower: {snapshot['blocked']} blocked, {sla['at_risk_count']} at risk, "
                f"{sla['breached_count']} breached, {quality['open_quality_issues']} open quality cases."
            ),
            [
                f"{idx}. {item['title']}" for idx, item in enumerate(actions, start=1)
            ]
            + [
                "Urgency mix: "
                + (", ".join(f"{u['urgency']}={u['count']}" for u in mix["urgency"][:4]) or "none"),
                f"Healthy open SLA: {sla['healthy_count']}",
            ],
            "Ask for the top three recommended actions with reasons.",
            rec_footer,
        )
        return answer, snapshot, context

    if intent == "cross_ops_risk":
        signals = queries.cross_ops_signals(conn, sla)
        answer = compose_response(
            f"Primary bottleneck signal: {signals['bottleneck']}.",
            signals["signals"]
            or [
                "No strong cross-functional blockage detected from blocked/QA/SLA queues right now.",
            ],
            "Ask for recommended supervisor actions.",
            rec_footer,
        )
        return answer, snapshot, context

    if intent == "planner_summary":
        mix = queries.planner_mix(conn)
        answer = compose_response(
            f"Order Planning view: {snapshot['orders_total']} total order(s), {snapshot['orders_placed']} currently placed.",
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

    if intent == "kpi_definitions":
        answer = compose_response(
            "DigiTech WMS KPI definitions used by Ask WMS:",
            KPI_DEFINITIONS,
            "Ask for the live SLA summary or inventory accuracy.",
            footer="Static definitions + live metrics available on request.",
        )
        return answer, snapshot, context

    if intent == "help_explain":
        normalized = classification.get("normalized") or ""
        if "workflow" in normalized or "planner to shipping" in normalized:
            answer = compose_response(
                "Orders move Order Planning -> Operations pick -> Quality verification -> Completed/shipped, with Supervisor handling escalations.",
                [
                    "Order Planning creates demand against source-warehouse inventory.",
                    "Operations picks and can block on shortage.",
                    "Quality verifies and may escalate discrepancies.",
                    "Executive dashboard monitors SLA, productivity, and inventory KPIs.",
                ],
                "Ask what the Supervisor Control Tower is for.",
                footer="Application documentation (not a live metric query).",
            )
        elif "sla" in normalized and "what is" in normalized:
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
        elif "read only" in normalized or "readonly" in normalized or "read-only" in (question or "").lower():
            answer = compose_response(
                "Ask WMS is read-only: it never ships orders, adjusts stock, or changes statuses from chat.",
                [
                    "It classifies intent, runs controlled SELECT queries on your visitor session DB, and returns deterministic HTML answers.",
                    "Write actions stay in Order Planning / Inventory / Operations / Quality / Supervisor screens.",
                ],
                "Try: How is the warehouse performing today?",
                footer="Safety rule: natural language never executes writes.",
            )
        elif "digitech" in normalized or "ask wms" in normalized or normalized in {"help", "capabilities"}:
            answer = compose_response(
                "DigiTech WMS is this portfolio warehouse demo; Ask WMS is its zero-paid-API assistant over your private session database.",
                HELP_CATEGORIES,
                "Try: Late? or What are the top three recommended actions?",
                footer="Ask WMS uses intent recognition, synonyms, follow-ups, and read-only SQL — not a paid LLM API.",
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
