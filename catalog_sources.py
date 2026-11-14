"""
catalog_sources.py – Multi-source metadata sync for MTA Applications Engine.

Provides async fetchers for:
  - Flathub AppStream API (Linux)
  - Homebrew Cask JSON feed (macOS)
  - Apple iTunes Search API (iOS)
  - WinGet via winget.run community API (Windows)
  - Curated web_apps.json (Web)

All sources normalise records to a common schema and deduplicate internally.
Sources fail independently - one failure never aborts another.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FLATHUB_MAX_APPS: int = int(os.getenv("FLATHUB_MAX_APPS", "1500"))
APPLE_SEARCH_TERMS: list[str] = [
    t.strip()
    for t in os.getenv(
        "APPLE_SEARCH_TERMS",
        "browser,office,photo,video,education,security,finance,"
        "communication,developer,utilities,messaging,maps,health,weather",
    ).split(",")
    if t.strip()
]
WINGET_SEARCH_TERMS: list[str] = APPLE_SEARCH_TERMS.copy()
APPLE_COUNTRY: str = os.getenv("APPLE_COUNTRY", "us")
APPLE_LIMIT: int = int(os.getenv("APPLE_LIMIT", "200"))
WINGET_LIMIT: int = int(os.getenv("WINGET_LIMIT", "200"))
CONCURRENCY_CAP: int = int(os.getenv("FLATHUB_CONCURRENCY", "8"))

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_APPS_PATH = os.path.join(DATA_DIR, "web_apps.json")


# ---------------------------------------------------------------------------
# URL validation helpers
# ---------------------------------------------------------------------------


def _is_https(url: str | None) -> bool:
    """Return True only when the URL is a non-empty HTTPS URI."""
    if not url:
        return False
    return url.startswith("https://")


def _clean_url(url: str | None) -> str | None:
    """Strip whitespace; return None for empty / None."""
    if url:
        stripped = url.strip()
        return stripped if stripped else None
    return None


def _validate_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise and validate a single record. Return None on failure."""
    url_fields = ["source_url", "app_url", "icon_url"]
    clean: dict[str, Any] = {}
    for key in url_fields:
        val = _clean_url(record.get(key))
        if key == "source_url":
            if not val or not _is_https(val):
                return None
            clean[key] = val
        else:
            if val and not _is_https(val):
                val = None
            clean[key] = val

    required = ["source_key", "external_id", "platform", "name"]
    for k in required:
        if not record.get(k):
            return None
        clean[k] = str(record[k]).strip()

    optional_str = ["developer", "summary", "description", "category", "license"]
    for k in optional_str:
        clean[k] = _clean_url(str(record.get(k) or "")) or None

    clean["metadata_json"] = record.get("metadata_json", {}) or {}

    return clean


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate within a single source by source_key + external_id."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        key = (rec["source_key"], rec["external_id"])
        if key not in seen:
            seen[key] = rec
    return list(seen.values())


# ---------------------------------------------------------------------------
# Pydantic normaliser
# ---------------------------------------------------------------------------


