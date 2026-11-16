"""Fedora Packages static index connector.

The Fedora Packages site publishes an alphabetic static index. We crawl the
index pages and retain official per-package detail URLs; we do not crawl every
individual package detail page or download RPM binaries.
"""
from __future__ import annotations

import asyncio
import logging
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from catalog_sources import _record, _unique

log = logging.getLogger("mta.catalogs.fedora")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.href is not None:
            self.links.append((self.href, " ".join(" ".join(self.parts).split())))
            self.href = None
            self.parts = []


async def fetch_fedora_packages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    index_url = "https://packages.fedoraproject.org/index-static.html"
    try:
        response = await client.get(index_url, timeout=TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Fedora static index failed: %s", exc)
        return []

    index = AnchorCollector()
    index.feed(response.text)
    prefix_urls = sorted({
        urljoin(index_url, href)
        for href, _ in index.links
        if href and "/index/" in href and href.endswith(".html")
    })
    if not prefix_urls:
        log.warning("Fedora static index did not expose prefix pages")
        return []

    semaphore = asyncio.Semaphore(8)

    async def fetch_prefix(url: str) -> list[tuple[str, str, str, str]]:
        async with semaphore:
            try:
                page = await client.get(url, timeout=TIMEOUT)
                page.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Fedora prefix page failed %s: %s", url, exc)
                return []
            parser = AnchorCollector()
            parser.feed(page.text)
            found = []
            for href, label in parser.links:
                if not href or "/pkgs/" not in href or not href.endswith("/index.html"):
                    continue
                path = urljoin(url, href).split("/pkgs/", 1)[-1]
                parts = path.split("/")
                if len(parts) < 3:
                    continue
                source_package, binary_package = parts[0], parts[1]
                found.append((urljoin(url, href), source_package, binary_package, label))
            return found

    pages = await asyncio.gather(*(fetch_prefix(url) for url in prefix_urls), return_exceptions=True)
    records: list[dict[str, Any]] = []
    for page_records in pages:
        if not isinstance(page_records, list):
            continue
        for listing_url, source_package, binary_package, label in page_records:
            package_name = label or binary_package
            item = _record(
                "fedora-packages", f"{source_package}/{binary_package}", "linux",
                package_name, listing_url,
                category="Fedora RPM package",
                metadata_json={
                    "catalog_type": "rpm-package",
                    "source_package": source_package,
                    "distribution": "Fedora Linux",
                    "metadata_completeness": "package-name-and-official-detail-link",
                },
            )
            if item:
                records.append(item)
    log.info("Fedora static index: %d package records across %d prefix pages", len(records), len(prefix_urls))
    return _unique(records)
