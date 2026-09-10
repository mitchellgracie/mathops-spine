"""Tests for the lane write guard (tools.lanes) — the branch=hat boundary, WP-C.

Pure-function tests plus hook-payload plumbing; nothing here shells out to git (the
branch is always passed explicitly) or touches the real working tree. What we pin is
the contract from .docs/TEAM.md: strict denial on lane-prefixed branches with the
owning lane and handoff move named, warn-only on main/unprefixed branches, hand-edit
denial for the machine-written trees, and fail-open behavior for anything the guard
cannot positively place.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import lanes
from tools.common import ROOT


# --- the acceptance pair (.docs/TEAM.md WP-C) ---------------------------------

def test_author_branch_editing_tools_is_denied_naming_infra_and_the_handoff():
    v = lanes.check_write("author/fix-a-statement", "tools/validate.py")
    assert v.decision == "deny"
    assert "AUTHOR" in v.message          # the lane the session is in
    assert "INFRA" in v.message           # the lane that owns the path
    assert "infra/" in v.message          # the concrete branch move
    assert "handoff" in v.message.lower()


def test_main_editing_tools_warns_instead_of_denying():
    v = lanes.check_write("main", "tools/validate.py")
    assert v.decision == "warn"
    assert "INFRA" in v.message
    assert "no lane" in v.message


# --- lane resolution ----------------------------------------------------------

def test_lane_for_branch_prefixes():
    assert lanes.lane_for_branch("infra/wp-c-lane-guard").name == "infra"
    assert lanes.lane_for_branch("author/test-run0").name == "author"
    assert lanes.lane_for_branch("research/attack-notes").name == "research"
    for unlaned in ("main", "", "legacy-x", "archive/old", "infra"):  # no slash
        assert lanes.lane_for_branch(unlaned) is None


def test_owning_lanes_raw_has_two_owners():
    # raw/ is the one tree AUTHOR and RESEARCHER share; the map must say so rather
    # than silently picking a winner.
    owners = {l.name for l in lanes.owning_lanes("raw/drafts/x.md")}
    assert owners == {"author", "research"}


# --- per-lane allow/deny matrix -----------------------------------------------

def test_each_lane_allows_its_own_surface():
    assert lanes.check_write("infra/x", "schemas/base.py").decision == "allow"
    assert lanes.check_write("infra/x", "Makefile").decision == "allow"
    assert lanes.check_write("infra/x", ".claude/commands/research.md").decision == "allow"
    assert lanes.check_write("author/x", "canon/statements/thm.a.md").decision == "allow"
    assert lanes.check_write("author/x", "extraction/plans/p.md").decision == "allow"
    assert lanes.check_write("author/x", "raw/papers/p.md").decision == "allow"
    assert lanes.check_write("research/x", "raw/sessions/s.md").decision == "allow"
    assert lanes.check_write("research/x", "computations/case_check.py").decision == "allow"


def test_cross_lane_writes_are_denied():
    assert lanes.check_write("infra/x", "canon/statements/thm.a.md").decision == "deny"
    assert lanes.check_write("infra/x", "raw/papers/p.md").decision == "deny"
    assert lanes.check_write("author/x", "schemas/base.py").decision == "deny"
    assert lanes.check_write("author/x", "Makefile").decision == "deny"
    # settled decision 1: the researcher writes the script, the file lists only
    # extract-plan for plans/ — so a direct plan edit is out of lane for research
    assert lanes.check_write("research/x", "extraction/plans/p.md").decision == "deny"
    assert lanes.check_write("research/x", "canon/statements/thm.a.md").decision == "deny"
    assert lanes.check_write("research/x", "tools/lanes.py").decision == "deny"


def test_exact_file_entries_do_not_match_prefixes():
    # "Makefile" is an exact entry: it must not license "Makefile.bak" or a
    # "Makefile/" tree; prefix semantics are reserved for entries ending in "/".
    assert lanes.check_write("infra/x", "Makefile.bak").decision == "deny"


def test_path_owned_by_no_lane_is_denied_on_a_lane_branch_and_allowed_on_main():
    v = lanes.check_write("author/x", "stray-toplevel-notes.md")
    assert v.decision == "deny"
    assert "No lane owns it" in v.message
    assert lanes.check_write("main", "stray-toplevel-notes.md").decision == "allow"


# --- machine-written trees ----------------------------------------------------

def test_machine_written_trees_are_denied_in_every_lane():
    for branch in ("infra/x", "author/x", "research/x"):
        for path in ("derived/graph.json", "derived/capsules/thm.a.md",
                     "expositions/survey/001.md"):
            v = lanes.check_write(branch, path)
            assert v.decision == "deny", (branch, path)
    assert "make build" in lanes.check_write("infra/x", "derived/graph.json").message
    assert "extract-apply" in lanes.check_write("author/x", "expositions/s/1.md").message


def test_machine_written_trees_warn_on_main():
    v = lanes.check_write("main", "derived/graph.json")
    assert v.decision == "warn"
    assert "denied on any lane branch" in v.message


# --- hook plumbing ------------------------------------------------------------

def _payload(file_path: str, cwd: str | None = None, key: str = "file_path") -> dict:
    return {"tool_name": "Edit", "tool_input": {key: file_path}, "cwd": cwd or str(ROOT)}


def test_hook_response_denies_with_permission_decision_shape():
    resp = lanes.hook_response(_payload(str(ROOT / "tools" / "validate.py")),
                               branch="author/x")
    out = resp["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "INFRA" in out["permissionDecisionReason"]


def test_hook_response_warns_via_system_message_on_main():
    resp = lanes.hook_response(_payload(str(ROOT / "tools" / "validate.py")),
                               branch="main")
    assert set(resp) == {"systemMessage"}


def test_hook_response_allows_in_lane_and_outside_repo_and_on_missing_path():
    assert lanes.hook_response(_payload(str(ROOT / "tools" / "x.py")),
                               branch="infra/x") == {}
    # outside the repo (e.g. the session scratchpad): not this guard's business
    assert lanes.hook_response(_payload("/tmp/scratch/notes.md"), branch="author/x") == {}
    # a payload with no recognizable path falls through, fail-open
    assert lanes.hook_response({"tool_name": "Edit", "tool_input": {}},
                               branch="author/x") == {}


def test_hook_response_resolves_relative_paths_against_cwd_and_notebook_key():
    resp = lanes.hook_response(
        _payload("canon/statements/thm.a.md", cwd=str(ROOT)), branch="infra/x")
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"
    resp = lanes.hook_response(
        _payload(str(ROOT / "canon" / "nb.ipynb"), key="notebook_path"), branch="infra/x")
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_hook_mode_is_fail_open_on_garbage_input(capsys):
    rc = lanes.main(["--hook"], stdin=io.StringIO("not json at all"))
    assert rc == 0                       # never blocks unrelated work
    assert "allowing write" in capsys.readouterr().err


def test_main_hook_mode_emits_json_and_exit_zero_even_on_deny(capsys):
    payload = json.dumps(_payload(str(ROOT / "schemas" / "base.py")))
    rc = lanes.main(["--hook", "--branch", "author/x"], stdin=io.StringIO(payload))
    assert rc == 0                       # hook protocol: the JSON carries the deny
    resp = json.loads(capsys.readouterr().out)
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the CI stream lint (WP-D) ------------------------------------------------

def test_lint_flags_a_lane_crossing_pr_naming_both_lanes_and_the_split():
    # The WP-D acceptance case: a PR from author/* touching schemas/.
    findings, note = lanes.lint_diff(
        "author/fix-thm", ["canon/statements/thm.a.md", "schemas/base.py"])
    assert note == ""
    assert [f.path for f in findings] == ["schemas/base.py"]
    msg = findings[0].message
    assert "AUTHOR" in msg and "INFRA" in msg
    assert "Split this PR" in msg
    assert "infra/" in msg  # the concrete branch to move the change to


def test_lint_allows_each_lanes_committable_tool_output():
    # derived/ is committable from every lane (whichever lane staled it rebuilds it),
    # expositions/ only via the Author lane's extract-apply, extraction/plans/ from
    # research's own extract-plan runs.
    for branch in ("infra/x", "author/x", "research/x"):
        assert lanes.lint_diff(branch, ["derived/indices/tags.json"])[0] == []
    assert lanes.lint_diff("author/x", ["expositions/survey/001.md"])[0] == []
    assert lanes.lint_diff("research/x", ["extraction/plans/p.md"])[0] == []
    # ...and the same trees are still crossings for the lanes that don't produce them
    assert lanes.lint_diff("infra/x", ["expositions/survey/001.md"])[0] != []
    assert lanes.lint_diff("research/x", ["extraction/approved/p.md"])[0] != []


def test_lint_clean_and_unprefixed_cases():
    findings, note = lanes.lint_diff(
        "infra/x", ["tools/lanes.py", "tests/test_lanes.py", ".github/x.yml"])
    assert findings == [] and note == ""
    findings, note = lanes.lint_diff("main", ["schemas/base.py", "canon/a.md"])
    assert findings == []
    assert "no lane prefix" in note and "skipped" in note


def test_changed_paths_uses_a_merge_base_diff():
    class FakeRun:
        def __call__(self, cmd, **kwargs):
            from types import SimpleNamespace
            self.cmd = cmd
            return SimpleNamespace(returncode=0, stdout="a.md\n\ntools/x.py\n", stderr="")

    fake = FakeRun()
    paths = lanes.changed_paths("origin/main", run=fake)
    assert paths == ["a.md", "tools/x.py"]
    assert "origin/main...HEAD" in fake.cmd  # triple-dot: only the PR's own commits


def test_main_lint_mode_is_advisory_by_default_and_strict_on_request(capsys):
    args = ["--lint", "--branch", "author/x", "schemas/base.py"]
    assert lanes.main(args) == 0                     # advisory: warn, don't fail
    out = capsys.readouterr().out
    assert "::warning file=schemas/base.py::" in out
    assert "Split this PR" in out
    assert lanes.main(["--strict", *args]) == 1      # hard once tuned
    assert lanes.main(["--lint", "--branch", "infra/x", "tools/x.py"]) == 0
    assert "clean" in capsys.readouterr().out


def test_main_cli_mode_exit_codes(capsys):
    assert lanes.main(["--branch", "author/x", "--path", "tools/x.py"]) == 2
    assert lanes.main(["--branch", "main", "--path", "tools/x.py"]) == 0
    assert lanes.main(["--branch", "infra/x", "--path", "tools/x.py"]) == 0
    out = capsys.readouterr().out
    assert "deny" in out and "warn" in out and "allow" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
