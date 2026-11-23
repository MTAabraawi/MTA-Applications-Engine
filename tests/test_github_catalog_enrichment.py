from scripts.enrich_github_catalog import (
    apply_results,
    crossmatch_icons,
    icon_provider_index,
    store_ids,
)


def test_store_ids_extracts_exact_google_play_and_apple_listing_ids():
    found = store_ids({
        "listings": [
            {"url": "https://play.google.com/store/apps/details?id=com.example.app"},
            {"url": "https://apps.apple.com/us/app/example/id123456789"},
            {"url": "https://example.com/apps/com.example.app"},
        ]
    })
    assert found == {("android", "com.example.app"), ("ios", "123456789")}


def test_icon_provider_index_uses_only_official_store_sources():
    records = [
        {"source_key": "fdroid", "platform": "android", "external_id": "com.example.app", "icon_url": "https://f-droid.org/icon.png"},
        {"source_key": "bluejetty", "platform": "android", "external_id": "com.other.app", "icon_url": "https://bluejetty.ai/icon.png"},
        {"source_key": "apple-itunes", "platform": "ios", "external_id": "123456789", "icon_url": "https://is1-ssl.mzstatic.com/icon.png"},
        {"source_key": "fdroid", "platform": "linux", "external_id": "not-an-app", "icon_url": "https://f-droid.org/package.png"},
    ]
    providers = icon_provider_index(records)
    assert set(providers) == {("android", "com.example.app"), ("ios", "123456789")}


def test_bluejetty_icon_crossmatch_requires_exact_official_listing_id():
    records = [
        {
            "source_key": "bluejetty",
            "external_id": "blue-1",
            "platform": "android",
            "name": "Example App",
            "metadata_json": {
                "listings": [{"url": "https://play.google.com/store/apps/details?id=com.example.app"}]
            },
        },
        {
            "source_key": "bluejetty",
            "external_id": "blue-2",
            "platform": "android",
            "name": "Similar Name But No Exact ID",
            "metadata_json": {"website": "https://example.com/app"},
        },
    ]
    providers = {
        ("android", "com.example.app"): {
            "source_key": "fdroid",
            "icon_url": "https://f-droid.org/repo/icons/com.example.app.png",
        }
    }
    updates, counts = crossmatch_icons(records, providers, "2026-11-23T00:00:00+00:00")
    by_id = {row["external_id"]: row for row in updates}
    assert by_id["blue-1"]["icon_url"] == "https://f-droid.org/repo/icons/com.example.app.png"
    assert by_id["blue-1"]["metadata_json"]["icon_crossmatch"]["method"] == "exact_official_store_listing_id"
    assert by_id["blue-2"].get("icon_url") is None
    assert by_id["blue-2"]["metadata_json"]["icon_crossmatch"]["status"] == "unmatched"
    assert counts == {"scanned": 2, "matched": 1, "unmatched": 1, "ambiguous": 0}


def test_crossmatch_does_not_overwrite_existing_source_icon():
    records = [{
        "source_key": "bluejetty",
        "external_id": "blue-1",
        "platform": "android",
        "icon_url": "https://cdn.bluejetty.ai/existing.png",
        "metadata_json": {"listings": [{"url": "https://play.google.com/store/apps/details?id=com.example.app"}]},
    }]
    updates, counts = crossmatch_icons(
        records,
        {("android", "com.example.app"): {"source_key": "fdroid", "icon_url": "https://f-droid.org/icon.png"}},
        "2026-11-23T00:00:00+00:00",
    )
    assert updates == []
    assert counts == {"scanned": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}


def test_enrichment_only_fills_missing_fields_and_preserves_existing_values():
    row = {
        "source_key": "bluejetty",
        "external_id": "blue-1",
        "developer": "Verified Publisher",
        "summary": None,
        "description": "Existing verified description",
        "metadata_json": {"source": {"id": "blue-1"}},
    }
    updates, counts = apply_results(
        [(row, {
            "developer": "Different guessed name",
            "summary": "Summary supplied by source",
            "description": "Replacement description",
            "metadata_json": {"bluejetty_detail": {"verified": True}},
        }, None)],
        "bluejetty",
        "2026-11-23T00:00:00+00:00",
    )
    result = updates[0]
    assert result["developer"] == "Verified Publisher"
    assert result["summary"] == "Summary supplied by source"
    assert result["description"] == "Existing verified description"
    assert result["metadata_json"]["source"]["id"] == "blue-1"
    assert result["metadata_json"]["bluejetty_detail"]["verified"] is True
    assert counts["fields_filled"] == {"summary": 1}
