# DigiTech WMS

A warehouse management system prototype built with Python and Flask.

## Features

- Warehouse Executive Dashboard
- Order Planning (blocks over-ordering against available inventory)
- Inventory Management
- Operations Workboard
- Quality Control
- Supervisor Control Tower
- Ask WMS (local operational intelligence assistant)
- Isolated per-visitor demo sessions with Reset Demo
- SLA Monitoring
- Productivity and Inventory Analytics

## Technologies

- Python
- Flask
- SQLite
- Pandas
- Matplotlib

## Ask WMS

Ask WMS uses intent recognition, entity extraction, controlled database queries, and deterministic operational rules to provide natural-language warehouse decision support without requiring an external paid AI service.

It can interpret multiple phrasings of the same operational question (for example “Top 3 actions?” and “What should we focus on?”), query the current visitor’s isolated demo data, summarize warehouse status, surface SLA/inventory/quality risks, and recommend priorities with explicit reasons.

Ask WMS is read-only: natural-language questions cannot create orders, adjust inventory, pick, audit, ship, or reset data. Use the normal role workflows for changes.

### Architecture

1. **Normalize + extract** (`wms_ask_concepts.py`) — spelling fixes, concept hits, entities (SKU, order id, urgency, date, warehouse, top-N).
2. **Classify intent** (`wms_ask_intents.py`) — weighted phrase/concept scoring with follow-up context and write-block priority.
3. **Safe query** (`wms_ask_queries.py`) — read-only SQL against the visitor session SQLite DB only.
4. **Recommend** (`wms_ask_recommendations.py`) — deterministic priority rules (critical SLA breach/risk, quality, shortages, blocked, pending QA, low stock, backlog).
5. **Compose answer** (`wms_ask_engine.py` via `answer_ask_wms()` in `whs_mgmt.py`) — headline, bullets, follow-up prompt, and session context for “which ones?” / “orders using it?”.

### What it can answer

- Warehouse / executive overview
- Orders open, by status, critical, and single-order status
- Shipping/completed counts for today (or a named date)
- SLA healthy / at risk / breached
- Inventory summary, value, accuracy, low stock, SKU rankings, SKU availability, open demand by SKU
- Operations / picking backlog
- Quality pass rate and open issues
- Supervisor recommended actions
- Planner destination / urgency mix
- Help / workflow explanations

### Local demo

Run `python whs_mgmt.py`, then open `http://127.0.0.1:5000/ask-wms`.

Phrase-variation intent tests: `python tests/test_ask_wms_intents.py`.

## Purpose

This project demonstrates how warehouse planning, inventory, operations, quality, and supervisory visibility can be brought together into one centralized system.

## Portfolio Project

Developed as an AI technology and business implementation project at Cal Poly Pomona.

Each browser visitor gets a private demo copy of the sample warehouse. No login and no paid APIs are required.
