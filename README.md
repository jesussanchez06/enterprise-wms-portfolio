# DigiTech WMS

A warehouse management system prototype built with Python and Flask — designed as a LinkedIn / recruiter demo with live Executive Dashboard KPIs and Ask WMS operational Q&A.

## Features

- Warehouse Executive Dashboard (real KPIs from SQLite records)
- Order Planning (blocks over-ordering against available inventory)
- Inventory Management
- Operations Workboard
- Quality Control
- Supervisor Control Tower
- Ask WMS (local operational intelligence assistant — no paid LLM APIs)
- Isolated per-visitor demo sessions with Reset Demo
- SLA Monitoring
- Productivity and Inventory Analytics

## Technologies

- Python
- Flask + Gunicorn
- SQLite
- Pandas
- Matplotlib

## Recruiter demo baseline (82 completed orders)

Every visitor starts from a shared master seed (`enterprise_wms_master.db`) with:

| KPI | Baseline |
| --- | --- |
| All-time orders | **82** |
| Orders Pending | **0** (all Completed / shipped) |
| Quality Audit Pass Rate | **>98%** (derived from `quality_audits`) |
| Open quality / shortage issues | **0** (historical exceptions are Resolved) |

KPIs are never hard-coded in the UI — they are calculated from order, audit, inventory, and transaction rows.

### Persistence model

1. **Master template** — `enterprise_wms_master.db` holds the completed 82-order baseline.
2. **Visitor isolation** — each browser gets a private clone under `demo_sessions/<session>.db`.
3. **Reset Demo** — reclones the same 82-order / 0-pending master (never an empty warehouse).
4. **Cold start / Render** — if the master DB is missing or off-baseline (common on free-tier ephemeral disk), `bootstrap_application()` rebuilds the full completed baseline automatically on process start or first request.
5. **Startup never wipes a healthy baseline** — reseeding only runs when the tagged 82-order completed set is absent.

### Render disk caveat

On Render’s free tier, the filesystem is ephemeral. After a deploy or idle spin-down the SQLite files may disappear. DigiTech WMS treats that as a cold start and **re-seeds the 82-order completed baseline** so recruiters still see live KPIs. For longer-lived state, attach a persistent disk or external DB later; the demo does not require it.

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

## Local demo

```bash
pip install -r requirements.txt
python whs_mgmt.py
```

Open:

- Executive Dashboard: `http://127.0.0.1:5000/executive`
- Ask WMS: `http://127.0.0.1:5000/ask-wms`

Phrase-variation intent tests:

```bash
python tests/test_ask_wms_intents.py
```

## Deploy (Render)

This branch includes:

- `Procfile` — gunicorn binding to `$PORT`
- `render.yaml` — Blueprint-style web service
- `runtime.txt` — Python 3.12
- `requirements.txt` — Flask, gunicorn, pandas, matplotlib, Werkzeug

### Steps

1. Push `wms-ai-upgrade` (or merge to the branch Render deploys from).
2. In Render: **New → Blueprint** (uses `render.yaml`) or **Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn whs_mgmt:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
3. Set `WMS_SECRET_KEY` (auto-generated via Blueprint) and `FLASK_DEBUG=0`.
4. After deploy, open `/executive` — expect **Orders Pending = 0**, **All-time = 82**, quality **>98%**.
5. Use **Reset Demo** anytime to restore the same baseline in your browser session.

Local PORT binding: `whs_mgmt.py` reads `PORT` / `HOST` via `get_runtime_config()` (default `5000` / `0.0.0.0`).

## Purpose

This project demonstrates how warehouse planning, inventory, operations, quality, and supervisory visibility can be brought together into one centralized system.

## Portfolio Project

Developed as an AI technology and business implementation project at Cal Poly Pomona.

Each browser visitor gets a private demo copy of the sample warehouse. No login and no paid APIs are required.
