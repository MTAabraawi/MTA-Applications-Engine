import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, URL, case, create_engine, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from catalog_sources import fetch_all_catalogs, fetch_fdroid
from expanded_catalog_sources import fetch_expanded_catalogs

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mta_engine.db")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")
GITHUB_STORAGE = os.getenv("MTA_STORAGE_BACKEND", "sql").strip().casefold() == "github"
GITHUB_CATALOGUE_DIR = os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data")
if GITHUB_STORAGE:
    engine = None
    SessionLocal = None
else:
    connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class AppRecord(Base):
    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("source_key", "external_id", name="uq_app_source_external"),
        Index("ix_apps_platform_name", "platform", "name"),
        Index("ix_apps_source_platform", "source_key", "platform"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    developer: Mapped[Optional[str]] = mapped_column(String(500))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(Text)
    license: Mapped[Optional[str]] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    app_url: Mapped[Optional[str]] = mapped_column(Text)
    icon_url: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


if not GITHUB_STORAGE and os.getenv("SKIP_SCHEMA_CREATE") != "1":
    Base.metadata.create_all(engine)

_worker_sessionmakers = {}

app = FastAPI(title="MTA Applications Engine", version="0.2.0")
allowed_origins = [x.strip() for x in os.getenv(
    "CORS_ORIGINS", "https://www.mtahqq.dpdns.org,https://mtahqq.dpdns.org"
).split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


def _binding_value(env, name: str):
    if env is None:
        return None
    if isinstance(env, dict):
        return env.get(name)
    return getattr(env, name, None)


def get_db(request: Request):
    if GITHUB_STORAGE:
        yield None
        return
    # Cloudflare Workers supplies Hyperdrive through the ASGI request scope.
    # Local/Docker/GitHub Actions continue using DATABASE_URL and SessionLocal.
    env = request.scope.get("env")
    hyperdrive = _binding_value(env, "HYPERDRIVE")
    if hyperdrive is not None:
        host = hyperdrive.host
        port = int(hyperdrive.port)
        username = hyperdrive.user
        password = hyperdrive.password
        database = hyperdrive.database
        cache_key = (host, port, username, database, hash(password))
        factory = _worker_sessionmakers.get(cache_key)
        if factory is None:
            worker_url = URL.create(
                "postgresql+psycopg",
                username=username,
                password=password,
                host=host,
                port=port,
                database=database,
                query={"sslmode": "disable"},
            )
            worker_engine = create_engine(
                worker_url,
                poolclass=NullPool,
            )
            factory = sessionmaker(bind=worker_engine, autoflush=False, autocommit=False)
            _worker_sessionmakers[cache_key] = factory
        db = factory()
    else:
        db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_token: Optional[str], request: Request) -> None:
    env_token = _binding_value(request.scope.get("env"), "ADMIN_API_TOKEN")
    expected_token = env_token or ADMIN_API_TOKEN
    if not expected_token or not x_admin_token or not secrets.compare_digest(x_admin_token, expected_token):
        raise HTTPException(status_code=403, detail="Ingestion is disabled or unauthorized")


QUALITY_FIELDS = ("icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url")


def _has_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _merge_metadata(previous, incoming):
    """Merge metadata recursively so enrichment survives sparse catalogue refreshes."""
    old = previous if isinstance(previous, dict) else {}
    new = incoming if isinstance(incoming, dict) else {}
    merged = dict(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata(merged[key], value)
        elif isinstance(value, list) and not value and key in merged and merged[key]:
            continue
        elif _has_value(value) or key not in merged:
            merged[key] = value
    return merged


def _quality_fields(total: int, counts: dict) -> dict:
    fields = {}
    for field in QUALITY_FIELDS:
        present = int(counts.get(field, 0) or 0)
        fields[field] = {
            "present": present,
            "missing": max(total - present, 0),
            "percent": round((present * 100.0 / total), 2) if total else 0.0,
        }
    return fields


def upsert_records(db: Session, records: list[dict]) -> dict:
    """Bulk upsert catalogue records without one SELECT round-trip per app."""
    inserted = updated = unchanged = skipped = 0
    field_map = {
        "platform": "platform",
        "name": "name",
        "developer": "developer",
        "summary": "summary",
        "description": "description",
        "category": "category",
        "license": "license",
        "source_url": "source_url",
        "app_url": "app_url",
        "icon_url": "icon_url",
        "metadata_json": "metadata",
    }

    # Normalize and deduplicate before batching. Some upstream search terms can
    # surface the same package more than once; PostgreSQL rejects duplicate keys
    # in a single ON CONFLICT statement.
    normalized = {}
    for item in records:
        source_key = str(item.get("source_key") or "").strip()
        external_id = str(item.get("external_id") or "").strip()
        values = {key: item.get(key) for key in field_map}
        values["metadata_json"] = values["metadata_json"] or {}
        if not source_key or not external_id or not values.get("name") or not values.get("source_url"):
            skipped += 1
            continue
        normalized[(source_key, external_id)] = values

    dialect = db.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        # Keep compatibility with any other SQLAlchemy dialect.
        for (source_key, external_id), values in normalized.items():
            row = db.execute(select(AppRecord).where(
                AppRecord.source_key == source_key,
                AppRecord.external_id == external_id,
            )).scalar_one_or_none()
            if row is None:
                db.add(AppRecord(source_key=source_key, external_id=external_id, **values))
                inserted += 1
                continue
            changed = False
            for key, value in values.items():
                if key == "metadata_json":
                    value = _merge_metadata(row.metadata_json, value)
                elif not _has_value(value):
                    value = getattr(row, key)
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                row.updated_at = datetime.now(timezone.utc)
                updated += 1
            else:
                unchanged += 1
        db.commit()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "skipped": skipped}

    table = AppRecord.__table__
    db_columns = list(field_map.values())
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    # Stay below SQLite's conservative bind-parameter limit; PostgreSQL can
    # safely handle larger batches while remaining far below its 65,535 limit.
    batch_size = 500 if dialect == "postgresql" else 50
    items = list(normalized.items())
    key_columns = [table.c.source_key, table.c.external_id]

    for offset in range(0, len(items), batch_size):
        chunk = items[offset:offset + batch_size]
        keys = [key for key, _ in chunk]
        existing_rows = db.execute(
            select(table.c.source_key, table.c.external_id,
                   *(table.c[column] for column in db_columns))
            .where(tuple_(table.c.source_key, table.c.external_id).in_(keys))
        ).all()
        existing = {
            (row[0], row[1]): dict(zip(db_columns, row[2:]))
            for row in existing_rows
        }

        new_payloads = []
        changed_payloads = []
        for (source_key, external_id), values in chunk:
            payload = {
                "source_key": source_key,
                "external_id": external_id,
                **{column: values[attr] for attr, column in field_map.items()},
            }
            previous = existing.get((source_key, external_id))
            if previous is None:
                new_payloads.append(payload)
            else:
                # Sparse feeds must never erase richer values acquired by enrichment.
                for column in db_columns:
                    incoming = payload[column]
                    if column == "metadata":
                        payload[column] = _merge_metadata(previous.get(column), incoming)
                    elif not _has_value(incoming):
                        payload[column] = previous.get(column)
                if any(previous[column] != payload[column] for column in db_columns):
                    changed_payloads.append(payload)
                else:
                    unchanged += 1

        if new_payloads:
            statement = insert_fn(table).values(new_payloads)
            db.execute(statement.on_conflict_do_nothing(index_elements=key_columns))
            inserted += len(new_payloads)

        if changed_payloads:
            statement = insert_fn(table).values(changed_payloads)
            update_values = {column: statement.excluded[column] for column in db_columns}
            update_values["updated_at"] = datetime.now(timezone.utc)
            db.execute(statement.on_conflict_do_update(
                index_elements=key_columns,
                set_=update_values,
            ))
            updated += len(changed_payloads)

    db.commit()
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "skipped": skipped}


