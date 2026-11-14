"""Cloudflare Workers ASGI entrypoint for the MTA FastAPI application."""
import os

# Cloudflare Worker requests use the Hyperdrive binding for database access.
# Database schema creation is performed by the GitHub Actions sync runner.
os.environ["SKIP_SCHEMA_CREATE"] = "1"

from workers import asgi
from app import app

Default = asgi.entrypoint(app)
