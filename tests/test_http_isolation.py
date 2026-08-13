"""HTTP-level dual-visitor isolation: Planner order must not leak across sessions."""

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


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def test_http_planner_order_isolated_between_visitors():
    wms.bootstrap_application(force_orders=False)
    seed_hash = _sha(wms.MASTER_DB)

    a = wms.app.test_client()
    b = wms.app.test_client()
    with a.session_transaction() as sess:
        sess["demo_session_id"] = "11111111111111111111111111111111"
    with b.session_transaction() as sess:
        sess["demo_session_id"] = "22222222222222222222222222222222"

    assert a.get("/executive").status_code == 200
    assert b.get("/executive").status_code == 200
    assert b"Private Demo Session" in a.get("/executive").data

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

        # Visitor DB paths never equal master
        assert os.path.abspath(path_a) != os.path.abspath(wms.MASTER_DB)
        assert os.path.abspath(path_b) != os.path.abspath(wms.MASTER_DB)
    finally:
        conn_a.close()
        conn_b.close()

    # Ask WMS KPI question works on visitor session
    ask = a.post("/ask-wms", data={"question": "what are the kpis"}, follow_redirects=True)
    assert ask.status_code == 200
    body = ask.data.decode("utf-8", errors="ignore").lower()
    assert "kpi" in body or "quality audit pass rate" in body or "inventory value" in body
    assert "i can answer operational warehouse questions" not in body or "quality audit pass rate" in body


if __name__ == "__main__":
    test_http_planner_order_isolated_between_visitors()
    print("HTTP ISOLATION + KPI ASK PASSED")
