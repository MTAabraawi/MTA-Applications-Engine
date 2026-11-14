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
                            "localized": {
                                "en-US": {
                                    "name": "Example Calendar",
                                    "summary": "Manage events",
                                }
                            },
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
