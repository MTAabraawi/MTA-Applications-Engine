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
