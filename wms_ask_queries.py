"""Read-only Ask WMS query handlers against the visitor session DB."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable


def _date_bounds(day_value, tzinfo=None):
    start = datetime(day_value.year, day_value.month, day_value.day)
    if tzinfo is not None:
        start = start.replace(tzinfo=tzinfo)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def count_orders_by_status(conn) -> dict[str, int]:
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM order_header GROUP BY status")
    return {str(status or ""): int(count or 0) for status, count in c.fetchall()}


def count_shipped_on_date(conn, day_value, tzinfo=None) -> tuple[int, list[str]]:
    start, end = _date_bounds(day_value, tzinfo=tzinfo)
    c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(*)
        FROM order_header
        WHERE status = 'Completed'
          AND date >= ? AND date < ?
        """,
        (start, end),
    )
    count = int(c.fetchone()[0] or 0)
    c.execute(
        """
        SELECT order_number
        FROM order_header
        WHERE status = 'Completed'
          AND date >= ? AND date < ?
        ORDER BY date DESC
        LIMIT 8
        """,
        (start, end),
    )
    return count, [row[0] for row in c.fetchall()]


def count_received_on_date(conn, day_value, tzinfo=None) -> int:
    start, end = _date_bounds(day_value, tzinfo=tzinfo)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM order_header WHERE date >= ? AND date < ?",
        (start, end),
    )
    return int(c.fetchone()[0] or 0)


def list_orders(
    conn,
    *,
    status: str | None = None,
    urgency: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    c = conn.cursor()
    query = "SELECT order_number, urgency, status, responsibility, date FROM order_header WHERE 1=1"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if urgency:
        query += " AND urgency = ?"
        params.append(urgency)
    query += " ORDER BY date ASC LIMIT ?"
    params.append(limit)
    c.execute(query, params)
    return [
        {
            "order_number": row[0],
            "urgency": row[1],
            "status": row[2],
            "responsibility": row[3],
            "date": row[4],
        }
        for row in c.fetchall()
    ]


def get_order_detail(conn, order_id: str) -> dict[str, Any] | None:
    c = conn.cursor()
    c.execute(
        """
        SELECT order_number, request_id, date, source, destination, urgency, status,
               responsibility, expected_quantity, picked_quantity
        FROM order_header
        WHERE order_number = ? OR order_number LIKE ?
        LIMIT 1
        """,
        (order_id, f"%{order_id}%"),
    )
    row = c.fetchone()
    if not row:
        return None
    detail = {
        "order_number": row[0],
        "request_id": row[1],
        "date": row[2],
        "source": row[3],
        "destination": row[4],
        "urgency": row[5],
        "status": row[6],
        "responsibility": row[7],
        "expected_quantity": int(row[8] or 0),
        "picked_quantity": int(row[9] or 0),
        "lines": [],
    }
    c.execute(
        "SELECT sku, quantity FROM order_lines WHERE order_number = ? ORDER BY sku",
        (detail["order_number"],),
    )
    detail["lines"] = [{"sku": sku, "quantity": int(qty or 0)} for sku, qty in c.fetchall()]
    return detail


def inventory_snapshot(conn) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(DISTINCT sku), COALESCE(SUM(quantity), 0),
               COALESCE(SUM(CAST(quantity AS REAL) * price), 0)
        FROM inventory
        WHERE quantity > 0
        """
    )
    sku_count, units, value = c.fetchone()
    return {
        "sku_count": int(sku_count or 0),
        "units_on_hand": int(units or 0),
        "inventory_value": float(value or 0),
    }


def rank_skus(conn, mode: str = "most_qty", limit: int = 5) -> list[dict[str, Any]]:
    c = conn.cursor()
    limit = max(1, min(int(limit or 5), 20))
    if mode == "most_value":
        order_sql = "ORDER BY val DESC, qty DESC, sku ASC"
    elif mode == "least_qty":
        order_sql = "ORDER BY qty ASC, sku ASC"
    else:
        order_sql = "ORDER BY qty DESC, val DESC, sku ASC"
    c.execute(
        f"""
        SELECT sku, COALESCE(SUM(quantity), 0) AS qty,
               COALESCE(SUM(CAST(quantity AS REAL) * price), 0) AS val
        FROM inventory
        WHERE quantity > 0 AND TRIM(COALESCE(sku, '')) <> ''
        GROUP BY sku
        {order_sql}
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {"sku": sku, "qty": int(qty or 0), "value": float(val or 0)}
        for sku, qty, val in c.fetchall()
    ]