@app.get("/health")
def health():
    return {
        "status": "ok", "service": "MTA Applications Engine", "version": "0.3.0",
        "storage": "github-json-shards" if GITHUB_STORAGE else "sql",
    }


def _github_store():
    from github_catalog_store import GitHubCatalogueStore
    return GitHubCatalogueStore(GITHUB_CATALOGUE_DIR)


def _github_api_record(row: dict) -> dict:
    return {
        "id": f"{row['source_key']}:{row['external_id']}",
        "source": row["source_key"], "external_id": row["external_id"],
        "platform": row.get("platform"), "name": row.get("name"),
        "developer": row.get("developer"), "summary": row.get("summary"),
        "description": row.get("description"), "category": row.get("category"),
        "license": row.get("license"), "source_url": row.get("source_url"),
        "app_url": row.get("app_url"), "icon_url": row.get("icon_url"),
        "metadata_json": row.get("metadata_json") or {},
        "first_seen_at": row.get("first_seen_at"), "updated_at": row.get("updated_at"),
    }


@app.get("/api/v1/apps")
def list_apps(
    q: Optional[str] = Query(default=None, min_length=1, max_length=120),
    platform: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if GITHUB_STORAGE:
        rows = _github_store().search(query=q, platform=platform, source=source, limit=limit, offset=offset)
        return [_github_api_record(row) for row in rows]

    stmt = select(AppRecord).order_by(AppRecord.name).limit(limit).offset(offset)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(AppRecord.name.ilike(term), AppRecord.developer.ilike(term), AppRecord.summary.ilike(term)))
    if platform:
        stmt = stmt.where(AppRecord.platform == platform.casefold())
    if source:
        stmt = stmt.where(AppRecord.source_key == source.casefold())
    rows = db.execute(stmt).scalars().all()
    return [{
        "id": x.id, "source": x.source_key, "external_id": x.external_id,
        "platform": x.platform, "name": x.name, "developer": x.developer,
        "summary": x.summary, "description": x.description, "category": x.category,
        "license": x.license, "source_url": x.source_url, "app_url": x.app_url,
        "icon_url": x.icon_url, "metadata_json": x.metadata_json, "first_seen_at": x.first_seen_at, "updated_at": x.updated_at
    } for x in rows]


