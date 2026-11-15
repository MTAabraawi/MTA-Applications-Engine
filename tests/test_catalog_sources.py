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



def test_izzyondroid_index_uses_independent_source_identity_and_listing_url():
    import asyncio
    from catalog_sources import fetch_izzyondroid

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "repo": {"name": "IzzyOnDroid"},
                "packages": {
                    "org.example.izzy": {
                        "metadata": {
                            "license": "MIT",
                            "authorName": "Example Developer",
                            "localized": {
                                "name": {"en-US": "Izzy Example"},
                                "summary": {"en-US": "An example Android app"},
                                "description": {"en-US": "Metadata from IzzyOnDroid"},
                            },
                            "categories": ["Tools"],
                        },
                        "versions": {},
                    }
                },
            }

    class FakeClient:
        requested_url = None

        async def get(self, url, **kwargs):
            self.requested_url = url
            return FakeResponse()

    client = FakeClient()
    records = asyncio.run(fetch_izzyondroid(client))
    assert client.requested_url == "https://apt.izzysoft.de/fdroid/repo/index-v2.json"
    assert len(records) == 1
    assert records[0]["source_key"] == "izzyondroid"
    assert records[0]["external_id"] == "org.example.izzy"
    assert records[0]["name"] == "Izzy Example"
    assert records[0]["platform"] == "android"
    assert records[0]["source_url"] == "https://apt.izzysoft.de/fdroid/index/apk/org.example.izzy"


def test_arch_linux_public_package_search_metadata_and_size():
    import asyncio
    from catalog_sources import fetch_arch_linux

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"results": [{
                "pkgname": "sample-app", "repo": "extra", "arch": "x86_64",
                "pkgver": "1.2.3-1", "pkgdesc": "A sample desktop application",
                "packager": "Arch Linux Team", "compressed_size": 123456,
                "url": "https://example.org"
            }]}

    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse()

    records = asyncio.run(fetch_arch_linux(FakeClient()))
    assert len(records) == 1
    assert records[0]["source_key"] == "arch-linux"
    assert records[0]["name"] == "sample-app"
    assert records[0]["source_url"] == "https://archlinux.org/packages/extra/x86_64/sample-app/"
    assert records[0]["metadata_json"]["size_bytes"] == 123456


def test_debian_package_connector_extracts_real_description_and_binary_size():
    import asyncio
    from catalog_sources import fetch_debian_packages

    class FakeResponse:
        def __init__(self, payload=None, text=""):
            self._payload, self.text = payload, text
            self.status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return self._payload

    class FakeClient:
        async def get(self, url, **kwargs):
            if "sources.debian.org/api/search/" in url:
                return FakeResponse({"results": {"exact": {"name": "7zip"}, "other": []}})
            return FakeResponse(text="""
                <h2>Links for 7zip</h2>
                <h2>7-Zip file archiver with a high compression ratio</h2>
                <p>7-Zip is a file archiver with a high compression ratio.</p>
                <p>Tags: role::program, works-with::archive</p>
                <h2>Other Packages Related to 7zip</h2>
                <h2>Download 7zip</h2>
                <table><tr><th>amd64</th><td>1,668.8 kB</td><td>7,542.0 kB</td></tr></table>
                <h1>Package: 7zip (26.06+dfsg-1)</h1>
            """)
    records = asyncio.run(fetch_debian_packages(FakeClient()))
    assert len(records) == 1
    assert records[0]["source_key"] == "debian-packages"
    assert records[0]["name"] == "7zip"
    assert "file archiver" in records[0]["summary"]
    assert records[0]["metadata_json"]["size_bytes"] == 1708851
    assert records[0]["metadata_json"]["version"] == "26.06+dfsg-1"
