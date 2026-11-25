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
