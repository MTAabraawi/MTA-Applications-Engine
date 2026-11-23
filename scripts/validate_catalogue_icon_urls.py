"""Deterministic, source-stratified validation of a sample of catalogue icon URLs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from github_catalog_store import GitHubCatalogueStore

HEADERS = {
    "User-Agent": "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)",
    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.9,*/*;q=0.1",
    "Range": "bytes=0-31",
}
TIMEOUT = httpx.Timeout(15.0, connect=6.0)


def sample_icons(records, per_source=20):
    grouped = defaultdict(list)
    for record in records:
        value = record.get("icon_url")
        if not isinstance(value, str) or not value.strip():
            continue
        key = f"{record.get('source_key','')}:{record.get('external_id','')}"
        grouped[str(record.get("source_key") or "unknown")].append(
            (hashlib.sha256(key.encode("utf-8")).hexdigest(), record)
        )
    return {
        source: [record for _, record in sorted(items, key=lambda item: item[0])[:per_source]]
        for source, items in sorted(grouped.items())
    }


async def validate_one(client, record, semaphore):
    url = str(record.get("icon_url") or "").strip()
    result = {
        "source_key": record.get("source_key"),
        "external_id": record.get("external_id"),
        "url": url,
        "status": "invalid_url",
        "http_status": None,
        "content_type": None,
        "final_url": None,
    }
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return result
    except ValueError:
        return result

    async with semaphore:
        try:
            response = await client.head(url, headers=HEADERS, timeout=TIMEOUT)
            if response.status_code in {403, 405, 501}:
                async with client.stream("GET", url, headers=HEADERS, timeout=TIMEOUT) as response:
                    result["http_status"] = response.status_code
                    result["content_type"] = response.headers.get("content-type", "").split(";")[0].lower() or None
                    result["final_url"] = str(response.url)
            else:
                result["http_status"] = response.status_code
                result["content_type"] = response.headers.get("content-type", "").split(";")[0].lower() or None
                result["final_url"] = str(response.url)

            final = urlsplit(result["final_url"] or "")
            if final.scheme != "https":
                result["status"] = "non_https_redirect"
            elif result["http_status"] not in {200, 206}:
                result["status"] = "http_error"
            elif result["content_type"] and result["content_type"].startswith("image/"):
                result["status"] = "ok"
            else:
                result["status"] = "non_image_content_type"
        except httpx.TimeoutException:
            result["status"] = "timeout"
        except httpx.HTTPError as exc:
            result["status"] = "http_error"
            result["error"] = type(exc).__name__
    return result


async def validate_samples(records, per_source=20, concurrency=6):
    samples = sample_icons(records, per_source)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5) as client:
        tasks = [
            validate_one(client, record, semaphore)
            for source_records in samples.values()
            for record in source_records
        ]
        results = await asyncio.gather(*tasks)
    by_source = defaultdict(lambda: defaultdict(int))
    for item in results:
        by_source[str(item["source_key"])][item["status"]] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sampling": {"method": "stable_sha256_per_source", "per_source_limit": per_source},
        "checked": len(results),
        "by_source": {source: dict(counts) for source, counts in sorted(by_source.items())},
        "results": results,
        "limitations": [
            "This is a stratified sample, not a claim that every icon URL was checked.",
            "CDNs may block HEAD or rate-limit automated requests; such responses are reported, not silently treated as missing metadata.",
            "Only HTTPS URLs and final HTTPS redirects are accepted.",
        ],
    }


def main():
    root = Path(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    output = Path(os.getenv("CATALOGUE_ICON_REPORT", "reports/catalogue-icon-validation.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    store = GitHubCatalogueStore(root)
    report = asyncio.run(validate_samples(
        store.iter_records(),
        per_source=max(1, min(int(os.getenv("ICON_VALIDATION_PER_SOURCE", "20")), 100)),
        concurrency=max(1, min(int(os.getenv("ICON_VALIDATION_CONCURRENCY", "6")), 12)),
    ))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    totals = defaultdict(int)
    for source in report["by_source"].values():
        for status, count in source.items():
            totals[status] += count
    print(json.dumps({"report": str(output), "checked": report["checked"], "totals": dict(totals)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
