# Global AI application discovery

MTA's global AI catalogue is a multi-source discovery layer, not a claim that one public directory contains every AI product in the world.

## Active automated sources

- **Hugging Face Spaces** — public, hosted AI web applications discovered through Hugging Face's documented semantic Spaces search. Each Space is stored as a web application with its publisher, SDK, tags, runtime stage, and public listing URL.
- **GitHub AI applications** — public repositories discovered through GitHub repository search using multilingual queries. These records represent open-source application projects; a repository is not labelled as a verified installer or released desktop binary unless a separate release source confirms it.

Both sources are included in `fetch_expanded_catalogs` and the GitHub-native JSON catalogue sync. The workflow uses the repository's `GITHUB_TOKEN` for authenticated GitHub search and respects source-level failures without blocking unrelated catalogue sources.

## Regional coverage strategy

Search queries include English, Simplified Chinese, Russian, Japanese, Korean, Vietnamese, Hindi/Indian English, Spanish, French, German, and Portuguese terms. Search language is only a discovery technique: **it is not proof of the publisher's country**.

The source registry also tracks regional ecosystems that still require a supported public catalogue integration:

- China: ModelScope, OpenXLab, Baidu AI, Tencent AI, Huawei AppGallery, Xiaomi GetApps.
- Russia: Yandex AI and Sber AI.
- Other regions: global and country-specific Android, desktop, Linux, browser, and web directories already present in `store_sources.json`.

Sources marked `external_only` are discovery targets, not live ingestion connectors. They must not be represented as fully indexed until a documented, permitted API or publisher-provided feed is implemented and tested.

## Data integrity

- Keep the official source listing URL and, when provided, a separate application/homepage URL.
- Preserve source identifiers, tags, SDK, publisher, release/project metadata, and discovery query.
- Do not infer origin country from language, username, domain suffix alone, or search term. Leave `origin_country` null until the publisher or a reliable source confirms it.
- Do not classify model-only repositories as end-user applications without evidence of a runnable interface or application project.
- Do not invent icons, descriptions, ratings, reviews, download counts, or installer links.
- Deduplicate by source plus stable source identifier. Cross-source identity matching is a separate reconciliation task and must preserve provenance.
- Respect each source's rate limits and terms. A source failure must be logged and isolated rather than silently treated as a complete catalogue.

## Current limits

Public search APIs are discovery feeds, not exhaustive global registries. GitHub search has result/rate limits; Hugging Face Spaces search is relevance-based and does not enumerate every Space. Regional stores without supported public APIs remain unindexed until an approved connector or data feed is available. Coverage reports must show records by source, query language, platform, and verified publisher country where known.
