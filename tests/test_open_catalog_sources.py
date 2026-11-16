import asyncio

import open_catalog_sources as module


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.handler(url, kwargs))


def test_bluejetty_follows_meta_next_and_keeps_official_store_urls(monkeypatch):
    monkeypatch.setenv("BLUEJETTY_MAX_APPS", "200")
    first_next = "https://bluejetty.ai/v1/apps?limit=100&cursor=next-page"

    def handler(url, kwargs):
        if url.endswith("cursor=next-page"):
            return {
                "data": [{
                    "blueJettyId": "12345679",
                    "name": "Example Android App",
                    "platforms": ["android"],
                    "playStoreUrl": "https://play.google.com/store/apps/details?id=org.example.app",
                }],
                "meta": {"next": None},
            }
        return {
            "data": [{
                "blueJettyId": "12345678",
                "name": "Example iOS App",
                "platforms": ["ios"],
                "appStoreUrl": "https://apps.apple.com/us/app/example/id12345678",
                "jsonUrl": "https://bluejetty.ai/v1/index/apps/12345678",
            }],
            "meta": {"next": first_next},
        }

    client = FakeClient(handler)
    records = asyncio.run(module.fetch_bluejetty(client))

    assert len(client.calls) == 2
    assert len(records) == 2
    assert records[0]["source_key"] == "bluejetty"
    assert records[0]["platform"] == "ios"
    assert records[0]["source_url"] == "https://apps.apple.com/us/app/example/id12345678"
    assert records[1]["platform"] == "android"
    assert records[1]["source_url"].startswith("https://play.google.com/store/apps/")
    assert records[0]["metadata_json"]["bluejetty_id"] == "12345678"


def test_openuse_imports_each_supported_platform_and_paginates(monkeypatch):
    monkeypatch.setenv("OPENUSE_MAX_APPS", "500")
    apps = [{
        "slug": f"sample-{n}",
        "name": f"Sample App {n}",
        "url": f"https://openuse.app/apps/sample-{n}",
        "platforms": ["windows", "linux", "android"],
        "openSource": True,
        "latestVersion": "1.2.3",
    } for n in range(100)]

    def handler(url, kwargs):
        offset = kwargs["params"]["offset"]
        if offset == 0:
            return {"data": apps, "meta": {"total": 101, "limit": 100, "offset": 0}}
        return {
            "data": [{
                "slug": "last-app", "name": "Last App",
                "url": "https://openuse.app/apps/last-app", "platforms": ["web"],
            }],
            "meta": {"total": 101, "limit": 100, "offset": 100},
        }

    client = FakeClient(handler)
    records = asyncio.run(module.fetch_openuse(client))

    assert len(client.calls) == 2
    assert len(records) == 301
    assert {row["platform"] for row in records} == {"windows", "linux", "android", "web"}
    assert records[0]["source_key"] == "openuse"
    assert records[0]["metadata_json"]["open_source"] is True


def test_chocolatey_uses_supported_cli_and_maps_package_pages(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/ProgramData/chocolatey/bin/choco.exe")
    monkeypatch.setattr(
        module,
        "_chocolatey_search_page",
        lambda choco, page, page_size: [("Git", "2.45.0"), ("7zip", "24.08")],
    )

    records = asyncio.run(module.fetch_chocolatey())

    assert len(records) == 2
    assert records[0]["source_key"] == "chocolatey"
    assert records[0]["external_id"] == "Git"
    assert records[0]["source_url"] == "https://community.chocolatey.org/packages/Git"
    assert records[0]["metadata_json"]["version"] == "2.45.0"
    assert records[0]["metadata_json"]["source_method"] == "supported Chocolatey CLI search"


def test_chocolatey_skips_when_cli_is_not_installed(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert asyncio.run(module.fetch_chocolatey()) == []
