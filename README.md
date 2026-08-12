# Enterprise Warehouse Management System

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

## Purpose

This project demonstrates how warehouse planning, inventory, operations, quality, and supervisory visibility can be brought together into one centralized system.

## Portfolio Project

Developed as an AI technology and business implementation project at Cal Poly Pomona.

Each browser visitor gets a private demo copy of the sample warehouse. No login and no paid APIs are required.
