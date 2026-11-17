from fedora_catalog_source import parse_fedora_detail
from open_catalog_sources import parse_bluejetty_detail


def test_bluejetty_detail_parser_maps_publisher_and_store_metadata():
    payload = {
        "data": {
            "blueJettyId": "00000003",
            "name": "Dojo",
            "description": "A personalized meditation app.",
            "developerName": "Medidojo Inc.",
            "category": "Health & Fitness",
            "websiteUrl": "https://www.medidojo.com/",
            "publisherAuthorization": {"status": "verified"},
            "storeObservations": [{
                "platform": "ios", "description": "A longer store description",
                "averageRating": 4.6, "ratingCount": 48,
                "listingUrl": "https://apps.apple.com/us/app/example/id123456789",
            }],
            "platforms": [{"platform": "ios", "listingUrl": "https://apps.apple.com/us/app/example/id123456789"}],
            "canonicalPageUrl": "https://bluejetty.ai/apps/dojo",
            "recordModifiedAt": "2026-11-16T12:00:00Z",
        }
    }
    result = parse_bluejetty_detail(payload)
    assert result["developer"] == "Medidojo Inc."
    assert result["summary"] == "A personalized meditation app."
    assert result["description"] == "A longer store description"
    assert result["category"] == "Health & Fitness"
    assert result["app_url"] == "https://www.medidojo.com/"
    assert result["metadata_json"]["bluejetty_detail"]["store_observations"][0]["averageRating"] == 4.6


def test_bluejetty_detail_parser_rejects_non_https_icon_and_website():
    result = parse_bluejetty_detail({"data": {
        "name": "Example", "description": "Description",
        "websiteUrl": "http://example.org", "iconUrl": "javascript:alert(1)",
    }})
    assert result["app_url"] is None
    assert result["icon_url"] is None


def test_fedora_detail_parser_extracts_description_release_rows_and_source_repo():
    page = """
    <html><body>
      <p>Cross-Platform RTS Game of Ancient Warfare</p>
      <p>0 A.D. is a free, open-source real-time strategy game developed by a volunteer team.</p>
      <p>You can contact the maintainers of this package via email.</p>
      <table>
        <tr><th>Release</th><th>Stable</th><th>Testing</th></tr>
        <tr><td>Fedora 45</td><td>0.28.0-7.fc45</td><td>-</td></tr>
      </table>
      <a href="https://src.fedoraproject.org/rpms/0ad">Source</a>
      <a href="https://play0ad.com">Homepage</a>
    </body></html>
    """
    result = parse_fedora_detail(page)
    assert result["summary"] == "Cross-Platform RTS Game of Ancient Warfare"
    assert "real-time strategy game" in result["description"]
    assert result["app_url"] == "https://play0ad.com"
    detail = result["metadata_json"]["fedora_detail"]
    assert detail["source_repository_url"] == "https://src.fedoraproject.org/rpms/0ad"
    assert detail["releases"][0]["release"] == "Fedora 45"
    assert detail["releases"][0]["stable"] == "0.28.0-7.fc45"
