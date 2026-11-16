import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, URL, create_engine, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from catalog_sources import fetch_all_catalogs, fetch_fdroid
from expanded_catalog_sources import fetch_expanded_catalogs

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mta_engine.db")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")
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


if os.getenv("SKIP_SCHEMA_CREATE") != "1":
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


def upsert_records(db: Session, records: list[dict]) -> dict:
    inserted = updated = unchanged = skipped = 0
    allowed = {
        "platform", "name", "developer", "summary", "description", "category",
        "license", "source_url", "app_url", "icon_url", "metadata_json",
    }
    for item in records:
        source_key = str(item.get("source_key") or "").strip()
        external_id = str(item.get("external_id") or "").strip()
        values = {key: item.get(key) for key in allowed}
        if not source_key or not external_id or not values.get("name") or not values.get("source_url"):
            skipped += 1
            continue
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


@app.get("/health")
def health():
    return {"status": "ok", "service": "MTA Applications Engine", "version": "0.2.0"}


@app.get("/api/v1/apps")
def list_apps(
    q: Optional[str] = Query(default=None, min_length=1, max_length=120),
    platform: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
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
    total = db.scalar(select(func.count()).select_from(AppRecord)) or 0
    by_platform = db.execute(
        select(AppRecord.platform, func.count()).group_by(AppRecord.platform).order_by(AppRecord.platform)
    ).all()
    by_source = db.execute(
        select(AppRecord.source_key, func.count()).group_by(AppRecord.source_key).order_by(AppRecord.source_key)
    ).all()
    return {
        "total": total,
        "platforms": {key: count for key, count in by_platform},
        "sources": {key: count for key, count in by_source},
        "metadata_only": True,
    }


@app.post("/api/v1/ingest/catalogs")
async def ingest_catalogs(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
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
