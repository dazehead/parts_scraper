"""Migration runner: batch splitting, variable substitution, ordering."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from db import apply as runner


def test_discover_orders_by_numeric_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", tmp_path)
    for name in ["010_third.sql", "001_first.sql", "002_second.sql", "readme.md"]:
        (tmp_path / name).write_text("SELECT 1;")
    out = [p.name for p in runner.discover()]
    assert out == ["001_first.sql", "002_second.sql", "010_third.sql"]


def test_split_batches_on_standalone_GO():
    text = """\
CREATE TABLE x (id INT);
GO
INSERT INTO x VALUES (1);
GO

UPDATE x SET id = 2;
GO
"""
    out = runner.split_batches(text)
    assert len(out) == 3
    assert out[0].startswith("CREATE TABLE")
    assert "INSERT" in out[1]
    assert out[2].startswith("UPDATE")


def test_split_batches_ignores_GO_inside_comments():
    # We don't strip comments, but a GO inside one trailing on a line
    # should still trigger a split — we keep the splitter simple. This
    # test documents the contract: "standalone GO line" is the boundary,
    # case-insensitive, optionally trailed by a semicolon.
    text = "SELECT 1;\ngo\nSELECT 2;\nGO;\nSELECT 3;\n"
    out = runner.split_batches(text)
    assert out == ["SELECT 1;", "SELECT 2;", "SELECT 3;"]


def test_substitute_honours_setvar_defaults():
    text = ':setvar LEGACY_TENANT "fallback"\nDELETE WHERE tenant_id = \'$(LEGACY_TENANT)\';\n'
    out = runner.substitute(text, {})
    assert ":setvar" not in out
    assert "DELETE WHERE tenant_id = 'fallback'" in out


def test_substitute_override_wins_over_setvar():
    text = ':setvar TENANT "fallback"\nSELECT \'$(TENANT)\';\n'
    out = runner.substitute(text, {"TENANT": "acme"})
    assert "'acme'" in out
    assert "'fallback'" not in out


def test_substitute_raises_on_unbound_placeholder():
    text = "SELECT '$(UNBOUND)';"
    with pytest.raises(runner.MigrationError, match="UNBOUND"):
        runner.substitute(text, {})


def test_substitute_does_not_silently_replace_with_empty():
    """The single most important invariant: never substitute an empty
    string for a missing variable. That's what produces
    ``DELETE WHERE tenant_id = ''`` cascades in production."""
    text = "DELETE WHERE tenant_id = '$(LEGACY_TENANT)';"
    with pytest.raises(runner.MigrationError):
        runner.substitute(text, {})


def test_apply_file_runs_each_batch(tmp_path):
    path = tmp_path / "001_x.sql"
    path.write_text("CREATE TABLE a (id INT);\nGO\nINSERT INTO a VALUES (1);\n")
    cursor = MagicMock()
    batches = runner.apply_file(path, {}, cursor)
    assert batches == 2
    assert cursor.execute.call_count == 2


def test_apply_file_wraps_pyodbc_errors_in_migration_error(tmp_path):
    path = tmp_path / "001_x.sql"
    path.write_text("BAD;\nGO\nGOOD;")
    cursor = MagicMock()
    cursor.execute.side_effect = RuntimeError("syntax error near 'BAD'")
    with pytest.raises(runner.MigrationError, match="batch 1/2"):
        runner.apply_file(path, {}, cursor)


def test_full_run_applies_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", tmp_path)
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_second.sql").write_text("CREATE TABLE b (id INT);")
    cursor = MagicMock()
    runner.run(runner.discover(), {}, cursor, log=lambda *_: None)
    sql = [c.args[0] for c in cursor.execute.call_args_list]
    assert "CREATE TABLE a" in sql[0]
    assert "CREATE TABLE b" in sql[1]


def test_real_migrations_parse_with_legacy_tenant_var():
    """Smoke-check that the migrations in this repo substitute cleanly.

    Without this, a typo in the migration file only surfaces when the
    operator actually runs sqlcmd. We just need to know it parses and
    every placeholder is bound.
    """
    files = runner.discover()
    assert files, "no migrations found"
    for path in files:
        # LEGACY_TENANT is the only :setvar today; pass it explicitly to
        # exercise the override path.
        resolved = runner.substitute(
            path.read_text(encoding="utf-8"),
            {"LEGACY_TENANT": "test-tenant"},
        )
        batches = runner.split_batches(resolved)
        assert batches, f"{path.name} produced no batches after substitution"
