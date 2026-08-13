"""Phrase-variation tests for Ask WMS intent classification (zero paid APIs)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wms_ask_concepts import CONCEPTS, SPELLING_FIXES, normalize_question
from wms_ask_intents import INTENT_DEFS, classify_intent


def _intent(question: str, prior=None) -> str:
    return classify_intent(question, prior_context=prior)["intent"]


def _result(question: str, prior=None):
    return classify_intent(question, prior_context=prior)


# ---------------------------------------------------------------------------
# Parametrized family coverage
# ---------------------------------------------------------------------------

RECOMMENDED_ACTIONS = [
    "What are the top three recommended actions?",
    "Top 3 actions?",
    "Give me three priorities.",
    "What should we do first?",
    "What needs attention?",
    "What should the supervisor focus on?",
    "Where should we focus right now?",
    "Biggest priorities?",
    "top recomendations",
    "Priorities?",
    "Actions?",
    "triage the floor",
    "what first?",
]

SHIPPING_TODAY = [
    "How many orders shipped today?",
    "Today's shipments?",
    "What went out today?",
    "Anything ship today?",
    "How many orders were dispatched?",
    "how many order shiped today?",
    "Shipments today?",
    "outbound today",
]

INVENTORY_ACCURACY = [
    "What's our stock accuracy?",
    "Inventory accuracy?",
    "How accurate is inventory?",
    "What's the current inventory accuracy rate?",
    "inventroy accuracy",
    "bin accuracy",
]

SLA_BREACHED = [
    "What's late?",
    "Any overdue orders?",
    "Which orders breached SLA?",
    "Anything past deadline?",
    "Late?",
    "overude orders",
    "breched sla",
]

SLA_AT_RISK = [
    "Which orders are at risk?",
    "Anything about to be late?",
    "SLA risk?",
    "close to deadline",
]

SLA_SUMMARY = [
    "SLA summary",
    "How is SLA compliance?",
    "service level overview",
    "SLA?",
]

SLA_HEALTHY = [
    "Which orders are SLA healthy?",
    "healthy sla orders",
    "still healthy",
]

OTIF = [
    "What's our OTIF?",
    "OTIF?",
    "on time in full rate",
    "perfect order rate",
    "otfi performance",
]

TOP_QTY = [
    "which part number has the most inventory?",
    "What sku has the highest stock?",
    "top parts by quantity",
    "Which SKU has the most inventory?",
]

LEAST_QTY = [
    "which sku has the lowest?",
    "Which SKU has the least inventory?",
    "which has the lowest stock?",
    "fewest units",
]

TOP_VALUE = [
    "which SKU is worth the most?",
    "Which SKU has the highest inventory value?",
    "top sku by value",
    "most valuable SKU",
]

PICKER_COUNT = [
    "how many pickers do you have?",
    "How many active pickers?",
    "list pickers",
    "Who are the pickers?",
    "Pickers?",
]

WAREHOUSE_SUMMARY = [
    "How is the warehouse performing today?",
    "Give me a warehouse summary.",
    "How is the warehouse doing?",
    "Executive summary",
    "Risks?",
    "Status?",
    "overview",
    "what's going on",
    "anything wrong?",
    "how we doing",
    "warehouse health",
    "operations dashboard",
]

KPI_SNAPSHOT = [
    "what are the kpis",
    "What are the KPIs?",
    "show me today's KPIs",
    "give me the important KPIs",
    "today's kpis",
    "KPI",
    "KPIs?",
    "Show warehouse KPIs",
    "current KPIs",
]

NATURAL_LANGUAGE = [
    "what's going on",
    "anything wrong",
    "healthy?",
    "fail today?",
    "behind?",
    "worried?",
    "how we doing",
]

EXECUTIVE_EXTRA = [
    "summarize today",
    "completion rate",
    "KPI needing attention",
    "vs yesterday",
    "biggest risk",
]

ORDER_LOOKUP_DT82 = [
    "ORD-DT82-0036",
    "details for ORD-DT82-0036",
    "where is ORD-DT82-0036",
    "status DT82-0036",
    "order DT82-0036",
    "is ORD-DT82-0036 delayed",
]

CROSS_MODULE_EXTRA = [
    "quality and picker",
    "picker quality issues",
    "warehouse delays",
    "SKUs causing delays",
    "blocked by inventory",
    "shortages to SLA",
]

ANALYTICS_PERIOD = [
    "this week",
    "this month",
    "trends",
]

SKU_MOVEMENT = [
    "slow-moving SKUs",
    "SKU that moved most",
    "moved least",
    "inventory movement",
]

PICKER_LEADERS = [
    "who completed most",
    "who completed fewest",
]

OPS_PHRASES = [
    "ready picks",
    "ops summary",
    "avg orders per day",
    "utilization",
]

PLANNER = [
    "Planner summary",
    "urgency mix",
    "workload by destination",
    "ship first",
    "inventory need",
]

SUPERVISOR = [
    "Supervisor summary",
    "control tower status",
    "needs my attention",
    "alerts",
    "shortages",
]

CROSS_OPS = [
    "What's the bottleneck?",
    "quality blocking shipping",
    "inventory causing sla delays",
    "what's blocking fulfillment",
    "quality to shipping",
]

AI_BRIEFING = [
    "What should I focus on today?",
    "summarize performance",
    "biggest risk right now",
    "vs yesterday",
    "30 minute review first",
    "why delays",
    "improve what",
    "investigate first",
]

KPI_ATTENTION = [
    "Which KPI needs attention?",
    "kpi needs attention",
    "which metric needs attention",
    "KPI needing attention",
    "KPIs below target",
]

PRODUCTIVITY = [
    "What's our productivity?",
    "units per hour",
    "picker productivity",
    "picks per hour",
    "throughput",
    "utilization",
]

WAREHOUSE_COUNT = [
    "how many warehouses do i have?",
    "How many warehouses?",
    "list my warehouses",
    "How many sites do I have?",
    "which warehouses do I have?",
    "warehouse count",
    "show my warehouses",
]

WAREHOUSE_COMPARE = [
    "Compare warehouses",
    "inventory by warehouse",
    "which warehouse has the most value?",
    "site comparison",
    "value by site",
]

ORDERS_OPEN = [
    "How many orders are open?",
    "How many open orders?",
    "Orders open?",
    "Any pending orders?",
    "active orders",
]

ORDERS_CREATED = [
    "How many orders were created today?",
    "orders received today",
    "new orders today",
    "demand today",
]

ORDERS_COMPLETED = [
    "How many completed orders?",
    "orders completed",
    "finished orders",
]

ORDERS_OLDEST = [
    "What are the oldest open orders?",
    "longest waiting order",
    "aging backlog",
    "oldest order",
]

REMAINING_WORK = [
    "What's remaining work?",
    "work remaining",
    "what's left to do?",
    "outstanding work",
    "Backlog?",
    "open workload",
]

QUALITY = [
    "Any quality problems?",
    "What's our quality pass rate?",
    "Any open quality issues?",
    "quailty issues",
    "Quality?",
]

SHIPPING_SUMMARY = [
    "Shipping summary",
    "ready to ship",
    "shipping queue",
    "Shipping?",
]

INVENTORY_SUMMARY = [
    "Inventory summary",
    "current inventory",
    "Inventory?",
    "Stock?",
]

LOW_STOCK = [
    "low stock",
    "running low",
    "out of stock skus",
    "need restock",
]

SKU_OPEN = [
    "open orders using SKU 11000",
    "Any open orders for SKU 11000?",
    "Which orders use SKU 11000?",
    "orders requesting 11000",
]

WRITE_BLOCKED = [
    "Ship order 1045",
    "Add 100 units to SKU 11000",
    "Delete order ORD-1",
    "Create order for SKU 11000",
    "Update inventory for 11000",
    "mark completed ORD-1",
    "reassign picker on ORD-1",
]

UNSUPPORTED = [
    "Forecast next week's demand",
    "Who should we fire?",
    "Predict if we will miss SLA next month",
    "Should we hire more pickers next week?",
]

KPI_DEFS = [
    "What is SLA?",
    "Define OTIF",
    "What is inventory accuracy?",
    "kpi definition",
    "meaning of otif",
]

HELP = [
    "Help?",
    "What can Ask WMS answer?",
    "What is DigiTech WMS?",
    "Is Ask WMS read only?",
    "capabilities",
]

ADJUSTMENTS = [
    "Any inventory adjustments?",
    "recent stock adjustments",
    "cycle count corrections",
]

CRITICAL = [
    "critical orders",
    "critical open jobs",
]

BLOCKED = [
    "Blocked?",
    "blocked orders",
    "stuck orders",
]


@pytest.mark.parametrize("phrase", RECOMMENDED_ACTIONS)
def test_recommended_actions(phrase):
    assert _intent(phrase) == "recommended_actions", phrase


@pytest.mark.parametrize("phrase", SHIPPING_TODAY)
def test_shipping_today(phrase):
    assert _intent(phrase) == "shipping_today", phrase


@pytest.mark.parametrize("phrase", INVENTORY_ACCURACY)
def test_inventory_accuracy(phrase):
    assert _intent(phrase) == "inventory_accuracy", phrase


@pytest.mark.parametrize("phrase", SLA_BREACHED)
def test_sla_breached(phrase):
    assert _intent(phrase) == "sla_breached", phrase


@pytest.mark.parametrize("phrase", SLA_AT_RISK)
def test_sla_at_risk(phrase):
    assert _intent(phrase) == "sla_at_risk", phrase


@pytest.mark.parametrize("phrase", SLA_SUMMARY)
def test_sla_summary(phrase):
    assert _intent(phrase) == "sla_summary", phrase


@pytest.mark.parametrize("phrase", SLA_HEALTHY)
def test_sla_healthy(phrase):
    assert _intent(phrase) == "sla_healthy", phrase


@pytest.mark.parametrize("phrase", OTIF)
def test_otif(phrase):
    assert _intent(phrase) == "otif_summary", phrase


@pytest.mark.parametrize("phrase", TOP_QTY)
def test_top_inventory_qty(phrase):
    assert _intent(phrase) == "top_inventory_qty", phrase


@pytest.mark.parametrize("phrase", LEAST_QTY)
def test_least_inventory_qty(phrase):
    assert _intent(phrase) == "least_inventory_qty", phrase


@pytest.mark.parametrize("phrase", TOP_VALUE)
def test_top_inventory_value(phrase):
    assert _intent(phrase) == "top_inventory_value", phrase


@pytest.mark.parametrize("phrase", PICKER_COUNT)
def test_picker_count(phrase):
    assert _intent(phrase) == "picker_count", phrase


@pytest.mark.parametrize("phrase", WAREHOUSE_SUMMARY)
def test_warehouse_summary(phrase):
    assert _intent(phrase) == "warehouse_summary"


@pytest.mark.parametrize("phrase", KPI_SNAPSHOT)
def test_kpi_snapshot(phrase):
    assert _intent(phrase) == "kpi_snapshot", phrase


@pytest.mark.parametrize("phrase", WAREHOUSE_COUNT)
def test_warehouse_count(phrase):
    assert _intent(phrase) == "warehouse_count", phrase


@pytest.mark.parametrize("phrase", WAREHOUSE_COMPARE)
def test_warehouse_compare(phrase):
    assert _intent(phrase) == "warehouse_compare", phrase


@pytest.mark.parametrize("phrase", ORDERS_OPEN)
def test_orders_open(phrase):
    assert _intent(phrase) == "orders_open", phrase


@pytest.mark.parametrize("phrase", ORDERS_CREATED)
def test_orders_created_today(phrase):
    assert _intent(phrase) == "orders_created_today", phrase


@pytest.mark.parametrize("phrase", ORDERS_COMPLETED)
def test_orders_completed(phrase):
    assert _intent(phrase) == "orders_completed", phrase


@pytest.mark.parametrize("phrase", ORDERS_OLDEST)
def test_orders_oldest(phrase):
    assert _intent(phrase) == "orders_oldest", phrase


@pytest.mark.parametrize("phrase", REMAINING_WORK)
def test_remaining_work(phrase):
    assert _intent(phrase) == "remaining_work", phrase


@pytest.mark.parametrize("phrase", QUALITY)
def test_quality_summary(phrase):
    assert _intent(phrase) == "quality_summary", phrase


@pytest.mark.parametrize("phrase", SHIPPING_SUMMARY)
def test_shipping_summary(phrase):
    assert _intent(phrase) == "shipping_summary", phrase


@pytest.mark.parametrize("phrase", INVENTORY_SUMMARY)
def test_inventory_summary(phrase):
    assert _intent(phrase) == "inventory_summary", phrase


@pytest.mark.parametrize("phrase", LOW_STOCK)
def test_low_stock(phrase):
    assert _intent(phrase) == "low_stock", phrase


@pytest.mark.parametrize("phrase", SKU_OPEN)
def test_sku_open_orders(phrase):
    result = _result(phrase)
    assert result["intent"] == "sku_open_orders", (phrase, result["intent"])
    assert result["entities"].get("sku") == "11000", phrase


@pytest.mark.parametrize("phrase", WRITE_BLOCKED)
def test_write_blocked(phrase):
    assert _intent(phrase) == "write_blocked", phrase


@pytest.mark.parametrize("phrase", UNSUPPORTED)
def test_unsupported_predictive(phrase):
    assert _intent(phrase) == "unsupported_predictive", phrase


@pytest.mark.parametrize("phrase", PRODUCTIVITY)
def test_productivity_limits(phrase):
    assert _intent(phrase) == "productivity_limits", phrase


@pytest.mark.parametrize("phrase", CROSS_OPS)
def test_cross_ops_risk(phrase):
    assert _intent(phrase) == "cross_ops_risk", phrase


@pytest.mark.parametrize("phrase", KPI_DEFS)
def test_kpi_definitions(phrase):
    assert _intent(phrase) == "kpi_definitions", phrase


@pytest.mark.parametrize("phrase", HELP)
def test_help_explain(phrase):
    assert _intent(phrase) == "help_explain", phrase


@pytest.mark.parametrize("phrase", PLANNER)
def test_planner_summary(phrase):
    assert _intent(phrase) == "planner_summary", phrase


@pytest.mark.parametrize("phrase", SUPERVISOR)
def test_supervisor_summary(phrase):
    assert _intent(phrase) == "supervisor_summary", phrase


@pytest.mark.parametrize("phrase", ADJUSTMENTS)
def test_inventory_adjustments(phrase):
    assert _intent(phrase) == "inventory_adjustments", phrase


@pytest.mark.parametrize("phrase", CRITICAL)
def test_critical_orders(phrase):
    assert _intent(phrase) == "critical_orders", phrase


@pytest.mark.parametrize("phrase", BLOCKED)
def test_blocked_short(phrase):
    result = _result(phrase)
    assert result["intent"] == "orders_by_status", (phrase, result["intent"])
    assert result["entities"].get("status_hint") == "Blocked" or "blocked" in result["normalized"]


# ---------------------------------------------------------------------------
# Entities, follow-ups, neighbors, catalog
# ---------------------------------------------------------------------------

def test_sku_and_order_entities():
    result = _result("How many parts left for SKU 11000?")
    assert result["intent"] == "sku_inventory"
    assert result["entities"].get("sku") == "11000"

    result = _result("What's the status of order ORD-20260101120000?")
    assert result["intent"] == "order_status"
    assert "ORD-20260101120000" in str(result["entities"].get("order_id"))


def test_order_id_not_fuzzy_sku():
    result = _result("status of order ORD-20260101120000")
    assert result["entities"].get("order_id")
    # ORD token path should not invent a fuzzy SKU match from the numeric tail alone as primary intent.
    assert result["intent"] == "order_status"


def test_sku_open_orders_follow_up():
    prior = {"sku": "11000", "intent": "sku_inventory", "entities": {"sku": "11000"}}
    result = _result("Any open orders using it?", prior=prior)
    assert result["intent"] == "sku_open_orders"
    assert result["entities"].get("sku") == "11000"


def test_inventory_ranking_follow_ups():
    prior = {
        "intent": "top_inventory_qty",
        "intent_family": "inventory_ranking",
        "entities": {},
    }
    assert _result("the lowest?", prior=prior)["intent"] == "least_inventory_qty"
    assert _result("which has the least?", prior=prior)["intent"] == "least_inventory_qty"
    assert _result("in terms of value money?", prior=prior)["intent"] == "top_inventory_value"
    assert _result("in terms of value?", prior=prior)["intent"] == "top_inventory_value"


def test_which_ones_follow_up():
    prior = {
        "intent": "sla_breached",
        "intent_family": "sla",
        "entities": {"order_ids": ["ORD-1"]},
    }
    result = _result("which ones?", prior=prior)
    assert result["intent"] == "sla_breached"
    assert result["entities"].get("follow_up") is True


def test_critical_ones_follow_up_after_orders():
    prior = {
        "intent": "orders_open",
        "intent_family": "orders",
        "entities": {},
    }
    result = _result("critical ones?", prior=prior)
    assert result["intent"] == "critical_orders"


def test_its_warehouse_follow_up():
    prior = {"sku": "11000", "intent": "sku_inventory", "entities": {"sku": "11000"}}
    result = _result("its warehouse?", prior=prior)
    assert result["intent"] == "sku_inventory"
    assert result["entities"].get("sku") == "11000"


def test_order_follow_up_sla():
    prior = {
        "order_id": "ORD-20260101120000",
        "intent": "order_status",
        "entities": {"order_id": "ORD-20260101120000"},
    }
    result = _result("is it late?", prior=prior)
    assert result["intent"] == "order_status"
    assert result["entities"].get("order_id") == "ORD-20260101120000"


def test_top_n_extraction():
    result = _result("Give me top 5 actions")
    assert result["intent"] == "recommended_actions"
    assert result["entities"].get("limit") == 5


def test_warehouse_count_neighbors():
    assert _intent("How many SKUs?") == "sku_count"
    assert _intent("How many orders are open?") == "orders_open"
    assert _intent("How many pickers do you have?") == "picker_count"


def test_typo_normalization():
    assert "inventory" in normalize_question("inventroy summary")
    assert "shipped" in normalize_question("shiped today")
    assert "critical" in normalize_question("critcal orders")
    assert "otif" in normalize_question("otfi rate")


def test_casual_recruiter_language():
    assert _intent("yo what's late in the warehouse") == "sla_breached"
    assert _intent("give me the vibe — how are we doing") in {
        "warehouse_summary",
        "recommended_actions",
    }
    assert _intent("show me the hot priorities") == "recommended_actions"
    assert _intent("anything wrong?") == "warehouse_summary"
    assert _intent("what's going on") == "warehouse_summary"


def test_ambiguous_and_unknown():
    result = _result("Tell me about orders.")
    assert result["confidence"] in {"medium", "low", "high"}
    assert result["intent"] in {
        "orders_open",
        "orders_by_status",
        "warehouse_summary",
        "unknown",
        "remaining_work",
    }

    result = _result("What's the weather in the warehouse?")
    assert result["intent"] == "unknown" or result["confidence"] == "low"


QUALITY_FAILED = [
    "Which audit failed?",
    "Which order failed inspection?",
    "Why did the audit fail?",
    "Which order has a quality issue?",
    "Which order is the one with the mistake?",
    "What discrepancy was found?",
    "carton damage?",
    "carton corner crush?",
]

QUALITY_LIST = [
    "Show failed audits",
    "quality history",
    "today's audits",
    "list failed audits",
    "show quality issues",
]

QUALITY_AUDITOR = [
    "Who audited?",
    "Which auditor?",
    "who inspected?",
]

AVG_COMPLETION = [
    "What's the average completion time?",
    "avg completion time",
    "average cycle time",
]

CONTEXT_PICKER = [
    "Who picked it?",
    "who picked that order?",
]

CONTEXT_SKU = [
    "What SKU was involved?",
    "which sku was involved?",
]

CONTEXT_SHIPPED = [
    "Was it shipped?",
    "did it ship?",
]


@pytest.mark.parametrize("phrase", QUALITY_FAILED)
def test_quality_failed_detail(phrase):
    assert _intent(phrase) == "quality_failed_detail", phrase


@pytest.mark.parametrize("phrase", QUALITY_LIST)
def test_quality_audits_list(phrase):
    assert _intent(phrase) == "quality_audits_list", phrase


@pytest.mark.parametrize("phrase", QUALITY_AUDITOR)
def test_quality_auditor(phrase):
    assert _intent(phrase) == "quality_auditor", phrase


@pytest.mark.parametrize("phrase", KPI_ATTENTION)
def test_kpi_attention(phrase):
    assert _intent(phrase) == "kpi_attention", phrase


@pytest.mark.parametrize("phrase", AI_BRIEFING)
def test_ai_briefing(phrase):
    assert _intent(phrase) == "ai_briefing", phrase


@pytest.mark.parametrize("phrase", AVG_COMPLETION)
def test_avg_completion_time(phrase):
    assert _intent(phrase) == "avg_completion_time", phrase


def test_ord_dt82_entity_extraction():
    result = _result("What's the status of order ORD-DT82-0081?")
    assert result["intent"] == "order_status"
    assert result["entities"].get("order_id") == "ORD-DT82-0081"

    result = _result("Tell me about ORD-DT82-0036")
    assert result["entities"].get("order_id") == "ORD-DT82-0036"

    result = _result("status DT82-0036")
    assert result["entities"].get("order_id") == "ORD-DT82-0036"


def test_quality_follow_ups_with_context():
    prior = {
        "intent": "quality_failed_detail",
        "intent_family": "quality",
        "order_id": "ORD-DT82-0081",
        "sku": "11043",
        "picker": "Maria Alvarez",
        "entities": {"order_id": "ORD-DT82-0081", "sku": "11043", "picker": "Maria Alvarez"},
    }
    assert _result("Who picked it?", prior=prior)["intent"] == "context_picker"
    assert _result("Who picked it?", prior=prior)["entities"].get("order_id") == "ORD-DT82-0081"
    assert _result("What SKU was involved?", prior=prior)["intent"] == "context_sku"
    assert _result("Was it shipped?", prior=prior)["intent"] == "context_shipped"
    assert _result("Who audited?", prior=prior)["intent"] == "quality_auditor"


@pytest.mark.parametrize("phrase", CONTEXT_PICKER)
def test_context_picker_intent(phrase):
    assert _intent(phrase) == "context_picker", phrase


@pytest.mark.parametrize("phrase", CONTEXT_SKU)
def test_context_sku_intent(phrase):
    assert _intent(phrase) == "context_sku", phrase


@pytest.mark.parametrize("phrase", CONTEXT_SHIPPED)
def test_context_shipped_intent(phrase):
    assert _intent(phrase) == "context_shipped", phrase


@pytest.mark.parametrize("phrase", NATURAL_LANGUAGE)
def test_natural_language_pulse(phrase):
    assert _intent(phrase) == "warehouse_summary", phrase


@pytest.mark.parametrize("phrase", EXECUTIVE_EXTRA)
def test_executive_extra_intents(phrase):
    intent = _intent(phrase)
    assert intent in {
        "warehouse_summary",
        "ai_briefing",
        "kpi_attention",
        "kpi_snapshot",
    }, (phrase, intent)


@pytest.mark.parametrize("phrase", ORDER_LOOKUP_DT82)
def test_order_lookup_dt82_intents(phrase):
    result = _result(phrase)
    assert result["intent"] == "order_status", (phrase, result["intent"])
    assert result["entities"].get("order_id") == "ORD-DT82-0036", phrase


def test_order_lookup_facets_dt82():
    assert _intent("who picked ORD-DT82-0036") == "context_picker"
    assert _result("who picked ORD-DT82-0036")["entities"].get("order_id") == "ORD-DT82-0036"
    assert _intent("was ORD-DT82-0036 shipped") == "context_shipped"
    assert _intent("SKUs on ORD-DT82-0036") == "context_sku"
    assert _intent("who verified ORD-DT82-0036") == "quality_auditor"


@pytest.mark.parametrize("phrase", CROSS_MODULE_EXTRA)
def test_cross_module_extra(phrase):
    assert _intent(phrase) == "cross_ops_risk", phrase


@pytest.mark.parametrize("phrase", ANALYTICS_PERIOD)
def test_analytics_period(phrase):
    assert _intent(phrase) == "analytics_period", phrase


@pytest.mark.parametrize("phrase", SKU_MOVEMENT)
def test_sku_movement(phrase):
    assert _intent(phrase) == "sku_movement", phrase


@pytest.mark.parametrize("phrase", PICKER_LEADERS)
def test_picker_leaders(phrase):
    assert _intent(phrase) == "picker_leaders", phrase


@pytest.mark.parametrize("phrase", OPS_PHRASES)
def test_ops_phrases(phrase):
    intent = _intent(phrase)
    assert intent in {
        "picking_summary",
        "warehouse_summary",
        "productivity_limits",
        "orders_completed",
        "remaining_work",
    }, (phrase, intent)


def test_intent_catalog_size():
    assert len(INTENT_DEFS) >= 52


def test_concept_and_spelling_coverage():
    assert len(CONCEPTS) >= 22
    assert len(SPELLING_FIXES) >= 40


def test_phrase_variation_count_floor():
    """Guardrail: keep substantial phrase-variation coverage across families."""
    buckets = [
        RECOMMENDED_ACTIONS,
        SHIPPING_TODAY,
        INVENTORY_ACCURACY,
        SLA_BREACHED,
        SLA_AT_RISK,
        SLA_SUMMARY,
        SLA_HEALTHY,
        OTIF,
        TOP_QTY,
        LEAST_QTY,
        TOP_VALUE,
        PICKER_COUNT,
        WAREHOUSE_SUMMARY,
        KPI_SNAPSHOT,
        NATURAL_LANGUAGE,
        EXECUTIVE_EXTRA,
        ORDER_LOOKUP_DT82,
        CROSS_MODULE_EXTRA,
        ANALYTICS_PERIOD,
        SKU_MOVEMENT,
        PICKER_LEADERS,
        OPS_PHRASES,
        WAREHOUSE_COUNT,
        WAREHOUSE_COMPARE,
        ORDERS_OPEN,
        ORDERS_CREATED,
        ORDERS_COMPLETED,
        ORDERS_OLDEST,
        REMAINING_WORK,
        QUALITY,
        QUALITY_FAILED,
        QUALITY_LIST,
        QUALITY_AUDITOR,
        SHIPPING_SUMMARY,
        INVENTORY_SUMMARY,
        LOW_STOCK,
        SKU_OPEN,
        WRITE_BLOCKED,
        UNSUPPORTED,
        PRODUCTIVITY,
        CROSS_OPS,
        KPI_DEFS,
        HELP,
        PLANNER,
        SUPERVISOR,
        ADJUSTMENTS,
        CRITICAL,
        BLOCKED,
        KPI_ATTENTION,
        AI_BRIEFING,
        AVG_COMPLETION,
        CONTEXT_PICKER,
        CONTEXT_SKU,
        CONTEXT_SHIPPED,
    ]
    total = sum(len(b) for b in buckets)
    assert total >= 220, total


if __name__ == "__main__":
    failed = 0
    cases = []
    for name, phrases, expected in [
        ("recommended_actions", RECOMMENDED_ACTIONS, "recommended_actions"),
        ("shipping_today", SHIPPING_TODAY, "shipping_today"),
        ("sla_breached", SLA_BREACHED, "sla_breached"),
        ("otif", OTIF, "otif_summary"),
        ("write", WRITE_BLOCKED, "write_blocked"),
        ("unsupported", UNSUPPORTED, "unsupported_predictive"),
    ]:
        for phrase in phrases:
            got = _intent(phrase)
            if got != expected:
                failed += 1
                cases.append((name, phrase, got, expected))
    test_inventory_ranking_follow_ups()
    test_sku_and_order_entities()
    test_intent_catalog_size()
    test_phrase_variation_count_floor()
    if failed:
        print("FAILURES:")
        for row in cases:
            print(row)
        raise SystemExit(1)
    print(
        f"SMOKE PASSED — catalog={len(INTENT_DEFS)} intents, "
        f"concepts={len(CONCEPTS)}, spelling_fixes={len(SPELLING_FIXES)}"
    )
