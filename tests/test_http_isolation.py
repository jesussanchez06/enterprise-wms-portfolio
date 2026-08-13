"""HTTP-level dual-visitor isolation: Planner order must not leak across sessions."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import whs_mgmt as wms


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cookie_header(response, name: str) -> str:
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header
    return ""


def _client_cookie(client, name: str):
    getter = getattr(client, "get_cookie", None)
    if callable(getter):
        cookie = getter(name)
        if cookie is not None:
            return getattr(cookie, "value", cookie)
    jar = getattr(client, "_cookies", None) or {}
    for cookie in jar.values() if isinstance(jar, dict) else jar:
        key = getattr(cookie, "key", None) or getattr(cookie, "name", None)
        if key == name:
            return cookie.value
    return None


def test_http_planner_order_isolated_between_visitors():
    wms.bootstrap_application(force_orders=False)
    seed_hash = _sha(wms.MASTER_DB)
    seed_orders = wms.sqlite_order_count(wms.MASTER_DB)

    a = wms.app.test_client()
    b = wms.app.test_client()
    with a.session_transaction() as sess:
        sess["demo_session_id"] = "11111111111111111111111111111111"
    with b.session_transaction() as sess:
        sess["demo_session_id"] = "22222222222222222222222222222222"

    exec_a = a.get("/executive")
    exec_b = b.get("/executive")
    assert exec_a.status_code == 200
    assert exec_b.status_code == 200
    assert b"Private Demo Session" not in exec_a.data
    # Reset Demo UI removed from nav; /reset-demo route remains for optional later use
    assert b"Reset Demo" not in a.get("/executive").data
    assert b'action="/reset-demo"' not in a.get("/executive").data

    cookie_a = _cookie_header(exec_a, wms.DEMO_SESSION_COOKIE)
    assert "11111111111111111111111111111111" in cookie_a
    assert "HttpOnly" in cookie_a
    assert "SameSite=Lax" in cookie_a or "SameSite=lax" in cookie_a

    path_a = os.path.join(wms.SESSION_DB_DIR, "11111111111111111111111111111111.db")
    path_b = os.path.join(wms.SESSION_DB_DIR, "22222222222222222222222222222222.db")
    assert os.path.isfile(path_a) and os.path.isfile(path_b)

    conn_a = sqlite3.connect(path_a)
    sku, qty = conn_a.execute(
        """
        SELECT sku, COALESCE(SUM(quantity),0)
        FROM inventory
        WHERE warehouse = ? AND quantity >= 5
        GROUP BY sku
        ORDER BY sku
        LIMIT 1
        """,
        (wms.SOURCE_WAREHOUSE,),
    ).fetchone()
    orders_a0 = conn_a.execute("SELECT COUNT(*) FROM order_header").fetchone()[0]
    conn_a.close()

    resp = a.post(
        "/planner",
        data={
            "destination": "Los Angeles Warehouse 200",
            "urgency": "Critical",
            "sku_1": sku,
            "qty_1": "3",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn_a = sqlite3.connect(path_a)
    conn_b = sqlite3.connect(path_b)
    try:
        orders_a1 = conn_a.execute("SELECT COUNT(*) FROM order_header").fetchone()[0]
        assert orders_a1 == orders_a0 + 1
        critical_a = conn_a.execute(
            "SELECT COUNT(*) FROM order_header WHERE urgency = 'Critical' AND status != 'Completed'"
        ).fetchone()[0]
        assert critical_a >= 1

        # B still baseline order count and does not see A's new critical open order surge
        # as a shared DB would if both wrote master.
        orders_b = conn_b.execute("SELECT COUNT(*) FROM order_header").fetchone()[0]
        assert orders_b == orders_a0  # B never created; still seed count
        # Master untouched
        assert _sha(wms.MASTER_DB) == seed_hash
        assert wms.sqlite_order_count(wms.MASTER_DB) == orders_a0
        assert wms.sqlite_order_count(wms.MASTER_DB) == seed_orders

        # Visitor DB paths never equal master
        assert os.path.abspath(path_a) != os.path.abspath(wms.MASTER_DB)
        assert os.path.abspath(path_b) != os.path.abspath(wms.MASTER_DB)
    finally:
        conn_a.close()
        conn_b.close()

    # Ask WMS KPI question works on visitor session and sees A's extra order, not B's baseline.
    ask_a = a.post("/ask-wms", data={"question": "what are the kpis"}, follow_redirects=True)
    ask_b = b.post("/ask-wms", data={"question": "what are the kpis"}, follow_redirects=True)
    assert ask_a.status_code == 200
    assert ask_b.status_code == 200
    body_a = ask_a.data.decode("utf-8", errors="ignore").lower()
    body_b = ask_b.data.decode("utf-8", errors="ignore").lower()
    assert "kpi" in body_a or "quality audit pass rate" in body_a or "inventory value" in body_a
    assert "i can answer operational warehouse questions" not in body_a or "quality audit pass rate" in body_a
    assert f"{orders_a0 + 1} total order" in body_a
    assert f"{orders_a0} total order" in body_b
    assert f"{orders_a0 + 1} total order" not in body_b

    assert _sha(wms.MASTER_DB) == seed_hash


def test_first_visit_clones_master_and_sets_cookie():
    wms.bootstrap_application(force_orders=False)
    seed_hash = _sha(wms.MASTER_DB)
    client = wms.app.test_client()
    resp = client.get("/executive")
    assert resp.status_code == 200
    header = _cookie_header(resp, wms.DEMO_SESSION_COOKIE)
    assert header
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header or "SameSite=lax" in header
    sid = _client_cookie(client, wms.DEMO_SESSION_COOKIE)
    assert sid and wms.validate_demo_session_id(sid)
    path = os.path.join(wms.SESSION_DB_DIR, f"{sid}.db")
    assert os.path.isfile(path)
    assert os.path.abspath(path) != os.path.abspath(wms.MASTER_DB)
    assert wms.sqlite_order_count(path) == wms.sqlite_order_count(wms.MASTER_DB)
    assert _sha(wms.MASTER_DB) == seed_hash


def test_ttl_expiry_reclones_session_db_from_master():
    """Expired session DBs are replaced from master; the session ID/cookie is kept."""
    wms.bootstrap_application(force_orders=False)
    seed_hash = _sha(wms.MASTER_DB)
    seed_orders = wms.sqlite_order_count(wms.MASTER_DB)
    sid = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    client = wms.app.test_client()
    with client.session_transaction() as sess:
        sess["demo_session_id"] = sid
    assert client.get("/executive").status_code == 200

    path = os.path.join(wms.SESSION_DB_DIR, f"{sid}.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO order_header (
                order_number, request_id, date, source, destination, urgency, status,
                responsibility, expected_quantity, picked_quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ORD-TTL-EXPIRE-1",
                "REQ-TTL-1",
                wms.now_pt().isoformat(),
                wms.SOURCE_WAREHOUSE,
                "Los Angeles Warehouse 200",
                "Standard",
                "Orders Placed",
                "Planner",
                1,
                0,
            ),
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-TTL-EXPIRE-1",),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    expired_mtime = time.time() - (wms.DEMO_SESSION_TTL_SECONDS + 120)
    os.utime(path, (expired_mtime, expired_mtime))
    assert wms.visitor_db_is_expired(path)

    assert client.get("/executive").status_code == 200
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-TTL-EXPIRE-1",),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM order_header").fetchone()[0] == seed_orders
    finally:
        conn.close()

    assert _client_cookie(client, wms.DEMO_SESSION_COOKIE) == sid
    assert _sha(wms.MASTER_DB) == seed_hash
    assert wms.sqlite_order_count(wms.MASTER_DB) == seed_orders


if __name__ == "__main__":
    test_http_planner_order_isolated_between_visitors()
    test_first_visit_clones_master_and_sets_cookie()
    test_ttl_expiry_reclones_session_db_from_master()
    print("HTTP ISOLATION + KPI ASK PASSED")
