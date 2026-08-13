"""Fetch /executive and print recruiter KPI values."""
from __future__ import annotations

import http.cookiejar
import os
import re
import sys
import urllib.request

URL = os.environ.get("WMS_EXEC_URL", "http://127.0.0.1:5000/executive")


def main() -> int:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    html = opener.open(URL, timeout=45).read().decode("utf-8", errors="replace")

    def after_label(label: str) -> str | None:
        pattern = (
            re.escape(label)
            + r"</div>\s*<div class='exec-kpi-number'>([^<]+)"
        )
        match = re.search(pattern, html)
        return match.group(1).strip() if match else None

    pending = after_label("Orders Pending")
    quality = after_label("Quality Audit Pass Rate")
    received = after_label("Orders Received Today")
    shipped = after_label("Orders Shipped Today")
    alltime = re.search(r"All-time(?: total)?: ([0-9,]+)", html)
    alltime_val = alltime.group(1) if alltime else None

    print("pending", pending)
    print("quality", quality)
    print("received_kpi", received)
    print("shipped_kpi", shipped)
    print("alltime_note", alltime_val)
    print("clear_badge", "Clear" in html and (pending == "0"))

    sessions = "demo_sessions"
    session_count = (
        len([n for n in os.listdir(sessions) if n.endswith(".db")])
        if os.path.isdir(sessions)
        else 0
    )
    print("session_dbs", session_count)

    quality_num = float(str(quality).rstrip("%")) if quality else 0.0
    ok = pending == "0" and alltime_val == "82" and quality_num > 98.0
    print("EXEC_OK" if ok else "EXEC_BAD")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
