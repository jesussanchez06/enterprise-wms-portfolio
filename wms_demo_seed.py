"""Deterministic DigiTech WMS recruiter-demo baseline: exactly 82 warehouse orders.

Seeds operational history (lines, picks, inventory decrements, audits, exceptions)
so Executive Dashboard KPIs and Ask WMS derive naturally from records — never
hard-coded display percentages.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

DEMO_ORDER_COUNT = 82
DEMO_ORDER_PREFIX = "ORD-DT82-"
# v2: all 82 orders Completed (0 pending) for recruiter Executive Dashboard baseline.
DEMO_SEED_VERSION = "dt82-v2-zero-pending"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def _iso(dt_value: datetime) -> str:
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=PACIFIC_TZ)
    return dt_value.isoformat()


def _business_days_back(anchor: datetime, count: int) -> list[datetime]:
    """Return `count` business-day anchors (06:00 PT), newest first then reversed to oldest-first."""
    days: list[datetime] = []
    cursor = anchor
    guard = 0
    while len(days) < count and guard < 80:
        guard += 1
        candidate = cursor.replace(hour=6, minute=0, second=0, microsecond=0)
        if candidate.weekday() < 5:
            days.append(candidate)
        cursor -= timedelta(days=1)
    days.reverse()
    return days


def clear_operational_tables(conn) -> dict[str, int]:
    c = conn.cursor()
    counts = {}
    for table in (
        "ask_wms_chat",
        "ask_wms_conversations",
        "supervisor_quality_issues",
        "quality_audits",
        "inventory_transactions",
        "order_lines",
        "order_header",
    ):
        try:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int(c.fetchone()[0] or 0)
            c.execute(f"DELETE FROM {table}")
        except Exception:
            counts[table] = 0
    return counts


def demo_baseline_present(conn) -> bool:
    """True only for the recruiter baseline: 82 tagged orders, all Completed."""
    c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) FROM order_header")
        total = int(c.fetchone()[0] or 0)
        c.execute(
            "SELECT COUNT(*) FROM order_header WHERE order_number LIKE ?",
            (f"{DEMO_ORDER_PREFIX}%",),
        )
        tagged = int(c.fetchone()[0] or 0)
        c.execute("SELECT COUNT(*) FROM order_header WHERE status = 'Completed'")
        completed = int(c.fetchone()[0] or 0)
        c.execute("SELECT COUNT(*) FROM order_header WHERE status != 'Completed'")
        pending = int(c.fetchone()[0] or 0)
    except Exception:
        return False
    return (
        total == DEMO_ORDER_COUNT
        and tagged == DEMO_ORDER_COUNT
        and completed == DEMO_ORDER_COUNT
        and pending == 0
    )


def _insert_order(
    conn,
    *,
    order_number: str,
    request_id: str,
    order_time: datetime,
    destination: str,
    urgency: str,
    status: str,
    responsibility: str,
    expected_quantity: int,
    picked_quantity: int,
    source: str,
):
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO order_header (
            order_number, request_id, date, source, destination, urgency,
            status, responsibility, expected_quantity, picked_quantity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_number,
            request_id,
            _iso(order_time),
            source,
            destination,
            urgency,
            status,
            responsibility,
            int(expected_quantity),
            int(picked_quantity),
        ),
    )


def _insert_lines(conn, order_number: str, lines: list[tuple[str, int]]):
    c = conn.cursor()
    for sku, qty in lines:
        c.execute("INSERT INTO order_lines VALUES (?, ?, ?)", (order_number, sku, int(qty)))


def _log_tx(
    log_inventory_transaction: Callable,
    conn,
    *,
    tx_code: str,
    order_number: str,
    sku: str,
    warehouse: str,
    location: str,
    qty_change: int,
    qty_before: int,
    qty_after: int,
    user_role: str,
    notes: str,
    tx_time: datetime,
):
    log_inventory_transaction(
        conn,
        tx_code,
        order_number,
        sku,
        warehouse,
        location,
        qty_change,
        qty_before,
        qty_after,
        user_role,
        notes,
        tx_time=tx_time,
    )


def _consume_pick(
    conn,
    *,
    log_inventory_transaction: Callable,
    get_best_inventory_location: Callable,
    order_number: str,
    sku: str,
    qty: int,
    source_wh: str,
    picker: str,
    tx_time: datetime,
    notes: str,
) -> tuple[str, str]:
    warehouse, location, on_hand = get_best_inventory_location(conn, sku, source_wh)
    on_hand = int(on_hand or 0)
    qty = int(qty)
    if qty <= 0:
        return warehouse, location
    if on_hand < qty:
        raise RuntimeError(
            f"Insufficient stock to seed pick for {order_number} SKU {sku}: "
            f"need {qty}, on_hand {on_hand} at {warehouse}/{location}"
        )
    qty_after = on_hand - qty
    c = conn.cursor()
    c.execute(
        """
        UPDATE inventory
        SET quantity = quantity - ?
        WHERE sku = ? AND warehouse = ? AND location = ? AND quantity >= ?
        """,
        (qty, sku, warehouse, location, qty),
    )
    if c.rowcount == 0:
        raise RuntimeError(f"Inventory update failed for {order_number} SKU {sku}")
    _log_tx(
        log_inventory_transaction,
        conn,
        tx_code="PICK",
        order_number=order_number,
        sku=sku,
        warehouse=warehouse,
        location=location,
        qty_change=-qty,
        qty_before=on_hand,
        qty_after=qty_after,
        user_role=picker,
        notes=notes,
        tx_time=tx_time,
    )
    return warehouse, location


def _seed_pick_lifecycle(
    conn,
    *,
    log_inventory_transaction: Callable,
    get_best_inventory_location: Callable,
    order_number: str,
    lines: list[tuple[str, int]],
    source_wh: str,
    destination: str,
    primary_picker: str,
    secondary_picker: str | None,
    start_time: datetime,
    pick_span_minutes: int,
    close_extra_minutes: int,
    pick_fraction: float = 1.0,
) -> int:
    """Apply PICK_START / PICK / optional takeover / PICK_CLOSE. Returns units picked."""
    _log_tx(
        log_inventory_transaction,
        conn,
        tx_code="PICK_START",
        order_number=order_number,
        sku="N/A",
        warehouse=source_wh,
        location="N/A",
        qty_change=0,
        qty_before=0,
        qty_after=0,
        user_role=primary_picker,
        notes="Demo seed pick start",
        tx_time=start_time,
    )

    total_expected = sum(int(q) for _, q in lines)
    target_units = int(round(total_expected * max(0.0, min(pick_fraction, 1.0))))
    if pick_fraction < 1.0 and total_expected > 1:
        target_units = max(1, min(target_units, total_expected - 1))
    elif pick_fraction >= 1.0:
        target_units = total_expected

    remaining_budget = target_units
    picked_total = 0
    cursor_time = start_time + timedelta(minutes=max(pick_span_minutes // max(len(lines), 1), 3))

    for line_index, (sku, qty) in enumerate(lines):
        if remaining_budget <= 0:
            break
        take = min(int(qty), remaining_budget)
        if take <= 0:
            continue
        picker = primary_picker
        if secondary_picker and line_index > 0 and line_index % 2 == 1:
            picker = secondary_picker
            _log_tx(
                log_inventory_transaction,
                conn,
                tx_code="PICK_TAKEOVER",
                order_number=order_number,
                sku="N/A",
                warehouse=source_wh,
                location="N/A",
                qty_change=0,
                qty_before=picked_total,
                qty_after=picked_total,
                user_role=secondary_picker,
                notes=f"Takeover from {primary_picker}",
                tx_time=cursor_time - timedelta(minutes=1),
            )
        _consume_pick(
            conn,
            log_inventory_transaction=log_inventory_transaction,
            get_best_inventory_location=get_best_inventory_location,
            order_number=order_number,
            sku=sku,
            qty=take,
            source_wh=source_wh,
            picker=picker,
            tx_time=cursor_time,
            notes=f"Pick transaction for destination {destination}",
        )
        picked_total += take
        remaining_budget -= take
        cursor_time += timedelta(minutes=max(4, pick_span_minutes // max(len(lines) + 2, 1)))

    if pick_fraction >= 1.0 and picked_total == total_expected:
        close_time = start_time + timedelta(minutes=pick_span_minutes + close_extra_minutes)
        _log_tx(
            log_inventory_transaction,
            conn,
            tx_code="PICK_CLOSE",
            order_number=order_number,
            sku="N/A",
            warehouse=source_wh,
            location="N/A",
            qty_change=0,
            qty_before=picked_total,
            qty_after=picked_total,
            user_role=secondary_picker or primary_picker,
            notes="Pick complete — moved to quality verification",
            tx_time=close_time,
        )
    return picked_total


def seed_demo_orders_82(
    conn,
    *,
    force: bool = False,
    now: datetime | None = None,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Rebuild (or ensure) the exact 82-order DigiTech demo baseline on `conn`.

    `dependencies` must provide callables from whs_mgmt when imported from outside:
      seed_demo_inventory, SOURCE_WAREHOUSE, DESTINATION_WAREHOUSES, PICKER_ROSTER,
      log_inventory_transaction, get_best_inventory_location, create_supervisor_issue,
      build_issue_type, SHORTAGE_ISSUE_TYPE, shortage_issue_audit_id
    """
    if dependencies is None:
        raise ValueError("seed_demo_orders_82 requires whs_mgmt dependency bundle")

    seed_demo_inventory = dependencies["seed_demo_inventory"]
    SOURCE_WAREHOUSE = dependencies["SOURCE_WAREHOUSE"]
    DESTINATION_WAREHOUSES = dependencies["DESTINATION_WAREHOUSES"]
    PICKER_ROSTER = dependencies["PICKER_ROSTER"]
    log_inventory_transaction = dependencies["log_inventory_transaction"]
    get_best_inventory_location = dependencies["get_best_inventory_location"]
    create_supervisor_issue = dependencies["create_supervisor_issue"]
    build_issue_type = dependencies["build_issue_type"]
    SHORTAGE_ISSUE_TYPE = dependencies["SHORTAGE_ISSUE_TYPE"]
    shortage_issue_audit_id = dependencies["shortage_issue_audit_id"]
    calculate_sla_deadline = dependencies.get("calculate_sla_deadline")

    if not force and demo_baseline_present(conn):
        return {
            "seeded": False,
            "orders_created": DEMO_ORDER_COUNT,
            "reason": "baseline_already_present",
            "version": DEMO_SEED_VERSION,
        }

    now = now or datetime.now(PACIFIC_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=PACIFIC_TZ)

    cleared = clear_operational_tables(conn)
    c = conn.cursor()
    c.execute("DELETE FROM inventory")
    inventory_rows = seed_demo_inventory(conn, sku_count=120)

    c.execute(
        "SELECT DISTINCT sku FROM inventory WHERE sku IS NOT NULL AND TRIM(sku) <> '' ORDER BY CAST(sku AS INTEGER)"
    )
    skus = [row[0] for row in c.fetchall()]
    if len(skus) < 20:
        raise RuntimeError("Not enough SKUs to seed demo orders")

    rng = random.Random(82)
    destinations = list(DESTINATION_WAREHOUSES)
    pickers = list(PICKER_ROSTER)

    # Spread create dates across ~18 business days ending today.
    day_anchors = _business_days_back(now, 18)
    if not day_anchors:
        day_anchors = [now.replace(hour=6, minute=0, second=0, microsecond=0)]

    # Status plan (exactly 82): every order ends Completed so Orders Pending = 0.
    # Two orders keep resolved historical exceptions (quality fail + shortage) for realism.
    status_plan = ["Completed"] * DEMO_ORDER_COUNT
    assert len(status_plan) == DEMO_ORDER_COUNT

    # Urgency mix ~45% Standard / 35% Urgent / 20% Critical
    urgency_plan = (
        ["Standard"] * 37
        + ["Urgent"] * 29
        + ["Critical"] * 16
    )
    rng.shuffle(urgency_plan)

    late_otif_indexes = {0, 1, 2}  # intentional late completions for OTIF history
    multi_picker_indexes = set(range(3, 28))
    today_completed_slots = {77, 78, 79, 80, 81}  # 5 shipped "today"
    quality_history_index = 80  # Completed after fail → resolve → pass
    shortage_history_index = 81  # Completed after shortage → restock → ship

    # Shortage SKU starts empty so the historical SHORTAGE event is realistic.
    shortage_sku = skus[-1]
    c.execute(
        "UPDATE inventory SET quantity = 0 WHERE sku = ? AND warehouse = ?",
        (shortage_sku, SOURCE_WAREHOUSE),
    )

    summary = {
        "seeded": True,
        "version": DEMO_SEED_VERSION,
        "orders_created": 0,
        "completed": 0,
        "active": 0,
        "audits_passed": 0,
        "audits_failed": 0,
        "quality_issues": 0,
        "blocked": 0,
        "inventory_rows": inventory_rows,
        "cleared": cleared,
        "urgency_counts": {"Standard": 0, "Urgent": 0, "Critical": 0},
        "status_counts": {},
        "pending": 0,
    }

    for index in range(DEMO_ORDER_COUNT):
        status = "Completed"
        urgency = urgency_plan[index]
        if index in (quality_history_index, shortage_history_index):
            urgency = "Standard"

        summary["urgency_counts"][urgency] = summary["urgency_counts"].get(urgency, 0) + 1
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1

        order_number = f"{DEMO_ORDER_PREFIX}{index + 1:04d}"
        request_id = f"REQ-DT82-{index + 1:04d}"
        destination = destinations[index % len(destinations)]
        primary_picker = pickers[index % len(pickers)]
        secondary_picker = pickers[(index + 3) % len(pickers)]

        line_count = [1, 2, 3, 4][index % 4]
        if index == shortage_history_index:
            selected = [shortage_sku]
            if line_count > 1:
                selected.append(skus[(index * 3) % (len(skus) - 1)])
        else:
            usable = skus[:-1]
            start = (index * 5) % len(usable)
            selected = [usable[(start + offset) % len(usable)] for offset in range(line_count)]

        lines: list[tuple[str, int]] = []
        for offset, sku in enumerate(selected):
            qty = 1 + ((index + offset * 7) % 9)
            lines.append((sku, qty))
        expected_quantity = sum(q for _, q in lines)

        if index in today_completed_slots:
            day = day_anchors[-1]
            minute_offset = 45 + (index % 8) * 17
            order_time = day.replace(hour=7, minute=0) + timedelta(minutes=minute_offset)
        else:
            day = day_anchors[index % len(day_anchors)]
            minute_offset = 20 + (index * 13) % (9 * 60)
            order_time = day + timedelta(minutes=minute_offset)

        if order_time.hour < 6:
            order_time = order_time.replace(hour=6, minute=15 + (index % 20))
        if order_time.hour >= 17:
            order_time = order_time.replace(hour=15, minute=10 + (index % 40))

        if urgency == "Critical":
            pick_span = 12 + (index * 3) % 28
            close_extra = 4 + (index % 8)
            start_delay = 5 + (index % 8)
        elif urgency == "Urgent":
            pick_span = 18 + (index * 5) % 55
            close_extra = 6 + (index % 12)
            start_delay = 8 + (index % 12)
        else:
            pick_span = 25 + (index * 7) % 95
            close_extra = 8 + (index * 3) % 22
            start_delay = 12 + (index % 18)
        start_time = order_time + timedelta(minutes=start_delay)

        responsibility = "Closed"
        _insert_order(
            conn,
            order_number=order_number,
            request_id=request_id,
            order_time=order_time,
            destination=destination,
            urgency=urgency,
            status=status,
            responsibility=responsibility,
            expected_quantity=expected_quantity,
            picked_quantity=0,
            source=SOURCE_WAREHOUSE,
        )
        _insert_lines(conn, order_number, lines)

        if index == shortage_history_index:
            # Historical block: SHORTAGE while empty → restock → finish pick → ship.
            non_shortage = [(sku, qty) for sku, qty in lines if sku != shortage_sku]
            picked_quantity = 0
            if non_shortage:
                picked_quantity = _seed_pick_lifecycle(
                    conn,
                    log_inventory_transaction=log_inventory_transaction,
                    get_best_inventory_location=get_best_inventory_location,
                    order_number=order_number,
                    lines=non_shortage,
                    source_wh=SOURCE_WAREHOUSE,
                    destination=destination,
                    primary_picker=primary_picker,
                    secondary_picker=None,
                    start_time=start_time,
                    pick_span_minutes=20,
                    close_extra_minutes=5,
                    pick_fraction=1.0,
                )
            else:
                _log_tx(
                    log_inventory_transaction,
                    conn,
                    tx_code="PICK_START",
                    order_number=order_number,
                    sku="N/A",
                    warehouse=SOURCE_WAREHOUSE,
                    location="N/A",
                    qty_change=0,
                    qty_before=0,
                    qty_after=0,
                    user_role=primary_picker,
                    notes="Demo seed pick start",
                    tx_time=start_time,
                )

            shortage_time = start_time + timedelta(minutes=28)
            wh, loc, on_hand = get_best_inventory_location(conn, shortage_sku, SOURCE_WAREHOUSE)
            required = next(qty for sku, qty in lines if sku == shortage_sku)
            log_inventory_transaction(
                conn,
                "SHORTAGE",
                order_number,
                shortage_sku,
                wh,
                loc,
                0,
                int(on_hand or 0),
                int(on_hand or 0),
                "Inventory Team",
                json.dumps(
                    {
                        "required_qty": int(required),
                        "on_hand_qty": int(on_hand or 0),
                        "reason": "No inventory on hand for the remaining requirement",
                    }
                ),
                tx_time=shortage_time,
            )
            audit_id = shortage_issue_audit_id(order_number, shortage_sku)
            issue_id = create_supervisor_issue(
                conn=conn,
                order_number=order_number,
                audit_id=audit_id,
                issue_date=_iso(shortage_time),
                issue_type=SHORTAGE_ISSUE_TYPE,
                part_snapshot_override=(
                    f"{shortage_sku} remaining {required} unit(s) at source / {loc or 'Unassigned'}"
                ),
                inspector_notes=(
                    f"No inventory on hand for the remaining requirement. "
                    f"Remaining {required} unit(s); on hand {int(on_hand or 0)} unit(s). "
                    f"Notification submitted by {primary_picker}."
                ),
            )
            restock_qty = max(required * 4, 40)
            restock_time = shortage_time + timedelta(minutes=35)
            restock_loc = loc if loc and loc != "N/A" else "A-01-01"
            c.execute(
                """
                SELECT quantity FROM inventory
                WHERE sku = ? AND warehouse = ? AND location = ?
                """,
                (shortage_sku, SOURCE_WAREHOUSE, restock_loc),
            )
            existing_row = c.fetchone()
            qty_before = int(existing_row[0] or 0) if existing_row else 0
            if existing_row:
                c.execute(
                    """
                    UPDATE inventory SET quantity = quantity + ?
                    WHERE sku = ? AND warehouse = ? AND location = ?
                    """,
                    (restock_qty, shortage_sku, SOURCE_WAREHOUSE, restock_loc),
                )
            else:
                c.execute(
                    """
                    INSERT INTO inventory (sku, warehouse, location, quantity, price)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (shortage_sku, SOURCE_WAREHOUSE, restock_loc, restock_qty, 12.5),
                )
            log_inventory_transaction(
                conn,
                "RECEIPT",
                order_number,
                shortage_sku,
                SOURCE_WAREHOUSE,
                restock_loc,
                restock_qty,
                qty_before,
                qty_before + restock_qty,
                "Inventory Control",
                "Demo seed replenishment after shortage escalation",
                tx_time=restock_time,
            )
            shortage_pick_time = restock_time + timedelta(minutes=12)
            _consume_pick(
                conn,
                log_inventory_transaction=log_inventory_transaction,
                get_best_inventory_location=get_best_inventory_location,
                order_number=order_number,
                sku=shortage_sku,
                qty=required,
                source_wh=SOURCE_WAREHOUSE,
                picker=primary_picker,
                tx_time=shortage_pick_time,
                notes=f"Pick transaction for destination {destination}",
            )
            picked_quantity += required
            close_time = shortage_pick_time + timedelta(minutes=8)
            _log_tx(
                log_inventory_transaction,
                conn,
                tx_code="PICK_CLOSE",
                order_number=order_number,
                sku="N/A",
                warehouse=SOURCE_WAREHOUSE,
                location="N/A",
                qty_change=0,
                qty_before=picked_quantity,
                qty_after=picked_quantity,
                user_role=primary_picker,
                notes="Pick complete after replenishment — moved to quality verification",
                tx_time=close_time,
            )
            c.execute(
                "UPDATE order_header SET picked_quantity = ? WHERE order_number = ?",
                (picked_quantity, order_number),
            )
            audit_time = close_time + timedelta(minutes=15)
            pass_audit_id = f"AUD-{order_number}-PASS"
            c.execute(
                """
                INSERT INTO quality_audits (
                    audit_id, audit_time, order_number, part_match, damage, qty_match,
                    result, auditor_role, discrepancy_type, inspector_notes, discrepancy_summary
                )
                VALUES (?, ?, ?, 'Yes', 'No', 'Yes', 'Passed', 'Quality', '', '', '')
                """,
                (pass_audit_id, _iso(audit_time), order_number),
            )
            resolve_time = audit_time + timedelta(minutes=5)
            c.execute(
                """
                UPDATE supervisor_quality_issues
                SET issue_status = 'Resolved',
                    resolved_flag = 1,
                    resolved_at = ?,
                    closed_at = ?,
                    assigned_to = 'Inventory Control',
                    resolution_notes = ?,
                    last_updated_at = ?
                WHERE issue_id = ?
                """,
                (
                    _iso(resolve_time),
                    _iso(resolve_time),
                    "Replenished source bin and completed shipment.",
                    _iso(resolve_time),
                    issue_id,
                ),
            )
            summary["audits_passed"] += 1
            summary["blocked"] += 1  # historical shortage exception (now resolved)
            summary["completed"] += 1

        elif index == quality_history_index:
            # Historical quality fail → supervisor resolve → re-audit pass → Completed.
            picked_quantity = _seed_pick_lifecycle(
                conn,
                log_inventory_transaction=log_inventory_transaction,
                get_best_inventory_location=get_best_inventory_location,
                order_number=order_number,
                lines=lines,
                source_wh=SOURCE_WAREHOUSE,
                destination=destination,
                primary_picker=primary_picker,
                secondary_picker=None,
                start_time=start_time,
                pick_span_minutes=pick_span,
                close_extra_minutes=close_extra,
                pick_fraction=1.0,
            )
            c.execute(
                "UPDATE order_header SET picked_quantity = ? WHERE order_number = ?",
                (picked_quantity, order_number),
            )
            fail_time = start_time + timedelta(minutes=pick_span + close_extra + 20)
            fail_audit_id = f"AUD-{order_number}-FAIL"
            part_match, damage, qty_match = "Yes", "Yes", "Yes"
            c.execute(
                """
                INSERT INTO quality_audits (
                    audit_id, audit_time, order_number, part_match, damage, qty_match,
                    result, auditor_role, discrepancy_type, inspector_notes, discrepancy_summary
                )
                VALUES (?, ?, ?, ?, ?, ?, 'Failed', 'Quality', ?, ?, ?)
                """,
                (
                    fail_audit_id,
                    _iso(fail_time),
                    order_number,
                    part_match,
                    damage,
                    qty_match,
                    "Damage",
                    "Carton corner crush found during verification.",
                    "Flagged discrepancy: Damage || Inspector notes: Carton corner crush found during verification.",
                ),
            )
            issue_id = create_supervisor_issue(
                conn=conn,
                order_number=order_number,
                audit_id=fail_audit_id,
                issue_date=_iso(fail_time),
                issue_type=build_issue_type(part_match, damage, qty_match),
                inspector_notes="Carton corner crush found during verification.",
            )
            rework_time = fail_time + timedelta(minutes=40)
            pass_audit_id = f"AUD-{order_number}-PASS"
            c.execute(
                """
                INSERT INTO quality_audits (
                    audit_id, audit_time, order_number, part_match, damage, qty_match,
                    result, auditor_role, discrepancy_type, inspector_notes, discrepancy_summary
                )
                VALUES (?, ?, ?, 'Yes', 'No', 'Yes', 'Passed', 'Quality', '', '', '')
                """,
                (pass_audit_id, _iso(rework_time), order_number),
            )
            resolve_time = rework_time + timedelta(minutes=8)
            c.execute(
                """
                UPDATE supervisor_quality_issues
                SET issue_status = 'Resolved',
                    resolved_flag = 1,
                    resolved_at = ?,
                    closed_at = ?,
                    assigned_to = 'Supervisor',
                    resolution_notes = ?,
                    corrective_action = ?,
                    last_updated_at = ?
                WHERE issue_id = ?
                """,
                (
                    _iso(resolve_time),
                    _iso(resolve_time),
                    "Repacked damaged carton; re-audit passed; order shipped.",
                    "Repack and re-verify",
                    _iso(resolve_time),
                    issue_id,
                ),
            )
            summary["audits_failed"] += 1
            summary["audits_passed"] += 1
            summary["quality_issues"] += 1  # historical, now resolved
            summary["completed"] += 1

        else:
            use_secondary = index in multi_picker_indexes
            picked_quantity = _seed_pick_lifecycle(
                conn,
                log_inventory_transaction=log_inventory_transaction,
                get_best_inventory_location=get_best_inventory_location,
                order_number=order_number,
                lines=lines,
                source_wh=SOURCE_WAREHOUSE,
                destination=destination,
                primary_picker=primary_picker,
                secondary_picker=secondary_picker if use_secondary else None,
                start_time=start_time,
                pick_span_minutes=pick_span,
                close_extra_minutes=close_extra,
                pick_fraction=1.0,
            )
            c.execute(
                "UPDATE order_header SET picked_quantity = ? WHERE order_number = ?",
                (picked_quantity, order_number),
            )

            natural_audit = start_time + timedelta(minutes=pick_span + close_extra + 10 + (index % 25))
            if natural_audit < order_time:
                natural_audit = order_time + timedelta(minutes=45)

            if index in late_otif_indexes:
                if urgency == "Critical":
                    audit_time = start_time + timedelta(hours=5, minutes=10 + (index % 20))
                elif urgency == "Urgent":
                    audit_time = start_time + timedelta(hours=8, minutes=15 + (index % 25))
                else:
                    audit_time = order_time + timedelta(days=4, hours=2)
            else:
                deadline = None
                if calculate_sla_deadline is not None:
                    try:
                        deadline = calculate_sla_deadline(order_time, urgency)
                    except Exception:
                        deadline = None
                if deadline is not None and natural_audit > deadline:
                    audit_time = deadline - timedelta(minutes=8 + (index % 7))
                    if audit_time <= order_time:
                        audit_time = order_time + timedelta(minutes=20)
                else:
                    audit_time = natural_audit

            audit_id = f"AUD-{order_number}-PASS"
            c.execute(
                """
                INSERT INTO quality_audits (
                    audit_id, audit_time, order_number, part_match, damage, qty_match,
                    result, auditor_role, discrepancy_type, inspector_notes, discrepancy_summary
                )
                VALUES (?, ?, ?, 'Yes', 'No', 'Yes', 'Passed', 'Quality', '', '', '')
                """,
                (audit_id, _iso(audit_time), order_number),
            )
            summary["audits_passed"] += 1
            summary["completed"] += 1

        summary["orders_created"] += 1

    # Guardrails — recruiter baseline must be zero-pending.
    c.execute("SELECT COUNT(*) FROM order_header")
    total_orders = int(c.fetchone()[0] or 0)
    if total_orders != DEMO_ORDER_COUNT:
        raise RuntimeError(f"Expected {DEMO_ORDER_COUNT} orders, found {total_orders}")

    c.execute("SELECT COUNT(*) FROM order_header WHERE status != 'Completed'")
    pending = int(c.fetchone()[0] or 0)
    if pending:
        raise RuntimeError(f"Expected 0 pending orders after seed, found {pending}")

    c.execute("SELECT COUNT(*) FROM quality_audits")
    audits_total = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result IN ('Pass', 'Passed')")
    audits_passed = int(c.fetchone()[0] or 0)
    pass_rate = (audits_passed / audits_total * 100.0) if audits_total else 0.0
    if pass_rate <= 98.0:
        raise RuntimeError(f"Quality audit pass rate must be >98%, got {pass_rate:.1f}%")

    c.execute(
        """
        SELECT COUNT(*) FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
        """
    )
    open_issues = int(c.fetchone()[0] or 0)
    if open_issues:
        raise RuntimeError(f"Expected 0 open supervisor issues, found {open_issues}")

    c.execute("SELECT COUNT(*) FROM inventory WHERE quantity < 0")
    negatives = int(c.fetchone()[0] or 0)
    if negatives:
        raise RuntimeError(f"Negative inventory rows after seed: {negatives}")

    conn.commit()
    summary["total_orders"] = total_orders
    summary["pending"] = pending
    summary["pass_rate"] = round(pass_rate, 1)
    summary["shortage_sku"] = shortage_sku
    return summary


def ensure_demo_order_baseline(conn, *, force: bool = False, dependencies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ensure master/session connection has the 82-order baseline."""
    return seed_demo_orders_82(conn, force=force, dependencies=dependencies)


def build_whs_dependencies(module) -> dict[str, Any]:
    return {
        "seed_demo_inventory": module.seed_demo_inventory,
        "SOURCE_WAREHOUSE": module.SOURCE_WAREHOUSE,
        "DESTINATION_WAREHOUSES": module.DESTINATION_WAREHOUSES,
        "PICKER_ROSTER": module.PICKER_ROSTER,
        "log_inventory_transaction": module.log_inventory_transaction,
        "get_best_inventory_location": module.get_best_inventory_location,
        "create_supervisor_issue": module.create_supervisor_issue,
        "build_issue_type": module.build_issue_type,
        "SHORTAGE_ISSUE_TYPE": module.SHORTAGE_ISSUE_TYPE,
        "shortage_issue_audit_id": module.shortage_issue_audit_id,
        "calculate_sla_deadline": getattr(module, "calculate_sla_deadline", None),
    }
