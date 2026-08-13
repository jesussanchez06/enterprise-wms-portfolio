"""Integration tests: quality answers + cross-module follow-ups against demo seed DB."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

import whs_mgmt as wms
import wms_ask_engine as engine
import wms_ask_queries as queries


def _demo_conn():
    """Prefer a seeded demo session DB; fall back to master."""
    session_dir = Path(wms.SESSION_DB_DIR)
    candidates = sorted(session_dir.glob("*.db")) if session_dir.is_dir() else []
    for path in candidates:
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT 1 FROM quality_audits WHERE order_number = 'ORD-DT82-0081' AND result = 'Failed'"
        ).fetchone()
        if row:
            return conn
        conn.close()
    if os.path.isfile(wms.MASTER_DB):
        conn = sqlite3.connect(wms.MASTER_DB)
        row = conn.execute(
            "SELECT 1 FROM quality_audits WHERE order_number = 'ORD-DT82-0081' AND result = 'Failed'"
        ).fetchone()
        if row:
            return conn
        conn.close()
    pytest.skip("No seeded demo DB with ORD-DT82-0081 failed audit")


def _answer(conn, question, prior=None):
    return engine.answer_question(
        conn,
        question,
        prior_context=prior or {},
        warehouses=list(wms.WAREHOUSE_NETWORK),
        source_warehouse=wms.SOURCE_WAREHOUSE,
        low_stock_threshold=wms.DEFAULT_LOW_STOCK_ALERT_THRESHOLD,
        shortage_issue_type=wms.SHORTAGE_ISSUE_TYPE,
        picker_roster=list(wms.PICKER_ROSTER),
        now_pt=wms.now_pt,
        parse_order_datetime=wms.parse_order_datetime,
        normalize_urgency=wms.normalize_urgency,
        calculate_sla_status=wms.calculate_sla_status,
        calculate_sla_deadline=wms.calculate_sla_deadline,
        sku_description=wms.sku_semiconductor_description,
    )


@pytest.fixture()
def conn():
    c = _demo_conn()
    try:
        yield c
    finally:
        c.close()


def test_failed_audit_helpers(conn):
    rows = queries.list_failed_audits(conn, limit=5)
    assert rows
    assert any(r["order_number"] == "ORD-DT82-0081" for r in rows)
    lead = queries.primary_failed_audit(conn)
    assert lead["order_number"] == "ORD-DT82-0081"
    assert "carton" in (lead.get("note") or "").lower() or "crush" in (lead.get("note") or "").lower()


@pytest.mark.parametrize(
    "question",
    [
        "Which audit failed?",
        "Which order failed inspection?",
        "Why did the audit fail?",
        "Which order has a quality issue?",
        "Which order is the one with the mistake?",
        "What discrepancy was found?",
        "carton damage?",
    ],
)
def test_quality_failed_answers(conn, question):
    html, _snap, context = _answer(conn, question)
    low = html.lower()
    assert "ord-dt82-0081" in low
    assert "fail" in low or "quality" in low
    assert "carton" in low or "crush" in low or "damage" in low
    assert context.get("order_id") == "ORD-DT82-0081"
    assert context.get("sku") == "11043" or context.get("picker")


def test_quality_auditor_answer(conn):
    html, _, context = _answer(conn, "Who audited?")
    low = html.lower()
    assert "quality" in low
    assert context.get("order_id") == "ORD-DT82-0081"


def test_quality_history_and_failed_list(conn):
    html, _, ctx = _answer(conn, "Show failed audits")
    assert "ord-dt82-0081" in html.lower()
    assert ctx.get("order_id") == "ORD-DT82-0081"

    html, _, ctx = _answer(conn, "quality history")
    assert "ord-dt82-0081" in html.lower() or "damage" in html.lower()


def test_cross_module_follow_ups(conn):
    _html, _, context = _answer(conn, "Which order is the one with the mistake?")
    assert context.get("order_id") == "ORD-DT82-0081"

    html, _, ctx = _answer(conn, "Who picked it?", prior=context)
    assert "maria" in html.lower()
    assert ctx.get("picker")

    html, _, ctx = _answer(conn, "What SKU was involved?", prior=context)
    assert "11043" in html
    assert ctx.get("sku") == "11043"

    html, _, ctx = _answer(conn, "Was it shipped?", prior=context)
    assert "completed" in html.lower() or "ship" in html.lower()
    assert "ord-dt82-0081" in html.lower()

    # Context must survive an intervening KPI question.
    _html, _, mid = _answer(conn, "What are the KPIs?", prior=context)
    assert mid.get("order_id") == "ORD-DT82-0081"
    html, _, ctx = _answer(conn, "Was it shipped?", prior=mid)
    assert "ord-dt82-0081" in html.lower()
    assert "completed" in html.lower() or "yes" in html.lower()


def test_kpi_snapshot_and_attention(conn):
    html, _, _ = _answer(conn, "What are the KPIs?")
    low = html.lower()
    assert "quality audit pass rate" in low or "inventory" in low
    assert "completion" in low or "sla" in low

    html, _, _ = _answer(conn, "Which KPI needs attention?")
    assert "kpi" in html.lower() or "attention" in html.lower() or "sla" in html.lower()


def test_executive_and_ai_phrases(conn):
    html, _, _ = _answer(conn, "Executive summary")
    assert "warehouse" in html.lower() or "order" in html.lower()

    html, _, _ = _answer(conn, "What should I focus on today?")
    assert "focus" in html.lower() or "action" in html.lower() or "risk" in html.lower()


def test_avg_completion_time_honest(conn):
    html, _, _ = _answer(conn, "What's the average completion time?")
    low = html.lower()
    assert "completion" in low or "hour" in low or "not available" in low


def test_order_dt82_0036_status(conn):
    html, _, context = _answer(conn, "status of order ORD-DT82-0036")
    assert "ord-dt82-0036" in html.lower()
    assert context.get("order_id") == "ORD-DT82-0036"


def test_order_dt82_0036_who_picked(conn):
    html, _, context = _answer(conn, "who picked ORD-DT82-0036")
    low = html.lower()
    assert "ord-dt82-0036" in low
    assert "sophia" in low or "picked" in low
    assert context.get("order_id") == "ORD-DT82-0036"
    assert context.get("picker")


def test_order_dt82_bare_prefix(conn):
    html, _, context = _answer(conn, "status DT82-0036")
    assert "ord-dt82-0036" in html.lower()
    assert context.get("order_id") == "ORD-DT82-0036"


def test_natural_language_anything_wrong(conn):
    html, _, _ = _answer(conn, "anything wrong?")
    low = html.lower()
    assert "order" in low or "warehouse" in low or "sla" in low or "blocked" in low


def test_cross_module_phrases(conn):
    html, _, _ = _answer(conn, "blocked by inventory")
    assert "bottleneck" in html.lower() or "blocked" in html.lower() or "sla" in html.lower()


def test_analytics_period_honest(conn):
    html, _, _ = _answer(conn, "this week")
    low = html.lower()
    assert "not fully supported" in low or "insufficient" in low or "yesterday" in low


def test_answer_ask_wms_wrapper(conn):
    html, snap, context = wms.answer_ask_wms(conn, "Which audit failed?")
    assert "ORD-DT82-0081" in html
    assert "carton" in html.lower() or "crush" in html.lower()
    assert isinstance(snap, dict)
    assert context.get("order_id") == "ORD-DT82-0081"
