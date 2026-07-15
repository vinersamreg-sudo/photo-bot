import json
import sys
from pathlib import Path


report_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".lighthouse/report.json")
report = json.loads(report_path.read_text(encoding="utf-8"))
required = ("performance", "accessibility", "best-practices", "seo")
failed = []
for category in required:
    score = round(float(report["categories"][category]["score"]) * 100)
    print(f"{category}={score}")
    if score < 95:
        failed.append(f"{category}={score}")
if failed:
    raise SystemExit("Lighthouse budget failed: " + ", ".join(failed))
