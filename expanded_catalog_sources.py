"""Additional public software catalogues for the MTA discovery engine.

Only public metadata and official listing URLs are collected. No installer
binaries, private endpoints, login-gated data, or access-control bypasses.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import html as html_module
import re
import os
import tarfile
from typing import Any

import httpx

from catalog_sources import _record, _unique
from fedora_catalog_source import fetch_fedora_packages
from open_catalog_sources import fetch_bluejetty, fetch_openuse, fetch_chocolatey

log = logging.getLogger("mta.catalogs.expanded")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
HEADERS = {"User-Agent": "MTA-Applications-Engine/0.4 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}
CONCURRENCY = max(1, min(int(os.getenv("CATALOG_CONCURRENCY", "5")), 10))


async def _json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()



AI_DISCOVERY_TERMS = [
    "AI assistant", "AI chatbot", "LLM application", "AI image generator",
    "AI video generator", "AI voice tools", "AI coding assistant", "local AI desktop",
    "人工智能 应用", "AI 助手", "大模型 应用", "文生图 工具",
    "нейросеть приложение", "ИИ помощник", "генерация изображений ИИ",
    "AIアプリ", "AIアシスタント", "인공지능 앱", "AI ứng dụng",
    "aplicación inteligencia artificial", "application intelligence artificielle",
    "KI Anwendung", "aplicativo inteligência artificial", "AI app India",
]

GITHUB_AI_DISCOVERY_TERMS = [
    '"AI app" in:name,description,readme',
    '"AI assistant" in:name,description,readme',
    '"LLM app" in:name,description,readme',
    '"AI chatbot" in:name,description,readme',
    '"AI desktop" in:name,description,readme',
    '"local LLM" in:name,description,readme',
    '"AI coding assistant" in:name,description,readme',
    '"image generation" AI in:name,description,readme',
    '"人工智能" 应用 in:name,description,readme',
    '"AI 助手" in:name,description,readme',
    '"大模型" 应用 in:name,description,readme',
    '"нейросеть" приложение in:name,description,readme',
    '"ИИ помощник" in:name,description,readme',
    '"AIアプリ" in:name,description,readme',
    '"인공지능" 앱 in:name,description,readme',
    '"IA generativa" app in:name,description,readme',
    '"IA assistant" application in:name,description,readme',
    '"AI application" India in:name,description,readme',
]


def _ai_discovery_locale(term: str) -> str:
    """Label the language used for discovery; never treat it as publisher origin."""
    value = str(term or "")
    if re.search(r"[\\u4e00-\\u9fff]", value):
        return "zh-CN"
    if re.search(r"[\\u0400-\\u04ff]", value):
        return "ru"
    if re.search(r"[\\u3040-\\u30ff]", value):
        return "ja"
    if re.search(r"[\\uac00-\\ud7af]", value):
        return "ko"
    if re.search(r"[\\u0900-\\u097f]", value):
        return "hi"
    folded = value.casefold()
    if any(word in folded for word in ("inteligencia artificial", "aplicación")):
        return "es"
    if any(word in folded for word in ("intelligence artificielle", "application")) and "ai" not in folded:
        return "fr"
    if any(word in folded for word in ("künstliche", "anwendung")):
        return "de"
    if "aplicativo" in folded or "inteligência artificial" in folded:
        return "pt"
    if "ứng dụng" in folded:
        return "vi"
    return "en"


async def fetch_huggingface_ai_spaces(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Discover runnable AI applications published as public Hugging Face Spaces.

    Spaces are web applications, not model-only records. Search is multilingual;
    origin country is deliberately not inferred from query language.
    """
    terms = [x.strip() for x in os.getenv(
        "AI_GLOBAL_HF_TERMS", ",".join(AI_DISCOVERY_TERMS)
    ).split(",") if x.strip()]
    terms = terms[:max(1, min(int(os.getenv("AI_GLOBAL_HF_MAX_TERMS", "24")), 40))]
    limit = max(10, min(int(os.getenv("AI_GLOBAL_HF_PER_TERM", "100")), 100))
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        try:
            payload = await _json(
                client, "https://huggingface.co/api/spaces/semantic-search",
                params={"q": term, "limit": limit, "includeNonRunning": "true"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Hugging Face Spaces search failed for %r: %s", term, exc)
            continue
        items = payload if isinstance(payload, list) else (
            payload.get("results", []) if isinstance(payload, dict) else []
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            space_id = str(item.get("id") or "").strip()
            if not space_id or space_id in seen or "/" not in space_id:
                continue
            seen.add(space_id)
            title = item.get("title") or space_id.rsplit("/", 1)[-1]
            author = item.get("author") or space_id.split("/", 1)[0]
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
            domains = runtime.get("domains") if isinstance(runtime.get("domains"), list) else []
            live_domain = next((x.get("domain") for x in domains if isinstance(x, dict) and x.get("domain")), None)
            app_url = ("https://" + str(live_domain).lstrip("https://")) if live_domain else f"https://huggingface.co/spaces/{space_id}"
            description = item.get("ai_short_description") or item.get("description") or ""
            category = item.get("ai_category") or next((tag for tag in tags if tag in {
                "text-generation", "image-generation", "text-to-speech", "automatic-speech-recognition",
                "video-generation", "chatbot", "computer-vision", "audio"
            }), "AI application")
            record = _record(
                "huggingface-ai-spaces", space_id, "web", title,
                f"https://huggingface.co/spaces/{space_id}",
                developer=author, summary=description, description=description,
                category=str(category), app_url=app_url,
                metadata_json={
                    "catalog_type": "hosted-ai-web-application",
                    "platform_type": "web",
                    "discovery_query": term,
                    "discovery_language": _ai_discovery_locale(term),
                    "origin_country": None,
                    "sdk": item.get("sdk"),
                    "tags": tags[:80],
                    "likes": item.get("likes"),
                    "runtime_stage": runtime.get("stage"),
                    "semantic_relevancy_score": item.get("semantic_relevancy_score"),
                    "trending_score": item.get("trending_score"),
                    "publisher_id": space_id,
                },
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.15)
    log.info("Hugging Face AI Spaces discovered: %d", len(records))
    return _unique(records)


async def fetch_github_ai_applications(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Discover open-source AI applications from GitHub's public repository search.

    Repository metadata and homepage links are retained as publisher references;
    country of origin is not guessed from language, owner name, or search term.
    """
    terms = [x.strip() for x in os.getenv(
        "AI_GLOBAL_GITHUB_TERMS", "\\n".join(GITHUB_AI_DISCOVERY_TERMS)
    ).splitlines() if x.strip()]
    max_terms = max(1, min(int(os.getenv("AI_GLOBAL_GITHUB_MAX_TERMS", "18")), 25))
    per_page = max(10, min(int(os.getenv("AI_GLOBAL_GITHUB_PER_PAGE", "100")), 100))
    terms = terms[:max_terms]
    token = os.getenv("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        try:
            payload = await _json(
                client, "https://api.github.com/search/repositories",
                params={"q": term + " fork:false archived:false", "sort": "updated",
                        "order": "desc", "per_page": per_page, "page": 1},
                headers=headers,
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GitHub AI repository search failed for %r: %s", term, exc)
            await asyncio.sleep(2.0)
            continue
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("full_name") or "").strip()
            html_url = item.get("html_url")
            if not full_name or full_name.casefold() in seen:
                continue
            topics = item.get("topics") if isinstance(item.get("topics"), list) else []
            name = item.get("name") or full_name.rsplit("/", 1)[-1]
            description = item.get("description") or ""
            haystack = (str(name) + " " + str(description) + " " + " ".join(topics)).casefold()
            app_terms = ("app", "assistant", "chat", "webui", "desktop", "client", "studio",
                         "interface", "frontend", "tool", "platform", "generator", "copilot")
            if not any(word in haystack for word in app_terms):
                continue
            seen.add(full_name.casefold())
            license_info = item.get("license") if isinstance(item.get("license"), dict) else {}
            homepage = item.get("homepage") or ""
            record = _record(
                "github-ai-applications", full_name, "source-code",
                name, html_url, developer=(item.get("owner") or {}).get("login"),
                summary=description, description=description,
                category=", ".join(topics[:12]) or "Open-source AI software",
                license=license_info.get("spdx_id"),
                app_url=homepage if str(homepage).startswith("https://") else html_url,
                metadata_json={
                    "catalog_type": "open-source-ai-application-project",
                    "platform_type": "source-code",
                    "discovery_query": term,
                    "discovery_language": "multilingual",
                    "origin_country": None,
                    "topics": topics[:100],
                    "language": item.get("language"),
                    "stars": item.get("stargazers_count"),
                    "forks": item.get("forks_count"),
                    "open_issues": item.get("open_issues_count"),
                    "updated_at": item.get("updated_at"),
                    "pushed_at": item.get("pushed_at"),
                    "homepage": homepage,
                    "default_branch": item.get("default_branch"),
                    "license": license_info.get("spdx_id"),
                    "publisher_id": full_name,
                },
            )
            if record:
                records.append(record)
        await asyncio.sleep(1.1 if not token else 0.25)
    log.info("GitHub AI application projects discovered: %d", len(records))
    return _unique(records)


async def fetch_firefox_addons(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect public Firefox extensions from Mozilla's documented AMO API."""
    terms = [x.strip() for x in os.getenv(
        "FIREFOX_ADDON_TERMS",
        "privacy,security,ad blocker,productivity,developer,translator,video,download,accessibility,shopping,web tools,password,tab,theme",
    ).split(",") if x.strip()]
    pages = max(1, min(int(os.getenv("FIREFOX_ADDON_PAGES", "3")), 5))
    records: list[dict[str, Any]] = []
    for term in terms:
        for page in range(1, pages + 1):
            try:
                payload = await _json(
                    client, "https://addons.mozilla.org/api/v5/addons/search/",
                    params={"q": term, "app": "firefox", "type": "extension",
                            "page_size": 50, "page": page},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Mozilla AMO search failed for %r page %d: %s", term, page, exc)
                break
            items = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                name = item.get("name")
                if isinstance(name, dict):
                    name = name.get("en-US") or name.get("en") or next(iter(name.values()), None)
                summary = item.get("summary")
                if isinstance(summary, dict):
                    summary = summary.get("en-US") or summary.get("en") or next(iter(summary.values()), None)
                description = item.get("description")
                if isinstance(description, dict):
                    description = description.get("en-US") or description.get("en") or next(iter(description.values()), None)
                authors = item.get("authors") or []
                author = authors[0].get("name") if authors and isinstance(authors[0], dict) else None
                icons = item.get("icons") or {}
                icon = item.get("icon_url") or (icons.get("64") if isinstance(icons, dict) else None)
                url = f"https://addons.mozilla.org/firefox/addon/{slug}/" if slug else None
                record = _record(
                    "firefox-addons", item.get("id") or slug, "browser-extensions",
                    name, url, developer=author, summary=summary,
                    description=description if isinstance(description, str) else None,
                    category=", ".join(item.get("categories") or []) if isinstance(item.get("categories"), list) else None,
                    icon_url=icon,
                    metadata_json={"slug": slug, "catalog_type": "browser-extension", "weekly_downloads": item.get("weekly_downloads"),
                                   "average_daily_users": item.get("average_daily_users"),
                                   "ratings": item.get("ratings"), "created": item.get("created"),
                                   "last_updated": item.get("last_updated"), "source": "Mozilla Add-ons API"},
                )
                if record:
                    records.append(record)
            await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_wordpress_plugins(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect WordPress plugins through the official WordPress.org directory API."""
    terms = [x.strip() for x in os.getenv(
        "WORDPRESS_PLUGIN_TERMS",
        "security,seo,backup,forms,commerce,performance,editor,analytics,media,accessibility,translation,blocks,cache,developer",
    ).split(",") if x.strip()]
    pages = max(1, min(int(os.getenv("WORDPRESS_PLUGIN_PAGES", "3")), 5))
    records: list[dict[str, Any]] = []
    for term in terms:
        for page in range(1, pages + 1):
            try:
                payload = await _json(
                    client, "https://api.wordpress.org/plugins/info/1.2/",
                    params={"action": "query_plugins", "request[search]": term,
                            "request[page]": page, "request[per_page]": 100,
                            "request[fields][short_description]": 1,
                            "request[fields][icons]": 1},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("WordPress plugin search failed for %r page %d: %s", term, page, exc)
                break
            items = payload.get("plugins", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                name = item.get("name")
                if isinstance(name, str):
                    name = html_module.unescape(re.sub(r"<[^>]+>", " ", name)).strip()
                if not slug or not name:
                    continue
                short_description = item.get("short_description")
                if isinstance(short_description, str):
                    short_description = html_module.unescape(re.sub(r"<[^>]+>", " ", short_description)).strip()
                author = item.get("author")
                if isinstance(author, str):
                    author = html_module.unescape(re.sub(r"<[^>]+>", " ", author)).strip()
                icons = item.get("icons") or {}
                icon = icons.get("2x") or icons.get("1x") or icons.get("default") if isinstance(icons, dict) else None
                record = _record(
                    "wordpress-plugins", slug, "web-plugins", name,
                    f"https://wordpress.org/plugins/{slug}/",
                    developer=author, summary=short_description,
                    description=short_description,
                    category="WordPress plugin", app_url=item.get("homepage"),
                    icon_url=icon,
                    metadata_json={"catalog_type": "wordpress-plugin", "version": item.get("version"), "rating": item.get("rating"),
                                   "num_ratings": item.get("num_ratings"),
                                   "active_installs": item.get("active_installs"),
                                   "last_updated": item.get("last_updated"),
                                   "requires": item.get("requires"), "tested": item.get("tested")},
                )
                if record:
                    records.append(record)
            await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_homebrew_formulae(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch the complete public Homebrew/core formula metadata feed with retry."""
    url = "https://formulae.brew.sh/api/formula.json"
    response = None
    payload = None
    for attempt in range(2):
        try:
            response = await client.get(
                url,
                timeout=httpx.Timeout(120.0, connect=20.0),
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            break
        except Exception as exc:
            if attempt == 0:
                log.warning("Homebrew formula feed attempt %d failed (%s); retrying", attempt + 1, type(exc).__name__)
                await asyncio.sleep(3)
                continue
            # Keep the failure actionable without logging the potentially large feed body.
            status = getattr(response, "status_code", None)
            headers = getattr(response, "headers", {}) if response is not None else {}
            content_type = headers.get("content-type") if hasattr(headers, "get") else None
            content_encoding = headers.get("content-encoding") if hasattr(headers, "get") else None
            content_length = headers.get("content-length") if hasattr(headers, "get") else None
            try:
                body_bytes = len(response.content) if response is not None else None
            except Exception:
                body_bytes = None
            log.exception(
                "Homebrew formula feed failed after retry: type=%s status=%s content_type=%r "
                "content_encoding=%r content_length=%r body_bytes=%s",
                type(exc).__name__, status, content_type, content_encoding,
                content_length, body_bytes,
            )
            return []
    if not isinstance(payload, list):
        log.warning("Homebrew formula feed returned an unexpected payload type: %s", type(payload).__name__)
        return []
    records = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        versions = item.get("versions") or {}
        record = _record(
            "homebrew-formulae", name, "macos", item.get("full_name") or name,
            f"https://formulae.brew.sh/formula/{name}",
            developer=item.get("tap"), summary=item.get("desc"), description=item.get("desc"),
            category=None,
            license=item.get("license"), app_url=item.get("homepage"),
            metadata_json={"catalog_type": "homebrew-formula", "version": versions.get("stable"), "tap": item.get("tap"),
                           "bottle": bool(item.get("bottle")),
                           "also_available_on_linux": True, "dependencies": item.get("dependencies", [])},
        )
        if record:
            records.append(record)
    return _unique(records)


async def fetch_scoop_buckets(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Read public Scoop bucket manifests from the official GitHub bucket archives."""
    buckets = [
        ("main", "ScoopInstaller/Main"),
        ("extras", "ScoopInstaller/Extras"),
        ("games", "ScoopInstaller/Games"),
        ("versions", "ScoopInstaller/Versions"),
    ]
    sem = asyncio.Semaphore(3)

    async def one_bucket(bucket: str, repository: str) -> list[dict[str, Any]]:
        async with sem:
            url = f"https://codeload.github.com/{repository}/tar.gz/refs/heads/master"
            try:
                response = await client.get(url, timeout=TIMEOUT)
                response.raise_for_status()
                archive = tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz")
            except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
                log.warning("Scoop bucket %s download failed: %s", bucket, exc)
                return []
            records = []
            with archive:
                for member in archive.getmembers():
                    if not member.isfile() or "/bucket/" not in member.name or not member.name.endswith(".json"):
                        continue
                    if member.size > 512_000:
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    try:
                        manifest = json.loads(handle.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(manifest, dict):
                        continue
                    package = member.name.rsplit("/", 1)[-1][:-5]
                    name = manifest.get("name") or package
                    description = manifest.get("description")
                    homepage = manifest.get("homepage")
                    license_value = manifest.get("license")
                    source_path = member.name.split("/bucket/", 1)[-1]
                    source_url = f"https://github.com/{repository}/blob/master/bucket/{source_path}"
                    record = _record(
                        "scoop", package.casefold(), "windows", name, source_url,
                        developer=None, summary=description, description=description,
                        category=f"Scoop {bucket}", license=license_value if isinstance(license_value, str) else None,
                        app_url=homepage if isinstance(homepage, str) else None,
                        metadata_json={"catalog_type": "scoop-manifest", "version": manifest.get("version"), "bucket": bucket,
                                       "homepage": homepage, "architecture": list((manifest.get("architecture") or {}).keys())
                                       if isinstance(manifest.get("architecture"), dict) else [],
                                       "manifest_path": source_path},
                    )
                    if record:
                        records.append(record)
            return records

    results = await asyncio.gather(*(one_bucket(name, repo) for name, repo in buckets), return_exceptions=True)
    combined = []
    for result in results:
        if isinstance(result, list):
            combined.extend(result)
    # Same package can occur in multiple buckets; keep the first (main > extras > games > versions).
    return _unique(combined)


async def fetch_alpine_packages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Parse official Alpine APKINDEX archives for x86_64 main and community packages."""
    indexes = [
        ("main", "https://dl-cdn.alpinelinux.org/alpine/edge/main/x86_64/APKINDEX.tar.gz"),
        ("community", "https://dl-cdn.alpinelinux.org/alpine/edge/community/x86_64/APKINDEX.tar.gz"),
    ]
    records: list[dict[str, Any]] = []
    for repository, url in indexes:
        try:
            response = await client.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            archive = tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz")
            with archive:
                member = next((x for x in archive.getmembers() if x.name.endswith("APKINDEX")), None)
                if member is None:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                raw = handle.read().decode("utf-8", errors="replace")
        except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
            log.warning("Alpine %s APKINDEX failed: %s", repository, exc)
            continue
        for block in raw.split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if len(line) > 2 and line[1] == ":":
                    fields[line[0]] = line[2:]
            package = fields.get("P")
            if not package:
                continue
            record = _record(
                "alpine-linux", f"{repository}/{package}", "linux", package,
                f"https://pkgs.alpinelinux.org/package/edge/{repository}/x86_64/{package}",
                developer=fields.get("m"), summary=fields.get("T"), description=fields.get("T"),
                license=fields.get("L"),
                app_url=fields.get("U"),
                metadata_json={"catalog_type": "alpine-package", "version": fields.get("V"), "architecture": fields.get("A"),
                               "origin": fields.get("o"), "size_bytes": int(fields["S"]) if fields.get("S", "").isdigit() else None,
                               "repository": repository, "branch": "edge"},
            )
            if record:
                records.append(record)
    return _unique(records)


def _appbrain_categories(env_name: str, default: str) -> list[str]:
    return [x.strip().upper() for x in os.getenv(env_name, default).split(",") if x.strip()]


async def _fetch_appbrain_platform(
    client: httpx.AsyncClient, *, platform: str, source_key: str,
    categories: list[str], max_pages: int,
) -> list[dict[str, Any]]:
    """Fetch AppBrain browse pages while respecting its documented offset ceiling."""
    records: list[dict[str, Any]] = []
    for category in categories:
        for page in range(max_pages):
            offset = page * 50
            try:
                params = {"apikey": os.getenv("APPBRAIN_API_KEY", "").strip(),
                          "category": category, "offset": offset, "limit": 50}
                if platform == "IOS":
                    params["platform"] = "IOS"
                payload = await _json(client, "https://api.appbrain.com/v2/info/browse", params=params)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("AppBrain %s category %s offset %d failed: %s", platform, category, offset, exc)
                break
            items = payload.get("apps", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                package = item.get("package")
                name = item.get("name")
                if platform == "IOS":
                    # AppBrain addresses App Store records as ios-<trackId>.
                    match = re.fullmatch(r"ios-(\d+)", str(package or ""), re.IGNORECASE)
                    track_id = match.group(1) if match else str(item.get("trackId") or "")
                    if not track_id.isdigit():
                        continue
                    external_id = track_id
                    listing_url = item.get("appStoreUrl") or f"https://apps.apple.com/app/id{track_id}"
                    record_platform = "ios"
                else:
                    if not package:
                        continue
                    external_id = str(package)
                    listing_url = f"https://play.google.com/store/apps/details?id={package}"
                    record_platform = "android"
                # Keep the complete public AppBrain record so newly exposed fields are
                # not silently discarded when AppBrain expands its response schema.
                metadata = dict(item)
                metadata["source_provider"] = "AppBrain"
                metadata["appbrain_platform"] = platform
                record = _record(
                    source_key, external_id, record_platform, name, listing_url,
                    developer=item.get("developerName") or item.get("developer") or item.get("publisher") or item.get("artistName"),
                    summary=item.get("shortDescription"), description=item.get("description"),
                    category=item.get("marketCategory") or category,
                    icon_url=item.get("iconUrl") or item.get("icon") or item.get("iconURL"),
                    app_url=listing_url, metadata_json=metadata,
                )
                if record:
                    records.append(record)
            if len(items) < 50:
                break
            await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_google_play_appbrain(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch Google Play metadata through AppBrain only when a run is explicitly enabled."""
    enabled = os.getenv("APPBRAIN_ENABLE_SYNC", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        log.info("Google Play/AppBrain connector skipped: APPBRAIN_ENABLE_SYNC is disabled to protect API credits")
        return []
    api_key = os.getenv("APPBRAIN_API_KEY", "").strip()
    if not api_key:
        log.info("Google Play/AppBrain connector skipped: APPBRAIN_API_KEY is not configured")
        return []
    categories = _appbrain_categories(
        "APPBRAIN_CATEGORIES",
        "BOOKS,BUSINESS,EDUCATION,ENTERTAINMENT,FINANCE,FOOD_AND_DRINK,GAMES,HEALTH_AND_FITNESS,LIFESTYLE,MAGAZINES_AND_NEWSPAPERS,MEDICAL,MUSIC,NAVIGATION,NEWS,PHOTO_AND_VIDEO,PRODUCTIVITY,REFERENCE,SHOPPING,SOCIAL_NETWORKING,SPORTS,TRAVEL,UTILITIES,WEATHER",
    )
    max_pages = max(1, min(int(os.getenv("APPBRAIN_MAX_PAGES", "1")), 11))
    return await _fetch_appbrain_platform(
        client, platform="ANDROID", source_key="google-play",
        categories=categories, max_pages=max_pages,
    )


async def fetch_apple_appbrain(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch Apple App Store metadata through AppBrain's iOS browse API when explicitly enabled."""
    enabled = os.getenv("APPBRAIN_ENABLE_SYNC", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        log.info("Apple App Store/AppBrain connector skipped: APPBRAIN_ENABLE_SYNC is disabled to protect API credits")
        return []
    api_key = os.getenv("APPBRAIN_API_KEY", "").strip()
    if os.getenv("APPBRAIN_ENABLE_IOS", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        log.info("Apple App Store/AppBrain connector skipped: APPBRAIN_ENABLE_IOS is disabled")
        return []
    if not api_key:
        log.info("Apple App Store/AppBrain connector skipped: APPBRAIN_API_KEY is not configured")
        return []
    categories = _appbrain_categories(
        "APPBRAIN_IOS_CATEGORIES",
        "BOOKS,BUSINESS,EDUCATION,ENTERTAINMENT,FINANCE,FOOD_AND_DRINK,GAMES,HEALTH_AND_FITNESS,LIFESTYLE,MAGAZINES_AND_NEWSPAPERS,MEDICAL,MUSIC,NAVIGATION,NEWS,PHOTO_AND_VIDEO,PRODUCTIVITY,REFERENCE,SHOPPING,SOCIAL_NETWORKING,SPORTS,TRAVEL,UTILITIES,WEATHER",
    )
    max_pages = max(1, min(int(os.getenv("APPBRAIN_IOS_MAX_PAGES", os.getenv("APPBRAIN_MAX_PAGES", "1"))), 11))
    return await _fetch_appbrain_platform(
        client, platform="IOS", source_key="apple-itunes",
        categories=categories, max_pages=max_pages,
    )


async def fetch_jetbrains_plugins(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect public JetBrains Marketplace plugin listings from its website search API."""
    terms = [x.strip() for x in os.getenv(
        "JETBRAINS_PLUGIN_TERMS",
        "python,kotlin,java,web,ide,git,database,theme,editor,testing,ai,markdown,docker,cloud,security",
    ).split(",") if x.strip()]
    limit = max(1, min(int(os.getenv("JETBRAINS_PLUGIN_LIMIT", "20")), 20))
    records: list[dict[str, Any]] = []
    for term in terms:
        try:
            payload = await _json(
                client, "https://plugins.jetbrains.com/api/searchPlugins",
                params={"search": term, "max": limit},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("JetBrains Marketplace search failed for %r: %s", term, exc)
            continue
        items = payload.get("plugins", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            plugin_id = item.get("id")
            name = item.get("name")
            link = item.get("link")
            if not plugin_id or not name:
                continue
            icon = item.get("icon")
            if isinstance(icon, str) and icon.startswith("/"):
                icon = "https://plugins.jetbrains.com" + icon
            vendor = item.get("vendor") or {}
            listing = "https://plugins.jetbrains.com" + link if isinstance(link, str) and link.startswith("/") else (
                f"https://plugins.jetbrains.com/plugin/{plugin_id}" if plugin_id else None
            )
            record = _record(
                "jetbrains-marketplace", plugin_id, "ide-plugins", name, listing,
                developer=vendor.get("name") if isinstance(vendor, dict) else None,
                summary=item.get("preview"), description=item.get("preview"),
                category=", ".join(item.get("tags") or []) if isinstance(item.get("tags"), list) else None,
                icon_url=icon,
                metadata_json={"catalog_type": "ide-plugin", "xml_id": item.get("xmlId"), "downloads": item.get("downloads"),
                               "rating": item.get("rating"), "pricing_model": item.get("pricingModel"),
                               "vendor_verified": vendor.get("isVerified") if isinstance(vendor, dict) else None},
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_gnome_extensions(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect GNOME Shell extensions from the public extensions.gnome.org search endpoint."""
    terms = [x.strip() for x in os.getenv(
        "GNOME_EXTENSION_TERMS",
        "system,desktop,appearance,weather,productivity,network,clipboard,window,monitor,media,keyboard,menu,privacy,accessibility",
    ).split(",") if x.strip()]
    records: list[dict[str, Any]] = []
    for term in terms:
        try:
            payload = await _json(
                client, "https://extensions.gnome.org/extension-query/",
                params={"search": term},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GNOME Extensions search failed for %r: %s", term, exc)
            continue
        items = payload.get("extensions", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            extension_id = item.get("uuid") or item.get("pk")
            name = item.get("name")
            link = item.get("link")
            if not extension_id or not name:
                continue
            listing = "https://extensions.gnome.org" + link if isinstance(link, str) and link.startswith("/") else None
            icon = item.get("icon")
            if isinstance(icon, str) and icon.startswith("/"):
                icon = "https://extensions.gnome.org" + icon
            record = _record(
                "gnome-extensions", extension_id, "desktop-extensions", name, listing,
                developer=item.get("creator"), summary=item.get("description"),
                description=item.get("description"), icon_url=icon,
                app_url=item.get("url"),
                metadata_json={"catalog_type": "desktop-extension", "downloads": item.get("downloads"), "shell_version_map": item.get("shell_version_map"),
                               "extension_id": item.get("pk")},
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.08)
    return _unique(records)



async def fetch_expanded_catalogs(on_source=None, source_filter: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Fetch selected public catalogues independently; isolate source failures."""
    async def collect(name: str, job):
        try:
            records = await job
        except Exception as exc:
            log.exception("Catalogue %s failed: %s", name, exc)
            if on_source:
                on_source(name, [], exc)
            return []
        if on_source:
            on_source(name, records)
        return records

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        available_jobs = {
            "huggingface-ai-spaces": lambda: fetch_huggingface_ai_spaces(client),
            "github-ai-applications": lambda: fetch_github_ai_applications(client),
            "firefox-addons": lambda: fetch_firefox_addons(client),
            "wordpress-plugins": lambda: fetch_wordpress_plugins(client),
            "homebrew-formulae": lambda: fetch_homebrew_formulae(client),
            "scoop": lambda: fetch_scoop_buckets(client),
            "alpine-linux": lambda: fetch_alpine_packages(client),
            "google-play": lambda: fetch_google_play_appbrain(client),
            "apple-appbrain": lambda: fetch_apple_appbrain(client),
            "jetbrains-marketplace": lambda: fetch_jetbrains_plugins(client),
            "gnome-extensions": lambda: fetch_gnome_extensions(client),
            "fedora-packages": lambda: fetch_fedora_packages(client),
            "bluejetty": lambda: fetch_bluejetty(client),
            "openuse": lambda: fetch_openuse(client),
            "chocolatey": lambda: fetch_chocolatey(client),
        }
        selected = set(source_filter) if source_filter else set(available_jobs)
        unknown = selected - set(available_jobs)
        if unknown:
            raise ValueError("Unknown expanded catalogue source(s): " + ", ".join(sorted(unknown)))
        jobs = {name: collect(name, available_jobs[name]()) for name in available_jobs if name in selected}
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)

    output: dict[str, list[dict[str, Any]]] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.exception("Expanded catalogue %s failed: %s", name, result)
            output[name] = []
        else:
            output[name] = result
            log.info("Expanded catalogue %s: %d records", name, len(result))
    return output
