"""Tests for tools.impact: reverse-mentions impact analysis.

Each test builds a tiny, real git repo in tmp_path (init, commit a base canon +
exposition fixture, then mutate it) and drives the module's functions directly against
that repo as `root` — same temp-git-repo style as tests/test_pr_annotate.py, since this
module reuses that one's diff machinery directly.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import impact as im
from tools import pr_annotate as pa


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "--allow-empty",
         "-q", "-m", "init")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def write(root: Path, relpath: str, body: str) -> None:
    """Write under `root/canon/relpath`."""
    p = root / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def write_exposition(root: Path, relpath: str, body: str) -> None:
    """Write under `root/expositions/relpath` (relpath includes the `<collection>/` shape)."""
    p = root / "expositions" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


BASE_THM = """
    ---
    id: thm.a
    type: statement
    name: A
    summary: The original summary.
    kind: theorem
    statement: |
      Original hypotheses and conclusion.
    status: conjectured
    ---
    A's body.
"""


def base_repo(tmp_path: Path) -> str:
    """A minimal valid canon + one exposition, committed as the repo's initial state.

    thm.a is mentioned by both a writeup (expositions/survey/001-a.md) and another
    entity's prose body (thm.c), and by nothing else (thm.b, and 002-unrelated.md,
    mention neither) — so tests can assert impact analysis reports *exactly* the
    referencing expositions/entities, not everything in the fixture.
    """
    _init_repo(tmp_path)
    write(tmp_path, "statements/a.md", BASE_THM)
    write(tmp_path, "statements/b.md", """
        ---
        id: thm.b
        type: statement
        name: B
        statement: x
        ---
        B's body — mentions nobody.
    """)
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        statement: x
        ---
        C's body references [[thm.a]] as its motivation.
    """)
    write_exposition(tmp_path, "survey/001-a.md", "A writeup about [[thm.a]].\n")
    write_exposition(tmp_path, "survey/002-unrelated.md", "A writeup about nothing in particular.\n")
    return _commit_all(tmp_path, "base")


CHANGED_THM = """
    ---
    id: thm.a
    type: statement
    name: A
    summary: The original summary.
    kind: theorem
    statement: |
      Strengthened hypotheses, same conclusion.
    status: proved
    ---
    A's body.
"""


# --- claim-bearing field change: the changed-hypotheses scenario --------------------

def test_changed_statement_flags_exactly_the_referencing_expositions_and_mentions(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    impact = im.build_impact(base, root=tmp_path)

    assert set(impact) == {"thm.a"}
    entry = impact["thm.a"]
    assert entry["expositions"] == ["expositions/survey/001-a.md"]
    assert entry["mentioned_by"] == ["thm.c"]
    assert "statement" in entry["claim_fields_changed"]
    assert "status" in entry["claim_fields_changed"]


def test_non_claim_field_change_does_not_flag_continuity_review(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", BASE_THM.replace(
        "The original summary.", "A revised, wordier summary changing nothing structural."))
    impact = im.build_impact(base, root=tmp_path)

    entry = impact["thm.a"]
    assert entry["claim_fields_changed"] == []
    # still reports who/what references it — just doesn't flag for continuity review
    assert entry["expositions"] == ["expositions/survey/001-a.md"]
    assert entry["mentioned_by"] == ["thm.c"]


# --- removed entity -------------------------------------------------------------------

def test_removed_entity_reported_with_no_crash(tmp_path):
    base = base_repo(tmp_path)
    (tmp_path / "canon" / "statements" / "a.md").unlink()
    impact = im.build_impact(base, root=tmp_path)

    assert "thm.a" in impact
    entry = impact["thm.a"]
    # the dangling references left behind are still surfaced — that's the point of an
    # impact report for a removal (everything that now points at nothing)
    assert entry["expositions"] == ["expositions/survey/001-a.md"]
    assert entry["mentioned_by"] == ["thm.c"]
    # every claim thm.a used to make is gone now
    assert "statement" in entry["claim_fields_changed"]
    assert "status" in entry["claim_fields_changed"]


# --- entity nothing references: empty impact, no crash --------------------------------

def test_changed_entity_nothing_references_is_empty_but_present(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/b.md", """
        ---
        id: thm.b
        type: statement
        name: B the Sharper
        statement: x
        ---
        B's body — mentions nobody, still.
    """)
    impact = im.build_impact(base, root=tmp_path)

    assert "thm.b" in impact
    entry = impact["thm.b"]
    assert entry["expositions"] == []
    assert entry["mentioned_by"] == []
    assert entry["claim_fields_changed"] == []


# --- JSON shape stability --------------------------------------------------------------

def test_json_shape_is_stable(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    impact = im.build_impact(base, root=tmp_path)

    assert isinstance(impact, dict)
    for eid, entry in impact.items():
        assert isinstance(eid, str)
        assert set(entry) == {"expositions", "mentioned_by", "claim_fields_changed"}
        assert isinstance(entry["expositions"], list)
        assert isinstance(entry["mentioned_by"], list)
        assert isinstance(entry["claim_fields_changed"], list)
    # top-level dict is sorted, for byte-stable --json output
    assert list(impact) == sorted(impact)


def test_no_changes_reports_empty_impact(tmp_path):
    base = base_repo(tmp_path)
    assert im.build_impact(base, root=tmp_path) == {}


# --- render_markdown ------------------------------------------------------------------

def test_render_markdown_no_changes_message():
    report = im.render_markdown("origin/main", {})
    assert "No canon changes detected" in report


def test_render_markdown_flags_continuity_review(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    impact = im.build_impact(base, root=tmp_path)
    report = im.render_markdown(base, impact)
    assert "thm.a" in report
    assert "CONTINUITY REVIEW" in report
    assert "001-a.md" in report


# --- pr_annotate integration: the "## Impact" section is wired in ---------------------

def test_pr_annotate_report_includes_impact_section(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    report = pa.annotate(base, root=tmp_path)
    assert "## PR annotation" in report
    assert "## Impact" in report
    assert "thm.a" in report
    assert "CONTINUITY REVIEW" in report


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
