# Catalogue metadata quality audit

## Scope

This audit covers the Git-backed public catalogue and the application/store pages that consume it. It is designed to improve source-backed metadata without manufacturing application facts.

## Enrichment rules

- Existing non-empty source fields are preserved.
- Missing descriptions, summaries, developers, licences, categories, links and icons are filled only from the matching publisher or catalogue provider's official record/API.
- Cross-store icon matching is accepted only for an exact platform + listing identifier match. The matched provider and identifier are retained in provenance metadata.
- Google Play artwork is accepted only from an official Google Play listing and its Google-hosted Open Graph image.
- Snap Store metadata is parsed from Snapcraft's official API.
- Steam descriptions and publisher fields come from the official Steam Store appdetails endpoint; header/capsule art is not mislabeled as an application icon.
- Chocolatey package metadata is parsed from the official community NuGet feed; package tags are not mislabeled as application categories.
- Icon URLs must use HTTPS, and URL validation results are reported separately from metadata presence.
- Unavailable, ambiguous, blocked, or non-applicable fields remain missing. A placeholder is never counted as substantive metadata.

## Source-aware applicability

The catalogue contains mobile apps, desktop applications, games, extensions, plugins and package-manager records. A graphical icon is not necessarily applicable to every command-line package or library. Likewise, a package's maintainer is not necessarily an application developer, and a missing licence must not be inferred from a package family. Quality reports therefore show per-source coverage and applicability notes rather than treating every empty field as an error that should be fabricated away.

## Reports

The audit produces:

- `catalogue-quality-baseline.json`: the main-branch field coverage captured before enrichment.
- `catalogue-quality-report.json`: post-enrichment totals and source-level coverage.
- `catalogue-quality-comparison.json`: before/after deltas, both overall and per source.
- `catalogue-icon-validation.json`: stable, source-stratified checks of a sample of HTTPS icon URLs.
- `google-play-icon-enrichment.json`: outcomes and provenance for Google Play icon lookups.
- Per-source coverage distinguishes missing values from malformed URLs and generic placeholder strings.

The icon URL check is a stratified sample, not a claim that every URL was fetched. CDN blocks and timeouts are reported distinctly from invalid or missing metadata.

## Page quality

Application pages must use the same substantive-description rule in server rendering and browser hydration. Pages with insufficient source description remain `noindex,follow`; they must not acquire an indexable SoftwareApplication schema from generic fallback copy. Store pages are checked for server-rendered listings, unique canonical URLs, real source icons when available, and neutral monogram fallbacks when artwork is absent.

## Release gate

This branch is an audit/fix branch. It is not merged into `main`, and no production deployment is initiated by this work. Merge and release remain subject to the project owner's explicit approval after the reports and test results have been reviewed.
