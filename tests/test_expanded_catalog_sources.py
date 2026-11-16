import asyncio
import json
import logging

from expanded_catalog_sources import (
    fetch_firefox_addons,
    fetch_homebrew_formulae,
    fetch_wordpress_plugins,
    fetch_google_play_appbrain,
    fetch_apple_appbrain,
    fetch_jetbrains_plugins,
    fetch_gnome_extensions,
)
from fedora_catalog_source import fetch_fedora_packages


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    status_code = 200
    headers = {"content-type": "application/json; charset=utf-8", "content-encoding": "identity", "content-length": "2"}
    @property
    def content(self):
        return self.payload.encode("utf-8") if isinstance(self.payload, str) else json.dumps(self.payload).encode("utf-8")
    def json(self):
        return json.loads(self.payload) if isinstance(self.payload, str) else self.payload
    @property
    def text(self):
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.handler(url, kwargs))


def test_firefox_addons_uses_mozilla_public_api_and_normalizes_records(monkeypatch):
    monkeypatch.setenv("FIREFOX_ADDON_TERMS", "privacy")
    monkeypatch.setenv("FIREFOX_ADDON_PAGES", "1")
    client = FakeClient(lambda url, kwargs: {"results": [{
        "id": 123, "slug": "privacy-sample", "name": "Privacy Sample",
        "summary": "Protect browsing", "authors": [{"name": "Example Dev"}],
        "icons": {"64": "https://cdn.example/icon.png"},
        "weekly_downloads": 1234, "average_daily_users": 5000,
    }]})
    records = asyncio.run(fetch_firefox_addons(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "firefox-addons"
    assert records[0]["external_id"] == "123"
    assert records[0]["platform"] == "browser-extensions"
    assert records[0]["source_url"] == "https://addons.mozilla.org/firefox/addon/privacy-sample/"
    assert records[0]["icon_url"] == "https://cdn.example/icon.png"
    assert "addons.mozilla.org/api/v5/addons/search/" in client.calls[0][0]


def test_wordpress_plugins_uses_official_directory_api(monkeypatch):
    monkeypatch.setenv("WORDPRESS_PLUGIN_TERMS", "security")
    monkeypatch.setenv("WORDPRESS_PLUGIN_PAGES", "1")
    client = FakeClient(lambda url, kwargs: {"info": {"page": 1, "pages": 1, "results": 1}, "plugins": [{
        "slug": "secure-sample", "name": "Secure Sample", "author": "Example Author",
        "short_description": "A security plugin", "version": "2.0",
        "rating": 96, "num_ratings": 20, "active_installs": 10000,
        "icons": {"1x": "https://ps.w.org/secure-sample/icon-128x128.png"},
    }]})
    records = asyncio.run(fetch_wordpress_plugins(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "wordpress-plugins"
    assert records[0]["external_id"] == "secure-sample"
    assert records[0]["platform"] == "web-plugins"
    assert records[0]["metadata_json"]["active_installs"] == 10000
    assert "api.wordpress.org/plugins/info/1.2/" in client.calls[0][0]


def test_homebrew_formula_feed_indexes_formula_metadata():
    client = FakeClient(lambda url, kwargs: [{
        "name": "sample-tool", "full_name": "sample-tool", "desc": "A sample CLI tool",
        "homepage": "https://example.org", "license": "MIT", "tap": "homebrew/core",
        "versions": {"stable": "1.2.3"}, "bottle": {"stable": {}},
    }])
    records = asyncio.run(fetch_homebrew_formulae(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "homebrew-formulae"
    assert records[0]["external_id"] == "sample-tool"
    assert records[0]["metadata_json"]["version"] == "1.2.3"


def test_homebrew_formula_feed_logs_json_failure_diagnostics(caplog):
    client = FakeClient(lambda url, kwargs: "not valid json")
    caplog.set_level(logging.ERROR, logger="mta.catalogs.expanded")

    records = asyncio.run(fetch_homebrew_formulae(client))

    assert records == []
    assert "Homebrew formula feed failed" in caplog.text
    assert "type=JSONDecodeError" in caplog.text
    assert "status=200" in caplog.text
    assert "content_type='application/json; charset=utf-8'" in caplog.text
    assert "body_bytes=14" in caplog.text


def test_google_play_appbrain_connector_is_safely_disabled_without_key(monkeypatch):
    monkeypatch.delenv("APPBRAIN_API_KEY", raising=False)
    client = FakeClient(lambda url, kwargs: {})
    records = asyncio.run(fetch_google_play_appbrain(client))
    assert records == []
    assert client.calls == []



def test_google_play_appbrain_paginates_and_preserves_full_metadata(monkeypatch):
    monkeypatch.setenv("APPBRAIN_API_KEY", "test-key")
    monkeypatch.setenv("APPBRAIN_CATEGORIES", "GAMES")
    monkeypatch.setenv("APPBRAIN_MAX_PAGES", "2")
    def handler(url, kwargs):
        params = kwargs["params"]
        if params["offset"] == 0:
            apps = [{
                "package": "com.example.game", "name": "Example Game",
                "developer": "Example Studio", "shortDescription": "A game",
                "description": "Full description", "versionString": "2.0",
                "rating": 4.7, "iconUrl": "https://cdn.example/icon.png",
                "featureGraphicUrl": "https://cdn.example/feature.png",
            }]
            apps.extend({"package": f"com.example.game{n}", "name": f"Example Game {n}"} for n in range(1, 50))
            return {"apps": apps}
        return {"apps": []}
    client = FakeClient(handler)
    records = asyncio.run(fetch_google_play_appbrain(client))
    assert len(records) == 50
    assert records[0]["source_key"] == "google-play"
    assert records[0]["source_url"] == "https://play.google.com/store/apps/details?id=com.example.game"
    assert records[0]["metadata_json"]["featureGraphicUrl"] == "https://cdn.example/feature.png"
    assert records[0]["metadata_json"]["rating"] == 4.7
    assert [call[1]["params"]["offset"] for call in client.calls] == [0, 50]


def test_apple_appbrain_uses_ios_platform_and_app_store_listing(monkeypatch):
    monkeypatch.setenv("APPBRAIN_API_KEY", "test-key")
    monkeypatch.setenv("APPBRAIN_IOS_CATEGORIES", "GAMES")
    monkeypatch.setenv("APPBRAIN_IOS_MAX_PAGES", "1")
    monkeypatch.setenv("APPBRAIN_ENABLE_IOS", "true")
    client = FakeClient(lambda url, kwargs: {"apps": [{
        "package": "ios-123456789", "trackId": "123456789", "name": "Example iOS App",
        "artistName": "Example Developer", "developerName": "AppBrain Developer", "appStoreUrl": "https://apps.apple.com/us/app/example/id123456789", "description": "iOS description",
        "iconUrl": "https://cdn.example/ios.png", "price": "$0.00",
    }]})
    records = asyncio.run(fetch_apple_appbrain(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "apple-itunes"
    assert records[0]["external_id"] == "123456789"
    assert records[0]["platform"] == "ios"
    assert records[0]["source_url"] == "https://apps.apple.com/us/app/example/id123456789"
    assert records[0]["developer"] == "AppBrain Developer"
    assert client.calls[0][1]["params"]["platform"] == "IOS"
    assert records[0]["metadata_json"]["price"] == "$0.00"

def test_jetbrains_marketplace_connector_maps_plugin_metadata(monkeypatch):
    monkeypatch.setenv("JETBRAINS_PLUGIN_TERMS", "python")
    client = FakeClient(lambda url, kwargs: {"plugins": [{
        "id": 631, "xmlId": "Pythonid", "link": "/plugin/631-python",
        "name": "Python", "preview": "Python language support",
        "downloads": 13015105, "rating": 3.37, "pricingModel": "FREE",
        "icon": "/files/631/icon/default.svg", "tags": ["Programming Language"],
        "vendor": {"name": "JetBrains", "isVerified": True},
    }]})
    records = asyncio.run(fetch_jetbrains_plugins(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "jetbrains-marketplace"
    assert records[0]["source_url"] == "https://plugins.jetbrains.com/plugin/631-python"
    assert records[0]["icon_url"] == "https://plugins.jetbrains.com/files/631/icon/default.svg"
    assert records[0]["metadata_json"]["downloads"] == 13015105


def test_gnome_extensions_connector_maps_public_extension_metadata(monkeypatch):
    monkeypatch.setenv("GNOME_EXTENSION_TERMS", "weather")
    client = FakeClient(lambda url, kwargs: {"extensions": [{
        "uuid": "weather@example.org", "pk": 4919, "name": "Weather",
        "creator": "Example Dev", "description": "Weather on desktop",
        "link": "/extension/4919/weather/",
        "icon": "/extension-data/icons/icon_4919.png",
        "url": "https://github.com/example/weather", "downloads": 28158,
        "shell_version_map": {"45": {"version": 1}},
    }]})
    records = asyncio.run(fetch_gnome_extensions(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "gnome-extensions"
    assert records[0]["source_url"] == "https://extensions.gnome.org/extension/4919/weather/"
    assert records[0]["icon_url"] == "https://extensions.gnome.org/extension-data/icons/icon_4919.png"
    assert records[0]["metadata_json"]["downloads"] == 28158


def test_fedora_static_index_collects_package_names_and_detail_links():
    def handler(url, kwargs):
        if url.endswith("index-static.html"):
            return '<html><a href="/index/fa.html">fa</a></html>'
        return '<html><a href="/pkgs/fedora-app/fedora-app/index.html">fedora-app</a></html>'
    client = FakeClient(handler)
    records = asyncio.run(fetch_fedora_packages(client))
    assert len(records) == 1
    assert records[0]["source_key"] == "fedora-packages"
    assert records[0]["external_id"] == "fedora-app/fedora-app"
    assert records[0]["source_url"] == "https://packages.fedoraproject.org/pkgs/fedora-app/fedora-app/index.html"
    assert records[0]["metadata_json"]["catalog_type"] == "rpm-package"



def test_expanded_catalogue_filter_fetches_only_selected_sources(monkeypatch):
    import expanded_catalog_sources as module

    called = []

    async def fake_homebrew(client):
        called.append("homebrew-formulae")
        return [{"source_key": "homebrew-formulae", "external_id": "one"}]

    async def fake_alpine(client):
        called.append("alpine-linux")
        return [{"source_key": "alpine-linux", "external_id": "two"}]

    monkeypatch.setattr(module, "fetch_homebrew_formulae", fake_homebrew)
    monkeypatch.setattr(module, "fetch_alpine_packages", fake_alpine)

    records = asyncio.run(module.fetch_expanded_catalogs(
        source_filter={"alpine-linux", "homebrew-formulae"}
    ))

    assert set(records) == {"alpine-linux", "homebrew-formulae"}
    assert set(called) == {"alpine-linux", "homebrew-formulae"}
    assert len(records["alpine-linux"]) == 1
    assert len(records["homebrew-formulae"]) == 1


def test_expanded_catalogue_filter_rejects_unknown_sources():
    import pytest
    import expanded_catalog_sources as module

    with pytest.raises(ValueError, match="Unknown expanded catalogue source"):
        asyncio.run(module.fetch_expanded_catalogs(source_filter={"not-a-source"}))
