"""Deterministic Ask WMS recommendation engine (read-only, zero paid APIs)."""

from __future__ import annotations

from typing import Any, Callable

import wms_ask_queries as queries


def build_recommendations(
    conn,
    *,
    limit: int = 3,
    parse_order_datetime: Callable,
    normalize_urgency: Callable,
    calculate_sla_status: Callable,
    now,
    low_stock_threshold: int = 25,
    shortage_issue_type: str = "Inventory Shortage",
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    sla = queries.build_sla_buckets(
        conn,
        parse_order_datetime=parse_order_datetime,
        normalize_urgency=normalize_urgency,
        calculate_sla_status=calculate_sla_status,
        now=now,
    )

    for row in sla["breached"]:
        if str(row.get("urgency")) == "Critical":
            actions.append(
                {
                    "priority": 1,
                    "title": f"Order {row['order_number']} — Critical SLA breach",
                    "why": f"Critical order is breached while still in {row['status']}.",
                    "area": "SLA",
                }
            )
    for row in sla["at_risk"]:
        if str(row.get("urgency")) == "Critical":
            actions.append(
                {
                    "priority": 2,
                    "title": f"Order {row['order_number']} — Critical SLA risk",
                    "why": f"Critical order is at risk and currently in {row['status']}.",
                    "area": "SLA",
                }
            )

    for issue in queries.open_quality_issues(conn, limit=8):
        if issue.get("issue_type") != shortage_issue_type:
            actions.append(
                {
                    "priority": 3,
                    "title": f"Quality escalation {issue['issue_id']} on {issue['order_number']}",
                    "why": f"Open {issue['issue_type']} issue in status {issue['issue_status']}.",
                    "area": "Quality",
                }
            )
        else:
            actions.append(
                {
                    "priority": 4,
                    "title": f"Inventory shortage blocking {issue['order_number']}",
                    "why": "Shortage escalation is open and can stall fulfillment.",
                    "area": "Inventory",
                }
            )

    for row in sla["at_risk"]:
        if str(row.get("urgency")) == "Urgent":
            actions.append(
                {
                    "priority": 5,
                    "title": f"Order {row['order_number']} — Urgent SLA risk",
                    "why": f"Urgent order is approaching deadline in {row['status']}.",
                    "area": "SLA",
                }
            )

    blocked = queries.list_orders(conn, status="Blocked", limit=6)
    for row in blocked:
        actions.append(
            {
                "priority": 4 if row.get("urgency") in {"Critical", "Urgent"} else 7,
                "title": f"Unblock order {row['order_number']}",
                "why": "Order is blocked and cannot progress until inventory/action clears.",
                "area": "Operations",
            }
        )

    pending_qa = queries.list_orders(conn, status="Pending Verification", limit=4)
    for row in pending_qa:
        actions.append(
            {
                "priority": 6,
                "title": f"Verify order {row['order_number']}",
                "why": "Completed picks are waiting in quality verification.",
                "area": "Quality",
            }
        )

    low = queries.low_stock_skus(conn, threshold=low_stock_threshold, limit=4)
    for row in low:
        actions.append(
            {
                "priority": 8,
                "title": f"Restock low SKU {row['sku']}",
                "why": f"Only {row['qty']} units remain below the {low_stock_threshold}-unit threshold.",
                "area": "Inventory",
            }
        )

    placed = queries.list_orders(conn, status="Orders Placed", limit=3)
    for row in placed:
        actions.append(
            {
                "priority": 9,
                "title": f"Release/start pick for {row['order_number']}",
                "why": "Order is still in Orders Placed and adds to the open backlog.",
                "area": "Operations",
            }
        )

    # Deduplicate by title while preserving best priority
    ranked: dict[str, dict[str, Any]] = {}
    for action in actions:
        key = action["title"]
        if key not in ranked or action["priority"] < ranked[key]["priority"]:
            ranked[key] = action

    ordered = sorted(ranked.values(), key=lambda item: (item["priority"], item["title"]))
    if not ordered:
        ordered = [
            {
                "priority": 10,
                "title": "No urgent interventions detected",
                "why": "No breached critical SLA, open quality escalations, or blocked orders were found.",
                "area": "Overview",
            }
        ]
    return ordered[: max(1, min(int(limit or 3), 10))]
