"""Apply SQL Server migrations in order.

This is a small, opinionated runner. It does three things and nothing
else:

1. Finds every ``*.sql`` file in ``db/migrations/`` and orders them by
   the leading three-digit prefix (``001_baseline.sql`` →
   ``002_tenant_id.sql`` → ...).
2. Splits each file on standalone ``GO`` separators (SQL Server's batch
   terminator; pyodbc can't execute multiple batches in one ``execute()``
   call).
3. Substitutes ``$(NAME)`` placeholders with values supplied via
   ``--var NAME=value`` (the same syntax as ``sqlcmd -v``).

What it deliberately does **not** do:

- It does not track which migrations have already been applied. Every
  migration in this repo is written to be idempotent (``IF NOT EXISTS``
  / ``IF OBJECT_ID(...) IS NOT NULL DROP``), so re-running is a no-op.
  When we need a real migration ledger we'll add a ``schema_versions``
  table; until then, idempotency keeps the surface area small.
- It does not wrap multiple files in a single transaction. SQL Server
  blocks most DDL inside an explicit transaction we'd start ourselves,
  and each migration is small enough that partial failure is a manual
  ops decision anyway.

Usage::

    DB_HOST=sql DB_USER=parts_app DB_PASSWORD=xxx \\
        python -m db.apply --database parts_db --var LEGACY_TENANT=acme

If the script can't import ``pyodbc`` (the project's ODBC driver) it
prints what it would have executed and exits 0 — useful for CI dry-runs
and for the test suite, which mocks the connection.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_GO_RE = re.compile(r"(?im)^\s*GO\s*;?\s*$")
_SETVAR_RE = re.compile(r"^:setvar\s+(\w+)\s+\"([^\"]*)\"\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\$\((\w+)\)")


class MigrationError(RuntimeError):
    pass


def discover() -> list[Path]:
    """Return every ``NNN_*.sql`` file under ``migrations/`` in order."""
    files = sorted(
        p for p in MIGRATIONS_DIR.glob("*.sql")
        if re.match(r"^\d{3}_", p.name)
    )
    return files


def split_batches(text: str) -> list[str]:
    """Split a SQL Server script on standalone ``GO`` lines.

    Lines containing only ``GO`` (possibly with surrounding whitespace
    and an optional trailing ``;``) are batch separators. Anything else
    is part of the current batch. Empty batches are dropped.
    """
    parts = _GO_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def substitute(text: str, variables: dict[str, str]) -> str:
    """Replace ``:setvar`` declarations and ``$(NAME)`` placeholders.

    ``:setvar X "default"`` lines are honoured as defaults — the caller
    can override them by passing ``--var X=other``. If a placeholder
    is referenced but no value (default or override) is supplied we
    raise ``MigrationError`` rather than substituting an empty string,
    because silent substitution is the kind of bug that produces
    ``DELETE WHERE tenant_id = ''`` in production.
    """
    resolved = {}
    for m in _SETVAR_RE.finditer(text):
        resolved[m.group(1)] = m.group(2)
    resolved.update(variables)

    # Strip the :setvar lines so pyodbc never sees them.
    text = _SETVAR_RE.sub("", text)

    missing: set[str] = set()

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in resolved:
            missing.add(name)
            return match.group(0)
        return resolved[name]

    text = _PLACEHOLDER_RE.sub(_replace, text)
    if missing:
        raise MigrationError(
            f"unbound variable(s) in migration: {sorted(missing)}; "
            "pass via --var NAME=value or set a :setvar default"
        )
    return text


def _connection_from_env(database: str):
    """Build a pyodbc connection from the standard env vars.

    Returns ``None`` when pyodbc isn't available — the caller falls
    back to dry-run mode.
    """
    try:
        import pyodbc  # type: ignore
    except ImportError:
        return None

    host = os.environ["DB_HOST"]
    port = os.getenv("DB_PORT", "1433")
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]
    driver = os.getenv("ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, autocommit=True)


def apply_file(path: Path, variables: dict[str, str], cursor) -> int:
    """Apply one migration file, batch by batch. Returns number of batches."""
    raw = path.read_text(encoding="utf-8")
    resolved = substitute(raw, variables)
    batches = split_batches(resolved)
    for i, batch in enumerate(batches, start=1):
        try:
            cursor.execute(batch)
        except Exception as e:
            raise MigrationError(
                f"{path.name} batch {i}/{len(batches)} failed: {e}\n"
                f"--- offending batch ---\n{batch[:500]}"
            ) from e
    return len(batches)


def run(
    files: Iterable[Path],
    variables: dict[str, str],
    cursor,
    *,
    log=print,
) -> None:
    for path in files:
        log(f"[apply] {path.name}")
        batches = apply_file(path, variables, cursor)
        log(f"  {batches} batch(es) applied")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply SQL Server migrations from db/migrations/."
    )
    parser.add_argument(
        "--database",
        default=os.getenv("DB_NAME", "parts_db"),
        help="Database name (defaults to $DB_NAME or 'parts_db').",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=value",
        help="Override a :setvar default. Repeat for multiple vars.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved SQL for each migration and exit without connecting.",
    )
    parser.add_argument(
        "--from",
        dest="from_step",
        default=None,
        help="Skip every migration whose filename sorts strictly before this value.",
    )
    args = parser.parse_args(argv)

    variables: dict[str, str] = {}
    for entry in args.var:
        if "=" not in entry:
            print(f"error: --var expects NAME=value, got {entry!r}", file=sys.stderr)
            return 2
        name, _, value = entry.partition("=")
        variables[name.strip()] = value

    files = discover()
    if not files:
        print("no migrations found under db/migrations/", file=sys.stderr)
        return 1
    if args.from_step:
        files = [p for p in files if p.name >= args.from_step]

    if args.dry_run:
        for path in files:
            print(f"=== {path.name} ===")
            try:
                resolved = substitute(path.read_text(encoding="utf-8"), variables)
            except MigrationError as e:
                print(f"error resolving {path.name}: {e}", file=sys.stderr)
                return 2
            print(resolved)
        return 0

    conn = _connection_from_env(args.database)
    if conn is None:
        print(
            "pyodbc is not installed in this environment; rerun with --dry-run "
            "or install the project's runtime requirements.",
            file=sys.stderr,
        )
        return 2

    cursor = conn.cursor()
    try:
        run(files, variables, cursor)
    except MigrationError as e:
        print(f"migration failed: {e}", file=sys.stderr)
        return 1
    finally:
        cursor.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
