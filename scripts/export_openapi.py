"""FastAPI アプリから OpenAPI JSON を書き出す(サーバー起動不要)。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.main import app  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
out.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
print(f"wrote {out}")
