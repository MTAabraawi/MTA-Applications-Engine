"""Resumable per-source official-page enrichment worker.

Each matrix job owns one source and emits JSONL patches only. It never writes
catalogue shards or pushes commits; a single collector applies all patches.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from github_catalog_store import GitHubCatalogueStore
from scripts.enrich_catalogue_official_pages import (
    FIELDS, USER_AGENT, _metadata_object, _retry_due,
    parse_official_metadata, valid_https,
)

ROOT = Path(__file__).resolve().parents[1]


def candidates_for_source(records, source_key: str, now: datetime) -> list[dict]:
    selected = []
    for row in records:
        if row.get("source_key") != source_key or not valid_https(row.get("source_url")):
            continue
        if not any(not row.get(field) for field in FIELDS):
            continue
        if _retry_due(_metadata_object(row.get("metadata_json")), now):
            selected.append(row)
    selected.sort(key=lambda row: str(
        _metadata_object(row.get("metadata_json")).get("generic_detail_last_attempt_at") or ""
    ))
    return selected


async def run_source_batch() -> dict:
    source = os.environ["MTA_ENRICH_SOURCE"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", source):
        raise ValueError(f"Invalid source key: {source!r}")

    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    now = datetime.now(timezone.utc)
    rows = list(store.iter_records())
    candidates = candidates_for_source(rows, source, now)

    batch_size = max(10, min(int(os.getenv("SOURCE_BATCH_SIZE", "300")), 1000))
    concurrency = max(1, min(int(os.getenv("SOURCE_BATCH_CONCURRENCY", "8")), 16))
    runtime_minutes = max(5, min(int(os.getenv("SOURCE_BATCH_RUNTIME_MINUTES", "300")), 320))
    deadline = time.monotonic() + runtime_minutes * 60

    output_dir = Path(os.getenv("SOURCE_BATCH_OUTPUT_DIR", "catalogue_data/enrichment-batch-output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source}.jsonl"
    output_path.write_text("", encoding="utf-8")

    totals = {
        "source_key": source, "candidates": len(candidates), "checked": 0,
        "enriched": 0, "retryable": 0, "unavailable": 0,
        "fields_filled": {}, "batches": 0, "remaining_at_deadline": 0,
    }
    timeout = httpx.Timeout(25, connect=10)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers) as client:
        with output_path.open("a", encoding="utf-8") as output:
            for offset in range(0, len(candidates), batch_size):
                if time.monotonic() >= deadline:
                    break
                batch = candidates[offset:offset + batch_size]

                async def fetch_one(row):
                    current_url = row["source_url"]
                    original_host = urlsplit(current_url).hostname
                    try:
                        for _ in range(4):
                            response = await client.get(current_url)
                            if response.status_code in {301, 302, 303, 307, 308}:
                                target = urljoin(current_url, response.headers.get("Location", ""))
                                if not valid_https(target) or urlsplit(target).hostname != original_host:
                                    return row, {}, "unavailable"
                                current_url = target
                                continue
                            if response.status_code in {400, 401, 403, 404, 410}:
                                return row, {}, "unavailable"
                            response.raise_for_status()
                            break
                        else:
                            return row, {}, "retryable"
                        if "html" not in response.headers.get("content-type", "").lower():
                            return row, {}, "unavailable"
                        return row, parse_official_metadata(response.text, str(response.url)), None
                    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError):
                        return row, {}, "retryable"
                    except (ValueError, UnicodeError):
                        return row, {}, "unavailable"

                semaphore = asyncio.Semaphore(concurrency)

                async def guarded(row):
                    async with semaphore:
                        return await fetch_one(row)

                results = await asyncio.gather(*(guarded(row) for row in batch))
                for row, parsed, error in results:
                    metadata = _metadata_object(row.get("metadata_json"))
                    metadata["generic_detail_last_attempt_at"] = now.isoformat()
                    patch = {
                        "source_key": source,
                        "external_id": row["external_id"],
                        "name": row["name"],
                        "source_url": row["source_url"],
                    }
                    if error:
                        metadata["generic_detail_status"] = error
                        totals[error] += 1
                    else:
                        filled = 0
                        for field in FIELDS:
                            value = parsed.get(field)
                            if not row.get(field) and value:
                                if field.endswith("_url") and not valid_https(value):
                                    continue
                                patch[field] = value
                                totals["fields_filled"][field] = totals["fields_filled"].get(field, 0) + 1
                                filled += 1
                        if parsed.get("_rating") or parsed.get("_reviews"):
                            metadata["structured_social_proof"] = {
                                "rating": parsed.get("_rating"),
                                "reviews": parsed.get("_reviews", [])[:10],
                                "source_url": row["source_url"],
                            }
                        metadata["generic_detail_status"] = "enriched" if filled else "checked_no_new_fields"
                        if filled:
                            totals["enriched"] += 1
                    patch["metadata_json"] = metadata
                    output.write(json.dumps(patch, ensure_ascii=False, separators=(",", ":")) + "\n")
                    totals["checked"] += 1
                output.flush()
                totals["batches"] += 1
                await asyncio.sleep(0.2)

    totals["remaining_at_deadline"] = max(len(candidates) - totals["checked"], 0)
    summary_path = output_dir / f"{source}.summary.json"
    summary_path.write_text(json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    return totals


if __name__ == "__main__":
    asyncio.run(run_source_batch())
