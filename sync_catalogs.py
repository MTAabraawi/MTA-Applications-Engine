"""Run a resumable-by-source catalogue metadata sync against the configured database.

Each source is committed as soon as its fetch completes. Re-running the workflow is
safe because upsert_records uses the (source_key, external_id) unique key.
"""
import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from catalog_sources import fetch_all_catalogs
from expanded_catalog_sources import fetch_expanded_catalogs

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main() -> None:
    backend = os.getenv("MTA_STORAGE_BACKEND", "sql").strip().casefold()
    github_store = None
    db = None
    if backend == "github":
        from github_catalog_store import GitHubCatalogueStore
        github_store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
        logging.info("Using Git-backed JSON catalogue storage at %s", github_store.root)
    elif backend == "sql":
        if not os.getenv("DATABASE_URL"):
            raise SystemExit("DATABASE_URL must point to a persistent database (for example PostgreSQL).")
        from app import SessionLocal, engine, upsert_records
        db = SessionLocal()
    else:
        raise SystemExit("MTA_STORAGE_BACKEND must be either sql or github.")

    # Widen bounded metadata fields in place. PostgreSQL otherwise rejects long
    # category tag lists and SPDX license expressions; existing rows are preserved.
    if backend == "sql" and engine.dialect.name == "postgresql":
        columns = {column["name"]: column for column in inspect(engine).get_columns("apps")}
        with engine.begin() as connection:
            for column_name in ("category", "license"):
                column = columns.get(column_name)
                if column and getattr(column["type"], "length", None) is not None:
                    connection.execute(text(
                        f"ALTER TABLE apps ALTER COLUMN {column_name} TYPE TEXT USING {column_name}::text"
                    ))
                    logging.info("Expanded apps.%s to TEXT without changing existing rows", column_name)

    selected_sources = {
        item.strip() for item in os.getenv("SYNC_SOURCES", "").split(",") if item.strip()
    }
    catalogue_dir = Path(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    state_path = catalogue_dir / "sync_state.json"
    try:
        sync_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sync_state = {"schema_version": 1, "sources": {}}
    if not isinstance(sync_state, dict):
        sync_state = {"schema_version": 1, "sources": {}}
    sync_state.setdefault("sources", {})
    resume_hours = max(0, min(int(os.getenv("SYNC_RESUME_HOURS", "24")), 168))
    now = datetime.now(timezone.utc)
    all_base = {"fdroid", "izzyondroid", "snap-store", "flathub", "homebrew-cask",
                "apple-itunes", "apple-macos", "winget-run-community", "arch-linux",
                "debian-packages", "steam", "mta-curated-web"}
    all_expanded = {"huggingface-ai-spaces", "github-ai-applications", "firefox-addons",
                    "wordpress-plugins", "homebrew-formulae", "scoop", "alpine-linux",
                    "google-play", "apple-appbrain", "jetbrains-marketplace",
                    "gnome-extensions", "fedora-packages", "bluejetty", "openuse", "chocolatey"}
    requested = selected_sources or (all_base | all_expanded)
    def recently_completed(source):
        entry = sync_state["sources"].get(source, {})
        stamp = entry.get("completed_at") if isinstance(entry, dict) else None
        if not stamp or not resume_hours:
            return False
        try:
            previous = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            return entry.get("status") == "success" and previous >= now - __import__("datetime").timedelta(hours=resume_hours)
        except (TypeError, ValueError):
            return False
    pending_sources = {source for source in requested if not recently_completed(source)}
    summary = {}
    
    def persist_checkpoint(source, result):
        sync_state["sources"][source] = {
            "status": "success" if "error" not in result else "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "fetched": int(result.get("fetched", 0)),
            **({"error": str(result["error"])[:500]} if "error" in result else {}),
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(sync_state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        temporary.replace(state_path)
        if os.getenv("MTA_GIT_CHECKPOINT", "").casefold() not in {"1", "true", "yes"}:
            return
        try:
            subprocess.run(["git", "add", str(catalogue_dir)], check=True, capture_output=True, text=True)
            changed = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
            if changed.returncode == 0:
                return
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
            subprocess.run(["git", "commit", "-m", f"Checkpoint catalogue source: {source}"], check=True, capture_output=True, text=True)
            branch = os.getenv("GITHUB_REF_NAME", "feature/unified-catalogue-enrichment")
            subprocess.run(["git", "pull", "--rebase", "origin", branch], check=True, capture_output=True, text=True)
            subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True, capture_output=True, text=True)
            logging.info("Durable GitHub checkpoint saved for source %s", source)
        except subprocess.CalledProcessError as exc:
            logging.error("Could not persist checkpoint for %s: %s", source, (exc.stderr or str(exc))[-1000:])
            raise

    def save_source(source: str, records: list[dict], error: Exception | None = None) -> None:
        started = datetime.now(timezone.utc).isoformat()
        if error is not None:
            result = {"fetched": 0, "error": str(error)}
            logging.error("Catalogue source %s failed during fetch: %s", source, error)
        else:
            try:
                if github_store is not None:
                    result = {"fetched": len(records), **github_store.upsert_many(records, rebuild_indexes=False)}
                else:
                    result = {"fetched": len(records), **upsert_records(db, records)}
            except Exception as exc:
                if db is not None:
                    db.rollback()
                logging.exception("Could not save source %s", source)
                result = {"fetched": len(records), "error": str(exc)}
        summary[source] = result
        persist_checkpoint(source, result)
        logging.info("SOURCE_PROGRESS %s", json.dumps({
            "source": source, "completed_at": started, **result
        }, ensure_ascii=False))
        print(json.dumps({"source": source, **result}, ensure_ascii=False), flush=True)

    try:
        base_pending = pending_sources & all_base
        expanded_pending = pending_sources & all_expanded
        if base_pending:
            logging.info("Starting base catalogue sources: %s", ", ".join(sorted(base_pending)))
            await fetch_all_catalogs(on_source=save_source, source_filter=base_pending)
        if expanded_pending:
            logging.info("Starting expanded catalogue sources: %s", ", ".join(sorted(expanded_pending)))
            await fetch_expanded_catalogs(on_source=save_source, source_filter=expanded_pending)
        skipped_sources = requested - pending_sources
        for source in sorted(skipped_sources):
            summary[source] = {"fetched": 0, "skipped": "recently_completed_checkpoint"}
            logging.info("Skipping recently completed source checkpoint: %s", source)
    finally:
        if db is not None:
            db.close()

    if github_store is not None:
        manifest = github_store.rebuild_indexes()
        logging.info("Git-backed catalogue manifest: %s", json.dumps(manifest, ensure_ascii=False))

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not any("error" not in result and result.get("fetched", 0) for result in summary.values()):
        raise SystemExit("No catalogue source returned records; review source availability/logs.")


if __name__ == "__main__":
    asyncio.run(main())
