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
from urllib.parse import urljoin, urlsplit

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


class FedoraDetailParser(HTMLParser):
    """Extract readable package descriptions, repository links, and release rows."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.rows: list[list[str]] = []
        self._paragraph: list[str] | None = None
        self._anchor: list[str] | None = None
        self._anchor_href: str | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.meta_description: str | None = None
        self.page_image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        tag = tag.lower()
        if tag == "meta":
            meta_name = (attrs_map.get("property") or attrs_map.get("name") or "").casefold()
            content = attrs_map.get("content")
            if content:
                if meta_name in {"og:description", "description", "twitter:description"} and not self.meta_description:
                    self.meta_description = content.strip()
                elif meta_name in {"og:image", "og:image:secure_url", "twitter:image"} and not self.page_image_url:
                    self.page_image_url = content.strip()
        elif tag == "p": self._paragraph = []
        elif tag == "a": self._anchor, self._anchor_href = [], attrs_map.get("href")
        elif tag == "tr": self._row = []
        elif tag in {"td", "th"}: self._cell = []

    def handle_data(self, data: str) -> None:
        if self._paragraph is not None: self._paragraph.append(data)
        if self._anchor is not None: self._anchor.append(data)
        if self._cell is not None: self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "p" and self._paragraph is not None:
            value = " ".join(" ".join(self._paragraph).split())
            if value: self.paragraphs.append(value)
            self._paragraph = None
        elif tag == "a" and self._anchor is not None:
            label = " ".join(" ".join(self._anchor).split())
            if self._anchor_href: self.links.append((self._anchor_href, label))
            self._anchor, self._anchor_href = None, None
        elif tag in {"td", "th"} and self._cell is not None:
            value = " ".join(" ".join(self._cell).split())
            if self._row is not None: self._row.append(value)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row: self.rows.append(self._row)
            self._row = None


def parse_fedora_detail(page_html: str) -> dict[str, Any]:
    """Extract only facts explicitly shown on a Fedora package detail page."""
    parser = FedoraDetailParser()
    parser.feed(page_html or "")
    paragraphs = [item for item in parser.paragraphs if not item.lower().startswith("view ")]
    summary = paragraphs[0] if paragraphs else parser.meta_description
    description = next((item for item in paragraphs[1:] if "contact the maintainers" not in item.lower()), None)
    if not description and parser.meta_description and parser.meta_description != summary:
        description = parser.meta_description
    source_repository = None
    homepage = None
    for href, _label in parser.links:
        absolute = urljoin("https://packages.fedoraproject.org/", href)
        try:
            parsed = urlsplit(absolute)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme != "https" or not host:
            continue
        if host == "src.fedoraproject.org":
            source_repository = absolute
        elif host not in {"packages.fedoraproject.org", "fedoraproject.org", "www.fedoraproject.org",
                          "koji.fedoraproject.org", "bodhi.fedoraproject.org", "bugzilla.redhat.com",
                          "koschei.fedoraproject.org", "retrace.fedoraproject.org", "pagure.io"}:
            homepage = homepage or absolute

    releases: list[dict[str, str]] = []
    if parser.rows:
        headers = [value.casefold() for value in parser.rows[0]]
        if "release" in headers:
            for row in parser.rows[1:]:
                if not row: continue
                releases.append({headers[i]: value for i, value in enumerate(row[:len(headers)]) if i < len(headers) and value})

    return {
        "summary": summary,
        "description": description,
        "app_url": homepage,
        "metadata_json": {
            "fedora_detail": {
                "source_repository_url": source_repository,
                "package_page_image_url": parser.page_image_url,
                "releases": releases,
            }
        },
    }


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