class NormalisedRecord(BaseModel):
    source_key: str
    external_id: str
    platform: str
    name: str
    developer: str | None = None
    summary: str | None = None
    description: str | None = None
    category: str | None = None
    license: str | None = None
    source_url: str
    app_url: str | None = None
    icon_url: str | None = None
    metadata_json: dict[str, Any] | None = None

    @field_validator("source_url", "app_url", "icon_url")
    @classmethod
    def must_be_https(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("https://"):
            raise ValueError("must be HTTPS")
        return v.strip()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Fetcher: Flathub
# ---------------------------------------------------------------------------


async def fetch_flathub(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch app IDs from Flathub, then fetch details with concurrency cap."""
    logger.info("Flathub: listing apps")
    try:
        resp = await client.get(
            "https://flathub.org/api/v2/appstream",
            params={"filter": "apps", "sort": "alphabetical"},
            timeout=30,
        )
        resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("Flathub list failed: %s", exc)
        return []

    ids: list[str] = []
    for item in resp.json():
        aid = item.get("id", "")
        if aid and aid not in ids:
            ids.append(aid)

    if not ids:
        return []

    ids = ids[:FLATHUB_MAX_APPS]
    logger.info("Flathub: %d IDs to fetch (capped at %d)", len(ids), FLATHUB_MAX_APPS)

    records: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(CONCURRENCY_CAP)
    lock = asyncio.Lock()

    async def _fetch_one(app_id: str) -> dict[str, Any] | None:
        async with semaphore:
            try:
                resp = await client.get(
                    f"https://flathub.org/api/v2/appstream/{app_id}",
                    params={"locale": "en"},
                    timeout=10,
                )
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError):
                return None
            data = resp.json()
            cats = data.get("categories", [])
            cat_str = ", ".join(cats) if cats else None
            screenshots = data.get("screenshots", [])
            icon = screenshots[0].get("url") if screenshots else None
            rec: dict[str, Any] = {
                "source_key": "flathub",
                "external_id": app_id,
                "platform": "linux",
                "name": data.get("name", app_id),
                "developer": data.get("developer_name") or data.get("developer"),
                "summary": data.get("summary"),
                "description": data.get("description"),
                "category": cat_str,
                "license": data.get("license"),
                "source_url": f"https://flathub.org/apps/{app_id}",
                "app_url": data.get("project_url") or data.get("launch_url"),
                "icon_url": icon,
                "metadata_json": {
                    "flatpak_id": data.get("id"),
                    "categories": cats,
                },
            }
            async with lock:
                records.append(rec)
            return rec

    tasks = [_fetch_one(aid) for aid in ids]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("Flathub: collected %d records", len(records))
    return records


# ---------------------------------------------------------------------------
# Fetcher: Homebrew Cask
# ---------------------------------------------------------------------------


async def fetch_homebrew_cask(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch all casks from Homebrew Cask official JSON feed."""
    logger.info("Homebrew Cask: fetching feed")
    try:
        resp = await client.get(
            "https://formulae.brew.sh/api/cask.json",
            timeout=60,
        )
        resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("Homebrew Cask fetch failed: %s", exc)
        return []

    data = resp.json()
    casks = data.get("casks", []) if isinstance(data, dict) else []
    logger.info("Homebrew Cask: %d casks in feed", len(casks))

    records: list[dict[str, Any]] = []
    for cask in casks:
        if not isinstance(cask, dict):
            continue
        cask_name = cask.get("name", "")
        token = cask.get("token", cask_name)
        desc = cask.get("desc", "")
        homepage = cask.get("homepage", "")
        livecheck_url = cask.get("livecheck", {}).get("url") if isinstance(cask.get("livecheck"), dict) else None
        rec: dict[str, Any] = {
            "source_key": "homebrew-cask",
            "external_id": token,
            "platform": "macos",
            "name": cask_name,
            "developer": None,
            "summary": desc,
            "description": desc,
            "category": cask.get("category"),
            "license": cask.get("license"),
            "source_url": f"https://formulae.brew.sh/cask/{token}",
            "app_url": homepage,
            "icon_url": cask.get("artwork", {}).get("icon") if isinstance(cask.get("artwork"), dict) else None,
            "metadata_json": {
                "version": cask.get("version"),
                "homepage": homepage,
                "livecheck_url": livecheck_url,
                "caveats": cask.get("caveats"),
            },
        }
        records.append(rec)

    logger.info("Homebrew Cask: collected %d records", len(records))
    return records


# ---------------------------------------------------------------------------
# Fetcher: Apple iTunes Search API
# ---------------------------------------------------------------------------


async def fetch_apple_itunes(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Apple iTunes Store for software using configurable search terms."""
    logger.info("Apple iTunes: searching %d terms", len(APPLE_SEARCH_TERMS))
    records: list[dict[str, Any]] = []

    for term in APPLE_SEARCH_TERMS:
        params = {
            "term": term,
            "country": APPLE_COUNTRY,
            "limit": APPLE_LIMIT,
            "media": "software",
        }
        try:
            resp = await client.get(
                "https://itunes.apple.com/search",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Apple iTunes search for '%s' failed: %s", term, exc)
            continue

        results = resp.json().get("results", [])
        for item in results:
            kind = item.get("kind", "")
            if kind != "software":
                continue
            rec: dict[str, Any] = {
                "source_key": "apple-itunes",
                "external_id": str(item.get("trackId", "")),
                "platform": "ios",
                "name": item.get("trackName", ""),
                "developer": item.get("artistName"),
                "summary": item.get("shortDescription"),
                "description": item.get("description"),
                "category": item.get("genres", [None])[0] if item.get("genres") else None,
                "license": None,
                "source_url": item.get("artistViewUrl"),
                "app_url": item.get("trackViewUrl"),
                "icon_url": item.get("artworkUrl60"),
                "metadata_json": {
                    "trackId": item.get("trackId"),
                    "bundleId": item.get("bundleId"),
                    "version": item.get("version"),
                    "price": item.get("price"),
                    "averageRating": item.get("averageRating"),
                    "genres": item.get("genres"),
                },
            }
            records.append(rec)

    logger.info("Apple iTunes: collected %d records", len(records))
    return records


# ---------------------------------------------------------------------------
# Fetcher: WinGet via winget.run community API
# ---------------------------------------------------------------------------


async def fetch_winget_community(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search WinGet packages via the winget.run third-party community API."""
    logger.info("WinGet (winget.run): searching %d terms", len(WINGET_SEARCH_TERMS))
    records: list[dict[str, Any]] = []

    for term in WINGET_SEARCH_TERMS:
        params = {"query": term, "take": WINGET_LIMIT, "skip": 0}
        try:
            resp = await client.get(
                "https://api.winget.run/v2/packages",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("WinGet search for '%s' failed: %s", term, exc)
            continue

        data = resp.json()
        packages = data if isinstance(data, list) else data.get("value", data.get("packages", []))
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            installers = pkg.get("installer", {}) or pkg.get("installers", {})
            if isinstance(installers, dict):
                installer_url = installers.get("installerUrl") or installers.get("downloadUrl")
            elif isinstance(installers, str):
                installer_url = installers
            else:
                installer_url = None

            rec: dict[str, Any] = {
                "source_key": "winget-run-community",
                "external_id": str(pkg.get("id", "")),
                "platform": "windows",
                "name": pkg.get("name", pkg.get("id", "Unknown")),
                "developer": pkg.get("publisher"),
                "summary": pkg.get("shortDescription"),
                "description": pkg.get("description"),
                "category": None,
                "license": pkg.get("license"),
                "source_url": f"https://winget.run/pkg/{pkg.get('id', '')}",
                "app_url": pkg.get("homePageUrl"),
                "icon_url": pkg.get("iconUrl"),
                "metadata_json": {
                    "id": pkg.get("id"),
                    "version": pkg.get("version"),
                    "tags": pkg.get("tags"),
                    "source": "winget-run-community",
                    "installer_url": installer_url,
                },
            }
            records.append(rec)

    logger.info("WinGet (winget.run): collected %d records", len(records))
    return records


# ---------------------------------------------------------------------------
# Fetcher: Curated web_apps.json
# ---------------------------------------------------------------------------


def fetch_curated_web_apps() -> list[dict[str, Any]]:
    """Load and validate curated web apps from web_apps.json."""
    if not os.path.exists(WEB_APPS_PATH):
        logger.warning("web_apps.json not found at %s", WEB_APPS_PATH)
        return []

    try:
        with open(WEB_APPS_PATH, "r", encoding="utf-8") as f:
            apps = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read web_apps.json: %s", exc)
        return []

    records: list[dict[str, Any]] = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        rec: dict[str, Any] = {
            "source_key": "mta-curated-web",
            "external_id": str(app.get("id", app.get("name", ""))),
            "platform": "web",
            "name": app.get("name", "Unknown"),
            "developer": app.get("developer"),
            "summary": app.get("summary"),
            "description": app.get("description"),
            "category": app.get("category"),
            "license": None,
            "source_url": f"https://github.com/MTAabraawi/MTA-Applications-Engine",
            "app_url": app.get("app_url"),
            "icon_url": app.get("icon_url"),
            "metadata_json": {
                "homepage": app.get("homepage"),
                "source": "mta-curated-web",
            },
        }
        records.append(rec)

    logger.info("Curated web: collected %d records", len(records))
    return records
