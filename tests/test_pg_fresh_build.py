"""tools/pg_fresh_build.py (23 Nov 2026): the table / column comparison between a freshly built PostgreSQL
database and a reference. The build itself is covered live by
tests/test_postgres_backend.py::test_a_database_built_from_scratch_on_postgres_applies_every_migration."""
import pytest


def test_compare_names_missing_tables_and_columns_both_ways():
    from tools.pg_fresh_build import compare
    new = {"a": ["id", "x"], "b": ["id"], "only_new": ["id"]}
    ref = {"a": ["id", "y"], "b": ["id"], "only_ref": ["id"]}
    out = compare(new, ref)
    assert out["tables_new"] == 3 and out["tables_ref"] == 3
    assert out["only_in_new"] == ["only_new"] and out["only_in_ref"] == ["only_ref"]
    assert out["column_differences"] == {"a": {"only_new": ["x"], "only_ref": ["y"]}}
    assert compare(ref, ref)["column_differences"] == {} and compare(ref, ref)["only_in_new"] == []


def test_the_tool_only_accepts_a_postgresql_url():
    from tools.pg_fresh_build import build
    with pytest.raises(SystemExit, match="postgresql"):
        build("sqlite:///tmp/x.db")
