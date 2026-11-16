"""Safely widen catalogue metadata columns in PostgreSQL without deleting rows."""
import os

from sqlalchemy import create_engine, inspect, text


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            print("No migration needed for this database dialect.")
            return
        columns = {column["name"]: column for column in inspect(engine).get_columns("apps")}
        with engine.begin() as connection:
            for name in ("category", "license"):
                column = columns.get(name)
                if column and getattr(column["type"], "length", None) is not None:
                    connection.execute(text(
                        f"ALTER TABLE apps ALTER COLUMN {name} TYPE TEXT USING {name}::text"
                    ))
                    print(f"Expanded apps.{name} to TEXT; existing records preserved.")
                else:
                    print(f"apps.{name} is already unbounded or unavailable; no change.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
