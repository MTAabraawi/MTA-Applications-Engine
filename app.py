import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, create_engine, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

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
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    developer: Mapped[Optional[str]] = mapped_column(String(500))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(160))
    license: Mapped[Optional[str]] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    app_url: Mapped[Optional[str]] = mapped_column(Text)
    icon_url: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)
app = FastAPI(title="MTA Applications Engine", version="0.1.0")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/health")
def health():
    return {"status": "ok", "service": "MTA Applications Engine", "version": "0.1.0"}

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
        "icon_url": x.icon_url, "first_seen_at": x.first_seen_at, "updated_at": x.updated_at
    } for x in rows]

@app.post("/api/v1/ingest/fdroid")
async def ingest_fdroid(
    x_admin_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    if not ADMIN_API_TOKEN or not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_API_TOKEN):
        raise HTTPException(status_code=403, detail="Ingestion is disabled or unauthorized")
    url = "https://f-droid.org/repo/index-v2.json"
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "MTA-Applications-Engine/0.1"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"F-Droid catalogue unavailable: {exc}") from exc
    apps = payload.get("apps", {})
    if not isinstance(apps, dict):
        raise HTTPException(status_code=502, detail="Unexpected F-Droid index structure")
    inserted = updated = skipped = 0
    for package_id, item in apps.items():
        if not isinstance(item, dict):
            skipped += 1
            continue
        localized = item.get("localized") or {}
        english = localized.get("en-US") or localized.get("en") or {}
        name = item.get("name") or english.get("name") or package_id
        values = {
            "platform": "android",
            "name": str(name),
            "developer": item.get("authorName") or item.get("author"),
            "summary": item.get("summary") or english.get("summary"),
            "description": item.get("description") or english.get("description"),
            "category": ", ".join(item.get("categories", [])) or None,
            "license": item.get("license"),
            "source_url": f"https://f-droid.org/packages/{package_id}/",
            "app_url": item.get("webSite") or item.get("sourceCode"),
            "icon_url": None,
            "metadata_json": {
                "package_name": package_id,
                "source_code": item.get("sourceCode"),
                "issue_tracker": item.get("issueTracker"),
                "website": item.get("webSite"),
                "added": item.get("added"),
                "last_updated": item.get("lastUpdated"),
            },
        }
        row = db.execute(select(AppRecord).where(AppRecord.source_key == "fdroid", AppRecord.external_id == package_id)).scalar_one_or_none()
        if row is None:
            row = AppRecord(source_key="fdroid", external_id=package_id, **values)
            db.add(row)
            inserted += 1
        else:
            changed = False
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                row.updated_at = datetime.now(timezone.utc)
                updated += 1
            else:
                skipped += 1
    db.commit()
    return {
        "source": "F-Droid", "discovered": len(apps), "inserted": inserted,
        "updated": updated, "unchanged_or_skipped": skipped,
        "note": "Metadata only. No APK files were downloaded, mirrored or hosted."
    }
