"""Verify the 82-order completed recruiter baseline on master (+ optional sessions)."""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "enterprise_wms_master.db")
SESSIONS = os.path.join(ROOT, "demo_sessions")


def inspect(path: str) -> dict:
    out = {"path": path, "exists": os.path.exists(path)}
    if not out["exists"]:
        return out
    conn = sqlite3.connect(path)
    try:
        out["orders"] = conn.execute("SELECT COUNT(*) FROM order_header").fetchone()[0]
        out["tagged"] = conn.execute(
            "SELECT COUNT(*) FROM order_header WHERE order_number LIKE 'ORD-DT82-%'"
        ).fetchone()[0]
        out["completed"] = conn.execute(
            "SELECT COUNT(*) FROM order_header WHERE status = 'Completed'"
        ).fetchone()[0]
        out["pending"] = conn.execute(
            "SELECT COUNT(*) FROM order_header WHERE status != 'Completed'"
        ).fetchone()[0]
        out["audits"] = conn.execute("SELECT COUNT(*) FROM quality_audits").fetchone()[0]
        out["passed"] = conn.execute(
            "SELECT COUNT(*) FROM quality_audits WHERE result IN ('Pass', 'Passed')"
        ).fetchone()[0]
        out["failed"] = conn.execute(
            "SELECT COUNT(*) FROM quality_audits WHERE result = 'Failed'"
        ).fetchone()[0]
        out["open_issues"] = conn.execute(
            """
            SELECT COUNT(*) FROM supervisor_quality_issues
            WHERE issue_status NOT IN ('Closed', 'Resolved')
            """
        ).fetchone()[0]
        out["statuses"] = conn.execute(
            "SELECT status, COUNT(*) FROM order_header GROUP BY status"
        ).fetchall()
        audits = out["audits"] or 0
        out["pass_rate"] = round(100.0 * out["passed"] / audits, 1) if audits else None
    finally:
        conn.close()
    return out


def main() -> int:
    if "--reseed" in sys.argv:
        from whs_mgmt import bootstrap_application

        result = bootstrap_application(force_orders=True, purge_sessions=True)
        print("RESEED", {k: result.get(k) for k in (
            "seeded_orders", "baseline_orders", "baseline_ok", "purged_session_files", "reseeded"
        )})

    print("MASTER", inspect(MASTER))
    session_dbs = sorted(
        os.path.join(SESSIONS, name)
        for name in os.listdir(SESSIONS)
        if name.endswith(".db")
    ) if os.path.isdir(SESSIONS) else []
    print(f"SESSION_COUNT {len(session_dbs)}")
    for path in session_dbs[:5]:
        print("SESSION", inspect(path))

    master = inspect(MASTER)
    ok = (
        master.get("exists")
        and master.get("orders") == 82
        and master.get("tagged") == 82
        and master.get("completed") == 82
        and master.get("pending") == 0
        and (master.get("pass_rate") or 0) > 98
        and master.get("open_issues") == 0
    )
    print("BASELINE_OK" if ok else "BASELINE_BAD")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
