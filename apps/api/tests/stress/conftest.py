"""計算ストレステストの結果を `WARILINK_STRESS_REPORT`(既定 var/stress-report.json)に書き出す。"""

from __future__ import annotations

import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path

from .support import REPORT, SEED


def pytest_sessionfinish(session, exitstatus):
    if not REPORT:
        return
    path = Path(os.environ.get("WARILINK_STRESS_REPORT", "var/stress-report.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": SEED,
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "exit_status": int(exitstatus),
        "results": REPORT,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
