"""Tests for the schema migration framework (tools.migrate) and the version gate.

The framework tests drive the machinery with a synthetic migration against a temp
canon: applying it must rewrite frontmatter and bump the recorded version, and the
validator must refuse when the recorded version and the code disagree. This fork ships
no migrations yet (SCHEMA_VERSION restarts at 1); when the first breaking change lands,
pin its transform's behavior here exactly — a migration is a historical replay.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import migrate as mig
from tools.loader import load_canon
from tools.validate import Report, check_schema_version


def write(tmp: Path, relpath: str, body: str) -> None:
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def _canon(tmp: Path) -> Path:
    write(tmp, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A
        ---
        Body stays untouched.
    """)
    return tmp / "canon"


def test_version_file_round_trips(tmp_path):
    vf = tmp_path / ".schema-version"
    assert mig.read_canon_version(vf) == 0          # missing -> 0
    mig.write_canon_version(3, vf)
    assert mig.read_canon_version(vf) == 3


def test_pending_selects_the_right_range():
    ms = [mig.Migration(2, "b", lambda m, t: m), mig.Migration(3, "c", lambda m, t: m)]
    assert [m.to_version for m in mig.pending_migrations(1, 3, ms)] == [2, 3]
    assert [m.to_version for m in mig.pending_migrations(2, 3, ms)] == [3]
    assert mig.pending_migrations(3, 3, ms) == []


def test_migrate_applies_transform_and_bumps_version(tmp_path):
    cd = _canon(tmp_path)
    vf = tmp_path / ".schema-version"
    mig.write_canon_version(1, vf)

    def add_tag(meta, type_name):
        return {**meta, "tags": sorted(set(meta.get("tags", []) + ["migrated"]))}

    m = mig.Migration(2, "add 'migrated' tag", add_tag)
    assert mig.migrate(cd, target=2, migrations=[m], version_file=vf) == 0
    assert mig.read_canon_version(vf) == 2

    canon = load_canon(cd)
    assert not canon.errors, canon.errors
    assert "migrated" in canon.entities["thm.a"].tags
    assert "Body stays untouched." in canon.bodies["thm.a"]   # prose preserved


def test_migrate_noop_when_current(tmp_path):
    cd = _canon(tmp_path)
    vf = tmp_path / ".schema-version"
    mig.write_canon_version(2, vf)
    assert mig.migrate(cd, target=2, migrations=[], version_file=vf) == 0


def test_migrate_refuses_downgrade(tmp_path):
    cd = _canon(tmp_path)
    vf = tmp_path / ".schema-version"
    mig.write_canon_version(5, vf)
    assert mig.migrate(cd, target=2, migrations=[], version_file=vf) == 1
    assert mig.read_canon_version(vf) == 5          # nothing rewritten


def test_validator_flags_version_mismatch():
    rep = Report()
    check_schema_version(rep, recorded=99)
    assert any(f.code == "schema-version" for f in rep.findings), rep.errors


def test_validator_accepts_matching_version(tmp_path):
    from schemas.version import SCHEMA_VERSION
    rep = Report()
    check_schema_version(rep, recorded=SCHEMA_VERSION)
    assert rep.ok(), rep.errors


def test_fork_starts_with_no_shipped_migrations():
    # The ancestor spine's migrations describe a schema this canon never had; the
    # fork's list starts empty and SCHEMA_VERSION at 1 (schemas/version.py).
    from schemas.version import SCHEMA_VERSION
    assert SCHEMA_VERSION == 1
    assert mig.MIGRATIONS == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
