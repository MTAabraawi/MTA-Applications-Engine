"""Apply independent source-worker JSONL patches in one serial collector."""
from __future__ import annotations

import json
import os
from pathlib import Path

from github_catalog_store import GitHubCatalogueStore


def main() -> dict:
    output_dir = Path(os.getenv("SOURCE_BATCH_OUTPUT_DIR", "catalogue_data/enrichment-batch-output"))
    patches = []
    source_summaries = []
    for path in sorted(output_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    patches.append(json.loads(line))
    for path in sorted(output_dir.glob("*.summary.json")):
        source_summaries.append(json.loads(path.read_text(encoding="utf-8")))

    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    result = store.upsert_many(patches, rebuild_indexes=False) if patches else {
        "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0
    }
    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir)

    summary = {
        "patch_records": len(patches),
        "store_write": result,
        "source_jobs": len(source_summaries),
        "sources": source_summaries,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()