def sku_availability(conn, sku: str, source_warehouse: str) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM inventory
        WHERE sku = ?
          AND warehouse = ?
        """,
        (sku, source_warehouse),
    )
    source_qty = int(c.fetchone()[0] or 0)
    c.execute(
        """
        SELECT warehouse, location, quantity
        FROM inventory
        WHERE sku = ? AND quantity > 0
        ORDER BY quantity DESC, warehouse ASC
        LIMIT 8
        """,
        (sku,),
    )
    locations = [
        {"warehouse": wh, "location": loc, "quantity": int(qty or 0)}
        for wh, loc, qty in c.fetchall()
    ]
    c.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM inventory
        WHERE sku = ? AND quantity > 0
        """,
        (sku,),
    )
    network_qty = int(c.fetchone()[0] or 0)
    return {
        "sku": sku,
        "source_qty": source_qty,
        "network_qty": network_qty,
        "locations": locations,
    }


def low_stock_skus(conn, threshold: int = 25, limit: int = 8) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT sku, SUM(quantity) AS tq
        FROM inventory
        GROUP BY sku
        HAVING tq > 0 AND tq < ?
        ORDER BY tq ASC
        LIMIT ?
        """,
        (threshold, limit),
    )
    return [{"sku": sku, "qty": int(qty or 0)} for sku, qty in c.fetchall()]


def inventory_by_warehouse(conn) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT warehouse,
               COALESCE(SUM(quantity), 0),
               COALESCE(SUM(CAST(quantity AS REAL) * price), 0)
        FROM inventory
        WHERE quantity > 0
        GROUP BY warehouse
        ORDER BY 3 DESC
        """
    )
    return [
        {"warehouse": wh, "units": int(units or 0), "value": float(value or 0)}
        for wh, units, value in c.fetchall()
    ]


def inventory_accuracy(conn) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(DISTINCT sku) FROM inventory
        WHERE quantity > 0
          AND TRIM(COALESCE(warehouse,'')) <> ''
          AND LOWER(TRIM(COALESCE(warehouse,''))) <> 'nan'
          AND TRIM(COALESCE(location,'')) <> ''
          AND LOWER(TRIM(COALESCE(location,''))) <> 'nan'
        """
    )
    valid = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(DISTINCT sku) FROM inventory WHERE quantity > 0")
    total = int(c.fetchone()[0] or 0)
    pct = round((valid / total) * 100, 1) if total else 100.0
    return {"valid_skus": valid, "total_skus": total, "accuracy_pct": pct}


def quality_snapshot(conn) -> dict[str, Any]:
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM quality_audits")
    total = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result IN ('Pass', 'Passed')")
    passed = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result = 'Failed'")
    failed = int(c.fetchone()[0] or 0)
    c.execute(
        """
        SELECT COUNT(*) FROM order_header
        WHERE status = 'Pending Verification'
        """
    )
    pending = int(c.fetchone()[0] or 0)
    c.execute(
        """
        SELECT COUNT(*) FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
          AND COALESCE(issue_type, '') != 'Inventory Shortage'
        """
    )
    open_issues = int(c.fetchone()[0] or 0)
    accuracy = round((passed / total) * 100, 1) if total else 100.0
    return {
        "audits_total": total,
        "audits_passed": passed,
        "audits_failed": failed,
        "pending_verification": pending,
        "open_quality_issues": open_issues,
        "pass_rate": accuracy,
    }


def open_quality_issues(conn, limit: int = 10) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT issue_id, order_number, issue_type, issue_status
        FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
        ORDER BY issue_date DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "issue_id": row[0],
            "order_number": row[1],
            "issue_type": row[2],
            "issue_status": row[3],
        }
        for row in c.fetchall()
    ]


def picker_workload(conn, limit: int = 8) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT COALESCE(user_role, 'Unassigned') AS picker_name, COUNT(*) AS pick_events
        FROM inventory_transactions
        WHERE tx_code = 'PICK'
        GROUP BY picker_name
        ORDER BY pick_events DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [{"picker": name, "picks": int(count or 0)} for name, count in c.fetchall()]


def planner_mix(conn) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        "SELECT destination, COUNT(*) FROM order_header GROUP BY destination ORDER BY COUNT(*) DESC"
    )
    destinations = [{"destination": d, "count": int(n or 0)} for d, n in c.fetchall()]
    c.execute(
        "SELECT urgency, COUNT(*) FROM order_header GROUP BY urgency ORDER BY COUNT(*) DESC"
    )
    urgency = [{"urgency": u, "count": int(n or 0)} for u, n in c.fetchall()]
    return {"destinations": destinations, "urgency": urgency}


def sku_open_order_demand(conn, sku: str) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT ol.order_number, oh.status, oh.urgency, ol.quantity
        FROM order_lines ol
        JOIN order_header oh ON oh.order_number = ol.order_number
        WHERE ol.sku = ?
          AND oh.status != 'Completed'
        ORDER BY oh.date ASC
        LIMIT 12
        """,
        (sku,),
    )
    return [
        {
            "order_number": order_number,
            "status": status,
            "urgency": urgency,
            "quantity": int(qty or 0),
        }
        for order_number, status, urgency, qty in c.fetchall()
    ]


