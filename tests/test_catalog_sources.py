from catalog_sources import _record, _unique


def test_normalizer_keeps_only_https_optional_urls():
    item = _record(
        "source", "id-1", "windows", "Sample",
        "https://catalog.example/item",
        app_url="http://insecure.example/download",
        icon_url="https://cdn.example/icon.png",
    )
    assert item is not None
    assert item["app_url"] is None
    assert item["icon_url"] == "https://cdn.example/icon.png"
    assert item["platform"] == "windows"


def test_normalizer_rejects_missing_identity():
    assert _record("source", "", "web", "Name", "https://example.org") is None
    assert _record("source", "id", "web", "", "https://example.org") is None


def test_deduplication_uses_source_and_external_id():
    first = _record("source-a", "same", "web", "A", "https://example.org/a")
    second = _record("source-a", "same", "web", "B", "https://example.org/b")
    third = _record("source-b", "same", "web", "C", "https://example.org/c")
    assert len(_unique([first, second, third])) == 2


def test_fdroid_v2_nested_metadata_is_normalized():
    import asyncio
    from catalog_sources import fetch_fdroid

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "apps": {
                    "org.example.reader": {
                        "metadata": {
                            "license": "GPL-3.0-or-later",
                            "authorName": "Example Team",
                            "sourceCode": "https://github.com/example/reader",
                            "localized": {
                                "en-US": {
                                    "name": {"en-US": "Example Reader"},
                                    "summary": {"en-US": "Read documents"},
                                    "description": {"en-US": "A sample reader app"},
                                }
                            },
                            "categories": ["Reading", "Office"],
                        },
                        "added": 1700000000000,
                    }
                }
            }

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    records = asyncio.run(fetch_fdroid(FakeClient()))
    assert len(records) == 1
    assert records[0]["external_id"] == "org.example.reader"
    assert records[0]["name"] == "Example Reader"
    assert records[0]["platform"] == "android"
    assert records[0]["category"] == "Reading, Office"
    assert records[0]["source_url"] == "https://f-droid.org/packages/org.example.reader/"


def test_fdroid_app_array_index_is_supported():
    import asyncio
    from catalog_sources import fetch_fdroid

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "apps": [{
                    "packageName": "org.example.notes",
                    "name": "Example Notes",
                    "summary": "A notes app",
                    "license": "MIT",
                    "categories": ["Writing"],
                }]
            }

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    records = asyncio.run(fetch_fdroid(FakeClient()))
    assert len(records) == 1
    assert records[0]["external_id"] == "org.example.notes"
    assert records[0]["name"] == "Example Notes"


def test_fdroid_index_v2_packages_map_is_supported():
    import asyncio
    from catalog_sources import fetch_fdroid

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "repo": {"name": "F-Droid"},
                "packages": {
                    "org.example.calendar": {
                        "metadata": {
                            "license": "GPL-3.0-or-later",
                            "name": {"en-US": "Example Calendar"},
                            "localized": {"summary": {"en-US": "Manage events"}},
                            "categories": ["Time"],
                        },
                        "versions": {},
                    }
                },
            }

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    records = asyncio.run(fetch_fdroid(FakeClient()))
    assert len(records) == 1
    assert records[0]["external_id"] == "org.example.calendar"
    assert records[0]["name"] == "Example Calendar"
    assert records[0]["category"] == "Time"


def test_fdroid_index_v2_field_locale_maps_resolve_english_text():
    import asyncio
    from catalog_sources import fetch_fdroid

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "repo": {"name": "F-Droid"},
                "packages": {
                    "org.example.reader": {
                        "metadata": {
                            "localized": {
                                "name": {"en-US": "Localized Reader", "de": "Leser"},
                                "summary": {"en-US": "Read books"},
                                "description": {"en-US": "A localized reading application"},
                            },
                            "categories": ["Reading"],
                        },
                        "versions": {},
                    }
                },
            }

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    records = asyncio.run(fetch_fdroid(FakeClient()))
    assert records[0]["name"] == "Localized Reader"
    assert records[0]["summary"] == "Read books"
    assert records[0]["description"] == "A localized reading application"


def test_snap_store_public_search_metadata_is_normalized():
    import asyncio
    from catalog_sources import fetch_snap_store

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "id": "snap-unique-id",
                "name": "sample-reader",
                "title": "Sample Reader",
                "summary": "Read documents",
                "description": "A sample document reader",
                "status": "available",
                "store-url": "https://snapcraft.io/sample-reader",
                "icon": "https://dashboard.snapcraft.io/site_media/sample.png",
                "publisher": {"username": "sample-team", "display-name": "Sample Team"},
                "version": "2.0",
                "channel": "stable",
                "license": "MIT",
                "confinement": "strict",
            }]

    class FakeClient:
        async def get(self, url, **kwargs):
            assert url == "https://api.snapcraft.io/v2/find"
            assert kwargs["params"]["q"]
            assert kwargs["headers"]["Snap-Device-Series"] == "16"
            return FakeResponse()

    records = asyncio.run(fetch_snap_store(FakeClient()))
    assert len(records) == 1
    assert records[0]["source_key"] == "snap-store"
    assert records[0]["external_id"] == "snap-unique-id"
    assert records[0]["name"] == "Sample Reader"
    assert records[0]["platform"] == "linux"
    assert records[0]["source_url"] == "https://snapcraft.io/sample-reader"
    assert records[0]["developer"] == "Sample Team"
    assert records[0]["metadata_json"]["version"] == "2.0"


def test_snap_store_ignores_unavailable_or_invalid_entries():
    import asyncio
    from catalog_sources import fetch_snap_store

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"id": "gone", "name": "gone", "title": "Gone", "status": "unavailable",
                 "store-url": "https://snapcraft.io/gone"},
                {"id": "bad", "name": "bad", "title": "Bad", "status": "available",
                 "store-url": "http://snapcraft.io/bad"},
            ]

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    assert asyncio.run(fetch_snap_store(FakeClient())) == []
