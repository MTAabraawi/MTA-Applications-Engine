"""Rate-limited generic enrichment for official catalogue listing pages.

Only explicit metadata in HTML meta tags or Schema.org JSON-LD is accepted.
No guessed app icons, ratings, reviews, publishers, or download links are created.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_catalog_store import GitHubCatalogueStore, _merge_metadata

USER_AGENT = "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"
FIELDS = ("developer", "summary", "description", "category", "license", "app_url", "icon_url")


def valid_https(value):
    if not isinstance(value, str) or not value.startswith("https://"):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.links = []
        self.jsonld = []
        self._script_type = ""
        self._script_data = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): v for k, v in attrs}
        if tag.lower() == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            value = attrs.get("content")
            if key and value:
                self.meta.setdefault(key, value.strip())
        elif tag.lower() == "link":
            self.links.append(attrs)
        elif tag.lower() == "script" and "ld+json" in str(attrs.get("type", "")).lower():
            self._script_type = "jsonld"
            self._script_data = []

    def handle_data(self, data):
        if self._script_type:
            self._script_data.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._script_type:
            raw = "".join(self._script_data).strip()
            if raw:
                try:
                    self.jsonld.append(json.loads(raw))
                except (TypeError, ValueError):
                    pass
            self._script_type = ""
            self._script_data = []


def _walk_jsonld(value, depth=0):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                yield from _walk_jsonld(child, depth + 1)
        for key in ("mainEntity", "itemListElement", "item", "application", "software"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yield from _walk_jsonld(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_jsonld(child, depth + 1)


def parse_official_metadata(html, page_url):
    parser = MetadataParser()
    parser.feed(html[:2_000_000])
    meta = parser.meta
    result = {}

    def first(*keys):
        return next((meta[k] for k in keys if meta.get(k)), None)

    description = first("description", "og:description", "twitter:description")
    if description:
        result["description"] = description[:12000]
        result["summary"] = description[:1000]

    developer = first("author", "article:author", "publisher")
    if developer:
        result["developer"] = developer[:500]

    category = first("article:section", "application-category", "category")
    if category:
        result["category"] = category[:500]

    license_value = first("license", "dcterms.license", "copyright")
    if license_value and "http" not in license_value.lower():
        result["license"] = license_value[:300]

    image = first("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    if image:
        image_url = urljoin(page_url, image)
        if valid_https(image_url):
            result["icon_url"] = image_url

    for obj in parser.jsonld:
        for node in _walk_jsonld(obj):
            kind = node.get("@type", [])
            kinds = {str(x).casefold() for x in (kind if isinstance(kind, list) else [kind])}
            if kinds & {"softwareapplication", "mobileapplication", "webapplication"}:
                result.setdefault("developer", (node.get("author") or {}).get("name") if isinstance(node.get("author"), dict) else node.get("author"))
                result.setdefault("description", node.get("description"))
                result.setdefault("category", node.get("applicationCategory"))
                result.setdefault("license", node.get("license"))
                app_url = node.get("url")
                if isinstance(app_url, str) and valid_https(urljoin(page_url, app_url)):
                    result.setdefault("app_url", urljoin(page_url, app_url))
                image_value = node.get("image")
                if isinstance(image_value, dict):
                    image_value = image_value.get("url")
                if isinstance(image_value, str) and valid_https(urljoin(page_url, image_value)):
                    result.setdefault("icon_url", urljoin(page_url, image_value))
                rating = node.get("aggregateRating")
                if isinstance(rating, dict):
                    rating_value = rating.get("ratingValue")
                    rating_count = rating.get("ratingCount") or rating.get("reviewCount")
                    if rating_value is not None:
                        result.setdefault("_rating", {"value": rating_value, "count": rating_count})
            if "review" in kinds or node.get("@type") == "Review":
                body = node.get("reviewBody")
                author = node.get("author")
                rating = node.get("reviewRating")
                if isinstance(body, str) and body.strip():
                    result.setdefault("_reviews", []).append({
                        "text": body.strip()[:2000],
                        "author": author.get("name") if isinstance(author, dict) else author,
                        "rating": rating.get("ratingValue") if isinstance(rating, dict) else None,
                        "source": page_url,
                    })

    return {k: v for k, v in result.items() if v is not None and (not isinstance(v, str) or v.strip())}


def _retry_due(metadata, now):
    attempt = metadata.get("generic_detail_last_attempt_at")
    if not attempt:
        return True
    try:
        previous = datetime.fromisoformat(str(attempt).replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    status = metadata.get("generic_detail_status")
    delay = timedelta(hours=12) if status == "retryable" else timedelta(days=7)
    return previous <= now - delay


async def enrich(store: GitHubCatalogueStore) -> dict:
    now = datetime.now(timezone.utc)
    records = list(store.iter_records())
    by_source = {}
    for row in records:
        if not valid_https(row.get("source_url")):
            continue
        missing = any(not row.get(field) for field in FIELDS)
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        if missing and _retry_due(metadata, now):
            by_source.setdefault(row.get("source_key") or "unknown", []).append(row)

    per_source_limit = max(1, min(int(os.getenv("GENERIC_DETAIL_BATCH_PER_SOURCE", "80")), 300))
    concurrency = max(1, min(int(os.getenv("GENERIC_DETAIL_CONCURRENCY", "4")), 8))
    runtime = max(5, min(int(os.getenv("GENERIC_DETAIL_RUNTIME_MINUTES", "300")), 330))
    deadline = time.monotonic() + runtime * 60
    totals = {"selected": 0, "checked": 0, "enriched": 0, "retryable": 0, "fields_filled": {}, "sources": {}}
    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(25, connect=10)

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        # Round-robin source ordering avoids starving smaller stores.
        queues = {source: sorted(rows, key=lambda r: str((r.get("metadata_json") or {}).get("generic_detail_last_attempt_at") or ""))[:per_source_limit]
                  for source, rows in by_source.items()}
        selected = [row for source in sorted(queues) for row in queues[source]]
        totals["selected"] = len(selected)

        async def one(row):
            async with semaphore:
                try:
                    current_url = row["source_url"]
                    original_host = urlsplit(current_url).hostname
                    for _ in range(4):
                        response = await client.get(current_url)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = urljoin(current_url, response.headers.get("Location", ""))
                            if not valid_https(target) or urlsplit(target).hostname != original_host:
                                return row, {}, "redirect_host_changed"
                            current_url = target
                            continue
                        response.raise_for_status()
                        break
                    else:
                        return row, {}, "too_many_redirects"
                    if "html" not in response.headers.get("content-type", "").lower():
                        return row, {}, "non_html"
                    return row, parse_official_metadata(response.text, str(response.url)), None
                except (httpx.HTTPError, ValueError) as exc:
                    return row, {}, type(exc).__name__

        pending = []
        for offset in range(0, len(selected), concurrency):
            if time.monotonic() >= deadline:
                break
            batch = selected[offset:offset + concurrency]
            pending.extend(await asyncio.gather(*(one(row) for row in batch)))
            await asyncio.sleep(0.2)

    updates = []
    for row, parsed, error in pending:
        item = dict(row)
        metadata = dict(item.get("metadata_json") or {})
        metadata["generic_detail_last_attempt_at"] = now.isoformat()
        totals["checked"] += 1
        source = item.get("source_key") or "unknown"
        source_counts = totals["sources"].setdefault(source, {"checked": 0, "enriched": 0, "retryable": 0})
        source_counts["checked"] += 1
        if error:
            metadata["generic_detail_status"] = "retryable" if error not in {"non_html", "redirect_host_changed"} else "unavailable"
            if metadata["generic_detail_status"] == "retryable":
                totals["retryable"] += 1
                source_counts["retryable"] += 1
        else:
            filled = 0
            for field in FIELDS:
                value = parsed.get(field)
                if not item.get(field) and value:
                    if field.endswith("_url") and not valid_https(value):
                        continue
                    item[field] = value
                    totals["fields_filled"][field] = totals["fields_filled"].get(field, 0) + 1
                    filled += 1
            if parsed.get("_rating") or parsed.get("_reviews"):
                metadata["structured_social_proof"] = {
                    "rating": parsed.get("_rating"),
                    "reviews": parsed.get("_reviews", [])[:10],
                    "source_url": item["source_url"],
                }
            metadata["generic_detail_status"] = "enriched" if filled else "checked_no_new_fields"
            if filled:
                totals["enriched"] += 1
                source_counts["enriched"] += 1
        item["metadata_json"] = _merge_metadata(metadata, {})
        updates.append(item)

    if updates:
        store.upsert_many(updates, rebuild_indexes=False)
    return totals


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(enrich(GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))))
    print(json.dumps(result, ensure_ascii=False, indent=2))