@app.get("/api/v1/catalog-stats")
def catalog_stats(db: Session = Depends(get_db)):
    if GITHUB_STORAGE:
        path = _github_store().indexes_dir / "stats.json"
        if not path.exists():
            raise HTTPException(status_code=503, detail="GitHub catalogue indexes are not built yet")
        return _github_store()._read_json(path, {})
    by_platform = db.execute(
        select(AppRecord.platform, func.count()).group_by(AppRecord.platform).order_by(AppRecord.platform)
    ).all()

    field_expressions = []
    for field in QUALITY_FIELDS:
        column = getattr(AppRecord, field)
        normalized = func.lower(func.trim(func.coalesce(column, "")))
        condition = normalized.like("https://%") if field.endswith("_url") else func.length(normalized) > 0
        field_expressions.append(func.sum(case((condition, 1), else_=0)).label(field))

    rows = db.execute(
        select(AppRecord.source_key, func.count().label("total"), *field_expressions)
        .group_by(AppRecord.source_key).order_by(AppRecord.source_key)
    ).all()

    source_quality = {}
    source_counts = {}
    global_counts = {field: 0 for field in QUALITY_FIELDS}
    total = 0
    for row in rows:
        source = row[0]
        source_total = int(row[1] or 0)
        counts = {field: int(row[index + 2] or 0) for index, field in enumerate(QUALITY_FIELDS)}
        total += source_total
        for field, count in counts.items():
            global_counts[field] += count
        source_counts[source] = source_total
        source_quality[source] = {
            "total": source_total,
            "fields": _quality_fields(source_total, counts),
        }

    return {
        "total": total,
        "with_icon": global_counts["icon_url"],
        "without_icon": max(total - global_counts["icon_url"], 0),
        "platforms": {key: count for key, count in by_platform},
        "sources": source_counts,
        "quality": {
            "fields": _quality_fields(total, global_counts),
            "sources": source_quality,
        },
        "metadata_only": True,
    }


@app.post("/api/v1/ingest/catalogs")
async def ingest_catalogs(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    if GITHUB_STORAGE:
        raise HTTPException(status_code=405, detail="GitHub-backed catalogue is read-only through the API. Run the GitHub Actions catalogue workflow to update it.")
    require_admin(x_admin_token, request)
    catalog_results = await fetch_all_catalogs()
    catalog_results.update(await fetch_expanded_catalogs())
    summary = {}
    for source_key, records in catalog_results.items():
        # Each source is committed independently so a later failure cannot erase earlier work.
        try:
            summary[source_key] = upsert_records(db, records)
            summary[source_key]["fetched"] = len(records)
        except Exception:
            db.rollback()
            summary[source_key] = {"fetched": len(records), "error": "Database write failed"}
    return {
        "status": "completed",
        "sources": summary,
        "note": "Metadata only. No installer files were downloaded, mirrored, or hosted.",
    }


@app.post("/api/v1/ingest/fdroid")
async def ingest_fdroid(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    if GITHUB_STORAGE:
        raise HTTPException(status_code=405, detail="GitHub-backed catalogue is read-only through the API. Run the GitHub Actions catalogue workflow to update it.")
    require_admin(x_admin_token, request)
    try:
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "MTA-Applications-Engine/0.2"},
        ) as client:
            records = await fetch_fdroid(client)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"F-Droid catalogue unavailable: {exc}") from exc
    if not records:
        raise HTTPException(status_code=502, detail="F-Droid returned no usable application metadata")
    summary = upsert_records(db, records)
    return {
        "source": "F-Droid",
        "discovered": len(records),
        **summary,
        "note": "Metadata only. No APK files were downloaded, mirrored, or hosted.",
    }
