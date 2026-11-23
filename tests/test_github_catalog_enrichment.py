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
    assert set(by_id) == {"blue-1"}
    assert by_id["blue-1"]["icon_url"] == "https://f-droid.org/repo/icons/com.example.app.png"
    assert by_id["blue-1"]["metadata_json"]["icon_crossmatch"]["method"] == "exact_official_store_listing_id"
    # Unmatched records are counted but not rewritten; this avoids churning
    # hundreds of thousands of catalogue rows without improving metadata.
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


def test_quality_report_distinguishes_missing_and_malformed_metadata():
    from scripts.catalogue_quality_report import build_report

    report = build_report([
        {
            "source_key": "fdroid",
            "platform": "android",
            "external_id": "good",
            "name": "Good App",
            "source_url": "https://f-droid.org/packages/good",
            "icon_url": "https://f-droid.org/icons/good.png",
            "description": "A sufficiently detailed and factual application description.",
            "summary": "A useful summary for this app.",
        },
        {
            "source_key": "fedora-packages",
            "platform": "linux",
            "external_id": "package",
            "name": "Package",
            "source_url": "https://packages.fedoraproject.org/pkgs/package",
            "icon_url": "javascript:alert(1)",
            "description": None,
        },
    ], generated_at="2026-11-23T00:00:00+00:00")

    assert report["total_records"] == 2
    assert report["by_source"]["fdroid"]["fields"]["icon_url"]["present"] == 1
    assert report["by_source"]["fedora-packages"]["fields"]["icon_url"]["malformed"] == 1
    assert report["by_source"]["fedora-packages"]["fields"]["description"]["missing"] == 1
    assert report["global"]["malformed_icon_url"] == 1
    assert report["policy"]["missing_icons_are_not_automatically_errors"] is True


def test_icon_url_validation_sampling_is_stable_and_source_stratified():
    from scripts.validate_catalogue_icon_urls import sample_icons

    records = [
        {"source_key": "fdroid", "external_id": str(i), "icon_url": f"https://f-droid.org/{i}.png"}
        for i in range(10)
    ] + [
        {"source_key": "flathub", "external_id": "one", "icon_url": "https://flathub.org/one.png"},
        {"source_key": "flathub", "external_id": "missing", "icon_url": ""},
    ]
    first = sample_icons(records, per_source=3)
    second = sample_icons(list(reversed(records)), per_source=3)
    assert first == second
    assert set(first) == {"fdroid", "flathub"}
    assert len(first["fdroid"]) == 3
    assert len(first["flathub"]) == 1


def test_quality_comparison_reports_field_and_source_deltas_without_fabrication():
    from scripts.compare_catalogue_quality import compare_stats

    before = {
        "total": 2,
        "quality": {"fields": {"icon_url": {"present": 1, "missing": 1, "percent": 50.0}},
                    "sources": {"bluejetty": {"total": 2, "fields": {"icon_url": {"present": 1, "missing": 1, "percent": 50.0}}}}},
    }
    after = {
        "total": 2,
        "quality": {"fields": {"icon_url": {"present": 2, "missing": 0, "percent": 100.0}},
                    "sources": {"bluejetty": {"total": 2, "fields": {"icon_url": {"present": 2, "missing": 0, "percent": 100.0}}}}},
    }
    report = compare_stats(before, after)
    assert report["overall"]["icon_url"]["present_delta"] == 1
    assert report["overall"]["icon_url"]["percentage_point_delta"] == 50.0
    assert report["by_source"]["bluejetty"]["fields"]["icon_url"]["present_delta"] == 1
    assert "not fabricated" in report["policy"]


def test_icon_provider_index_rejects_non_https_or_malformed_icon_urls():
    from scripts.enrich_github_catalog import icon_provider_index

    providers = icon_provider_index([
        {"source_key": "fdroid", "platform": "android", "external_id": "bad-http", "icon_url": "http://f-droid.org/icon.png"},
        {"source_key": "fdroid", "platform": "android", "external_id": "bad-js", "icon_url": "javascript:alert(1)"},
        {"source_key": "fdroid", "platform": "android", "external_id": "good", "icon_url": "https://f-droid.org/icon.png"},
    ])
    assert set(providers) == {("android", "good")}


