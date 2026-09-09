"""Tests for tools.canon_check: the LLM half of the continuity-check pipeline.

Fake runner only — zero network, zero real LLM calls. Same temp-git-repo style as
tests/test_impact.py (this module consumes its `build_impact` directly), since
tools.canon_check's context assembly shells out to `git diff` for the per-entity diff
block.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import canon_check as cc


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


def write_exposition(root: Path, relpath: str, body: str) -> None:
    p = root / "expositions" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


BASE_THM = """
    ---
    id: thm.a
    type: statement
    name: A
    statement: |
      Original hypotheses, original conclusion.
    status: conjectured
    stated_in: src.paper
    ---
    A's body.
"""
CHANGED_THM = """
    ---
    id: thm.a
    type: statement
    name: A
    statement: |
      Weakened hypotheses, same conclusion.
    status: proved
    stated_in: src.paper
    ---
    A's body, revised.
"""


def base_repo(tmp_path: Path) -> str:
    """A minimal valid canon + one exposition: thm.a (mentioned by a writeup and by
    thm.c's prose) and thm.b (mentions nobody), committed as the repo's initial state."""
    _init_repo(tmp_path)
    write(tmp_path, "sources/paper.md", "---\nid: src.paper\ntype: source\nname: P\n---\nbody\n")
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
        C's body references [[thm.a]] as its main input.
    """)
    write_exposition(tmp_path, "survey/001-a.md", "A writeup about [[thm.a]].\n")
    return _commit_all(tmp_path, "base")


class FakeRunner:
    """Stand-in Runner: records every (system, prompt) call, replays canned replies."""

    def __init__(self, replies: list[str] | None = None, default: str = "[]"):
        self.replies = list(replies or [])
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self.replies:
            return self.replies.pop(0)
        return self.default


# --- no-changes short-circuit: zero runner calls -------------------------------------

def test_no_changes_makes_zero_runner_calls(tmp_path):
    base = base_repo(tmp_path)
    runner = FakeRunner()

    findings = cc.check_canon(base, root=tmp_path, runner=runner)

    assert findings == {}
    assert runner.calls == []


def test_only_new_entity_is_still_zero_runner_calls(tmp_path):
    # A brand-new entity has nothing yet to contradict (see module docstring) — scope is
    # changed_ids only, so a diff that ONLY adds an entity still short-circuits.
    base = base_repo(tmp_path)
    write(tmp_path, "statements/d.md", """
        ---
        id: thm.d
        type: statement
        name: D
        statement: x
        ---
        Brand new.
    """)
    runner = FakeRunner()

    findings = cc.check_canon(base, root=tmp_path, runner=runner)

    assert findings == {}
    assert runner.calls == []


