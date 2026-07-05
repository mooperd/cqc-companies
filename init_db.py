"""Idempotent schema sync for the CQC database.

Brings an existing database up to the current ``model.py`` schema **without
dropping data**: it creates any missing tables and additively adds any missing
columns and indexes to tables that already exist. Safe to run repeatedly.

Why this exists: ``db.metadata.create_all()`` (used ad-hoc by the ingest/enrich
scripts) creates missing *tables* but never alters existing ones, so a column
added to ``model.py`` after a table was first created — e.g. the ADR 0016 PWS
additions ``Provider.linkedin_company_id``, ``Person.linkedin_url``,
``Person.acquired_at`` — silently never lands. This script closes that gap.

This is the interim step. ADR 0002 chose create_all/no-migrations; the next move
(flagged 2026-07-05) is real versioned migrations (Alembic) so schema changes are
ordered, reversible, and reviewable. Until then, run this after pulling schema
changes.

Usage:
    uv run python init_db.py              # create-or-update (non-destructive)
    uv run python init_db.py --dry-run    # print planned DDL, change nothing
    uv run python init_db.py --drop       # DANGER: drop ALL model tables, recreate
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

import model  # noqa: F401 — registers every db.Model on db.metadata
from model import db

logger = logging.getLogger("init_db")

DEFAULT_DATABASE_URL = "postgresql://darwinist:darwinist@localhost:5432/darwinist"


def _column_ddl(engine: Engine, table, column) -> tuple[str, str | None]:
    """DDL to add one column, plus an optional warning.

    Additive columns must be nullable on a populated table — a NOT NULL column
    with no server default cannot be back-filled here, so we add it nullable and
    warn rather than fail the whole sync.

    This sync only expresses a plain scalar column. It deliberately refuses any
    shape it cannot emit safely (a foreign key, a unique constraint) rather than
    silently adding the column without the constraint — that is the boundary at
    which you move to real migrations (Alembic; see ADR 0002 / the plan).
    """
    if column.foreign_keys:
        raise NotImplementedError(
            f"{table.name}.{column.name} has a foreign key — this additive sync "
            f"cannot emit REFERENCES. Add it by hand or via a migration."
        )
    if column.unique:
        raise NotImplementedError(
            f"{table.name}.{column.name} is UNIQUE — this additive sync cannot emit "
            f"the constraint. Add it by hand or via a migration."
        )
    coltype = column.type.compile(dialect=engine.dialect)
    nullable = column.nullable
    warning = None
    if not column.nullable and column.server_default is None:
        nullable = True
        warning = (
            f"{table.name}.{column.name} is NOT NULL with no server default; "
            f"adding it NULLable so the existing rows survive — set values then "
            f"tighten the constraint by hand if required."
        )
    null_sql = "" if nullable else " NOT NULL"
    ddl = (
        f'ALTER TABLE "{table.name}" '
        f'ADD COLUMN IF NOT EXISTS "{column.name}" {coltype}{null_sql}'
    )
    return ddl, warning


def plan(engine: Engine) -> list[str]:
    """Compute the ordered DDL statements needed to reach the model schema.

    Missing tables come first (whole ``CREATE TABLE`` via metadata), then missing
    columns, then missing indexes on already-existing tables.
    """
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    statements: list[str] = []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            statements.append(f"-- CREATE TABLE {table.name} (+ its columns/indexes)")
            continue

        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing_cols:
                ddl, warning = _column_ddl(engine, table, column)
                if warning:
                    logger.warning(warning)
                statements.append(ddl)

        existing_idx = {i["name"] for i in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in existing_idx:
                continue
            cols = ", ".join(f'"{c.name}"' for c in index.columns)
            unique = "UNIQUE " if index.unique else ""
            statements.append(
                f'CREATE {unique}INDEX IF NOT EXISTS "{index.name}" '
                f'ON "{table.name}" ({cols})'
            )

    return statements


def sync(engine: Engine, *, dry_run: bool) -> list[str]:
    """Create missing tables, then additively add missing columns/indexes.

    Returns the DDL applied (or, for a dry run, the DDL that *would* apply).
    """
    statements = plan(engine)

    if dry_run:
        return statements

    # create_all is idempotent (it only emits CREATE for absent tables) and brings
    # each new table fully in line with model.py — columns and indexes included. It
    # never alters an existing table, so the ALTERs computed above for tables that
    # already existed are exactly the ALTERs still needed afterwards. Reuse them
    # rather than re-planning (a second DB inspection that would re-warn identically).
    db.metadata.create_all(engine)

    alter_statements = [s for s in statements if not s.startswith("--")]
    with engine.begin() as conn:
        for ddl in alter_statements:
            logger.info("applying: %s", ddl)
            conn.execute(text(ddl))
    return statements


def drop_and_recreate(engine: Engine) -> None:
    """DANGER: drop every model-defined table, then recreate the full schema."""
    logger.warning("dropping ALL model tables, then recreating")
    db.metadata.drop_all(engine)
    db.metadata.create_all(engine)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="init_db",
        description="Idempotently create or update the CQC database schema.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="print the DDL that would run, change nothing")
    g.add_argument("--drop", action="store_true",
                   help="DANGER: drop all model tables and recreate from scratch")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    # Announce the target so a --drop can't hit the wrong DB unnoticed.
    logger.info("database: %s", engine.url.render_as_string(hide_password=True))

    if args.drop:
        drop_and_recreate(engine)
        logger.info("schema dropped and recreated.")
        return 0

    statements = sync(engine, dry_run=args.dry_run)
    if not statements:
        logger.info("schema already up to date — nothing to do.")
    elif args.dry_run:
        logger.info("planned DDL (%d statement(s)):", len(statements))
        for s in statements:
            print(s)
    else:
        logger.info("schema synced (%d change(s)).", len(statements))
    return 0


if __name__ == "__main__":
    sys.exit(main())
