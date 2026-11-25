import json

from scripts.run_unified_catalogue import build_source_quality


def test_unified_quality_report_covers_each_source_and_required_fields():
    rows = [
        {
            "source_key": "fdroid",
            "external_id": "one",
            "icon_url": "https://cdn.example/icon.png",
            "app_url": "https://example.org/app",
            "description": "A verified description",
            "source_url": "https://f-droid.org/packages/one",
        },
        {
            "source_key": "fdroid",
            "external_id": "two",
            "icon_url": None,
            "app_url": None,
            "description": None,
            "source_url": "https://f-droid.org/packages/two",
        },
        {
            "source_key": "flathub",
            "external_id": "three",
            "icon_url": "http://invalid.example/icon.png",
            "source_url": "https://flathub.org/apps/three",
        },
    ]
    report = build_source_quality(rows)
    assert set(report) == {"fdroid", "flathub"}
    assert report["fdroid"]["records"] == 2
    assert report["fdroid"]["fields"]["icon_url"] == {
        "present": 1, "missing": 1, "percent": 50.0
    }
    assert report["fdroid"]["fields"]["app_url"]["missing"] == 1
    assert report["flathub"]["fields"]["icon_url"]["present"] == 0
    assert report["flathub"]["fields"]["source_url"]["present"] == 1



def test_metadata_values_only_uses_explicit_source_fields_and_https_urls():
    from scripts.run_unified_catalogue import _metadata_values

    result = _metadata_values({
        "publisher": {"name": "Verified Publisher"},
        "short_description": "Official short summary",
        "categories": ["Utilities", "Security"],
        "homepage": "https://publisher.example/app",
        "icon": "http://insecure.example/icon.png",
        "license": "MIT",
    })
    assert result["developer"] == "Verified Publisher"
    assert result["summary"] == "Official short summary"
    assert result["category"] == "Utilities, Security"
    assert result["app_url"] == "https://publisher.example/app"
    assert result["license"] == "MIT"
    assert "icon_url" not in result


def test_metadata_values_rejects_unverified_guesses():
    from scripts.run_unified_catalogue import _metadata_values

    assert _metadata_values({"name": "Example App", "stars": 5, "reviews": ["great"]}) == {}