def test_only_changed_entity_gets_a_call_not_new_ones(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/d.md", """
        ---
        id: thm.d
        type: statement
        name: D
        statement: x
        ---
        Brand new.
    """)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    runner = FakeRunner()

    findings = cc.check_canon(base, root=tmp_path, runner=runner)

    assert list(findings) == ["thm.a"]
    assert len(runner.calls) == 1


# --- context assembly: budget + truncation --------------------------------------------

def test_context_includes_source_diff_neighbor_and_exposition(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    from tools.loader import load_canon

    canon = load_canon(tmp_path / "canon")
    impact_entry = {"expositions": ["expositions/survey/001-a.md"], "mentioned_by": ["thm.c"],
                    "claim_fields_changed": ["statement", "status"]}

    context = cc.assemble_entity_context(
        "thm.a", base=base, canon=canon, impact_entry=impact_entry, root=tmp_path,
        capsules_dir=tmp_path / "derived" / "capsules",
    )

    assert "ENTITY thm.a" in context and "A's body, revised." in context
    assert f"DIFF vs {base}" in context and "Weakened hypotheses" in context
    assert "neighbor: src.paper" in context        # thm.a's 1-hop neighbor (its source)
    assert "exposition excerpt: expositions/survey/001-a.md" in context
    assert "[[thm.a]]" in context


def test_context_truncates_long_exposition_excerpt(tmp_path):
    base = base_repo(tmp_path)
    long_text = "word " * (cc.MAX_SCENE_EXCERPT_CHARS)  # comfortably over the cap
    write_exposition(tmp_path, "survey/001-a.md", f"[[thm.a]] {long_text}\n")
    _commit_all(tmp_path, "lengthen writeup")
    write(tmp_path, "statements/a.md", CHANGED_THM)
    from tools.loader import load_canon

    canon = load_canon(tmp_path / "canon")
    impact_entry = {"expositions": ["expositions/survey/001-a.md"], "mentioned_by": [],
                    "claim_fields_changed": ["statement"]}

    context = cc.assemble_entity_context(
        "thm.a", base=base, canon=canon, impact_entry=impact_entry, root=tmp_path,
        capsules_dir=tmp_path / "derived" / "capsules",
    )

    assert "...[truncated]" in context
    # the excerpt block itself never exceeds the per-file cap (plus its own headers)
    excerpt_start = context.index("exposition excerpt: expositions/survey/001-a.md")
    excerpt = context[excerpt_start:excerpt_start + cc.MAX_SCENE_EXCERPT_CHARS + 200]
    assert len(long_text) > cc.MAX_SCENE_EXCERPT_CHARS   # sanity: the fixture really is long
    assert "...[truncated]" in excerpt


def test_budget_drops_lower_priority_blocks_with_notice(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    from tools.loader import load_canon

    canon = load_canon(tmp_path / "canon")
    impact_entry = {"expositions": ["expositions/survey/001-a.md"], "mentioned_by": ["thm.c"],
                    "claim_fields_changed": ["statement"]}

    # A budget too small to fit source+diff plus anything else -> every optional block
    # (neighbors, exposition excerpt) is dropped, with a visible notice.
    context = cc.assemble_entity_context(
        "thm.a", base=base, canon=canon, impact_entry=impact_entry, root=tmp_path,
        budget=1, capsules_dir=tmp_path / "derived" / "capsules",
    )

    assert "ENTITY thm.a" in context               # source: always kept
    assert f"DIFF vs {base}" in context            # diff: always kept
    assert "dropped" in context and "budget 1 tok" in context
    assert "neighbor: src.paper" not in context
    assert "exposition excerpt" not in context


# --- a canned valid finding renders to Markdown and JSON ------------------------------

VALID_REPLY = """[
  {
    "severity": "high",
    "entities": ["thm.a", "thm.c"],
    "claim": "thm.a's hypotheses were weakened but thm.c's proof still needs the stronger form.",
    "rationale": "thm.c's body invokes thm.a as its main input; the diff removes the hypothesis that use depends on.",
    "suggested_override": "intentional_conflicts:\\n  - target: thm.c\\n    nature: \\"hypothesis mismatch\\"\\n    resolution_status: unresolved\\n"
  }
]"""


def test_canned_valid_finding_renders_markdown_and_json(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    runner = FakeRunner([VALID_REPLY])

    findings = cc.check_canon(base, root=tmp_path, runner=runner,
                              capsules_dir=tmp_path / "derived" / "capsules")

    assert list(findings) == ["thm.a"]
    [finding] = findings["thm.a"]
    assert finding.severity == "high"
    assert finding.entities == ["thm.a", "thm.c"]
    assert "intentional_conflicts" in finding.suggested_override

    md = cc.render_markdown(base, findings)
    assert "HIGH" in md and "thm.a" in md and "thm.c" in md
    assert "suggested override" in md
    assert "intentional_conflicts:" in md

    data = cc.to_json(findings)
    assert data == {"thm.a": [finding.to_dict()]}
    assert data["thm.a"][0]["severity"] == "high"


def test_empty_array_reply_is_a_valid_no_issues_result(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", BASE_THM.replace("A's body.", "A's body, reworded."))
    runner = FakeRunner(["[]"])

    findings = cc.check_canon(base, root=tmp_path, runner=runner,
                              capsules_dir=tmp_path / "derived" / "capsules")

    assert findings == {"thm.a": []}
    md = cc.render_markdown(base, findings)
    assert "no issues found" in md


# --- malformed model output degrades to the one advisory finding ---------------------

def test_malformed_output_degrades_to_advisory_finding(tmp_path):
    base = base_repo(tmp_path)
    write(tmp_path, "statements/a.md", CHANGED_THM)
    runner = FakeRunner(["Sorry, I can't help with that in JSON form."])

    findings = cc.check_canon(base, root=tmp_path, runner=runner,
                              capsules_dir=tmp_path / "derived" / "capsules")

    [finding] = findings["thm.a"]
    assert finding.claim == "canon-check output unparseable"
    assert finding.severity == "low"
    assert "Sorry, I can't help" in finding.rationale
    assert finding.suggested_override is None


def test_malformed_json_but_not_a_list_degrades(tmp_path):
    assert cc.parse_findings('{"severity": "high"}', entity_id="thm.a")[0].claim == \
        "canon-check output unparseable"


def test_markdown_fenced_valid_json_still_parses(tmp_path):
    fenced = "```json\n[]\n```"
    assert cc.parse_findings(fenced, entity_id="thm.a") == []


def test_one_bad_item_invalidates_the_whole_batch(tmp_path):
    # A partially-conforming response is not selectively trusted (see parse_findings'
    # docstring) — one malformed element degrades the WHOLE response, not just that item.
    raw = '[{"severity": "high", "entities": ["thm.a"], "claim": "x", "rationale": "y", ' \
          '"suggested_override": null}, {"severity": "not-a-real-severity"}]'
    result = cc.parse_findings(raw, entity_id="thm.a")
    assert len(result) == 1
    assert result[0].claim == "canon-check output unparseable"


# --- --strict exit codes ---------------------------------------------------------------

def test_strict_exits_1_on_high_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "check_canon", lambda *a, **k: {
        "thm.a": [cc.CanonCheckFinding("high", ["thm.a"], "claim", "rationale")],
    })
    assert cc.main(["--strict"]) == 1


def test_strict_exits_0_on_low_or_no_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "check_canon", lambda *a, **k: {
        "thm.a": [cc.CanonCheckFinding("low", ["thm.a"], "claim", "rationale")],
    })
    assert cc.main(["--strict"]) == 0

    monkeypatch.setattr(cc, "check_canon", lambda *a, **k: {})
    assert cc.main(["--strict"]) == 0


def test_non_strict_exits_0_even_with_high_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "check_canon", lambda *a, **k: {
        "thm.a": [cc.CanonCheckFinding("high", ["thm.a"], "claim", "rationale")],
    })
    assert cc.main([]) == 0


def test_bad_base_ref_exits_1(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise cc.BaseRefError("no such ref")

    monkeypatch.setattr(cc, "check_canon", _raise)
    assert cc.main(["--base", "nonsense-ref"]) == 1


# --- render_markdown: no-changes message ------------------------------------------------

def test_render_markdown_no_changes_message():
    md = cc.render_markdown("origin/main", {})
    assert "Nothing to check" in md


# --- workflow YAML sanity (mirrors tests/test_pr_annotate.py's pattern) ---------------

def test_pr_workflow_has_canon_check_step_and_secret_gating():
    """The PR workflow still parses, gates the LLM step on the ANTHROPIC_API_KEY secret
    via env indirection (never referenced directly inside an `if:`), always sets
    continue-on-error on it, and posts a second, independently marker-keyed PR comment
    section.
    """
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow_path = root / ".github" / "workflows" / "pr-validation.yml"
    workflow_text = workflow_path.read_text()
    workflow = yaml.safe_load(workflow_text)
    assert "jobs" in workflow and workflow["jobs"], "workflow defines no jobs"

    # secrets are never referenced directly inside an `if:` line
    for line in workflow_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("if:"):
            assert "secrets." not in stripped, f"secret referenced directly in if: {stripped!r}"

    assert "secrets.ANTHROPIC_API_KEY" in workflow_text
    assert "has_key" in workflow_text
    assert "--backend api" in workflow_text
    assert "<!-- mathops-canon-check -->" in workflow_text
    assert "<!-- mathops-pr-annotate -->" in workflow_text   # the annotation marker

    steps = workflow["jobs"]["validate-and-annotate"]["steps"]
    canon_check_step = next(s for s in steps if s.get("id") == "canon_check_run")
    assert canon_check_step.get("continue-on-error") is True
    comment_steps = [s for s in steps if "canon-check PR comment" in (s.get("name") or "")]
    assert len(comment_steps) == 1
    assert comment_steps[0].get("continue-on-error") is True


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