def test_google_play_icon_enrichment_accepts_only_official_listing_and_icon_hosts():
    from scripts.enrich_google_play_icons import extract_official_og_icon, official_google_play_url

    record = {
        "app_url": "https://play.google.com/store/apps/details?id=com.example.app",
        "metadata_json": {},
    }
    assert official_google_play_url(record) == "https://play.google.com/store/apps/details?id=com.example.app"
    assert official_google_play_url({"app_url": "https://play.google.com.evil.test/store/apps/details?id=x"}) is None
    html = '<html><head><meta property="og:image" content="https://play-lh.googleusercontent.com/example-icon=s0"></head></html>'
    assert extract_official_og_icon(html) == "https://play-lh.googleusercontent.com/example-icon=s0"
    bad_html = '<meta property="og:image" content="https://evil.example/icon.png">'
    assert extract_official_og_icon(bad_html) is None
    userinfo_html = '<meta property="og:image" content="https://attacker@play-lh.googleusercontent.com/icon.png">'
    assert extract_official_og_icon(userinfo_html) is None


def test_google_play_icon_enrichment_prioritizes_rating_count_and_never_selects_existing_icons():
    from scripts.enrich_google_play_icons import select_candidates
    from datetime import datetime, timezone

    records = [
        {
            "source_key": "bluejetty", "platform": "android", "external_id": "low",
            "icon_url": None, "app_url": "https://play.google.com/store/apps/details?id=low",
            "metadata_json": {"bluejetty_detail": {"ratings": {"count": 2}}},
        },
        {
            "source_key": "bluejetty", "platform": "android", "external_id": "high",
            "icon_url": None, "app_url": "https://play.google.com/store/apps/details?id=high",
            "metadata_json": {"bluejetty_detail": {"ratings": {"count": 200}}},
        },
        {
            "source_key": "bluejetty", "platform": "android", "external_id": "already",
            "icon_url": "https://cdn.example.org/icon.png",
            "app_url": "https://play.google.com/store/apps/details?id=already",
            "metadata_json": {},
        },
    ]
    candidates = select_candidates(records, limit=1, now=datetime(2026, 11, 23, tzinfo=timezone.utc))
    assert [record["external_id"] for record, _ in candidates] == ["high"]


def test_steam_detail_parser_extracts_only_source_supplied_text_and_metadata():
    from scripts.enrich_github_catalog import parse_steam_detail

    payload = {"730": {"success": True, "data": {
        "type": "game",
        "short_description": "Official short description.",
        "detailed_description": "<p>Official <strong>long description</strong>.</p>",
        "developers": ["Valve"],
        "publishers": ["Valve"],
        "genres": [{"id": "1", "description": "Action"}],
        "categories": [{"id": 1, "description": "Multi-player"}],
        "platforms": {"windows": True, "mac": False, "linux": True},
        "release_date": {"date": "Aug 21, 2012", "coming_soon": False},
        "website": "http://counter-strike.net/",
    }}}
    result = parse_steam_detail(payload, {"external_id": "730"})
    assert result["summary"] == "Official short description."
    assert result["description"] == "Official long description."
    assert result["developer"] == "Valve"
    assert result["category"] == "Action"
    assert result["app_url"] is None
    assert result["metadata_json"]["steam_detail"]["publishers"] == ["Valve"]
    assert result["metadata_json"]["steam_detail"]["platforms"]["linux"] is True


def test_steam_detail_parser_does_not_invent_values_for_unavailable_records():
    from scripts.enrich_github_catalog import parse_steam_detail

    assert parse_steam_detail({"730": {"success": False}}, {"external_id": "730"}) == {}


