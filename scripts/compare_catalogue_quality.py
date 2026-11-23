"""Compare catalogue metadata coverage against the pre-audit main-branch snapshot."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def compare_stats(before, after):
    fields = sorted(set(before.get("quality", {}).get("fields", {})) | set(after.get("quality", {}).get("fields", {})))
    overall = {}
    for field in fields:
        old = before.get("quality", {}).get("fields", {}).get(field, {})
        new = after.get("quality", {}).get("fields", {}).get(field, {})
        overall[field] = {
            "before_present": int(old.get("present", 0)),
            "after_present": int(new.get("present", 0)),
            "present_delta": int(new.get("present", 0)) - int(old.get("present", 0)),
            "before_percent": float(old.get("percent", 0)),
            "after_percent": float(new.get("percent", 0)),
            "percentage_point_delta": round(float(new.get("percent", 0)) - float(old.get("percent", 0)), 2),
        }

    before_sources = before.get("quality", {}).get("sources", {})
    after_sources = after.get("quality", {}).get("sources", {})
    sources = {}
    for source in sorted(set(before_sources) | set(after_sources)):
        old_source = before_sources.get(source, {})
        new_source = after_sources.get(source, {})
        source_fields = {}
        for field in fields:
            old = old_source.get("fields", {}).get(field, {})
            new = new_source.get("fields", {}).get(field, {})
            source_fields[field] = {
                "before_present": int(old.get("present", 0)),
                "after_present": int(new.get("present", 0)),
                "present_delta": int(new.get("present", 0)) - int(old.get("present", 0)),
                "before_percent": float(old.get("percent", 0)),
                "after_percent": float(new.get("percent", 0)),
                "percentage_point_delta": round(float(new.get("percent", 0)) - float(old.get("percent", 0)), 2),
            }
        sources[source] = {
            "before_total": int(old_source.get("total", 0)),
            "after_total": int(new_source.get("total", 0)),
            "fields": source_fields,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": "main branch snapshot captured before enrichment",
        "before_total": int(before.get("total", 0)),
        "after_total": int(after.get("total", 0)),
        "total_delta": int(after.get("total", 0)) - int(before.get("total", 0)),
        "overall": overall,
        "by_source": sources,
        "policy": "Coverage changes are measured from source-supplied values only; missing fields are not fabricated.",
    }


def main():
    baseline = Path(os.getenv("CATALOGUE_QUALITY_BASELINE", "reports/catalogue-quality-baseline.json"))
    current = Path(os.getenv("CATALOGUE_QUALITY_CURRENT", "catalogue_data/indexes/stats.json"))
    output = Path(os.getenv("CATALOGUE_QUALITY_COMPARISON", "reports/catalogue-quality-comparison.json"))
    before_data = json.loads(baseline.read_text(encoding="utf-8"))
    after_data = json.loads(current.read_text(encoding="utf-8"))
    report = compare_stats(before_data, after_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "before_total": report["before_total"],
        "after_total": report["after_total"],
        "total_delta": report["total_delta"],
        "overall": report["overall"],
        "report": str(output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
