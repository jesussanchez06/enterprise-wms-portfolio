"""Visitor isolation tests — Recruiter A must never affect Recruiter B."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

import whs_mgmt as wms


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sku_qty(conn, sku: str, warehouse: str | None = None) -> int:
    warehouse = warehouse or wms.SOURCE_WAREHOUSE
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM inventory WHERE sku = ? AND warehouse = ?",
        (sku, warehouse),
    ).fetchone()
    return int(row[0] or 0)


def _order_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM order_header").fetchone()[0] or 0)


def test_session_id_validation():
    assert wms.validate_demo_session_id("a" * 32)
    assert not wms.validate_demo_session_id("../evil")
    assert not wms.validate_demo_session_id("abc")
    assert not wms.validate_demo_session_id("a" * 31 + "g")


def test_two_visitors_isolated_orders_and_inventory():
    wms.bootstrap_application(force_orders=False)
    assert os.path.exists(wms.MASTER_DB)
    seed_hash_before = _file_sha256(wms.MASTER_DB)
    seed_orders_before = wms.sqlite_order_count(wms.MASTER_DB)

    client_a = wms.app.test_client()
    client_b = wms.app.test_client()

    # Separate cookie jars = separate demo sessions
    with client_a.session_transaction() as sess:
        sess["demo_session_id"] = "a" * 32
    with client_b.session_transaction() as sess:
        sess["demo_session_id"] = "b" * 32

    # Touch both sessions so DBs are cloned
    assert client_a.get("/executive").status_code == 200
    assert client_b.get("/executive").status_code == 200

    path_a = os.path.join(wms.SESSION_DB_DIR, f"{'a' * 32}.db")
    path_b = os.path.join(wms.SESSION_DB_DIR, f"{'b' * 32}.db")
    assert os.path.isfile(path_a) and os.path.isfile(path_b)
    assert os.path.abspath(path_a) != os.path.abspath(wms.MASTER_DB)

    conn_a = sqlite3.connect(path_a)
    conn_b = sqlite3.connect(path_b)
    try:
        sku = conn_a.execute(
            "SELECT sku FROM inventory WHERE warehouse = ? AND quantity > 5 ORDER BY sku LIMIT 1",
            (wms.SOURCE_WAREHOUSE,),
        ).fetchone()[0]
        qty_a0 = _sku_qty(conn_a, sku)
        qty_b0 = _sku_qty(conn_b, sku)
        assert qty_a0 == qty_b0 > 0
        orders_b0 = _order_count(conn_b)

        # Recruiter A creates an order that consumes 1 unit at source.
        with wms.app.test_request_context("/planner"):
            wms.session["demo_session_id"] = "a" * 32
            conn = wms.get_conn()
            try:
                assert os.path.abspath(wms.resolve_db_path()) == os.path.abspath(path_a)
                before = _sku_qty(conn, sku)
                conn.execute(
                    "UPDATE inventory SET quantity = quantity - 1 WHERE sku = ? AND warehouse = ? AND quantity > 0",
                    (sku, wms.SOURCE_WAREHOUSE),
                )
                order_no = "ORD-ISO-TEST-A1"
                conn.execute(
                    """
                    INSERT INTO order_header (
                        order_number, request_id, date, source, destination, urgency, status,
                        responsibility, expected_quantity, picked_quantity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_no,
                        "REQ-ISO-A1",
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
                conn.execute(
                    "INSERT INTO order_lines (order_number, sku, quantity) VALUES (?, ?, ?)",
                    (order_no, sku, 1),
                )
                conn.commit()
                after = _sku_qty(conn, sku)
                assert after == before - 1
            finally:
                conn.close()

        conn_a.close()
        conn_a = sqlite3.connect(path_a)
        conn_b.close()
        conn_b = sqlite3.connect(path_b)

        assert _sku_qty(conn_a, sku) == qty_a0 - 1
        # Recruiter B still at baseline inventory and does not see A's order.
        assert _sku_qty(conn_b, sku) == qty_b0
        assert _order_count(conn_b) == orders_b0
        assert (
            conn_b.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-ISO-TEST-A1",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn_a.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-ISO-TEST-A1",),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn_a.close()
        conn_b.close()

    # Active workspace must NOT be wiped just because it diverged from the 82-order tag.
    assert client_a.get("/executive").status_code == 200
    conn_a = sqlite3.connect(path_a)
    try:
        assert (
            conn_a.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-ISO-TEST-A1",),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn_a.close()

    # Seed integrity: master unchanged by visitor writes.
    assert _file_sha256(wms.MASTER_DB) == seed_hash_before
    assert wms.sqlite_order_count(wms.MASTER_DB) == seed_orders_before

    # Reset Demo affects only visitor A.
    with client_a.session_transaction() as sess:
        sess["demo_session_id"] = "a" * 32
    resp = client_a.post("/reset-demo", follow_redirects=False)
    assert resp.status_code in {302, 303}
    conn_a = sqlite3.connect(path_a)
    conn_b = sqlite3.connect(path_b)
    try:
        assert (
            conn_a.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-ISO-TEST-A1",),
            ).fetchone()[0]
            == 0
        )
        assert _sku_qty(conn_a, sku) == qty_a0
        # B untouched
        assert _sku_qty(conn_b, sku) == qty_b0
        assert (
            conn_b.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-ISO-TEST-A1",),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn_a.close()
        conn_b.close()

    assert _file_sha256(wms.MASTER_DB) == seed_hash_before


def test_visitor_never_resolves_to_master(monkeypatch=None):
    client = wms.app.test_client()
    with client.session_transaction() as sess:
        sess["demo_session_id"] = "c" * 32
    assert client.get("/executive").status_code == 200
    with wms.app.test_request_context("/executive"):
        wms.session["demo_session_id"] = "c" * 32
        path = wms.get_current_demo_db()
        assert os.path.abspath(path) != os.path.abspath(wms.MASTER_DB)
        conn = wms.get_conn()
        try:
            assert os.path.abspath(wms.resolve_db_path()) != os.path.abspath(wms.MASTER_DB)
        finally:
            conn.close()


def test_ttl_recreate_keeps_session_id_and_restores_master():
    import time

    wms.bootstrap_application(force_orders=False)
    seed_hash = _file_sha256(wms.MASTER_DB)
    seed_orders = wms.sqlite_order_count(wms.MASTER_DB)
    sid = "d" * 32
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
                "ORD-TTL-VISITOR-1",
                "REQ-TTL-V1",
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
    finally:
        conn.close()

    os.utime(path, (time.time() - wms.DEMO_SESSION_TTL_SECONDS - 60,) * 2)
    assert client.get("/ask-wms").status_code == 200
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM order_header WHERE order_number = ?",
                ("ORD-TTL-VISITOR-1",),
            ).fetchone()[0]
            == 0
        )
        assert _order_count(conn) == seed_orders
    finally:
        conn.close()
    assert _file_sha256(wms.MASTER_DB) == seed_hash


if __name__ == "__main__":
    test_session_id_validation()
    test_two_visitors_isolated_orders_and_inventory()
    test_visitor_never_resolves_to_master()
    test_ttl_recreate_keeps_session_id_and_restores_master()
    print("ALL VISITOR ISOLATION TESTS PASSED")
