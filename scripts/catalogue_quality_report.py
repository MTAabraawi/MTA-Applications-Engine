"""Evidence-based quality audit for the Git-backed application catalogue.

The report distinguishes absent metadata from malformed metadata. It does not
invent values or treat every package/extension record as a GUI application.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_catalog_store import GitHubCatalogueStore


FIELDS = ("icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url")
PLACEHOLDERS = {"", "unknown", "n/a", "na", "not available", "not provided", "not specified", "none", "null", "tbd", "-"}


def present(value):
    return value is not None and (not isinstance(value, str) or value.strip().casefold() not in PLACEHOLDERS)


def valid_https(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def substantive(value, minimum):
    return isinstance(value, str) and len(" ".join(value.split())) >= minimum


def build_report(records, generated_at=None):
    global_counts = defaultdict(int)
    sources = {}
    platforms = {}
    malformed = []
    total = 0

    def bucket():
        return {"total": 0, "fields": {
            field: {"present": 0, "missing": 0, "malformed": 0}
            for field in FIELDS
        }, "substantive": {"description_40_chars": 0, "summary_20_chars": 0}}

    source_buckets = defaultdict(bucket)
    platform_buckets = defaultdict(bucket)

    for record in records:
        total += 1
        source = str(record.get("source_key") or "unknown")
        platform = str(record.get("platform") or "unknown")
        targets = (source_buckets[source], platform_buckets[platform])
        for target in targets:
            target["total"] += 1

        for field in FIELDS:
            value = record.get(field)
            is_present = present(value)
            is_url = field.endswith("_url")
            is_valid = valid_https(value) if is_url and is_present else (is_present if not is_url else False)
            for target in targets:
                stats = target["fields"][field]
                if not is_present:
                    stats["missing"] += 1
                elif is_valid:
                    stats["present"] += 1
                else:
                    stats["malformed"] += 1
            if is_present and not is_valid:
                global_counts[f"malformed_{field}"] += 1
                if len(malformed) < 100:
                    malformed.append({
                        "source_key": source,
                        "external_id": str(record.get("external_id") or ""),
                        "field": field,
                    })

        if substantive(record.get("description"), 40):
            global_counts["substantive_descriptions"] += 1
            for target in targets:
                target["substantive"]["description_40_chars"] += 1
        if substantive(record.get("summary"), 20):
            global_counts["substantive_summaries"] += 1
            for target in targets:
                target["substantive"]["summary_20_chars"] += 1

        if not present(record.get("name")) or not valid_https(record.get("source_url")):
            global_counts["invalid_required_records"] += 1

    def finalize(items):
        output = {}
        for key, item in sorted(items.items()):
            fields = {}
            for field, counts in item["fields"].items():
                fields[field] = {
                    **counts,
                    "present_percent": round(counts["present"] * 100 / item["total"], 2) if item["total"] else 0,
                }
            output[key] = {
                "total": item["total"],
                "fields": fields,
                "substantive": item["substantive"],
            }
        return output

    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "total_records": total,
        "global": {
            **dict(sorted(global_counts.items())),
            "invalid_required_records": global_counts["invalid_required_records"],
            "malformed_url_or_metadata_fields": sum(
                value for key, value in global_counts.items() if key.startswith("malformed_")
            ),
        },
        "by_source": finalize(source_buckets),
        "by_platform": finalize(platform_buckets),
        "malformed_samples": malformed,
        "policy": {
            "no_metadata_is_fabricated": True,
            "missing_icons_are_not_automatically_errors": True,
            "non_gui_packages_and_extensions_are_not_forced_to_have_app_icons": True,
            "url_fields_require_absolute_https_urls": True,
            "substantive_description_minimum_characters": 40,
            "substantive_summary_minimum_characters": 20,
            "source_interpretation": {
                "bluejetty": "Application listings; direct source details and exact official store listing identifiers are used for enrichment.",
                "fedora-packages": "Linux package records include libraries and non-GUI components; missing application icons are not automatically defects.",
                "snap-store": "Icons are accepted only from Snap Store media entries explicitly typed as icon.",
                "steam": "Descriptions and publisher data come from Steam appdetails; header and capsule artwork are not substituted as application icons.",
            },
        },
    }


def main():
    root = Path(__sys_env("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    output = Path(__sys_env("CATALOGUE_QUALITY_REPORT", "catalogue-quality-report.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    store = GitHubCatalogueStore(root)
    report = build_report(store.iter_records())
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(output),
        "total_records": report["total_records"],
        "global": report["global"],
        "source_count": len(report["by_source"]),
        "platform_count": len(report["by_platform"]),
        "malformed_sample_count": len(report["malformed_samples"]),
    }, ensure_ascii=False, indent=2))
    if report["total_records"] == 0:
        raise SystemExit("Catalogue quality audit found no records.")
    if report["global"].get("invalid_required_records", 0):
        raise SystemExit("Catalogue contains records without required names or HTTPS source URLs.")


def __sys_env(key, default):
    import os
    return os.getenv(key, default)


if __name__ == "__main__":
    main()
