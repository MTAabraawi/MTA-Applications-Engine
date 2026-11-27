import asyncio
import json

import scripts.run_unified_catalogue as pipeline
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



def test_official_page_parser_uses_explicit_meta_and_schema_org_only():
    from scripts.enrich_catalogue_official_pages import parse_official_metadata

    html = """
    <html><head>
      <meta name="description" content="Official application description">
      <meta name="author" content="Publisher Ltd">
      <meta property="og:image" content="https://cdn.example/app.png">
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"SoftwareApplication",
       "name":"Example","applicationCategory":"Utilities","license":"MIT",
       "url":"https://publisher.example/product",
       "aggregateRating":{"ratingValue":"4.7","ratingCount":"125"}}
      </script>
    </head></html>
    """
    result = parse_official_metadata(html, "https://store.example/apps/example")
    assert result["description"] == "Official application description"
    assert result["developer"] == "Publisher Ltd"
    assert result["icon_url"] == "https://cdn.example/app.png"
    assert result["app_url"] == "https://publisher.example/product"
    assert result["category"] == "Utilities"
    assert result["_rating"] == {"value": "4.7", "count": "125"}


def test_official_page_parser_does_not_accept_http_images_or_guess_reviews():
    from scripts.enrich_catalogue_official_pages import parse_official_metadata

    result = parse_official_metadata(
        '<meta property="og:image" content="http://bad.example/icon.png">',
        "https://store.example/app",
    )
    assert "icon_url" not in result
    assert "_rating" not in result
    assert "_reviews" not in result



def test_detail_enrichment_is_awaited(monkeypatch):
    called = []

    async def fake_detail_main():
        called.append("completed")

    monkeypatch.setattr(pipeline.detail_enrichment, "main", fake_detail_main)
    result = asyncio.run(pipeline.run_detail_enrichment())

    assert called == ["completed"]
    assert result == {
        "status": "executed",
        "adapters": ["bluejetty", "fedora-packages"],
    }

def test_official_page_parser_prefers_explicit_icon_and_reads_canonical_and_publisher():
    from scripts.enrich_catalogue_official_pages import parse_official_metadata

    html = """
    <html><head>
      <link rel="icon" href="/assets/app-icon.png">
      <link rel="canonical" href="https://publisher.example/apps/example">
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"SoftwareApplication",
       "publisher":{"@type":"Organization","name":"Verified Publisher"},
       "applicationCategory":["Utilities","Productivity"],
       "image":{"url":"https://cdn.example/schema-icon.png"}}
      </script>
    </head></html>
    """
    result = parse_official_metadata(html, "https://store.example/listing/example")
    assert result["icon_url"] == "https://store.example/assets/app-icon.png"
    assert result["app_url"] == "https://publisher.example/apps/example"
    assert result["developer"] == "Verified Publisher"
    assert result["category"] == "Utilities, Productivity"
