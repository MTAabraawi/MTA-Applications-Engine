import asyncio

from expanded_catalog_sources import (
    fetch_firefox_addons,
    fetch_homebrew_formulae,
    fetch_wordpress_plugins,
    fetch_google_play_appbrain,
    fetch_jetbrains_plugins,
    fetch_gnome_extensions,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


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


def test_google_play_appbrain_connector_is_safely_disabled_without_key(monkeypatch):
    monkeypatch.delenv("APPBRAIN_API_KEY", raising=False)
    client = FakeClient(lambda url, kwargs: {})
    records = asyncio.run(fetch_google_play_appbrain(client))
    assert records == []
    assert client.calls == []


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
