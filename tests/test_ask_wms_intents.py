"""Phrase-variation tests for Ask WMS intent classification (zero paid APIs)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wms_ask_intents import INTENT_DEFS, classify_intent


def _intent(question: str) -> str:
    return classify_intent(question)["intent"]


def test_recommended_actions_variations():
    phrases = [
        "What are the top three recommended actions?",
        "Top 3 actions?",
        "Give me three priorities.",
        "What should we do first?",
        "What needs attention?",
        "What should the supervisor focus on?",
        "Where should we focus right now?",
        "Biggest priorities?",
        "top recomendations",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "recommended_actions", phrase


def test_shipping_today_variations():
    phrases = [
        "How many orders shipped today?",
        "Today's shipments?",
        "What went out today?",
        "Anything ship today?",
        "How many orders were dispatched?",
        "how many order shiped today?",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "shipping_today", phrase


def test_inventory_accuracy_variations():
    phrases = [
        "What's our stock accuracy?",
        "Inventory accuracy?",
        "How accurate is inventory?",
        "What's the current inventory accuracy rate?",
        "inventroy accuracy",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "inventory_accuracy", phrase


def test_sla_breached_variations():
    phrases = [
        "What's late?",
        "Any overdue orders?",
        "Which orders breached SLA?",
        "Anything past deadline?",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "sla_breached", phrase


def test_top_inventory_variations():
    phrases = [
        "which part number has the most inventory?",
        "What sku has the highest stock?",
        "top parts by quantity",
        "Which SKU has the most inventory?",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "top_inventory_qty", phrase


def test_warehouse_summary_variations():
    phrases = [
        "How is the warehouse performing today?",
        "Give me a warehouse summary.",
        "How is the warehouse doing?",
        "Executive summary",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "warehouse_summary", phrase


def test_quality_summary_variations():
    phrases = [
        "Any quality problems?",
        "What's our quality pass rate?",
        "Any open quality issues?",
        "quailty issues",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "quality_summary", phrase


def test_sku_and_order_entities():
    result = classify_intent("How many parts left for SKU 11000?")
    assert result["intent"] == "sku_inventory"
    assert result["entities"].get("sku") == "11000"

    result = classify_intent("What's the status of order ORD-20260101120000?")
    assert result["intent"] == "order_status"
    assert "ORD-20260101120000" in str(result["entities"].get("order_id"))


def test_write_requests_blocked_intent():
    phrases = [
        "Ship order 1045",
        "Add 100 units to SKU 11000",
        "Delete order ORD-1",
    ]
    for phrase in phrases:
        assert _intent(phrase) == "write_blocked", phrase


def test_top_n_extraction():
    result = classify_intent("Give me top 5 actions")
    assert result["intent"] == "recommended_actions"
    assert result["entities"].get("limit") == 5


def test_ambiguous_and_unknown():
    result = classify_intent("Tell me about orders.")
    assert result["confidence"] in {"medium", "low", "high"}
    assert result["intent"] in {"orders_open", "orders_by_status", "warehouse_summary", "unknown"}

    result = classify_intent("What's the weather in the warehouse?")
    assert result["intent"] == "unknown" or result["confidence"] == "low"


def test_intent_catalog_size():
    assert len(INTENT_DEFS) >= 20


if __name__ == "__main__":
    test_recommended_actions_variations()
    test_shipping_today_variations()
    test_inventory_accuracy_variations()
    test_sla_breached_variations()
    test_top_inventory_variations()
    test_warehouse_summary_variations()
    test_quality_summary_variations()
    test_sku_and_order_entities()
    test_write_requests_blocked_intent()
    test_top_n_extraction()
    test_ambiguous_and_unknown()
    test_intent_catalog_size()
    print(f"ALL INTENT VARIATION TESTS PASSED ({len(INTENT_DEFS)} intents in catalog)")
