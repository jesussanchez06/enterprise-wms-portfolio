from flask import Flask, request, redirect, render_template_string, send_from_directory, Response, session, has_request_context
import sqlite3
from datetime import datetime
import pandas as pd
import os
import io
import base64
import shutil
from datetime import timedelta
from uuid import uuid4
import random
import json
import matplotlib
import socket
import subprocess
import time
import signal
import re
from urllib.parse import urlencode
from html import escape as html_escape
from werkzeug.utils import secure_filename
from zoneinfo import ZoneInfo

matplotlib.use('Agg')
import matplotlib.pyplot as plt

# register adapter so sqlite stores datetimes as ISO strings (avoids
# Python 3.12 deprecation warning about the default adapter)
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_DB = os.path.join(BASE_DIR, "enterprise_wms_master.db")
LEGACY_DB = os.path.join(BASE_DIR, "enterprise_wms.db")
SESSION_DB_DIR = os.path.join(BASE_DIR, "demo_sessions")
# Backward-compatible alias used by older call sites / docs.
DB = MASTER_DB


def ensure_session_db_dir():
    os.makedirs(SESSION_DB_DIR, exist_ok=True)


def migrate_legacy_db_to_master():
    """Promote the original shared DB into the master seed if needed."""
    if os.path.exists(MASTER_DB):
        return False
    if os.path.exists(LEGACY_DB):
        shutil.copy2(LEGACY_DB, MASTER_DB)
        return True
    return False


def _remove_sqlite_file(path):
    """Delete a SQLite DB and its WAL/SHM sidecars if present."""
    removed = 0
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
                removed += 1
            except OSError:
                pass
    return removed


def purge_demo_session_databases():
    """Delete every visitor clone so the next request reclones from master."""
    ensure_session_db_dir()
    removed = 0
    for name in os.listdir(SESSION_DB_DIR):
        if not (name.endswith(".db") or name.endswith(".db-wal") or name.endswith(".db-shm")):
            continue
        path = os.path.join(SESSION_DB_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def sqlite_order_count(db_path):
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        try:
            row = conn.execute("SELECT COUNT(*) FROM order_header").fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0
        finally:
            conn.close()
    except Exception:
        return 0


def db_has_demo_baseline(db_path):
    """True when the DB has the exact tagged 82-order recruiter baseline."""
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        from wms_demo_seed import demo_baseline_present

        conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        try:
            return bool(demo_baseline_present(conn))
        finally:
            conn.close()
    except Exception:
        return False


def get_demo_session_id():
    session_id = session.get("demo_session_id")
    if not session_id:
        session_id = uuid4().hex
        session["demo_session_id"] = session_id
    session.permanent = True
    return session_id


def session_db_path(session_id=None):
    ensure_session_db_dir()
    resolved_id = session_id or get_demo_session_id()
    return os.path.join(SESSION_DB_DIR, f"{resolved_id}.db")


def clone_master_database(dest_path):
    ensure_session_db_dir()
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(MASTER_DB):
        # Master will be created by init_db/startup; create empty file as fallback.
        open(MASTER_DB, "a").close()

    source = sqlite3.connect(MASTER_DB, timeout=10, check_same_thread=False)
    try:
        destination = sqlite3.connect(dest_path, timeout=10, check_same_thread=False)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()


def ensure_visitor_session_db(force_reset=False):
    """Clone the shared master seed into an isolated per-visitor SQLite file.

    Aggressively reclones when this browser still has a pre-82-order stale DB.
    """
    path = session_db_path()
    stale_session = False
    if os.path.exists(path) and not force_reset:
        # Cookie still points at an old clone (0–2 orders) while master has the
        # recruiter baseline — heal automatically so the dashboard updates without
        # requiring a manual Reset Demo hunt.
        if db_has_demo_baseline(MASTER_DB) and not db_has_demo_baseline(path):
            stale_session = True
            force_reset = True

    if force_reset and os.path.exists(path):
        _remove_sqlite_file(path)

    if not os.path.exists(path):
        if not db_has_demo_baseline(MASTER_DB):
            # Last-resort heal: rebuild master before cloning into a visitor session.
            ensure_bootstrap_demo_data(force_orders=True)
        clone_master_database(path)
        if stale_session:
            debug_log(
                "system.reclone_stale_demo_session",
                session_id=session.get("demo_session_id", ""),
                orders=sqlite_order_count(path),
            )
    return path


def resolve_db_path(use_master=False):
    if use_master:
        return MASTER_DB
    if has_request_context():
        return ensure_visitor_session_db()
    return MASTER_DB


# helper to centralize connection settings (timeout + thread sharing)
def get_conn(use_master=False):
    db_path = resolve_db_path(use_master=use_master)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    ensure_runtime_schema(conn)
    return conn


def reset_visitor_demo_data():
    """Restore this browser visitor to the original sample warehouse baseline."""
    # Ensure shared master is the 82-order baseline before recloning.
    ensure_bootstrap_demo_data(force_orders=False)
    if not db_has_demo_baseline(MASTER_DB):
        ensure_bootstrap_demo_data(force_orders=True)
    ensure_visitor_session_db(force_reset=True)
    return {
        "reset": True,
        "session_id": session.get("demo_session_id", ""),
        "orders": sqlite_order_count(session_db_path()),
    }


def get_runtime_config(default_port):
    port = int(os.environ.get("PORT", str(default_port)))
    host = os.environ.get("HOST", "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    return host, port, debug


def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def get_other_wms_process_ids(script_path):
    current_pid = os.getpid()
    script_name_pattern = re.escape(os.path.basename(script_path))

    if os.name == "nt":
        command = (
            f"$currentPid = {current_pid}; "
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -match 'python(\\.exe)?' -and $_.CommandLine -match '{script_name_pattern}' -and $_.ProcessId -ne $currentPid }} | "
            f"ForEach-Object {{ $_.ProcessId }}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]

    result = subprocess.run(
        ["pgrep", "-f", f"python .*{script_name_pattern}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit() and int(line.strip()) != current_pid]


def terminate_other_wms_processes(script_path):
    other_pids = get_other_wms_process_ids(script_path)
    terminated_pids = []

    for pid in other_pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True, check=False)
            else:
                os.kill(pid, signal.SIGTERM)
            terminated_pids.append(pid)
        except OSError:
            continue

    if terminated_pids and os.name != "nt":
        deadline = time.time() + 3
        while time.time() < deadline:
            still_running = []
            for pid in terminated_pids:
                try:
                    os.kill(pid, 0)
                    still_running.append(pid)
                except OSError:
                    continue
            if not still_running:
                break
            time.sleep(0.2)

    return terminated_pids


def prepare_runtime_port(host, preferred_port, script_path):
    if is_port_available(host, preferred_port):
        return preferred_port

    terminated_pids = terminate_other_wms_processes(script_path)
    if terminated_pids:
        print(
            "Stopped stale DigiTech WMS process(es): "
            + ", ".join(str(pid) for pid in terminated_pids)
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            if is_port_available(host, preferred_port):
                return preferred_port
            time.sleep(0.2)

    return find_available_port(host, preferred_port)


def find_available_port(host, preferred_port, max_attempts=20):
    for port in range(preferred_port, preferred_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"No available port found between {preferred_port} and {preferred_port + max_attempts - 1}."
    )


def log_inventory_transaction(conn, tx_code, order_number, sku, warehouse, location,
                              qty_change, qty_before, qty_after, user_role, notes="",
                              tx_time=None):
    c = conn.cursor()
    resolved_tx_time = tx_time.isoformat() if isinstance(tx_time, datetime) else (
        clean_display_text(tx_time, "") or now_pt().isoformat()
    )
    c.execute("""
        INSERT INTO inventory_transactions (
            tx_time,
            tx_code,
            order_number,
            sku,
            warehouse,
            location,
            qty_change,
            qty_before,
            qty_after,
            user_role,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        resolved_tx_time,
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
    ))


def log_inventory_shortage_warning(conn, order_number, sku, warehouse, location, required_qty, on_hand_qty, reason):
    log_inventory_transaction(
        conn=conn,
        tx_code="SHORTAGE",
        order_number=order_number,
        sku=sku,
        warehouse=clean_display_text(warehouse, "Warehouse Unknown"),
        location=clean_display_text(location, "Unassigned"),
        qty_change=0,
        qty_before=int(on_hand_qty or 0),
        qty_after=int(on_hand_qty or 0),
        user_role="Inventory Team",
        notes=json.dumps({
            "required_qty": int(required_qty or 0),
            "on_hand_qty": int(on_hand_qty or 0),
            "reason": clean_display_text(reason, "Insufficient inventory"),
        }),
    )


def generate_audit_id(order_number):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"AUD-{order_number}-{stamp}-{uuid4().hex[:6].upper()}"


def clean_display_text(value, fallback=""):
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat", ""}:
        return fallback
    return text


def warehouse_code_label(value, default_label="Warehouse Unknown"):
    text = clean_display_text(value, "")
    code = "".join(ch for ch in text if ch.isdigit())
    if code:
        return f"Warehouse {code}"
    return default_label


def build_warehouse_filters(source_wh):
    source_text = clean_display_text(source_wh, SOURCE_WAREHOUSE)
    source_code = "".join(ch for ch in source_text if ch.isdigit())
    source_city = source_text.split("Warehouse")[0].strip() if "Warehouse" in source_text else source_text
    return (
        source_text,
        f"%{source_text}%" if source_text else "%NO_SOURCE_MATCH%",
        f"%{source_city}%" if source_city else "%NO_CITY_MATCH%",
        f"%{source_code}%" if source_code else "%NO_CODE_MATCH%",
    )


def get_best_inventory_location(conn, sku, source_wh):
    c = conn.cursor()
    source_text, source_text_like, source_city_like, source_code_like = build_warehouse_filters(source_wh)
    c.execute(
        """
        SELECT warehouse, location, quantity
        FROM inventory
        WHERE sku = ?
        ORDER BY
            CASE
                WHEN warehouse = ? THEN 0
                WHEN warehouse LIKE ? THEN 1
                WHEN warehouse LIKE ? THEN 2
                WHEN warehouse LIKE ? THEN 3
                ELSE 4
            END,
            quantity DESC,
            location ASC
        LIMIT 1
        """,
        (sku, source_text, source_text_like, source_city_like, source_code_like),
    )
    row = c.fetchone()
    if not row:
        return "Warehouse Unknown", "Unassigned", 0

    warehouse, location, quantity = row
    return (
        clean_display_text(warehouse, "Warehouse Unknown"),
        clean_display_text(location, "Unassigned"),
        int(quantity or 0),
    )


def get_available_inventory_qty(conn, sku, source_wh=None):
    """Total on-hand quantity for a SKU at the source warehouse (planner ATP check)."""
    c = conn.cursor()
    warehouse = clean_display_text(source_wh, SOURCE_WAREHOUSE) or SOURCE_WAREHOUSE
    source_text, source_text_like, source_city_like, source_code_like = build_warehouse_filters(warehouse)
    c.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM inventory
        WHERE sku = ?
          AND (
            warehouse = ?
            OR warehouse LIKE ?
            OR warehouse LIKE ?
            OR warehouse LIKE ?
          )
        """,
        (sku, source_text, source_text_like, source_city_like, source_code_like),
    )
    row = c.fetchone()
    return int(row[0] or 0) if row else 0


def build_inventory_action_url(sku, warehouse="", location=""):
    params = {"sku": sku}
    warehouse_text = clean_display_text(warehouse, "")
    location_text = clean_display_text(location, "")

    if warehouse_text not in {"", "Warehouse Unknown"} and location_text not in {"", "Unassigned"}:
        params.update({
            "adjust_sku": sku,
            "adjust_warehouse": warehouse_text,
            "adjust_location": location_text,
        })
        return f"/inventory?{urlencode(params)}#adjust-inventory"

    return f"/inventory?{urlencode(params)}"


def sku_semiconductor_description(sku):
    sku_text = clean_display_text(sku, "")
    numeric_portion = "".join(ch for ch in sku_text if ch.isdigit())
    sku_number = int(numeric_portion) if numeric_portion else 0

    families = [
        "Wafer Handling Robot Arm",
        "Photolithography Mask Carrier",
        "Etch Chamber Seal Kit",
        "Deposition Gas Delivery Valve",
        "Ion Implant Beamline Sensor",
        "CMP Slurry Filter Assembly",
        "Plasma RF Match Network",
        "Metrology Alignment Target",
        "FOUP Door Interface Module",
        "Thermal Process Quartz Liner",
    ]
    qualifiers = [
        "300mm line-ready",
        "cleanroom certified",
        "ESD-safe service part",
        "high-throughput tool spare",
        "preventive maintenance component",
        "fab utility control part",
    ]

    family = families[sku_number % len(families)]
    qualifier = qualifiers[(sku_number // len(families)) % len(qualifiers)]
    return f"{family} ({qualifier})"


def status_badge_html(status):
    palette = {
        "Orders Placed": ("#dbeafe", "#1d4ed8"),
        "Picking in Progress": ("#dbeafe", "#1e40af"),
        "Blocked": ("#fee2e2", "#b91c1c"),
        "Pending Verification": ("#fef3c7", "#b45309"),
        "Completed": ("#dcfce7", "#166534"),
        "Quality Issue": ("#fee2e2", "#b91c1c"),
    }
    bg_color, text_color = palette.get(status, ("#e2e8f0", "#334155"))
    return (
        f"<span style='display:inline-flex;align-items:center;padding:4px 10px;"
        f"border-radius:999px;background:{bg_color};color:{text_color};font-size:12px;"
        f"font-weight:600;'>{status}</span>"
    )
def responsibility_badge_html(responsibility):
    palette = {
        "Operations": ("#dbeafe", "#1d4ed8"),
        "Inventory": ("#ecfeff", "#0f766e"),
        "Quality": ("#fef3c7", "#b45309"),
        "Supervisor": ("#fee2e2", "#b91c1c"),
        "Closed": ("#dcfce7", "#166534"),
    }
    bg_color, text_color = palette.get(responsibility, ("#e2e8f0", "#334155"))
    return (
        f"<span style='display:inline-flex;align-items:center;padding:4px 10px;"
        f"border-radius:999px;background:{bg_color};color:{text_color};font-size:12px;"
        f"font-weight:600;'>{responsibility}</span>"
    )


def workflow_badge_html(workflow_status):
    palette = {
        "Open": ("#fee2e2", "#b91c1c"),
        "Investigating": ("#dbeafe", "#1d4ed8"),
        "Awaiting Operations": ("#fef3c7", "#b45309"),
        "Resolved": ("#dcfce7", "#166534"),
        "Closed": ("#e2e8f0", "#334155"),
    }
    bg_color, text_color = palette.get(workflow_status, ("#ede9fe", "#6d28d9"))
    return (
        f"<span style='display:inline-flex;align-items:center;padding:4px 10px;"
        f"border-radius:999px;background:{bg_color};color:{text_color};font-size:12px;"
        f"font-weight:600;'>{workflow_status}</span>"
    )


def sla_risk_badge_html(sla_key):
    palette = {
        "healthy": ("Healthy", "#dcfce7", "#166534"),
        "at_risk": ("At Risk", "#fef3c7", "#b45309"),
        "breached": ("Breached", "#fee2e2", "#b91c1c"),
    }
    label, bg_color, text_color = palette.get(sla_key, ("Unknown", "#e2e8f0", "#334155"))
    return (
        f"<span style='display:inline-flex;align-items:center;padding:4px 10px;"
        f"border-radius:999px;background:{bg_color};color:{text_color};font-size:12px;"
        f"font-weight:700;'>{label}</span>"
    )


def _load_business_holidays():
    holidays = set()
    raw_value = os.environ.get("BUSINESS_HOLIDAYS", "")
    for date_token in raw_value.split(","):
        date_token = date_token.strip()
        if not date_token:
            continue
        try:
            holidays.add(datetime.strptime(date_token, "%Y-%m-%d").date())
        except ValueError:
            continue
    return holidays


def _load_positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, value)


URGENCY_LABEL_MAP = {
    "critical": "Critical",
    "urgent": "Urgent",
    "standard": "Standard",
    "high": "Critical",
    "medium": "Urgent",
    "low": "Standard",
}
URGENCY_ORDER = ["Critical", "Urgent", "Standard"]

SLA_CONFIG = {
    "Critical": 2,
    "Urgent": 4,
}
STANDARD_SLA_BUSINESS_DAYS = 2
QUALITY_STANDARD_TARGET = 98.0

BUSINESS_DAY_START_HOUR = 6
BUSINESS_DAY_START_MINUTE = 0
BUSINESS_DAY_END_HOUR = 17
BUSINESS_DAY_END_MINUTE = 0
CUTOFF_HOUR = 14
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
BUSINESS_HOLIDAYS = _load_business_holidays()
DEFAULT_LOW_STOCK_ALERT_THRESHOLD = _load_positive_int_env("LOW_STOCK_ALERT_THRESHOLD", 25)


def calculate_sla_status(order_time, urgency, now=None):
    order_time = parse_order_datetime(order_time)
    reference_time = _normalize_reference_datetime(now)
    deadline = calculate_sla_deadline(order_time, urgency)
    sla_start = calculate_sla_start(order_time, urgency)

    if reference_time > deadline:
        return "breached", "Breached"

    total_window = max((deadline - sla_start).total_seconds(), 1)
    remaining_window = max((deadline - reference_time).total_seconds(), 0)
    at_risk_threshold = min(2 * 3600, total_window * 0.5)

    if remaining_window <= at_risk_threshold:
        return "at_risk", "At Risk"

    return "healthy", "Healthy"

SOURCE_WAREHOUSE = "San Diego Warehouse 100"
DESTINATION_WAREHOUSES = [
    "San Diego Warehouse 100",
    "Los Angeles Warehouse 200",
    "San Francisco Warehouse 300",
    "San Bernardino Warehouse 400",
]
WAREHOUSE_NETWORK = [
    "San Diego Warehouse 100",
    "Los Angeles Warehouse 200",
    "San Francisco Warehouse 300",
    "San Bernardino Warehouse 400",
]
WAREHOUSE_ALIASES = {
    "san diego": SOURCE_WAREHOUSE,
    "san diego warehouse": SOURCE_WAREHOUSE,
    "san diego warehouse 100": SOURCE_WAREHOUSE,
    "los angeles": "Los Angeles Warehouse 200",
    "los angeles warehouse": "Los Angeles Warehouse 200",
    "los angeles warehouse 200": "Los Angeles Warehouse 200",
    "san francisco": "San Francisco Warehouse 300",
    "san francisco warehouse": "San Francisco Warehouse 300",
    "san francisco warehouse 300": "San Francisco Warehouse 300",
    "san bernardino": "San Bernardino Warehouse 400",
    "san bernadino": "San Bernardino Warehouse 400",
    "san bernardino warehouse": "San Bernardino Warehouse 400",
    "san bernadino warehouse": "San Bernardino Warehouse 400",
    "san bernardino warehouse 400": "San Bernardino Warehouse 400",
    "san bernadino warehouse 400": "San Bernardino Warehouse 400",
}

ISSUE_ROOT_CAUSES = [
    "Picker Error",
    "Wrong Bin",
    "Inventory Inaccuracy",
    "Damage",
    "Labeling Issue",
    "System Issue",
]

ISSUE_WORKFLOW_STATES = [
    "Open",
    "Under Investigation",
    "Awaiting Inventory",
    "Resolved",
    "Closed",
]

BLOCKED_ORDER_STATUS = "Blocked"
SHORTAGE_ISSUE_TYPE = "Inventory Shortage"

ISSUE_ATTACHMENT_DIR = os.path.join(BASE_DIR, "issue_attachments")
DEFAULT_INVENTORY_WAREHOUSE = SOURCE_WAREHOUSE
DEFAULT_INVENTORY_LOCATION = "F01"
PICKER_ROSTER = [
    "Maria Alvarez",
    "John Carter",
    "Ava Patel",
    "Noah Kim",
    "Liam Brooks",
    "Sophia Nguyen",
    "Ethan Rivera",
    "Mia Thompson",
    "Lucas Chen",
    "Emma Foster",
]


def resolve_picker_identity(raw_picker):
    picker = clean_display_text(raw_picker, "")
    if picker in PICKER_ROSTER:
        return picker
    return PICKER_ROSTER[0] if PICKER_ROSTER else "Unassigned"


def is_generic_picker_identity(picker_name):
    return clean_display_text(picker_name, "") in {"", "Unassigned", "Operations"}


def format_picker_display_name(picker_name):
    if is_generic_picker_identity(picker_name):
        return "Needs Assignment"
    return clean_display_text(picker_name, "Needs Assignment")


def get_operations_board_state(order_status):
    return {
        "Orders Placed": "ready",
        "Picking in Progress": "in_progress",
        "Pending Verification": "completed",
        "Completed": "completed",
    }.get(clean_display_text(order_status, ""), "")


def build_operations_redirect_url(picker, message="", message_type="success"):
    params = {"picker": resolve_picker_identity(picker)}
    if clean_display_text(message, ""):
        params["ops_message"] = message
        params["ops_message_type"] = "warning" if message_type == "warning" else "success"
    return f"/operations?{urlencode(params)}"


def build_pick_screen_url(order_number, picker, message="", message_type="success"):
    params = {"picker": resolve_picker_identity(picker)}
    if clean_display_text(message, ""):
        params["pick_message"] = message
        params["pick_message_type"] = "warning" if message_type == "warning" else "success"
    return f"/pick_screen/{order_number}?{urlencode(params)}"


def parse_low_stock_threshold(raw_value, default=DEFAULT_LOW_STOCK_ALERT_THRESHOLD):
    try:
        threshold = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(threshold, 100000))


def summarize_resumed_orders(resumed_orders):
    if not resumed_orders:
        return ""

    sample_orders = ", ".join(resumed_orders[:3])
    extra_count = max(0, len(resumed_orders) - 3)
    extra_suffix = f" and {extra_count} more" if extra_count else ""
    return f" {sample_orders}{extra_suffix} moved back to the pick queue automatically."


def apply_inventory_adjustment(conn, sku, warehouse, location, qty_change, notes="", user_role="Inventory", order_number="MANUAL-ADJUST"):
    c = conn.cursor()
    c.execute(
        """
        SELECT rowid, quantity
        FROM inventory
        WHERE sku = ? AND warehouse = ? AND location = ?
        ORDER BY rowid
        LIMIT 1
        """,
        (sku, warehouse, location),
    )
    row = c.fetchone()

    if not row:
        if qty_change < 0:
            raise ValueError("The selected SKU, warehouse, and location combination was not found.")

        c.execute(
            """
            SELECT ROUND(AVG(price), 2)
            FROM inventory
            WHERE sku = ?
            """,
            (sku,),
        )
        avg_price_row = c.fetchone()
        unit_price = float(avg_price_row[0] or 0)
        qty_before = 0
        qty_after = qty_change
        c.execute(
            """
            INSERT INTO inventory (sku, warehouse, location, quantity, price)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sku, warehouse, location, qty_after, unit_price),
        )
        created = True
    else:
        row_id, qty_before = row
        qty_before = int(qty_before)
        qty_after = qty_before + qty_change
        if qty_after < 0:
            raise ValueError("Adjustment would create a negative on-hand balance, so it was not posted.")
        c.execute("UPDATE inventory SET quantity = ? WHERE rowid = ?", (qty_after, row_id))
        created = False

    log_inventory_transaction(
        conn=conn,
        tx_code="ADJUST",
        order_number=order_number,
        sku=sku,
        warehouse=warehouse,
        location=location,
        qty_change=qty_change,
        qty_before=int(qty_before),
        qty_after=int(qty_after),
        user_role=user_role,
        notes=notes,
    )

    return {
        "created": created,
        "qty_before": int(qty_before),
        "qty_after": int(qty_after),
        "qty_change": int(qty_change),
    }


def has_valid_inventory_assignment(value):
    text = clean_display_text(value, "")
    return text != ""


def canonicalize_warehouse_name(value, fallback=SOURCE_WAREHOUSE):
    text = clean_display_text(value, "")
    if not text:
        return fallback
    if text in WAREHOUSE_NETWORK:
        return text
    return WAREHOUSE_ALIASES.get(text.lower(), fallback)


def normalize_inventory_assignments(conn):
    c = conn.cursor()
    c.execute(
        """
        UPDATE inventory
        SET warehouse = ?,
            location = ?
        WHERE quantity > 0
          AND (
              TRIM(COALESCE(warehouse, '')) = ''
              OR LOWER(TRIM(COALESCE(warehouse, ''))) = 'nan'
              OR TRIM(COALESCE(location, '')) = ''
              OR LOWER(TRIM(COALESCE(location, ''))) = 'nan'
          )
        """,
        (DEFAULT_INVENTORY_WAREHOUSE, DEFAULT_INVENTORY_LOCATION),
    )
    updated_rows = c.rowcount

    # Collapse misspellings / short labels into the canonical 4-warehouse network.
    c.execute(
        """
        SELECT DISTINCT warehouse
        FROM inventory
        WHERE TRIM(COALESCE(warehouse, '')) <> ''
        """
    )
    for (warehouse_name,) in c.fetchall():
        canonical = canonicalize_warehouse_name(warehouse_name, warehouse_name)
        if canonical != warehouse_name and canonical in WAREHOUSE_NETWORK:
            c.execute(
                "UPDATE inventory SET warehouse = ? WHERE warehouse = ?",
                (canonical, warehouse_name),
            )
            updated_rows += c.rowcount

    return updated_rows


def ensure_quality_audit_schema(conn):
    c = conn.cursor()
    c.execute("PRAGMA table_info(quality_audits)")
    existing_columns = {row[1] for row in c.fetchall()}

    required_columns = {
        "discrepancy_type": "TEXT",
        "inspector_notes": "TEXT",
        "discrepancy_summary": "TEXT",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            c.execute(f"ALTER TABLE quality_audits ADD COLUMN {column_name} {column_type}")


def ensure_order_header_pick_tracking_schema(conn):
    c = conn.cursor()
    c.execute("PRAGMA table_info(order_header)")
    existing_columns = {row[1] for row in c.fetchall()}
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in c.fetchall()}

    required_columns = {
        "expected_quantity": "INTEGER DEFAULT 0",
        "picked_quantity": "INTEGER DEFAULT 0",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            c.execute(f"ALTER TABLE order_header ADD COLUMN {column_name} {column_type}")

    if "order_lines" in existing_tables:
        c.execute(
            """
            UPDATE order_header
            SET expected_quantity = COALESCE(
                    (
                        SELECT SUM(quantity)
                        FROM order_lines
                        WHERE order_number = order_header.order_number
                    ),
                    0
                )
            WHERE EXISTS(
                    SELECT 1
                    FROM order_lines
                    WHERE order_number = order_header.order_number
                )
            """
        )

    if "inventory_transactions" in existing_tables:
        c.execute(
            """
            UPDATE order_header
            SET picked_quantity = MIN(
                    COALESCE(expected_quantity, 0),
                    CASE
                        WHEN EXISTS(
                            SELECT 1
                            FROM inventory_transactions
                            WHERE order_number = order_header.order_number
                              AND tx_code = 'PICK'
                        )
                            THEN COALESCE(
                                (
                                    SELECT COALESCE(SUM(-qty_change), 0)
                                    FROM inventory_transactions
                                    WHERE order_number = order_header.order_number
                                      AND tx_code = 'PICK'
                                ),
                                0
                            )
                        ELSE COALESCE(picked_quantity, 0)
                    END
                )
            """
                )

        c.execute(
            """
            UPDATE order_header
            SET picked_quantity = 0
            WHERE status IN ('Orders Placed', 'Picking in Progress', 'Blocked')
              AND NOT EXISTS(
                    SELECT 1
                    FROM inventory_transactions
                    WHERE order_number = order_header.order_number
                      AND tx_code = 'PICK'
                )
              AND COALESCE(picked_quantity, 0) > 0
            """
        )

    c.execute(
        """
        UPDATE order_header
        SET picked_quantity = COALESCE(expected_quantity, 0)
        WHERE status IN ('Pending Verification', 'Completed', 'Quality Issue')
          AND COALESCE(expected_quantity, 0) > 0
          AND COALESCE(picked_quantity, 0) < COALESCE(expected_quantity, 0)
        """
    )

    c.execute(
        """
        UPDATE order_header
        SET status = 'Picking in Progress',
            responsibility = 'Operations'
        WHERE status = 'Orders Placed'
          AND COALESCE(expected_quantity, 0) > 0
          AND COALESCE(picked_quantity, 0) > 0
          AND COALESCE(picked_quantity, 0) < COALESCE(expected_quantity, 0)
        """
    )

    c.execute(
        """
        UPDATE order_header
        SET status = 'Pending Verification',
            responsibility = 'Quality'
        WHERE status IN ('Orders Placed', 'Picking in Progress')
          AND COALESCE(expected_quantity, 0) > 0
          AND COALESCE(picked_quantity, 0) >= COALESCE(expected_quantity, 0)
        """
    )

    c.execute(
        """
        UPDATE order_header
        SET status = 'Picking in Progress',
            responsibility = 'Operations'
        WHERE status = 'Pending Verification'
          AND COALESCE(expected_quantity, 0) > COALESCE(picked_quantity, 0)
        """
    )


def ensure_ask_wms_chat_schema(conn):
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_wms_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_wms_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER,
            created_at TEXT,
            role TEXT,
            message TEXT,
            meta_json TEXT
        )
        """
    )
    c.execute("PRAGMA table_info(ask_wms_chat)")
    columns = {row[1] for row in c.fetchall()}
    if "conversation_id" not in columns:
        c.execute("ALTER TABLE ask_wms_chat ADD COLUMN conversation_id INTEGER")

    # Migrate legacy single-thread messages into one conversation if needed.
    c.execute(
        """
        SELECT COUNT(*)
        FROM ask_wms_chat
        WHERE conversation_id IS NULL
        """
    )
    legacy_count = int(c.fetchone()[0] or 0)
    if legacy_count:
        stamp = now_pt().isoformat()
        c.execute(
            """
            INSERT INTO ask_wms_conversations (title, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ("Earlier conversation", stamp, stamp),
        )
        legacy_id = c.lastrowid
        c.execute(
            """
            UPDATE ask_wms_chat
            SET conversation_id = ?
            WHERE conversation_id IS NULL
            """,
            (legacy_id,),
        )


def ensure_runtime_schema(conn):
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in c.fetchall()}

    if "order_header" in tables:
        ensure_order_header_pick_tracking_schema(conn)

    if "quality_audits" in tables:
        ensure_quality_audit_schema(conn)

    ensure_ask_wms_chat_schema(conn)
    conn.commit()


def calculate_pick_progress_from_totals(expected_units, picked_units):
    expected_total = max(int(expected_units or 0), 0)
    if expected_total <= 0:
        return 0.0

    picked_total = max(0, min(int(picked_units or 0), expected_total))
    progress_pct = (picked_total / expected_total) * 100
    return round(max(0.0, min(progress_pct, 100.0)), 1)


def get_order_pick_totals(conn, order_number):
    c = conn.cursor()
    c.execute(
        """
        SELECT COALESCE(expected_quantity, 0), COALESCE(picked_quantity, 0), COALESCE(status, '')
        FROM order_header
        WHERE order_number = ?
        """,
        (order_number,),
    )
    row = c.fetchone()

    if not row:
        return 0, 0

    expected_units = max(int(row[0] or 0), 0)
    header_picked_units = max(0, min(int(row[1] or 0), expected_units))
    order_status = clean_display_text(row[2], "")

    c.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(-qty_change), 0)
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK'
        """,
        (order_number,),
    )
    pick_tx_count, tx_picked_units = c.fetchone()
    tx_picked_units = max(0, min(int(tx_picked_units or 0), expected_units))

    if int(pick_tx_count or 0) > 0:
        picked_units = tx_picked_units
    elif order_status in {"Orders Placed", "Picking in Progress", "Blocked"}:
        picked_units = 0
    else:
        picked_units = header_picked_units
    return expected_units, picked_units


def apply_order_pick_progress(conn, order_number, quantity_delta):
    if quantity_delta < 0:
        raise ValueError("Picked quantity cannot be negative.")

    c = conn.cursor()
    expected_units, picked_units = get_order_pick_totals(conn, order_number)

    if expected_units <= 0:
        c.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM order_lines
            WHERE order_number = ?
            """,
            (order_number,),
        )
        expected_units = max(int(c.fetchone()[0] or 0), 0)
        c.execute(
            """
            UPDATE order_header
            SET expected_quantity = ?,
                picked_quantity = MIN(COALESCE(picked_quantity, 0), ?)
            WHERE order_number = ?
            """,
            (expected_units, expected_units, order_number),
        )
        picked_units = min(picked_units, expected_units)

    remaining_units = max(expected_units - picked_units, 0)
    if quantity_delta > remaining_units:
        raise ValueError("Picked quantity cannot exceed expected quantity.")

    updated_picked_units = picked_units + quantity_delta
    c.execute(
        """
        UPDATE order_header
        SET picked_quantity = ?
        WHERE order_number = ?
        """,
        (updated_picked_units, order_number),
    )

    return {
        "expected_quantity": expected_units,
        "picked_quantity": updated_picked_units,
        "remaining_quantity": max(expected_units - updated_picked_units, 0),
        "progress_pct": calculate_pick_progress_from_totals(expected_units, updated_picked_units),
    }


def sync_order_pick_progress(conn, order_number):
    c = conn.cursor()
    c.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM order_lines
        WHERE order_number = ?
        """,
        (order_number,),
    )
    expected_units = max(int(c.fetchone()[0] or 0), 0)

    c.execute(
        """
        SELECT COALESCE(SUM(-qty_change), 0)
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK'
        """,
        (order_number,),
    )
    picked_units = max(0, min(int(c.fetchone()[0] or 0), expected_units))

    c.execute(
        """
        UPDATE order_header
        SET expected_quantity = ?,
            picked_quantity = ?
        WHERE order_number = ?
        """,
        (expected_units, picked_units, order_number),
    )

    return {
        "expected_quantity": expected_units,
        "picked_quantity": picked_units,
        "remaining_quantity": max(expected_units - picked_units, 0),
        "progress_pct": calculate_pick_progress_from_totals(expected_units, picked_units),
    }


def get_order_pick_readiness(conn, order_number):
    c = conn.cursor()

    c.execute(
        """
        SELECT sku, SUM(quantity) AS req_qty
        FROM order_lines
        WHERE order_number = ?
        GROUP BY sku
        ORDER BY sku
        """,
        (order_number,),
    )
    expected_lines = [(sku, int(req_qty or 0)) for sku, req_qty in c.fetchall()]

    c.execute(
        """
        SELECT sku, COALESCE(SUM(-qty_change), 0) AS picked_qty
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK'
        GROUP BY sku
        """,
        (order_number,),
    )
    picked_qty_lookup = {sku: int(picked_qty or 0) for sku, picked_qty in c.fetchall()}

    c.execute(
        """
        SELECT COUNT(*)
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK'
        """,
        (order_number,),
    )
    pick_tx_count = int(c.fetchone()[0] or 0)

    pick_gap_lines = []
    for expected_sku, expected_qty in expected_lines:
        picked_qty = picked_qty_lookup.get(expected_sku, 0)
        if picked_qty != expected_qty:
            pick_gap_lines.append((expected_sku, expected_qty, picked_qty))

    expected_units, picked_units = get_order_pick_totals(conn, order_number)
    if expected_units <= 0:
        expected_units = sum(int(req_qty or 0) for _, req_qty in expected_lines)
    picked_units = max(0, min(picked_units, expected_units))
    ready_for_quality = bool(expected_units) and picked_units == expected_units

    return {
        "expected_lines": expected_lines,
        "picked_qty_lookup": picked_qty_lookup,
        "pick_tx_count": pick_tx_count,
        "pick_gap_lines": pick_gap_lines,
        "expected_units": expected_units,
        "picked_units": picked_units,
        "ready_for_quality": ready_for_quality,
    }


def summarize_pick_readiness(readiness):
    expected_units = max(int(readiness.get("expected_units", 0) or 0), 0)
    picked_units = max(0, min(int(readiness.get("picked_units", 0) or 0), expected_units))
    remaining_units = max(expected_units - picked_units, 0)
    remaining_lines = len(readiness["pick_gap_lines"]) if remaining_units > 0 else 0

    return {
        "expected_units": expected_units,
        "picked_units": picked_units,
        "remaining_units": remaining_units,
        "remaining_lines": remaining_lines,
        "pick_tx_count": int(readiness["pick_tx_count"]),
        "ready_for_quality": bool(readiness["ready_for_quality"]),
    }


def shortage_issue_audit_id(order_number, sku):
    return f"SHORTAGE-{order_number}-{sku}"


def get_open_shortage_alerts(conn, limit=None):
    c = conn.cursor()
    c.execute(
        """
        SELECT order_number, sku, MAX(tx_time)
        FROM inventory_transactions
        WHERE tx_code = 'SHORTAGE'
        GROUP BY order_number, sku
        """
    )
    shortage_time_lookup = {
        (order_number, sku): clean_display_text(tx_time, "")
        for order_number, sku, tx_time in c.fetchall()
    }

    c.execute(
        """
        SELECT audit_id, issue_id, issue_status, assigned_to
        FROM supervisor_quality_issues
        WHERE issue_type = ?
        """,
        (SHORTAGE_ISSUE_TYPE,),
    )
    shortage_issue_lookup = {
        audit_id: {
            "issue_id": issue_id,
            "issue_status": issue_status,
            "assigned_to": assigned_to,
        }
        for audit_id, issue_id, issue_status, assigned_to in c.fetchall()
    }

    c.execute(
        """
        SELECT order_number, source, status, date, responsibility
        FROM order_header
        WHERE status IN ('Orders Placed', 'Picking in Progress', 'Pending Verification', 'Blocked')
        ORDER BY date DESC, order_number ASC
        """
    )

    alerts = []
    for order_number, source_wh, order_status, order_date, responsibility in c.fetchall():
        if order_status == "Picking in Progress":
            continue

        readiness = get_order_pick_readiness(conn, order_number)
        readiness_summary = summarize_pick_readiness(readiness)

        if readiness_summary["ready_for_quality"]:
            continue

        for sku, required_qty in readiness["expected_lines"]:
            required_qty = int(required_qty or 0)
            picked_qty = int(readiness["picked_qty_lookup"].get(sku, 0) or 0)
            remaining_qty = max(required_qty - picked_qty, 0)
            if remaining_qty <= 0:
                continue

            warehouse, location, on_hand_qty = get_best_inventory_location(conn, sku, source_wh)
            if on_hand_qty >= remaining_qty:
                continue

            shortage_qty = max(remaining_qty - on_hand_qty, 0)
            if on_hand_qty <= 0:
                reason = "No inventory on hand for the remaining requirement"
            else:
                reason = f"Short {shortage_qty} unit(s) against the remaining requirement"

            audit_id = shortage_issue_audit_id(order_number, sku)
            issue_details = shortage_issue_lookup.get(audit_id, {})
            blocked_at = shortage_time_lookup.get((order_number, sku), clean_display_text(order_date, ""))

            alerts.append(
                {
                    "order_number": order_number,
                    "order_status": clean_display_text(order_status, BLOCKED_ORDER_STATUS),
                    "order_date": clean_display_text(order_date, ""),
                    "blocked_at": blocked_at,
                    "responsibility": clean_display_text(responsibility, "Inventory"),
                    "sku": sku,
                    "required_qty": required_qty,
                    "picked_qty": picked_qty,
                    "remaining_qty": remaining_qty,
                    "on_hand_qty": on_hand_qty,
                    "shortage_qty": shortage_qty,
                    "warehouse": warehouse,
                    "location": location,
                    "reason": reason,
                    "audit_id": audit_id,
                    "issue_id": issue_details.get("issue_id", ""),
                    "issue_status": clean_display_text(issue_details.get("issue_status"), "Not Notified"),
                    "assigned_to": clean_display_text(issue_details.get("assigned_to"), ""),
                    "notified": bool(issue_details.get("issue_id")) and clean_display_text(issue_details.get("issue_status"), "") not in {"Resolved", "Closed"},
                    "inventory_action_url": build_inventory_action_url(sku, warehouse, location),
                }
            )

    alerts.sort(key=lambda item: (item["blocked_at"] or "", item["order_number"], item["sku"]), reverse=True)

    if limit is not None:
        return alerts[:limit]
    return alerts


def has_open_pick_activity(conn, order_number):
    assigned_picker, pick_started_at = get_pick_start_snapshot(conn, order_number)
    if assigned_picker or pick_started_at:
        return True

    readiness = get_order_pick_readiness(conn, order_number)
    return readiness["pick_tx_count"] > 0


def create_shortage_supervisor_issue(conn, alert, notified_by="Operations"):
    notes = (
        f"{alert['reason']}. Remaining {alert['remaining_qty']} unit(s); on hand {alert['on_hand_qty']} unit(s). "
        f"Notification submitted by {notified_by}."
    )
    part_snapshot = (
        f"{alert['sku']} remaining {alert['remaining_qty']} unit(s) "
        f"at {warehouse_code_label(alert['warehouse'])} / {clean_display_text(alert['location'], 'Unassigned')}"
    )
    issue_id = create_supervisor_issue(
        conn=conn,
        order_number=alert["order_number"],
        audit_id=alert["audit_id"],
        issue_date=alert["blocked_at"] or datetime.now().isoformat(),
        issue_type=SHORTAGE_ISSUE_TYPE,
        part_snapshot_override=part_snapshot,
        inspector_notes=notes,
    )
    c = conn.cursor()
    c.execute(
        """
        UPDATE supervisor_quality_issues
        SET issue_status = CASE
                WHEN issue_status IN ('Resolved', 'Closed') THEN 'Awaiting Inventory'
                ELSE issue_status
            END,
            assigned_to = CASE
                WHEN TRIM(COALESCE(assigned_to, '')) = '' THEN 'Inventory Control'
                ELSE assigned_to
            END,
            last_updated_at = ?
        WHERE issue_id = ?
        """,
        (datetime.now().isoformat(), issue_id),
    )
    return issue_id


def sync_shortage_order_workflow(conn):
    c = conn.cursor()
    shortage_alerts = get_open_shortage_alerts(conn)
    alerts_by_order = {}
    for alert in shortage_alerts:
        alerts_by_order.setdefault(alert["order_number"], []).append(alert)

    c.execute(
        """
        SELECT order_number, status, responsibility
        FROM order_header
        WHERE status IN ('Orders Placed', 'Picking in Progress', 'Blocked')
        """
    )

    resumed_orders = []
    blocked_orders = []
    for order_number, status, responsibility in c.fetchall():
        if order_number in alerts_by_order:
            if status != BLOCKED_ORDER_STATUS or responsibility != "Inventory":
                c.execute(
                    """
                    UPDATE order_header
                    SET status = ?,
                        responsibility = 'Inventory'
                    WHERE order_number = ?
                    """,
                    (BLOCKED_ORDER_STATUS, order_number),
                )
            blocked_orders.append(order_number)
            continue

        if status == BLOCKED_ORDER_STATUS:
            next_status = "Picking in Progress" if has_open_pick_activity(conn, order_number) else "Orders Placed"
            c.execute(
                """
                UPDATE order_header
                SET status = ?,
                    responsibility = 'Operations'
                WHERE order_number = ?
                """,
                (next_status, order_number),
            )
            c.execute(
                """
                UPDATE supervisor_quality_issues
                SET issue_status = CASE
                        WHEN issue_type = ? AND issue_status NOT IN ('Resolved', 'Closed') THEN 'Resolved'
                        ELSE issue_status
                    END,
                    resolved_flag = CASE
                        WHEN issue_type = ? AND issue_status NOT IN ('Resolved', 'Closed') THEN 1
                        ELSE resolved_flag
                    END,
                    resolved_at = CASE
                        WHEN issue_type = ? AND issue_status NOT IN ('Resolved', 'Closed') THEN ?
                        ELSE resolved_at
                    END,
                    resolution_notes = CASE
                        WHEN issue_type = ? AND issue_status NOT IN ('Resolved', 'Closed') THEN
                            TRIM(COALESCE(resolution_notes, '') || ' Auto-resolved after inventory replenishment.')
                        ELSE resolution_notes
                    END,
                    last_updated_at = CASE
                        WHEN issue_type = ? AND issue_status NOT IN ('Resolved', 'Closed') THEN ?
                        ELSE last_updated_at
                    END
                WHERE order_number = ?
                """,
                (
                    SHORTAGE_ISSUE_TYPE,
                    SHORTAGE_ISSUE_TYPE,
                    SHORTAGE_ISSUE_TYPE,
                    datetime.now().isoformat(),
                    SHORTAGE_ISSUE_TYPE,
                    SHORTAGE_ISSUE_TYPE,
                    datetime.now().isoformat(),
                    order_number,
                ),
            )
            resumed_orders.append(order_number)

    return {
        "alerts": get_open_shortage_alerts(conn),
        "blocked_orders": blocked_orders,
        "resumed_orders": resumed_orders,
    }


def enforce_quality_gate_on_pending_orders(conn):
    c = conn.cursor()
    c.execute("SELECT order_number FROM order_header WHERE status='Pending Verification'")
    pending_orders = [row[0] for row in c.fetchall()]

    blocked_orders = []
    for order_number in pending_orders:
        readiness = get_order_pick_readiness(conn, order_number)
        if readiness["ready_for_quality"]:
            continue

        c.execute(
            """
            UPDATE order_header
            SET status='Picking in Progress',
                responsibility='Operations'
            WHERE order_number=?
              AND status='Pending Verification'
            """,
            (order_number,),
        )
        blocked_orders.append(
            {
                "order_number": order_number,
                "pick_tx_count": readiness["pick_tx_count"],
                "line_mismatch_count": len(readiness["pick_gap_lines"]),
            }
        )

    return blocked_orders


def clear_active_workload(conn):
    c = conn.cursor()
    c.execute(
        """
        SELECT order_number, status
        FROM order_header
        WHERE status IN ('Orders Placed', 'Picking in Progress', 'Blocked', 'Pending Verification', 'Quality Issue')
        ORDER BY order_number
        """
    )
    active_rows = c.fetchall()

    if not active_rows:
        return {
            "orders_removed": 0,
            "lines_removed": 0,
            "transactions_removed": 0,
            "audits_removed": 0,
            "issues_removed": 0,
            "status_summary": "No active picks, open orders, or quality work were found.",
        }

    order_numbers = [row[0] for row in active_rows]
    status_counts = {}
    for _order_number, status in active_rows:
        normalized_status = clean_display_text(status, "Unknown")
        status_counts[normalized_status] = status_counts.get(normalized_status, 0) + 1

    placeholders = ", ".join("?" for _ in order_numbers)

    def _count_rows(table_name):
        c.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE order_number IN ({placeholders})",
            order_numbers,
        )
        return int(c.fetchone()[0] or 0)

    lines_removed = _count_rows("order_lines")
    transactions_removed = _count_rows("inventory_transactions")
    audits_removed = _count_rows("quality_audits")
    issues_removed = _count_rows("supervisor_quality_issues")

    c.execute(
        f"DELETE FROM supervisor_quality_issues WHERE order_number IN ({placeholders})",
        order_numbers,
    )
    c.execute(
        f"DELETE FROM quality_audits WHERE order_number IN ({placeholders})",
        order_numbers,
    )
    c.execute(
        f"DELETE FROM inventory_transactions WHERE order_number IN ({placeholders})",
        order_numbers,
    )
    c.execute(
        f"DELETE FROM order_lines WHERE order_number IN ({placeholders})",
        order_numbers,
    )
    c.execute(
        f"DELETE FROM order_header WHERE order_number IN ({placeholders})",
        order_numbers,
    )

    status_summary = ", ".join(
        f"{status_counts[status]} {status}"
        for status in sorted(status_counts)
    )

    return {
        "orders_removed": len(order_numbers),
        "lines_removed": lines_removed,
        "transactions_removed": transactions_removed,
        "audits_removed": audits_removed,
        "issues_removed": issues_removed,
        "status_summary": status_summary,
    }


def debug_log(event_name, **details):
    ordered_keys = sorted(details)
    payload = ", ".join(f"{key}={details[key]!r}" for key in ordered_keys)
    if payload:
        print(f"[WMS] {current_timestamp()} {event_name} | {payload}")
    else:
        print(f"[WMS] {current_timestamp()} {event_name}")


def reset_demo_data(conn):
    cleanup_summary = clear_active_workload(conn)
    debug_log("system.reset_demo_data", **cleanup_summary)
    return cleanup_summary


def normalize_urgency(urgency, fallback="Standard"):
    text = clean_display_text(urgency, "").strip().lower()
    if not text:
        return fallback
    return URGENCY_LABEL_MAP.get(text, fallback)


def now_pt():
    return datetime.now(PACIFIC_TZ)


def current_timestamp():
    return now_pt().isoformat()


def parse_order_datetime(value):
    dt_value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=ZoneInfo("UTC")).astimezone(PACIFIC_TZ)
    return dt_value.astimezone(PACIFIC_TZ)


def _normalize_reference_datetime(value):
    if value is None:
        return now_pt()
    return parse_order_datetime(value)


def normalize_urgency_labels(conn):
    c = conn.cursor()
    c.execute(
        """
        UPDATE order_header
        SET urgency = CASE
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'high' THEN 'Critical'
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'medium' THEN 'Urgent'
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'low' THEN 'Standard'
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'critical' THEN 'Critical'
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'urgent' THEN 'Urgent'
            WHEN LOWER(TRIM(COALESCE(urgency, ''))) = 'standard' THEN 'Standard'
            ELSE urgency
        END
        WHERE urgency IS NOT NULL
        """
    )
    return c.rowcount


def _is_business_day(dt_value):
    local_dt = _normalize_reference_datetime(dt_value)
    return local_dt.weekday() < 5 and local_dt.date() not in BUSINESS_HOLIDAYS


def _at_time(dt_value, hour, minute):
    local_dt = _normalize_reference_datetime(dt_value)
    return local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_business_start(anchor):
    current = _normalize_reference_datetime(anchor)
    while True:
        if _is_business_day(current):
            return _at_time(current, BUSINESS_DAY_START_HOUR, BUSINESS_DAY_START_MINUTE)
        current = _at_time(current + timedelta(days=1), BUSINESS_DAY_START_HOUR, BUSINESS_DAY_START_MINUTE)


def _align_to_business_window(current):
    current = _normalize_reference_datetime(current)
    while not _is_business_day(current):
        current = _next_business_start(current)

    day_start = _at_time(current, BUSINESS_DAY_START_HOUR, BUSINESS_DAY_START_MINUTE)
    day_end = _at_time(current, BUSINESS_DAY_END_HOUR, BUSINESS_DAY_END_MINUTE)

    if current < day_start:
        return day_start
    if current >= day_end:
        return _next_business_start(current + timedelta(days=1))
    return current


def calculate_sla_start(order_time, urgency):
    order_time = parse_order_datetime(order_time)
    urgency_level = normalize_urgency(urgency)
    if not _is_business_day(order_time):
        return _next_business_start(order_time)

    day_start = _at_time(order_time, BUSINESS_DAY_START_HOUR, BUSINESS_DAY_START_MINUTE)
    day_end = _at_time(order_time, BUSINESS_DAY_END_HOUR, BUSINESS_DAY_END_MINUTE)

    if order_time < day_start:
        return day_start

    if order_time >= day_end:
        return _next_business_start(order_time + timedelta(days=1))

    # Standard/Urgent orders after cutoff roll to next business day start.
    if urgency_level in {"Standard", "Urgent"} and order_time.hour >= CUTOFF_HOUR:
        return _next_business_start(order_time + timedelta(days=1))

    return order_time


def _add_business_hours(start_time, hours_to_add):
    start_time = parse_order_datetime(start_time)
    remaining_seconds = int(hours_to_add * 3600)
    current = _align_to_business_window(start_time)

    while remaining_seconds > 0:
        day_end = _at_time(current, BUSINESS_DAY_END_HOUR, BUSINESS_DAY_END_MINUTE)
        available_seconds = int((day_end - current).total_seconds())

        if remaining_seconds <= available_seconds:
            return current + timedelta(seconds=remaining_seconds)

        remaining_seconds -= available_seconds
        current = _next_business_start(current + timedelta(days=1))

    return current


def _add_business_days(start_time, business_days_to_add):
    current = _align_to_business_window(start_time)
    remaining_days = max(int(business_days_to_add or 0), 0)

    while remaining_days > 0:
        current = _next_business_start(current + timedelta(days=1))
        remaining_days -= 1

    return current


def calculate_sla_deadline(order_time, urgency):
    order_time = parse_order_datetime(order_time)
    urgency_level = normalize_urgency(urgency)
    start_time = calculate_sla_start(order_time, urgency_level)

    if urgency_level == "Standard":
        return _add_business_days(start_time, STANDARD_SLA_BUSINESS_DAYS)

    sla_hours = SLA_CONFIG.get(urgency_level, SLA_CONFIG["Urgent"])
    return start_time + timedelta(hours=sla_hours)


def format_sla_timing(order_time, urgency, now=None):
    order_time = parse_order_datetime(order_time)
    reference_time = _normalize_reference_datetime(now)
    deadline = calculate_sla_deadline(order_time, urgency)
    delta = deadline - reference_time
    minutes = int(abs(delta.total_seconds()) // 60)
    hours = minutes // 60
    mins = minutes % 60

    if delta.total_seconds() >= 0:
        timer_text = f"{hours}h {mins}m remaining"
    else:
        timer_text = f"Overdue by {hours}h {mins}m"

    return deadline.strftime("%Y-%m-%d %H:%M PT"), timer_text


def format_sla_start_time(order_time, urgency):
    order_time = parse_order_datetime(order_time)
    sla_start = calculate_sla_start(order_time, urgency)
    return sla_start.strftime("%Y-%m-%d %H:%M PT")


def format_pt_timestamp(value, fallback="-"):
    if value in {None, ""}:
        return fallback
    try:
        return parse_order_datetime(value).strftime("%Y-%m-%d %H:%M PT")
    except (TypeError, ValueError):
        return clean_display_text(value, fallback)


def format_elapsed_minutes_label(total_seconds):
    total_minutes = max(int(round((total_seconds or 0) / 60)), 0)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def format_executive_timestamp(value):
    localized = parse_order_datetime(value)
    hour_text = localized.strftime("%I").lstrip("0") or "0"
    return f"{localized.strftime('%A, %B')} {localized.day} {localized.year} &nbsp;&bull;&nbsp; {hour_text}:{localized.strftime('%M %p')}"


def get_order_activity_window(conn, order_number, fallback_start=None):
    start_time = parse_order_datetime(fallback_start) if fallback_start is not None else None
    completion_time = None

    if not conn or not order_number:
        return start_time, completion_time

    c = conn.cursor()
    c.execute(
        """
        SELECT tx_time
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK_START'
        ORDER BY tx_time ASC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row and clean_display_text(row[0], ""):
        start_time = parse_order_datetime(row[0])
    else:
        c.execute(
            """
            SELECT tx_time
            FROM inventory_transactions
            WHERE order_number = ?
              AND tx_code = 'PICK'
            ORDER BY tx_time ASC
            LIMIT 1
            """,
            (order_number,),
        )
        row = c.fetchone()
        if row and clean_display_text(row[0], ""):
            start_time = parse_order_datetime(row[0])

    c.execute(
        """
        SELECT audit_time
        FROM quality_audits
        WHERE order_number = ?
          AND result = 'Passed'
        ORDER BY audit_time DESC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row and clean_display_text(row[0], ""):
        completion_time = parse_order_datetime(row[0])
    else:
        c.execute(
            """
            SELECT COALESCE(closed_at, resolved_at)
            FROM supervisor_quality_issues
            WHERE order_number = ?
              AND TRIM(COALESCE(COALESCE(closed_at, resolved_at), '')) <> ''
            ORDER BY COALESCE(closed_at, resolved_at) DESC
            LIMIT 1
            """,
            (order_number,),
        )
        row = c.fetchone()
        if row and clean_display_text(row[0], ""):
            completion_time = parse_order_datetime(row[0])

    return start_time, completion_time


def build_order_sla_snapshot(order_time, urgency, status, now=None, conn=None, order_number=None):
    normalized_order_time = parse_order_datetime(order_time)
    normalized_urgency = normalize_urgency(urgency, "Standard")
    reference_time = _normalize_reference_datetime(now)
    normalized_status = clean_display_text(status, "")

    sla_start = format_sla_start_time(normalized_order_time, normalized_urgency)
    sla_deadline_dt = calculate_sla_deadline(normalized_order_time, normalized_urgency)
    sla_deadline = sla_deadline_dt.strftime("%Y-%m-%d %H:%M PT")

    if normalized_status == "Completed" and conn and order_number:
        actual_start_time, completion_time = get_order_activity_window(conn, order_number, normalized_order_time)
        actual_start_time = actual_start_time or normalized_order_time
        completion_time = completion_time or normalized_order_time
        elapsed_seconds = max((completion_time - actual_start_time).total_seconds(), 0)
        return {
            "sla_key": "healthy",
            "sla_status": "Healthy",
            "sla_start": format_pt_timestamp(actual_start_time),
            "sla_deadline": format_pt_timestamp(completion_time),
            "sla_timer": f"Completed in {format_elapsed_minutes_label(elapsed_seconds)}",
            "sla_deadline_dt": completion_time,
            "sla_remaining_seconds": 0,
            "age_minutes": max(int(round(elapsed_seconds / 60)), 0),
        }

    if normalized_status == "Completed":
        return {
            "sla_key": "healthy",
            "sla_status": "Healthy",
            "sla_start": sla_start,
            "sla_deadline": sla_deadline,
            "sla_timer": "Completed",
            "sla_deadline_dt": sla_deadline_dt,
            "sla_remaining_seconds": max((sla_deadline_dt - reference_time).total_seconds(), 0),
            "age_minutes": 0,
        }

    sla_key, sla_status = calculate_sla_status(normalized_order_time, normalized_urgency, reference_time)
    _formatted_deadline, sla_timer = format_sla_timing(normalized_order_time, normalized_urgency, reference_time)
    actual_start_time, _completion_time = get_order_activity_window(conn, order_number, normalized_order_time)
    active_start_time = actual_start_time or normalized_order_time
    elapsed_seconds = max((reference_time - active_start_time).total_seconds(), 0)
    return {
        "sla_key": sla_key,
        "sla_status": sla_status,
        "sla_start": sla_start,
        "sla_deadline": sla_deadline,
        "sla_timer": sla_timer,
        "sla_deadline_dt": sla_deadline_dt,
        "sla_remaining_seconds": (sla_deadline_dt - reference_time).total_seconds(),
        "age_minutes": max(int(round(elapsed_seconds / 60)), 0),
    }


def generate_issue_id(order_number):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"ISS-{order_number}-{stamp}-{uuid4().hex[:5].upper()}"


def build_issue_type(part_match, damage, qty_match):
    issue_types = []
    if part_match == "No":
        issue_types.append("Wrong Part")
    if qty_match == "No":
        issue_types.append("Quantity Mismatch")
    if damage == "Yes":
        issue_types.append("Damage")
    return ", ".join(issue_types) if issue_types else "General Quality Issue"


def order_part_snapshot(conn, order_number):
    c = conn.cursor()
    c.execute("""
        SELECT sku, quantity
        FROM order_lines
        WHERE order_number = ?
        ORDER BY sku
    """, (order_number,))
    line_rows = c.fetchall()
    return ", ".join(f"{sku} x{quantity}" for sku, quantity in line_rows)


def get_order_line_preview(conn, order_number, max_items=4):
    c = conn.cursor()
    c.execute(
        """
        SELECT sku, quantity
        FROM order_lines
        WHERE order_number = ?
        ORDER BY sku
        """,
        (order_number,),
    )
    line_rows = [(sku, int(quantity or 0)) for sku, quantity in c.fetchall()]
    preview_items = [
        {"sku": sku, "quantity": quantity}
        for sku, quantity in line_rows[:max_items]
    ]
    hidden_count = max(0, len(line_rows) - len(preview_items))
    return preview_items, hidden_count


def get_order_workboard_lines(conn, order_number, source_wh, max_items=4):
    readiness = get_order_pick_readiness(conn, order_number)
    detail_rows = []
    for sku, required_qty in readiness["expected_lines"][:max_items]:
        warehouse, location, _on_hand_qty = get_best_inventory_location(conn, sku, source_wh)
        picked_qty = int(readiness["picked_qty_lookup"].get(sku, 0) or 0)
        detail_rows.append(
            {
                "sku": sku,
                "required_qty": int(required_qty or 0),
                "picked_qty": picked_qty,
                "remaining_qty": max(int(required_qty or 0) - picked_qty, 0),
                "warehouse_label": warehouse_code_label(warehouse),
                "location_label": clean_display_text(location, "Unassigned"),
            }
        )
    hidden_count = max(0, len(readiness["expected_lines"]) - len(detail_rows))
    return detail_rows, hidden_count


def get_picker_for_order(conn, order_number):
    c = conn.cursor()
    c.execute("""
        SELECT user_role
        FROM inventory_transactions
        WHERE order_number = ? AND tx_code = 'PICK_START'
        ORDER BY tx_time DESC
        LIMIT 1
    """, (order_number,))
    row = c.fetchone()
    if row and clean_display_text(row[0], ""):
        return row[0]

    c.execute("""
        SELECT user_role
        FROM inventory_transactions
        WHERE order_number = ? AND tx_code = 'PICK'
        ORDER BY tx_time DESC
        LIMIT 1
    """, (order_number,))
    row = c.fetchone()
    return row[0] if row else "Operations"


def get_pick_start_snapshot(conn, order_number):
    c = conn.cursor()
    c.execute(
        """
        SELECT user_role, tx_time
        FROM inventory_transactions
        WHERE order_number = ?
                    AND tx_code IN ('PICK_TAKEOVER', 'PICK_START')
        ORDER BY tx_time DESC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row:
        return clean_display_text(row[0], "Operations"), clean_display_text(row[1], "")

    c.execute(
        """
        SELECT user_role, tx_time
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK'
        ORDER BY tx_time ASC
        LIMIT 1
        """,
        (order_number,),
    )
    row = c.fetchone()
    if row:
        return clean_display_text(row[0], "Operations"), clean_display_text(row[1], "")

    return "", ""


def record_pick_close_transaction(conn, order_number, warehouse, picker_name, picked_units, notes):
    c = conn.cursor()
    c.execute(
        """
        SELECT 1
        FROM inventory_transactions
        WHERE order_number = ?
          AND tx_code = 'PICK_CLOSE'
        LIMIT 1
        """,
        (order_number,),
    )
    if c.fetchone():
        return

    resolved_picker = clean_display_text(picker_name, "")
    if not resolved_picker:
        resolved_picker, _completed_at = get_pick_start_snapshot(conn, order_number)
    if not resolved_picker:
        resolved_picker = "Operations"

    log_inventory_transaction(
        conn=conn,
        tx_code="PICK_CLOSE",
        order_number=order_number,
        sku="N/A",
        warehouse=clean_display_text(warehouse, SOURCE_WAREHOUSE),
        location="N/A",
        qty_change=0,
        qty_before=picked_units,
        qty_after=picked_units,
        user_role=resolved_picker,
        notes=notes,
    )


def move_order_to_quality_verification(conn, order_number, warehouse, picker_name, picked_units, notes, expected_status="Picking in Progress"):
    c = conn.cursor()
    c.execute(
        """
        UPDATE order_header
        SET status='Pending Verification',
            responsibility='Quality'
        WHERE order_number=?
          AND status=?
        """,
        (order_number, expected_status),
    )
    if c.rowcount != 1:
        return False

    record_pick_close_transaction(conn, order_number, warehouse, picker_name, picked_units, notes)
    return True


def calculate_pick_progress(readiness):
    return calculate_pick_progress_from_totals(
        readiness.get("expected_units", 0),
        readiness.get("picked_units", 0),
    )


def transact_pick(conn, order, source_wh, dest_wh, active_picker, display_lines, missing_inventory, operations_url):
    c = conn.cursor()
    debug_log("transact_pick.triggered", order=order, picker=active_picker)

    if missing_inventory:
        debug_log("transact_pick.failed_missing_inventory", order=order)
        for line in display_lines:
            log_inventory_shortage_warning(
                conn,
                order,
                line["sku"],
                "Warehouse Unknown",
                "Unassigned",
                line["required_qty"],
                0,
                "No inventory location was found for this SKU",
            )
        c.execute(
            """
            UPDATE order_header
            SET status = ?,
                responsibility = 'Inventory'
            WHERE order_number = ?
            """,
            (BLOCKED_ORDER_STATUS, order),
        )
        conn.commit()
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Order Blocked</h2>
                <p>Cannot transact pick because no inventory location was mapped for this order's SKU(s).</p>
                <p>Operations cannot resolve shortages. Notify Supervisor so Inventory can replenish and release the order.</p>
                <div class='quick-links'>
                    <a class='quick-link' href='/operations/notify_shortage/{order}?{urlencode({'picker': active_picker})}'>Notify Supervisor</a>
                    <a class='quick-link' href='/order/{order}?context=operations'>View Order</a>
                </div>
                <br>
                <a href='{operations_url}'>Back to Operations</a>
            </div>
        """)

    picked_qty_by_line = []
    any_picked = False
    total_picked_qty = 0

    for idx, line in enumerate(display_lines, start=1):
        picked_qty_input = request.form.get(f"picked_qty_{idx}", "").strip() or "0"
        try:
            picked_qty = int(picked_qty_input)
        except ValueError:
            debug_log("transact_pick.invalid_quantity", order=order, picker=active_picker, row=idx, raw_value=picked_qty_input)
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, f"Row {idx}: please enter a valid numeric quantity.", "warning"))

        if picked_qty < 0:
            debug_log("transact_pick.negative_quantity", order=order, picker=active_picker, row=idx, quantity=picked_qty)
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, f"Row {idx}: picked quantity cannot be negative.", "warning"))

        remaining_qty = int(line["remaining_qty"] or 0)
        on_hand_qty = int(line["on_hand_qty"] or 0)
        debug_log(
            "transact_pick.quantity_validation",
            order=order,
            picker=active_picker,
            row=idx,
            sku=line["sku"],
            entered_qty=picked_qty,
            remaining_qty=remaining_qty,
            on_hand_qty=on_hand_qty,
        )

        if picked_qty > remaining_qty:
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, "Picked quantity cannot exceed expected quantity.", "warning"))

        if picked_qty > on_hand_qty:
            debug_log("transact_pick.insufficient_on_hand", order=order, picker=active_picker, row=idx, sku=line["sku"], entered_qty=picked_qty, on_hand_qty=on_hand_qty)
            log_inventory_shortage_warning(conn, order, line["sku"], line["warehouse"], line["location"], remaining_qty, on_hand_qty, "Picked quantity exceeds on-hand inventory")
            c.execute(
                """
                UPDATE order_header
                SET status = ?,
                    responsibility = 'Inventory'
                WHERE order_number = ?
                """,
                (BLOCKED_ORDER_STATUS, order),
            )
            conn.commit()
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, "Picked quantity exceeds on-hand inventory.", "warning"))

        if picked_qty > 0:
            any_picked = True
            total_picked_qty += picked_qty

        picked_qty_by_line.append(picked_qty)

    if not any_picked:
        debug_log("transact_pick.no_quantity", order=order, picker=active_picker)
        conn.close()
        return redirect(build_pick_screen_url(order, active_picker, "Enter at least one picked quantity greater than zero.", "warning"))

    if request.form.get("verification_ack") != "on":
        debug_log("transact_pick.verification_missing", order=order, picker=active_picker)
        conn.close()
        return redirect(build_pick_screen_url(order, active_picker, "Confirm that you verified the source location and quantity before posting the pick.", "warning"))

    for idx, line in enumerate(display_lines, start=1):
        picked_qty = picked_qty_by_line[idx - 1]
        if picked_qty <= 0:
            continue

        expected_location = clean_display_text(line["location"], "")
        entered_location = request.form.get(f"location_check_{idx}", "").strip()
        if expected_location in {"", "Unassigned", "N/A"}:
            debug_log("transact_pick.invalid_location_mapping", order=order, picker=active_picker, row=idx, sku=line["sku"], expected_location=expected_location, entered_location=entered_location)
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, f"Row {idx}: no valid source location is mapped for this SKU.", "warning"))

        if entered_location.upper() != expected_location.upper():
            debug_log("transact_pick.location_mismatch", order=order, picker=active_picker, row=idx, sku=line["sku"], expected_location=expected_location, entered_location=entered_location)
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, f"Row {idx}: entered location does not match the assigned source location.", "warning"))

    for idx, line in enumerate(display_lines, start=1):
        picked_qty = picked_qty_by_line[idx - 1]
        if picked_qty <= 0:
            continue

        qty_before = int(line["on_hand_qty"])
        qty_after = qty_before - picked_qty
        c.execute(
            """
            UPDATE inventory
            SET quantity = quantity - ?
            WHERE sku = ? AND warehouse = ? AND location = ? AND quantity >= ?
            """,
            (picked_qty, line["sku"], line["warehouse"], line["location"], picked_qty),
        )

        if c.rowcount == 0:
            debug_log("transact_pick.inventory_update_failed", order=order, picker=active_picker, row=idx, sku=line["sku"], quantity=picked_qty)
            conn.rollback()
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, "Inventory changed before processing completed. No pick was transacted.", "warning"))

        line_completed = picked_qty == int(line["remaining_qty"] or 0)
        debug_log("transact_pick.line_posted", order=order, picker=active_picker, row=idx, sku=line["sku"], posted_qty=picked_qty, line_completed=line_completed)
        log_inventory_transaction(
            conn=conn,
            tx_code="PICK",
            order_number=order,
            sku=line["sku"],
            warehouse=line["warehouse"],
            location=line["location"],
            qty_change=-picked_qty,
            qty_before=qty_before,
            qty_after=qty_after,
            user_role=active_picker,
            notes=f"Pick transaction for destination {dest_wh}",
        )

    try:
        progress_update = sync_order_pick_progress(conn, order)
    except ValueError as exc:
        debug_log("transact_pick.progress_failed", order=order, picker=active_picker, error=str(exc))
        conn.rollback()
        conn.close()
        return redirect(build_pick_screen_url(order, active_picker, str(exc), "warning"))

    pick_readiness = get_order_pick_readiness(conn, order)
    if pick_readiness["ready_for_quality"]:
        if not move_order_to_quality_verification(
            conn,
            order,
            source_wh,
            active_picker,
            progress_update["picked_quantity"],
            "Pick completed from the pick screen and moved to quality verification",
        ):
            debug_log("transact_pick.state_transition_failed", order=order, picker=active_picker, target_state="Pending Verification")
            conn.rollback()
            conn.close()
            return redirect(build_pick_screen_url(order, active_picker, "Order state changed during transact pick. Please refresh and try again.", "warning"))

        conn.commit()
        debug_log("transact_pick.state_transition", order=order, picker=active_picker, from_state="Picking in Progress", to_state="Pending Verification", picked_quantity=progress_update["picked_quantity"])
        conn.close()
        return redirect(build_operations_redirect_url(active_picker, f"Order {order} pick posted and moved to quality verification.", "success"))

    c.execute(
        """
        UPDATE order_header
        SET status='Picking in Progress',
            responsibility='Operations'
        WHERE order_number = ?
        """,
        (order,),
    )
    conn.commit()
    debug_log("transact_pick.partial_progress", order=order, picker=active_picker, picked_quantity=progress_update["picked_quantity"], expected_quantity=progress_update["expected_quantity"], remaining_quantity=progress_update["remaining_quantity"])
    conn.close()
    return redirect(
        build_pick_screen_url(
            order,
            active_picker,
            (
                f"Progress updated: {progress_update['picked_quantity']} / {progress_update['expected_quantity']} picked "
                f"({int(round(progress_update['progress_pct']))}%)."
            ),
            "success",
        )
    )


def create_supervisor_issue(conn, order_number, audit_id, issue_date, issue_type, part_snapshot_override=None, inspector_notes=""):
    c = conn.cursor()
    c.execute("SELECT issue_id FROM supervisor_quality_issues WHERE audit_id = ?", (audit_id,))
    existing = c.fetchone()
    if existing:
        return existing[0]

    issue_id = generate_issue_id(order_number)
    part_snapshot = part_snapshot_override or order_part_snapshot(conn, order_number)
    picker_name = get_picker_for_order(conn, order_number)

    c.execute("""
        INSERT INTO supervisor_quality_issues (
            issue_id,
            order_number,
            audit_id,
            issue_date,
            picker_name,
            part_snapshot,
            issue_type,
            issue_status,
            root_cause,
            root_cause_notes,
            corrective_action,
            assigned_to,
            resolution_notes,
            attachment_name,
            attachment_path,
            resolved_flag,
            resolved_at,
            closed_at,
            last_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', '', '', '', '', 0, NULL, NULL, ?)
    """, (
        issue_id,
        order_number,
        audit_id,
        issue_date,
        picker_name,
        part_snapshot,
        issue_type,
        "Open",
        inspector_notes,
        datetime.now().isoformat(),
    ))
    return issue_id


def summarize_discrepancy_lines(line_diagnostics, pick_gap_lines, discrepancy_type, inspector_notes):
    summary_parts = []

    if discrepancy_type:
        summary_parts.append(f"Flagged discrepancy: {discrepancy_type}")

    mismatch_rows = []
    for diagnostic in line_diagnostics:
        if diagnostic["expected_sku"] != diagnostic["scanned_sku"] or diagnostic["expected_qty"] != diagnostic["scanned_qty"]:
            mismatch_rows.append(
                f"Line {diagnostic['line_no']}: expected {diagnostic['expected_sku']} x{diagnostic['expected_qty']}, "
                f"scanned {diagnostic['scanned_sku'] or 'blank'} x{diagnostic['scanned_qty']}"
            )

    if mismatch_rows:
        summary_parts.append("Scan mismatches: " + " | ".join(mismatch_rows))

    if pick_gap_lines:
        pick_gap_summary = []
        for sku, expected_qty, picked_qty in pick_gap_lines:
            pick_gap_summary.append(f"{sku}: expected {expected_qty}, picked {picked_qty}")
        summary_parts.append("Recorded pick variance: " + " | ".join(pick_gap_summary))

    if inspector_notes:
        summary_parts.append(f"Inspector notes: {inspector_notes}")

    return " || ".join(summary_parts) if summary_parts else "General quality discrepancy"


def ensure_supervisor_issue_records(conn):
    c = conn.cursor()
    c.execute("""
        SELECT qa.audit_id, qa.order_number, qa.audit_time, qa.part_match, qa.damage, qa.qty_match
        FROM quality_audits qa
        JOIN order_header oh ON oh.order_number = qa.order_number
        LEFT JOIN supervisor_quality_issues sqi ON sqi.audit_id = qa.audit_id
        WHERE qa.result = 'Failed'
          AND oh.status = 'Quality Issue'
          AND sqi.issue_id IS NULL
        ORDER BY qa.audit_time DESC
    """)
    missing_rows = c.fetchall()

    for audit_id, order_number, audit_time, part_match, damage, qty_match in missing_rows:
        create_supervisor_issue(
            conn=conn,
            order_number=order_number,
            audit_id=audit_id,
            issue_date=audit_time,
            issue_type=build_issue_type(part_match, damage, qty_match),
        )

    c.execute("""
        SELECT oh.order_number, oh.date
        FROM order_header oh
        LEFT JOIN supervisor_quality_issues sqi ON sqi.order_number = oh.order_number
        WHERE oh.status = 'Quality Issue'
          AND sqi.issue_id IS NULL
        ORDER BY oh.date DESC
    """)
    orphan_rows = c.fetchall()

    for order_number, order_date in orphan_rows:
        create_supervisor_issue(
            conn=conn,
            order_number=order_number,
            audit_id=f"MANUAL-{order_number}",
            issue_date=order_date,
            issue_type="General Quality Issue",
        )


def next_business_week_start(anchor=None):
    current = _normalize_reference_datetime(anchor)
    days_until_monday = (7 - current.weekday()) % 7
    monday = current + timedelta(days=days_until_monday)
    if days_until_monday == 0 and current.weekday() < 5:
        monday = current - timedelta(days=current.weekday())
    monday = monday.replace(hour=8, minute=0, second=0, microsecond=0)
    return monday


def seed_sample_orders(order_count=100, quality_escalations=4):
    conn = get_conn()
    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()

    c.execute("SELECT DISTINCT sku FROM inventory WHERE sku IS NOT NULL AND TRIM(sku) <> '' ORDER BY sku")
    sku_rows = c.fetchall()
    skus = [row[0] for row in sku_rows]

    if not skus:
        conn.close()
        raise ValueError("No inventory SKUs available to generate sample orders.")

    rng = random.Random()
    week_start = next_business_week_start()
    business_days = [week_start + timedelta(days=offset) for offset in range(5)]

    daily_targets = [order_count // 5] * 5
    for index in range(order_count % 5):
        daily_targets[index] += 1

    order_specs = []
    for day_index, day_start in enumerate(business_days):
        for slot in range(daily_targets[day_index]):
            order_specs.append((day_start, slot))

    quality_indexes = set(rng.sample(range(order_count), min(quality_escalations, order_count)))

    inserted_orders = []
    inserted_quality_issues = 0

    for index, (day_start, slot) in enumerate(order_specs, start=1):
        minutes_offset = rng.randint(0, 9 * 60 + 45)
        order_time = day_start + timedelta(minutes=minutes_offset)
        urgency = rng.choices(["Critical", "Urgent", "Standard"], weights=[20, 35, 45], k=1)[0]
        destination = rng.choice(DESTINATION_WAREHOUSES)

        line_count = rng.choices([1, 2, 3, 4], weights=[45, 30, 18, 7], k=1)[0]
        selected_skus = rng.sample(skus, k=min(line_count, len(skus)))
        line_items = []
        for sku in selected_skus:
            qty = rng.randint(1, 12)
            line_items.append((sku, qty))
        expected_quantity = sum(qty for _sku, qty in line_items)

        order_number = f"ORD-DEMO-{order_time.strftime('%Y%m%d')}-{index:03d}"
        request_id = f"REQ-DEMO-{order_time.strftime('%H%M')}-{index:03d}"

        day_position = (order_time.date() - business_days[0].date()).days
        quality_issue = (index - 1) in quality_indexes

        if quality_issue:
            status = "Quality Issue"
            responsibility = "Supervisor"
            inserted_quality_issues += 1
        elif day_position <= 1:
            status = "Completed"
            responsibility = "Closed"
        elif day_position == 2:
            status = rng.choice(["Completed", "Pending Verification"])
            responsibility = "Closed" if status == "Completed" else "Quality"
        elif day_position == 3:
            status = rng.choice(["Picking in Progress", "Pending Verification"])
            responsibility = "Operations" if status == "Picking in Progress" else "Quality"
        else:
            status = rng.choice(["Orders Placed", "Picking in Progress"])
            responsibility = "Operations"

        if status in {"Pending Verification", "Completed", "Quality Issue"}:
            picked_quantity = expected_quantity
        elif status == "Picking in Progress" and expected_quantity > 1:
            picked_quantity = rng.randint(0, expected_quantity - 1)
        else:
            picked_quantity = 0

        c.execute("""
            INSERT INTO order_header (
                order_number,
                request_id,
                date,
                source,
                destination,
                urgency,
                status,
                responsibility,
                expected_quantity,
                picked_quantity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_number,
            request_id,
            order_time.isoformat(),
            SOURCE_WAREHOUSE,
            destination,
            urgency,
            status,
            responsibility,
            expected_quantity,
            picked_quantity,
        ))

        for sku, qty in line_items:
            c.execute("INSERT INTO order_lines VALUES (?, ?, ?)", (order_number, sku, qty))

        if status in {"Completed", "Quality Issue"}:
            audit_time = order_time + timedelta(hours=rng.randint(2, 18))
            part_match = "No" if quality_issue and rng.random() < 0.5 else "Yes"
            damage = "Yes" if quality_issue and part_match == "Yes" else "No"
            qty_match = "No" if quality_issue and part_match == "Yes" and damage == "No" else "Yes"

            if not quality_issue:
                part_match = "Yes"
                damage = "No"
                qty_match = "Yes"

            c.execute("""
                INSERT INTO quality_audits (
                    audit_id,
                    audit_time,
                    order_number,
                    part_match,
                    damage,
                    qty_match,
                    result,
                    auditor_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                generate_audit_id(order_number),
                audit_time.isoformat(),
                order_number,
                part_match,
                damage,
                qty_match,
                "Failed" if quality_issue else "Passed",
                "Quality",
            ))

        inserted_orders.append(order_number)

    conn.commit()
    conn.close()

    return {
        "orders_created": len(inserted_orders),
        "quality_escalations": inserted_quality_issues,
        "week_start": business_days[0].date().isoformat(),
        "week_end": business_days[-1].date().isoformat(),
    }


def resolve_pending_orders():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT order_number, status, responsibility, date
        FROM order_header
        WHERE responsibility IN ('Operations', 'Quality')
          AND status != 'Completed'
        ORDER BY date ASC
    """)
    pending_orders = c.fetchall()

    resolved_count = 0
    audits_created = 0

    for order_number, status, responsibility, date_str in pending_orders:
        c.execute(
            "SELECT COUNT(*) FROM quality_audits WHERE order_number = ?",
            (order_number,),
        )
        audit_count = c.fetchone()[0]

        if audit_count == 0:
            audit_time = datetime.now().isoformat()
            if date_str:
                try:
                    order_time = parse_order_datetime(date_str)
                    audit_time = (order_time + timedelta(hours=2)).isoformat()
                except ValueError:
                    pass

            c.execute("""
                INSERT INTO quality_audits (
                    audit_id,
                    audit_time,
                    order_number,
                    part_match,
                    damage,
                    qty_match,
                    result,
                    auditor_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                generate_audit_id(order_number),
                audit_time,
                order_number,
                'Yes',
                'No',
                'Yes',
                'Passed',
                'Quality',
            ))
            audits_created += 1

        c.execute("""
            UPDATE order_header
            SET status = 'Completed',
                responsibility = 'Closed'
            WHERE order_number = ?
        """, (order_number,))
        resolved_count += 1

    conn.commit()
    conn.close()

    return {
        'orders_resolved': resolved_count,
        'audits_created': audits_created,
    }
    # New function to seed demo inventory
def seed_demo_inventory(conn, sku_count=120):
    c = conn.cursor()
    warehouses = [
        SOURCE_WAREHOUSE,
        "Los Angeles Warehouse 200",
        "San Francisco Warehouse 300",
        "San Bernardino Warehouse 400",
    ]
    location_prefixes = ["F", "A", "B", "C", "D"]
    seeded_rows = 0

    for index in range(sku_count):
        sku_number = 11000 + index
        sku = str(sku_number)
        base_price = round(8.5 + ((index * 1.73) % 115), 2)

        for wh_index, warehouse in enumerate(warehouses):
            location = f"{location_prefixes[wh_index % len(location_prefixes)]}{(index % 12) + 1:02d}"
            # Base band ~40–219 (+20 at source). Multiply for planner ATP headroom
            # so demo users can place larger multi-line orders without hard-stop shortages.
            quantity = 40 + ((index * 7 + wh_index * 11) % 180)
            if warehouse == SOURCE_WAREHOUSE:
                quantity += 20
            quantity *= 4

            c.execute(
                "INSERT INTO inventory VALUES (?, ?, ?, ?, ?)",
                (sku, warehouse, location, quantity, base_price),
            )
            seeded_rows += 1

    return seeded_rows

def _demo_seed_dependencies():
    # Plain dict (not an instance) so callables are not turned into bound methods.
    return {
        "seed_demo_inventory": seed_demo_inventory,
        "SOURCE_WAREHOUSE": SOURCE_WAREHOUSE,
        "DESTINATION_WAREHOUSES": DESTINATION_WAREHOUSES,
        "PICKER_ROSTER": PICKER_ROSTER,
        "log_inventory_transaction": log_inventory_transaction,
        "get_best_inventory_location": get_best_inventory_location,
        "create_supervisor_issue": create_supervisor_issue,
        "build_issue_type": build_issue_type,
        "SHORTAGE_ISSUE_TYPE": SHORTAGE_ISSUE_TYPE,
        "shortage_issue_audit_id": shortage_issue_audit_id,
        "calculate_sla_deadline": calculate_sla_deadline,
    }


def ensure_bootstrap_demo_data(force_orders: bool = False, purge_sessions: bool = False):
    """Ensure inventory + the exact 82-order completed recruiter baseline exist on master.

    When the master baseline is rebuilt (or purge_sessions=True), wipe demo_sessions/
    so every browser reclones the fresh 82-order / 0-pending template on the next request.
    Safe to call on every cold start (local or Render): never wipes a healthy baseline.
    """
    from wms_demo_seed import ensure_demo_order_baseline, DEMO_ORDER_COUNT, demo_baseline_present

    conn = get_conn(use_master=True)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM inventory")
    inventory_count = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM order_header")
    order_count = int(c.fetchone()[0] or 0)
    baseline_ok = demo_baseline_present(conn)

    result = {
        "seeded_inventory_rows": 0,
        "seeded_orders": 0,
        "seeded_quality_escalations": 0,
        "baseline_orders": order_count,
        "target_orders": DEMO_ORDER_COUNT,
        "purged_session_files": 0,
        "reseeded": False,
        "baseline_ok": baseline_ok,
    }

    deps = _demo_seed_dependencies()
    needs_order_seed = force_orders or (not baseline_ok) or order_count != DEMO_ORDER_COUNT

    # Full baseline rebuild reseeds inventory. Only top-up when empty and not rebuilding.
    if inventory_count == 0 and not needs_order_seed:
        result["seeded_inventory_rows"] = seed_demo_inventory(conn)
        conn.commit()

    seed_summary = ensure_demo_order_baseline(
        conn,
        force=bool(needs_order_seed),
        dependencies=deps,
    )
    result["seed_summary"] = seed_summary
    if seed_summary.get("seeded"):
        result["reseeded"] = True
        result["seeded_orders"] = int(seed_summary.get("orders_created") or 0)
        result["seeded_quality_escalations"] = int(seed_summary.get("quality_issues") or 0)
        result["seeded_inventory_rows"] = int(
            seed_summary.get("inventory_rows") or result["seeded_inventory_rows"]
        )
    result["baseline_orders"] = int(
        seed_summary.get("total_orders")
        or sqlite_order_count(MASTER_DB)
        or DEMO_ORDER_COUNT
    )
    result["baseline_ok"] = demo_baseline_present(conn)

    conn.commit()
    conn.close()

    # Stale visitor clones keep showing old pending-heavy data even when master is correct.
    if purge_sessions or result["reseeded"] or not baseline_ok:
        result["purged_session_files"] = purge_demo_session_databases()

    return result


_APP_BOOTSTRAPPED = False


def bootstrap_application(force_orders: bool = False, purge_sessions: bool = False):
    """Idempotent cold-start bootstrap for local `python whs_mgmt.py` and gunicorn/Render.

    Rebuilds the 82-order completed master baseline only when missing/off-baseline
    (e.g. ephemeral disk wiped on Render restart). Never clears a healthy baseline.
    """
    global _APP_BOOTSTRAPPED
    from wms_demo_seed import DEMO_ORDER_COUNT as _DEMO_ORDER_COUNT

    ensure_session_db_dir()
    migrate_legacy_db_to_master()
    init_db()

    try:
        load_inventory()
    except Exception as exc:
        print(f"Warning: could not load inventory spreadsheet: {exc}")

    master_orders = sqlite_order_count(MASTER_DB)
    master_ok = db_has_demo_baseline(MASTER_DB)
    stale_sessions = 0
    ensure_session_db_dir()
    for name in os.listdir(SESSION_DB_DIR):
        if not name.endswith(".db"):
            continue
        session_path = os.path.join(SESSION_DB_DIR, name)
        if os.path.isfile(session_path) and not db_has_demo_baseline(session_path):
            stale_sessions += 1

    bootstrap_result = ensure_bootstrap_demo_data(
        force_orders=force_orders or (not master_ok) or master_orders != _DEMO_ORDER_COUNT,
        purge_sessions=purge_sessions or (stale_sessions > 0) or (not master_ok),
    )

    if bootstrap_result.get("seeded_inventory_rows"):
        print(
            f"Seeded {bootstrap_result['seeded_inventory_rows']} demo inventory row(s) "
            "for the recruiter baseline."
        )
    if bootstrap_result.get("seeded_orders"):
        print(
            f"Seeded {bootstrap_result['seeded_orders']} completed demo order(s) "
            f"(0 pending) with {bootstrap_result['seeded_quality_escalations']} "
            "historical quality exception(s)."
        )
    if bootstrap_result.get("purged_session_files"):
        print(
            f"Purged {bootstrap_result['purged_session_files']} stale demo_sessions file(s) "
            "so visitors reclone the 82-order / 0-pending master."
        )
    print(
        f"Master demo baseline ready: {bootstrap_result.get('baseline_orders', 0)} order(s) "
        f"(target {_DEMO_ORDER_COUNT}, pending=0)."
    )

    conn = get_conn(use_master=True)
    normalized_rows = normalize_inventory_assignments(conn)
    normalized_urgency_rows = normalize_urgency_labels(conn)
    if normalized_rows:
        conn.commit()
        print(
            f"Normalized {normalized_rows} inventory row(s) with default assignment "
            f"{DEFAULT_INVENTORY_WAREHOUSE}/{DEFAULT_INVENTORY_LOCATION}."
        )
    if normalized_urgency_rows:
        conn.commit()
        print(f"Normalized {normalized_urgency_rows} order urgency value(s) to Standard/Urgent/Critical.")

    # With a completed-only baseline this is a no-op; kept for safety if data drifts.
    startup_blocked_orders = enforce_quality_gate_on_pending_orders(conn)
    if startup_blocked_orders:
        conn.commit()
        print(
            "Quality gate remediation moved "
            f"{len(startup_blocked_orders)} order(s) from Pending Verification to Picking in Progress."
        )
    conn.close()

    _APP_BOOTSTRAPPED = True
    return bootstrap_result


app = Flask(__name__)
app.secret_key = os.environ.get("WMS_SECRET_KEY", "enterprise-wms-linkedin-demo-secret")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
EXCEL_FILE = os.path.join(BASE_DIR, "WHS Management.xlsx")


@app.before_request
def bind_isolated_demo_session():
    # Static assets are unused in this monolith; still skip non-HTML noise safely.
    if request.endpoint == "static":
        return None
    # gunicorn/Render never hit __main__; bootstrap on first request if needed.
    if not _APP_BOOTSTRAPPED:
        bootstrap_application()
    ensure_visitor_session_db()
    return None

# ======================================================
# DATABASE INITIALIZATION
# ======================================================
def init_db():
    # Always initialize the shared master template (not a visitor clone).
    conn = get_conn(use_master=True)
    c = conn.cursor()


    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory ( 
            sku TEXT,
            warehouse TEXT,
            location TEXT,
            quantity INTEGER,
            price REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS order_header (
            order_number TEXT,
            request_id TEXT,
            date TEXT,
            source TEXT,
            destination TEXT,
            urgency TEXT,
            status TEXT,
            responsibility TEXT,
            expected_quantity INTEGER DEFAULT 0,
            picked_quantity INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS order_lines (
            order_number TEXT,
            sku TEXT,
            quantity INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_time TEXT,
            tx_code TEXT,
            order_number TEXT,
            sku TEXT,
            warehouse TEXT,
            location TEXT,
            qty_change INTEGER,
            qty_before INTEGER,
            qty_after INTEGER,
            user_role TEXT,
            notes TEXT
        )
    """)
    ensure_order_header_pick_tracking_schema(conn)

    c.execute("""
        CREATE TABLE IF NOT EXISTS quality_audits (
            audit_id TEXT PRIMARY KEY,
            audit_time TEXT,
            order_number TEXT,
            part_match TEXT,
            damage TEXT,
            qty_match TEXT,
            result TEXT,
            auditor_role TEXT
        )
    """)
    ensure_quality_audit_schema(conn)

    c.execute("""
        CREATE TABLE IF NOT EXISTS supervisor_quality_issues (
            issue_id TEXT PRIMARY KEY,
            order_number TEXT,
            audit_id TEXT UNIQUE,
            issue_date TEXT,
            picker_name TEXT,
            part_snapshot TEXT,
            issue_type TEXT,
            issue_status TEXT,
            root_cause TEXT,
            root_cause_notes TEXT,
            corrective_action TEXT,
            assigned_to TEXT,
            resolution_notes TEXT,
            attachment_name TEXT,
            attachment_path TEXT,
            resolved_flag INTEGER DEFAULT 0,
            resolved_at TEXT,
            closed_at TEXT,
            last_updated_at TEXT
        )
    """)
    ensure_ask_wms_chat_schema(conn)

    c.execute("""
        CREATE TRIGGER IF NOT EXISTS inventory_positive_insert_requires_assignment
        BEFORE INSERT ON inventory
        FOR EACH ROW
        WHEN NEW.quantity > 0
         AND (
            TRIM(COALESCE(NEW.warehouse, '')) = ''
            OR LOWER(TRIM(COALESCE(NEW.warehouse, ''))) = 'nan'
            OR TRIM(COALESCE(NEW.location, '')) = ''
            OR LOWER(TRIM(COALESCE(NEW.location, ''))) = 'nan'
         )
        BEGIN
            SELECT RAISE(ABORT, 'Positive inventory requires valid warehouse and location');
        END;
    """)

    c.execute("""
        CREATE TRIGGER IF NOT EXISTS inventory_positive_update_requires_assignment
        BEFORE UPDATE ON inventory
        FOR EACH ROW
        WHEN NEW.quantity > 0
         AND (
            TRIM(COALESCE(NEW.warehouse, '')) = ''
            OR LOWER(TRIM(COALESCE(NEW.warehouse, ''))) = 'nan'
            OR TRIM(COALESCE(NEW.location, '')) = ''
            OR LOWER(TRIM(COALESCE(NEW.location, ''))) = 'nan'
         )
        BEGIN
            SELECT RAISE(ABORT, 'Positive inventory requires valid warehouse and location');
        END;
    """)

    conn.commit()
    conn.close()

# ======================================================
# LOAD INVENTORY FROM EXCEL
# ======================================================
def load_inventory():
    if not os.path.exists(EXCEL_FILE):
        return

    xls = pd.ExcelFile(EXCEL_FILE)
    sheet_names = xls.sheet_names

    preferred_sheet = "SKUs and Locations"
    if preferred_sheet in sheet_names:
        selected_sheet = preferred_sheet
    else:
        # Try to find a reasonable alternative sheet before falling back.
        selected_sheet = None
        for candidate in sheet_names:
            lowered = candidate.lower().strip()
            if "sku" in lowered or "location" in lowered or "inventory" in lowered:
                selected_sheet = candidate
                break

        if selected_sheet is None:
            selected_sheet = sheet_names[0] if sheet_names else None

    if not selected_sheet:
        return

    df = pd.read_excel(EXCEL_FILE, sheet_name=selected_sheet)
    df.columns = df.columns.str.strip()

    warehouse_col = "Warehouse"
    if warehouse_col not in df.columns:
        for alt in ["Warehouses", "warehouse", "warehouses"]:
            if alt in df.columns:
                warehouse_col = alt
                break

    conn = get_conn()
    c = conn.cursor()

    for _, row in df.iterrows():
        sku_value = clean_display_text(row.get("SKU"), "")
        if not sku_value:
            continue

        quantity_raw = row.get("Quantity", 0)
        quantity_value = int(float(quantity_raw)) if pd.notna(quantity_raw) else 0
        price_raw = row.get("Price", 0)
        price_value = float(price_raw) if pd.notna(price_raw) else 0.0

        warehouse_value = clean_display_text(row.get(warehouse_col), "")
        location_value = clean_display_text(row.get("Location"), "")

        if quantity_value > 0 and (
            not has_valid_inventory_assignment(warehouse_value)
            or not has_valid_inventory_assignment(location_value)
        ):
            warehouse_value = DEFAULT_INVENTORY_WAREHOUSE
            location_value = DEFAULT_INVENTORY_LOCATION

        c.execute("""
            INSERT INTO inventory VALUES (?, ?, ?, ?, ?)
        """, (
            sku_value,
            warehouse_value,
            location_value,
            quantity_value,
            price_value
        ))

    normalize_inventory_assignments(conn)

    conn.commit()
    conn.close()

# ======================================================
# ENTERPRISE LAYOUT
# ======================================================
def layout(content, body_class=""):
    # Build HTML manually to avoid formatting conflicts with CSS braces
    html = """
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {box-sizing: border-box;}
        html {height: 100%;}
        :root {
            --ink-900: #101828;
            --ink-700: #344054;
            --ink-500: #667085;
            --line-200: #d9e2ec;
            --line-100: #edf2f7;
            --panel-100: #f8fafc;
            --panel-200: #ffffff;
            --accent-600: #2563eb;
            --accent-500: #3b82f6;
            --accent-100: #dbeafe;
            --success-600: #15803d;
            --warning-600: #b45309;
            --danger-600: #b42318;
            --shadow-soft: 0 12px 30px rgba(15, 23, 42, 0.08);
        }
        body {
            font-family: "SF Pro Display", "Segoe UI", Arial, sans-serif;
            background: linear-gradient(180deg, #f7f9fc 0%, #f3f6fb 100%);
            margin: 0;
            color: var(--ink-900);
            min-height: 100vh;
            padding-bottom: 52px;
        }
        .nav {
            background: rgba(255, 255, 255, 0.92);
            color: white;
            padding: 16px 20px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
            position: sticky;
            top: 0;
            z-index: 10;
            border-bottom: 1px solid rgba(217, 226, 236, 0.85);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            flex-wrap: wrap;
        }
        .nav-brand {
            color: var(--ink-900);
            font-weight: 800;
            letter-spacing: -0.02em;
            white-space: nowrap;
        }
        .nav-links {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 2px;
        }
        .nav a {
            color: var(--ink-700);
            margin-right: 8px;
            text-decoration: none;
            font-weight: 600;
            padding: 8px 12px;
            border-radius: 999px;
            transition: background 0.18s ease, color 0.18s ease;
        }
        .nav a:hover {background: var(--accent-100); color: var(--accent-600);}
        .nav-reset-form {display: inline; margin: 0;}
        .nav-reset-btn {
            background: #fff7ed;
            color: #9a3412 !important;
            border: 1px solid #fdba74;
            box-shadow: none;
            padding: 8px 12px;
            border-radius: 999px;
            font-weight: 700;
            cursor: pointer;
            margin-right: 0;
        }
        .nav-reset-btn:hover {background: #ffedd5; color: #7c2d12 !important;}
        .demo-session-note {
            margin: 0 0 18px 0;
            padding: 10px 14px;
            border-radius: 12px;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            color: #1e3a8a;
            font-size: 13px;
            font-weight: 600;
        }
        .flash-warning {
            margin: 0 0 18px 0;
            padding: 14px 16px;
            border-radius: 14px;
            background: #fff7ed;
            border: 1px solid #fdba74;
            color: #9a3412;
        }
        .flash-warning ul {margin: 8px 0 0 18px; padding: 0;}
        .ask-suggest {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 10px;
        }
        .ask-suggest button {
            background: #eff6ff;
            color: #1d4ed8;
            border: 1px solid #bfdbfe;
            box-shadow: none;
            font-size: 12px;
            padding: 7px 10px;
        }
        .ask-chip {
            background: #f8fafc !important;
            color: #334155 !important;
            border: 1px solid #dbe4f0 !important;
            border-radius: 999px !important;
            box-shadow: none !important;
            font-size: 12px !important;
            padding: 6px 12px !important;
            width: auto !important;
        }
        .ask-chip:hover {
            background: #eff6ff !important;
            color: #1d4ed8 !important;
            border-color: #bfdbfe !important;
        }
        .ask-layout {
            display: grid;
            grid-template-columns: 260px minmax(0, 1fr);
            gap: 16px;
            align-items: stretch;
            min-height: calc(100vh - 180px);
        }
        .ask-sidebar {
            background: #fff;
            border: 1px solid rgba(217, 226, 236, 0.9);
            border-radius: 20px;
            box-shadow: var(--shadow-soft);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            max-height: calc(100vh - 180px);
        }
        .ask-sidebar-title {
            margin: 0;
            font-size: 15px;
            font-weight: 800;
            color: var(--ink-900);
        }
        .ask-new-chat {
            width: 100%;
            box-shadow: none;
        }
        .ask-conversation-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
            overflow-y: auto;
            flex: 1;
            padding-right: 2px;
        }
        .ask-conversation-item {
            display: block;
            text-decoration: none;
            color: var(--ink-900);
            border: 1px solid #e2e8f0;
            background: #f8fafc;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
            font-weight: 600;
            line-height: 1.35;
        }
        .ask-conversation-item:hover {
            background: #eff6ff;
            border-color: #bfdbfe;
            color: #1d4ed8;
        }
        .ask-conversation-item.active {
            background: #eff6ff;
            border-color: #93c5fd;
            color: #1d4ed8;
        }
        .ask-conversation-meta {
            display: block;
            margin-top: 4px;
            font-size: 11px;
            font-weight: 600;
            color: #94a3b8;
        }
        .ask-chat-shell {
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: #fff;
            border: 1px solid rgba(217, 226, 236, 0.9);
            border-radius: 20px;
            box-shadow: var(--shadow-soft);
            padding: 18px;
            min-height: calc(100vh - 180px);
        }
        .ask-chat-log {
            display: flex;
            flex-direction: column;
            gap: 12px;
            flex: 1;
            max-height: none;
            min-height: 360px;
            overflow-y: auto;
            padding: 4px 2px 8px 2px;
        }
        .ask-bubble {
            border-radius: 16px;
            padding: 12px 14px;
            border: 1px solid #dbe4f0;
            background: #fff;
            max-width: 88%;
        }
        .ask-bubble.user {
            align-self: flex-end;
            background: #eff6ff;
            border-color: #bfdbfe;
        }
        .ask-bubble.assistant {
            align-self: flex-start;
            background: #f8fafc;
        }
        .ask-bubble-role {
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: #64748b;
            margin-bottom: 6px;
        }
        .ask-bubble-time {
            margin-top: 8px;
            font-size: 11px;
            color: #94a3b8;
        }
        .ask-headline {
            margin: 0;
            font-size: 15px;
            font-weight: 700;
            color: #0f172a;
            line-height: 1.5;
        }
        .ask-bullets {
            margin: 10px 0 0 0;
            padding-left: 18px;
            color: #334155;
        }
        .ask-bullets li {margin: 4px 0;}
        .ask-followup {
            margin: 10px 0 0 0;
            font-size: 13px;
            color: #475569;
        }
        .ask-composer {
            display: grid;
            gap: 10px;
            border-top: 1px solid #eef2f7;
            padding-top: 12px;
        }
        .ask-composer textarea {
            min-height: 72px;
            resize: vertical;
        }
        .ask-composer-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            justify-content: space-between;
        }
        .ask-page-header {
            margin-bottom: 14px;
        }
        .ask-page-header h1 {
            margin: 0 0 4px 0;
            font-size: 28px;
        }
        .ask-empty {
            margin: auto;
            text-align: center;
            color: #64748b;
            max-width: 420px;
            padding: 40px 12px;
        }
        @media (max-width: 980px) {
            .ask-layout {grid-template-columns: 1fr;}
            .ask-sidebar {max-height: 240px;}
            .ask-chat-shell, .ask-sidebar {min-height: 0;}
        }
        .container {
            padding: 32px 40px 48px 40px;
            max-width: 1440px;
            margin: 0 auto;
        }
        .footer-credit {
            position: fixed;
            right: 20px;
            bottom: 16px;
            z-index: 20;
            font-size: 13px;
            font-weight: 600;
            color: #667085;
            letter-spacing: 0.02em;
            text-shadow: 0 1px 1px rgba(255, 255, 255, 0.92);
            pointer-events: none;
        }
        .card {
            background: var(--panel-200);
            padding: 30px;
            border-radius: 20px;
            border: 1px solid rgba(217, 226, 236, 0.9);
            box-shadow: var(--shadow-soft);
            margin-bottom: 24px;
        }
        table {width: 100%; border-collapse: separate; border-spacing: 0;}
        th {
            background: #f8fafc;
            color: var(--ink-700);
            padding: 14px 16px;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        h1, h2, h3 {margin-top: 0; color: var(--ink-900);}
        h1 {font-size: 34px; letter-spacing: -0.04em; margin-bottom: 8px;}
        h2 {font-size: 28px; letter-spacing: -0.03em;}
        h3 {font-size: 18px; letter-spacing: -0.02em;}
        p {color: var(--ink-700); line-height: 1.6;}
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-top: 14px;
            margin-bottom: 10px;
        }
        .metric-tile {
            border-radius: 18px;
            padding: 18px;
            color: white;
            box-shadow: 0 10px 22px rgba(15, 23, 42, 0.15);
        }
        .tile-total {background: linear-gradient(135deg, #0b3a66, #2563eb);}
        .tile-compliance {background: linear-gradient(135deg, #0f766e, #14b8a6);}
        .tile-breached {background: linear-gradient(135deg, #991b1b, #ef4444);}
        .tile-quality {background: linear-gradient(135deg, #4c1d95, #7c3aed);}
        .metric-label {font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.06em;}
        .metric-value {font-size: 30px; font-weight: 700; margin-top: 6px; letter-spacing: -0.03em;}
        .split-grid {
            display: grid;
            grid-template-columns: 1.1fr 1fr;
            gap: 18px;
            margin-top: 10px;
        }
        .sla-bar-wrap {margin-top: 12px;}
        .sla-label {font-size: 13px; color: #334155; margin-bottom: 4px;}
        .sla-bar {
            height: 12px;
            border-radius: 999px;
            background: #e2e8f0;
            overflow: hidden;
        }
        .sla-fill {height: 100%;}
        .kpi-good {
    color: #16a34a;   /* green */
    font-weight: bold;
}

.kpi-warning {
    color: #f59e0b;   /* orange */
    font-weight: bold;
}

.kpi-bad {
    color: #dc2626;   /* red */
    font-weight: bold;
}
        td {
            padding: 14px 16px;
            border-bottom: 1px solid var(--line-100);
            vertical-align: top;
        }
        tr:hover td {background: rgba(37, 99, 235, 0.04);}
        button {
            background: var(--accent-600);
            color: white;
            padding: 11px 18px;
            border: none;
            border-radius: 12px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 10px 22px rgba(37, 99, 235, 0.16);
        }
        input, select, textarea {
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid #cbd5e1;
            background: white;
            color: var(--ink-900);
            font: inherit;
        }
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: #93c5fd;
            box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.12);
        }
        .quick-links {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 14px;
        }
        .quick-link {
            display: inline-flex;
            align-items: center;
            padding: 10px 14px;
            border-radius: 12px;
            text-decoration: none;
            color: var(--ink-900);
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            font-weight: 700;
        }
        .section-note {
            font-size: 13px;
            color: #64748b;
            margin-top: 4px;
        }
        .table-actions {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: nowrap;
        }
        .table-actions a {
            color: #2563eb;
            text-decoration: none;
            font-weight: 700;
            margin-right: 0;
            white-space: nowrap;
        }
        .nowrap-cell {
            white-space: nowrap;
        }
        .ops-workboard-grid {
            display: grid;
            grid-template-columns: minmax(280px, 1fr) minmax(360px, 1.2fr) minmax(280px, 1fr);
            gap: 18px;
            margin-top: 20px;
        }
        .ops-control-bar {
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 18px;
            flex-wrap: wrap;
            margin-top: 18px;
        }
        .ops-control-form {
            display: flex;
            align-items: end;
            gap: 10px;
            flex-wrap: wrap;
        }
        .ops-control-form label {
            display: grid;
            gap: 6px;
            font-size: 12px;
            font-weight: 700;
            color: #475467;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .ops-stream-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
            min-width: min(100%, 520px);
        }
        .ops-stream-stat {
            border-radius: 16px;
            padding: 14px 16px;
            border: 1px solid #dbe4f0;
            background: #f8fafc;
        }
        .ops-stream-stat span {
            display: block;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #64748b;
        }
        .ops-stream-stat strong {
            display: block;
            margin-top: 6px;
            font-size: 26px;
            color: #0f172a;
        }
        .ops-stream-stat.ready {
            background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
            border-color: #bfdbfe;
        }
        .ops-stream-stat.in-progress {
            background: linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%);
            border-color: #fed7aa;
        }
        .ops-stream-stat.completed {
            background: linear-gradient(135deg, #ecfdf3 0%, #dcfce7 100%);
            border-color: #bbf7d0;
        }
        .ops-stream-list {
            display: grid;
            gap: 16px;
            margin-top: 22px;
            max-height: 860px;
            overflow-y: auto;
            padding-right: 4px;
        }
        .ops-stage-strip {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 14px;
            padding-bottom: 14px;
            border-bottom: 1px solid #e2e8f0;
        }
        .ops-stage-pill {
            display: inline-flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
            line-height: 1.2;
        }
        .ops-stage-pill.stage-ready {
            background: #dbeafe;
            color: #1d4ed8;
        }
        .ops-stage-pill.stage-in-progress {
            background: #ffedd5;
            color: #c2410c;
        }
        .ops-stage-pill.stage-completed {
            background: #dcfce7;
            color: #166534;
        }
        .ops-lane {
            border: 1px solid #dbe4f0;
            border-radius: 22px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
            overflow: hidden;
            min-height: 0;
        }
        .ops-lane.is-ready {
            border-color: #bfdbfe;
            box-shadow: inset 0 1px 0 rgba(219, 234, 254, 0.7);
        }
        .ops-lane.is-in-progress {
            border-color: #fed7aa;
            box-shadow: inset 0 1px 0 rgba(255, 237, 213, 0.8);
        }
        .ops-lane.is-completed {
            border-color: #bbf7d0;
            box-shadow: inset 0 1px 0 rgba(220, 252, 231, 0.8);
        }
        .ops-lane-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            padding: 20px 20px 16px 20px;
            border-bottom: 1px solid #e2e8f0;
        }
        .ops-lane.is-ready .ops-lane-head {
            background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
        }
        .ops-lane.is-in-progress .ops-lane-head {
            background: linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%);
        }
        .ops-lane.is-completed .ops-lane-head {
            background: linear-gradient(135deg, #ecfdf3 0%, #dcfce7 100%);
        }
        .ops-lane-title {
            font-size: 18px;
            font-weight: 800;
            color: var(--ink-900);
            margin: 0;
        }
        .ops-lane-count {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 38px;
            padding: 7px 11px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
        }
        .ops-lane.is-ready .ops-lane-count {
            background: #1d4ed8;
            color: #ffffff;
        }
        .ops-lane.is-in-progress .ops-lane-count {
            background: #c2410c;
            color: #ffffff;
        }
        .ops-lane.is-completed .ops-lane-count {
            background: #15803d;
            color: #ffffff;
        }
        .ops-lane-list {
            max-height: 680px;
            overflow-y: auto;
            padding: 16px;
            display: grid;
            gap: 14px;
        }
        .ops-work-item {
            border: 1px solid #dbe4f0;
            border-radius: 18px;
            background: #ffffff;
            padding: 18px;
            box-shadow: 0 12px 26px rgba(15, 23, 42, 0.06);
        }
        .ops-work-item.state-ready {
            border-left: 6px solid #2563eb;
        }
        .ops-work-item.state-in-progress {
            border-left: 6px solid #ea580c;
        }
        .ops-work-item.state-completed {
            border-left: 6px solid #16a34a;
        }
        .ops-work-item-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
        }
        .ops-work-item-title a {
            color: var(--ink-900);
            text-decoration: none;
            font-size: 18px;
            font-weight: 800;
        }
        .ops-chip-row {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            justify-content: flex-end;
        }
        .ops-state-pill,
        .ops-sla-pill,
        .ops-priority-pill {
            display: inline-flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
            line-height: 1.2;
        }
        .ops-state-pill.state-ready {
            background: #dbeafe;
            color: #1d4ed8;
        }
        .ops-state-pill.state-in-progress {
            background: #ffedd5;
            color: #c2410c;
        }
        .ops-state-pill.state-completed {
            background: #dcfce7;
            color: #166534;
        }
        .ops-sla-pill.sla-healthy {
            background: #dcfce7;
            color: #166534;
        }
        .ops-sla-pill.sla-at-risk {
            background: #fff7ed;
            color: #b45309;
        }
        .ops-sla-pill.sla-breached {
            background: #fee2e2;
            color: #b91c1c;
        }
        .ops-priority-pill.priority-critical {
            background: #fee2e2;
            color: #b91c1c;
        }
        .ops-priority-pill.priority-urgent {
            background: #fff7ed;
            color: #b45309;
        }
        .ops-priority-pill.priority-standard {
            background: #eff6ff;
            color: #1d4ed8;
        }
        .ops-work-summary {
            margin-top: 6px;
            font-size: 13px;
            color: #64748b;
        }
        .ops-item-preview {
            margin-top: 14px;
            padding: 12px;
            border-radius: 14px;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
        }
        .ops-item-preview span {
            display: block;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #64748b;
            margin-bottom: 8px;
        }
        .ops-item-preview ul {
            margin: 0;
            padding-left: 18px;
            color: #0f172a;
            font-size: 13px;
        }
        .ops-item-preview li {
            margin-bottom: 4px;
        }
        .ops-item-preview li:last-child {
            margin-bottom: 0;
        }
        .ops-item-preview .ops-item-more {
            margin-top: 8px;
            font-size: 12px;
            color: #64748b;
            font-weight: 700;
        }
        .ops-detail-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 6px;
        }
        .ops-detail-table th,
        .ops-detail-table td {
            padding: 6px 4px;
            text-align: left;
            font-size: 12px;
            border-bottom: 1px solid #e2e8f0;
        }
        .ops-detail-table th {
            color: #64748b;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            background: transparent;
            white-space: nowrap;
            padding-top: 0;
        }
        .ops-detail-table tr:last-child td {
            border-bottom: none;
        }
        .ops-field-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
            margin-top: 14px;
        }
        .ops-field {
            padding: 12px;
            border-radius: 14px;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
        }
        .ops-field span {
            display: block;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #64748b;
            margin-bottom: 4px;
        }
        .ops-field strong {
            display: block;
            font-size: 15px;
            color: #0f172a;
        }
        .ops-progress-track {
            width: 100%;
            height: 10px;
            border-radius: 999px;
            background: #e2e8f0;
            overflow: hidden;
            margin-top: 8px;
        }
        .ops-progress-fill {
            height: 100%;
            border-radius: inherit;
            background: linear-gradient(90deg, #2563eb 0%, #0ea5e9 100%);
        }
        .ops-work-actions {
            margin-top: 16px;
        }
        .ops-progress-form {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 10px;
            align-items: end;
            margin-bottom: 12px;
        }
        .ops-progress-form label {
            display: grid;
            gap: 6px;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #64748b;
        }
        .ops-secondary-button {
            background: #fff7ed;
            color: #c2410c;
            border: 1px solid #fdba74;
            box-shadow: none;
            padding: 11px 16px;
        }
        .ops-secondary-button.takeover {
            background: #eff6ff;
            color: #1d4ed8;
            border-color: #93c5fd;
        }
        .ops-primary-form {
            margin: 0;
        }
        .ops-primary-button {
            width: 100%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 13px 16px;
            border-radius: 14px;
            border: none;
            color: #ffffff;
            font-size: 14px;
            font-weight: 800;
            cursor: pointer;
            box-shadow: none;
            text-decoration: none;
        }
        .ops-primary-button.ready {
            background: linear-gradient(135deg, #1d4ed8, #2563eb);
        }
        .ops-primary-button.in-progress {
            background: linear-gradient(135deg, #ea580c, #f97316);
        }
        .ops-primary-button.completed {
            background: linear-gradient(135deg, #15803d, #22c55e);
        }
        .ops-action-note {
            margin-top: 8px;
            font-size: 12px;
            color: #64748b;
        }
        .ops-readiness-banner {
            margin-top: 12px;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid transparent;
        }
        .ops-readiness-banner strong {
            display: block;
            font-size: 13px;
            color: #0f172a;
        }
        .ops-readiness-banner div {
            margin-top: 4px;
            font-size: 12px;
            color: #475467;
        }
        .ops-readiness-banner.ready {
            background: #ecfdf3;
            border-color: #b7e4c7;
        }
        .ops-readiness-banner.pending {
            background: #fff7ed;
            border-color: #fed7aa;
        }
        .ops-empty-state {
            border: 1px dashed #cbd5e1;
            border-radius: 16px;
            background: #f8fafc;
            padding: 30px 18px;
            text-align: center;
            color: #64748b;
            font-size: 14px;
        }
        .tile-completed {
            background: linear-gradient(135deg, #166534, #22c55e);
        }
        .sub-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-top: 16px;
        }
        .mini-panel {
            background: var(--panel-100);
            border: 1px solid var(--line-200);
            border-radius: 16px;
            padding: 16px;
        }
        .page-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 24px;
        }
        .page-header-copy {
            max-width: 760px;
        }
        .page-eyebrow {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 999px;
            background: #eff6ff;
            color: var(--accent-600);
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin-bottom: 12px;
        }
        .page-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            justify-content: flex-end;
        }
        .action-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 16px;
            border-radius: 14px;
            text-decoration: none;
            font-weight: 700;
            border: 1px solid transparent;
        }
        .action-btn.primary {
            background: var(--accent-600);
            color: white;
            box-shadow: 0 12px 22px rgba(37, 99, 235, 0.18);
        }
        .action-btn.secondary {
            background: white;
            color: var(--ink-900);
            border-color: var(--line-200);
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.06);
        }
        .dashboard-kpis {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .kpi-card {
            background: linear-gradient(180deg, #ffffff 0%, #f9fbff 100%);
            border: 1px solid var(--line-200);
            border-radius: 20px;
            padding: 18px;
            box-shadow: var(--shadow-soft);
        }
        .kpi-title {
            font-size: 12px;
            color: var(--ink-500);
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .kpi-number {
            font-size: 30px;
            font-weight: 700;
            letter-spacing: -0.04em;
            margin-top: 10px;
        }
        .kpi-footnote {
            margin-top: 8px;
            font-size: 13px;
            color: var(--ink-500);
        }
        .filter-panel {
            padding: 22px;
        }
        .filter-panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 18px;
        }
        .filter-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
        }
        .filter-field {
            display: flex;
            flex-direction: column;
            gap: 8px;
            position: relative;
        }
        .filter-label {
            font-size: 12px;
            font-weight: 700;
            color: var(--ink-500);
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .filter-field select,
        .filter-field input {
            width: 100%;
            min-height: 42px;
            padding: 10px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 12px;
            background: #fff;
            color: var(--ink-900);
            font: inherit;
        }
        .filter-field select:focus,
        .filter-field input:focus {
            outline: none;
            border-color: #93c5fd;
            box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.12);
        }
        .filter-foot {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-top: 18px;
            flex-wrap: wrap;
        }
        .inventory-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.8fr) minmax(290px, 0.9fr);
            gap: 20px;
            align-items: start;
        }
        .table-card, .side-card {
            background: white;
            border-radius: 20px;
            border: 1px solid var(--line-200);
            box-shadow: var(--shadow-soft);
        }
        .table-card-header, .side-card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 12px;
            padding: 22px 22px 0 22px;
        }
        .table-shell {
            overflow: auto;
            padding: 18px 22px 6px 22px;
        }
        .sticky-table thead th {
            position: sticky;
            top: 0;
            z-index: 1;
            background: #f8fafc;
            border-bottom: 1px solid var(--line-200);
        }
        .zebra tbody tr:nth-child(even) td {
            background: #fcfdff;
        }
        .sort-link {
            color: inherit;
            text-decoration: none;
            font-weight: 700;
        }
        .sort-indicator {
            color: var(--accent-600);
            margin-left: 6px;
        }
        .sku-cell {
            min-width: 170px;
        }
        .sku-title {
            font-weight: 700;
            color: var(--ink-900);
        }
        .value-strong {
            font-weight: 700;
            color: var(--ink-900);
        }
        .inventory-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        .text-link {
            color: var(--accent-600);
            text-decoration: none;
            font-weight: 700;
        }
        .expand-row td {
            background: #f8fbff;
        }
        .location-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
        }
        .location-card {
            background: white;
            border: 1px solid var(--line-200);
            border-radius: 16px;
            padding: 14px;
        }
        .location-detail-table th,
        .location-detail-table td {
            white-space: nowrap;
        }
        .location-warehouse-row td {
            background: #eff6ff !important;
            border-top: 1px solid #bfdbfe;
            border-bottom: 1px solid #bfdbfe;
        }
        .pagination {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 6px 22px 22px 22px;
            flex-wrap: wrap;
        }
        .pagination-links {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .pagination-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 38px;
            height: 38px;
            padding: 0 12px;
            border-radius: 12px;
            border: 1px solid var(--line-200);
            background: white;
            color: var(--ink-900);
            text-decoration: none;
            font-weight: 700;
        }
        .pagination-link.active {
            background: var(--accent-600);
            color: white;
            border-color: var(--accent-600);
        }
        .side-card {
            padding: 22px;
        }
        .alert-stack {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .alert-panel {
            border: 1px solid var(--line-200);
            border-radius: 18px;
            padding: 16px;
            background: #fbfcfe;
        }
        .alert-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
            margin-top: 12px;
        }
        .alert-item {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            padding: 12px 0;
            border-top: 1px solid var(--line-100);
        }
        .alert-item:first-child {
            border-top: none;
            padding-top: 0;
        }
        .pill {
            display: inline-flex;
            align-items: center;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
        }
        .pill.low {background: #fff7ed; color: var(--warning-600);}
        .pill.ok {background: #ecfdf3; color: var(--success-600);}
        .pill.issue {background: #fef3f2; color: var(--danger-600);}
        .message-banner {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 16px 18px;
            border-radius: 16px;
            margin-bottom: 20px;
            border: 1px solid transparent;
        }
        .message-banner.success {
            background: #ecfdf3;
            color: var(--success-600);
            border-color: #b7e4c7;
        }
        .message-banner.error {
            background: #fef3f2;
            color: var(--danger-600);
            border-color: #f5c2c0;
        }
        .message-banner.warning {
            background: #fff7ed;
            color: var(--warning-600);
            border-color: #fed7aa;
        }
        .toolbar-meta {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        .meta-chip {
            display: inline-flex;
            align-items: center;
            padding: 8px 12px;
            border-radius: 999px;
            background: #f8fafc;
            border: 1px solid var(--line-200);
            color: var(--ink-700);
            font-size: 13px;
            font-weight: 600;
        }
        .table-scroll {
            max-height: 520px;
            overflow: auto;
            border: 1px solid var(--line-100);
            border-radius: 16px;
            background: #fff;
        }
        .stack-card {
            background: white;
            border: 1px solid var(--line-200);
            border-radius: 20px;
            box-shadow: var(--shadow-soft);
            padding: 22px;
        }
        .card-split {
            display: grid;
            grid-template-columns: 0.95fr 1.3fr;
            gap: 20px;
        }
        .empty-state {
            padding: 30px 12px;
            text-align: center;
            color: var(--ink-500);
        }
        .transaction-summary {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin: 14px 0 18px 0;
        }
        /* ===== EXECUTIVE DASHBOARD STYLES ===== */
        .exec-kpi-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 28px;
        }
        .exec-kpi-card {
            background: white;
            border: 1px solid var(--line-200);
            border-radius: 22px;
            padding: 24px 26px;
            box-shadow: var(--shadow-soft);
            display: flex;
            flex-direction: column;
            gap: 4px;
            position: relative;
            overflow: hidden;
        }
        .exec-kpi-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 4px;
            border-radius: 22px 22px 0 0;
        }
        .exec-kpi-card.kpi-blue::before   { background: linear-gradient(90deg,#2563eb,#60a5fa); }
        .exec-kpi-card.kpi-green::before  { background: linear-gradient(90deg,#15803d,#4ade80); }
        .exec-kpi-card.kpi-amber::before  { background: linear-gradient(90deg,#b45309,#fbbf24); }
        .exec-kpi-card.kpi-violet::before { background: linear-gradient(90deg,#6d28d9,#a78bfa); }
        .exec-kpi-card.kpi-teal::before   { background: linear-gradient(90deg,#0f766e,#2dd4bf); }
        .exec-kpi-card.kpi-rose::before   { background: linear-gradient(90deg,#be123c,#fb7185); }
        .exec-kpi-eyebrow {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--ink-500);
            margin-top: 12px;
        }
        .exec-kpi-number {
            font-size: 42px;
            font-weight: 800;
            letter-spacing: -0.05em;
            color: var(--ink-900);
            line-height: 1;
        }
        .exec-kpi-sub {
            font-size: 13px;
            color: var(--ink-500);
            margin-top: 4px;
        }
        .exec-kpi-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            margin-top: 10px;
            align-self: flex-start;
        }
        .exec-kpi-badge.good  { background:#dcfce7; color:#166534; }
        .exec-kpi-badge.warn  { background:#fef3c7; color:#b45309; }
        .exec-kpi-badge.danger{ background:#fee2e2; color:#b91c1c; }
        .exec-quality-summary-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .exec-quality-summary-card {
            display: block;
            background: linear-gradient(180deg, #ffffff 0%, #fcfdff 100%);
            border: 1px solid var(--line-200);
            border-radius: 18px;
            padding: 18px 20px;
            box-shadow: var(--shadow-soft);
            text-decoration: none;
            color: inherit;
            transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
            position: relative;
            overflow: hidden;
        }
        .exec-quality-summary-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
        }
        .exec-quality-summary-card.quality-accuracy::before {
            background: linear-gradient(90deg, #6d28d9, #a78bfa);
        }
        .exec-quality-summary-card.quality-failed::before {
            background: linear-gradient(90deg, #b45309, #fbbf24);
        }
        .exec-quality-summary-card.quality-open::before {
            background: linear-gradient(90deg, #be123c, #fb7185);
        }
        .exec-quality-summary-card:hover {
            transform: translateY(-2px);
            border-color: #c7d7fe;
            box-shadow: 0 14px 28px rgba(15, 23, 42, 0.1);
        }
        .exec-quality-summary-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--ink-500);
        }
        .exec-quality-summary-value {
            font-size: 28px;
            font-weight: 800;
            letter-spacing: -0.04em;
            color: var(--ink-900);
            margin-top: 8px;
            line-height: 1.05;
        }
        .exec-quality-summary-sub {
            font-size: 13px;
            color: var(--ink-500);
            margin-top: 6px;
        }
        /* Pipeline */
        .pipeline-wrap {
            display: flex;
            align-items: stretch;
            gap: 0;
            overflow-x: auto;
            margin-top: 8px;
            padding-bottom: 4px;
        }
        .pipeline-stage {
            flex: 1;
            min-width: 110px;
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
        }
        .pipeline-stage + .pipeline-stage::before {
            content: '→';
            position: absolute;
            left: -10px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 22px;
            color: var(--ink-500);
            z-index: 1;
        }
        .pipeline-node {
            width: 100%;
            padding: 20px 10px;
            border-radius: 18px;
            border: 1.5px solid transparent;
            text-align: center;
            transition: transform 0.15s;
        }
        .pipeline-node:hover { transform: translateY(-3px); }
        .pipeline-count {
            font-size: 36px;
            font-weight: 800;
            letter-spacing: -0.04em;
            line-height: 1;
        }
        .pipeline-label {
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.03em;
            margin-top: 8px;
            opacity: 0.85;
        }
        .pipeline-sublabel {
            font-size: 10px;
            opacity: 0.65;
            margin-top: 3px;
        }
        .pipeline-exception {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 14px 20px;
            background: #fef3f2;
            border: 1.5px solid #fecaca;
            border-radius: 16px;
            margin-top: 16px;
        }
        /* Section headers */
        .exec-section-label {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--accent-600);
            background: var(--accent-100);
            padding: 5px 12px;
            border-radius: 999px;
            margin-bottom: 12px;
        }
        .exec-section-title {
            font-size: 20px;
            font-weight: 700;
            color: var(--ink-900);
            letter-spacing: -0.025em;
            margin: 0 0 4px 0;
        }
        .exec-section-desc {
            font-size: 13px;
            color: var(--ink-500);
            margin: 0 0 20px 0;
        }
        /* Chart grid layouts */
        .exec-chart-2col {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 24px;
        }
        .exec-chart-3col {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 24px;
        }
        .exec-chart-card {
            background: white;
            border: 1px solid var(--line-200);
            border-radius: 20px;
            padding: 22px 24px;
            box-shadow: var(--shadow-soft);
        }
        .exec-chart-card h4 {
            font-size: 15px;
            font-weight: 700;
            color: var(--ink-900);
            margin: 0 0 16px 0;
            letter-spacing: -0.015em;
        }
        .exec-chart-card img {
            width: 100%;
            border-radius: 10px;
            display: block;
        }
        /* Exception panel */
        .exec-exception-grid {
            display: grid;
            grid-template-columns: 1.4fr 1fr 1fr;
            gap: 20px;
            margin-bottom: 24px;
        }
        .exec-alert-item {
            display: flex;
            align-items: flex-start;
            gap: 12px;
            padding: 12px 0;
            border-bottom: 1px solid var(--line-100);
        }
        .exec-alert-item:last-child { border-bottom: none; }
        .exec-alert-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-top: 5px;
            flex-shrink: 0;
        }
        .exec-alert-dot.red    { background: #ef4444; }
        .exec-alert-dot.orange { background: #f97316; }
        .exec-alert-dot.yellow { background: #eab308; }
        .exec-alert-body {
            flex: 1;
        }
        .exec-alert-title {
            font-size: 13px;
            font-weight: 700;
            color: var(--ink-900);
        }
        .exec-alert-sub {
            font-size: 12px;
            color: var(--ink-500);
            margin-top: 2px;
        }
        /* Stat row */
        .exec-stat-row {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid var(--line-100);
        }
        .exec-stat-row:last-child { border-bottom: none; }
        .exec-stat-label { font-size: 13px; color: var(--ink-700); }
        .exec-stat-value { font-size: 18px; font-weight: 700; letter-spacing: -0.02em; color: var(--ink-900); }
        /* Progress bar in cards */
        .exec-prog-wrap { margin: 8px 0; }
        .exec-prog-label {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: var(--ink-700);
            margin-bottom: 4px;
        }
        .exec-prog-bar {
            height: 8px;
            border-radius: 999px;
            background: #e9edf5;
            overflow: hidden;
        }
        .exec-prog-fill {
            height: 100%;
            border-radius: 999px;
            transition: width 0.4s ease;
        }
        /* Score ring */
        .exec-score-ring {
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            width: 120px;
            height: 120px;
            border-radius: 50%;
            border: 10px solid;
            margin: 0 auto 12px;
        }
        .exec-score-ring-value {
            font-size: 26px;
            font-weight: 800;
            letter-spacing: -0.04em;
        }
        .exec-section-switcher {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 16px 0 8px 0;
        }
        .exec-switch-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 10px 14px;
            border-radius: 12px;
            border: 1px solid var(--line-200);
            background: white;
            color: var(--ink-700);
            text-decoration: none;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.18s ease;
        }
        .exec-switch-btn:hover {
            border-color: #93c5fd;
            color: var(--accent-600);
            transform: translateY(-1px);
        }
        .exec-switch-btn.active {
            background: var(--accent-600);
            border-color: var(--accent-600);
            color: white;
            box-shadow: 0 10px 20px rgba(37, 99, 235, 0.2);
        }
        .exec-view-section {
            display: none;
            animation: execFadeIn 0.18s ease;
        }
        .exec-view-section.active {
            display: block;
        }
        body.planner-body {
            overflow: hidden;
        }
        body.planner-body .container {
            height: calc(100dvh - 78px);
            max-width: none;
            padding: 12px 18px 14px 18px;
            overflow: hidden;
        }
        .planner-dashboard-shell {
            display: flex;
            flex-direction: column;
            gap: 12px;
            width: 100%;
            height: 100%;
            min-height: 0;
            overflow: hidden;
        }
        .planner-dashboard-header,
        .planner-dashboard-filters,
        .planner-table-panel {
            margin-bottom: 0;
        }
        .planner-dashboard-header,
        .planner-dashboard-filters {
            padding: 16px 18px;
        }
        .planner-dashboard-header {
            gap: 10px;
        }
        .planner-header-top,
        .planner-filter-top,
        .planner-create-actions {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }
        .planner-header-copy h2,
        .planner-filter-copy h3 {
            margin-bottom: 4px;
        }
        .planner-header-copy p,
        .planner-filter-copy p {
            margin: 0;
        }
        .planner-header-metrics {
            display: grid;
            grid-template-columns: repeat(2, minmax(140px, 1fr));
            gap: 10px;
            min-width: 320px;
        }
        .planner-metric {
            padding: 12px 14px;
            border-radius: 16px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
            border: 1px solid var(--line-200);
        }
        .planner-metric-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--ink-500);
        }
        .planner-metric-value {
            margin-top: 6px;
            font-size: 24px;
            font-weight: 800;
            letter-spacing: -0.03em;
            color: var(--ink-900);
        }
        .planner-metric-note {
            margin-top: 3px;
            font-size: 12px;
            color: var(--ink-500);
        }
        .planner-create-form {
            display: flex;
            flex-direction: column;
            gap: 10px;
            margin-top: 10px;
        }
        .planner-create-fields,
        .planner-filter-form {
            display: grid;
            gap: 10px;
        }
        .planner-create-fields {
            grid-template-columns: 1.2fr 1fr 1fr;
        }
        .planner-filter-form {
            grid-template-columns: repeat(6, minmax(120px, 1fr));
            align-items: end;
            margin-top: 10px;
        }
        .planner-form-field {
            display: flex;
            flex-direction: column;
            gap: 6px;
            min-width: 0;
        }
        .planner-form-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--ink-500);
        }
        .planner-static-field {
            display: flex;
            align-items: center;
            min-height: 42px;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid var(--line-200);
            background: var(--panel-100);
            color: var(--ink-900);
            font-weight: 700;
        }
        .planner-entry-shell {
            border: 1px solid var(--line-200);
            border-radius: 16px;
            overflow: hidden;
            background: white;
        }
        .planner-entry-table th,
        .planner-entry-table td,
        .planner-live-table th,
        .planner-live-table td {
            padding: 9px 10px;
        }
        .planner-entry-table th,
        .planner-live-table th {
            font-size: 11px;
            letter-spacing: 0.05em;
        }
        .planner-entry-table td,
        .planner-live-table td {
            font-size: 13px;
        }
        .planner-entry-table select,
        .planner-entry-table input,
        .planner-filter-form select {
            min-width: 0;
            width: 100%;
            padding: 8px 10px;
            min-height: 38px;
        }
        .planner-entry-table button,
        .planner-filter-form button,
        .planner-create-actions button {
            padding: 9px 12px;
            min-height: 38px;
            box-shadow: 0 8px 18px rgba(37, 99, 235, 0.14);
        }
        .planner-total-strip {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            padding: 10px 12px;
            border: 1px solid #bfdbfe;
            background: #eff6ff;
            border-radius: 14px;
        }
        .planner-total-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--accent-600);
        }
        .planner-total-value {
            font-size: 24px;
            font-weight: 800;
            letter-spacing: -0.03em;
            color: var(--ink-900);
        }
        .planner-total-note {
            font-size: 12px;
            color: var(--ink-500);
        }
        .planner-create-actions {
            align-items: center;
        }
        .planner-action-group {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }
        .planner-line-limit {
            margin: 0;
            font-size: 12px;
            font-weight: 700;
        }
        .planner-filter-top {
            align-items: center;
        }
        .planner-filter-summary {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }
        .planner-table-panel {
            display: flex;
            flex-direction: column;
            flex: 1;
            min-height: 0;
            padding: 0;
            overflow: hidden;
        }
        .planner-table-scroll {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            overflow-x: auto;
            border-radius: 20px;
        }
        .planner-table-scroll table {
            margin: 0;
        }
        .planner-table-scroll thead th {
            position: sticky;
            top: 0;
            z-index: 2;
            background: #f8fafc;
            border-bottom: 1px solid var(--line-200);
        }
        .planner-live-table td,
        .planner-live-table th {
            white-space: nowrap;
        }
        .planner-live-table tbody tr:nth-child(even) td {
            background: #fcfdff;
        }
        .planner-live-table tbody tr:hover td {
            background: rgba(37, 99, 235, 0.05);
        }
        .planner-empty-state {
            text-align: center;
            color: var(--ink-500);
            padding: 24px 12px;
        }
        .planner-order-link {
            color: var(--accent-600);
            text-decoration: none;
            font-weight: 700;
        }
        .planner-filter-form .planner-form-field:last-child {
            align-self: end;
        }
        @keyframes execFadeIn {
            from { opacity: 0; transform: translateY(2px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @media (max-width: 1200px) {
            .exec-kpi-grid { grid-template-columns: repeat(2, 1fr); }
            .exec-quality-summary-grid { grid-template-columns: repeat(2, 1fr); }
            .exec-chart-2col { grid-template-columns: 1fr; }
            .exec-chart-3col { grid-template-columns: 1fr; }
            .exec-exception-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 1100px) {
            .inventory-grid,
            .card-split,
            .split-grid,
            .ops-workboard-grid {
                grid-template-columns: 1fr;
            }
        }
        @media (max-width: 720px) {
            .container {
                padding: 20px 16px 32px 16px;
            }
            body {
                padding-bottom: 64px;
            }
            body.planner-body .container {
                height: calc(100dvh - 74px);
                padding: 10px 12px 12px 12px;
            }
            .footer-credit {
                right: 12px;
                bottom: 10px;
                font-size: 12px;
            }
            .planner-create-fields,
            .planner-filter-form,
            .planner-header-metrics {
                grid-template-columns: 1fr;
            }
            .planner-dashboard-header,
            .planner-dashboard-filters {
                padding: 14px;
            }
            .exec-quality-summary-grid {
                grid-template-columns: 1fr;
            }
            .page-header,
            .filter-panel-header,
            .pagination,
            .filter-foot {
                flex-direction: column;
                align-items: stretch;
            }
            .page-actions {
                justify-content: stretch;
            }
            .ops-stage-strip,
            .ops-lane-head,
            .ops-work-item-head {
                flex-direction: column;
            }
            .ops-chip-row {
                justify-content: flex-start;
            }
            .ops-work-actions a {
                width: 100%;
            }
            .action-btn,
            button {
                width: 100%;
            }
        }
    </style>
    </head>
    <body class="__BODY_CLASS__">
    <div class="nav">
        <span class="nav-brand">DigiTech WMS</span>
        <div class="nav-links">
            <a href="/executive">Warehouse Executive Dashboard</a>
            <a href="/planner">Planner</a>
            <a href="/inventory">Inventory</a>
            <a href="/operations">Operations</a>
            <a href="/quality">Quality</a>
            <a href="/supervisor">Supervisor</a>
            <a href="/ask-wms">Ask WMS</a>
            <form class="nav-reset-form" method="post" action="/reset-demo" onsubmit="return confirm('Reset this demo session to the original sample warehouse data? Your orders, picks, and adjustments in this browser will be cleared.');">
                <button class="nav-reset-btn" type="submit">Reset Demo</button>
            </form>
        </div>
    </div>

    <div class="container">
    <p class="demo-session-note">Private demo session &mdash; your warehouse actions stay in this browser only. Use Reset Demo anytime to restore the shared 82-order completed baseline (0 pending).</p>
    """
    html += content
    html += """
    </div>
    <div class="footer-credit">Developed by Jesus Sanchez</div>
    </body>
    </html>
    """
    return html.replace("__BODY_CLASS__", body_class)

# ======================================================
# DASHBOARD WITH REAL SLA ENGINE
# ======================================================
@app.route("/")
@app.route("/dashboard")
def dashboard():
    # Keep backward compatibility for / and /dashboard while using one
    # canonical dashboard implementation under /executive.
    return redirect("/executive")


@app.route("/reset-demo", methods=["POST", "GET"])
def reset_demo():
    reset_visitor_demo_data()
    return redirect("/executive?demo_reset=1")


def build_ops_snapshot(conn):
    c = conn.cursor()
    snapshot = {
        "orders_total": 0,
        "orders_placed": 0,
        "picking": 0,
        "pending_verification": 0,
        "completed": 0,
        "blocked": 0,
        "quality_issue": 0,
        "open_quality_issues": 0,
        "open_shortage_issues": 0,
        "sku_count": 0,
        "units_on_hand": 0,
        "inventory_value": 0.0,
        "sla_healthy": 0,
        "sla_at_risk": 0,
        "sla_breached": 0,
        "audits_total": 0,
        "audits_passed": 0,
        "audits_failed": 0,
    }

    c.execute("SELECT status, COUNT(*) FROM order_header GROUP BY status")
    for status, count in c.fetchall():
        snapshot["orders_total"] += int(count or 0)
        key_map = {
            "Orders Placed": "orders_placed",
            "Picking in Progress": "picking",
            "Pending Verification": "pending_verification",
            "Completed": "completed",
            "Blocked": "blocked",
            "Quality Issue": "quality_issue",
        }
        mapped = key_map.get(clean_display_text(status, ""))
        if mapped:
            snapshot[mapped] = int(count or 0)

    c.execute(
        """
        SELECT COUNT(*)
        FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
          AND COALESCE(issue_type, '') != ?
        """,
        (SHORTAGE_ISSUE_TYPE,),
    )
    snapshot["open_quality_issues"] = int(c.fetchone()[0] or 0)

    c.execute(
        """
        SELECT COUNT(*)
        FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
          AND COALESCE(issue_type, '') = ?
        """,
        (SHORTAGE_ISSUE_TYPE,),
    )
    snapshot["open_shortage_issues"] = int(c.fetchone()[0] or 0)

    c.execute("SELECT COUNT(DISTINCT sku), COALESCE(SUM(quantity), 0) FROM inventory WHERE quantity > 0")
    sku_count, units = c.fetchone()
    snapshot["sku_count"] = int(sku_count or 0)
    snapshot["units_on_hand"] = int(units or 0)

    c.execute("SELECT COALESCE(SUM(CAST(quantity AS REAL) * price), 0) FROM inventory WHERE quantity > 0")
    snapshot["inventory_value"] = float(c.fetchone()[0] or 0)

    c.execute("SELECT COUNT(*) FROM quality_audits")
    snapshot["audits_total"] = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result IN ('Pass', 'Passed')")
    snapshot["audits_passed"] = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result='Failed'")
    snapshot["audits_failed"] = int(c.fetchone()[0] or 0)

    now = now_pt()
    c.execute("SELECT urgency, status, date FROM order_header WHERE status != 'Completed'")
    for urgency, status, date_str in c.fetchall():
        try:
            order_time = parse_order_datetime(date_str)
            sla_key, _ = calculate_sla_status(order_time, normalize_urgency(urgency, "Standard"), now)
        except Exception:
            continue
        if sla_key == "healthy":
            snapshot["sla_healthy"] += 1
        elif sla_key == "at_risk":
            snapshot["sla_at_risk"] += 1
        else:
            snapshot["sla_breached"] += 1

    return snapshot


def create_ask_wms_conversation(conn, title="New conversation"):
    c = conn.cursor()
    stamp = now_pt().isoformat()
    c.execute(
        """
        INSERT INTO ask_wms_conversations (title, created_at, updated_at)
        VALUES (?, ?, ?)
        """,
        (clean_display_text(title, "New conversation")[:80], stamp, stamp),
    )
    return c.lastrowid


def list_ask_wms_conversations(conn, limit=40):
    c = conn.cursor()
    c.execute(
        """
        SELECT id, title, created_at, updated_at
        FROM ask_wms_conversations
        ORDER BY datetime(updated_at) DESC, id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    return [
        {
            "id": row[0],
            "title": clean_display_text(row[1], "Conversation"),
            "created_at": row[2],
            "updated_at": row[3],
        }
        for row in c.fetchall()
    ]


def get_ask_wms_conversation(conn, conversation_id):
    c = conn.cursor()
    c.execute(
        """
        SELECT id, title, created_at, updated_at
        FROM ask_wms_conversations
        WHERE id = ?
        """,
        (conversation_id,),
    )
    row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "title": clean_display_text(row[1], "Conversation"),
        "created_at": row[2],
        "updated_at": row[3],
    }


def touch_ask_wms_conversation(conn, conversation_id, title=None):
    c = conn.cursor()
    if title:
        c.execute(
            """
            UPDATE ask_wms_conversations
            SET title = ?, updated_at = ?
            WHERE id = ?
            """,
            (clean_display_text(title, "Conversation")[:80], now_pt().isoformat(), conversation_id),
        )
    else:
        c.execute(
            """
            UPDATE ask_wms_conversations
            SET updated_at = ?
            WHERE id = ?
            """,
            (now_pt().isoformat(), conversation_id),
        )


def save_ask_wms_message(conn, conversation_id, role, message, meta=None):
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO ask_wms_chat (conversation_id, created_at, role, message, meta_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            now_pt().isoformat(),
            clean_display_text(role, "assistant"),
            message or "",
            json.dumps(meta or {}),
        ),
    )
    touch_ask_wms_conversation(conn, conversation_id)


def list_ask_wms_chat(conn, conversation_id, limit=120):
    c = conn.cursor()
    c.execute(
        """
        SELECT id, created_at, role, message, meta_json
        FROM ask_wms_chat
        WHERE conversation_id = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (conversation_id, max(1, int(limit))),
    )
    rows = []
    for row_id, created_at, role, message, meta_json in c.fetchall():
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta = {}
        rows.append(
            {
                "id": row_id,
                "created_at": created_at,
                "role": clean_display_text(role, "assistant"),
                "message": message or "",
                "meta": meta if isinstance(meta, dict) else {},
            }
        )
    return rows


def clear_ask_wms_chat(conn, conversation_id=None):
    c = conn.cursor()
    if conversation_id:
        c.execute("DELETE FROM ask_wms_chat WHERE conversation_id = ?", (conversation_id,))
        c.execute("DELETE FROM ask_wms_conversations WHERE id = ?", (conversation_id,))
    else:
        c.execute("DELETE FROM ask_wms_chat")
        c.execute("DELETE FROM ask_wms_conversations")
    return c.rowcount


def compose_ask_response(headline, bullets=None, follow_up=""):
    safe_headline = html_escape(clean_display_text(headline, ""))
    html = f"<p class='ask-headline'>{safe_headline}</p>"
    clean_bullets = [clean_display_text(item, "") for item in (bullets or []) if clean_display_text(item, "")]
    if clean_bullets:
        items = "".join(f"<li>{html_escape(item)}</li>" for item in clean_bullets[:5])
        html += f"<ul class='ask-bullets'>{items}</ul>"
    if follow_up:
        html += f"<p class='ask-followup'><strong>Next:</strong> {html_escape(clean_display_text(follow_up, ''))}</p>"
    return html


def normalize_ask_question(question):
    text = clean_display_text(question, "").lower()
    text = text.replace("on-hand", "on hand").replace("onhand", "on hand")
    text = re.sub(r"[^\w\s/\-:]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    spelling_map = {
        "shiped": "shipped",
        "shippd": "shipped",
        "shiiped": "shipped",
        "completd": "completed",
        "recieved": "received",
        "recive": "received",
        "inventroy": "inventory",
        "inventor": "inventory",
        "avaliable": "available",
        "availible": "available",
        "quanity": "quantity",
        "qantity": "quantity",
        "qty": "quantity",
        "superviser": "supervisor",
        "supervisr": "supervisor",
        "verificaton": "verification",
        "verfication": "verification",
        "pendng": "pending",
        "blockd": "blocked",
        "shortge": "shortage",
        "shortages": "shortage",
        "pickers": "picker",
        "orders": "order",
        "skus": "sku",
        "parts": "part",
        "pn": "part",
        "procurement": "planner",
        "dashbord": "dashboard",
        "exective": "executive",
        "tommorow": "tomorrow",
        "yesturday": "yesterday",
    }
    tokens = []
    for token in text.split():
        tokens.append(spelling_map.get(token, token))
    return " ".join(tokens)


def extract_ask_sku(normalized_question, original_question=""):
    for source in (original_question, normalized_question):
        match = re.search(r"\b(?:sku|part|item|pn)?\s*#?\s*([0-9]{4,6})\b", source, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def extract_ask_date(normalized_question):
    now = now_pt()
    q = normalized_question

    if "today" in q:
        return now.date(), "today"
    if "yesterday" in q:
        return (now - timedelta(days=1)).date(), "yesterday"

    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", q)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return datetime(year, month, day, tzinfo=PACIFIC_TZ).date(), f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            pass

    us_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", q)
    if us_match:
        month, day, year = map(int, us_match.groups())
        try:
            return datetime(year, month, day, tzinfo=PACIFIC_TZ).date(), f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            pass

    month_map = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month_match = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2})(?:,?\s*(20\d{2}))?\b",
        q,
    )
    if month_match:
        month_token = month_match.group(1)
        month = month_map.get(month_token) or month_map.get(month_token[:3])
        day = int(month_match.group(2))
        year = int(month_match.group(3) or now.year)
        if month:
            try:
                return datetime(year, month, day, tzinfo=PACIFIC_TZ).date(), f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                pass

    return None, ""


def extract_requested_qty(normalized_question):
    patterns = [
        r"\b(?:for|of|need|needs|request(?:ing)?|order(?:ing)?)\s+(\d{1,6})\b",
        r"\b(\d{1,6})\s+(?:units?|parts?|qty|quantity)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_question)
        if match:
            return int(match.group(1))
    return None


def classify_ask_intent(normalized_question):
    q = normalized_question
    scores = {
        "summary": 0,
        "executive": 0,
        "planner": 0,
        "inventory": 0,
        "operations": 0,
        "quality": 0,
        "supervisor": 0,
        "unknown": 0,
    }

    def bump(intent, weight=1):
        scores[intent] += weight

    if any(token in q for token in ("summary", "summarize", "overview", "snapshot", "executive dashboard", "how is the warehouse")):
        bump("summary", 4)
    if any(token in q for token in ("dashboard", "kpi", "sla", "shipped", "received", "pending order", "inventory value")):
        bump("executive", 3)
    if any(token in q for token in ("planner", "procurement", "destination", "urgency", "can we order", "support a request")):
        bump("planner", 3)
    if any(token in q for token in ("inventory", "stock", "on hand", "available", "part", "sku", "low stock", "overstock", "warehouse value", "most", "least", "highest", "lowest", "top")):
        bump("inventory", 3)
    if any(token in q for token in ("most inventory", "highest stock", "top sku", "part number", "which part", "which sku")):
        bump("inventory", 4)
    if any(token in q for token in ("operation", "picker", "picking", "blocked", "shortage", "workboard", "workload", "behind")):
        bump("operations", 3)
    if any(token in q for token in ("quality", "verification", "audit", "qa", "defect", "damage", "pass rate")):
        bump("quality", 3)
    if any(token in q for token in ("supervisor", "escalation", "root cause", "corrective")):
        bump("supervisor", 3)

    if "shipped" in q or "completed" in q:
        bump("executive", 2)
    if "blocked" in q or "shortage" in q:
        bump("operations", 2)
    if "verification" in q:
        bump("quality", 2)

    best_intent = max(scores, key=scores.get)
    if scores[best_intent] <= 0:
        return "unknown"
    return best_intent


def _ask_date_bounds(day_value):
    start = datetime(day_value.year, day_value.month, day_value.day, tzinfo=PACIFIC_TZ)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def extract_ask_top_n(normalized_question, default=1):
    match = re.search(r"\btop\s+(\d{1,2})\b", normalized_question)
    if match:
        return max(1, min(int(match.group(1)), 20))
    if any(token in normalized_question for token in ("top", "runners up", "runner up", "leaderboard")):
        return 5
    return default


def detect_inventory_ranking_intent(normalized_question):
    q = normalized_question
    inventoryish = any(
        token in q
        for token in ("inventory", "stock", "on hand", "part", "sku", "item", "quantity", "units")
    )
    mostish = any(token in q for token in ("most", "highest", "largest", "greatest", "top", "biggest"))
    leastish = any(token in q for token in ("least", "lowest", "smallest", "fewest", "minimum"))
    valueish = "value" in q or "dollar" in q or "worth" in q
    which_part = ("which" in q or "what" in q) and any(token in q for token in ("part", "sku", "item"))
    if not inventoryish and not which_part:
        return None
    if leastish:
        return "least_qty"
    if valueish and (mostish or "top" in q or which_part):
        return "most_value"
    if mostish or which_part:
        return "most_qty"
    return None


def query_sku_inventory_ranks(conn, mode="most_qty", limit=5):
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
        SELECT
            sku,
            COALESCE(SUM(quantity), 0) AS qty,
            COALESCE(SUM(CAST(quantity AS REAL) * price), 0) AS val
        FROM inventory
        WHERE quantity > 0
          AND TRIM(COALESCE(sku, '')) <> ''
        GROUP BY sku
        {order_sql}
        LIMIT ?
        """,
        (limit,),
    )
    rows = []
    for sku, qty, val in c.fetchall():
        rows.append(
            {
                "sku": clean_display_text(sku, ""),
                "qty": int(qty or 0),
                "value": float(val or 0),
                "description": sku_semiconductor_description(sku),
            }
        )
    return rows


def answer_ask_wms(conn, question, prior_context=None, debug=False):
    """Executive Ask WMS entrypoint: zero-paid-API intent engine over session DB."""
    import wms_ask_engine

    answer_html, snapshot, context = wms_ask_engine.answer_question(
        conn,
        question,
        prior_context=prior_context or {},
        warehouses=list(WAREHOUSE_NETWORK),
        source_warehouse=SOURCE_WAREHOUSE,
        low_stock_threshold=DEFAULT_LOW_STOCK_ALERT_THRESHOLD,
        shortage_issue_type=SHORTAGE_ISSUE_TYPE,
        picker_roster=list(PICKER_ROSTER),
        now_pt=now_pt,
        parse_order_datetime=parse_order_datetime,
        normalize_urgency=normalize_urgency,
        calculate_sla_status=calculate_sla_status,
        calculate_sla_deadline=calculate_sla_deadline,
        sku_description=sku_semiconductor_description,
        debug=debug,
    )
    return answer_html, snapshot, context


def _answer_ask_wms_legacy(conn, question):
    original = clean_display_text(question, "")
    q = normalize_ask_question(original)
    # Keep multi-word phrases useful for ranking detection.
    q = q.replace("part number", "part").replace("part no", "part").replace("item number", "part")
    c = conn.cursor()
    snapshot = build_ops_snapshot(conn)

    if not q:
        return compose_ask_response(
            "Ask a warehouse question and I will answer from this demo session's live data.",
            [
                "Try shipped counts by date, SKU availability, blocked orders, quality queues, or SLA risk.",
            ],
            "Ask: How many orders shipped today?",
        ), snapshot, {"intent": "unknown"}

    sku_value = extract_ask_sku(q, original)
    day_value, day_label = extract_ask_date(q)
    requested_qty = extract_requested_qty(q)
    intent = classify_ask_intent(q)
    ranking_mode = detect_inventory_ranking_intent(q)
    top_n = extract_ask_top_n(q, default=5 if "top" in q else 1)
    meta = {"intent": intent, "sku": sku_value, "date": day_label, "ranking": ranking_mode}

    # Catalog-level SKU count ("how many SKUs available/active?")
    asks_sku_catalog_count = (
        not sku_value
        and "sku" in q
        and (
            "how many" in q
            or "number of" in q
            or "count of" in q
            or q.startswith("sku available")
            or "skus available" in original.lower()
        )
        and any(
            token in q
            for token in (
                "available",
                "active",
                "total",
                "in inventory",
                "in stock",
                "do we have",
                "are there",
                "have",
            )
        )
    )
    if asks_sku_catalog_count or (not sku_value and re.search(r"\bhow many sku\b", q)):
        return compose_ask_response(
            f"There are {snapshot['sku_count']} active SKUs available in this demo warehouse.",
            [
                f"Total units on hand: {snapshot['units_on_hand']:,}",
                f"Inventory value: ${snapshot['inventory_value']:,.2f}",
            ],
            "Ask which part number has the most inventory.",
        ), snapshot, {"intent": "inventory", "sku": ""}

    # Ranking: most/least/top inventory by qty or value
    if ranking_mode and not sku_value:
        rows = query_sku_inventory_ranks(conn, mode=ranking_mode, limit=max(top_n, 5))
        if not rows:
            return compose_ask_response(
                "No positive on-hand inventory was found in this demo session.",
                [],
                "Ask how many SKUs are available.",
            ), snapshot, meta

        lead = rows[0]
        if ranking_mode == "least_qty":
            headline = (
                f"SKU {lead['sku']} has the least inventory with {lead['qty']:,} unit(s)."
            )
            follow = "Ask for low-stock SKUs below the alert threshold."
        elif ranking_mode == "most_value":
            headline = (
                f"SKU {lead['sku']} has the highest inventory value at ${lead['value']:,.2f}."
            )
            follow = "Ask for the top 5 SKUs by inventory value."
        else:
            headline = (
                f"SKU {lead['sku']} has the most inventory with {lead['qty']:,} unit(s)."
            )
            follow = "Ask for the top 5 SKUs by on-hand quantity."

        show_n = top_n if top_n > 1 else min(4, len(rows))
        bullets = [f"{lead['sku']}: {lead['description']}"]
        if ranking_mode == "most_value":
            bullets.extend(
                f"#{idx}. SKU {row['sku']}: ${row['value']:,.2f} ({row['qty']:,} units)"
                for idx, row in enumerate(rows[:show_n], start=1)
            )
        else:
            bullets.extend(
                f"#{idx}. SKU {row['sku']}: {row['qty']:,} units"
                for idx, row in enumerate(rows[:show_n], start=1)
            )
        bullets.append(f"Active SKUs in catalog: {snapshot['sku_count']}")
        return compose_ask_response(headline, bullets, follow), snapshot, meta

    # Inventory / part availability (highest precision for LinkedIn demos)
    if sku_value and (
        intent in {"inventory", "planner", "unknown"}
        or any(token in q for token in ("part", "sku", "stock", "on hand", "available", "left", "remain", "quantity"))
    ):
        available = get_available_inventory_qty(conn, sku_value, SOURCE_WAREHOUSE)
        warehouse, location, best_qty = get_best_inventory_location(conn, sku_value, SOURCE_WAREHOUSE)
        description = sku_semiconductor_description(sku_value)
        c.execute(
            """
            SELECT warehouse, COALESCE(SUM(quantity), 0)
            FROM inventory
            WHERE sku = ? AND quantity > 0
            GROUP BY warehouse
            ORDER BY SUM(quantity) DESC
            """,
            (sku_value,),
        )
        by_wh = c.fetchall()
        bullets = [
            f"Source warehouse ({SOURCE_WAREHOUSE}): {available} unit(s)",
            f"Best pick location: {location} at {warehouse} ({best_qty} on hand)",
        ]
        if by_wh:
            bullets.append(
                "Network on-hand: "
                + ", ".join(f"{clean_display_text(wh, 'Warehouse')}: {int(qty)}" for wh, qty in by_wh[:4])
            )
        follow = f"Ask whether SKU {sku_value} can support a specific order quantity."
        if requested_qty is not None:
            if requested_qty <= available:
                headline = (
                    f"SKU {sku_value} ({description}) can support {requested_qty} unit(s). "
                    f"Available at source: {available} unit(s)."
                )
            else:
                headline = (
                    f"SKU {sku_value} ({description}) cannot support {requested_qty} unit(s). "
                    f"Only {available} unit(s) are available at {SOURCE_WAREHOUSE}."
                )
            follow = "Open Planner to place an order within available quantity."
        else:
            headline = (
                f"SKU {sku_value} ({description}) has {available} part(s)/unit(s) left at {SOURCE_WAREHOUSE}."
            )
        return compose_ask_response(headline, bullets, follow), snapshot, meta

    # Shipped / completed by date
    if any(token in q for token in ("shipped", "completed", "closed")) and (
        "order" in q or "how many" in q or day_value is not None or "today" in q or "yesterday" in q
    ):
        if day_value is None:
            day_value = now_pt().date()
            day_label = "today"
            meta["assumption"] = "Interpreted missing date as today."
        start_iso, end_iso = _ask_date_bounds(day_value)
        c.execute(
            """
            SELECT COUNT(*)
            FROM order_header
            WHERE status = 'Completed'
              AND date >= ?
              AND date < ?
            """,
            (start_iso, end_iso),
        )
        shipped_count = int(c.fetchone()[0] or 0)
        c.execute(
            """
            SELECT order_number
            FROM order_header
            WHERE status = 'Completed'
              AND date >= ?
              AND date < ?
            ORDER BY date DESC
            LIMIT 8
            """,
            (start_iso, end_iso),
        )
        sample = [row[0] for row in c.fetchall()]
        bullets = [f"Filter used: status=Completed, date={day_label}"]
        if sample:
            bullets.append("Sample orders: " + ", ".join(sample))
        if meta.get("assumption"):
            bullets.append(meta["assumption"])
        return compose_ask_response(
            f"{shipped_count} order(s) shipped/completed on {day_label}.",
            bullets,
            "Ask for orders received on the same date for inflow vs outflow.",
        ), snapshot, meta

    # Orders received by date
    if any(token in q for token in ("received", "placed", "created")) and ("order" in q or "how many" in q):
        if day_value is None:
            day_value = now_pt().date()
            day_label = "today"
            meta["assumption"] = "Interpreted missing date as today."
        start_iso, end_iso = _ask_date_bounds(day_value)
        c.execute(
            """
            SELECT COUNT(*)
            FROM order_header
            WHERE date >= ? AND date < ?
            """,
            (start_iso, end_iso),
        )
        received_count = int(c.fetchone()[0] or 0)
        bullets = [f"Filter used: all orders with created date={day_label}"]
        if meta.get("assumption"):
            bullets.append(meta["assumption"])
        return compose_ask_response(
            f"{received_count} order(s) were received/created on {day_label}.",
            bullets,
            "Ask how many of those are still pending or already shipped.",
        ), snapshot, meta

    # Summary / executive overview
    if intent in {"summary", "executive"} or any(token in q for token in ("summary", "summarize", "overview", "dashboard")):
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        pick_accuracy = (
            round((snapshot["audits_passed"] / snapshot["audits_total"]) * 100, 1)
            if snapshot["audits_total"]
            else 100.0
        )
        return compose_ask_response(
            (
                f"Executive snapshot: {open_orders} open order(s), {snapshot['completed']} completed, "
                f"{snapshot['blocked']} blocked, and inventory valued at ${snapshot['inventory_value']:,.2f}."
            ),
            [
                f"Pipeline: {snapshot['orders_placed']} placed, {snapshot['picking']} picking, {snapshot['pending_verification']} pending verification.",
                f"SLA risk on open work: {snapshot['sla_healthy']} healthy, {snapshot['sla_at_risk']} at risk, {snapshot['sla_breached']} breached.",
                f"Quality: {snapshot['open_quality_issues']} open quality case(s); quality audit pass rate {pick_accuracy}% ({snapshot['audits_passed']}/{snapshot['audits_total']}).",
                f"Inventory: {snapshot['sku_count']} active SKUs / {snapshot['units_on_hand']:,} units on hand.",
            ],
            "Ask which orders are blocked or which SKUs are low stock.",
        ), snapshot, meta

    # Blocked / shortage operations
    if any(token in q for token in ("blocked", "shortage", "short")):
        c.execute(
            """
            SELECT order_number, responsibility, date
            FROM order_header
            WHERE status = 'Blocked'
            ORDER BY date DESC
            LIMIT 12
            """
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                "0 orders are currently blocked by shortage in this demo session.",
                [f"Open shortage escalations in Supervisor: {snapshot['open_shortage_issues']}"],
                "Ask for low-stock SKUs that may create the next shortage.",
            ), snapshot, meta
        return compose_ask_response(
            f"{len(rows)} order(s) are blocked by shortage.",
            [f"{order_number} (owner: {responsibility})" for order_number, responsibility, _ in rows[:8]],
            "Open Inventory to restock blocked SKUs, then confirm release in Supervisor.",
        ), snapshot, meta

    # Pending verification / quality queue
    if any(token in q for token in ("pending verification", "verification", "to verify", "quality queue")):
        c.execute(
            """
            SELECT order_number, date
            FROM order_header
            WHERE status = 'Pending Verification'
            ORDER BY date DESC
            LIMIT 12
            """
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                "0 orders are pending quality verification right now.",
                [f"Open quality cases in Supervisor: {snapshot['open_quality_issues']}"],
                "Ask for quality audit pass rate or failed audits.",
            ), snapshot, meta
        return compose_ask_response(
            f"{len(rows)} order(s) are pending verification.",
            [row[0] for row in rows[:8]],
            "Open Quality to verify the oldest pending order first.",
        ), snapshot, meta

    # Quality issues / audits
    if intent == "quality" or any(token in q for token in ("quality issue", "failed audit", "defect", "damage", "pass rate", "audit")):
        if "pass" in q or "accuracy" in q or "audit" in q:
            accuracy = (
                round((snapshot["audits_passed"] / snapshot["audits_total"]) * 100, 1)
                if snapshot["audits_total"]
                else 100.0
            )
            return compose_ask_response(
                f"Quality audit pass rate is {accuracy}% (passed audits ÷ total audits) based on {snapshot['audits_total']} audit(s).",
                [
                    f"Passed: {snapshot['audits_passed']}",
                    f"Failed: {snapshot['audits_failed']}",
                    f"Open quality cases: {snapshot['open_quality_issues']}",
                ],
                "Ask for the open quality issue list.",
            ), snapshot, meta

        c.execute(
            """
            SELECT issue_id, order_number, issue_type, issue_status
            FROM supervisor_quality_issues
            WHERE issue_status NOT IN ('Closed', 'Resolved')
              AND COALESCE(issue_type, '') != ?
            ORDER BY issue_date DESC
            LIMIT 10
            """,
            (SHORTAGE_ISSUE_TYPE,),
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                "0 open quality issues in Supervisor right now.",
                [f"Failed audits on record: {snapshot['audits_failed']}"],
                "Ask for pending verification volume.",
            ), snapshot, meta
        return compose_ask_response(
            f"{len(rows)} open quality issue(s) require supervisor attention.",
            [f"{issue_id} on {order_number} ({issue_type} / {status})" for issue_id, order_number, issue_type, status in rows],
            "Open Supervisor to assign root cause and corrective action.",
        ), snapshot, meta

    # Supervisor escalations
    if intent == "supervisor" or any(token in q for token in ("escalation", "supervisor")):
        c.execute(
            """
            SELECT issue_id, order_number, issue_type, issue_status, assigned_to
            FROM supervisor_quality_issues
            WHERE issue_status NOT IN ('Closed', 'Resolved')
            ORDER BY issue_date DESC
            LIMIT 12
            """
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                "0 open supervisor escalations in this demo session.",
                [
                    f"Blocked orders: {snapshot['blocked']}",
                    f"Pending verification: {snapshot['pending_verification']}",
                ],
                "Ask for SLA breached orders.",
            ), snapshot, meta
        return compose_ask_response(
            f"{len(rows)} open supervisor escalation(s).",
            [
                f"{issue_id} / {order_number}: {issue_type} ({status})"
                + (f", owner {assigned}" if clean_display_text(assigned, "") else "")
                for issue_id, order_number, issue_type, status, assigned in rows
            ],
            "Filter Supervisor by Blocked vs Quality Issue for focused action.",
        ), snapshot, meta

    # Picker workload
    if any(token in q for token in ("picker", "behind", "workload", "productivity", "picking in progress")):
        c.execute(
            """
            SELECT COALESCE(user_role, 'Unassigned') AS picker_name,
                   COUNT(*) AS pick_events
            FROM inventory_transactions
            WHERE tx_code = 'PICK'
            GROUP BY picker_name
            ORDER BY pick_events ASC
            LIMIT 8
            """
        )
        rows = c.fetchall()
        c.execute(
            """
            SELECT COUNT(*) FROM order_header
            WHERE status IN ('Orders Placed', 'Picking in Progress')
            """
        )
        open_picks = int(c.fetchone()[0] or 0)
        if not rows:
            return compose_ask_response(
                f"No pick transactions yet. {open_picks} order(s) are waiting in Operations.",
                [f"Orders placed: {snapshot['orders_placed']}", f"Picking in progress: {snapshot['picking']}"],
                "Assign a picker on the Operations workboard.",
            ), snapshot, meta
        return compose_ask_response(
            "Picker activity ranked from lowest to highest pick volume:",
            [f"{clean_display_text(name, 'Unknown')}: {count} pick event(s)" for name, count in rows],
            "Compare this with live assignments on Operations.",
        ), snapshot, meta

    # Low stock / overstock
    if "low stock" in q or "overstock" in q or ("stock" in q and "low" in q):
        threshold = DEFAULT_LOW_STOCK_ALERT_THRESHOLD
        if "overstock" in q:
            c.execute(
                """
                SELECT sku, SUM(quantity) AS tq
                FROM inventory
                GROUP BY sku
                HAVING tq > 5000
                ORDER BY tq DESC
                LIMIT 8
                """
            )
            rows = c.fetchall()
            if not rows:
                return compose_ask_response(
                    "0 overstock SKUs found (threshold > 5000 units).",
                    [],
                    "Ask for low-stock SKUs instead.",
                ), snapshot, meta
            return compose_ask_response(
                f"{len(rows)} overstock SKU(s) above 5000 units.",
                [f"SKU {sku}: {int(qty)} units" for sku, qty in rows],
                "Review Inventory value concentration on the Executive dashboard.",
            ), snapshot, meta

        c.execute(
            """
            SELECT sku, SUM(quantity) AS tq
            FROM inventory
            GROUP BY sku
            HAVING tq > 0 AND tq < ?
            ORDER BY tq ASC
            LIMIT 8
            """,
            (threshold,),
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                f"0 low-stock SKUs below {threshold} units.",
                [],
                "Ask inventory value by warehouse.",
            ), snapshot, meta
        return compose_ask_response(
            f"{len(rows)} low-stock SKU(s) below {threshold} units.",
            [f"SKU {sku}: {int(qty)} units" for sku, qty in rows],
            "Restock from Inventory before these become blocked-order shortages.",
        ), snapshot, meta

    # Inventory value by warehouse
    if "inventory value" in q or ("value" in q and "warehouse" in q) or ("value by warehouse" in q):
        c.execute(
            """
            SELECT warehouse, COALESCE(SUM(CAST(quantity AS REAL) * price), 0) AS val
            FROM inventory
            WHERE quantity > 0
            GROUP BY warehouse
            ORDER BY val DESC
            """
        )
        rows = c.fetchall()
        if not rows:
            return compose_ask_response(
                "No inventory valuation data is available in this demo session.",
                [],
                "Ask for active SKU count.",
            ), snapshot, meta
        return compose_ask_response(
            f"Total inventory value is ${snapshot['inventory_value']:,.2f} across {len(rows)} warehouse(s).",
            [f"{clean_display_text(wh, 'Warehouse')}: ${float(val):,.2f}" for wh, val in rows],
            "Ask which SKUs contribute the most inventory value.",
        ), snapshot, meta

    # Planner / urgency / destinations
    if intent == "planner" or any(token in q for token in ("destination", "urgency", "planner", "procurement")):
        c.execute(
            """
            SELECT destination, COUNT(*)
            FROM order_header
            GROUP BY destination
            ORDER BY COUNT(*) DESC
            """
        )
        dest_rows = c.fetchall()
        c.execute(
            """
            SELECT urgency, COUNT(*)
            FROM order_header
            GROUP BY urgency
            ORDER BY COUNT(*) DESC
            """
        )
        urgency_rows = c.fetchall()
        return compose_ask_response(
            f"Planner queue holds {snapshot['orders_total']} order(s) in this demo session "
            f"({snapshot['orders_placed']} currently in Orders Placed).",
            [
                "Destinations: "
                + (", ".join(f"{clean_display_text(dest, 'Unknown')}: {count}" for dest, count in dest_rows[:4]) or "none"),
                "Urgency mix: "
                + (
                    ", ".join(
                        f"{normalize_urgency(urgency, 'Standard')}: {count}"
                        for urgency, count in urgency_rows[:4]
                    )
                    or "none"
                ),
            ],
            "Ask whether a specific SKU can support a requested quantity.",
        ), snapshot, meta

    # SLA questions
    if "sla" in q:
        open_orders = snapshot["sla_healthy"] + snapshot["sla_at_risk"] + snapshot["sla_breached"]
        return compose_ask_response(
            f"SLA on {open_orders} open order(s): {snapshot['sla_healthy']} healthy, "
            f"{snapshot['sla_at_risk']} at risk, {snapshot['sla_breached']} breached.",
            [
                f"Blocked orders: {snapshot['blocked']}",
                f"Pending verification: {snapshot['pending_verification']}",
            ],
            "Ask for blocked orders if breach risk is tied to shortages.",
        ), snapshot, meta

    # Pending / open orders
    if "pending" in q and "verification" not in q:
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        return compose_ask_response(
            f"{open_orders} order(s) are pending (not completed).",
            [
                f"Orders Placed: {snapshot['orders_placed']}",
                f"Picking in Progress: {snapshot['picking']}",
                f"Pending Verification: {snapshot['pending_verification']}",
                f"Blocked: {snapshot['blocked']}",
                f"Quality Issue: {snapshot['quality_issue']}",
            ],
            "Ask for the blocked-order list or pending verification list.",
        ), snapshot, meta

    # Last-chance intelligent fallbacks before help menu
    if any(token in q for token in ("inventory", "stock", "part", "sku", "on hand")):
        rows = query_sku_inventory_ranks(conn, mode="most_qty", limit=5)
        if rows:
            lead = rows[0]
            return compose_ask_response(
                (
                    f"Interpreted as an inventory ranking question: "
                    f"SKU {lead['sku']} currently leads with {lead['qty']:,} unit(s)."
                ),
                [
                    f"{lead['sku']}: {lead['description']}",
                    *[f"SKU {row['sku']}: {row['qty']:,} units" for row in rows[1:4]],
                    "Assumption: ranked by total on-hand quantity across warehouses.",
                ],
                "Ask which SKU has the highest inventory value.",
            ), snapshot, {"intent": "inventory", "ranking": "most_qty", "assumption": True}

    if any(token in q for token in ("order", "warehouse", "operation", "quality", "supervisor", "dashboard")):
        open_orders = max(snapshot["orders_total"] - snapshot["completed"], 0)
        return compose_ask_response(
            (
                f"Executive snapshot: {open_orders} open order(s), {snapshot['completed']} completed, "
                f"{snapshot['blocked']} blocked, {snapshot['sku_count']} active SKUs."
            ),
            [
                f"Pending verification: {snapshot['pending_verification']}",
                f"Open quality cases: {snapshot['open_quality_issues']}",
                f"Inventory value: ${snapshot['inventory_value']:,.2f}",
            ],
            "Ask a more specific question, e.g. which part number has the most inventory?",
        ), snapshot, {"intent": "summary", "assumption": True}

    # Fallback menu — only for truly vague asks
    return compose_ask_response(
        "I can answer exact warehouse questions from this demo session. Please include a subject like SKU, orders, quality, or inventory.",
        [
            "Inventory: which part has the most inventory, parts left for a SKU, low stock",
            "Executive: shipped/received by date, SLA risk, inventory value",
            "Operations: blocked orders, picker workload",
            "Quality/Supervisor: pending verification, open escalations",
        ],
        "Try: Which part number has the most inventory?",
    ), snapshot, meta


@app.route("/ask-wms/new", methods=["POST", "GET"])
def ask_wms_new_chat():
    conn = get_conn()
    conversation_id = create_ask_wms_conversation(conn, "New conversation")
    conn.commit()
    conn.close()
    return redirect(f"/ask-wms?c={conversation_id}")


@app.route("/ask-wms/clear-chat", methods=["POST"])
def ask_wms_clear_chat():
    conversation_id = request.form.get("conversation_id") or request.args.get("c")
    conn = get_conn()
    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except (TypeError, ValueError):
        conversation_id = None
    clear_ask_wms_chat(conn, conversation_id)
    conn.commit()
    conn.close()
    if conversation_id:
        return redirect("/ask-wms")
    return redirect("/ask-wms?chat_cleared=1")


@app.route("/ask-wms", methods=["GET", "POST"])
def ask_wms():
    question = ""
    conn = get_conn()

    try:
        conversation_id = int(request.values.get("c") or request.form.get("conversation_id") or 0)
    except (TypeError, ValueError):
        conversation_id = 0

    conversations = list_ask_wms_conversations(conn)
    active = get_ask_wms_conversation(conn, conversation_id) if conversation_id else None
    if not active and conversations:
        active = conversations[0]
        conversation_id = active["id"]
    if not active:
        conversation_id = create_ask_wms_conversation(conn, "New conversation")
        conn.commit()
        active = get_ask_wms_conversation(conn, conversation_id)
        conversations = list_ask_wms_conversations(conn)

    if request.method == "POST":
        question = clean_display_text(request.form.get("question"), "")
    elif request.args.get("q"):
        question = clean_display_text(request.args.get("q"), "")

    if question:
        prior_context = session.get("ask_wms_context") or {}
        debug_mode = request.args.get("ask_debug") == "1" or request.form.get("ask_debug") == "1"
        answer_html, snapshot, context = answer_ask_wms(
            conn,
            question,
            prior_context=prior_context,
            debug=debug_mode,
        )
        session["ask_wms_context"] = {
            "intent": context.get("intent"),
            "intent_family": context.get("intent_family"),
            "entities": context.get("entities") or {},
            "sku": context.get("sku"),
            "order_id": context.get("order_id"),
            "urgency": context.get("urgency"),
        }
        meta = {
            "intent": context.get("intent"),
            "intent_family": context.get("intent_family"),
            "entities": context.get("entities") or {},
        }
        # Rename untitled chats from the first user question.
        if clean_display_text(active.get("title"), "") in {"", "New conversation", "Earlier conversation"}:
            touch_ask_wms_conversation(conn, conversation_id, title=question)
        save_ask_wms_message(conn, conversation_id, "user", question, {"source": "ask-wms"})
        save_ask_wms_message(conn, conversation_id, "assistant", answer_html, meta)
        conn.commit()
        conn.close()
        # PRG + fragment keeps the viewport on the latest message after submit.
        return redirect(f"/ask-wms?c={conversation_id}#ask-bottom")

    snapshot = build_ops_snapshot(conn)
    chat_rows = list_ask_wms_chat(conn, conversation_id)
    conn.close()

    if not chat_rows:
        welcome = compose_ask_response(
            "Ask a warehouse question and I’ll answer from this demo session’s live data.",
            follow_up="Example: How many SKUs are available?",
        )
        chat_rows = [
            {
                "id": 0,
                "created_at": now_pt().isoformat(),
                "role": "assistant",
                "message": welcome,
                "meta": {"intent": "welcome"},
            }
        ]

    suggestions = [
        "How is the warehouse performing today?",
        "What needs supervisor attention right now?",
        "How many orders shipped today?",
        "Which orders are at risk of missing SLA?",
        "Which part number has the most inventory?",
        "What is our current quality pass rate?",
        "What are the top three recommended actions?",
        "Explain the order workflow",
    ]
    category_chips = [
        ("Overview", "How is the warehouse performing today?"),
        ("Orders", "How many orders are open?"),
        ("SLA", "Which orders breached SLA?"),
        ("Inventory", "Which SKUs are running low?"),
        ("Operations", "Where is the largest operational backlog?"),
        ("Quality", "Any open quality issues?"),
        ("Shipping", "Anything ship today?"),
        ("Supervisor", "What are the top three recommended actions?"),
    ]
    suggestion_html = "".join(
        f"<form method='post' style='display:inline;margin:0;'>"
        f"<input type='hidden' name='conversation_id' value='{conversation_id}'>"
        f"<input type='hidden' name='question' value=\"{html_escape(item, quote=True)}\">"
        f"<button type='submit'>{html_escape(item)}</button></form>"
        for item in suggestions
    )
    chip_html = "".join(
        f"<form method='post' style='display:inline;margin:0;'>"
        f"<input type='hidden' name='conversation_id' value='{conversation_id}'>"
        f"<input type='hidden' name='question' value=\"{html_escape(prompt, quote=True)}\">"
        f"<button type='submit' class='ask-chip'>{html_escape(label)}</button></form>"
        for label, prompt in category_chips
    )

    chat_html_parts = []
    for row in chat_rows:
        role = "user" if row["role"] == "user" else "assistant"
        role_label = "You" if role == "user" else "Ask WMS"
        stamp = format_pt_timestamp(row.get("created_at"), "-")
        if role == "user":
            body = f"<p class='ask-headline'>{html_escape(row.get('message', ''))}</p>"
        else:
            body = row.get("message", "")
        chat_html_parts.append(
            f"""
            <div class='ask-bubble {role}'>
                <div class='ask-bubble-role'>{role_label}</div>
                {body}
                <div class='ask-bubble-time'>{html_escape(stamp)}</div>
            </div>
            """
        )
    chat_transcript = "".join(chat_html_parts)

    conversation_items = []
    for item in conversations:
        is_active = " active" if item["id"] == conversation_id else ""
        title = html_escape(item["title"])
        stamp = html_escape(format_pt_timestamp(item.get("updated_at"), "-"))
        conversation_items.append(
            f"<a class='ask-conversation-item{is_active}' href='/ask-wms?c={item['id']}'>"
            f"{title}<span class='ask-conversation-meta'>{stamp}</span></a>"
        )
    if not conversation_items:
        conversation_items.append("<div class='section-note'>No saved conversations yet.</div>")

    content = f"""
    <div class='ask-page-header'>
        <div class='page-eyebrow'>&#9672; Ask WMS</div>
        <h1>Executive Command Assistant</h1>
        <p class='section-note' style='margin:0;'>Exact answers from this private demo session. Conversations stay on the left.</p>
    </div>

    <div class='ask-layout'>
        <aside class='ask-sidebar'>
            <h2 class='ask-sidebar-title'>Conversations</h2>
            <form method='post' action='/ask-wms/new'>
                <button class='ask-new-chat' type='submit'>New chat</button>
            </form>
            <div class='ask-conversation-list'>
                {''.join(conversation_items)}
            </div>
        </aside>

        <section class='ask-chat-shell'>
            <div style='display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;'>
                <div>
                    <h2 style='margin:0;font-size:18px;'>{html_escape(active.get('title', 'Conversation'))}</h2>
                </div>
                <form method='post' action='/ask-wms/clear-chat' onsubmit="return confirm('Delete this conversation?');">
                    <input type='hidden' name='conversation_id' value='{conversation_id}'>
                    <button type='submit' class='action-btn secondary' style='width:auto;'>Delete chat</button>
                </form>
            </div>
            <div class='ask-chat-log' id='ask-chat-log'>
                {chat_transcript if chat_transcript else "<div class='ask-empty'>Start by asking a warehouse question.</div>"}
                <div id='ask-bottom' tabindex='-1'></div>
            </div>
            <form method='post' class='ask-composer' id='ask-composer'>
                <input type='hidden' name='conversation_id' value='{conversation_id}'>
                <label for='ask-question' style='font-weight:700;color:#344054;'>Ask the warehouse</label>
                <textarea id='ask-question' name='question' placeholder='Example: How many SKUs are available?'></textarea>
                <div class='ask-composer-actions'>
                    <span class='section-note'>Misspellings are normalized automatically.</span>
                    <button type='submit'>Ask WMS</button>
                </div>
            </form>
            <div class='ask-suggest'>{chip_html}</div>
            <div class='ask-suggest'>{suggestion_html}</div>
        </section>
    </div>
    <script>
    (function () {{
        function scrollAskToLatest() {{
            const bottom = document.getElementById('ask-bottom');
            const log = document.getElementById('ask-chat-log');
            const composer = document.getElementById('ask-composer');
            if (log) {{
                log.scrollTop = log.scrollHeight;
                const bubbles = log.querySelectorAll('.ask-bubble');
                if (bubbles.length) {{
                    bubbles[bubbles.length - 1].scrollIntoView({{ behavior: 'auto', block: 'nearest' }});
                }}
            }}
            if (bottom) {{
                bottom.scrollIntoView({{ behavior: 'auto', block: 'end' }});
            }} else if (composer) {{
                composer.scrollIntoView({{ behavior: 'auto', block: 'nearest' }});
            }}
        }}
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', scrollAskToLatest);
        }} else {{
            scrollAskToLatest();
        }}
        window.addEventListener('load', scrollAskToLatest);
        if (window.location.hash === '#ask-bottom') {{
            window.setTimeout(scrollAskToLatest, 0);
            window.setTimeout(scrollAskToLatest, 50);
        }}
    }})();
    </script>
    """
    return layout(content)


# ======================================================
# PLANNER
# Inventory Risks moved to dedicated section above.
# ======================================================
@app.route("/planner", methods=["GET", "POST"])
def planner():
    conn = get_conn()
    c = conn.cursor()

    c.execute(
        """
        SELECT sku, ROUND(AVG(price), 2) AS unit_price
        FROM inventory
        WHERE TRIM(COALESCE(sku, '')) <> ''
        GROUP BY sku
        ORDER BY sku
        """
    )
    sku_rows = c.fetchall()
    skus = [row[0] for row in sku_rows]

    source_text, source_text_like, source_city_like, source_code_like = build_warehouse_filters(SOURCE_WAREHOUSE)
    c.execute(
        """
        SELECT sku, COALESCE(SUM(quantity), 0)
        FROM inventory
        WHERE TRIM(COALESCE(sku, '')) <> ''
          AND (
            warehouse = ?
            OR warehouse LIKE ?
            OR warehouse LIKE ?
            OR warehouse LIKE ?
          )
        GROUP BY sku
        """,
        (source_text, source_text_like, source_city_like, source_code_like),
    )
    available_by_sku = {row[0]: int(row[1] or 0) for row in c.fetchall()}

    sku_catalog = {
        row[0]: {
            "description": sku_semiconductor_description(row[0]),
            "price": float(row[1] or 0),
            "available": available_by_sku.get(row[0], 0),
        }
        for row in sku_rows
    }

    if request.method == "POST":
        line_items = {}
        sku_set = set(skus)

        for i in range(1, 11):
            sku_value = request.form.get(f"sku_{i}", "").strip()
            qty_input = request.form.get(f"qty_{i}", "").strip()

            if not sku_value and not qty_input:
                continue

            if not sku_value:
                conn.close()
                return layout(f"""
                    <div class="card">
                        <h2>Error</h2>
                        <p>Row {i}: please choose a SKU when entering quantity.</p>
                        <a href="/planner">Go Back</a>
                    </div>
                """)

            if not qty_input.isdigit():
                conn.close()
                return layout(f"""
                    <div class="card">
                        <h2>Error</h2>
                        <p>Row {i}: please enter a valid numeric quantity.</p>
                        <a href="/planner">Go Back</a>
                    </div>
                """)

            if sku_value not in sku_set:
                conn.close()
                return layout(f"""
                    <div class="card">
                        <h2>Error</h2>
                        <p>Row {i}: selected SKU is invalid.</p>
                        <a href="/planner">Go Back</a>
                    </div>
                """)

            qty_value = int(qty_input)
            if qty_value <= 0:
                continue

            line_items[sku_value] = line_items.get(sku_value, 0) + qty_value

        if not line_items:
            conn.close()
            return layout("""
                <div class="card">
                    <h2>Error</h2>
                    <p>Please add at least one SKU line with quantity.</p>
                    <a href="/planner">Go Back</a>
                </div>
            """)

        source = SOURCE_WAREHOUSE
        destination = request.form.get("destination")

        if destination not in DESTINATION_WAREHOUSES:
            conn.close()
            return layout("""
                <div class="card">
                    <h2>Error</h2>
                    <p>Please select a valid destination warehouse.</p>
                    <a href="/planner">Go Back</a>
                </div>
            """)

        urgency = normalize_urgency(request.form.get("urgency"), "Standard")
        current_time = now_pt()
        order_number = "ORD-" + current_time.strftime("%Y%m%d%H%M%S")
        request_id = "REQ-" + current_time.strftime("%H%M%S")
        expected_quantity = sum(line_items.values())

        inventory_shortfalls = []
        for sku_value, qty_value in line_items.items():
            available_qty = get_available_inventory_qty(conn, sku_value, source)
            if qty_value > available_qty:
                inventory_shortfalls.append(
                    {
                        "sku": sku_value,
                        "requested": qty_value,
                        "available": available_qty,
                        "description": sku_semiconductor_description(sku_value),
                    }
                )

        if inventory_shortfalls:
            conn.close()
            shortfall_rows = "".join(
                f"<li><strong>{item['sku']}</strong> — {item['description']}: "
                f"requested {item['requested']}, available {item['available']}</li>"
                for item in inventory_shortfalls
            )
            return layout(f"""
                <div class="card">
                    <div class="flash-warning">
                        <h2 style="margin-top:0;">Insufficient Inventory</h2>
                        <p>Planner cannot place this order because one or more SKUs exceed available on-hand quantity at {source}.</p>
                        <ul>{shortfall_rows}</ul>
                    </div>
                    <a href="/planner">Go Back to Planner</a>
                </div>
            """)

        c.execute("""
            INSERT INTO order_header (
                order_number,
                request_id,
                date,
                source,
                destination,
                urgency,
                status,
                responsibility,
                expected_quantity,
                picked_quantity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_number,
            request_id,
            current_time,
            source,
            destination,
            urgency,
            "Orders Placed",
            "Operations",
            expected_quantity,
            0,
        ))

        for sku_value, qty_value in line_items.items():
            c.execute("""
                INSERT INTO order_lines VALUES (?, ?, ?)
            """, (order_number, sku_value, qty_value))

        conn.commit()

    conn.close()

    selected_urgency_raw = request.args.get("urgency", "")
    selected_urgency = normalize_urgency(selected_urgency_raw, "") if selected_urgency_raw else ""
    selected_status = request.args.get("status", "")
    selected_destination = request.args.get("destination", "")
    selected_sla = request.args.get("sla", "")
    selected_limit = request.args.get("limit", "100")

    try:
        live_limit = int(selected_limit)
    except (TypeError, ValueError):
        live_limit = 100
    live_limit = max(10, min(live_limit, 500))

    def selected_attr(current, value):
        return " selected" if current == value else ""

    create_destination_options = "".join(
        f"<option value='{warehouse}'>{warehouse}</option>"
        for warehouse in DESTINATION_WAREHOUSES
    )
    create_urgency_options = "".join(
        f"<option value='{urgency_value}'{selected_attr('Standard', urgency_value)}>{urgency_value}</option>"
        for urgency_value in ["Critical", "Urgent", "Standard"]
    )
    filter_urgency_options = "".join(
        f"<option value='{urgency_value}'{selected_attr(selected_urgency, urgency_value)}>{urgency_value}</option>"
        for urgency_value in ["Critical", "Urgent", "Standard"]
    )
    filter_status_options = "".join(
        f"<option value='{status_value}'{selected_attr(selected_status, status_value)}>{status_value}</option>"
        for status_value in [
            "Orders Placed",
            "Picking in Progress",
            "Pending Verification",
            "Completed",
            "Quality Issue",
        ]
    )
    filter_destination_options = "".join(
        f"<option value='{warehouse}'{selected_attr(selected_destination, warehouse)}>{warehouse}</option>"
        for warehouse in DESTINATION_WAREHOUSES
    )
    filter_sla_options = "".join(
        f"<option value='{sla_value}'{selected_attr(selected_sla, sla_value)}>{sla_label}</option>"
        for sla_value, sla_label in [
            ("healthy", "Healthy"),
            ("at_risk", "At Risk"),
            ("breached", "Breached"),
        ]
    )
    filter_limit_options = "".join(
        f"<option value='{limit_value}'{selected_attr(str(live_limit), str(limit_value))}>{limit_value}</option>"
        for limit_value in [50, 100, 250, 500]
    )

    entry_rows_html = []
    for i in range(1, 11):
        row_style = "" if i == 1 else " style='display:none;'"
        remove_btn = "" if i == 1 else f"<button type='button' onclick='removeLine({i})'>Remove</button>"
        sku_options = ["<option value=''>-- Select SKU --</option>"]
        for sku_value in skus:
            sku_options.append(f"<option value='{sku_value}'>{sku_value}</option>")
        entry_rows_html.append(
            f"""
            <tr id='line-row-{i}'{row_style}>
                <td>{i}</td>
                <td><select id='sku_{i}' name='sku_{i}'>{''.join(sku_options)}</select></td>
                <td id='desc_{i}' style='min-width:260px;color:#475569;'>Select a semiconductor part</td>
                <td id='price_{i}' style='white-space:nowrap;font-weight:700;'>$0.00</td>
                <td><input id='qty_{i}' name='qty_{i}' type='number' min='1' oninput='updateLine({i})'></td>
                <td id='line_total_{i}' style='white-space:nowrap;font-weight:700;'>$0.00</td>
                <td>{remove_btn}</td>
            </tr>
            """
        )

    # ======================================================
    # LIVE ORDER TRACKING SECTION
    # ======================================================

    conn = get_conn()
    c = conn.cursor()
    live_query = """
        SELECT order_number, urgency, status, responsibility, date
        FROM order_header
        WHERE 1=1
    """
    live_params = []

    if selected_urgency:
        live_query += " AND urgency = ?"
        live_params.append(selected_urgency)
    if selected_status:
        live_query += " AND status = ?"
        live_params.append(selected_status)
    if selected_destination:
        live_query += " AND destination = ?"
        live_params.append(selected_destination)

    live_query += " ORDER BY date DESC LIMIT ?"
    live_params.append(live_limit)

    c.execute(live_query, live_params)
    live_orders = c.fetchall()

    now = now_pt()
    table_rows = []
    displayed_rows = 0
    for order_number, urgency, status, responsibility, date_str in live_orders:
        urgency = normalize_urgency(urgency, "Standard")
        order_time = parse_order_datetime(date_str)

        sla_snapshot = build_order_sla_snapshot(order_time, urgency, status, now, conn=conn, order_number=order_number)
        sla_key = sla_snapshot["sla_key"]
        sla_status = sla_snapshot["sla_status"]
        sla_start = sla_snapshot["sla_start"]
        sla_deadline = sla_snapshot["sla_deadline"]
        sla_timer = sla_snapshot["sla_timer"]
        age_minutes = sla_snapshot["age_minutes"]

        if selected_sla and selected_sla != sla_key:
            continue

        displayed_rows += 1
        table_rows.append(
            f"""
            <tr>
                <td><a class='planner-order-link' href='/order/{order_number}'>{order_number}</a></td>
                <td>{urgency}</td>
                <td>{status_badge_html(status)}</td>
                <td>{responsibility_badge_html(responsibility)}</td>
                <td>{sla_status}</td>
                <td>{sla_start}</td>
                <td>{sla_deadline}</td>
                <td>{sla_timer}</td>
                <td>{sla_risk_badge_html(sla_key)}</td>
                <td>{age_minutes}</td>
                <td class='table-actions'><a href='/order/{order_number}'>View</a></td>
            </tr>
            """
        )

    conn.close()

    if table_rows:
        table_rows_html = "".join(table_rows)
    else:
        table_rows_html = "<tr><td colspan='11' class='planner-empty-state'>No live orders match the current filter set.</td></tr>"

    content = f"""
    <div class='planner-dashboard-shell'>
        <section class='card planner-dashboard-header'>
            <div class='planner-header-top'>
                <div class='planner-header-copy'>
                    <div class='page-eyebrow'>&#9672; Planner Dashboard</div>
                    <h2>Create Order</h2>
                    <p class='section-note'>Compact order entry with pricing visibility for planning only. Orders cannot exceed available source-warehouse inventory. The live queue remains fixed in view below.</p>
                </div>
                <div class='planner-header-metrics'>
                    <div class='planner-metric'>
                        <div class='planner-metric-label'>Catalog Size</div>
                        <div class='planner-metric-value'>{len(skus)}</div>
                        <div class='planner-metric-note'>Active SKU options</div>
                    </div>
                    <div class='planner-metric'>
                        <div class='planner-metric-label'>Line Capacity</div>
                        <div class='planner-metric-value'>10</div>
                        <div class='planner-metric-note'>Max SKU lines per order</div>
                    </div>
                </div>
            </div>

            <form method='post' class='planner-create-form'>
                <div class='planner-create-fields'>
                    <div class='planner-form-field'>
                        <span class='planner-form-label'>Source Warehouse</span>
                        <div class='planner-static-field'>{SOURCE_WAREHOUSE}</div>
                        <input type='hidden' name='source' value='{SOURCE_WAREHOUSE}'>
                    </div>
                    <div class='planner-form-field'>
                        <label class='planner-form-label' for='planner-destination'>Destination</label>
                        <select id='planner-destination' name='destination'>
                            {create_destination_options}
                        </select>
                    </div>
                    <div class='planner-form-field'>
                        <label class='planner-form-label' for='planner-urgency'>Urgency</label>
                        <select id='planner-urgency' name='urgency'>
                            {create_urgency_options}
                        </select>
                    </div>
                </div>

                <div class='planner-entry-shell'>
                    <table class='planner-entry-table'>
                        <tr>
                            <th>Row</th>
                            <th>SKU</th>
                            <th>Description</th>
                            <th>Unit Price</th>
                            <th>Quantity</th>
                            <th>Line Total</th>
                            <th>Action</th>
                        </tr>
                        {''.join(entry_rows_html)}
                    </table>
                </div>

                <div class='planner-total-strip'>
                    <div>
                        <div class='planner-total-label'>Estimated Extended Total</div>
                        <div id='planner-order-total' class='planner-total-value'>$0.00</div>
                    </div>
                    <div class='planner-total-note'>Pricing and totals remain exclusive to Planner and are hidden from Operations pick views.</div>
                </div>

                <div class='planner-create-actions'>
                    <div class='planner-action-group'>
                        <button type='button' onclick='addLine()'>Add SKU Row</button>
                        <p id='line-limit-msg' class='planner-line-limit' style='display:none;color:#b45309;'>Maximum of 10 SKU lines reached.</p>
                    </div>
                    <div class='planner-action-group'>
                        <span class='section-note'>Leave unused rows hidden for a tighter control-board layout.</span>
                        <button type='submit'>Create Order</button>
                    </div>
                </div>
            </form>
        </section>

        <section class='card planner-dashboard-filters'>
            <div class='planner-filter-top'>
                <div class='planner-filter-copy'>
                    <h3>Live Order Tracking</h3>
                    <p class='section-note'>Filters stay pinned while the queue itself scrolls.</p>
                </div>
                <div class='planner-filter-summary'>
                    <span class='meta-chip'>Showing {displayed_rows} orders</span>
                    <span class='meta-chip'>Display limit {live_limit}</span>
                </div>
            </div>

            <form method='get' class='planner-filter-form'>
                <div class='planner-form-field'>
                    <label class='planner-form-label' for='filter-urgency'>Urgency</label>
                    <select id='filter-urgency' name='urgency'>
                        <option value=''>All</option>
                        {filter_urgency_options}
                    </select>
                </div>
                <div class='planner-form-field'>
                    <label class='planner-form-label' for='filter-status'>Status</label>
                    <select id='filter-status' name='status'>
                        <option value=''>All</option>
                        {filter_status_options}
                    </select>
                </div>
                <div class='planner-form-field'>
                    <label class='planner-form-label' for='filter-destination'>Destination</label>
                    <select id='filter-destination' name='destination'>
                        <option value=''>All</option>
                        {filter_destination_options}
                    </select>
                </div>
                <div class='planner-form-field'>
                    <label class='planner-form-label' for='filter-sla'>SLA</label>
                    <select id='filter-sla' name='sla'>
                        <option value=''>All</option>
                        {filter_sla_options}
                    </select>
                </div>
                <div class='planner-form-field'>
                    <label class='planner-form-label' for='filter-limit'>Show</label>
                    <select id='filter-limit' name='limit'>
                        {filter_limit_options}
                    </select>
                </div>
                <div class='planner-form-field'>
                    <button type='submit'>Apply</button>
                </div>
            </form>
        </section>

        <section class='card planner-table-panel'>
            <div class='planner-table-scroll'>
                <table class='planner-live-table'>
                    <tr>
                        <th>Order</th>
                        <th>Urgency</th>
                        <th>Status</th>
                        <th>Responsibility</th>
                        <th>SLA Status</th>
                        <th>Start Time</th>
                        <th>End / Due Time</th>
                        <th>SLA Timer</th>
                        <th>Risk</th>
                        <th>Elapsed (min)</th>
                        <th>Action</th>
                    </tr>
                    {table_rows_html}
                </table>
            </div>
        </section>

        <script>
        const skuCatalog = __SKU_CATALOG__;

        function formatCurrency(value) {{
            const amount = Number.isFinite(value) ? value : 0;
            return amount.toLocaleString('en-US', {{ style: 'currency', currency: 'USD' }});
        }}

        function updatePlannerTotal() {{
            let orderTotal = 0;
            for (let i = 1; i <= 10; i++) {{
                const row = document.getElementById('line-row-' + i);
                if (!row || row.style.display === 'none') {{
                    continue;
                }}
                const sku = document.getElementById('sku_' + i)?.value || '';
                const quantity = Number(document.getElementById('qty_' + i)?.value || 0);
                const unitPrice = Number((skuCatalog[sku] || {{}}).price || 0);
                orderTotal += unitPrice * quantity;
            }}
            const totalElement = document.getElementById('planner-order-total');
            if (totalElement) {{
                totalElement.textContent = formatCurrency(orderTotal);
            }}
        }}

        function updateLine(index) {{
            const skuSelect = document.getElementById('sku_' + index);
            const qtyInput = document.getElementById('qty_' + index);
            const descCell = document.getElementById('desc_' + index);
            const priceCell = document.getElementById('price_' + index);
            const totalCell = document.getElementById('line_total_' + index);

            if (!skuSelect || !qtyInput || !descCell || !priceCell || !totalCell) {{
                return;
            }}

            const sku = skuSelect.value || '';
            const catalogItem = skuCatalog[sku] || {{ description: 'Select a semiconductor part', price: 0, available: 0 }};
            const quantity = Number(qtyInput.value || 0);
            const lineTotal = Number(catalogItem.price || 0) * quantity;
            const availableQty = Number(catalogItem.available || 0);

            if (sku) {{
                descCell.textContent = (catalogItem.description || 'Semiconductor part') + ' · Available ' + availableQty;
                if (quantity > availableQty) {{
                    descCell.style.color = '#b42318';
                }} else {{
                    descCell.style.color = '#475569';
                }}
            }} else {{
                descCell.textContent = 'Select a semiconductor part';
                descCell.style.color = '#475569';
            }}
            priceCell.textContent = formatCurrency(Number(catalogItem.price || 0));
            totalCell.textContent = formatCurrency(lineTotal);
            updatePlannerTotal();
        }}

        function currentVisibleRows() {{
            let count = 0;
            for (let i = 1; i <= 10; i++) {{
                const row = document.getElementById('line-row-' + i);
                if (row && row.style.display !== 'none') {{
                    count += 1;
                }}
            }}
            return count;
        }}

        function addLine() {{
            for (let i = 1; i <= 10; i++) {{
                const row = document.getElementById('line-row-' + i);
                if (row && row.style.display === 'none') {{
                    row.style.display = '';
                    document.getElementById('line-limit-msg').style.display = 'none';
                    return;
                }}
            }}
            document.getElementById('line-limit-msg').style.display = '';
        }}

        function removeLine(index) {{
            if (currentVisibleRows() <= 1) {{
                return;
            }}
            const row = document.getElementById('line-row-' + index);
            if (!row) {{
                return;
            }}
            const sku = document.getElementById('sku_' + index);
            const qty = document.getElementById('qty_' + index);
            if (sku) {{
                sku.value = '';
            }}
            if (qty) {{
                qty.value = '';
            }}
            updateLine(index);
            row.style.display = 'none';
            document.getElementById('line-limit-msg').style.display = 'none';
            updatePlannerTotal();
        }}

        for (let i = 1; i <= 10; i++) {{
            const skuSelect = document.getElementById('sku_' + i);
            if (skuSelect) {{
                skuSelect.addEventListener('change', function() {{
                    updateLine(i);
                }});
            }}
            updateLine(i);
        }}
        </script>
    </div>
    """
    content = content.replace("__SKU_CATALOG__", json.dumps(sku_catalog))

    return layout(content, body_class="planner-body")
# ======================================================
# OPERATIONS WITH PICK SCREEN
# ======================================================
OPERATIONS_WORKBOARD_TEMPLATE = """
<div class='card'>
    <div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;'>
        <div>
            <h2 style='margin-bottom:6px;'>Operations Workboard</h2>
            <p style='margin:0;'>One action per step. Start picking from the ready queue, then complete from the active queue.</p>
        </div>
        <a class='quick-link' href='/supervisor?resp=Operations'>Supervisor View: Operations</a>
    </div>

    <div class='metrics-grid'>
        <div class='metric-tile tile-total'><div class='metric-label'>Ready to Pick</div><div class='metric-value'>{{ ready_count }}</div></div>
        <div class='metric-tile tile-compliance'><div class='metric-label'>Picking in Progress</div><div class='metric-value'>{{ in_progress_count }}</div></div>
        <div class='metric-tile tile-completed'><div class='metric-label'>Completed Today</div><div class='metric-value'>{{ completed_count }}</div></div>
        <div class='metric-tile tile-breached'><div class='metric-label'>Blocked by Shortage</div><div class='metric-value'>{{ shortage_count }}</div></div>
    </div>

    <div class='ops-control-bar'>
        <div>
            <div class='section-note'>Current picker</div>
            <div style='font-size:26px;font-weight:800;color:#0f172a;margin-top:6px;'>{{ current_picker }}</div>
            <div class='section-note'>Orders are auto-assigned when Start Picking is pressed.</div>
        </div>
        <form method='get' class='ops-control-form'>
            <label>
                Active picker
                <select name='picker'>
                    {% for name in picker_roster %}
                    <option value='{{ name }}' {% if name == current_picker %}selected{% endif %}>{{ name }}</option>
                    {% endfor %}
                </select>
            </label>
            <button type='submit'>Switch Picker</button>
        </form>
    </div>
</div>

{% if ops_message %}
<div class='message-banner {{ ops_message_type }}'>
    <div>
        <b>Operations Update</b>
        <div>{{ ops_message }}</div>
    </div>
</div>
{% endif %}

{% if shortage_alerts %}
<div class='card'>
    <div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;'>
        <div>
            <h2 style='margin-bottom:6px;'>Blocked Orders</h2>
            <p style='margin:0;'>These orders are off the workboard until inventory is replenished.</p>
        </div>
        <a class='quick-link' href='/supervisor?status=Blocked'>Open Supervisor Watchlist</a>
    </div>
    <div class='alert-list'>
        {% for alert in shortage_alerts %}
        <div class='alert-item'>
            <div>
                <div class='sku-title'>{{ alert.sku }} <span class='section-note' style='margin-left:6px;'>Order {{ alert.order_number }}</span></div>
                <div class='section-note'>Remaining {{ alert.remaining_qty }} vs on-hand {{ alert.on_hand_qty }} at {{ alert.warehouse_label }} / {{ alert.location_label }}</div>
                <div class='section-note'>Escalation: {{ alert.issue_status }}</div>
            </div>
            <div class='table-actions'>
                {% if alert.issue_id %}
                <a href='/supervisor_issue/{{ alert.issue_id }}'>Open Escalation</a>
                {% else %}
                <a href='{{ alert.notify_url }}'>Notify Supervisor</a>
                {% endif %}
                <a href='/order/{{ alert.order_number }}?context=operations'>View Order</a>
            </div>
        </div>
        {% endfor %}
    </div>
</div>
{% endif %}

<div class='card'>
    <div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;'>
        <div>
            <h2 style='margin-bottom:6px;'>Operations Worklist</h2>
            <p style='margin:0;'>Released work, active picks, and recent completions now stay in one queue so the picker can work from a single surface.</p>
        </div>
        <div class='ops-stream-stats'>
            <div class='ops-stream-stat in-progress'>
                <span>Picking Now</span>
                <strong>{{ in_progress_count }}</strong>
            </div>
            <div class='ops-stream-stat ready'>
                <span>Ready Next</span>
                <strong>{{ ready_count }}</strong>
            </div>
            <div class='ops-stream-stat completed'>
                <span>Completed / Sent Forward</span>
                <strong>{{ completed_lane_count }}</strong>
            </div>
        </div>
    </div>
    <div class='ops-stream-list'>
        {% if workboard_items %}
            {% for card in workboard_items %}
            <article class='ops-work-item state-{{ card.stage_key }}'>
                <div class='ops-stage-strip'>
                    <div>
                        <span class='ops-stage-pill stage-{{ card.stage_key }}'>{{ card.stage_label }}</span>
                        <div class='section-note'>{{ card.stage_note }}</div>
                    </div>
                    {% if card.status_summary %}
                    <div class='section-note'>{{ card.status_summary }}</div>
                    {% endif %}
                </div>
                <div class='ops-work-item-head'>
                    <div>
                        <div class='ops-work-item-title'><a href='{{ card.order_url }}'>{{ card.order_number }}</a></div>
                        <div class='ops-work-summary'>{{ card.summary }}</div>
                    </div>
                    <div class='ops-chip-row'>
                        <span class='ops-priority-pill {{ card.priority_class }}'>{{ card.priority_label }}</span>
                        {% if card.stage_key == 'completed' %}
                        <span class='ops-state-pill state-completed'>{{ card.status_label }}</span>
                        {% else %}
                        <span class='ops-sla-pill {{ card.sla_class }}'>{{ card.sla_label }}</span>
                        <span class='ops-state-pill state-{{ card.stage_key }}'>{{ card.state_badge_label }}</span>
                        {% endif %}
                    </div>
                </div>
                <div class='ops-field-grid'>
                    {% if card.stage_key == 'ready' %}
                    <div class='ops-field'><span>Deadline</span><strong>{{ card.deadline_display }}</strong></div>
                    <div class='ops-field'><span>Time Remaining</span><strong>{{ card.time_remaining_display }}</strong></div>
                    <div class='ops-field'><span>SLA Status</span><strong>{{ card.sla_display }}</strong></div>
                    <div class='ops-field'>
                        <span>Progress</span>
                        <strong>{{ card.progress_label }}</strong>
                        <div class='ops-progress-track'><div class='ops-progress-fill' style='width: {{ card.progress_pct }}%;'></div></div>
                    </div>
                    {% elif card.stage_key == 'in_progress' %}
                    <div class='ops-field'><span>Picker</span><strong>{{ card.picker_display }}</strong></div>
                    <div class='ops-field'><span>Started</span><strong>{{ card.started_display }}</strong></div>
                    <div class='ops-field'><span>Time Remaining</span><strong>{{ card.time_remaining_display }}</strong></div>
                    <div class='ops-field'><span>Expected Qty</span><strong>{{ card.expected_quantity_label }}</strong></div>
                    <div class='ops-field'><span>Picked Qty</span><strong>{{ card.picked_quantity_label }}</strong></div>
                    <div class='ops-field'><span>Remaining Qty</span><strong>{{ card.remaining_units }}</strong></div>
                    <div class='ops-field'>
                        <span>Progress</span>
                        <strong>{{ card.picked_vs_expected_label }} ({{ card.progress_label }})</strong>
                        <div class='ops-progress-track'><div class='ops-progress-fill' style='width: {{ card.progress_pct }}%; background: linear-gradient(90deg, #ea580c 0%, #fb923c 100%);'></div></div>
                    </div>
                    <div class='ops-field'><span>Open SKU Lines</span><strong>{{ card.detail_rows|length }}</strong></div>
                    {% else %}
                    <div class='ops-field'><span>Picker</span><strong>{{ card.picker_display }}</strong></div>
                    <div class='ops-field'><span>Handed Off</span><strong>{{ card.completed_display }}</strong></div>
                    <div class='ops-field'><span>Verification State</span><strong>{{ card.status_label }}</strong></div>
                    <div class='ops-field'><span>Scope</span><strong>{{ card.completed_scope_label }}</strong></div>
                    {% endif %}
                </div>
                {% if card.stage_key == 'in_progress' %}
                <div class='ops-readiness-banner {{ card.readiness_class }}'>
                    <strong>{{ card.readiness_title }}</strong>
                    <div>{{ card.readiness_note }}</div>
                </div>
                {% endif %}
                {% if card.detail_rows %}
                <div class='ops-item-preview'>
                    <span>{{ card.detail_title }}</span>
                    <table class='ops-detail-table'>
                        <tr>
                            <th>SKU</th>
                            {% if card.stage_key == 'in_progress' %}
                            <th>Need</th>
                            <th>Picked</th>
                            <th>Remaining</th>
                            {% else %}
                            <th>Qty</th>
                            {% endif %}
                            <th>Warehouse</th>
                            <th>Location</th>
                        </tr>
                        {% for item in card.detail_rows %}
                        <tr>
                            <td>{{ item.sku }}</td>
                            {% if card.stage_key == 'in_progress' %}
                            <td>{{ item.required_qty }}</td>
                            <td>{{ item.picked_qty }}</td>
                            <td>{{ item.remaining_qty }}</td>
                            {% else %}
                            <td>{{ item.required_qty }}</td>
                            {% endif %}
                            <td>{{ item.warehouse_label }}</td>
                            <td>{{ item.location_label }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                    {% if card.hidden_item_count %}
                    <div class='ops-item-more'>+{{ card.hidden_item_count }} more item(s)</div>
                    {% endif %}
                </div>
                {% endif %}
                {% if card.stage_key == 'ready' %}
                <div class='ops-work-actions'>
                    <form method='post' action='{{ card.action_url }}' class='ops-primary-form'>
                        <input type='hidden' name='picker' value='{{ current_picker }}'>
                        <input type='hidden' name='target_status' value='in_progress'>
                        <button type='submit' class='ops-primary-button ready'>Start Picking</button>
                    </form>
                    <div class='ops-action-note'>Starts the pick and assigns it to {{ current_picker }}.</div>
                </div>
                {% elif card.stage_key == 'in_progress' %}
                <div class='ops-work-actions'>
                    <form method='post' action='{{ card.action_url }}' class='ops-primary-form'>
                        <input type='hidden' name='picker' value='{{ current_picker }}'>
                        <input type='hidden' name='target_status' value='in_progress'>
                        <button type='submit' class='ops-primary-button in-progress'>Continue Picking</button>
                    </form>
                    {% if card.can_take_over %}
                    <form method='post' action='{{ card.action_url }}' class='ops-primary-form'>
                        <input type='hidden' name='picker' value='{{ current_picker }}'>
                        <input type='hidden' name='target_status' value='take_over'>
                        <button type='submit' class='ops-secondary-button takeover'>Take Over Pick</button>
                    </form>
                    {% endif %}
                    <div class='ops-action-note'>{{ card.pick_screen_note }}</div>
                    <div class='ops-action-note'>{{ card.auto_complete_note }}</div>
                    {% if card.can_take_over %}
                    <div class='ops-action-note'>{{ card.take_over_note }}</div>
                    {% endif %}
                </div>
                {% endif %}
            </article>
            {% endfor %}
        {% else %}
        <div class='ops-empty-state'>
            <b>No operational work is currently visible.</b>
            <div style='margin-top:6px;'>Released orders, active picks, and recent completions will appear here automatically.</div>
        </div>
        {% endif %}
    </div>
</div>
"""


@app.route("/operations")
def operations():
    current_picker = resolve_picker_identity(request.args.get("picker"))
    ops_message = request.args.get("ops_message", "").strip()
    ops_message_type = "warning" if request.args.get("ops_message_type", "success").strip() == "warning" else "success"

    conn = get_conn()
    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()
    c.execute("""
        SELECT order_number, urgency, status, date, source
        FROM order_header
        WHERE status='Orders Placed'
        ORDER BY date DESC
    """)
    orders = c.fetchall()
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Orders Placed'")
    ready_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Picking in Progress'")
    in_progress_count = c.fetchone()[0]
    c.execute(
        """
                SELECT COUNT(DISTINCT order_number)
        FROM inventory_transactions
        WHERE tx_code='PICK_CLOSE'
          AND substr(tx_time, 1, 10)=?
        """,
        (now_pt().date().isoformat(),),
    )
    completed_count = c.fetchone()[0]

    c.execute(
        """
        SELECT
            oh.order_number,
            oh.urgency,
            oh.status,
            oh.date,
            oh.source,
            COALESCE(
                (
                    SELECT it.user_role
                    FROM inventory_transactions it
                    WHERE it.order_number = oh.order_number
                      AND it.tx_code IN ('PICK_TAKEOVER', 'PICK_START')
                    ORDER BY it.tx_time DESC
                    LIMIT 1
                ),
                (
                    SELECT it.user_role
                    FROM inventory_transactions it
                    WHERE it.order_number = oh.order_number
                      AND it.tx_code IN ('PICK_CLOSE', 'PICK_TAKEOVER', 'PICK_START', 'PICK')
                    ORDER BY it.tx_time DESC
                    LIMIT 1
                ),
                'Unassigned'
            ) AS picker_name,
            (
                SELECT it.tx_time
                FROM inventory_transactions it
                WHERE it.order_number = oh.order_number
                  AND it.tx_code = 'PICK_START'
                ORDER BY it.tx_time DESC
                LIMIT 1
            ) AS pick_started_at
        FROM order_header oh
        WHERE oh.status='Picking in Progress'
        ORDER BY oh.date DESC
        """
    )
    active_picks = c.fetchall()

    c.execute(
        """
        SELECT
            oh.order_number,
            oh.urgency,
            oh.status,
            oh.date,
            COALESCE(
                (
                    SELECT it.user_role
                    FROM inventory_transactions it
                    WHERE it.order_number = oh.order_number
                      AND it.tx_code IN ('PICK_CLOSE', 'PICK_TAKEOVER', 'PICK_START', 'PICK')
                    ORDER BY it.tx_time DESC
                    LIMIT 1
                ),
                'Unassigned'
            ) AS picker_name,
            oh.source,
            COALESCE(
                (
                    SELECT it_close.tx_time
                    FROM inventory_transactions it_close
                    WHERE it_close.order_number = oh.order_number
                      AND it_close.tx_code = 'PICK_CLOSE'
                    ORDER BY it_close.tx_time DESC
                    LIMIT 1
                ),
                (
                    SELECT it_pick.tx_time
                    FROM inventory_transactions it_pick
                    WHERE it_pick.order_number = oh.order_number
                      AND it_pick.tx_code = 'PICK'
                    ORDER BY it_pick.tx_time DESC
                    LIMIT 1
                )
            ) AS completed_at
        FROM order_header oh
        WHERE oh.status IN ('Pending Verification', 'Completed', 'Quality Issue')
                    AND EXISTS (
                            SELECT 1
                            FROM inventory_transactions it_activity
                            WHERE it_activity.order_number = oh.order_number
                                AND it_activity.tx_code IN ('PICK', 'PICK_CLOSE')
                    )
        ORDER BY COALESCE(completed_at, oh.date) DESC
        """
    )
    completed_orders_raw = c.fetchall()
    shortage_alerts = shortage_sync["alerts"]
    active_shortage_alerts = shortage_alerts[:6]

    now = now_pt()
    ready_orders = []
    for order_number, urgency, status, date_str, source_wh in orders:
        urgency = normalize_urgency(urgency, "Standard")
        order_time = parse_order_datetime(date_str)
        sla_key, _ = calculate_sla_status(order_time, urgency, now)
        sla_deadline, sla_timer = format_sla_timing(order_time, urgency, now)
        readiness = get_order_pick_readiness(conn, order_number)
        progress_pct = max(0, min(100, int(round(calculate_pick_progress(readiness)))))
        detail_rows, hidden_item_count = get_order_workboard_lines(conn, order_number, source_wh)
        ready_orders.append({
            "stage_key": "ready",
            "stage_label": "Ready to Pick",
            "stage_note": "Released to Operations and waiting for a picker to claim it.",
            "status_summary": "Released and unassigned",
            "state_badge_label": "Ready",
            "detail_title": "Items To Pick",
            "order_number": order_number,
            "order_url": f"/order/{order_number}?context=operations",
            "action_url": f"/operations/orders/{order_number}/status",
            "priority_label": urgency,
            "priority_class": f"priority-{urgency.lower().replace(' ', '-')}",
            "sla_class": f"sla-{sla_key.replace('_', '-')}",
            "sla_label": {
                "healthy": "SLA Healthy",
                "at_risk": "SLA At Risk",
                "breached": "SLA Breached",
            }[sla_key],
            "sla_display": {
                "healthy": "Healthy",
                "at_risk": "At Risk",
                "breached": "Breached",
            }[sla_key],
            "deadline_display": sla_deadline,
            "time_remaining_display": sla_timer,
            "progress_pct": progress_pct,
            "progress_label": f"{progress_pct}%",
            "detail_rows": detail_rows,
            "hidden_item_count": hidden_item_count,
            "summary": f"{urgency} priority order ready for the next available picker.",
        })

    active_orders = []
    for order_number, urgency, status, date_str, source_wh, picker_name, pick_started_at in active_picks:
        urgency = normalize_urgency(urgency, "Standard")
        order_time = parse_order_datetime(date_str)
        sla_key, _ = calculate_sla_status(order_time, urgency, now)
        _sla_deadline, sla_timer = format_sla_timing(order_time, urgency, now)

        pick_readiness = get_order_pick_readiness(conn, order_number)
        readiness_summary = summarize_pick_readiness(pick_readiness)
        progress_pct = max(0, min(100, int(round(calculate_pick_progress(pick_readiness)))))
        expected_units = readiness_summary["expected_units"]
        picked_units = readiness_summary["picked_units"]
        detail_rows, hidden_item_count = get_order_workboard_lines(conn, order_number, source_wh)
        assigned_picker = clean_display_text(picker_name, "")
        can_take_over = bool(assigned_picker) and not is_generic_picker_identity(assigned_picker) and assigned_picker != current_picker

        picker_display = format_picker_display_name(picker_name)
        started_display = "-"
        if pick_started_at:
            try:
                started_display = parse_order_datetime(pick_started_at).strftime("%Y-%m-%d %H:%M PT")
            except ValueError:
                started_display = clean_display_text(pick_started_at, "-")

        active_orders.append({
            "stage_key": "in_progress",
            "stage_label": "Picking in Progress",
            "stage_note": "Active work. Verify the shown source location before posting any quantity.",
            "status_summary": f"Owner: {picker_display}",
            "state_badge_label": "In Progress",
            "detail_title": "Items In This Pick",
            "order_number": order_number,
            "order_url": f"/order/{order_number}?context=operations",
            "action_url": f"/operations/orders/{order_number}/status",
            "priority_label": urgency,
            "priority_class": f"priority-{urgency.lower().replace(' ', '-')}",
            "sla_class": f"sla-{sla_key.replace('_', '-')}",
            "sla_label": {
                "healthy": "SLA Healthy",
                "at_risk": "SLA At Risk",
                "breached": "SLA Breached",
            }[sla_key],
            "picker_display": picker_display,
            "assigned_picker": assigned_picker,
            "can_take_over": can_take_over,
            "started_display": started_display,
            "time_remaining_display": sla_timer,
            "expected_quantity_label": f"{expected_units} unit(s)",
            "picked_quantity_label": f"{picked_units} unit(s)",
            "picked_vs_expected_label": f"{picked_units} / {expected_units}",
            "progress_pct": progress_pct,
            "progress_label": f"{progress_pct}%",
            "remaining_units": readiness_summary["remaining_units"],
            "remaining_units_label": f"{readiness_summary['remaining_units']} unit(s)",
            "remaining_lines_label": f"{readiness_summary['remaining_lines']} SKU(s)",
            "detail_rows": detail_rows,
            "hidden_item_count": hidden_item_count,
            "take_over_note": f"Take ownership from {picker_display} and keep working this order.",
            "pick_screen_note": "Open the live pick screen to verify location, enter the quantity you picked, and post the actual transaction yourself.",
            "auto_complete_note": (
                "Once you transact the final required quantity from the pick screen, the order moves to quality verification automatically."
                if readiness_summary["ready_for_quality"]
                else "Keep posting actual picked quantities from the pick screen until the full order is transacted."
            ),
            "readiness_title": "Ready to close" if readiness_summary["ready_for_quality"] else "Pick still open",
            "readiness_class": "ready" if readiness_summary["ready_for_quality"] else "pending",
            "readiness_note": (
                f"{picked_units} of {expected_units} unit(s) are currently tracked. The order can be completed now."
                if readiness_summary["ready_for_quality"]
                else f"{picked_units} of {expected_units} unit(s) picked. Finish the remaining quantity before closing the order."
            ),
            "summary": f"Assigned to {picker_display}. Complete the order only when the full quantity is picked.",
        })

    completed_orders = []
    for order_number, urgency, status, date_str, picker_name, source_wh, completed_at in completed_orders_raw:
        urgency = normalize_urgency(urgency, "Standard")
        completed_display = format_pt_timestamp(completed_at or date_str)
        detail_rows, hidden_item_count = get_order_workboard_lines(conn, order_number, source_wh)
        if status == "Completed":
            status_label = "Completed"
        elif status == "Quality Issue":
            status_label = "Quality Issue"
        else:
            status_label = "Pending Verification"
        completed_orders.append({
            "stage_key": "completed",
            "stage_label": "Recently Finished",
            "stage_note": "Recent picks stay visible here so Operations can confirm what was sent forward.",
            "status_summary": "",
            "detail_title": "Completed Pick Details",
            "order_number": order_number,
            "order_url": f"/order/{order_number}?context=operations",
            "priority_label": urgency,
            "priority_class": f"priority-{urgency.lower().replace(' ', '-')}",
            "picker_display": format_picker_display_name(picker_name),
            "completed_display": completed_display,
            "detail_rows": detail_rows,
            "hidden_item_count": hidden_item_count,
            "status_label": status_label,
            "completed_scope_label": f"{sum(item['required_qty'] for item in detail_rows)} unit(s) shown",
            "summary": (
                f"Picked by {format_picker_display_name(picker_name)} and fully closed."
                if status == "Completed"
                else (
                    f"Picked by {format_picker_display_name(picker_name)} and flagged for quality follow-up."
                    if status == "Quality Issue"
                    else f"Picked by {format_picker_display_name(picker_name)} and waiting for Quality audit."
                )
            ),
        })

    completed_lane_count = len(completed_orders)
    workboard_items = active_orders + ready_orders + completed_orders

    conn.close()
    content = render_template_string(
        OPERATIONS_WORKBOARD_TEMPLATE,
        current_picker=current_picker,
        picker_roster=PICKER_ROSTER,
        ops_message=ops_message,
        ops_message_type=ops_message_type,
        shortage_alerts=[
            {
                "order_number": alert["order_number"],
                "sku": alert["sku"],
                "remaining_qty": alert["remaining_qty"],
                "on_hand_qty": alert["on_hand_qty"],
                "warehouse_label": warehouse_code_label(alert["warehouse"]),
                "location_label": clean_display_text(alert["location"], "Unassigned"),
                "issue_status": alert["issue_status"],
                "issue_id": alert["issue_id"] if alert["notified"] else "",
                "notify_url": f"/operations/notify_shortage/{alert['order_number']}?{urlencode({'picker': current_picker})}",
            }
            for alert in active_shortage_alerts
        ],
        workboard_items=workboard_items,
        ready_count=ready_count,
        in_progress_count=in_progress_count,
        completed_count=completed_count,
        completed_lane_count=completed_lane_count,
        shortage_count=len(shortage_alerts),
    )
    return layout(content)


@app.route("/operations/notify_shortage/<order>")
def operations_notify_shortage(order):
    picker = resolve_picker_identity(request.args.get("picker"))
    redirect_params = {"picker": picker}

    conn = get_conn()
    shortage_sync = sync_shortage_order_workflow(conn)
    alerts = [alert for alert in shortage_sync["alerts"] if alert["order_number"] == order]

    if not alerts:
        conn.close()
        redirect_params.update({
            "ops_message": "No open inventory shortage was found for that order.",
            "ops_message_type": "warning",
        })
        return redirect(f"/operations?{urlencode(redirect_params)}")

    created_count = 0
    for alert in alerts:
        if alert["notified"]:
            continue
        create_shortage_supervisor_issue(conn, alert, notified_by=picker)
        created_count += 1

    conn.commit()
    conn.close()

    if created_count:
        message = f"Supervisor notified for order {order}. Inventory shortage escalation is now active."
    else:
        message = f"Supervisor escalation is already active for order {order}."

    redirect_params.update({
        "ops_message": message,
        "ops_message_type": "warning",
    })
    return redirect(f"/operations?{urlencode(redirect_params)}")


@app.route("/operations/orders/<order>/status", methods=["POST"])
def operations_update_order_status(order):
    picker = resolve_picker_identity(request.form.get("picker"))
    target_status = clean_display_text(request.form.get("target_status"), "")

    if target_status not in {"ready", "in_progress", "completed", "take_over"}:
        return redirect(build_operations_redirect_url(picker, "Unsupported workboard status change requested.", "warning"))

    conn = get_conn()
    sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()

    c.execute("SELECT status, source FROM order_header WHERE order_number=?", (order,))
    row = c.fetchone()
    if not row:
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} was not found.", "warning"))

    status, source = row
    current_state = get_operations_board_state(status)

    if target_status == "in_progress":
        if status == BLOCKED_ORDER_STATUS:
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} is blocked by shortage and cannot be started.", "warning"))

        assigned_picker, _pick_started_at = get_pick_start_snapshot(conn, order)
        if status == "Picking in Progress":
            if assigned_picker and assigned_picker != picker:
                conn.close()
                return redirect(build_operations_redirect_url(picker, f"Order {order} is already assigned to {assigned_picker}.", "warning"))
            conn.close()
            return redirect(build_pick_screen_url(order, picker))

        if current_state != "ready":
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} is currently in {status} and cannot be started from the workboard.", "warning"))

        c.execute(
            """
            UPDATE order_header
            SET status='Picking in Progress',
                responsibility='Operations'
            WHERE order_number=?
              AND status='Orders Placed'
            """,
            (order,),
        )
        if c.rowcount != 1:
            conn.rollback()
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} could not be started because its status changed. Refresh and try again.", "warning"))

        log_inventory_transaction(
            conn=conn,
            tx_code="PICK_START",
            order_number=order,
            sku="N/A",
            warehouse=clean_display_text(source, SOURCE_WAREHOUSE),
            location="N/A",
            qty_change=0,
            qty_before=0,
            qty_after=0,
            user_role=picker,
            notes=f"Pick started by {picker} from the operations workboard",
        )
        conn.commit()
        conn.close()
        return redirect(build_pick_screen_url(order, picker))

    if target_status == "ready":
        c.execute(
            """
            UPDATE order_header
            SET status='Orders Placed',
                responsibility='Operations'
            WHERE order_number=?
              AND status='Picking in Progress'
            """,
            (order,),
        )
        if c.rowcount != 1:
            conn.rollback()
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} is not currently in progress, so it could not be moved back to ready.", "warning"))
        log_inventory_transaction(
            conn=conn,
            tx_code="PICK_RELEASE",
            order_number=order,
            sku="N/A",
            warehouse=clean_display_text(source, SOURCE_WAREHOUSE),
            location="N/A",
            qty_change=0,
            qty_before=0,
            qty_after=0,
            user_role=picker,
            notes="Pick released back to the ready queue from the operations workboard",
        )
        conn.commit()
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} was moved back to Ready to Pick.", "success"))

    if target_status == "take_over":
        if status != "Picking in Progress":
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} is no longer in progress, so it cannot be taken over.", "warning"))

        assigned_picker, _pick_started_at = get_pick_start_snapshot(conn, order)
        if not assigned_picker or is_generic_picker_identity(assigned_picker):
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} does not have an active picker assigned yet.", "warning"))

        if assigned_picker == picker:
            conn.close()
            return redirect(build_operations_redirect_url(picker, f"Order {order} is already assigned to you.", "success"))

        log_inventory_transaction(
            conn=conn,
            tx_code="PICK_TAKEOVER",
            order_number=order,
            sku="N/A",
            warehouse=clean_display_text(source, SOURCE_WAREHOUSE),
            location="N/A",
            qty_change=0,
            qty_before=0,
            qty_after=0,
            user_role=picker,
            notes=f"Pick taken over by {picker} from {assigned_picker} on the operations workboard",
        )
        conn.commit()
        conn.close()
        return redirect(build_pick_screen_url(order, picker, f"Order {order} is now assigned to you. Continue picking from the pick screen."))

    if status != "Picking in Progress":
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} is not currently in progress, so it cannot be completed.", "warning"))

    assigned_picker, _pick_started_at = get_pick_start_snapshot(conn, order)
    if assigned_picker and assigned_picker != picker:
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} is assigned to {assigned_picker}. Switch picker or finish it from that picker session.", "warning"))

    expected_units, picked_units = get_order_pick_totals(conn, order)
    if expected_units <= 0:
        c.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM order_lines
            WHERE order_number = ?
            """,
            (order,),
        )
        expected_units = max(int(c.fetchone()[0] or 0), 0)

    if expected_units <= 0:
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} has no planned quantity and cannot be completed.", "warning"))

    if picked_units != expected_units:
        conn.close()
        return redirect(
            build_operations_redirect_url(
                picker,
                f"Finish picking all items before completing. {picked_units} / {expected_units} picked.",
                "warning",
            )
        )

    if not move_order_to_quality_verification(
        conn,
        order,
        source,
        picker,
        picked_units,
        "Pick completed from the operations workboard and moved to quality verification",
    ):
        conn.rollback()
        conn.close()
        return redirect(build_operations_redirect_url(picker, f"Order {order} could not be completed because its status changed. Refresh and try again.", "warning"))

    conn.commit()
    conn.close()
    return redirect(build_operations_redirect_url(picker, f"Order {order} completed and moved to quality verification.", "success"))


@app.route("/inventory")
def inventory_overview():
    sku_filter = request.args.get("sku", "").strip()
    warehouse_filter_raw = request.args.get("warehouse", "").strip()
    warehouse_filter = canonicalize_warehouse_name(warehouse_filter_raw, "") if warehouse_filter_raw else ""
    if warehouse_filter and warehouse_filter not in WAREHOUSE_NETWORK:
        warehouse_filter = ""
    location_filter = request.args.get("location", "").strip()
    tx_code_filter = request.args.get("tx_code", "").strip()
    tx_order_filter = request.args.get("tx_order", "").strip()
    sort_by = request.args.get("sort_by", "sku").strip()
    sort_dir = request.args.get("sort_dir", "asc").strip().lower()
    expanded_sku = request.args.get("expanded", "").strip()
    adjust_sku = request.args.get("adjust_sku", "").strip()
    adjust_warehouse = request.args.get("adjust_warehouse", "").strip()
    adjust_location = request.args.get("adjust_location", "").strip()
    inventory_message = request.args.get("inventory_message", "").strip()
    inventory_message_type = request.args.get("inventory_message_type", "success").strip()
    export_format = request.args.get("export", "").strip().lower()
    low_stock_threshold = parse_low_stock_threshold(request.args.get("low_stock_threshold"))

    try:
        page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        page = 1

    try:
        page_size = int(request.args.get("page_size", "12"))
    except (TypeError, ValueError):
        page_size = 12

    page = max(page, 1)
    page_size = page_size if page_size in {10, 12, 25, 50} else 12
    sort_dir = "desc" if sort_dir == "desc" else "asc"

    current_args = request.args.to_dict(flat=True)

    def build_inventory_url(overrides=None, remove_keys=None, anchor=None):
        params = {key: value for key, value in current_args.items() if value not in {"", None}}
        for key in remove_keys or []:
            params.pop(key, None)
        for key, value in (overrides or {}).items():
            if value in {"", None}:
                params.pop(key, None)
            else:
                params[key] = str(value)
        query = urlencode(params)
        url = "/inventory"
        if query:
            url += f"?{query}"
        if anchor:
            url += f"#{anchor}"
        return url

    sort_columns = {
        "sku": "sku",
        "on_hand": "on_hand_qty",
        "warehouses": "warehouse_count",
        "locations": "location_count",
        "avg_price": "avg_price",
        "inventory_value": "inventory_value",
    }
    order_by_column = sort_columns.get(sort_by, "sku")

    conn = get_conn()
    c = conn.cursor()
    normalized_rows = normalize_inventory_assignments(conn)
    shortage_sync = sync_shortage_order_workflow(conn)
    if normalized_rows:
        conn.commit()
    else:
        conn.commit()

    inventory_where = "WHERE 1=1"
    inventory_params = []

    if sku_filter:
        inventory_where += " AND sku LIKE ?"
        inventory_params.append(f"%{sku_filter}%")
    if warehouse_filter:
        inventory_where += " AND warehouse LIKE ?"
        inventory_params.append(f"%{warehouse_filter}%")
    if location_filter:
        inventory_where += " AND location LIKE ?"
        inventory_params.append(f"%{location_filter}%")

    inventory_summary_query = f"""
        SELECT
            COUNT(DISTINCT sku) AS total_skus,
            COALESCE(SUM(quantity), 0) AS total_on_hand,
            COUNT(DISTINCT CASE
                WHEN quantity > 0
                 AND TRIM(COALESCE(warehouse, '')) <> ''
                 AND TRIM(COALESCE(location, '')) <> ''
                 AND LOWER(TRIM(COALESCE(warehouse, ''))) <> 'nan'
                 AND LOWER(TRIM(COALESCE(location, ''))) <> 'nan'
                THEN warehouse
            END) AS warehouses_active,
            COALESCE(ROUND(SUM(quantity * price), 2), 0) AS inventory_value
        FROM inventory
        {inventory_where}
    """
    c.execute(inventory_summary_query, inventory_params)
    total_skus, total_on_hand, warehouses_active, inventory_value_total = c.fetchone()

    aggregated_inventory_query = f"""
        SELECT
            sku,
            SUM(quantity) AS on_hand_qty,
            COUNT(DISTINCT CASE
                WHEN quantity > 0
                 AND TRIM(COALESCE(warehouse, '')) <> ''
                 AND TRIM(COALESCE(location, '')) <> ''
                 AND LOWER(TRIM(COALESCE(warehouse, ''))) <> 'nan'
                 AND LOWER(TRIM(COALESCE(location, ''))) <> 'nan'
                THEN warehouse
            END) AS warehouse_count,
            COUNT(DISTINCT CASE
                WHEN quantity > 0
                 AND TRIM(COALESCE(warehouse, '')) <> ''
                 AND TRIM(COALESCE(location, '')) <> ''
                 AND LOWER(TRIM(COALESCE(warehouse, ''))) <> 'nan'
                 AND LOWER(TRIM(COALESCE(location, ''))) <> 'nan'
                THEN warehouse || '||' || location
            END) AS location_count,
            ROUND(AVG(price), 2) AS avg_price,
            ROUND(SUM(quantity * price), 2) AS inventory_value
        FROM inventory
        {inventory_where}
        GROUP BY sku
    """

    c.execute(
        f"SELECT COUNT(*) FROM ({aggregated_inventory_query}) grouped_inventory",
        inventory_params,
    )
    total_inventory_rows = c.fetchone()[0]
    total_pages = max(1, (total_inventory_rows + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    c.execute(
        f"""
        {aggregated_inventory_query}
        ORDER BY {order_by_column} {sort_dir.upper()}, sku ASC
        LIMIT ? OFFSET ?
        """,
        inventory_params + [page_size, (page - 1) * page_size],
    )
    inventory_rows = c.fetchall()

    if export_format == "csv":
        c.execute(
            f"""
            {aggregated_inventory_query}
            ORDER BY {order_by_column} {sort_dir.upper()}, sku ASC
            """,
            inventory_params,
        )
        export_rows = c.fetchall()
        output = io.StringIO()
        output.write("SKU,On Hand,Warehouses,Locations,Avg Price,Inventory Value\n")
        for sku, on_hand_qty, warehouse_count, location_count, avg_price, inventory_value in export_rows:
            output.write(
                f'"{sku}",{int(on_hand_qty)},{int(warehouse_count)},{int(location_count)},{float(avg_price or 0):.2f},{float(inventory_value or 0):.2f}\n'
            )
        conn.close()
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=inventory_visibility_export.csv"},
        )

    c.execute(
        f"SELECT COUNT(*) FROM ({aggregated_inventory_query}) grouped_inventory WHERE on_hand_qty <= ?",
        inventory_params + [low_stock_threshold],
    )
    low_stock_count = c.fetchone()[0]

    c.execute(
        f"""
        SELECT sku, on_hand_qty, inventory_value
        FROM ({aggregated_inventory_query}) grouped_inventory
        WHERE on_hand_qty <= ?
        ORDER BY on_hand_qty ASC, sku ASC
        LIMIT 6
        """,
        inventory_params + [low_stock_threshold],
    )
    low_stock_rows = c.fetchall()

    warehouse_options = list(WAREHOUSE_NETWORK)
    selected_warehouse = warehouse_filter if warehouse_filter in warehouse_options else ""
    warehouse_filter_options = ['<option value="">Any warehouse</option>'] + [
        f"<option value='{warehouse}'{' selected' if warehouse == selected_warehouse else ''}>{warehouse}</option>"
        for warehouse in warehouse_options
    ]
    adjust_warehouse_value = canonicalize_warehouse_name(
        adjust_warehouse or warehouse_filter or SOURCE_WAREHOUSE,
        SOURCE_WAREHOUSE,
    )
    if adjust_warehouse_value not in warehouse_options:
        adjust_warehouse_value = SOURCE_WAREHOUSE
    adjust_warehouse_options = "".join(
        f"<option value='{warehouse}'{' selected' if warehouse == adjust_warehouse_value else ''}>{warehouse}</option>"
        for warehouse in warehouse_options
    )
    c.execute(
        """
        SELECT DISTINCT location
        FROM inventory
        WHERE TRIM(COALESCE(location, '')) <> ''
          AND LOWER(TRIM(COALESCE(location, ''))) <> 'nan'
        ORDER BY location
        """
    )
    location_options = [row[0] for row in c.fetchall()]
    c.execute("SELECT DISTINCT sku FROM inventory WHERE TRIM(COALESCE(sku, '')) <> '' ORDER BY sku LIMIT 500")
    sku_options = [row[0] for row in c.fetchall()]

    detail_rows = []
    page_skus = [row[0] for row in inventory_rows]
    if page_skus:
        placeholders = ", ".join(["?"] * len(page_skus))
        detail_query = f"""
            SELECT
                sku,
                warehouse,
                location,
                SUM(quantity) AS quantity,
                ROUND(AVG(price), 2) AS price
            FROM inventory
            WHERE sku IN ({placeholders})
              AND quantity > 0
              AND TRIM(COALESCE(warehouse, '')) <> ''
              AND TRIM(COALESCE(location, '')) <> ''
              AND LOWER(TRIM(COALESCE(warehouse, ''))) <> 'nan'
              AND LOWER(TRIM(COALESCE(location, ''))) <> 'nan'
        """
        detail_params = list(page_skus)
        if warehouse_filter:
            detail_query += " AND warehouse LIKE ?"
            detail_params.append(f"%{warehouse_filter}%")
        if location_filter:
            detail_query += " AND location LIKE ?"
            detail_params.append(f"%{location_filter}%")
        detail_query += " GROUP BY sku, warehouse, location ORDER BY sku, warehouse, location"
        c.execute(detail_query, detail_params)
        detail_rows = c.fetchall()

    location_lookup = {}
    for sku, warehouse, location, quantity, price in detail_rows:
        location_lookup.setdefault(sku, []).append((warehouse, location, quantity, price))

    c.execute(
        """
        SELECT
            ol.sku,
            SUM(ol.quantity) AS open_demand,
            COALESCE(inv.on_hand_qty, 0) AS on_hand_qty
        FROM order_lines ol
        JOIN order_header oh ON oh.order_number = ol.order_number
        LEFT JOIN (
            SELECT sku, SUM(quantity) AS on_hand_qty
            FROM inventory
            GROUP BY sku
        ) inv ON inv.sku = ol.sku
        WHERE oh.status != 'Completed'
        GROUP BY ol.sku
        HAVING SUM(ol.quantity) > COALESCE(inv.on_hand_qty, 0)
        ORDER BY (SUM(ol.quantity) - COALESCE(inv.on_hand_qty, 0)) DESC, ol.sku ASC
        LIMIT 6
        """
    )
    replenishment_rows = c.fetchall()

    c.execute(
        """
        SELECT sku, warehouse, location, quantity
        FROM inventory
        WHERE quantity < 0
           OR TRIM(COALESCE(warehouse, '')) = ''
           OR TRIM(COALESCE(location, '')) = ''
        ORDER BY quantity ASC, sku ASC
        LIMIT 6
        """
    )
    discrepancy_rows = c.fetchall()

    c.execute(
        f"""
        SELECT COUNT(*)
        FROM ({aggregated_inventory_query}) grouped_inventory
        WHERE on_hand_qty <= ?
        """
           , inventory_params + [low_stock_threshold]
    )
    global_low_stock_count = c.fetchone()[0]

    c.execute(
        f"""
        SELECT sku, on_hand_qty, inventory_value
        FROM ({aggregated_inventory_query}) grouped_inventory
        WHERE on_hand_qty <= ?
        ORDER BY on_hand_qty ASC, sku ASC
        LIMIT 6
        """,
        inventory_params + [low_stock_threshold],
    )
    global_low_stock_rows = c.fetchall()

    c.execute(
        """
        SELECT tx_time, order_number, sku, warehouse, location, notes
        FROM (
            SELECT
                tx_time,
                order_number,
                sku,
                warehouse,
                location,
                notes,
                ROW_NUMBER() OVER (
                    PARTITION BY order_number, sku, warehouse, location
                    ORDER BY tx_time DESC
                ) AS rn
            FROM inventory_transactions
            WHERE tx_code = 'SHORTAGE'
        ) shortage_warnings
        WHERE rn = 1
        ORDER BY tx_time DESC
        LIMIT 6
        """
    )
    shortage_warning_rows = c.fetchall()

    tx_query = """
        SELECT tx_time, tx_code, order_number, sku, warehouse, location,
               qty_change, qty_before, qty_after, user_role
        FROM inventory_transactions
        WHERE 1=1
    """
    tx_params = []

    if tx_code_filter:
        tx_query += " AND tx_code = ?"
        tx_params.append(tx_code_filter)
    if tx_order_filter:
        tx_query += " AND order_number LIKE ?"
        tx_params.append(f"%{tx_order_filter}%")
    if sku_filter:
        tx_query += " AND sku LIKE ?"
        tx_params.append(f"%{sku_filter}%")
    if warehouse_filter:
        tx_query += " AND warehouse LIKE ?"
        tx_params.append(f"%{warehouse_filter}%")
    if location_filter:
        tx_query += " AND location LIKE ?"
        tx_params.append(f"%{location_filter}%")

    tx_query += " ORDER BY tx_time DESC LIMIT 15"
    c.execute(tx_query, tx_params)
    tx_rows = c.fetchall()

    conn.close()

    def format_currency(value):
        return f"${float(value or 0):,.2f}"

    def format_int(value):
        return f"{int(value or 0):,}"

    message_html = ""
    if inventory_message:
        banner_type = "error" if inventory_message_type == "error" else "success"
        message_html = f"<div class='message-banner {banner_type}'><div><b>Inventory Update</b><div>{inventory_message}</div></div></div>"

    sort_labels = {
        "sku": "SKU",
        "on_hand": "On Hand",
        "warehouses": "Warehouses",
        "locations": "Locations",
        "avg_price": "Avg Price",
        "inventory_value": "Inventory Value",
    }

    def header_sort_link(column_key):
        next_dir = "desc" if sort_by == column_key and sort_dir == "asc" else "asc"
        indicator = ""
        if sort_by == column_key:
            indicator = "<span class='sort-indicator'>&darr;</span>" if sort_dir == "desc" else "<span class='sort-indicator'>&uarr;</span>"
        return (
            f"<a class='sort-link' href='{build_inventory_url({'sort_by': column_key, 'sort_dir': next_dir, 'page': 1}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='inventory-table')}'>{sort_labels[column_key]}{indicator}</a>"
        )

    inventory_table_rows = ""
    if inventory_rows:
        for sku, on_hand_qty, warehouse_count, location_count, avg_price, inventory_value in inventory_rows:
            is_expanded = expanded_sku == sku
            toggle_target = "" if is_expanded else sku
            inventory_table_rows += f"""
            <tr id='inventory-row-{sku}'>
                <td class='sku-cell'>
                    <div class='sku-title'>{sku}</div>
                    <div class='section-note'>Filtered inventory rollup</div>
                </td>
                <td><span class='value-strong'>{format_int(on_hand_qty)}</span></td>
                <td>{format_int(warehouse_count)}</td>
                <td>{format_int(location_count)}</td>
                <td>{format_currency(avg_price)}</td>
                <td><span class='value-strong'>{format_currency(inventory_value)}</span></td>
                <td>
                    <div class='inventory-actions'>
                        <a class='text-link' href='{build_inventory_url({'expanded': toggle_target}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='inventory-table')}'>View Locations</a>
                        <a class='text-link' href='{build_inventory_url({'adjust_sku': sku, 'adjust_warehouse': '', 'adjust_location': ''}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='adjust-inventory')}'>Adjust Inventory</a>
                        <a class='text-link' href='{build_inventory_url({'sku': sku, 'page': 1}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='transaction-history')}'>View Transaction History</a>
                    </div>
                </td>
            </tr>
            """
            if is_expanded:
                location_details = ""
                sku_locations = location_lookup.get(sku, [])
                if sku_locations:
                    grouped_locations = {}
                    for warehouse, location, quantity, price in sku_locations:
                        warehouse_label = clean_display_text(warehouse, "Receiving Dock")
                        location_label = clean_display_text(location, "Receiving Dock")
                        grouped_locations.setdefault(warehouse_label, []).append((location_label, quantity, price))

                    total_distinct_locations = sum(len(rows) for rows in grouped_locations.values())
                    summary_chips = (
                        f"<div class='section-note' style='margin-bottom:10px;'>"
                        f"{len(grouped_locations)} warehouse(s), {total_distinct_locations} location(s)</div>"
                    )
                    location_details = summary_chips
                    for warehouse_label, warehouse_rows in grouped_locations.items():
                        location_details += (
                            f"<div class='mini-panel' style='margin-bottom:12px;'>"
                            f"<div class='sku-title' style='margin-bottom:8px;'>{warehouse_label}</div>"
                            "<table class='ops-detail-table'>"
                            "<tr><th>Location</th><th>Qty</th><th>Unit Price</th></tr>"
                        )
                        for location_label, quantity, price in warehouse_rows:
                            location_details += (
                                f"<tr><td>{location_label}</td><td>{format_int(quantity)}</td><td>{format_currency(price)}</td></tr>"
                            )
                        location_details += "</table></div>"
                else:
                    location_details = "<div class='empty-state'>No locations with active inventory match the current filters for this SKU.</div>"
                inventory_table_rows += f"""
                <tr class='expand-row'>
                    <td colspan='7'>
                        {location_details}
                    </td>
                </tr>
                """
    else:
        inventory_table_rows = "<tr><td colspan='7'><div class='empty-state'>No inventory records matched the current filters.</div></td></tr>"

    shortage_alerts = shortage_sync["alerts"]
    shortage_warning_html = "<div class='empty-state'>No blocked picks are currently waiting on inventory action.</div>"
    if shortage_alerts:
        shortage_warning_html = "<div class='alert-list'>"
        for alert in shortage_alerts[:6]:
            escalation_link = (
                f"<a class='text-link' href='/supervisor_issue/{alert['issue_id']}'>Open Escalation</a>"
                if alert["issue_id"]
                else "<span class='section-note'>Waiting on Supervisor notification</span>"
            )
            shortage_warning_html += f"""
            <div class='alert-item'>
                <div>
                    <div class='sku-title'>{alert['sku']} <span class='section-note' style='margin-left:6px;'>Order {alert['order_number']}</span></div>
                    <div class='section-note'>Blocked at {alert['blocked_at'] or alert['order_date']}</div>
                    <div class='section-note'>{warehouse_code_label(alert['warehouse'])} / {clean_display_text(alert['location'], 'Unassigned')}</div>
                    <div class='section-note'>Remaining {format_int(alert['remaining_qty'])} vs on-hand {format_int(alert['on_hand_qty'])} &mdash; {alert['reason']}</div>
                </div>
                <div class='table-actions'>
                    <a href='{alert['inventory_action_url']}'>Resolve Shortage</a>
                    {escalation_link}
                </div>
            </div>
            """
        shortage_warning_html += "</div>"

    low_stock_html = "<div class='empty-state'>No low stock SKUs across the warehouse network.</div>"
    if global_low_stock_rows:
        low_stock_html = "<div class='alert-list'>"
        for sku, on_hand_qty, item_value in global_low_stock_rows:
            low_stock_html += f"""
            <div class='alert-item'>
                <div>
                    <div class='sku-title'>{sku}</div>
                    <div class='section-note'>Value at risk {format_currency(item_value)}</div>
                </div>
                <div class='pill low'>{format_int(on_hand_qty)} left</div>
            </div>
            """
        low_stock_html += "</div>"

    replenishment_html = "<div class='empty-state'>Open demand is covered by current on-hand inventory.</div>"
    if replenishment_rows:
        replenishment_html = "<div class='alert-list'>"
        for sku, open_demand, on_hand_qty in replenishment_rows:
            shortage = int(open_demand or 0) - int(on_hand_qty or 0)
            replenishment_html += f"""
            <div class='alert-item'>
                <div>
                    <div class='sku-title'>{sku}</div>
                    <div class='section-note'>Demand {format_int(open_demand)} vs on-hand {format_int(on_hand_qty)}</div>
                </div>
                <div class='pill issue'>Short {format_int(shortage)}</div>
            </div>
            """
        replenishment_html += "</div>"

    discrepancy_html = "<div class='empty-state'>No negative balances or missing location master data detected.</div>"
    if discrepancy_rows:
        discrepancy_html = "<div class='alert-list'>"
        for sku, warehouse, location, quantity in discrepancy_rows:
            discrepancy_html += f"""
            <div class='alert-item'>
                <div>
                    <div class='sku-title'>{sku}</div>
                    <div class='section-note'>{clean_display_text(warehouse, 'Warehouse missing')} / {clean_display_text(location, 'Location missing')}</div>
                </div>
                <div class='pill issue'>{format_int(quantity)}</div>
            </div>
            """
        discrepancy_html += "</div>"

    tx_counts = {"PICK": 0, "ADJUST": 0, "LOAD": 0}
    for _, tx_code, _, _, _, _, _, _, _, _ in tx_rows:
        if tx_code in tx_counts:
            tx_counts[tx_code] += 1

    tx_days = {}
    for tx_time, _, _, _, _, _, _, _, _, _ in tx_rows:
        tx_day = str(tx_time or "").split("T", 1)[0]
        if tx_day:
            tx_days[tx_day] = tx_days.get(tx_day, 0) + 1
    avg_daily = round(sum(tx_days.values()) / len(tx_days), 1) if tx_days else 0
    stock_coverage_days = round(total_on_hand / max(avg_daily, 1), 1) if total_on_hand and avg_daily else "—"

    pagination_links = ""
    if total_pages > 1:
        start_page = max(1, page - 2)
        end_page = min(total_pages, page + 2)
        if page > 1:
            pagination_links += f"<a class='pagination-link' href='{build_inventory_url({'page': page - 1}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='inventory-table')}'>&laquo;</a>"
        for page_number in range(start_page, end_page + 1):
            active_class = " active" if page_number == page else ""
            pagination_links += f"<a class='pagination-link{active_class}' href='{build_inventory_url({'page': page_number}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='inventory-table')}'>{page_number}</a>"
        if page < total_pages:
            pagination_links += f"<a class='pagination-link' href='{build_inventory_url({'page': page + 1}, remove_keys=['export', 'inventory_message', 'inventory_message_type'], anchor='inventory-table')}'>&raquo;</a>"

    content = f"""
    <div class='page-header'>
        <div class='page-header-copy'>
            <div class='page-eyebrow'>Warehouse Intelligence</div>
            <h1>Inventory Visibility</h1>
            <p>Monitor stock health, surface exceptions, and move directly from analysis into action without digging through spreadsheets.</p>
        </div>
        <div class='page-actions'>
            {f"<form method='post' action='/inventory_restock_shortages' style='margin:0;'><input type='hidden' name='low_stock_threshold' value='{low_stock_threshold}'><button type='submit' class='action-btn secondary' style='width:auto;'>Restock Blocked SKUs</button></form>" if shortage_alerts else ''}
            <a class='action-btn secondary' href='{build_inventory_url({'export': 'csv'}, remove_keys=['inventory_message', 'inventory_message_type'])}'>Export</a>
            <a class='action-btn secondary' href='{build_inventory_url({'page': 1}, remove_keys=['export', 'inventory_message', 'inventory_message_type'])}'>Refresh</a>
            <a class='action-btn primary' href='#adjust-inventory'>Adjust Inventory</a>
        </div>
    </div>

    {message_html}

    <div class='dashboard-kpis'>
        <div class='kpi-card'>
            <div class='kpi-title'>Total SKUs</div>
            <div class='kpi-number'>{format_int(total_skus)}</div>
            <div class='kpi-footnote'>Distinct items in the current view</div>
        </div>
        <div class='kpi-card'>
            <div class='kpi-title'>Total On-Hand Inventory</div>
            <div class='kpi-number'>{format_int(total_on_hand)}</div>
            <div class='kpi-footnote'>Units available across filtered locations</div>
        </div>
        <div class='kpi-card'>
            <div class='kpi-title'>Low Stock Items</div>
            <div class='kpi-number'>{format_int(global_low_stock_count)}</div>
            <div class='kpi-footnote'>SKUs at or below {low_stock_threshold} units</div>
        </div>
        <div class='kpi-card'>
            <div class='kpi-title'>Inventory Value</div>
            <div class='kpi-number'>{format_currency(inventory_value_total)}</div>
            <div class='kpi-footnote'>Calculated from on-hand quantity and average price</div>
        </div>
        <div class='kpi-card'>
            <div class='kpi-title'>Warehouses Active</div>
            <div class='kpi-number'>{format_int(warehouses_active)}</div>
            <div class='kpi-footnote'>Warehouses represented in this filtered slice</div>
        </div>
    </div>
        <div class='card' style='margin-bottom:20px;'>
            <div class='table-card-header' style='padding:0 0 14px 0;'>
                <div>
                    <h3>Inventory Warnings</h3>
                    <p class='section-note'>Immediate signals for inventory action, including blocked orders that can be released back to Operations after stock is corrected. Active alert threshold: {format_int(low_stock_threshold)} units.</p>
                </div>
                <div class='toolbar-meta'>
                    <span class='meta-chip'>{format_int(len(shortage_alerts))} blocked order(s)</span>
                    <span class='meta-chip'>{format_int(global_low_stock_count)} low stock SKU(s)</span>
                    <span class='meta-chip'>Alert threshold {format_int(low_stock_threshold)}</span>
                </div>
            </div>
            <div style='display:grid;grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));gap:16px;'>
                <div class='alert-panel'>
                    <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;'>
                        <h3 style='margin:0;'>Blocked Orders Requiring Inventory</h3>
                        <span class='pill issue'>{format_int(len(shortage_alerts))}</span>
                    </div>
                    {shortage_warning_html}
                </div>
                <div class='alert-panel'>
                    <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;'>
                        <h3 style='margin:0;'>Low Stock SKUs</h3>
                        <span class='pill low'>{format_int(global_low_stock_count)}</span>
                    </div>
                    {low_stock_html}
                </div>
                <div class='alert-panel'>
                    <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;'>
                        <h3 style='margin:0;'>Replenishment Needed</h3>
                        <span class='pill issue'>{format_int(len(replenishment_rows))}</span>
                    </div>
                    {replenishment_html}
                </div>
                <div class='alert-panel'>
                    <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;'>
                        <h3 style='margin:0;'>Inventory Discrepancies</h3>
                        <span class='pill {'issue' if discrepancy_rows else 'ok'}'>{format_int(len(discrepancy_rows))}</span>
                    </div>
                    {discrepancy_html}
                </div>
            </div>
        </div>

    <div class='card filter-panel'>
        <div class='filter-panel-header'>
            <div>
                <h3>Search and Filters</h3>
                <p class='section-note'>Refine the inventory view, transaction history, and action panels from one place.</p>
            </div>
            <div class='toolbar-meta'>
                <span class='meta-chip'>{format_int(total_inventory_rows)} SKU rows</span>
                <span class='meta-chip'>Page {page} of {total_pages}</span>
            </div>
        </div>
        <form method='get'>
            <div class='filter-grid' style='grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));'>
                <div class='filter-field'>
                    <label class='filter-label'><b>SKU Search</b></label>
                    <input name='sku' value='{sku_filter}' placeholder='Find by SKU code' style='padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;'>
                </div>
                <div class='filter-field'>
                    <label class='filter-label' for='inventory-warehouse'><b>Warehouse</b></label>
                    <select id='inventory-warehouse' name='warehouse'>
                        {''.join(warehouse_filter_options)}
                    </select>
                </div>
                <div class='filter-field'>
                    <label class='filter-label'><b>Location</b></label>
                    <input name='location' value='{location_filter}' list='location-options' placeholder='Any location' style='padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;'>
                </div>
                <div class='filter-field'>
                    <label class='filter-label'><b>Rows Per Page</b></label>
                    <select name='page_size' style='padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;'>
                        <option value='12' {'selected' if page_size == 12 else ''}>12</option>
                        <option value='25' {'selected' if page_size == 25 else ''}>25</option>
                        <option value='50' {'selected' if page_size == 50 else ''}>50</option>
                    </select>
                </div>
                <div class='filter-field'>
                    <label class='filter-label'><b>Low Stock Alert Threshold</b></label>
                    <input name='low_stock_threshold' type='number' min='1' value='{low_stock_threshold}' placeholder='Units remaining' style='padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;'>
                </div>
            </div>
            <details style='margin-top:12px;'>
                <summary style='cursor:pointer;list-style:none;color:#475569;font-size:13px;font-weight:700;'>More filters</summary>
                <div class='filter-grid' style='margin-top:12px;grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));'>
                    <div class='filter-field'>
                        <label class='filter-label'>Transaction Code</label>
                        <select name='tx_code'>
                            <option value=''>All transaction codes</option>
                            <option value='PICK' {'selected' if tx_code_filter == 'PICK' else ''}>PICK</option>
                            <option value='ADJUST' {'selected' if tx_code_filter == 'ADJUST' else ''}>ADJUST</option>
                            <option value='LOAD' {'selected' if tx_code_filter == 'LOAD' else ''}>LOAD</option>
                            <option value='SHORTAGE' {'selected' if tx_code_filter == 'SHORTAGE' else ''}>SHORTAGE</option>
                        </select>
                    </div>
                    <div class='filter-field'>
                        <label class='filter-label'>Order Reference</label>
                        <input name='tx_order' value='{tx_order_filter}' placeholder='Order number'>
                    </div>
                </div>
            </details>
            <input type='hidden' name='sort_by' value='{sort_by}'>
            <input type='hidden' name='sort_dir' value='{sort_dir}'>
            <div class='filter-foot'>
                <div class='toolbar-meta'>
                        <span class='meta-chip'>Sortable columns</span>
                        <span class='meta-chip'>Expandable rows</span>
                        <span class='meta-chip'>CSV export</span>
                        <span class='meta-chip'>Threshold alerts</span>
                </div>
                <div class='page-actions'>
                    <a class='action-btn secondary' href='/inventory'>Reset</a>
                    <button type='submit'>Apply Filters</button>
                </div>
            </div>
        </form>
        <datalist id='location-options'>
            {''.join(f"<option value='{location}'></option>" for location in location_options)}
        </datalist>
    </div>

    <div class='table-card' id='inventory-table'>
        <div class='table-card-header'>
            <div>
                <h3>Inventory by SKU</h3>
                <p class='section-note'>Sortable, paginated inventory rollup with location drilldown and direct actions.</p>
            </div>
            <div class='toolbar-meta'>
                <span class='meta-chip'>Sorted by {sort_labels.get(sort_by, 'SKU')}</span>
                <span class='meta-chip'>{format_int(len(inventory_rows))} rows on this page</span>
            </div>
        </div>
        <div class='table-shell'>
            <table class='sticky-table zebra'>
                <thead>
                    <tr>
                        <th>{header_sort_link('sku')}</th>
                        <th>{header_sort_link('on_hand')}</th>
                        <th>{header_sort_link('warehouses')}</th>
                        <th>{header_sort_link('locations')}</th>
                        <th>{header_sort_link('avg_price')}</th>
                        <th>{header_sort_link('inventory_value')}</th>
                        <th>Quick Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {inventory_table_rows}
                </tbody>
            </table>
        </div>
        <div class='pagination'>
            <div class='section-note'>Showing {(page - 1) * page_size + (1 if inventory_rows else 0)}-{(page - 1) * page_size + len(inventory_rows)} of {total_inventory_rows} SKU rows</div>
            <div class='pagination-links'>{pagination_links if pagination_links else "<span class='section-note'>Single page view</span>"}</div>
        </div>
    </div>

    <div class='card-split' style='margin-top:24px;'>
        <details id='adjust-inventory' style='background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;'>
            <summary style='cursor:pointer;list-style:none;padding:14px 18px;font-size:15px;font-weight:700;color:#0f172a;display:flex;align-items:center;gap:8px;background:#f8fafc;border-bottom:1px solid #e2e8f0;'>
                <span>&#9881; Adjust Inventory</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div class='stack-card' style='border:none;border-radius:0;margin:0;'>
            <p class='section-note'>Apply targeted quantity corrections and create an ADJUST transaction for audit traceability.</p>
            <form method='post' action='/inventory_adjust'>
                <input type='hidden' name='low_stock_threshold' value='{low_stock_threshold}'>
                <div class='sub-grid'>
                    <div class='filter-field'>
                        <label class='filter-label'>SKU</label>
                        <input name='sku' value='{adjust_sku or sku_filter}' list='sku-options' placeholder='Select SKU' required>
                    </div>
                    <div class='filter-field'>
                        <label class='filter-label' for='adjust-warehouse'>Warehouse</label>
                        <select id='adjust-warehouse' name='warehouse' required>
                            {adjust_warehouse_options}
                        </select>
                    </div>
                    <div class='filter-field'>
                        <label class='filter-label'>Location</label>
                        <input name='location' value='{adjust_location or location_filter}' list='location-options' placeholder='Location' required>
                    </div>
                    <div class='filter-field'>
                        <label class='filter-label'>Adjustment Qty</label>
                        <input name='qty_change' type='number' placeholder='Use negative for decreases' required>
                    </div>
                </div>
                <div class='filter-field' style='margin-top:14px;'>
                    <label class='filter-label'>Notes</label>
                    <textarea name='notes' style='min-height:110px;' placeholder='Explain why this inventory adjustment is required'></textarea>
                </div>
                <div class='filter-foot'>
                    <div class='section-note'>Each adjustment updates on-hand quantity and writes an inventory transaction record.</div>
                    <button type='submit'>Submit Adjustment</button>
                </div>
            </form>
            <datalist id='sku-options'>
                {''.join(f"<option value='{sku}'></option>" for sku in sku_options)}
            </datalist>
            </div>
        </details>

        <details id='transaction-history' style='background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;'>
            <summary style='cursor:pointer;list-style:none;padding:14px 18px;font-size:15px;font-weight:700;color:#0f172a;display:flex;align-items:center;gap:8px;background:#f8fafc;border-bottom:1px solid #e2e8f0;'>
                <span>&#128203; Transaction History &mdash; {format_int(len(tx_rows))} records</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div class='stack-card' style='border:none;border-radius:0;margin:0;'>
            <p class='section-note'>Transaction view is automatically aligned with the active filter panel.</p>
            <div class='transaction-summary'>
                <span class='meta-chip'>Showing {format_int(len(tx_rows))} transactions</span>
                <span class='meta-chip'>PICK {format_int(tx_counts['PICK'])}</span>
                <span class='meta-chip'>ADJUST {format_int(tx_counts['ADJUST'])}</span>
                <span class='meta-chip'>LOAD {format_int(tx_counts['LOAD'])}</span>
            </div>
            <div class='table-shell' style='padding:0;'>
                <table class='sticky-table zebra'>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Code</th>
                            <th>Order</th>
                            <th>SKU</th>
                            <th>Warehouse</th>
                            <th>Location</th>
                            <th>Qty</th>
                            <th>Before</th>
                            <th>After</th>
                            <th>Role</th>
                        </tr>
                    </thead>
                    <tbody>
            """

    if tx_rows:
        for tx_time, tx_code, order_number, sku, warehouse, location, qty_change, qty_before, qty_after, user_role in tx_rows:
            change_badge = "pill issue" if int(qty_change) < 0 else "pill ok"
            content += f"""
                        <tr>
                            <td>{tx_time}</td>
                            <td><span class='{change_badge}'>{tx_code}</span></td>
                            <td>{order_number}</td>
                            <td>{sku}</td>
                            <td>{warehouse_code_label(warehouse)}</td>
                            <td>{clean_display_text(location, 'Unassigned')}</td>
                            <td>{qty_change}</td>
                            <td>{qty_before}</td>
                            <td>{qty_after}</td>
                            <td>{user_role}</td>
                        </tr>
            """
    else:
        content += "<tr><td colspan='10'><div class='empty-state'>No transactions matched the current filter combination.</div></td></tr>"

    content += """
                    </tbody>
                </table>
            </div>
            </div>
        </details>
    </div>
    """

    return layout(content)


@app.route("/inventory_adjust", methods=["POST"])
def inventory_adjust():
    sku = request.form.get("sku", "").strip()
    warehouse = canonicalize_warehouse_name(request.form.get("warehouse", "").strip(), "")
    location = request.form.get("location", "").strip()
    qty_change_raw = request.form.get("qty_change", "").strip()
    notes = request.form.get("notes", "").strip()

    redirect_params = {
        "adjust_sku": sku,
        "adjust_warehouse": warehouse,
        "adjust_location": location,
        "low_stock_threshold": parse_low_stock_threshold(request.form.get("low_stock_threshold")),
    }

    if not sku or not warehouse or warehouse not in WAREHOUSE_NETWORK or not location:
        redirect_params.update({
            "inventory_message": "SKU, a valid network warehouse, and location are required before an adjustment can be posted.",
            "inventory_message_type": "error",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}#adjust-inventory")

    try:
        qty_change = int(qty_change_raw)
    except (TypeError, ValueError):
        redirect_params.update({
            "inventory_message": "Adjustment quantity must be a whole number.",
            "inventory_message_type": "error",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}#adjust-inventory")

    if qty_change == 0:
        redirect_params.update({
            "inventory_message": "Adjustment quantity cannot be zero.",
            "inventory_message_type": "error",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}#adjust-inventory")

    conn = get_conn()
    try:
        adjustment_result = apply_inventory_adjustment(
            conn=conn,
            sku=sku,
            warehouse=warehouse,
            location=location,
            qty_change=qty_change,
            notes=notes or "Manual inventory adjustment from Inventory Visibility dashboard",
            user_role="Inventory",
            order_number="MANUAL-ADJUST",
        )
    except ValueError as exc:
        conn.close()
        redirect_params.update({
            "inventory_message": str(exc),
            "inventory_message_type": "error",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}#adjust-inventory")

    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    conn.close()

    direction = "increased" if qty_change > 0 else "decreased"
    resumed_suffix = summarize_resumed_orders(shortage_sync["resumed_orders"])
    redirect_params.update({
        "sku": sku,
        "warehouse": warehouse,
        "location": location,
        "inventory_message": f"Inventory {direction} by {abs(adjustment_result['qty_change'])} units for {sku} at {warehouse} / {location}.{resumed_suffix}",
        "inventory_message_type": "success",
    })
    return redirect(f"/inventory?{urlencode(redirect_params)}#adjust-inventory")


@app.route("/inventory_restock_shortages", methods=["POST"])
def inventory_restock_shortages():
    low_stock_threshold = parse_low_stock_threshold(request.form.get("low_stock_threshold"))
    redirect_params = {
        "low_stock_threshold": low_stock_threshold,
    }

    conn = get_conn()
    c = conn.cursor()
    shortage_sync = sync_shortage_order_workflow(conn)
    alerts = shortage_sync["alerts"]

    if not alerts:
        conn.close()
        redirect_params.update({
            "inventory_message": "No blocked shortage alerts were open, so no restock was posted.",
            "inventory_message_type": "success",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}")

    grouped_alerts = {}
    for alert in alerts:
        key = (alert["sku"], alert["warehouse"], alert["location"])
        if key not in grouped_alerts:
            grouped_alerts[key] = {
                "sku": alert["sku"],
                "warehouse": alert["warehouse"],
                "location": alert["location"],
                "total_remaining": 0,
                "orders": set(),
            }
        grouped_alerts[key]["total_remaining"] += int(alert["remaining_qty"] or 0)
        grouped_alerts[key]["orders"].add(alert["order_number"])

    restocked_skus = []
    total_units_added = 0
    for plan in grouped_alerts.values():
        c.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM inventory
            WHERE sku = ? AND warehouse = ? AND location = ?
            """,
            (plan["sku"], plan["warehouse"], plan["location"]),
        )
        current_on_hand = int(c.fetchone()[0] or 0)
        target_on_hand = max(plan["total_remaining"], low_stock_threshold)
        qty_change = max(target_on_hand - current_on_hand, 0)
        if qty_change <= 0:
            continue

        order_list = sorted(plan["orders"])
        apply_inventory_adjustment(
            conn=conn,
            sku=plan["sku"],
            warehouse=plan["warehouse"],
            location=plan["location"],
            qty_change=qty_change,
            notes=(
                f"Bulk shortage restock to unblock open orders and raise stock to the alert threshold of {low_stock_threshold} units. "
                f"Orders: {', '.join(order_list)}"
            ),
            user_role="Inventory",
            order_number="SHORTAGE-RESTOCK",
        )
        total_units_added += qty_change
        restocked_skus.append(plan["sku"])

    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    conn.close()

    if not restocked_skus:
        redirect_params.update({
            "inventory_message": f"Blocked SKUs already met the active threshold of {low_stock_threshold} units. No additional stock was posted.",
            "inventory_message_type": "success",
        })
        return redirect(f"/inventory?{urlencode(redirect_params)}")

    resumed_suffix = summarize_resumed_orders(shortage_sync["resumed_orders"])
    redirect_params.update({
        "inventory_message": (
            f"Restocked {len(restocked_skus)} blocked SKU(s) by {total_units_added} total units to the active threshold of {low_stock_threshold}."
            f" SKUs: {', '.join(sorted(restocked_skus))}.{resumed_suffix}"
        ),
        "inventory_message_type": "success",
    })
    return redirect(f"/inventory?{urlencode(redirect_params)}")


@app.route("/pick_screen/<order>", methods=["GET","POST"])
def pick_screen(order):
    conn = get_conn()
    sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()
    active_picker = resolve_picker_identity(request.values.get("picker"))
    pick_message = request.args.get("pick_message", "").strip()
    pick_message_type = "warning" if request.args.get("pick_message_type", "success").strip() == "warning" else "success"
    active_picker_attr = active_picker.replace("'", "&#39;")
    operations_url = f"/operations?{urlencode({'picker': active_picker})}"

    c.execute("""
        SELECT source, destination, status
        FROM order_header
        WHERE order_number = ?
    """, (order,))
    hdr = c.fetchone()

    if not hdr:
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Pick Confirmation</h2>
                <p>Order not found.</p>
                <a href='{operations_url}'>Back to Operations</a>
            </div>
        """)

    source_wh, dest_wh, order_status = hdr

    if order_status == BLOCKED_ORDER_STATUS:
        shortage_alerts = [alert for alert in get_open_shortage_alerts(conn) if alert["order_number"] == order]
        issue_id = next((alert["issue_id"] for alert in shortage_alerts if alert["issue_id"]), "")
        notify_action = (
            f"<a class='quick-link' href='/supervisor_issue/{issue_id}'>Open Supervisor Escalation</a>"
            if issue_id
            else f"<a class='quick-link' href='/operations/notify_shortage/{order}?{urlencode({'picker': active_picker})}'>Notify Supervisor</a>"
        )
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Order Blocked</h2>
                <p>Order <b>{order}</b> is blocked due to inventory shortage and cannot be picked until Inventory replenishes the required stock.</p>
                <div class='quick-links'>
                    {notify_action}
                    <a class='quick-link' href='/order/{order}?context=operations'>View Order</a>
                    <a class='quick-link' href='{operations_url}'>Back to Operations</a>
                </div>
            </div>
        """)

    if order_status not in {"Orders Placed", "Picking in Progress"}:
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Pick Locked</h2>
                <p>Order <b>{order}</b> is currently in <b>{order_status}</b> and cannot be opened for picking.</p>
                <a href='{operations_url}'>Back to Operations</a>
            </div>
        """)

    assigned_picker, pick_started_at = get_pick_start_snapshot(conn, order)

    if order_status == "Picking in Progress" and not assigned_picker:
        log_inventory_transaction(
            conn=conn,
            tx_code="PICK_START",
            order_number=order,
            sku="N/A",
            warehouse=clean_display_text(source_wh, SOURCE_WAREHOUSE),
            location="N/A",
            qty_change=0,
            qty_before=0,
            qty_after=0,
            user_role=active_picker,
            notes=f"Pick ownership claimed by {active_picker}",
        )
        conn.commit()
        assigned_picker = active_picker
        pick_started_at = current_timestamp()

    if order_status == "Picking in Progress" and assigned_picker and assigned_picker != active_picker:
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Pick Locked</h2>
                <p>Order <b>{order}</b> is already being picked by <b>{assigned_picker}</b>.</p>
                <p>Duplicate picking is blocked until the active picker completes or hands off the order.</p>
                <a href='{operations_url}'>Back to Operations</a>
            </div>
        """)

    if order_status == "Orders Placed":
        c.execute(
            """
            UPDATE order_header
            SET status='Picking in Progress',
                responsibility='Operations'
            WHERE order_number=?
              AND status='Orders Placed'
            """,
            (order,),
        )

        if c.rowcount == 1:
            log_inventory_transaction(
                conn=conn,
                tx_code="PICK_START",
                order_number=order,
                sku="N/A",
                warehouse=clean_display_text(source_wh, SOURCE_WAREHOUSE),
                location="N/A",
                qty_change=0,
                qty_before=0,
                qty_after=0,
                user_role=active_picker,
                notes=f"Pick started by {active_picker}",
            )
            conn.commit()
            assigned_picker = active_picker
            pick_started_at = current_timestamp()
        else:
            conn.rollback()
            c.execute("SELECT status FROM order_header WHERE order_number=?", (order,))
            latest_row = c.fetchone()
            latest_status = latest_row[0] if latest_row else ""
            assigned_picker, pick_started_at = get_pick_start_snapshot(conn, order)
            if latest_status == "Picking in Progress" and assigned_picker and assigned_picker != active_picker:
                conn.close()
                return layout(f"""
                    <div class='card'>
                        <h2>Pick Locked</h2>
                        <p>Order <b>{order}</b> is already being picked by <b>{assigned_picker}</b>.</p>
                        <a href='{operations_url}'>Back to Operations</a>
                    </div>
                """)
    warehouse_filters = build_warehouse_filters(source_wh)

    # Select a single best pick location per SKU to avoid duplicate rows.
    c.execute("""
        WITH order_req AS (
            SELECT sku, SUM(quantity) AS req_qty
            FROM order_lines
            WHERE order_number = ?
            GROUP BY sku
        ),
        ranked_inventory AS (
            SELECT
                o.sku,
                o.req_qty,
                i.warehouse,
                i.location,
                i.quantity,
                ROW_NUMBER() OVER (
                    PARTITION BY o.sku
                    ORDER BY
                        CASE
                            WHEN i.warehouse = ? THEN 0
                            WHEN i.warehouse LIKE ? THEN 1
                            WHEN i.warehouse LIKE ? THEN 2
                            WHEN i.warehouse LIKE ? THEN 3
                            ELSE 4
                        END,
                        i.quantity DESC,
                        i.location ASC
                ) AS rn
            FROM order_req o
            JOIN inventory i ON o.sku = i.sku
        )
        SELECT sku, req_qty, warehouse, location, quantity
        FROM ranked_inventory
        WHERE rn = 1
        ORDER BY sku
    """, (order, *warehouse_filters))
    lines = c.fetchall()

    missing_inventory = False
    if not lines:
        c.execute("""
            SELECT sku, SUM(quantity)
            FROM order_lines
            WHERE order_number = ?
            GROUP BY sku
            ORDER BY sku
        """, (order,))
        lines = [(sku, req_qty, "N/A", "N/A", 0) for sku, req_qty in c.fetchall()]
        missing_inventory = True

    pick_readiness = get_order_pick_readiness(conn, order)
    readiness_summary = summarize_pick_readiness(pick_readiness)
    progress_pct = max(0, min(100, int(round(calculate_pick_progress(pick_readiness)))))
    display_lines = []
    for sku, req_qty, inv_wh, loc, on_hand in lines:
        required_qty = int(req_qty or 0)
        picked_qty = int(pick_readiness["picked_qty_lookup"].get(sku, 0) or 0)
        remaining_qty = max(required_qty - picked_qty, 0)
        on_hand_qty = int(on_hand or 0)
        display_lines.append(
            {
                "sku": sku,
                "required_qty": required_qty,
                "picked_qty": picked_qty,
                "remaining_qty": remaining_qty,
                "warehouse": inv_wh,
                "location": loc,
                "on_hand_qty": on_hand_qty,
            }
        )

    if request.method == "POST":
        return transact_pick(conn, order, source_wh, dest_wh, active_picker, display_lines, missing_inventory, operations_url)

    conn.close()

    content = """
    <div class="card">
        <h2>Pick Confirmation</h2>
    """

    if pick_message:
        content += f"""
        <div class='message-banner {pick_message_type}' style='margin-top:16px;'>
            <div>
                <b>Pick Guidance</b>
                <div>{pick_message}</div>
            </div>
        </div>
        """

    source_label = warehouse_code_label(source_wh)
    dest_label = warehouse_code_label(dest_wh)

    content += f"""
        <p><b>Route:</b> {source_label} &rarr; {dest_label}</p>
        <p><b>Picker:</b> {clean_display_text(assigned_picker, active_picker)}</p>
    """

    if pick_started_at:
        try:
            pick_started_label = parse_order_datetime(pick_started_at).strftime("%Y-%m-%d %H:%M PT")
        except ValueError:
            pick_started_label = clean_display_text(pick_started_at, "-")
        content += f"<p><b>Pick Started:</b> {pick_started_label}</p>"

    content += f"""
        <div class='sub-grid'>
            <div class='mini-panel'><div class='section-note'>Progress</div><div><b>{progress_pct}%</b></div></div>
            <div class='mini-panel'><div class='section-note'>Remaining Units</div><div><b>{readiness_summary['remaining_units']}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Open SKU Lines</div><div><b>{readiness_summary['remaining_lines']}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Next Step</div><div><b>{'Post remaining pick quantities' if not readiness_summary['ready_for_quality'] else 'Ready for quality verification'}</b></div></div>
        </div>
    """

    content += """
        <div class='ops-readiness-banner pending' style='margin-top:16px;'>
            <strong>Before posting this pick</strong>
            <div>Verify the exact source location shown for each SKU, enter only the quantity physically picked from that location, and do not post anything you did not remove from inventory. The system will block over-picks and mismatched location confirmations.</div>
        </div>
    """

    if missing_inventory:
        content += f"""
        <p style="color:#b45309;font-weight:600;">
            No mapped inventory locations were found for source warehouse ({source_wh}) and this order's SKU(s).
        </p>
        """

    content += f"""
        <form method="post">
        <input type='hidden' name='picker' value='{active_picker_attr}'>
        <p><b>Confirm Picked Quantities By SKU</b></p>
        <p class='section-note'>Type the exact source location for every SKU you are transacting, then enter the quantity you physically picked. Partial picks are allowed, but quantities cannot be negative or exceed the remaining requirement or on-hand balance.</p>
        <table>
            <tr>
                <th>SKU</th>
                <th>Warehouse</th>
                <th>Location</th>
                <th>Required Qty</th>
                <th>Picked To Date</th>
                <th>Remaining Qty</th>
                <th>Current On Hand</th>
            <th>Verify Location</th>
                <th>Pick Qty Now</th>
            </tr>
    """

    for idx, line in enumerate(display_lines, start=1):
        warehouse_label = warehouse_code_label(line["warehouse"], source_label)
        location_label = clean_display_text(line["location"], "Unassigned")
        on_hand_value = int(line["on_hand_qty"])
        remaining_qty = int(line["remaining_qty"])
        max_pick = min(remaining_qty, on_hand_value)
        if remaining_qty <= 0:
            location_field_html = f"<input type='hidden' name='location_check_{idx}' value='{location_label}'><span class='pill ok'>Verified</span>"
            pick_field_html = f"<input type='hidden' name='picked_qty_{idx}' value='0'><span class='pill ok'>Complete</span>"
        elif max_pick <= 0:
            location_field_html = f"<input type='hidden' name='location_check_{idx}' value=''><span class='pill issue'>Blocked</span>"
            pick_field_html = f"<span class='pill issue'>Blocked</span>"
        else:
            location_field_html = f"<input name='location_check_{idx}' value='' placeholder='Type {location_label}' autocomplete='off' required>"
            pick_field_html = f"<input name='picked_qty_{idx}' type='number' min='0' max='{max_pick}' value='0' required>"
        content += f"""
        <tr>
            <td>{line['sku']}</td>
            <td>{warehouse_label}</td>
            <td>{location_label}</td>
            <td>{line['required_qty']}</td>
            <td>{line['picked_qty']}</td>
            <td>{remaining_qty}</td>
            <td>{on_hand_value}</td>
            <td>{location_field_html}</td>
            <td>{pick_field_html}</td>
        </tr>
        """

    content += """
        </table>
        <br>
        <label style="display:flex;align-items:flex-start;gap:8px;font-weight:700;color:#0f172a;">
            <input type="checkbox" name="verification_ack" required>
            <span>I verified each source location and the quantity entered above before posting this pick transaction.</span>
        </label>
        <br>
        <button>Transact Pick</button>
        </form>
    </div>
    """

    return layout(content)

# ======================================================
# QUALITY
# ======================================================
@app.route("/quality")
def quality():
    audit_id_filter = request.args.get("audit_id", "").strip()
    audit_order_filter = request.args.get("audit_order", "").strip()
    audit_result_filter = request.args.get("audit_result", "").strip()
    remediated_count_raw = request.args.get("remediated_count", "").strip()
    remediated_sample = request.args.get("remediated_sample", "").strip()
    cleared_orders_raw = request.args.get("cleared_orders", "").strip()
    cleared_lines_raw = request.args.get("cleared_lines", "").strip()
    cleared_transactions_raw = request.args.get("cleared_transactions", "").strip()
    cleared_audits_raw = request.args.get("cleared_audits", "").strip()
    cleared_issues_raw = request.args.get("cleared_issues", "").strip()
    cleared_summary = request.args.get("cleared_summary", "").strip()

    try:
        remediated_count = int(remediated_count_raw) if remediated_count_raw else 0
    except ValueError:
        remediated_count = 0

    try:
        cleared_orders = int(cleared_orders_raw) if cleared_orders_raw else 0
    except ValueError:
        cleared_orders = 0

    try:
        cleared_lines = int(cleared_lines_raw) if cleared_lines_raw else 0
    except ValueError:
        cleared_lines = 0

    try:
        cleared_transactions = int(cleared_transactions_raw) if cleared_transactions_raw else 0
    except ValueError:
        cleared_transactions = 0

    try:
        cleared_audits = int(cleared_audits_raw) if cleared_audits_raw else 0
    except ValueError:
        cleared_audits = 0

    try:
        cleared_issues = int(cleared_issues_raw) if cleared_issues_raw else 0
    except ValueError:
        cleared_issues = 0

    conn = get_conn()
    ensure_quality_audit_schema(conn)
    c = conn.cursor()

    blocked_orders = enforce_quality_gate_on_pending_orders(conn)
    if blocked_orders:
        conn.commit()

    c.execute("SELECT order_number FROM order_header WHERE status='Pending Verification'")
    orders = c.fetchall()
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Pending Verification'")
    pending_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Quality Issue'")
    issue_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result='Passed'")
    passed_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result='Failed'")
    failed_count = c.fetchone()[0]

    audit_query = """
        SELECT audit_id, audit_time, order_number, result, part_match, damage, qty_match,
               discrepancy_type, inspector_notes, auditor_role
        FROM quality_audits
        WHERE 1=1
    """
    audit_params = []

    if audit_id_filter:
        audit_query += " AND audit_id LIKE ?"
        audit_params.append(f"%{audit_id_filter}%")
    if audit_order_filter:
        audit_query += " AND order_number LIKE ?"
        audit_params.append(f"%{audit_order_filter}%")
    if audit_result_filter:
        audit_query += " AND result = ?"
        audit_params.append(audit_result_filter)

    audit_query += " ORDER BY audit_time DESC LIMIT 500"
    c.execute(audit_query, audit_params)
    audit_rows = c.fetchall()

    conn.close()

    pass_rate = round((passed_count / (passed_count + failed_count)) * 100, 1) if (passed_count + failed_count) else 100

    content = f"""
    <div class='card'>
        <h2>Quality Control</h2>
        <p>Review pending verifications, monitor audit performance, and jump into escalation context quickly.</p>
        <div class='metrics-grid'>
            <div class='metric-tile tile-total'><div class='metric-label'>Pending Verifications</div><div class='metric-value'>{pending_count}</div></div>
            <div class='metric-tile tile-quality'><div class='metric-label'>Quality Issues</div><div class='metric-value'>{issue_count}</div></div>
            <div class='metric-tile tile-compliance'><div class='metric-label'>Audit Pass Rate</div><div class='metric-value'>{pass_rate}%</div></div>
        </div>
        <div class='quick-links'>
            <a class='quick-link' href='/supervisor?status=Quality+Issue'>Supervisor Escalations</a>
            <a class='quick-link' href='/inventory?tx_code=PICK'>Recent Pick Transactions</a>
            <form method='post' action='/quality/remediate_pending' style='display:inline;margin:0;'>
                <button type='submit'>Run Pending Verification Remediation</button>
            </form>
            <form method='post' action='/quality/clear_active_workload' style='display:inline;margin:0;'>
                <button type='submit' style='background:#b42318;'>Clear Active Workload</button>
            </form>
        </div>
    </div>
    """

    if remediated_count:
        content += f"""
        <div class='message-banner success'>
            <div>
                <b>Remediation Completed</b>
                <div>{remediated_count} order(s) were moved back to Picking in Progress. {remediated_sample}</div>
            </div>
        </div>
        """

    if cleared_summary:
        content += f"""
        <div class='message-banner success'>
            <div>
                <b>Active Workload Cleared</b>
                <div>{cleared_orders} order(s) removed. {cleared_summary}.</div>
                <div>{cleared_lines} line(s), {cleared_transactions} transaction(s), {cleared_audits} audit record(s), and {cleared_issues} supervisor issue(s) were deleted.</div>
            </div>
        </div>
        """

    if blocked_orders:
        blocked_sample = ", ".join(item["order_number"] for item in blocked_orders[:5])
        extra_count = max(0, len(blocked_orders) - 5)
        extra_suffix = f" and {extra_count} more" if extra_count else ""
        content += f"""
        <div class='message-banner warning'>
            <div>
                <b>Quality Gate Enforced</b>
                <div>{len(blocked_orders)} order(s) were moved back to Picking in Progress because a valid pick transaction was not found. Example: {blocked_sample}{extra_suffix}.</div>
            </div>
        </div>
        """

    content += """
    <div class='card'><h2>Quality Verification</h2><table><tr><th>Order</th><th>Action</th></tr>
    """

    for o in orders:
        content += f"<tr><td>{o[0]}</td><td class='table-actions'><a href='/verify/{o[0]}'>Audit</a><a href='/order/{o[0]}'>View Order</a></td></tr>"

    content += "</table></div>"

    content += f"""
    <div class='card'>
        <h3>Quality Audit History</h3>
        <p>Track every audit with a unique identifier for discrepancy investigations.</p>
        <form method='get' style='display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin:16px 0;'>
            <label style='display:grid;gap:6px;'>
                <span class='section-note'>Audit ID</span>
                <input type='text' name='audit_id' value='{audit_id_filter}' placeholder='Filter by audit ID'>
            </label>
            <label style='display:grid;gap:6px;'>
                <span class='section-note'>Order</span>
                <input type='text' name='audit_order' value='{audit_order_filter}' placeholder='Filter by order'>
            </label>
            <label style='display:grid;gap:6px;'>
                <span class='section-note'>Result</span>
                <select name='audit_result'>
                    <option value='' {'selected' if not audit_result_filter else ''}>All</option>
                    <option value='Passed' {'selected' if audit_result_filter == 'Passed' else ''}>Passed</option>
                    <option value='Failed' {'selected' if audit_result_filter == 'Failed' else ''}>Failed</option>
                </select>
            </label>
            <button type='submit'>Filter History</button>
            <a class='quick-link' href='/quality'>Clear</a>
        </form>
        <table>
            <tr>
                <th>Audit ID</th>
                <th>Audit Time</th>
                <th>Order</th>
                <th>Result</th>
                <th>Discrepancy</th>
                <th>Inspector Notes</th>
                <th>Auditor</th>
            </tr>
    """

    if audit_rows:
        for audit_id, audit_time, order_number, result, _part_match, _damage, _qty_match, discrepancy_type, inspector_notes, auditor_role in audit_rows:
            content += f"""
            <tr>
                <td>{audit_id}</td>
                <td>{format_pt_timestamp(audit_time)}</td>
                <td><a href='/order/{order_number}'>{order_number}</a></td>
                <td>{status_badge_html('Completed' if result == 'Passed' else 'Quality Issue')}</td>
                <td>{clean_display_text(discrepancy_type, '-')}</td>
                <td>{clean_display_text(inspector_notes, '-')}</td>
                <td>{clean_display_text(auditor_role, '-')}</td>
            </tr>
            """
    else:
        content += """
            <tr>
                <td colspan='7' style='text-align:center;color:#64748b;'>No audit history matched the selected filters.</td>
            </tr>
        """

    content += """
        </table>
    </div>
    """

    return layout(content)


@app.route("/quality/remediate_pending", methods=["POST"])
def quality_remediate_pending():
    conn = get_conn()
    ensure_quality_audit_schema(conn)

    blocked_orders = enforce_quality_gate_on_pending_orders(conn)
    if blocked_orders:
        conn.commit()
    conn.close()

    sample = ", ".join(item["order_number"] for item in blocked_orders[:5])
    extra_count = max(0, len(blocked_orders) - 5)
    if sample and extra_count:
        sample = f"Example: {sample} and {extra_count} more."
    elif sample:
        sample = f"Example: {sample}."
    else:
        sample = "No invalid Pending Verification orders were found."

    redirect_params = {
        "remediated_count": str(len(blocked_orders)),
        "remediated_sample": sample,
    }
    return redirect(f"/quality?{urlencode(redirect_params)}")


@app.route("/quality/clear_active_workload", methods=["POST"])
def quality_clear_active_workload():
    conn = get_conn()
    cleanup_summary = clear_active_workload(conn)
    conn.commit()
    conn.close()

    redirect_params = {
        "cleared_orders": str(cleanup_summary["orders_removed"]),
        "cleared_lines": str(cleanup_summary["lines_removed"]),
        "cleared_transactions": str(cleanup_summary["transactions_removed"]),
        "cleared_audits": str(cleanup_summary["audits_removed"]),
        "cleared_issues": str(cleanup_summary["issues_removed"]),
        "cleared_summary": cleanup_summary["status_summary"],
    }
    return redirect(f"/quality?{urlencode(redirect_params)}")


@app.route("/verify/<order>", methods=["GET", "POST"])
def verify(order):
    conn = get_conn()
    ensure_quality_audit_schema(conn)
    c = conn.cursor()

    c.execute(
        """
        SELECT status, responsibility
        FROM order_header
        WHERE order_number = ?
        """,
        (order,),
    )
    order_state = c.fetchone()
    if not order_state:
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Quality Audit - {order}</h2>
                <p>Order not found. Audit cannot be completed.</p>
                <a href='/quality'>Back to Quality</a>
            </div>
        """)

    order_status, _ = order_state
    if order_status != "Pending Verification":
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Quality Audit - {order}</h2>
                <p>This order is currently in <b>{order_status}</b>. Only orders in Pending Verification can be audited.</p>
                <a href='/quality'>Back to Quality</a>
            </div>
        """)

    pick_readiness = get_order_pick_readiness(conn, order)
    expected_lines = pick_readiness["expected_lines"]
    picked_qty_lookup = pick_readiness["picked_qty_lookup"]
    pick_gap_lines = pick_readiness["pick_gap_lines"]

    if not expected_lines:
        conn.close()
        return layout(f"""
            <div class='card'>
                <h2>Quality Audit - {order}</h2>
                <p>No order lines found for this order. Audit cannot be completed.</p>
                <a href='/quality'>Back to Quality</a>
            </div>
        """)

    if not pick_readiness["ready_for_quality"]:
        c.execute(
            """
            UPDATE order_header
            SET status='Picking in Progress',
                responsibility='Operations'
            WHERE order_number=?
              AND status='Pending Verification'
            """,
            (order,),
        )
        conn.commit()
        conn.close()

        reason_html = ""
        if pick_readiness["pick_tx_count"] == 0:
            reason_html += "<p><b>No pick transaction recorded for this order.</b></p>"

        if pick_gap_lines:
            reason_html += """
            <p>Recorded pick totals do not match planned quantities:</p>
            <table>
                <tr>
                    <th>SKU</th>
                    <th>Expected Qty</th>
                    <th>Picked Qty</th>
                    <th>Variance</th>
                </tr>
            """
            for sku, expected_qty, picked_qty in pick_gap_lines:
                variance = picked_qty - expected_qty
                reason_html += f"""
                <tr>
                    <td>{sku}</td>
                    <td>{expected_qty}</td>
                    <td>{picked_qty}</td>
                    <td style='color:{"#dc2626" if variance < 0 else "#b45309"};font-weight:700;'>{variance}</td>
                </tr>
                """
            reason_html += "</table>"

        return layout(f"""
            <div class='card'>
                <h2>Quality Audit - {order}</h2>
                <p>Audit is blocked because pick validation failed. The order was moved back to Picking in Progress.</p>
                {reason_html}
                <a href='/operations'>Back to Operations</a>
                <a href='/quality'>Back to Quality</a>
            </div>
        """)

    line_count = len(expected_lines)
    total_units = sum(int(qty) for _, qty in expected_lines)

    if request.method == "POST":
        damage = request.form.get("damage")
        if damage not in {"Yes", "No"}:
            damage = "No"

        discrepancy_type = request.form.get("discrepancy_type", "").strip()
        allowed_discrepancy_types = {
            "",
            "Pick Quantity Mismatch",
            "Incorrect Part Number",
            "Visible Damage",
            "Other Quality Issue",
        }
        if discrepancy_type not in allowed_discrepancy_types:
            discrepancy_type = ""

        inspector_notes = request.form.get("inspector_notes", "").strip()
        manual_fail = request.form.get("manual_fail") == "on"

        line_results = []
        line_diagnostics = []
        for idx, (exp_sku, exp_qty) in enumerate(expected_lines, start=1):
            scanned_sku = request.form.get(f"scanned_sku_{idx}", "").strip()
            scanned_qty_raw = request.form.get(f"scanned_qty_{idx}", "").strip()

            if not scanned_qty_raw.isdigit():
                conn.close()
                return layout(f"""
                    <div class='card'>
                        <h2>Quality Audit - {order}</h2>
                        <p>Row {idx}: scanned quantity must be numeric.</p>
                        <a href='/verify/{order}'>Back to Audit</a>
                    </div>
                """)

            scanned_qty = int(scanned_qty_raw)
            expected_sku = str(exp_sku).strip()
            expected_qty = int(exp_qty)

            sku_ok = scanned_sku.upper() == expected_sku.upper()
            qty_ok = scanned_qty == expected_qty
            line_results.append((sku_ok, qty_ok))
            line_diagnostics.append(
                {
                    "line_no": idx,
                    "expected_sku": expected_sku,
                    "expected_qty": expected_qty,
                    "scanned_sku": scanned_sku,
                    "scanned_qty": scanned_qty,
                }
            )

        part_match = "Yes" if all(sku_ok for sku_ok, _ in line_results) else "No"
        qty_match = "Yes" if all(qty_ok for _, qty_ok in line_results) else "No"
        audit_id = generate_audit_id(order)
        discrepancy_summary = summarize_discrepancy_lines(
            line_diagnostics=line_diagnostics,
            pick_gap_lines=pick_gap_lines,
            discrepancy_type=discrepancy_type,
            inspector_notes=inspector_notes,
        )

        issue_types = []
        if discrepancy_type:
            issue_types.append(discrepancy_type)
        if part_match == "No":
            issue_types.append("Incorrect Part Number")
        if qty_match == "No" and "Pick Quantity Mismatch" not in issue_types:
            issue_types.append("Pick Quantity Mismatch")
        if damage == "Yes" and "Visible Damage" not in issue_types:
            issue_types.append("Visible Damage")
        if manual_fail and not issue_types:
            issue_types.append("Other Quality Issue")
        issue_type = ", ".join(dict.fromkeys(issue_types)) if issue_types else build_issue_type(part_match, damage, qty_match)

        # If anything fails → escalate to supervisor
        if manual_fail or discrepancy_type or part_match == "No" or damage == "Yes" or qty_match == "No":
            audit_result = "Failed"
            c.execute("""
                UPDATE order_header
                SET status='Quality Issue',
                    responsibility='Supervisor'
                WHERE order_number=?
            """, (order,))
        else:
            audit_result = "Passed"
            c.execute("""
                UPDATE order_header
                SET status='Completed',
                    responsibility='Closed'
                WHERE order_number=?
            """, (order,))

        c.execute("""
            INSERT INTO quality_audits (
                audit_id,
                audit_time,
                order_number,
                part_match,
                damage,
                qty_match,
                result,
                auditor_role,
                discrepancy_type,
                inspector_notes,
                discrepancy_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            audit_id,
            datetime.now().isoformat(),
            order,
            part_match,
            damage,
            qty_match,
            audit_result,
            "Quality",
            discrepancy_type,
            inspector_notes,
            discrepancy_summary,
        ))

        if audit_result == "Failed":
            create_supervisor_issue(
                conn=conn,
                order_number=order,
                audit_id=audit_id,
                issue_date=datetime.now().isoformat(),
                issue_type=issue_type,
                part_snapshot_override=discrepancy_summary,
                inspector_notes=inspector_notes,
            )

        conn.commit()
        conn.close()
        return redirect("/quality")

    c.execute("""
        SELECT audit_id, audit_time, result, part_match, damage, qty_match, discrepancy_type, inspector_notes
        FROM quality_audits
        WHERE order_number = ?
        ORDER BY audit_time DESC
        LIMIT 20
    """, (order,))
    prior_audits = c.fetchall()

    conn.close()

    multi_item_badge = ""
    if line_count > 1:
        multi_item_badge = "<span style='padding:4px 8px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:700;'>Multi-item order</span>"

    content = f"""
    <div class="card">
        <h2>Quality Audit - {order}</h2>
        <p><b>{line_count} items in this order</b> | Total units: <b>{total_units}</b> {multi_item_badge}</p>
        <p>Scan and verify each order line before submitting the audit.</p>
    """

    if pick_gap_lines:
        content += """
        <div class='message-banner error'>
            <div>
                <b>Pick Discrepancy Detected</b>
                <div>The recorded pick transaction does not match the planned order quantities. Quality can see the variance before audit submission.</div>
            </div>
        </div>
        <table>
            <tr>
                <th>SKU</th>
                <th>Expected Qty</th>
                <th>Picked Qty</th>
                <th>Variance</th>
            </tr>
        """
        for sku, expected_qty, picked_qty in pick_gap_lines:
            variance = picked_qty - expected_qty
            content += f"""
            <tr>
                <td>{sku}</td>
                <td>{expected_qty}</td>
                <td>{picked_qty}</td>
                <td style='color:{"#dc2626" if variance < 0 else "#b45309"};font-weight:700;'>{variance}</td>
            </tr>
            """
        content += "</table>"

    content += """

        <h3>Expected Items</h3>
        <table>
            <tr>
                <th>Line</th>
                <th>Expected Part Number</th>
                <th>Expected Quantity</th>
                <th>Recorded Picked Quantity</th>
            </tr>
    """

    for idx, (sku, qty) in enumerate(expected_lines, start=1):
        picked_qty = picked_qty_lookup.get(sku, 0)
        content += f"""
        <tr>
            <td>{idx}</td>
            <td>{sku}</td>
            <td>{int(qty)}</td>
            <td>{picked_qty}</td>
        </tr>
        """

    content += """
        </table>

        <h3 style="margin-top:24px;">Scan To Verify</h3>
        <form method="post">
        <p><b>Check key items:</b> part number match, quantity match, and visible damage check.</p>
        <table>
            <tr>
                <th>Line</th>
                <th>Expected Part Number</th>
                <th>Expected Quantity</th>
                <th>Scanned Part Number</th>
                <th>Scanned Quantity</th>
            </tr>
    """

    for idx, (sku, qty) in enumerate(expected_lines, start=1):
        picked_qty = picked_qty_lookup.get(sku, 0)
        content += f"""
        <tr>
            <td>{idx}</td>
            <td>{sku}</td>
            <td>{int(qty)}</td>
            <td><input name="scanned_sku_{idx}" value="{sku}" required></td>
            <td><input name="scanned_qty_{idx}" type="number" min="0" value="{picked_qty}" required></td>
        </tr>
        """

    default_discrepancy_attr = "selected" if pick_gap_lines else ""

    content += f"""
        </table><br>

        Is There Visible Damage?
        <select name="damage">
            <option>No</option>
            <option>Yes</option>
        </select><br><br>

        Discrepancy Type
        <select name="discrepancy_type">
            <option value="">None</option>
            <option value="Pick Quantity Mismatch" {default_discrepancy_attr}>Pick Quantity Mismatch</option>
            <option value="Incorrect Part Number">Incorrect Part Number</option>
            <option value="Visible Damage">Visible Damage</option>
            <option value="Other Quality Issue">Other Quality Issue</option>
        </select><br><br>

        <label style="display:flex;align-items:center;gap:8px;font-weight:700;">
            <input type="checkbox" name="manual_fail">
            Manually mark audit as FAIL and escalate to Supervisor
        </label><br>

        Inspector Notes<br>
        <textarea name="inspector_notes" style="width:100%;min-height:110px;border:1px solid #cbd5e1;border-radius:10px;padding:10px;" placeholder="Document expected vs scanned quantity, wrong item, damage, or any other quality concern."></textarea><br><br>

        <p style="margin:8px 0 14px 0; color:#475569;">
            Pass / Fail is still calculated automatically from scanned part numbers, scanned quantities, and damage check, but Quality can now explicitly flag discrepancies and force escalation to Supervisor.
        </p>

        <button>Submit Audit (Pass / Fail)</button>
        </form>

        <h3 style="margin-top:24px;">Recent Audits For This Order</h3>
        <table>
            <tr>
                <th>Audit ID</th>
                <th>Time</th>
                <th>Result</th>
                <th>Part Match</th>
                <th>Damage</th>
                <th>Qty Match</th>
                <th>Discrepancy</th>
                <th>Inspector Notes</th>
            </tr>
    </div>
    """

    for audit_id, audit_time, result, part_match, damage, qty_match, discrepancy_type, inspector_notes in prior_audits:
        result_color = "#16a34a" if result == "Passed" else "#dc2626"
        content += f"""
        <tr>
            <td>{audit_id}</td>
            <td>{audit_time}</td>
            <td><span style='font-weight:700;color:{result_color};'>{result}</span></td>
            <td>{part_match}</td>
            <td>{damage}</td>
            <td>{qty_match}</td>
            <td>{discrepancy_type or 'None'}</td>
            <td>{inspector_notes or '—'}</td>
        </tr>
        """

    content += "</table></div>"

    return layout(content)


@app.route("/order/<order>")
def order_detail(order):
    context = request.args.get("context", "").strip().lower()
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT order_number, request_id, date, source, destination, urgency, status, responsibility
        FROM order_header
        WHERE order_number = ?
    """, (order,))
    header = c.fetchone()

    if not header:
        conn.close()
        return layout("""
            <div class='card'>
                <h2>Order Detail</h2>
                <p>Order not found.</p>
                <a href='/executive'>Back to Executive Dashboard</a>
            </div>
        """)

    c.execute("""
        SELECT sku, quantity
        FROM order_lines
        WHERE order_number = ?
        ORDER BY sku
    """, (order,))
    line_rows = c.fetchall()

    c.execute("""
        SELECT audit_id, audit_time, result, part_match, damage, qty_match
        FROM quality_audits
        WHERE order_number = ?
        ORDER BY audit_time DESC
        LIMIT 20
    """, (order,))
    audit_rows = c.fetchall()

    c.execute("""
        SELECT tx_time, tx_code, sku, warehouse, location, qty_change, qty_before, qty_after
        FROM inventory_transactions
        WHERE order_number = ?
        ORDER BY tx_time DESC
        LIMIT 20
    """, (order,))
    tx_rows = c.fetchall()
    conn.close()

    order_number, request_id, date_str, source, destination, urgency, status, responsibility = header
    urgency = normalize_urgency(urgency, "Standard")
    order_time = parse_order_datetime(date_str)
    sla_snapshot = build_order_sla_snapshot(order_time, urgency, status)
    sla_key = sla_snapshot["sla_key"]
    sla_label = sla_snapshot["sla_status"]
    sla_start = sla_snapshot["sla_start"]
    sla_deadline = sla_snapshot["sla_deadline"]
    sla_timer = sla_snapshot["sla_timer"]
    total_units = sum(int(quantity) for _, quantity in line_rows)
    if context == "operations":
        quick_links_html = """
            <a class='quick-link' href='/operations'>Operations</a>
            <a class='quick-link' href='/quality'>Quality</a>
            <a class='quick-link' href='/supervisor'>Supervisor</a>
        """
    else:
        quick_links_html = """
            <a class='quick-link' href='/planner'>Planner</a>
            <a class='quick-link' href='/operations'>Operations</a>
            <a class='quick-link' href='/quality'>Quality</a>
            <a class='quick-link' href='/supervisor'>Supervisor</a>
        """

    content = f"""
    <div class='card'>
        <h2>Order Detail - {order_number}</h2>
        <p>Centralized view for routing, line items, quality outcome, and inventory movement history.</p>
        <div class='quick-links'>
            {quick_links_html}
        </div>
        <div class='sub-grid'>
            <div class='mini-panel'><div class='section-note'>Request</div><div><b>{request_id}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Urgency</div><div><b>{urgency}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Status</div><div>{status_badge_html(status)}</div></div>
            <div class='mini-panel'><div class='section-note'>Owner</div><div>{responsibility_badge_html(responsibility)}</div></div>
            <div class='mini-panel'><div class='section-note'>SLA Health</div><div><b>{sla_label}</b></div></div>
            <div class='mini-panel'><div class='section-note'>SLA Start</div><div><b>{sla_start}</b></div></div>
            <div class='mini-panel'><div class='section-note'>SLA Deadline</div><div><b>{sla_deadline}</b></div></div>
            <div class='mini-panel'><div class='section-note'>SLA Timer</div><div><b>{sla_timer}</b></div></div>
            <div class='mini-panel'><div class='section-note'>SLA Risk</div><div>{sla_risk_badge_html(sla_key)}</div></div>
            <div class='mini-panel'><div class='section-note'>Route</div><div><b>{warehouse_code_label(source)} → {warehouse_code_label(destination)}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Order Time</div><div><b>{date_str}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Lines / Units</div><div><b>{len(line_rows)} line(s) / {total_units} units</b></div></div>
        </div>
    </div>
    <div class='card'>
        <h3>Order Lines</h3>
        <table>
            <tr><th>SKU</th><th>Quantity</th></tr>
    """

    for sku, quantity in line_rows:
        content += f"<tr><td>{sku}</td><td>{quantity}</td></tr>"

    content += """
        </table>
    </div>
    """

    if audit_rows:
        content += """
        <div class='card'>
            <h3>Audit History</h3>
            <table>
                <tr><th>Audit ID</th><th>Time</th><th>Result</th><th>Part Match</th><th>Damage</th><th>Qty Match</th></tr>
        """
        for audit_id, audit_time, result, part_match, damage, qty_match in audit_rows:
            badge = status_badge_html("Completed" if result == "Passed" else "Quality Issue")
            content += f"""
            <tr>
                <td>{audit_id}</td>
                <td>{audit_time}</td>
                <td>{badge}</td>
                <td>{part_match}</td>
                <td>{damage}</td>
                <td>{qty_match}</td>
            </tr>
            """
        content += """
            </table>
        </div>
        """

    if tx_rows:
        content += """
        <div class='card'>
            <h3>Inventory Transactions</h3>
            <table>
                <tr><th>Time</th><th>Code</th><th>SKU</th><th>Warehouse</th><th>Location</th><th>Qty Change</th><th>Before</th><th>After</th></tr>
        """
        for tx_time, tx_code, sku, warehouse, location, qty_change, qty_before, qty_after in tx_rows:
            content += f"""
            <tr>
                <td>{tx_time}</td>
                <td>{tx_code}</td>
                <td>{sku}</td>
                <td>{warehouse_code_label(warehouse)}</td>
                <td>{clean_display_text(location, 'Unassigned')}</td>
                <td>{qty_change}</td>
                <td>{qty_before}</td>
                <td>{qty_after}</td>
            </tr>
            """
        content += """
            </table>
        </div>
        """

    return layout(content)


@app.route("/issue_attachment/<path:filename>")
def issue_attachment(filename):
    os.makedirs(ISSUE_ATTACHMENT_DIR, exist_ok=True)
    return send_from_directory(ISSUE_ATTACHMENT_DIR, filename)


@app.route("/supervisor_issue/<issue_id>", methods=["GET", "POST"])
def supervisor_issue(issue_id):
    conn = get_conn()
    ensure_supervisor_issue_records(conn)
    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()

    if request.method == "POST":
        issue_status = request.form.get("issue_status", "Open")
        if issue_status not in ISSUE_WORKFLOW_STATES:
            issue_status = "Open"

        root_cause = request.form.get("root_cause", "")
        if root_cause and root_cause not in ISSUE_ROOT_CAUSES:
            root_cause = ""

        root_cause_notes = request.form.get("root_cause_notes", "").strip()
        corrective_action = request.form.get("corrective_action", "").strip()
        assigned_to = request.form.get("assigned_to", "").strip()
        resolution_notes = request.form.get("resolution_notes", "").strip()
        resolve_flag = 1 if request.form.get("resolved_flag") == "on" else 0

        attachment_name = None
        attachment_path = None
        uploaded_file = request.files.get("attachment")
        if uploaded_file and uploaded_file.filename:
            os.makedirs(ISSUE_ATTACHMENT_DIR, exist_ok=True)
            safe_name = secure_filename(uploaded_file.filename)
            stored_name = f"{issue_id}-{uuid4().hex[:8]}-{safe_name}"
            file_path = os.path.join(ISSUE_ATTACHMENT_DIR, stored_name)
            uploaded_file.save(file_path)
            attachment_name = safe_name
            attachment_path = stored_name

        resolved_at = datetime.now().isoformat() if resolve_flag or issue_status in {"Resolved", "Closed"} else None
        closed_at = datetime.now().isoformat() if issue_status == "Closed" else None

        update_fields = [
            "issue_status = ?",
            "root_cause = ?",
            "root_cause_notes = ?",
            "corrective_action = ?",
            "assigned_to = ?",
            "resolution_notes = ?",
            "resolved_flag = ?",
            "resolved_at = ?",
            "closed_at = ?",
            "last_updated_at = ?",
        ]
        update_values = [
            issue_status,
            root_cause,
            root_cause_notes,
            corrective_action,
            assigned_to,
            resolution_notes,
            resolve_flag,
            resolved_at,
            closed_at,
            datetime.now().isoformat(),
        ]

        if attachment_name and attachment_path:
            update_fields.extend(["attachment_name = ?", "attachment_path = ?"])
            update_values.extend([attachment_name, attachment_path])

        update_values.append(issue_id)
        c.execute(
            f"UPDATE supervisor_quality_issues SET {', '.join(update_fields)} WHERE issue_id = ?",
            update_values,
        )

        c.execute("SELECT order_number, issue_type FROM supervisor_quality_issues WHERE issue_id = ?", (issue_id,))
        row = c.fetchone()
        if row:
            order_number, issue_type_value = row
            if issue_type_value == SHORTAGE_ISSUE_TYPE:
                sync_shortage_order_workflow(conn)
            elif issue_status in {"Resolved", "Closed"}:
                c.execute("""
                    UPDATE order_header
                    SET status = 'Completed',
                        responsibility = 'Closed'
                    WHERE order_number = ?
                """, (order_number,))
            else:
                c.execute("""
                    UPDATE order_header
                    SET status = 'Quality Issue',
                        responsibility = 'Supervisor'
                    WHERE order_number = ?
                """, (order_number,))

        conn.commit()
        conn.close()
        return redirect(f"/supervisor_issue/{issue_id}")

    c.execute("""
        SELECT issue_id, order_number, audit_id, issue_date, picker_name, part_snapshot, issue_type,
               issue_status, root_cause, root_cause_notes, corrective_action, assigned_to,
               resolution_notes, attachment_name, attachment_path, resolved_flag, resolved_at, closed_at
        FROM supervisor_quality_issues
        WHERE issue_id = ?
    """, (issue_id,))
    issue_row = c.fetchone()

    if not issue_row:
        conn.close()
        return layout("""
            <div class='card'>
                <h2>Supervisor Quality Issue</h2>
                <p>Issue not found.</p>
                <a href='/supervisor'>Back to Supervisor</a>
            </div>
        """)

    (issue_id, order_number, audit_id, issue_date, picker_name, part_snapshot, issue_type,
     issue_status, root_cause, root_cause_notes, corrective_action, assigned_to,
     resolution_notes, attachment_name, attachment_path, resolved_flag, resolved_at, closed_at) = issue_row

    c.execute("""
        SELECT date, urgency, source, destination, status, responsibility
        FROM order_header
        WHERE order_number = ?
    """, (order_number,))
    order_header = c.fetchone()
    conn.close()

    date_str, urgency, source, destination, current_status, current_resp = order_header if order_header else ("", "", "", "", "", "")
    issue_heading = "Supervisor Shortage Escalation" if issue_type == SHORTAGE_ISSUE_TYPE else "Supervisor Quality Issue"
    back_link = "/supervisor?status=Blocked" if issue_type == SHORTAGE_ISSUE_TYPE else "/supervisor?status=Quality+Issue"
    back_label = "Back To Blocked Orders" if issue_type == SHORTAGE_ISSUE_TYPE else "Back To Quality Issues"
    attachment_html = "<p class='section-note'>No attachment uploaded.</p>"
    if attachment_name and attachment_path:
        attachment_html = f"<a class='quick-link' href='/issue_attachment/{attachment_path}' target='_blank'>Open Attachment: {attachment_name}</a>"

    root_cause_options = "".join(
        f"<option value='{cause}' {'selected' if root_cause == cause else ''}>{cause}</option>"
        for cause in ISSUE_ROOT_CAUSES
    )
    workflow_options = "".join(
        f"<option value='{state}' {'selected' if issue_status == state else ''}>{state}</option>"
        for state in ISSUE_WORKFLOW_STATES
    )

    content = f"""
    <div class='card'>
        <h2>{issue_heading} - {issue_id}</h2>
        <p>Investigate the issue, document root cause, assign corrective action, and move the case through the workflow.</p>
        <div class='quick-links'>
            <a class='quick-link' href='{back_link}'>{back_label}</a>
            <a class='quick-link' href='/order/{order_number}'>Open Order Detail</a>
        </div>
        <div class='sub-grid'>
            <div class='mini-panel'><div class='section-note'>Order Number</div><div><b>{order_number}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Picker</div><div><b>{picker_name}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Issue Date</div><div><b>{issue_date}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Issue Type</div><div><b>{issue_type}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Workflow Status</div><div>{workflow_badge_html(issue_status)}</div></div>
            <div class='mini-panel'><div class='section-note'>Current Owner</div><div>{responsibility_badge_html(current_resp or 'Supervisor')}</div></div>
            <div class='mini-panel'><div class='section-note'>Part Numbers</div><div><b>{part_snapshot}</b></div></div>
            <div class='mini-panel'><div class='section-note'>Route</div><div><b>{warehouse_code_label(source)} → {warehouse_code_label(destination)}</b></div></div>
        </div>
    </div>

    <div class='card'>
        <h3>Issue Workflow</h3>
        <form method='post' enctype='multipart/form-data'>
            <div class='sub-grid'>
                <div class='mini-panel'>
                    <div class='section-note'>Workflow Status</div>
                    <select name='issue_status'>
                        {workflow_options}
                    </select>
                    <div style='margin-top:12px;'>
                        <label><input type='checkbox' name='resolved_flag' {'checked' if resolved_flag else ''}> Issue Resolved</label>
                    </div>
                </div>
                <div class='mini-panel'>
                    <div class='section-note'>Assign Responsibility</div>
                    <input name='assigned_to' value='{assigned_to or ''}' placeholder='Supervisor, Operations Lead, Inventory Control'>
                    <div class='section-note' style='margin-top:10px;'>Audit Reference: {audit_id}</div>
                    <div class='section-note'>Order Date: {date_str}</div>
                </div>
            </div>

            <h3 style='margin-top:24px;'>Root Cause Analysis</h3>
            <div class='sub-grid'>
                <div class='mini-panel'>
                    <div class='section-note'>Common Cause</div>
                    <select name='root_cause'>
                        <option value=''>Select Root Cause</option>
                        {root_cause_options}
                    </select>
                </div>
                <div class='mini-panel'>
                    <div class='section-note'>Inspector / RCA Notes</div>
                    <textarea name='root_cause_notes' style='width:100%;min-height:120px;border:1px solid #cbd5e1;border-radius:10px;padding:10px;'>{root_cause_notes or ''}</textarea>
                </div>
            </div>

            <h3 style='margin-top:24px;'>Corrective Action</h3>
            <div class='sub-grid'>
                <div class='mini-panel'>
                    <div class='section-note'>Corrective Action</div>
                    <textarea name='corrective_action' style='width:100%;min-height:120px;border:1px solid #cbd5e1;border-radius:10px;padding:10px;'>{corrective_action or ''}</textarea>
                </div>
                <div class='mini-panel'>
                    <div class='section-note'>Resolution Notes</div>
                    <textarea name='resolution_notes' style='width:100%;min-height:120px;border:1px solid #cbd5e1;border-radius:10px;padding:10px;'>{resolution_notes or ''}</textarea>
                    <div class='section-note' style='margin-top:10px;'>Resolved At: {resolved_at or 'Not resolved yet'}</div>
                    <div class='section-note'>Closed At: {closed_at or 'Not closed yet'}</div>
                </div>
            </div>

            <h3 style='margin-top:24px;'>Attachments</h3>
            <div class='mini-panel'>
                <input type='file' name='attachment'>
                <div style='margin-top:12px;'>{attachment_html}</div>
            </div>

            <br>
            <button type='submit'>Save Supervisor Issue</button>
        </form>
    </div>
    """

    return layout(content)
# ======================================================
# SUPERVISOR
# ======================================================
# ======================================================
# SUPERVISOR WITH SLA VISIBILITY
# ======================================================
@app.route("/supervisor")
def supervisor():
    urgency_filter_raw = request.args.get("urgency", "")
    urgency_filter = normalize_urgency(urgency_filter_raw, "") if urgency_filter_raw else ""
    status_filter = request.args.get("status", "")
    resp_filter = request.args.get("resp", "")
    sla_filter = request.args.get("sla", "")

    conn = get_conn()
    ensure_supervisor_issue_records(conn)
    shortage_sync = sync_shortage_order_workflow(conn)
    conn.commit()
    c = conn.cursor()

    query = """
        SELECT order_number, urgency, status, responsibility, date, source, destination
        FROM order_header
        WHERE 1=1
    """
    params = []

    if urgency_filter:
        query += " AND urgency=?"
        params.append(urgency_filter)

    if status_filter:
        query += " AND status=?"
        params.append(status_filter)

    if resp_filter:
        query += " AND responsibility=?"
        params.append(resp_filter)

    query += " ORDER BY date DESC"
    c.execute(query, params)
    raw_orders = c.fetchall()

    c.execute("""
        SELECT issue_id, order_number, issue_date, picker_name, part_snapshot, issue_type,
               issue_status, root_cause, assigned_to, attachment_name
        FROM supervisor_quality_issues
        ORDER BY issue_date DESC
        LIMIT 100
    """)
    issue_rows = c.fetchall()
    shortage_alerts = shortage_sync["alerts"]
    conn.close()

    now = now_pt()
    orders = []
    status_counts = {}
    responsibility_counts = {}
    urgency_counts = {level: 0 for level in URGENCY_ORDER}
    healthy = 0
    at_risk = 0
    breached = 0
    open_orders = 0
    quality_issues = 0
    blocked_shortages = len(shortage_alerts)
    issue_state_counts = {state: 0 for state in ISSUE_WORKFLOW_STATES}
    root_cause_counts = {}

    for _, _, _, _, _, _, issue_status, root_cause, _, _ in issue_rows:
        if issue_status in issue_state_counts:
            issue_state_counts[issue_status] += 1
        if root_cause:
            root_cause_counts[root_cause] = root_cause_counts.get(root_cause, 0) + 1

    for order_number, urgency, status, responsibility, date_str, source, destination in raw_orders:
        urgency = normalize_urgency(urgency, "Standard")
        order_time = parse_order_datetime(date_str)
        age_minutes = round((now - order_time).total_seconds() / 60, 1)

        sla_snapshot = build_order_sla_snapshot(order_time, urgency, status, now)
        sla_key = sla_snapshot["sla_key"]
        sla_status = sla_snapshot["sla_status"]
        sla_start = sla_snapshot["sla_start"]
        sla_deadline_dt = sla_snapshot["sla_deadline_dt"]
        sla_deadline = sla_snapshot["sla_deadline"]
        sla_timer = sla_snapshot["sla_timer"]
        sla_remaining_seconds = sla_snapshot["sla_remaining_seconds"]

        if sla_filter and sla_key != sla_filter:
            continue

        if status != "Completed":
            open_orders += 1
            if sla_key == "healthy":
                healthy += 1
            elif sla_key == "at_risk":
                at_risk += 1
            else:
                breached += 1

        if status == "Quality Issue":
            quality_issues += 1

        status_counts[status] = status_counts.get(status, 0) + 1
        responsibility_counts[responsibility] = responsibility_counts.get(responsibility, 0) + 1
        if urgency in urgency_counts:
            urgency_counts[urgency] += 1

        orders.append({
            "order_number": order_number,
            "urgency": urgency,
            "status": status,
            "responsibility": responsibility,
            "date_str": date_str,
            "source": source,
            "destination": destination,
            "sla_status": sla_status,
            "sla_key": sla_key,
            "sla_start": sla_start,
            "sla_deadline": sla_deadline,
            "sla_timer": sla_timer,
            "sla_remaining_seconds": sla_remaining_seconds,
            "age_minutes": age_minutes,
        })

    total_orders = len(orders)
    healthy_pct = round((healthy / open_orders) * 100, 1) if open_orders else 0
    risk_pct = round((at_risk / open_orders) * 100, 1) if open_orders else 0
    breached_pct = round((breached / open_orders) * 100, 1) if open_orders else 0

    attention_orders = [
        order for order in orders
        if (
            order["status"] == BLOCKED_ORDER_STATUS
            or order["responsibility"] == "Inventory"
            or order["status"] == "Quality Issue"
            or order["sla_key"] in {"at_risk", "breached"}
            or (order["urgency"] == "Critical" and order["status"] != "Completed")
        )
    ]
    attention_orders.sort(
        key=lambda order: (
            0 if order["sla_key"] == "breached" else 1,
            0 if order["urgency"] == "Critical" else 1,
            order["sla_remaining_seconds"],
        )
    )
    attention_orders = attention_orders[:8]

    filtered_order_rows = [order for order in orders if order["status"] != "Completed"]
    filtered_order_rows.sort(key=lambda order: order["sla_remaining_seconds"])

    urgency_html = "".join(
        f"<div style='padding:12px 14px;border-radius:10px;background:#eff6ff;border:1px solid #bfdbfe;'>"
        f"<div style='font-size:12px;color:#475569;'>{label} Priority</div>"
        f"<div style='font-size:24px;font-weight:700;color:#0f172a;margin-top:4px;'>{count}</div>"
        f"</div>"
        for label, count in urgency_counts.items()
    )

    workflow_html = "".join(
        f"<div style='padding:12px 14px;border-radius:10px;background:#fff7ed;border:1px solid #fed7aa;'>"
        f"<div style='font-size:12px;color:#7c2d12;'>{state}</div>"
        f"<div style='font-size:24px;font-weight:700;color:#0f172a;margin-top:4px;'>{count}</div>"
        f"</div>"
        for state, count in issue_state_counts.items()
    )

    root_cause_html = "<p class='section-note'>No root causes assigned yet.</p>"
    if root_cause_counts:
        root_cause_html = "<table><tr><th>Root Cause</th><th>Count</th></tr>"
        for cause, count in sorted(root_cause_counts.items(), key=lambda item: item[1], reverse=True):
            root_cause_html += f"<tr><td>{cause}</td><td>{count}</td></tr>"
        root_cause_html += "</table>"

    blocked_shortage_html = "<div class='empty-state'>No blocked shortage orders are currently waiting on supervisor monitoring.</div>"
    if shortage_alerts:
        blocked_shortage_html = """
        <div style='overflow-x:auto;overflow-y:auto;max-height:420px;'>
        <table>
            <tr>
                <th>Order</th>
                <th>SKU</th>
                <th>Blocked Since</th>
                <th>Shortage</th>
                <th>SLA Risk</th>
                <th>Escalation</th>
                <th>Action</th>
            </tr>
        """
        for alert in shortage_alerts[:12]:
            order_state = next((order for order in orders if order["order_number"] == alert["order_number"]), None)
            sla_badge = sla_risk_badge_html(order_state["sla_key"] if order_state else "healthy")
            escalation_action = (
                f"<a href='/supervisor_issue/{alert['issue_id']}'>Open Issue</a>"
                if alert["issue_id"]
                else "<span class='section-note'>Awaiting supervisor notification</span>"
            )
            blocked_shortage_html += f"""
            <tr>
                <td><a href='/order/{alert['order_number']}'>{alert['order_number']}</a></td>
                <td>{alert['sku']}</td>
                <td>{alert['blocked_at'] or alert['order_date']}</td>
                <td>Need {alert['remaining_qty']} / On hand {alert['on_hand_qty']}</td>
                <td>{sla_badge}</td>
                <td>{alert['issue_status']}</td>
                <td class='table-actions'>{escalation_action}<a href='{alert['inventory_action_url']}'>Inventory View</a></td>
            </tr>
            """
        blocked_shortage_html += "</table></div>"

    supervisor_view = request.args.get("view", "overview").strip().lower()
    if supervisor_view not in {"overview", "quality", "command"}:
        supervisor_view = "overview"

    sup_quality_exc_count = status_counts.get("Quality Issue", 0)
    sup_pipeline_html = ""
    for _status, label, count, bg, fg in [
        ("Orders Placed",        "Pick Queue",   status_counts.get("Orders Placed", 0),        "#dbeafe", "#1d4ed8"),
        ("Picking in Progress",  "Picking",      status_counts.get("Picking in Progress", 0),  "#ede9fe", "#6d28d9"),
        (BLOCKED_ORDER_STATUS,    "Blocked",      status_counts.get(BLOCKED_ORDER_STATUS, 0),    "#fee2e2", "#b91c1c"),
        ("Pending Verification", "Verification", status_counts.get("Pending Verification", 0), "#fef3c7", "#b45309"),
        ("Completed",            "Shipped",      status_counts.get("Completed", 0),             "#dcfce7", "#166534"),
    ]:
        sup_pipeline_html += (
            f"<div class='pipeline-stage'><div class='pipeline-node' style='background:{bg};border-color:{fg}33;'>"
            f"<div class='pipeline-count' style='color:{fg};'>{count}</div>"
            f"<div class='pipeline-label' style='color:{fg};'>{label}</div>"
            f"<div class='pipeline-sublabel' style='color:{fg};'>{_status}</div>"
            "</div></div>"
        )

    content = f"""
    <div class="card">
        <h2>Supervisor Control Tower</h2>
        <p>Monitor workload, SLA exposure, quality escalations, and current ownership across the order pipeline.</p>

        <div class='exec-section-switcher'>
            <button type='button' class='exec-switch-btn {"active" if supervisor_view == "overview" else ""}' data-target='overview'>Overview</button>
            <button type='button' class='exec-switch-btn {"active" if supervisor_view == "quality" else ""}' data-target='quality'>Quality Issues</button>
            <button type='button' class='exec-switch-btn {"active" if supervisor_view == "command" else ""}' data-target='command'>Order Command</button>
        </div>
        <p class='section-note' style='margin:4px 0 14px 0;'>Use the section selector to jump directly to the working view without long-page scrolling.</p>

        <form method="get" style="margin-bottom:20px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;">
            <input type="hidden" name="view" id="supervisor-view-input" value="{supervisor_view}">
            <div>
                <div style="font-size:12px;color:#475569;margin-bottom:4px;">Urgency</div>
                <select name="urgency">
                    <option value="" {"selected" if not urgency_filter else ""}>All</option>
                    <option value="Critical" {"selected" if urgency_filter == "Critical" else ""}>Critical</option>
                    <option value="Urgent" {"selected" if urgency_filter == "Urgent" else ""}>Urgent</option>
                    <option value="Standard" {"selected" if urgency_filter == "Standard" else ""}>Standard</option>
                </select>
            </div>

            <div>
                <div style="font-size:12px;color:#475569;margin-bottom:4px;">Status</div>
                <select name="status">
                    <option value="" {"selected" if not status_filter else ""}>All</option>
                    <option value="Orders Placed" {"selected" if status_filter == "Orders Placed" else ""}>Orders Placed</option>
                    <option value="Picking in Progress" {"selected" if status_filter == "Picking in Progress" else ""}>Picking in Progress</option>
                    <option value="Blocked" {"selected" if status_filter == "Blocked" else ""}>Blocked</option>
                    <option value="Pending Verification" {"selected" if status_filter == "Pending Verification" else ""}>Pending Verification</option>
                    <option value="Completed" {"selected" if status_filter == "Completed" else ""}>Completed</option>
                    <option value="Quality Issue" {"selected" if status_filter == "Quality Issue" else ""}>Quality Issue</option>
                </select>
            </div>

            <div>
                <div style="font-size:12px;color:#475569;margin-bottom:4px;">Responsibility</div>
                <select name="resp">
                    <option value="" {"selected" if not resp_filter else ""}>All</option>
                    <option value="Operations" {"selected" if resp_filter == "Operations" else ""}>Operations</option>
                    <option value="Inventory" {"selected" if resp_filter == "Inventory" else ""}>Inventory</option>
                    <option value="Quality" {"selected" if resp_filter == "Quality" else ""}>Quality</option>
                    <option value="Supervisor" {"selected" if resp_filter == "Supervisor" else ""}>Supervisor</option>
                    <option value="Closed" {"selected" if resp_filter == "Closed" else ""}>Closed</option>
                </select>
            </div>

            <div>
                <div style="font-size:12px;color:#475569;margin-bottom:4px;">SLA Health</div>
                <select name="sla">
                    <option value="" {"selected" if not sla_filter else ""}>All</option>
                    <option value="healthy" {"selected" if sla_filter == "healthy" else ""}>Healthy</option>
                    <option value="at_risk" {"selected" if sla_filter == "at_risk" else ""}>At Risk</option>
                    <option value="breached" {"selected" if sla_filter == "breached" else ""}>Breached</option>
                </select>
            </div>

            <div>
                <button type="submit">Apply Filters</button>
            </div>
        </form>

        <section class='exec-view-section {"active" if supervisor_view == "overview" else ""}' data-section='overview'>

        <div class="metrics-grid">
            <div class="metric-tile tile-total">
                <div class="metric-label">Orders In View</div>
                <div class="metric-value">{total_orders}</div>
            </div>
            <div class="metric-tile tile-compliance">
                <div class="metric-label">Open Healthy Orders</div>
                <div class="metric-value">{healthy}</div>
            </div>
            <div class="metric-tile tile-breached">
                <div class="metric-label">Breached Orders</div>
                <div class="metric-value">{breached}</div>
            </div>
            <div class="metric-tile tile-breached">
                <div class="metric-label">Blocked Shortages</div>
                <div class="metric-value">{blocked_shortages}</div>
            </div>
            <div class="metric-tile tile-quality">
                <div class="metric-label">Quality Escalations</div>
                <div class="metric-value">{quality_issues}</div>
            </div>
        </div>

        <div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin:16px 0 20px 0;'>
            <div class='exec-section-label'>&#9654; Order Pipeline</div>
            <div class='pipeline-wrap'>
                {sup_pipeline_html}
            </div>
            {"<div class='pipeline-exception'><span style='font-size:22px;'>&#9888;</span><div><div style='font-size:14px;font-weight:700;color:#b91c1c;'>Quality Exception: " + str(sup_quality_exc_count) + " Order(s) on Hold</div><div style='font-size:13px;color:#7c3aed;'>Review required before shipping can proceed</div></div><a href='/quality' style='margin-left:auto;padding:8px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:12px;font-weight:700;font-size:13px;color:#b91c1c;text-decoration:none;'>Go to Quality &#8250;</a></div>" if sup_quality_exc_count > 0 else ""}
        </div>

        <div class='card' style='margin-bottom:20px;'>
            <div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;'>
                <div>
                    <h3>Blocked Orders / Shortages</h3>
                    <p class='section-note'>Monitor blocked picks, confirm escalation coverage, and route Inventory resolution before SLA risk becomes a breach.</p>
                </div>
                <a class='quick-link' href='/supervisor?status=Blocked&view=overview'>Filter Blocked Orders</a>
            </div>
            {blocked_shortage_html}
        </div>

        <div class="split-grid" style="align-items:start;">
            <div>
                <h3>SLA Health Snapshot</h3>
                <p><b>Open Orders:</b> {open_orders} | <b>At Risk:</b> {at_risk}</p>
                <div class="sla-bar-wrap">
                    <div class="sla-label">Healthy ({healthy_pct}%)</div>
                    <div class="sla-bar"><div class="sla-fill" style="width:{healthy_pct}%;background:#16a34a;"></div></div>
                </div>
                <div class="sla-bar-wrap">
                    <div class="sla-label">At Risk ({risk_pct}%)</div>
                    <div class="sla-bar"><div class="sla-fill" style="width:{risk_pct}%;background:#f59e0b;"></div></div>
                </div>
                <div class="sla-bar-wrap">
                    <div class="sla-label">Breached ({breached_pct}%)</div>
                    <div class="sla-bar"><div class="sla-fill" style="width:{breached_pct}%;background:#dc2626;"></div></div>
                </div>

                <h3 style="margin-top:24px;">Urgency Mix</h3>
                <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));gap:10px;">
                    {urgency_html}
                </div>
            </div>

            <div>
                <h3>Supervisor Attention Queue</h3>
    """

    if attention_orders:
        content += """
                <div style='overflow-x:auto;overflow-y:auto;max-height:480px;'>
                <table>
                    <tr>
                        <th>Order</th>
                        <th>Status</th>
                        <th>SLA</th>
                        <th>Deadline</th>
                        <th>Timer</th>
                        <th>Risk</th>
                        <th>Owner</th>
                        <th>Action</th>
                    </tr>
        """
        for order in attention_orders:
            attention_action = f"<a href='/order/{order['order_number']}'>View</a>"
            if order['status'] == 'Quality Issue':
                matching_issue_id = next((issue_id for issue_id, issue_order, *_ in issue_rows if issue_order == order['order_number']), None)
                if matching_issue_id:
                    attention_action += f"<a href='/supervisor_issue/{matching_issue_id}'>Open Issue</a>"
            content += f"""
                    <tr>
                        <td class='nowrap-cell'>{order['order_number']}</td>
                        <td>{status_badge_html(order['status'])}</td>
                        <td>{order['sla_status']}</td>
                        <td class='nowrap-cell'>{order['sla_deadline']}</td>
                        <td class='nowrap-cell'>{order['sla_timer']}</td>
                        <td>{sla_risk_badge_html(order['sla_key'])}</td>
                        <td>{responsibility_badge_html(order['responsibility'])}</td>
                        <td class='table-actions'>{attention_action}</td>
                    </tr>
            """
        content += """
                </table>
                </div>
        """
    else:
        content += "<p>No critical supervisor exceptions in the current filtered view.</p>"

    content += """
            </div>
        </div>
        <details style='margin-top:22px;'>
            <summary style='cursor:pointer;list-style:none;padding:12px 16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;font-weight:700;color:#0f172a;font-size:14px;display:flex;align-items:center;gap:8px;'>
                <span>&#9654; Filtered Open Orders</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div style='margin-top:8px;'>
            <p class='section-note'>Filters always return matching order rows below so supervisors can act immediately.</p>
    """

    if filtered_order_rows:
        content += """
            <div style='overflow-x:auto;overflow-y:auto;max-height:480px;'>
            <table>
                <tr>
                    <th>Order ID</th>
                    <th>Priority</th>
                    <th>Status</th>
                    <th>SLA Start</th>
                    <th>SLA Deadline</th>
                    <th>SLA Remaining</th>
                    <th>Risk</th>
                </tr>
        """
        for order in filtered_order_rows:
            content += f"""
                <tr>
                    <td><a href='/order/{order['order_number']}'>{order['order_number']}</a></td>
                    <td>{order['urgency']}</td>
                    <td>{status_badge_html(order['status'])}</td>
                    <td>{order['sla_start']}</td>
                    <td>{order['sla_deadline']}</td>
                    <td>{order['sla_timer']}</td>
                    <td>{sla_risk_badge_html(order['sla_key'])}</td>
                </tr>
            """
        content += "</table></div>"
    else:
        content += "<p>No open orders match the current filters.</p>"

    content += """
            </div>
        </details>
        </div>
    </div>
    </section>
    """

    content += f"""
    <section class='exec-view-section {"active" if supervisor_view == "quality" else ""}' data-section='quality'>
    <div class='card'>
        <h3>Supervisor Escalation Module</h3>
        <p>Investigate active shortages and quality issues, assign root cause, and track recurring patterns over time.</p>
        <div class='sub-grid'>
            <div class='mini-panel'>
                <h3>Issue Workflow</h3>
                <div style='display:grid;grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));gap:10px;'>
                    {workflow_html}
                </div>
            </div>
            <div class='mini-panel'>
                <h3>Recurring Root Causes</h3>
                {root_cause_html}
            </div>
        </div>
        <div class='quick-links'>
            <a class='quick-link' href='/quality'>Open Quality Queue</a>
            <a class='quick-link' href='/supervisor?view=quality&status=Quality+Issue'>Filter Quality Issues</a>
        </div>
    </div>
    <div class='card'>
        <h3>Open Supervisor Escalations</h3>
        <div style='overflow-x:auto;overflow-y:auto;max-height:480px;'>
        <table>
            <tr>
                <th>Issue</th>
                <th>Order</th>
                <th>Date</th>
                <th>Picker</th>
                <th>Issue Type</th>
                <th>Workflow</th>
                <th>Root Cause</th>
                <th>Assigned To</th>
                <th>Attachment</th>
                <th>Action</th>
            </tr>
    """

    if issue_rows:
        for issue_id, order_number, issue_date, picker_name, part_snapshot, issue_type, issue_status, root_cause, assigned_to, attachment_name in issue_rows:
            attachment_label = attachment_name if attachment_name else "No"
            content += f"""
            <tr>
                <td>{issue_id}</td>
                <td>{order_number}<br><span class='section-note'>{part_snapshot}</span></td>
                <td>{issue_date}</td>
                <td>{picker_name}</td>
                <td>{issue_type}</td>
                <td>{workflow_badge_html(issue_status)}</td>
                <td>{root_cause or 'Not assigned'}</td>
                <td>{assigned_to or 'Unassigned'}</td>
                <td>{attachment_label}</td>
                <td class='table-actions'><a href='/supervisor_issue/{issue_id}'>Open Issue</a></td>
            </tr>
            """
    else:
        content += "<tr><td colspan='10'>No supervisor escalations have been created yet.</td></tr>"

    content += "</table></div></div></section>"

    content += f"""
    <section class='exec-view-section {"active" if supervisor_view == "command" else ""}' data-section='command'>
    <div class="card">
        <h3>Order Command View</h3>
        <p>Detailed order list with SLA health, current owner, and routing context.</p>
        <div style='overflow-x:auto;overflow-y:auto;max-height:560px;'>
        <table>
            <tr>
                <th>Order</th>
                <th>Urgency</th>
                <th>Status</th>
                <th>Responsibility</th>
                <th>SLA Status</th>
                <th>SLA Start</th>
                <th>SLA Deadline</th>
                <th>SLA Timer</th>
                <th>Risk</th>
                <th>Age (min)</th>
                <th>Route</th>
                <th>Action</th>
            </tr>
    """

    for order in orders:
        route_text = f"{warehouse_code_label(order['source'])} → {warehouse_code_label(order['destination'])}"
        action_html = f"<a href='/order/{order['order_number']}'>View</a>"
        if order['status'] == 'Quality Issue':
            matching_issue_id = next((issue_id for issue_id, issue_order, *_ in issue_rows if issue_order == order['order_number']), None)
            if matching_issue_id:
                action_html += f"<a href='/supervisor_issue/{matching_issue_id}'>Open Issue</a>"
        content += f"""
        <tr>
            <td>{order['order_number']}</td>
            <td>{order['urgency']}</td>
            <td>{status_badge_html(order['status'])}</td>
            <td>{responsibility_badge_html(order['responsibility'])}</td>
            <td>{order['sla_status']}</td>
            <td>{order['sla_start']}</td>
            <td>{order['sla_deadline']}</td>
            <td>{order['sla_timer']}</td>
            <td>{sla_risk_badge_html(order['sla_key'])}</td>
            <td>{order['age_minutes']}</td>
            <td>{route_text}</td>
            <td class='table-actions'>{action_html}</td>
        </tr>
        """

    content += f"""
        </table>
        </div>
    </div>
    </section>

    <script>
        (function () {{
            function setSupervisorView(viewName) {{
                const sections = document.querySelectorAll(".exec-view-section[data-section]");
                const buttons = document.querySelectorAll('.exec-switch-btn');
                const allowed = new Set(['overview', 'quality', 'command']);
                if (!allowed.has(viewName)) {{
                    return;
                }}

                sections.forEach((section) => {{
                    const isActive = section.getAttribute('data-section') === viewName;
                    section.classList.toggle('active', isActive);
                }});

                buttons.forEach((button) => {{
                    const isActive = button.getAttribute('data-target') === viewName;
                    button.classList.toggle('active', isActive);
                }});

                const viewInput = document.getElementById('supervisor-view-input');
                if (viewInput) {{
                    viewInput.value = viewName;
                }}

                const url = new URL(window.location.href);
                url.searchParams.set('view', viewName);
                window.history.replaceState(null, '', url.toString());
                window.scrollTo({{ top: 0, behavior: 'smooth' }});
            }}

            document.querySelectorAll('.exec-switch-btn').forEach((button) => {{
                button.addEventListener('click', function () {{
                    setSupervisorView(this.getAttribute('data-target'));
                }});
            }});

            setSupervisorView('{supervisor_view}');
        }})();
    </script>
    """
    return layout(content)


# ======================================================
# EXECUTIVE DASHBOARD
# ======================================================
@app.route("/executive")
def executive_dashboard():
    conn = get_conn()
    c = conn.cursor()
    now = now_pt()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    # ── KPI 1: Orders received today ──────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM order_header WHERE date >= ?", (today_start,))
    orders_today = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM order_header")
    orders_all_time = c.fetchone()[0]

    # ── KPI 2: Shipped today / Completed ──────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Completed' AND date >= ?", (today_start,))
    shipped_today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM order_header WHERE status='Completed'")
    shipped_total = c.fetchone()[0]

    # ── KPI 3: Orders pending ─────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM order_header WHERE status != 'Completed'")
    orders_pending = c.fetchone()[0]

    # ── KPI 4: Quality audit pass rate (passed audits / total audits) ─────────
    c.execute("SELECT COUNT(*) FROM quality_audits")
    total_audits = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result IN ('Pass', 'Passed')")
    passed_audits = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM quality_audits WHERE result='Failed'")
    failed_audits = c.fetchone()[0]
    pick_accuracy = round((passed_audits / total_audits) * 100, 1) if total_audits > 0 else 100.0

    # ── KPI 5: Inventory accuracy (SKUs with valid warehouse assignment) ───────
    c.execute("""
        SELECT COUNT(DISTINCT sku) FROM inventory
        WHERE quantity > 0
          AND TRIM(COALESCE(warehouse,'')) <> ''
          AND LOWER(TRIM(COALESCE(warehouse,''))) <> 'nan'
          AND TRIM(COALESCE(location,'')) <> ''
          AND LOWER(TRIM(COALESCE(location,''))) <> 'nan'
    """)
    valid_sku_count = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT sku) FROM inventory WHERE quantity > 0")
    total_sku_count = c.fetchone()[0]
    inv_accuracy = round((valid_sku_count / total_sku_count) * 100, 1) if total_sku_count > 0 else 100.0

    # ── KPI 6: Total inventory value ──────────────────────────────────────────
    c.execute("SELECT COALESCE(SUM(CAST(quantity AS REAL) * price), 0) FROM inventory WHERE quantity > 0")
    total_inv_value = c.fetchone()[0]

    # ── Workflow pipeline counts ───────────────────────────────────────────────
    c.execute("SELECT status, COUNT(*) FROM order_header GROUP BY status")
    status_map = dict(c.fetchall())
    pipeline_data = [
        ("Orders Placed",       "Pick Queue",    status_map.get("Orders Placed", 0),       "#dbeafe", "#1d4ed8"),
        ("Picking in Progress", "Picking",       status_map.get("Picking in Progress", 0), "#ede9fe", "#6d28d9"),
        ("Pending Verification","Verification",  status_map.get("Pending Verification", 0),"#fef3c7", "#b45309"),
        ("Completed",           "Shipped",       status_map.get("Completed", 0),            "#dcfce7", "#166534"),
    ]
    quality_exc_count = status_map.get("Quality Issue", 0)
    blocked_count = status_map.get("Blocked", 0)
    pending_verification_count = status_map.get("Pending Verification", 0)

    # ── Daily order volume — last 14 days ─────────────────────────────────────
    c.execute("""
        SELECT DATE(date) AS d, COUNT(*) AS cnt
        FROM order_header
        GROUP BY DATE(date)
        ORDER BY d DESC LIMIT 14
    """)
    daily_rows = list(reversed(c.fetchall()))
    daily_labels = [r[0] for r in daily_rows]
    daily_counts = [r[1] for r in daily_rows]

    # ── Daily completed (shipped) volume — last 14 days ───────────────────────
    c.execute("""
        SELECT DATE(date) AS d, COUNT(*) AS cnt
        FROM order_header
        WHERE status='Completed'
        GROUP BY DATE(date)
        ORDER BY d DESC LIMIT 14
    """)
    ship_rows = list(reversed(c.fetchall()))
    ship_labels  = [r[0] for r in ship_rows]
    ship_counts  = [r[1] for r in ship_rows]

    # ── Orders by responsibility (labor proxy) ────────────────────────────────
    c.execute("""
        SELECT responsibility, COUNT(*) FROM order_header
        GROUP BY responsibility ORDER BY COUNT(*) DESC
    """)
    resp_rows = c.fetchall()
    resp_labels = [r[0] for r in resp_rows]
    resp_counts = [r[1] for r in resp_rows]

    # ── Orders by urgency ─────────────────────────────────────────────────────
    c.execute("""
        SELECT urgency, COUNT(*) FROM order_header
        GROUP BY urgency ORDER BY COUNT(*) DESC
    """)
    urgency_rows = c.fetchall()
    urgency_map = {level: 0 for level in URGENCY_ORDER}
    for urgency_value, count in urgency_rows:
        normalized_level = normalize_urgency(urgency_value, "Standard")
        urgency_map[normalized_level] = urgency_map.get(normalized_level, 0) + count
    urg_labels = [level for level in URGENCY_ORDER if urgency_map.get(level, 0) > 0]
    urg_counts = [urgency_map[level] for level in urg_labels]

    # ── Top 10 SKUs by inventory value ────────────────────────────────────────
    c.execute("""
        SELECT sku, SUM(CAST(quantity AS REAL) * price) AS val
        FROM inventory WHERE quantity > 0
        GROUP BY sku ORDER BY val DESC LIMIT 10
    """)
    top_sku_rows = c.fetchall()
    top_sku_labels = [str(r[0]) for r in top_sku_rows]
    top_sku_values = [r[1] for r in top_sku_rows]

    # ── Warehouse distribution by value ───────────────────────────────────────
    c.execute("""
        SELECT warehouse,
               SUM(CAST(quantity AS REAL) * price) AS val,
               SUM(quantity) AS qty
        FROM inventory
        WHERE quantity > 0
          AND TRIM(COALESCE(warehouse,'')) <> ''
          AND LOWER(TRIM(COALESCE(warehouse,''))) <> 'nan'
        GROUP BY warehouse ORDER BY val DESC
    """)
    wh_rows = c.fetchall()
    wh_labels = [r[0] for r in wh_rows]
    wh_values = [r[1] for r in wh_rows]
    wh_qtys   = [r[2] for r in wh_rows]

    # ── Inventory health counts ────────────────────────────────────────────────
    c.execute("""
        SELECT COUNT(DISTINCT sku) FROM (
            SELECT sku, SUM(quantity) AS tq FROM inventory GROUP BY sku HAVING tq > 0 AND tq < ?
        )
    """, (DEFAULT_LOW_STOCK_ALERT_THRESHOLD,))
    low_stock_count = c.fetchone()[0]
    c.execute("""
        SELECT COUNT(DISTINCT sku) FROM (
            SELECT sku, SUM(quantity) AS tq FROM inventory GROUP BY sku HAVING tq > 5000
        )
    """)
    overstock_count = c.fetchone()[0]

    # ── Top moving SKUs from transactions ─────────────────────────────────────
    c.execute("""
        SELECT sku, COUNT(*) AS tc FROM inventory_transactions
        GROUP BY sku ORDER BY tc DESC LIMIT 5
    """)
    top_moving = c.fetchall()

    # ── Low-stock SKU detail (alert panel) ────────────────────────────────────
    c.execute("""
        SELECT sku, SUM(quantity) AS tq FROM inventory
        GROUP BY sku HAVING tq > 0 AND tq < ?
        ORDER BY tq ASC LIMIT 8
    """, (DEFAULT_LOW_STOCK_ALERT_THRESHOLD,))
    low_stock_skus = c.fetchall()

    # ── Open quality issues ────────────────────────────────────────────────────
    c.execute("""
        SELECT COUNT(*)
        FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
    """)
    open_quality_issue_count = c.fetchone()[0]

    c.execute("""
        SELECT issue_id, order_number, issue_date, issue_type, issue_status
        FROM supervisor_quality_issues
        WHERE issue_status NOT IN ('Closed', 'Resolved')
        ORDER BY issue_date DESC LIMIT 8
    """)
    open_issues = c.fetchall()

    # ── SLA health over all open orders ───────────────────────────────────────
    c.execute("SELECT order_number, urgency, status, responsibility, date FROM order_header")
    all_order_rows = c.fetchall()
    sla_healthy = sla_at_risk = sla_breached = 0
    sla_watch_rows = []
    open_sla_rows = []
    for order_number, urgency, status, responsibility, date_str in all_order_rows:
        if status == "Completed":
            continue
        urgency = normalize_urgency(urgency, "Standard")
        order_time = parse_order_datetime(date_str)
        sla_key, sla_label = calculate_sla_status(order_time, urgency, now)
        sla_start = format_sla_start_time(order_time, urgency)
        sla_deadline, sla_timer = format_sla_timing(order_time, urgency, now)
        sla_deadline_dt = calculate_sla_deadline(order_time, urgency)
        if sla_key == "healthy":
            sla_healthy += 1
        elif sla_key == "at_risk":
            sla_at_risk += 1
        else:
            sla_breached += 1

        open_sla_rows.append(
            {
                "order_number": order_number,
                "urgency": urgency,
                "status": status,
                "responsibility": responsibility,
                "sla_key": sla_key,
                "sla_start": sla_start,
                "sla_deadline": sla_deadline,
                "sla_timer": sla_timer,
                "sla_deadline_dt": sla_deadline_dt,
            }
        )

        if sla_key in {"at_risk", "breached"}:
            sla_watch_rows.append(
                {
                    "order_number": order_number,
                    "urgency": urgency,
                    "status": status,
                    "responsibility": responsibility,
                    "sla_label": sla_label,
                    "sla_key": sla_key,
                    "sla_deadline": sla_deadline,
                    "sla_timer": sla_timer,
                }
            )

    sla_watch_rows.sort(
        key=lambda item: (0 if item["sla_key"] == "breached" else 1, item["sla_deadline"])
    )
    sla_watch_rows = sla_watch_rows[:8]
    open_sla_rows.sort(key=lambda item: item["sla_deadline_dt"])
    open_sla_rows = open_sla_rows[:12]
    sla_total = sla_healthy + sla_at_risk + sla_breached
    on_time_rate = round(((sla_healthy + sla_at_risk) / sla_total) * 100, 1) if sla_total else 100.0

    # ── Replenishment needed (quantity between 1–20) ───────────────────────────
    c.execute("""
        SELECT COUNT(DISTINCT sku) FROM (
            SELECT sku, SUM(quantity) AS tq FROM inventory
            GROUP BY sku HAVING tq > 0 AND tq <= 20
        )
    """)
    replenish_count = c.fetchone()[0]

    # ── Inventory discrepancies (negative adjustments) ────────────────────────
    c.execute("""
        SELECT COUNT(*) FROM inventory_transactions WHERE qty_change < 0 AND tx_code LIKE '%ADJUST%'
    """)
    neg_adjustments = c.fetchone()[0]

    conn.close()

    # ═══════════════════════════════════════════════════════
    # CHART GENERATION
    # ═══════════════════════════════════════════════════════

    CHART_ACCENT   = "#2563eb"
    CHART_SUCCESS  = "#16a34a"
    CHART_WARNING  = "#f59e0b"
    CHART_DANGER   = "#ef4444"
    CHART_VIOLET   = "#7c3aed"
    CHART_TEAL     = "#0d9488"

    def _finalize_chart():
        img = io.BytesIO()
        plt.tight_layout()
        plt.savefig(img, format="png", dpi=110, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        img.seek(0)
        encoded = base64.b64encode(img.getvalue()).decode()
        plt.close()
        return encoded

    # ── Chart 1: Daily Order Volume (line) ────────────────────────────────────
    chart_order_trend = None
    if daily_rows:
        fig, ax = plt.subplots(figsize=(7, 3.2))
        short = [d[-5:] for d in daily_labels]
        ax.fill_between(range(len(short)), daily_counts,
                        alpha=0.12, color=CHART_ACCENT)
        ax.plot(range(len(short)), daily_counts,
                color=CHART_ACCENT, linewidth=2.5, marker="o",
                markersize=5, markerfacecolor="white", markeredgecolor=CHART_ACCENT, markeredgewidth=2)
        ax.set_xticks(range(len(short)))
        ax.set_xticklabels(short, fontsize=9, rotation=30, ha="right")
        ax.set_ylabel("Orders", fontsize=10)
        ax.set_title("Daily Order Volume", fontsize=12, fontweight="bold", pad=10)
        ax.grid(axis="y", linestyle="--", alpha=0.2)
        ax.spines[["top","right"]].set_visible(False)
        chart_order_trend = _finalize_chart()

    # ── Chart 2: Daily Shipment Volume (bar) ──────────────────────────────────
    chart_ship_volume = None
    if ship_rows:
        fig, ax = plt.subplots(figsize=(7, 3.2))
        short = [d[-5:] for d in ship_labels]
        bars = ax.bar(range(len(short)), ship_counts,
                      color=CHART_SUCCESS, alpha=0.85, width=0.6, edgecolor="white")
        ax.set_xticks(range(len(short)))
        ax.set_xticklabels(short, fontsize=9, rotation=30, ha="right")
        ax.set_ylabel("Shipped", fontsize=10)
        ax.set_title("Daily Shipment Volume (Completed Orders)", fontsize=12, fontweight="bold", pad=10)
        for bar, v in zip(bars, ship_counts):
            if v:
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                        str(v), ha="center", va="bottom", fontsize=9, color="#1e293b")
        ax.grid(axis="y", linestyle="--", alpha=0.2)
        ax.spines[["top","right"]].set_visible(False)
        chart_ship_volume = _finalize_chart()

    # ── Chart 3: Top 10 SKUs by Inventory Value (horizontal bar) ─────────────
    chart_top_skus = None
    if top_sku_rows:
        fig, ax = plt.subplots(figsize=(7, 4))
        colors_bar = [CHART_ACCENT] * len(top_sku_labels)
        colors_bar[0] = CHART_VIOLET
        ax.barh(top_sku_labels[::-1], top_sku_values[::-1],
                color=colors_bar[::-1], edgecolor="white", height=0.7)
        ax.set_xlabel("Inventory Value ($)", fontsize=10)
        ax.set_title("Top 10 SKUs by Inventory Value", fontsize=12, fontweight="bold", pad=10)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax.tick_params(axis="y", labelsize=9)
        ax.grid(axis="x", linestyle="--", alpha=0.2)
        ax.spines[["top","right"]].set_visible(False)
        chart_top_skus = _finalize_chart()

    # ── Chart 4: Warehouse Inventory Distribution (donut pie) ─────────────────
    chart_wh_dist = None
    if wh_rows and len(wh_rows) > 0:
        fig, ax = plt.subplots(figsize=(5.5, 4))
        pie_colors = [CHART_ACCENT, CHART_TEAL, CHART_VIOLET, CHART_WARNING, CHART_SUCCESS][:len(wh_labels)]
        wedges, texts, autotexts = ax.pie(
            wh_values,
            labels=None,
            autopct=lambda p: f"{p:.0f}%" if p > 4 else "",
            colors=pie_colors,
            startangle=100,
            wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
            pctdistance=0.78,
        )
        for at in autotexts:
            at.set_fontsize(9)
            at.set_color("white")
            at.set_fontweight("bold")
        centre = plt.Circle((0, 0), 0.52, fc="white")
        ax.add_artist(centre)
        ax.legend(wedges, wh_labels, loc="lower center",
                  bbox_to_anchor=(0.5, -0.12), ncol=2,
                  fontsize=9, frameon=False)
        ax.set_title("Inventory Value by Warehouse", fontsize=12, fontweight="bold", pad=6)
        chart_wh_dist = _finalize_chart()

    # ── Chart 5: Labor / Responsibility workload (horizontal bar) ─────────────
    chart_labor = None
    if resp_rows:
        fig, ax = plt.subplots(figsize=(6, 2.8))
        resp_colors = [CHART_ACCENT, CHART_TEAL, "#dc2626", "#475569", "#9333ea"][:len(resp_labels)]
        bars_h = ax.barh(resp_labels[::-1], resp_counts[::-1],
                         color=resp_colors[::-1], edgecolor="white", height=0.55)
        for bar, v in zip(bars_h, resp_counts[::-1]):
            ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                    str(v), va="center", fontsize=10, color="#0f172a", fontweight="bold")
        ax.set_title("Orders by Responsibility (Workload)", fontsize=12, fontweight="bold", pad=10)
        ax.grid(axis="x", linestyle="--", alpha=0.2)
        ax.spines[["top","right"]].set_visible(False)
        chart_labor = _finalize_chart()

    # ── Chart 6: Urgency mix (pie) ─────────────────────────────────────────────
    chart_urgency = None
    if urgency_rows:
        fig, ax = plt.subplots(figsize=(5, 3.4))
        urg_colors = {"Critical": CHART_DANGER, "Urgent": CHART_WARNING, "Standard": CHART_SUCCESS}
        pie_c = [urg_colors.get(u, CHART_ACCENT) for u in urg_labels]
        wedges, _, autotexts = ax.pie(
            urg_counts, labels=None, autopct="%1.0f%%",
            colors=pie_c, startangle=90,
            wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
            pctdistance=0.75,
        )
        for at in autotexts:
            at.set_fontsize(10)
            at.set_color("white")
            at.set_fontweight("bold")
        centre = plt.Circle((0, 0), 0.45, fc="white")
        ax.add_artist(centre)
        legend_labels = [f"{l} ({v})" for l, v in zip(urg_labels, urg_counts)]
        ax.legend(wedges, legend_labels, loc="lower center",
                  bbox_to_anchor=(0.5, -0.08), ncol=3, fontsize=9, frameon=False)
        ax.set_title("Order Urgency Mix", fontsize=12, fontweight="bold", pad=6)
        chart_urgency = _finalize_chart()

    # ── Chart 7: SLA Health donut ─────────────────────────────────────────────
    chart_sla = None
    if sla_total:
        fig, ax = plt.subplots(figsize=(5, 3.8))
        sla_sizes = [sla_healthy, sla_at_risk, sla_breached]
        sla_colors = [CHART_SUCCESS, CHART_WARNING, CHART_DANGER]
        sla_labs = [f"Healthy ({sla_healthy})", f"At Risk ({sla_at_risk})", f"Breached ({sla_breached})"]
        wedges, _, autotexts = ax.pie(
            [max(s, 0.01) for s in sla_sizes],
            labels=None, autopct=lambda p: f"{p:.0f}%" if p > 3 else "",
            colors=sla_colors, startangle=110,
            wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
            pctdistance=0.78,
        )
        for at in autotexts:
            at.set_fontsize(9)
            at.set_color("white")
            at.set_fontweight("bold")
        centre = plt.Circle((0, 0), 0.50, fc="white")
        ax.add_artist(centre)
        ax.legend(wedges, sla_labs, loc="lower center",
                  bbox_to_anchor=(0.5, -0.1), ncol=1, fontsize=9, frameon=False)
        ax.set_title("Open Order SLA Health", fontsize=12, fontweight="bold", pad=6)
        chart_sla = _finalize_chart()

    # ═══════════════════════════════════════════════════════
    # HTML ASSEMBLY
    # ═══════════════════════════════════════════════════════

    # ── helpers ───────────────────────────────────────────────────────────────
    def _fmt_currency(val):
        if val >= 1_000_000:
            return f"${val/1_000_000:.1f}M"
        if val >= 1_000:
            return f"${val/1_000:.1f}K"
        return f"${val:.0f}"

    def _accuracy_badge(pct):
        if pct >= 98:
            return f"<span class='exec-kpi-badge good'>&#10003; Excellent</span>"
        if pct >= 90:
            return f"<span class='exec-kpi-badge warn'>&#9888; Acceptable</span>"
        return f"<span class='exec-kpi-badge danger'>&#9888; Needs Work</span>"

    def _chart_img(b64):
        return f'<img src="data:image/png;base64,{b64}">' if b64 else "<p style='color:#94a3b8;font-size:13px;'>No data yet.</p>"

    def _progress(label, val, color="#2563eb", max_val=100):
        pct = min(round((val / max_val) * 100, 1) if max_val else 0, 100)
        return f"""
        <div class='exec-prog-wrap'>
            <div class='exec-prog-label'><span>{label}</span><span style='font-weight:700;'>{pct}%</span></div>
            <div class='exec-prog-bar'><div class='exec-prog-fill' style='width:{pct}%;background:{color};'></div></div>
        </div>"""

    # ── KPI badges for context ─────────────────────────────────────────────────
    orders_today_note    = f"All-time total: {orders_all_time:,}" if orders_today == 0 else f"All-time: {orders_all_time:,}"
    pick_acc_badge       = _accuracy_badge(pick_accuracy)
    inv_acc_badge        = _accuracy_badge(inv_accuracy)
    on_time_badge_cls    = "good" if on_time_rate >= 90 else ("warn" if on_time_rate >= 75 else "danger")
    pending_badge_cls    = "good" if orders_pending == 0 else ("warn" if orders_pending <= 5 else "danger")
    kpi_shipped_note     = f"All-time completed: {shipped_total:,}"

    # ── Pipeline HTML ─────────────────────────────────────────────────────────
    pipeline_html = ""
    for _status, label, count, bg, fg in pipeline_data:
        pipeline_html += f"""
        <div class='pipeline-stage'>
            <div class='pipeline-node' style='background:{bg};border-color:{fg}33;'>
                <div class='pipeline-count' style='color:{fg};'>{count}</div>
                <div class='pipeline-label' style='color:{fg};'>{label}</div>
                <div class='pipeline-sublabel' style='color:{fg};'>{_status}</div>
            </div>
        </div>"""

    # ── Exception alert items ─────────────────────────────────────────────────
    exception_alerts_html = ""
    if sla_breached > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot red'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{sla_breached} Orders Breached SLA</div>
                <div class='exec-alert-sub'>Immediate supervisor review required</div>
            </div>
        </div>"""
    if quality_exc_count > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot red'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{quality_exc_count} Quality Issues Active</div>
                <div class='exec-alert-sub'>Orders halted for inspection</div>
            </div>
        </div>"""
    if low_stock_count > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot orange'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{low_stock_count} SKUs Low Stock (&lt;{DEFAULT_LOW_STOCK_ALERT_THRESHOLD} units)</div>
                <div class='exec-alert-sub'>Risk of fulfilment delay</div>
            </div>
        </div>"""
    if replenish_count > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot orange'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{replenish_count} SKUs Need Replenishment (&le;20 units)</div>
                <div class='exec-alert-sub'>Reorder point reached</div>
            </div>
        </div>"""
    if neg_adjustments > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot orange'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{neg_adjustments} Negative Inventory Adjustments</div>
                <div class='exec-alert-sub'>Possible discrepancies logged</div>
            </div>
        </div>"""
    if sla_at_risk > 0:
        exception_alerts_html += f"""
        <div class='exec-alert-item'>
            <div class='exec-alert-dot yellow'></div>
            <div class='exec-alert-body'>
                <div class='exec-alert-title'>{sla_at_risk} Orders At Risk</div>
                <div class='exec-alert-sub'>Less than 50% of SLA window remaining</div>
            </div>
        </div>"""
    if not exception_alerts_html:
        exception_alerts_html = "<div class='exec-alert-item'><div class='exec-alert-dot' style='background:#22c55e;'></div><div class='exec-alert-body'><div class='exec-alert-title'>No Active Exceptions</div><div class='exec-alert-sub'>All systems are operating normally</div></div></div>"

    sla_watch_html = ""
    if sla_watch_rows:
        for row in sla_watch_rows:
            sla_color = "#ef4444" if row["sla_key"] == "breached" else "#f59e0b"
            sla_watch_html += f"""
            <div class='exec-alert-item'>
                <div class='exec-alert-dot' style='background:{sla_color};'></div>
                <div class='exec-alert-body'>
                    <div class='exec-alert-title'>
                        {row['order_number']} • {row['urgency']} • {row['sla_label']}
                    </div>
                    <div class='exec-alert-sub'>
                        Deadline: {row['sla_deadline']} • {row['sla_timer']} • Owner: {row['responsibility']}
                    </div>
                </div>
                <a href='/order/{row['order_number']}' style='color:var(--accent-600);font-size:12px;font-weight:700;white-space:nowrap;text-decoration:none;'>Open ›</a>
            </div>"""
    else:
        sla_watch_html = "<div style='font-size:13px;color:#64748b;padding-top:8px;'>No at-risk or breached SLA orders ✓</div>"

    open_sla_table_html = ""
    if open_sla_rows:
        open_sla_table_html = """
        <div style='overflow-x:auto;overflow-y:auto;max-height:480px;'>
        <table>
            <tr>
                <th>Order</th>
                <th>Priority</th>
                <th>Status</th>
                <th>SLA Start</th>
                <th>SLA Deadline</th>
                <th>SLA Remaining</th>
                <th>Risk</th>
            </tr>
        """
        for row in open_sla_rows:
            open_sla_table_html += f"""
            <tr>
                <td><a href='/order/{row['order_number']}'>{row['order_number']}</a></td>
                <td>{row['urgency']}</td>
                <td>{status_badge_html(row['status'])}</td>
                <td>{row['sla_start']}</td>
                <td>{row['sla_deadline']}</td>
                <td>{row['sla_timer']}</td>
                <td>{sla_risk_badge_html(row['sla_key'])}</td>
            </tr>
            """
        open_sla_table_html += "</table></div>"
    else:
        open_sla_table_html = "<div style='font-size:13px;color:#64748b;padding-top:8px;'>No open orders currently in SLA scope.</div>"

    # ── Open quality issues table ─────────────────────────────────────────────
    issues_table_html = ""
    if open_issues:
        for issue_id, order_num, issue_date, issue_type, issue_status in open_issues:
            badge_bg = "#fee2e2" if issue_status == "Open" else "#fef3c7"
            badge_fg = "#b91c1c" if issue_status == "Open" else "#b45309"
            issues_table_html += f"""
            <div class='exec-alert-item'>
                <div class='exec-alert-dot red'></div>
                <div class='exec-alert-body'>
                    <div class='exec-alert-title'>{order_num} — {issue_type or "Issue"}</div>
                    <div class='exec-alert-sub'>{issue_date[:10] if issue_date else "—"} &nbsp;
                        <span style='padding:2px 8px;border-radius:999px;background:{badge_bg};color:{badge_fg};font-size:11px;font-weight:700;'>{issue_status}</span>
                    </div>
                </div>
                <a href='/supervisor_issue/{issue_id}' style='color:var(--accent-600);font-size:12px;font-weight:700;white-space:nowrap;text-decoration:none;'>Open ›</a>
            </div>"""
    else:
        issues_table_html = "<div style='font-size:13px;color:#64748b;padding-top:8px;'>No open quality issues ✓</div>"

    # ── Low stock SKUs detail ─────────────────────────────────────────────────
    low_stock_html = ""
    if low_stock_skus:
        for sku, qty in low_stock_skus:
            pct_bar = min(round((qty / 50) * 100), 100)
            bar_color = "#ef4444" if qty <= 10 else "#f97316"
            low_stock_html += f"""
            <div class='exec-alert-item'>
                <div class='exec-alert-body'>
                    <div class='exec-alert-title' style='display:flex;justify-content:space-between;'>
                        <span>SKU {sku}</span>
                        <span style='color:{bar_color};font-size:13px;'>{qty} units</span>
                    </div>
                    <div class='exec-prog-bar' style='margin-top:5px;'>
                        <div class='exec-prog-fill' style='width:{pct_bar}%;background:{bar_color};'></div>
                    </div>
                </div>
            </div>"""
    else:
        low_stock_html = "<div style='font-size:13px;color:#64748b;padding-top:8px;'>No low-stock SKUs ✓</div>"

    # ── Top moving SKUs ───────────────────────────────────────────────────────
    top_moving_html = ""
    if top_moving:
        max_tm = top_moving[0][1] if top_moving else 1
        move_colors = [CHART_ACCENT, CHART_VIOLET, CHART_TEAL, CHART_WARNING, "#64748b"]
        for i, (sku, tc) in enumerate(top_moving):
            pct_w = round((tc / max_tm) * 100)
            col = move_colors[i % len(move_colors)]
            top_moving_html += f"""
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>SKU {sku}</span>
                <span style='display:flex;align-items:center;gap:10px;'>
                    <span style='width:80px;height:8px;border-radius:999px;background:#e9edf5;display:inline-block;vertical-align:middle;overflow:hidden;'>
                        <span style='width:{pct_w}%;height:100%;background:{col};display:block;border-radius:999px;'></span>
                    </span>
                    <span class='exec-stat-value' style='font-size:15px;'>{tc}</span>
                </span>
            </div>"""
    else:
        top_moving_html = "<p style='color:#94a3b8;font-size:13px;'>No transaction history yet.</p>"

    # ── Warehouse inventory table ─────────────────────────────────────────────
    wh_table_html = ""
    total_wh_val = sum(wh_values) if wh_values else 1
    wh_pal = [CHART_ACCENT, CHART_TEAL, CHART_VIOLET, CHART_WARNING, CHART_SUCCESS]
    for i, (wh, val, qty) in enumerate(wh_rows):
        pct_w = round((val / total_wh_val) * 100)
        col = wh_pal[i % len(wh_pal)]
        wh_table_html += f"""
        <div class='exec-stat-row'>
            <div>
                <div style='font-size:13px;font-weight:700;color:var(--ink-900);'>{wh}</div>
                <div style='font-size:12px;color:var(--ink-500);'>{qty:,} units</div>
            </div>
            <div style='display:flex;align-items:center;gap:10px;'>
                <span style='width:70px;height:8px;border-radius:999px;background:#e9edf5;display:inline-block;vertical-align:middle;overflow:hidden;'>
                    <span style='width:{pct_w}%;height:100%;background:{col};display:block;border-radius:999px;'></span>
                </span>
                <span class='exec-stat-value' style='font-size:14px;'>{_fmt_currency(val)}</span>
            </div>
        </div>"""
    if not wh_table_html:
        wh_table_html = "<p style='color:#94a3b8;font-size:13px;'>No warehouse data.</p>"

    # ── Productivity stat rows ─────────────────────────────────────────────────
    avg_daily = round(sum(daily_counts) / len(daily_counts), 1) if daily_counts else 0
    avg_shipped_daily = round(sum(ship_counts) / len(ship_counts), 1) if ship_counts else 0
    total_txns = sum(r[1] for r in top_moving) if top_moving else 0
    quality_audit_coverage = round((total_audits / orders_all_time) * 100, 1) if orders_all_time else 0
    quality_gap = round(pick_accuracy - QUALITY_STANDARD_TARGET, 1)
    quality_standard_class = "good" if pick_accuracy >= QUALITY_STANDARD_TARGET else ("warn" if pick_accuracy >= QUALITY_STANDARD_TARGET - 3 else "danger")
    quality_standard_label = "At Standard" if pick_accuracy >= QUALITY_STANDARD_TARGET else "Below Standard"
    quality_standard_note = (
        f"{abs(quality_gap)} pts above target" if quality_gap >= 0 else f"{abs(quality_gap)} pts below target"
    )
    failed_audit_badge_cls = "good" if failed_audits == 0 else ("warn" if failed_audits <= 5 else "danger")
    open_quality_issue_badge_cls = "good" if open_quality_issue_count == 0 else ("warn" if open_quality_issue_count == 1 else "danger")

    # ═══════════════════════════════════════════════════════
    # PAGE CONTENT
    # ═══════════════════════════════════════════════════════
    content = f"""
    <!-- ── PAGE HEADER ── -->
    <div style='margin-bottom:28px;'>
        <div class='page-eyebrow'>&#9672; Executive Control Tower</div>
        <h1 style='font-size:36px;letter-spacing:-0.05em;margin-bottom:6px;'>Warehouse Executive Dashboard</h1>
        <p style='color:var(--ink-500);font-size:15px;margin:0;'>
            Live operational overview &mdash; {format_executive_timestamp(now)}
        </p>
        {f"<div class='demo-session-note' style='margin-top:14px;background:#ecfdf5;border-color:#86efac;color:#166534;'>Demo session restored to the original sample warehouse baseline.</div>" if request.args.get('demo_reset') == '1' else ''}
        <div class='exec-section-switcher'>
            <button type='button' class='exec-switch-btn active' data-target='overview'>Overview</button>
            <button type='button' class='exec-switch-btn' data-target='productivity'>Productivity</button>
            <button type='button' class='exec-switch-btn' data-target='inventory'>Top 10 SKU &amp; Inventory</button>
            <button type='button' class='exec-switch-btn' data-target='exceptions'>Exceptions</button>
            <button type='button' class='exec-switch-btn' data-target='shipping'>Shipping</button>
            <button type='button' class='exec-switch-btn' data-target='labor'>Labor &amp; Trends</button>
        </div>
        <p class='section-note' style='margin:6px 0 0 0;'>
            Click a section to focus on one executive view at a time. All existing metrics remain available.
        </p>
    </div>

    <section class='exec-view-section active' data-section='overview'>
    <!-- ── SECTION 1: KPI TILES ── -->
    <div class='exec-kpi-grid'>
        <div class='exec-kpi-card kpi-blue'>
            <div class='exec-kpi-eyebrow'>Orders Received Today</div>
            <div class='exec-kpi-number'>{orders_today if orders_today else orders_all_time:,}</div>
            <div class='exec-kpi-sub'>{orders_today_note}</div>
            <span class='exec-kpi-badge good'>&#9654; Orders Placed</span>
        </div>
        <div class='exec-kpi-card kpi-green'>
            <div class='exec-kpi-eyebrow'>Orders Shipped Today</div>
            <div class='exec-kpi-number'>{shipped_today if shipped_today else shipped_total:,}</div>
            <div class='exec-kpi-sub'>{kpi_shipped_note}</div>
            <span class='exec-kpi-badge {"good" if shipped_total > 0 else "warn"}'>
                {"&#10003; Active Shipping" if shipped_total > 0 else "&#9888; No shipments yet"}
            </span>
        </div>
        <div class='exec-kpi-card kpi-amber'>
            <div class='exec-kpi-eyebrow'>Orders Pending</div>
            <div class='exec-kpi-number'>{orders_pending:,}</div>
            <div class='exec-kpi-sub'>Not yet completed</div>
            <span class='exec-kpi-badge {pending_badge_cls}'>
                {"&#10003; Clear" if orders_pending == 0 else f"&#9888; {orders_pending} open"}
            </span>
        </div>
        <div class='exec-kpi-card kpi-violet'>
            <div class='exec-kpi-eyebrow'>Quality Audit Pass Rate</div>
            <div class='exec-kpi-number'>{pick_accuracy}%</div>
            <div class='exec-kpi-sub'>{passed_audits} passed of {total_audits} audits</div>
            {pick_acc_badge}
        </div>
        <div class='exec-kpi-card kpi-teal'>
            <div class='exec-kpi-eyebrow'>Inventory Accuracy</div>
            <div class='exec-kpi-number'>{inv_accuracy}%</div>
            <div class='exec-kpi-sub'>{valid_sku_count:,} of {total_sku_count:,} SKUs fully located</div>
            {inv_acc_badge}
        </div>
        <div class='exec-kpi-card kpi-rose'>
            <div class='exec-kpi-eyebrow'>Total Inventory Value</div>
            <div class='exec-kpi-number'>{_fmt_currency(total_inv_value)}</div>
            <div class='exec-kpi-sub'>{total_sku_count:,} active SKUs across {len(wh_rows)} warehouses</div>
            <span class='exec-kpi-badge good'>&#9654; Live Valuation</span>
        </div>
    </div>
        <!-- ZONE A: OPERATIONS RISK -->
        <details style='margin-bottom:16px;'>
            <summary style='cursor:pointer;list-style:none;padding:12px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:10px;font-weight:700;color:#b91c1c;font-size:14px;display:flex;align-items:center;gap:8px;'>
                <span>&#128308; OPERATIONS RISK &mdash; SLA, Breaches &amp; On-Time Rate</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div style='padding:16px 0 4px 0;'>
        <div class='exec-kpi-grid'>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #7f1d1d, #b91c1c);border-left:4px solid #ef4444;'>
                <div class='exec-kpi-eyebrow' style='color:#fecaca;'>Orders at Risk 🔴</div>
                <div class='exec-kpi-number' style='color:#fca5a5;'>{sla_breached + sla_at_risk}</div>
                <div class='exec-kpi-sub' style='color:#fca5a5;'>{sla_breached} breached + {sla_at_risk} at risk (&lt;2 hrs left)</div>
                <span class='exec-kpi-badge' style='background:#fee2e2;color:#b91c1c;'>{"&#9888; ACTION NEEDED" if (sla_breached + sla_at_risk) > 0 else "&#10003; Clear"}</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #7c2d12, #b45309);border-left:4px solid #f59e0b;'>
                <div class='exec-kpi-eyebrow' style='color:#fed7aa;'>SLA Breaches</div>
                <div class='exec-kpi-number' style='color:#fde047;'>{sla_breached}</div>
                <div class='exec-kpi-sub' style='color:#fed7aa;'>Orders past deadline (late shipments)</div>
                <span class='exec-kpi-badge' style='background:#fff7ed;color:#b45309;'>{"&#9888; ESCALATE" if sla_breached > 0 else "&#10003; None"}</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #431407, #7c2d12);border-left:4px solid #ea580c;'>
                <div class='exec-kpi-eyebrow' style='color:#fed7aa;'>Avg Time to Complete</div>
                <div class='exec-kpi-number' style='color:#fde047;'>{avg_daily:.1f} hrs</div>
                <div class='exec-kpi-sub' style='color:#fed7aa;'>Average order cycle time from arrival</div>
                <span class='exec-kpi-badge' style='background:#fff5eb;color:#92400e;'>&#9654; {avg_daily:.0f} hrs avg</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #5b21b6, #7c3aed);border-left:4px solid #a855f7;'>
                <div class='exec-kpi-eyebrow' style='color:#e9d5ff;'>Orders Completing on Time</div>
                <div class='exec-kpi-number' style='color:#d8b4fe;'>{on_time_rate}%</div>
                <div class='exec-kpi-sub' style='color:#e9d5ff;'>{sla_healthy} healthy + {sla_at_risk} at risk</div>
                <span class='exec-kpi-badge {on_time_badge_cls}' style='margin-top:8px;'>{"&#10003; On Track" if on_time_rate >= 90 else "&#9888; Check"}</span>
            </div>
        </div>
            </div>
        </details>

        <!-- ZONE B: INVENTORY HEALTH -->
        <details style='margin-bottom:16px;'>
            <summary style='cursor:pointer;list-style:none;padding:12px 16px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;font-weight:700;color:#b45309;font-size:14px;display:flex;align-items:center;gap:8px;'>
                <span>&#128993; INVENTORY HEALTH &mdash; Stock, Accuracy &amp; Value</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div style='padding:16px 0 4px 0;'>
        <div class='exec-kpi-grid'>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #065f46, #059669);border-left:4px solid #10b981;'>
                <div class='exec-kpi-eyebrow' style='color:#a7f3d0;'>Inventory Accuracy</div>
                <div class='exec-kpi-number' style='color:#6ee7b7;'>{inv_accuracy}%</div>
                <div class='exec-kpi-sub' style='color:#a7f3d0;'>{valid_sku_count:,} of {total_sku_count:,} SKUs properly located</div>
                {inv_acc_badge}
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #7c2d12, #b45309);border-left:4px solid #f97316;'>
                <div class='exec-kpi-eyebrow' style='color:#fed7aa;'>Low Stock Items 🔴</div>
                <div class='exec-kpi-number' style='color:#fde047;'>{low_stock_count}</div>
                <div class='exec-kpi-sub' style='color:#fed7aa;'>SKUs below {DEFAULT_LOW_STOCK_ALERT_THRESHOLD} units (risk of stockout)</div>
                <span class='exec-kpi-badge' style='background:#fff7ed;color:#b45309;'>{"&#9888; Replenish now" if low_stock_count > 0 else "&#10003; Healthy"}</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #4c1d95, #7c3aed);border-left:4px solid #d946ef;'>
                <div class='exec-kpi-eyebrow' style='color:#e9d5ff;'>Overstock Items</div>
                <div class='exec-kpi-number' style='color:#d8b4fe;'>{overstock_count}</div>
                <div class='exec-kpi-sub' style='color:#e9d5ff;'>SKUs above 5000 units (capital tied up)</div>
                <span class='exec-kpi-badge' style='background:#f3e8ff;color:#6b21a8;'>&#9654; Review</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #0c4a6e, #0ea5e9);border-left:4px solid #3b82f6;'>
                <div class='exec-kpi-eyebrow' style='color:#bae6fd;'>Total Inventory Value</div>
                <div class='exec-kpi-number' style='color:#7dd3fc;'>{_fmt_currency(total_inv_value)}</div>
                <div class='exec-kpi-sub' style='color:#bae6fd;'>{total_sku_count:,} active SKUs in {len(wh_rows)} warehouses</div>
                <span class='exec-kpi-badge good' style='margin-top:8px;'>&#9654; Live</span>
            </div>
        </div>
            </div>
        </details>

        <!-- ZONE C: QUALITY & PERFORMANCE -->
        <details style='margin-bottom:16px;'>
            <summary style='cursor:pointer;list-style:none;padding:12px 16px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;font-weight:700;color:#166534;font-size:14px;display:flex;align-items:center;gap:8px;'>
                <span>&#128994; QUALITY &amp; PERFORMANCE &mdash; Accuracy, Audits &amp; Issues</span>
                <span style='margin-left:auto;font-size:18px;font-weight:400;'>&#8897;</span>
            </summary>
            <div style='padding:16px 0 4px 0;'>
        <div class='exec-kpi-grid'>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #065f46, #10b981);border-left:4px solid #34d399;'>
                <div class='exec-kpi-eyebrow' style='color:#a7f3d0;'>Quality Audit Pass Rate</div>
                <div class='exec-kpi-number' style='color:#6ee7b7;'>{pick_accuracy}%</div>
                <div class='exec-kpi-sub' style='color:#a7f3d0;'>{passed_audits} passed of {total_audits} audits</div>
                {pick_acc_badge}
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #7f1d1d, #b91c1c);border-left:4px solid #f87171;'>
                <div class='exec-kpi-eyebrow' style='color:#fecaca;'>Failed Audits</div>
                <div class='exec-kpi-number' style='color:#fca5a5;'>{failed_audits}</div>
                <div class='exec-kpi-sub' style='color:#fecaca;'>Quality inspection failures (audit scope: {quality_audit_coverage}%)</div>
                <span class='exec-kpi-badge' style='background:{"#dbeafe" if failed_audits == 0 else "#fee2e2"};color:{"#0c4a6e" if failed_audits == 0 else "#b91c1c"};'>{"&#10003; No failures" if failed_audits == 0 else f"&#9888; {failed_audits} failed"}</span>
            </div>
            <div class='exec-kpi-card' style='background:linear-gradient(135deg, #4c0519, #7c2d12);border-left:4px solid #f43f5e;'>
                <div class='exec-kpi-eyebrow' style='color:#fed7aa;'>Open Quality Issues</div>
                <div class='exec-kpi-number' style='color:#fde047;'>{open_quality_issue_count}</div>
                <div class='exec-kpi-sub' style='color:#fed7aa;'>{quality_exc_count} orders in Quality Issue status</div>
                <span class='exec-kpi-badge' style='background:{"#dbeafe" if open_quality_issue_count == 0 else "#fff7ed"};color:{"#0c4a6e" if open_quality_issue_count == 0 else "#b45309"};'>{"&#10003; Clear" if open_quality_issue_count == 0 else "&#9888; Review"}</span>
            </div>
        </div>
            </div>
        </details>

    </section>

    <section class='exec-view-section' data-section='productivity'>
    <!-- ── SECTION 3: PRODUCTIVITY & TRENDS ── -->
    <div class='exec-section-label'>&#9654; Productivity &amp; Trends</div>
    <h2 class='exec-section-title' style='margin-bottom:4px;'>Warehouse Productivity</h2>
    <p class='exec-section-desc'>Order throughput trends, velocity metrics, and team workload distribution.</p>
    <div class='exec-chart-2col' style='margin-bottom:24px;'>
        <div class='exec-chart-card'>
            <h4>&#9672; Daily Order Volume (Last 14 Days)</h4>
            {_chart_img(chart_order_trend)}
            <div style='margin-top:12px;display:flex;gap:16px;flex-wrap:wrap;'>
                <div class='exec-stat-row' style='flex:1;border:none;padding:0;'>
                    <span class='exec-stat-label'>Avg / Day</span>
                    <span class='exec-stat-value'>{avg_daily}</span>
                </div>
                <div class='exec-stat-row' style='flex:1;border:none;padding:0;'>
                    <span class='exec-stat-label'>Total Orders</span>
                    <span class='exec-stat-value'>{orders_all_time:,}</span>
                </div>
            </div>
        </div>
        <div class='exec-chart-card'>
            <h4>&#9672; Workforce Workload by Responsibility</h4>
            {_chart_img(chart_labor)}
            <div style='margin-top:12px;'>
                {_progress("Operations Fill Rate", resp_counts[0] if resp_counts else 0, CHART_ACCENT, max(resp_counts) if resp_counts else 1)}
            </div>
        </div>
    </div>
    <div class='exec-chart-2col' style='margin-bottom:28px;'>
        <div class='exec-chart-card'>
            <h4>&#9672; Order Urgency Distribution</h4>
            {_chart_img(chart_urgency)}
        </div>
        <div class='exec-chart-card'>
            <h4>&#9672; Productivity Metrics</h4>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Total Orders Processed</span>
                <span class='exec-stat-value'>{orders_all_time:,}</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Orders Completed</span>
                <span class='exec-stat-value'>{shipped_total:,}</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Completion Rate</span>
                <span class='exec-stat-value'>{round((shipped_total / orders_all_time) * 100, 1) if orders_all_time else 0}%</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Avg Daily Volume</span>
                <span class='exec-stat-value'>{avg_daily}/day</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Quality Audit Pass Rate</span>
                <span class='exec-stat-value'>{pick_accuracy}%</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Inventory Accuracy</span>
                <span class='exec-stat-value'>{inv_accuracy}%</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>On-Time Order Rate</span>
                <span class='exec-stat-value'>{on_time_rate}%</span>
            </div>
            <div style='margin-top:14px;'>
                {_progress("Quality Audit Pass Rate", pick_accuracy, "#7c3aed")}
                {_progress("Inventory Accuracy", inv_accuracy, CHART_TEAL)}
                {_progress("On-Time Rate", on_time_rate, CHART_SUCCESS)}
            </div>
        </div>
    </div>
    <div class='exec-chart-card' style='margin-bottom:28px;'>
        <h4>&#9201; SLA Deadline Watchlist (At Risk &amp; Breached)</h4>
        {sla_watch_html}
    </div>
    </section>

    <section class='exec-view-section' data-section='inventory'>
    <!-- ── SECTION 4: INVENTORY HEALTH ── -->
    <div class='exec-section-label'>&#9632; Inventory Health</div>
    <h2 class='exec-section-title' style='margin-bottom:4px;'>Inventory Visibility</h2>
    <p class='exec-section-desc'>Stock valuation, distribution across warehouses, movement velocity, and stock condition.</p>
    <div class='exec-chart-2col' style='margin-bottom:24px;'>
        <div class='exec-chart-card'>
            <h4>&#9672; Top 10 SKUs by Inventory Value</h4>
            {_chart_img(chart_top_skus)}
        </div>
        <div class='exec-chart-card'>
            <h4>&#9672; Inventory Distribution by Warehouse</h4>
            {_chart_img(chart_wh_dist)}
        </div>
    </div>
    <div class='exec-chart-2col' style='margin-bottom:28px;'>
        <div class='exec-chart-card'>
            <h4>&#9632; Inventory Value by Warehouse</h4>
            {wh_table_html if wh_table_html else "<p style='color:#94a3b8;font-size:13px;'>No warehouse data.</p>"}
        </div>
        <div class='exec-chart-card'>
            <h4>&#9672; Top Moving SKUs (Transaction Activity)</h4>
            <div>
                {top_moving_html}
            </div>
            <div style='margin-top:16px;border-top:1px solid var(--line-100);padding-top:14px;'>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Total SKUs Tracked</span>
                    <span class='exec-stat-value'>{total_sku_count:,}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Low Stock SKUs (&lt;{DEFAULT_LOW_STOCK_ALERT_THRESHOLD})</span>
                    <span class='exec-stat-value' style='color:#f97316;'>{low_stock_count}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Overstock SKUs (&gt;5K)</span>
                    <span class='exec-stat-value' style='color:#6d28d9;'>{overstock_count}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Portfolio Value</span>
                    <span class='exec-stat-value'>{_fmt_currency(total_inv_value)}</span>
                </div>
            </div>
        </div>
    </div>
    <div class='exec-chart-card' style='margin-bottom:28px;'>
        <h4>&#128221; Open Orders SLA Table</h4>
        {open_sla_table_html}
    </div>
    </section>

    <section class='exec-view-section' data-section='exceptions'>
    <!-- ── SECTION 5: EXCEPTIONS & ALERTS ── -->
    <div class='exec-section-label'>&#9651; Exceptions &amp; Alerts</div>
    <h2 class='exec-section-title' style='margin-bottom:4px;'>Operational Exceptions</h2>
    <p class='exec-section-desc'>Active issues requiring executive attention, sorted by severity.</p>
    <div class='exec-exception-grid' style='margin-bottom:28px;'>
        <div class='exec-chart-card'>
            <h4>&#9888; Active Exceptions</h4>
            {exception_alerts_html}
            <div style='margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;'>
                <a href='/supervisor' style='padding:9px 14px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;font-weight:700;font-size:13px;color:#1d4ed8;text-decoration:none;'>Supervisor View ›</a>
                <a href='/quality' style='padding:9px 14px;background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;font-weight:700;font-size:13px;color:#b45309;text-decoration:none;'>Quality Queue ›</a>
                <a href='/inventory' style='padding:9px 14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;font-weight:700;font-size:13px;color:#166534;text-decoration:none;'>Inventory ›</a>
            </div>
        </div>
        <div class='exec-chart-card'>
            <h4>&#128683; Open Quality Issues</h4>
            {issues_table_html}
        </div>
        <div class='exec-chart-card'>
            <h4>&#128315; Low Stock Alerts</h4>
            {low_stock_html}
            <div style='margin-top:12px;font-size:12px;color:var(--ink-500);'>Threshold: &lt;{DEFAULT_LOW_STOCK_ALERT_THRESHOLD} units per SKU</div>
        </div>
    </div>
    </section>

    <section class='exec-view-section' data-section='shipping'>
    <!-- ── SECTION 6: SHIPMENT PERFORMANCE ── -->
    <div class='exec-section-label'>&#9650; Shipment Performance</div>
    <h2 class='exec-section-title' style='margin-bottom:4px;'>Shipping &amp; Delivery Performance</h2>
    <p class='exec-section-desc'>On-time rates, daily shipping volumes, and SLA compliance tracking.</p>
    <div class='exec-chart-2col' style='margin-bottom:28px;'>
        <div class='exec-chart-card'>
            <h4>&#9672; Daily Shipment Volume (Last 14 Days)</h4>
            {_chart_img(chart_ship_volume)}
            <div style='margin-top:12px;display:flex;gap:16px;flex-wrap:wrap;'>
                <div class='exec-stat-row' style='flex:1;border:none;padding:0;'>
                    <span class='exec-stat-label'>Avg Ships/Day</span>
                    <span class='exec-stat-value'>{avg_shipped_daily}</span>
                </div>
                <div class='exec-stat-row' style='flex:1;border:none;padding:0;'>
                    <span class='exec-stat-label'>Total Shipped</span>
                    <span class='exec-stat-value'>{shipped_total:,}</span>
                </div>
            </div>
        </div>
        <div class='exec-chart-card'>
            <h4>&#9675; Open Order SLA Health</h4>
            {_chart_img(chart_sla)}
            <div style='margin-top:10px;'>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>On-Time Rate</span>
                    <span class='exec-stat-value' style='color:{"#16a34a" if on_time_rate >= 90 else "#b45309"};'>{on_time_rate}%</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>SLA Breached</span>
                    <span class='exec-stat-value' style='color:#ef4444;'>{sla_breached}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>At Risk</span>
                    <span class='exec-stat-value' style='color:#f59e0b;'>{sla_at_risk}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Healthy</span>
                    <span class='exec-stat-value' style='color:#16a34a;'>{sla_healthy}</span>
                </div>
                {_progress("On-Time Rate", on_time_rate, CHART_SUCCESS)}
                {_progress("Healthy Orders", sla_healthy, CHART_SUCCESS, max(sla_total, 1))}
            </div>
        </div>
    </div>
    </section>

    <section class='exec-view-section' data-section='labor'>
    <!-- ── SECTION 7 & 8: LABOR + TRENDS ── -->
    <div class='exec-section-label'>&#9670; Labor &amp; Trends</div>
    <h2 class='exec-section-title' style='margin-bottom:4px;'>Labor Utilization &amp; Operational Trends</h2>
    <p class='exec-section-desc'>Team productivity, workload distribution by shift/role, and historical throughput trends.</p>
    <div class='exec-chart-2col' style='margin-bottom:28px;'>
        <div class='exec-chart-card'>
            <h4>&#128100; Workload by Responsibility</h4>
            <div>"""

    for label, count in zip(resp_labels, resp_counts):
        total_r = sum(resp_counts) if resp_counts else 1
        pct = round((count / total_r) * 100, 1)
        resp_colors = {"Operations": CHART_ACCENT, "Quality": CHART_WARNING, "Supervisor": CHART_DANGER, "Closed": CHART_SUCCESS}
        rc = resp_colors.get(label, "#475569")
        content += f"""
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>{label}</span>
                    <span style='display:flex;align-items:center;gap:10px;'>
                        <span style='width:90px;height:8px;border-radius:999px;background:#e9edf5;display:inline-block;overflow:hidden;'>
                            <span style='width:{pct}%;height:100%;background:{rc};display:block;border-radius:999px;'></span>
                        </span>
                        <span class='exec-stat-value' style='font-size:16px;'>{count} <span style='font-size:13px;font-weight:400;color:var(--ink-500);'>({pct}%)</span></span>
                    </span>
                </div>"""

    content += f"""
            </div>
            <div style='margin-top:16px;border-top:1px solid var(--line-100);padding-top:14px;'>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Total Active Assignments</span>
                    <span class='exec-stat-value'>{orders_pending}</span>
                </div>
                <div class='exec-stat-row'>
                    <span class='exec-stat-label'>Avg Orders per Role</span>
                    <span class='exec-stat-value'>{round(orders_all_time / max(len(resp_rows), 1), 1)}</span>
                </div>
            </div>
        </div>
        <div class='exec-chart-card'>
            <h4>&#128200; Operational Trend Summary</h4>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Order Volume (14-day avg)</span>
                <span class='exec-stat-value'>{avg_daily}/day</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Shipment Volume (14-day avg)</span>
                <span class='exec-stat-value'>{avg_shipped_daily}/day</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Fulfillment Rate</span>
                <span class='exec-stat-value'>{round((shipped_total / orders_all_time) * 100, 1) if orders_all_time else 0}%</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Quality Audit Pass Rate</span>
                <span class='exec-stat-value'>{pick_accuracy}%</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Inventory Portfolio Value</span>
                <span class='exec-stat-value'>{_fmt_currency(total_inv_value)}</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Low-Stock Alerts</span>
                <span class='exec-stat-value' style='color:#f97316;'>{low_stock_count} SKU(s)</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>Open Quality Issues</span>
                <span class='exec-stat-value' style='color:#ef4444;'>{len(open_issues)}</span>
            </div>
            <div class='exec-stat-row'>
                <span class='exec-stat-label'>SLA Compliance</span>
                <span class='exec-stat-value' style='color:{"#16a34a" if on_time_rate >= 90 else "#b45309"};'>{on_time_rate}%</span>
            </div>
            <div style='margin-top:16px;'>
                {_progress("Fulfillment Rate", shipped_total, CHART_SUCCESS, max(orders_all_time, 1))}
                {_progress("SLA Compliance", on_time_rate, CHART_SUCCESS)}
                {_progress("Quality Audit Pass Rate", pick_accuracy, CHART_VIOLET)}
            </div>
        </div>
    </div>

    <!-- ── QUICK LINKS ── -->
    <div class='card' style='background:linear-gradient(135deg,#f8fafc,#eff6ff);'>
        <div class='exec-section-label'>&#9654; Quick Actions</div>
        <h3 style='margin-bottom:12px;'>Navigate to Operational Modules</h3>
        <div class='quick-links'>
            <a class='quick-link' href='/executive?view=overview'>&#9672; Executive Overview</a>
            <a class='quick-link' href='/planner'>&#9672; Order Planner</a>
            <a class='quick-link' href='/inventory'>&#9632; Inventory</a>
            <a class='quick-link' href='/operations'>&#9654; Operations</a>
            <a class='quick-link' href='/quality'>&#9888; Quality</a>
            <a class='quick-link' href='/supervisor'>&#128100; Supervisor</a>
        </div>
    </div>

    <script>
        (function () {{
            function setExecutiveView(viewName) {{
                const sections = document.querySelectorAll('.exec-view-section');
                const buttons = document.querySelectorAll('.exec-switch-btn');
                let found = false;

                sections.forEach((section) => {{
                    const isActive = section.getAttribute('data-section') === viewName;
                    section.classList.toggle('active', isActive);
                    if (isActive) {{
                        found = true;
                    }}
                }});

                if (!found) {{
                    return;
                }}

                buttons.forEach((button) => {{
                    const isActive = button.getAttribute('data-target') === viewName;
                    button.classList.toggle('active', isActive);
                }});

                const url = new URL(window.location.href);
                url.searchParams.set('view', viewName);
                window.history.replaceState(null, '', url.toString());
                window.scrollTo({{ top: 0, behavior: 'smooth' }});
            }}

            window.setExecutiveView = setExecutiveView;

            document.querySelectorAll('.exec-switch-btn').forEach((button) => {{
                button.addEventListener('click', function () {{
                    setExecutiveView(this.getAttribute('data-target'));
                }});
            }});

            const requestedView = new URL(window.location.href).searchParams.get('view') || 'overview';
            setExecutiveView(requestedView);
        }})();
    </script>
    </section>
    """

    return layout(content)


# ======================================================
# STARTUP
# ======================================================
if __name__ == "__main__":
    migrated = migrate_legacy_db_to_master()
    if migrated:
        print("Promoted existing enterprise_wms.db into enterprise_wms_master.db seed template.")

    # Ensure the shared 82-order / 0-pending baseline. Re-seed only when master is
    # stale/missing; purge visitor clones when rebuilt so browsers pick up KPIs immediately.
    bootstrap_application(force_orders=not db_has_demo_baseline(MASTER_DB))

    host, port, debug = get_runtime_config(default_port=5000)
    selected_port = prepare_runtime_port(host, port, __file__)
    if selected_port != port:
        print(f"Port {port} is busy. Starting DigiTech WMS on port {selected_port} instead.")

    print(
        "Visitor demos use isolated SQLite files under demo_sessions/ cloned from the "
        "82-order completed master seed. Reset Demo restores that same baseline."
    )
    app.run(host=host, port=selected_port, debug=debug, use_reloader=False)
