import asyncio
import json
from pathlib import Path

import catalog_sources


def test_store_registry_includes_all_registered_sources():
    registry_path = Path(__file__).resolve().parents[1] / "store_sources.json"
    entries = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(entries) == 33
    assert len({entry["id"] for entry in entries}) == 33
    assert {entry["integration_mode"] for entry in entries} == {"live_api", "external_only"}
    live = {entry["source_key"] for entry in entries if entry.get("source_key")}
    assert {
        "fdroid", "izzyondroid", "snap-store", "flathub", "homebrew-cask",
        "apple-itunes", "apple-macos", "winget-run-community", "steam", "mta-curated-web",
    } <= live


def test_snap_store_connector_uses_public_find_api_and_maps_listing_metadata(monkeypatch):
    monkeypatch.setattr(catalog_sources, "SNAP_SEARCH_TERMS", ["browser"])

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [{
                    "name": "example-browser",
                    "title": "Example Browser",
                    "summary": "Browse the web",
                    "description": "An example browser",
                    "publisher": {"display-name": "Example Publisher"},
                    "id": "snap-example-id",
                    "media": [{"type": "icon", "url": "https://cdn.example/icon.png"}],
                    "categories": ["productivity"],
                }]
            }

    class FakeClient:
        headers_seen = None
        params_seen = None
        url_seen = None

        async def get(self, url, **kwargs):
            self.url_seen = url
            self.headers_seen = kwargs.get("headers")
            self.params_seen = kwargs.get("params")
            return FakeResponse()

    client = FakeClient()
    records = asyncio.run(catalog_sources.fetch_snap_store(client))
    assert client.url_seen == "https://api.snapcraft.io/v2/snaps/find"
    assert client.params_seen == {"q": "browser"}
    assert client.headers_seen["Snap-Device-Series"] == "16"
    assert len(records) == 1
    assert records[0]["source_key"] == "snap-store"
    assert records[0]["external_id"] == "example-browser"
    assert records[0]["source_url"] == "https://snapcraft.io/example-browser"
    assert records[0]["icon_url"] == "https://cdn.example/icon.png"


def test_apple_macos_connector_uses_mac_software_entity(monkeypatch):
    monkeypatch.setattr(catalog_sources, "APPLE_SEARCH_TERMS", ["editor"])

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{
                "trackId": 987,
                "trackName": "Example Mac Editor",
                "trackViewUrl": "https://apps.apple.com/us/app/example/id987",
                "artistName": "Example Developer",
                "kind": "mac-software",
                "artworkUrl512": "https://cdn.example/mac-icon.png",
            }]}

    class FakeClient:
        params_seen = None

        async def get(self, url, **kwargs):
            self.params_seen = kwargs.get("params")
            return FakeResponse()

    client = FakeClient()
    records = asyncio.run(catalog_sources.fetch_apple_macos(client))
    assert client.params_seen["entity"] == "macSoftware"
    assert records[0]["source_key"] == "apple-macos"
    assert records[0]["platform"] == "macos"
    assert records[0]["name"] == "Example Mac Editor"




def test_steam_connector_uses_public_storefront_search(monkeypatch):
    monkeypatch.setattr(catalog_sources, "STEAM_SEARCH_TERMS", ["strategy"])

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [{
                "id": 1234,
                "name": "Example Strategy",
                "type": "app",
                "tiny_image": "https://cdn.example/steam.png",
                "price": {"currency": "USD", "final": 999},
            }]}

    class FakeClient:
        params_seen = None
        url_seen = None

        async def get(self, url, **kwargs):
            self.url_seen = url
            self.params_seen = kwargs.get("params")
            return FakeResponse()

    client = FakeClient()
    records = asyncio.run(catalog_sources.fetch_steam(client))
    assert client.url_seen == "https://store.steampowered.com/api/storesearch/"
    assert client.params_seen == {"term": "strategy", "l": "english", "cc": "us"}
    assert len(records) == 1
    assert records[0]["source_key"] == "steam"
    assert records[0]["external_id"] == "1234"
    assert records[0]["platform"] == "pc-games"
    assert records[0]["icon_url"] == "https://cdn.example/steam.png"
