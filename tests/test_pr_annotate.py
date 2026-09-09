"""Tests for tools.pr_annotate: diff -> touched ids -> filtered validator findings.

Each test builds a tiny, real git repo in tmp_path (init, commit a base canon, then mutate
it) and drives the module's functions directly against that repo as `root` — mirroring how
CI actually invokes it (a base ref plus a working tree containing the PR's changes), per
the style of tests/test_lifecycle.py.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import pr_annotate as pa
from tools.loader import load_canon


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
    p = root / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def stage(root: Path) -> None:
    """`git add -A`, no commit. A brand-new file is invisible to `git diff <ref>` until it
    is at least staged (plain `git diff <ref>` diffs the ref against the working tree, but
    "working tree" for an untracked path means "doesn't exist" as far as diff is concerned —
    see tools/pr_annotate's module docstring). In real CI the PR branch's new files are
    already committed, so this only matters for the test harness."""
    _git(root, "add", "-A")


def base_repo(tmp_path: Path) -> str:
    """A minimal valid canon, committed as the repo's initial state. Returns its ref."""
    _init_repo(tmp_path)
    write(tmp_path, "sources/paper.md", "---\nid: src.paper\ntype: source\nname: P\n---\nbody\n")
    write(tmp_path, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A
        statement: x
        stated_in: src.paper
        ---
        A's body.
    """)
    return _commit_all(tmp_path, "base")


# --- git_diff_name_status / summarize_diff: new / changed / removed -----------------

def test_new_entity_detected_as_new(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        statement: x
        stated_in: src.paper
        ---
        C's body.
    """)
    stage(tmp_path)
    summary = pa.summarize_diff(base, root=tmp_path)
    assert summary.new_ids == ["thm.c"]
    assert summary.changed_ids == []
    assert summary.removed == []


def test_modified_entity_detected_as_changed(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A the Sharper
        statement: x
        stated_in: src.paper
        ---
        A's body, revised.
    """)
    summary = pa.summarize_diff(base, root=tmp_path)
    assert summary.new_ids == []
    assert summary.changed_ids == ["thm.a"]


def test_deleted_file_reports_id_from_base_ref(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/lonely.md", """
        ---
        id: thm.lonely
        type: statement
        name: Lonely
        ---
        unreferenced, safe to delete.
    """)
    base2 = _commit_all(tmp_path, "add lonely")
    (tmp_path / "canon" / "statements" / "lonely.md").unlink()

    summary = pa.summarize_diff(base2, root=tmp_path)
    assert summary.new_ids == []
    assert summary.removed == [("canon/statements/lonely.md", "thm.lonely")]


def test_no_changes_against_current_head(tmp_path):
    base = base_repo(tmp_path)
    summary = pa.summarize_diff(base, root=tmp_path)
    assert not summary.new_ids and not summary.changed_ids and not summary.removed


def test_bad_base_ref_raises_clear_error(tmp_path):
    base_repo(tmp_path)
    try:
        pa.summarize_diff("origin/does-not-exist", root=tmp_path)
        assert False, "expected BaseRefError"
    except pa.BaseRefError as exc:
        assert "does-not-exist" in str(exc)


# --- filter_findings: scoped to touched ids/files -----------------------------------

def test_findings_filtered_excludes_unrelated_preexisting_error(tmp_path):
    # thm.q has a pre-existing dangling reference, present at the base ref already and
    # untouched by this PR's diff — it should not show up in the scoped report.
    base = base_repo(tmp_path)
    write(tmp_path, "statements/q.md", """
        ---
        id: thm.q
        type: statement
        name: Q
        statement: x
        stated_in: src.nowhere
        ---
        Q's body.
    """)
    base2 = _commit_all(tmp_path, "add thm.q with dangling stated_in")

    # PR: add an unrelated, valid new entity.
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        statement: x
        stated_in: src.paper
        ---
        C's body.
    """)
    stage(tmp_path)

    canon = load_canon(tmp_path / "canon")
    assert canon.entities["thm.q"]  # loaded fine; the dangling ref is a validator finding
    summary = pa.summarize_diff(base2, root=tmp_path, canon=canon)
    assert summary.new_ids == ["thm.c"]

    from tools.validate import validate
    rep = validate(canon)
    assert any(f.id == "thm.q" for f in rep.findings)    # the pre-existing error exists...

    touched_ids = set(summary.new_ids) | set(summary.changed_ids)
    scoped = pa.filter_findings(rep.findings, touched_ids, summary.touched_files)
    assert not any(f.id == "thm.q" for f in scoped)      # ...but is filtered out of scope


def test_findings_filtered_includes_diff_introduced_error(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        statement: x
        stated_in: src.missing
        ---
        C's body.
    """)
    stage(tmp_path)
    canon = load_canon(tmp_path / "canon")
    summary = pa.summarize_diff(base, root=tmp_path, canon=canon)
    assert summary.new_ids == ["thm.c"]

    from tools.validate import validate
    rep = validate(canon)
    touched_ids = set(summary.new_ids) | set(summary.changed_ids)
    scoped = pa.filter_findings(rep.findings, touched_ids, summary.touched_files)
    assert any(f.id == "thm.c" and f.code == "dangling-reference" for f in scoped)


# --- render_report / annotate: end-to-end -------------------------------------------

def test_annotate_reports_new_entity_and_zero_findings_on_clean_canon(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        statement: x
        stated_in: src.paper
        ---
        C's body.
    """)
    stage(tmp_path)
    report = pa.annotate(base, root=tmp_path)
    assert "thm.c" in report
    assert "New entities (1)" in report
    assert "no findings scoped to this diff" in report


def test_render_report_no_changes_message():
    summary = pa.DiffSummary()
    report = pa.render_report("origin/main", summary, [])
    assert "No canon changes detected" in report


# --- workflow YAML sanity ------------------------------------------------------------

def test_pr_validation_workflow_yaml_is_valid_and_targets_exist():
    """The workflow parses, and every make target it references actually exists in the
    Makefile. (As written, the workflow calls `python -m tools.*` directly rather than
    through `make`, mirroring ci.yml — so this is also a guard against a future edit
    reintroducing a `make <target>` call that typos.)
    """
    import re

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow_path = root / ".github" / "workflows" / "pr-validation.yml"
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)
    assert "jobs" in workflow and workflow["jobs"], "workflow defines no jobs"

    makefile_text = (root / "Makefile").read_text()
    declared_targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile_text, re.MULTILINE))
    assert {"pr-annotate", "impact", "canon-check"} <= declared_targets

    workflow_text = workflow_path.read_text()
    referenced = set(re.findall(r"make ([\w-]+)", workflow_text))
    assert referenced <= declared_targets, \
        f"workflow references undefined make target(s): {referenced - declared_targets}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