def build_sla_buckets(
    conn,
    *,
    parse_order_datetime: Callable,
    normalize_urgency: Callable,
    calculate_sla_status: Callable,
    now,
) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        "SELECT order_number, urgency, status, date FROM order_header WHERE status != 'Completed'"
    )
    buckets = {"healthy": [], "at_risk": [], "breached": []}
    for order_number, urgency, status, date_str in c.fetchall():
        try:
            order_time = parse_order_datetime(date_str)
            sla_key, _ = calculate_sla_status(order_time, normalize_urgency(urgency, "Standard"), now)
        except Exception:
            continue
        buckets.setdefault(sla_key, []).append(
            {
                "order_number": order_number,
                "urgency": urgency,
                "status": status,
                "sla": sla_key,
            }
        )
    return {
        "healthy": buckets.get("healthy", []),
        "at_risk": buckets.get("at_risk", []),
        "breached": buckets.get("breached", []),
        "healthy_count": len(buckets.get("healthy", [])),
        "at_risk_count": len(buckets.get("at_risk", [])),
        "breached_count": len(buckets.get("breached", [])),
    }


def list_oldest_open_orders(conn, limit: int = 8) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT order_number, urgency, status, date
        FROM order_header
        WHERE status != 'Completed'
        ORDER BY date ASC
        LIMIT ?
        """,
        (max(1, min(int(limit or 8), 20)),),
    )
    return [
        {
            "order_number": row[0],
            "urgency": row[1],
            "status": row[2],
            "date": row[3],
        }
        for row in c.fetchall()
    ]


def inventory_adjustments(conn, limit: int = 10, sku: str | None = None) -> list[dict[str, Any]]:
    c = conn.cursor()
    if sku:
        c.execute(
            """
            SELECT tx_time, sku, warehouse, location, qty_change, notes
            FROM inventory_transactions
            WHERE tx_code = 'ADJUST' AND sku = ?
            ORDER BY tx_time DESC
            LIMIT ?
            """,
            (sku, max(1, min(int(limit or 10), 25))),
        )
    else:
        c.execute(
            """
            SELECT tx_time, sku, warehouse, location, qty_change, notes
            FROM inventory_transactions
            WHERE tx_code = 'ADJUST'
            ORDER BY tx_time DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 10), 25)),),
        )
    return [
        {
            "tx_time": row[0],
            "sku": row[1],
            "warehouse": row[2],
            "location": row[3],
            "qty_change": int(row[4] or 0),
            "notes": row[5],
        }
        for row in c.fetchall()
    ]


