"""Git-backed JSON catalogue store.

The repository checkout is the durable store: catalogue records are partitioned
into deterministic JSON shards, with a manifest and compact lookup indexes.
Writes are local and are intended to be committed in one Git commit per batch
by the scheduled GitHub Actions workflow. This module deliberately has no
database/network dependency.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
DEFAULT_SHARD_COUNT = 256
FIELDS = (
    "source_key", "external_id", "platform", "name", "developer", "summary",
    "description", "category", "license", "source_url", "app_url", "icon_url",
    "metadata_json", "first_seen_at", "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), default=_json_default)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _has_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _merge_metadata(old, new):
    merged = dict(old) if isinstance(old, dict) else {}
    for key, value in (new if isinstance(new, dict) else {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata(merged[key], value)
        elif isinstance(value, list) and not value and merged.get(key):
            continue
        elif _has_value(value) or key not in merged:
            merged[key] = value
    return merged


def _quality_fields(total: int, counts: dict) -> dict:
    result = {}
    for field, present in counts.items():
        result[field] = {
            "present": int(present),
            "missing": max(total - int(present), 0),
            "percent": round((int(present) * 100.0 / total), 2) if total else 0.0,
        }
    return result


def _quality_snapshot(records: Iterable[dict]) -> dict:
    by_source: dict[str, dict] = {}
    fields = ("icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url")
    for record in records:
        source = str(record.get("source_key") or "unknown")
        item = by_source.setdefault(source, {"total": 0, "counts": {field: 0 for field in fields}})
        item["total"] += 1
        for field in fields:
            value = record.get(field)
            if _has_value(value) and (not field.endswith("_url") or str(value).lower().startswith("https://")):
                item["counts"][field] += 1
    return {
        source: {"total": item["total"], "fields": _quality_fields(item["total"], item["counts"])}
        for source, item in sorted(by_source.items())
    }


class GitHubCatalogueStore:
    """Deterministic sharded JSON store designed to live in a Git repository."""

    def __init__(self, root: str | Path = "catalogue_data", shard_count: int | None = None):
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        if shard_count is None:
            try:
                with self.manifest_path.open("r", encoding="utf-8") as stream:
                    shard_count = int(json.load(stream).get("shard_count", DEFAULT_SHARD_COUNT))
            except (OSError, ValueError, TypeError):
                shard_count = DEFAULT_SHARD_COUNT
        if shard_count < 1 or shard_count > 4096:
            raise ValueError("shard_count must be between 1 and 4096")
        self.shard_count = shard_count
        self.shards_dir = self.root / "shards"
        self.indexes_dir = self.root / "indexes"

    def _key(self, record: dict) -> str:
        return f"{record['source_key']}:{record['external_id']}"

    def _shard_number(self, key: str) -> int:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.shard_count

    def _shard_path(self, number: int) -> Path:
        return self.shards_dir / f"{number:04d}.json"

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)

    def _read_shard(self, number: int) -> dict:
        payload = self._read_json(self._shard_path(number), {})
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid catalogue shard: {number:04d}")
        return payload

    def get(self, source_key: str, external_id: str):
        key = f"{source_key}:{external_id}"
        record = self._read_shard(self._shard_number(key)).get(key)
        return dict(record) if record is not None else None

    def upsert_many(self, records: Iterable[dict], rebuild_indexes: bool = True) -> dict:
        changed_shards: dict[int, dict] = {}
        inserted = updated = unchanged = skipped = 0
        now = _now()

        for incoming in records:
            source = str(incoming.get("source_key") or "").strip()
            external = str(incoming.get("external_id") or "").strip()
            if not source or not external or not _has_value(incoming.get("name")) or not _has_value(incoming.get("source_url")):
                skipped += 1
                continue
            key = f"{source}:{external}"
            number = self._shard_number(key)
            shard = changed_shards.setdefault(number, self._read_shard(number))
            old = shard.get(key)
            item = {field: incoming.get(field) for field in FIELDS if field in incoming}
            item["source_key"], item["external_id"] = source, external
            item["metadata_json"] = item.get("metadata_json") or {}
            item["first_seen_at"] = (old or {}).get("first_seen_at") or item.get("first_seen_at") or now
            item["updated_at"] = now

            if old:
                merged = dict(old)
                for field, value in item.items():
                    if field == "metadata_json":
                        merged[field] = _merge_metadata(old.get(field), value)
                    elif field in {"source_key", "external_id", "first_seen_at"}:
                        continue
                    elif _has_value(value):
                        merged[field] = value
                comparable_old = {k: v for k, v in old.items() if k != "updated_at"}
                comparable_new = {k: v for k, v in merged.items() if k != "updated_at"}
                if comparable_old == comparable_new:
                    unchanged += 1
                    continue
                merged["updated_at"] = now
                shard[key] = merged
                updated += 1
            else:
                shard[key] = item
                inserted += 1

        for number, payload in changed_shards.items():
            _atomic_json(self._shard_path(number), payload)

        if rebuild_indexes and (inserted or updated):
            self.rebuild_indexes()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "skipped": skipped}

    def iter_records(self):
        for path in sorted(self.shards_dir.glob("*.json")):
            payload = self._read_json(path, {})
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid catalogue shard: {path.name}")
            for key in sorted(payload):
                yield payload[key]

    def rebuild_indexes(self) -> dict:
        platforms: dict[str, list[str]] = {}
        sources: dict[str, list[str]] = {}
        categories: dict[str, list[str]] = {}
        search_buckets: dict[str, dict[str, list[str]]] = {}
        browse: list[tuple[str, str]] = []
        field_counts: dict[str, int] = {name: 0 for name in (
            "icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url"
        )}
        total = 0
        for record in self.iter_records():
            total += 1
            key = self._key(record)
            platform = record.get("platform") or "unknown"
            source = record.get("source_key") or "unknown"
            platforms.setdefault(platform, []).append(key)
            sources.setdefault(source, []).append(key)
            category = record.get("category")
            if category:
                categories.setdefault(str(category), []).append(key)
            browse.append((str(record.get("name") or "").casefold(), key))
            for field in field_counts:
                value = record.get(field)
                if _has_value(value) and (not field.endswith("_url") or str(value).lower().startswith("https://")):
                    field_counts[field] += 1
            searchable = " ".join(str(record.get(field) or "") for field in (
                "name", "developer", "summary", "description", "category"
            )).casefold()
            tokens = set(__import__("re").findall(r"[\w.+#-]{2,}", searchable))
            for token in tokens:
                for size in range(2, min(len(token), 12) + 1):
                    prefix = token[:size]
                    bucket = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:2]
                    search_buckets.setdefault(bucket, {}).setdefault(prefix, []).append(key)
                if len(token) > 12:
                    bucket = hashlib.sha256(token.encode("utf-8")).hexdigest()[:2]
                    search_buckets.setdefault(bucket, {}).setdefault(token, []).append(key)

        browse.sort(key=lambda item: (item[0], item[1]))
        for name, mapping in (("platform", platforms), ("source", sources), ("category", categories)):
            for values in mapping.values():
                values.sort()
            _atomic_json(self.indexes_dir / f"{name}.json", mapping)
        search_dir = self.indexes_dir / "search"
        search_dir.mkdir(parents=True, exist_ok=True)
        for bucket, mapping in search_buckets.items():
            for values in mapping.values():
                values.sort()
            _atomic_json(search_dir / f"{bucket}.json", mapping)
        _atomic_json(self.indexes_dir / "browse.json", [key for _, key in browse])
        _atomic_json(self.indexes_dir / "stats.json", {
            "total": total,
            "with_icon": field_counts["icon_url"],
            "without_icon": max(total - field_counts["icon_url"], 0),
            "platforms": {key: len(value) for key, value in platforms.items()},
            "sources": {key: len(value) for key, value in sources.items()},
            "quality": {
                "fields": _quality_fields(total, field_counts),
                "sources": _quality_snapshot(self.iter_records()),
            },
            "metadata_only": True,
            "storage": "github-json-shards",
        })

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "storage": "git-json-shards",
            "shard_count": self.shard_count,
            "record_count": total,
            "updated_at": _now(),
            "indexes": ["platform.json", "source.json", "category.json", "search/*.json", "browse.json", "stats.json"],
        }
        _atomic_json(self.manifest_path, manifest)
        return manifest

    def stats(self) -> dict:
        manifest = self._read_json(self.manifest_path, None)
        if manifest is None:
            manifest = self.rebuild_indexes()
        platforms = self._read_json(self.indexes_dir / "platform.json", {})
        sources = self._read_json(self.indexes_dir / "source.json", {})
        return {
            "total": int(manifest.get("record_count", 0)),
            "platforms": {key: len(value) for key, value in platforms.items()},
            "sources": {key: len(value) for key, value in sources.items()},
            "metadata_only": True,
            "storage": "git-json-shards",
        }

    def _get_key(self, key: str):
        number = self._shard_number(key)
        record = self._read_shard(number).get(key)
        return dict(record) if record is not None else None

    def _records_for_keys(self, keys: Iterable[str]) -> dict[str, dict]:
        grouped: dict[int, list[str]] = {}
        for key in keys:
            grouped.setdefault(self._shard_number(key), []).append(key)
        found = {}
        for number, shard_keys in grouped.items():
            shard = self._read_shard(number)
            for key in shard_keys:
                if key in shard:
                    found[key] = dict(shard[key])
        return found

    def search(self, query: str | None = None, platform: str | None = None,
               source: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        if limit < 1 or limit > 200 or offset < 0:
            raise ValueError("invalid pagination")
        import re

        term = (query or "").casefold().strip()
        platform = platform.casefold() if platform else None
        source = source.casefold() if source else None
        platform_index = self._read_json(self.indexes_dir / "platform.json", None)
        source_index = self._read_json(self.indexes_dir / "source.json", None)
        browse = self._read_json(self.indexes_dir / "browse.json", None)

        candidates = None
        if platform and isinstance(platform_index, dict):
            candidates = set(platform_index.get(platform, []))
        if source and isinstance(source_index, dict):
            source_keys = set(source_index.get(source, []))
            candidates = source_keys if candidates is None else candidates & source_keys

        # Prefix buckets narrow ordinary text searches to matching records.
        # The final substring check preserves the API's established semantics.
        tokens = re.findall(r"[\w.+#-]{2,}", term)
        if tokens and all(len(token) >= 2 for token in tokens):
            search_dir = self.indexes_dir / "search"
            for token in tokens:
                prefix = token[:12]
                bucket = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:2]
                mapping = self._read_json(search_dir / f"{bucket}.json", {})
                token_keys = set(mapping.get(prefix, []))
                candidates = token_keys if candidates is None else candidates & token_keys

        if candidates is None:
            if isinstance(browse, list):
                ordered_keys = browse
            else:
                rows = list(self.iter_records())
                rows.sort(key=lambda row: (str(row.get("name") or "").casefold(),
                                           str(row.get("source_key") or ""),
                                           str(row.get("external_id") or "")))
                return self._filter_rows(rows, term, platform, source, limit, offset)
        elif isinstance(browse, list):
            ordered_keys = [key for key in browse if key in candidates]
        else:
            rows = [self._get_key(key) for key in candidates]
            rows = [row for row in rows if row is not None]
            rows.sort(key=lambda row: (str(row.get("name") or "").casefold(),
                                       str(row.get("source_key") or ""),
                                       str(row.get("external_id") or "")))
            return self._filter_rows(rows, term, platform, source, limit, offset)

        if candidates is not None:
            ordered_keys = [key for key in ordered_keys if key in candidates]
        loaded = self._records_for_keys(ordered_keys)
        matches = []
        for key in ordered_keys:
            item = loaded.get(key)
            if item is None:
                continue
            if term and term not in " ".join(str(item.get(field) or "") for field in (
                "name", "developer", "summary", "description", "category"
            )).casefold():
                continue
            matches.append(item)
        return matches[offset:offset + limit]

    @staticmethod
    def _filter_rows(rows, term, platform, source, limit, offset):
        matches = []
        for item in rows:
            if platform and str(item.get("platform", "")).casefold() != platform:
                continue
            if source and str(item.get("source_key", "")).casefold() != source:
                continue
            searchable = " ".join(str(item.get(field) or "") for field in (
                "name", "developer", "summary", "description", "category"
            )).casefold()
            if term and term not in searchable:
                continue
            matches.append(item)
        return matches[offset:offset + limit]