def test_snapcraft_detail_parser_maps_official_publisher_description_license_and_icon():
    from scripts.enrich_github_catalog import parse_snap_detail

    payload = {"snap": {
        "name": "0ad",
        "publisher": {"display-name": "Wildfire Games", "username": "play0ad", "validation": "verified"},
        "summary": "Real-time strategy game of ancient warfare",
        "description": "A free, open-source historical strategy game.",
        "license": "GPL-2.0+",
        "categories": [{"name": "games"}],
        "media": [
            {"type": "icon", "url": "https://dashboard.snapcraft.io/site_media/appmedia/icon.png"},
            {"type": "screenshot", "url": "https://dashboard.snapcraft.io/site_media/appmedia/screenshot.png"},
        ],
        "website": "https://play0ad.com",
    }}
    result = parse_snap_detail(payload, {"external_id": "0ad"})
    assert result["developer"] == "Wildfire Games"
    assert result["summary"] == "Real-time strategy game of ancient warfare"
    assert result["description"] == "A free, open-source historical strategy game."
    assert result["category"] == "games"
    assert result["license"] == "GPL-2.0+"
    assert result["app_url"] == "https://play0ad.com"
    assert result["icon_url"] == "https://dashboard.snapcraft.io/site_media/appmedia/icon.png"
    assert len(result["metadata_json"]["snap_store_detail"]["media"]) == 1


def test_snapcraft_detail_parser_rejects_non_https_icons_and_missing_snap_data():
    from scripts.enrich_github_catalog import parse_snap_detail

    result = parse_snap_detail({"snap": {
        "name": "sample",
        "media": [{"type": "icon", "url": "http://example.org/icon.png"}],
        "publisher": {},
    }}, {"external_id": "sample"})
    assert result["icon_url"] is None
    assert parse_snap_detail({}, {"external_id": "missing"}) == {}


def test_quality_report_does_not_count_placeholder_strings_as_metadata():
    from scripts.catalogue_quality_report import present

    assert not present("Unknown")
    assert not present("N/A")
    assert not present("Not provided")
    assert not present("No description provided by source.")
    assert not present("Developer not specified by source")
    assert present("Mozilla")


def test_chocolatey_detail_parser_uses_official_package_metadata_and_https_assets():
    from scripts.enrich_github_catalog import parse_chocolatey_detail

    xml = """<entry xmlns="http://www.w3.org/2005/Atom"
        xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
        xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <content type="application/xml"><m:properties>
        <d:Id>010editor</d:Id>
        <d:Version>17.0.0</d:Version>
        <d:Title>010 Editor</d:Title>
        <d:Authors>SweetScape Software Inc.</d:Authors>
        <d:Summary></d:Summary>
        <d:Description>010 Editor is a hex and process editor for binary templates.</d:Description>
        <d:ProjectUrl>https://www.sweetscape.com/010editor/</d:ProjectUrl>
        <d:IconUrl>https://cdn.jsdelivr.net/gh/example/icons/010editor.png</d:IconUrl>
        <d:Tags>editor hex binary</d:Tags>
        <d:LicenseUrl>https://www.sweetscape.com/010editor/manual/License.htm</d:LicenseUrl>
      </m:properties></content>
    </entry>"""
    result = parse_chocolatey_detail(xml, {"external_id": "010editor"})
    assert result["developer"] == "SweetScape Software Inc."
    assert result["summary"] == result["description"][:240]
    assert result["app_url"] == "https://www.sweetscape.com/010editor/"
    assert result["icon_url"] == "https://cdn.jsdelivr.net/gh/example/icons/010editor.png"
    assert result["metadata_json"]["chocolatey_detail"]["license_url"].endswith("/License.htm")


def test_chocolatey_detail_parser_rejects_http_project_and_icon_urls():
    from scripts.enrich_github_catalog import parse_chocolatey_detail

    xml = """<entry xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
        xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <content><m:properties>
        <d:Description>Official package description.</d:Description>
        <d:ProjectUrl>http://example.org/project</d:ProjectUrl>
        <d:IconUrl>http://example.org/icon.png</d:IconUrl>
      </m:properties></content>
    </entry>"""
    result = parse_chocolatey_detail(xml, {"external_id": "sample"})
    assert result["app_url"] is None
    assert result["icon_url"] is None