def _order_completion_time(conn, order_number: str) -> str | None:
    """Best-effort completion timestamp for OTIF (audit pass, else pick close/pick)."""
    c = conn.cursor()
    c.execute(
        """
        SELECT audit_time
        FROM quality_audits
        WHERE order_number = ?
          AND result IN ('Pass', 'Passed')
        ORDER BY audit_time DESC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row and row[0]:
        return str(row[0])
    c.execute(
        """
        SELECT tx_time
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code IN ('PICK_CLOSE', 'PICK')
        ORDER BY
            CASE tx_code WHEN 'PICK_CLOSE' THEN 0 ELSE 1 END,
            tx_time DESC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row and row[0]:
        return str(row[0])
    return None


def otif_snapshot(
    conn,
    *,
    parse_order_datetime: Callable,
    normalize_urgency: Callable,
    calculate_sla_deadline: Callable,
) -> dict[str, Any]:
    """
    Honest OTIF for Completed orders when completion time is recoverable.

    On-time: completion_time <= SLA deadline from order create date + urgency.
    In-full: picked_quantity >= expected_quantity (when expected_quantity > 0).
    """
    c = conn.cursor()
    c.execute(
        """
        SELECT order_number, urgency, date, expected_quantity, picked_quantity
        FROM order_header
        WHERE status = 'Completed'
        """
    )
    rows = c.fetchall()
    total = len(rows)
    measured = 0
    otif_count = 0
    on_time_count = 0
    in_full_count = 0
    missing_completion = 0
    samples: list[dict[str, Any]] = []

    for order_number, urgency, date_str, expected_qty, picked_qty in rows:
        expected = int(expected_qty or 0)
        picked = int(picked_qty or 0)
        in_full = (expected <= 0) or (picked >= expected)
        completion_raw = _order_completion_time(conn, str(order_number))
        if not completion_raw:
            missing_completion += 1
            continue
        try:
            order_time = parse_order_datetime(date_str)
            completion_time = parse_order_datetime(completion_raw)
            deadline = calculate_sla_deadline(order_time, normalize_urgency(urgency, "Standard"))
            on_time = completion_time <= deadline
        except Exception:
            missing_completion += 1
            continue
        measured += 1
        if on_time:
            on_time_count += 1
        if in_full:
            in_full_count += 1
        is_otif = on_time and in_full
        if is_otif:
            otif_count += 1
        if len(samples) < 6 and not is_otif:
            samples.append(
                {
                    "order_number": order_number,
                    "on_time": on_time,
                    "in_full": in_full,
                    "urgency": urgency,
                }
            )

    otif_pct = round((otif_count / measured) * 100, 1) if measured else None
    return {
        "completed_total": total,
        "measured": measured,
        "missing_completion": missing_completion,
        "otif_count": otif_count,
        "otif_pct": otif_pct,
        "on_time_count": on_time_count,
        "in_full_count": in_full_count,
        "samples_missed": samples,
        "supported": measured > 0,
        "definition": (
            "OTIF = Completed orders that finished on/before SLA deadline "
            "and picked_quantity >= expected_quantity."
        ),
    }


def cross_ops_signals(conn, sla_buckets: dict[str, Any]) -> dict[str, Any]:
    """Deterministic cross-functional risk signals from live queues."""
    status_map = count_orders_by_status(conn)
    blocked = int(status_map.get("Blocked", 0) or 0)
    pending_qa = int(status_map.get("Pending Verification", 0) or 0)
    quality_issue = int(status_map.get("Quality Issue", 0) or 0)
    low = low_stock_skus(conn, threshold=25, limit=5)
    breached = sla_buckets.get("breached") or []
    at_risk = sla_buckets.get("at_risk") or []
    shortage_linked = [
        r for r in breached + at_risk if str(r.get("status")) in {"Blocked", "Orders Placed"}
    ]
    quality_linked = [
        r
        for r in breached + at_risk
        if str(r.get("status")) in {"Pending Verification", "Quality Issue"}
    ]
    signals = []
    if blocked:
        signals.append(f"{blocked} blocked order(s) — inventory shortage is stalling fulfillment.")
    if pending_qa or quality_issue:
        signals.append(
            f"{pending_qa + quality_issue} order(s) waiting on quality — shipping cannot close."
        )
    if shortage_linked:
        signals.append(
            f"{len(shortage_linked)} SLA-risk/breached order(s) sit in blocked/placed stages "
            "(inventory↔SLA pressure)."
        )
    if quality_linked:
        signals.append(
            f"{len(quality_linked)} SLA-risk/breached order(s) sit in QA/quality-issue stages "
            "(quality↔shipping pressure)."
        )
    if low:
        signals.append(f"{len(low)} low-stock SKU(s) may create future blocks.")
    bottleneck = "None clear"
    if blocked >= max(pending_qa, 1) and blocked > 0:
        bottleneck = "Inventory shortages / blocked orders"
    elif (pending_qa + quality_issue) > blocked:
        bottleneck = "Quality verification queue"
    elif len(breached) + len(at_risk) > 0:
        bottleneck = "SLA pressure on open work"
    elif int(status_map.get("Orders Placed", 0) or 0) > int(status_map.get("Picking in Progress", 0) or 0):
        bottleneck = "Picking release / ready-to-pick queue"
    return {
        "signals": signals,
        "bottleneck": bottleneck,
        "blocked": blocked,
        "pending_qa": pending_qa,
        "quality_issue": quality_issue,
        "sla_pressure": len(breached) + len(at_risk),
    }


def sla_breach_cause_hint(status: str) -> str:
    mapping = {
        "Blocked": "Likely inventory shortage / blocked workflow",
        "Pending Verification": "Waiting on quality verification",
        "Quality Issue": "Open quality escalation",
        "Orders Placed": "Not yet released/picked",
        "Picking in Progress": "Pick still in progress",
    }
    return mapping.get(str(status or ""), "Cause not fully attributable from available fields")
