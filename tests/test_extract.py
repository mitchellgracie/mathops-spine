"""Tests for the extraction pipeline (tools.extract): plan, check, apply.

Each builds a tiny knowledge base (canon + raw file + hand-written plan) in a temp dir
and drives the tool functions directly, with the LLM step faked (make_plan takes an
injected runner; apply never calls one at all). They pin the pipeline's contract:
apply refuses before its first write (staleness, undecided findings, bad findings),
what the human approved is exactly what lands (rejected findings don't), declared
relations gain their reciprocal edge, and the emitted exposition is the raw prose
verbatim apart from anchor -> [[id]] substitutions and the provenance frontmatter.
"""
from __future__ import annotations

import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import extract as ex
from tools import provenance
from tools.build import build_unintegrated
from tools.capsules import hash_for
from tools.expositions import load_expositions
from tools.loader import load_canon
from tools.orchestrate import ROLES
from tools.validate import (Report, check_references, check_relations,
                            check_unintegrated)

RAW_PROSE = "# Chapter\n\nThe Base Bound gives the Sharp Bound at once. Nothing else is needed.\n"


def write(tmp: Path, relpath: str, body: str) -> Path:
    p = tmp / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())
    return p


def base_world(tmp: Path) -> Path:
    """A canon of three entities, plus one raw chapter mentioning a new statement."""
    write(tmp, "canon/statements/base_bound.md", """
        ---
        id: thm.base_bound
        type: statement
        name: Base Bound
        statement: x
        stated_in: src.paper
        ---
        Base bound body.
    """)
    write(tmp, "canon/sources/paper.md", "---\nid: src.paper\ntype: source\nname: P\n---\nbody\n")
    write(tmp, "canon/sources/other.md", "---\nid: src.other\ntype: source\nname: O\n---\nbody\n")
    write(tmp, "raw/chapter.md", RAW_PROSE)
    return tmp / "canon"


FINDING_CREATE = """
### Sharp Bound
```yaml
action: create
type: statement
id: thm.sharp_bound
evidence: ["The Base Bound gives the Sharp Bound at once."]
entity:
  name: Sharp Bound
  relations:
    - {type: generalized_by, target: thm.base_bound}
body: |
  A sharpening of [[thm.base_bound]].
anchors: ["Sharp Bound"]
decision: approved
```
"""

FINDING_MATCH = """
### Base Bound
```yaml
action: match
id: thm.base_bound
evidence: ["The Base Bound gives"]
add:
  aliases: [the fundamental bound]
anchors: ["Base Bound"]
decision: approved
```
"""

FINDING_REJECTED = """
### Ghost
```yaml
action: create
type: statement
id: thm.ghost
entity: {name: Ghost}
decision: rejected
```
"""

FINDING_SKIP = """
### The chapter's phrasing
```yaml
action: skip
evidence: ["not an entity"]
decision: approved
```
"""


def write_plan(tmp: Path, *findings: str, meta_overrides: dict | None = None) -> Path:
    raw = tmp / "raw" / "chapter.md"
    meta = {
        "source": "raw/chapter.md",
        "source_sha": ex.source_hash(raw.read_bytes()),
        "created": "20260101T000000Z",
        "status": "pending",
        "collection": "chapter",
        "title": "Chapter",
        **(meta_overrides or {}),
    }
    body = "# Extraction plan: raw/chapter.md\n\n## Summary\nA test.\n\n## Findings\n" \
           + "\n".join(findings)
    plan = tmp / "extraction" / "plans" / "chapter.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(ex._plan_text(meta, body))
    return plan


def run_apply(tmp: Path, plan: Path, **kwargs):
    return ex.apply_plan(
        plan,
        root=tmp,
        canon_dir=tmp / "canon",
        expositions_dir=tmp / "expositions",
        capsules_dir=tmp / "capsules",
        raw_extracted_dir=tmp / "raw" / "extracted",
        approved_dir=tmp / "extraction" / "approved",
        derive=False,
        **kwargs,
    )


# --- role registration ---------------------------------------------------------

def test_extractor_role_registered_and_guarded():
    assert ROLES["extractor"].edits_canon is False
    assert ROLES["extractor"].prompt_file == "extractor.md"


# --- plan parsing --------------------------------------------------------------

def test_parse_plan_round_trip(tmp_path):
    base_world(tmp_path)
    plan = ex.parse_plan(write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH, FINDING_SKIP))
    assert plan.meta["collection"] == "chapter"
    assert [f.action for f in plan.findings] == ["create", "match", "skip"]
    assert plan.findings[0].id == "thm.sharp_bound"
    assert plan.findings[0].anchors == ["Sharp Bound"]
    assert plan.findings[1].add["aliases"] == ["the fundamental bound"]
    assert not plan.structural_problems


def test_parse_plan_flags_broken_yaml(tmp_path):
    base_world(tmp_path)
    broken = "```yaml\naction: [unclosed\n```\n"
    plan = ex.parse_plan(write_plan(tmp_path, broken))
    assert any("unparseable" in p for p in plan.structural_problems)


# --- apply refusals (all fire before the first write) ---------------------------

def test_apply_refuses_undecided_findings(tmp_path):
    cd = base_world(tmp_path)
    pending = FINDING_CREATE.replace("decision: approved", "decision: pending")
    assert run_apply(tmp_path, write_plan(tmp_path, pending)) == 1
    assert not (cd / "statements" / "sharp_bound.md").exists()
    assert (tmp_path / "raw" / "chapter.md").exists()  # nothing moved


def test_apply_refuses_stale_source(tmp_path):
    base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE)
    (tmp_path / "raw" / "chapter.md").write_text(RAW_PROSE + "\nAn edit after planning.\n")
    assert run_apply(tmp_path, plan) == 1


def test_apply_refuses_create_of_existing_id(tmp_path):
    cd = base_world(tmp_path)
    clash = FINDING_CREATE.replace("thm.sharp_bound", "thm.base_bound") \
                          .replace("### Sharp Bound", "### Clash")
    assert run_apply(tmp_path, write_plan(tmp_path, clash)) == 1
    # and the original file was not touched
    assert "Base bound body." in (cd / "statements" / "base_bound.md").read_text()


def test_apply_refuses_missing_anchor(tmp_path):
    base_world(tmp_path)
    bad = FINDING_CREATE.replace('anchors: ["Sharp Bound"]', 'anchors: ["Nobody"]')
    assert run_apply(tmp_path, write_plan(tmp_path, bad)) == 1


def test_apply_refuses_anchor_only_in_heading(tmp_path):
    base_world(tmp_path)
    bad = FINDING_MATCH.replace('anchors: ["Base Bound"]', 'anchors: ["Chapter"]')
    assert run_apply(tmp_path, write_plan(tmp_path, FINDING_CREATE, bad)) == 1


def test_apply_refuses_set_conflict(tmp_path):
    base_world(tmp_path)
    conflict = FINDING_MATCH.replace("add:\n  aliases: [the fundamental bound]",
                                     "set:\n  stated_in: src.other")
    assert run_apply(tmp_path, write_plan(tmp_path, FINDING_CREATE, conflict)) == 1


def test_apply_refuses_unknown_add_key(tmp_path):
    """An `add:` key that isn't aliases/relations refuses instead of silently no-oping.

    Regression (inherited from the ancestor spine): a plan's approved updates carried
    an `add.` key no schema declares; apply printed "updated" per entity while writing
    nothing.
    """
    base_world(tmp_path)
    finding = """
### Base Bound
```yaml
action: update
id: thm.base_bound
add:
  corollaries: ["a fact with no schema field"]
decision: approved
```
"""
    plan = write_plan(tmp_path, finding)
    assert run_apply(tmp_path, plan) == 1
    assert "add.corollaries is not a mergeable field" in " ".join(
        ex.finding_problems(ex.parse_plan(plan).findings[0],
                            canon=load_canon(tmp_path / "canon"), created_ids=set(),
                            prose=None, claimed_anchors={}))


def test_apply_refuses_relation_target_outside_plan_and_canon(tmp_path):
    base_world(tmp_path)
    dangling = FINDING_CREATE.replace("target: thm.base_bound", "target: thm.nobody")
    assert run_apply(tmp_path, write_plan(tmp_path, dangling)) == 1


# --- apply: the happy path -------------------------------------------------------

def test_apply_materializes_approved_findings(tmp_path):
    cd = base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH, FINDING_REJECTED, FINDING_SKIP)
    assert run_apply(tmp_path, plan) == 0

    canon = load_canon(cd)
    assert not canon.errors
    assert "thm.sharp_bound" in canon.entities
    assert "thm.ghost" not in canon.entities             # rejected finding never lands
    assert "the fundamental bound" in canon.entities["thm.base_bound"].aliases

    # declared relation gained its reciprocal edge, so the relation gate is green
    rep = Report()
    check_references(canon, rep)
    check_relations(canon, rep)
    assert rep.ok(), rep.errors
    base_rels = {(r.type, r.target) for r in canon.entities["thm.base_bound"].relations}
    assert ("specializes", "thm.sharp_bound") in base_rels

    # bookkeeping: raw and plan moved, plan meta records what happened
    assert not (tmp_path / "raw" / "chapter.md").exists()
    assert (tmp_path / "raw" / "extracted" / "chapter.md").exists()
    approved = ex.parse_plan(tmp_path / "extraction" / "approved" / "chapter.md")
    assert approved.meta["status"] == "applied"
    assert approved.meta["created_entities"] == ["thm.sharp_bound"]
    assert approved.meta["exposition"] == "expositions/chapter/001-chapter.md"
    assert not (tmp_path / "extraction" / "plans" / "chapter.md").exists()


def test_apply_is_idempotent_refusal_on_rerun(tmp_path):
    base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)
    assert run_apply(tmp_path, plan) == 0
    # the plan moved; re-applying the archived copy must refuse (status: applied)
    archived = tmp_path / "extraction" / "approved" / "chapter.md"
    assert run_apply(tmp_path, archived) == 1


# --- add.notes: schema-less facts -> stamped unintegrated body blocks ---------------

FINDING_NOTES = """
### Base Bound (notes)
```yaml
action: update
id: thm.base_bound
evidence: ["The Base Bound gives"]
add:
  notes:
    - "The bound is sharp over finite fields."
    - "It fails in characteristic 2."
anchors: ["Base Bound"]
decision: approved
```
"""

APPLY_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def test_apply_appends_stamped_notes_block(tmp_path):
    cd = base_world(tmp_path)
    before = load_canon(cd)
    hash_before = hash_for(before, "thm.base_bound")
    plan = write_plan(tmp_path, FINDING_NOTES)
    assert run_apply(tmp_path, plan, now=APPLY_NOW) == 0

    body = (cd / "statements" / "base_bound.md").read_text().split("---\n", 2)[2]
    assert ("<!-- extraction: date=2026-09-09 source=raw/extracted/chapter.md "
            "plan=extraction/approved/chapter.md status=unintegrated -->") in body
    assert "- The bound is sharp over finite fields." in body
    assert "- It fails in characteristic 2." in body
    assert provenance.CLOSE_MARKER in body
    assert body.startswith("Base bound body.")   # settled prose untouched, block appended

    # the append is invisible to the capsule projection: still fresh until integration
    assert hash_for(load_canon(cd), "thm.base_bound") == hash_before
    # and the plan's bookkeeping records the entity as updated
    approved = ex.parse_plan(tmp_path / "extraction" / "approved" / "chapter.md")
    assert approved.meta["updated_entities"] == ["thm.base_bound"]


def test_apply_coalesces_notes_into_one_block_per_entity(tmp_path):
    cd = base_world(tmp_path)
    second = FINDING_NOTES.replace("### Base Bound (notes)", "### Base Bound (more)").replace(
        'notes:\n    - "The bound is sharp over finite fields."\n'
        '    - "It fails in characteristic 2."',
        'notes:\n    - "The constant is effective."',
    ).replace('anchors: ["Base Bound"]\n', "")
    plan = write_plan(tmp_path, FINDING_NOTES, second)
    assert run_apply(tmp_path, plan, now=APPLY_NOW) == 0
    body = (cd / "statements" / "base_bound.md").read_text()
    assert body.count("<!-- extraction:") == 1    # one stamp per entity per apply
    blocks = provenance.parse_blocks(body)
    assert [n.text for n in blocks[0].notes] == [
        "The bound is sharp over finite fields.",
        "It fails in characteristic 2.",
        "The constant is effective.",
    ]


def test_apply_notes_only_update_is_append_not_merge(tmp_path, capsys):
    cd = base_world(tmp_path)
    fm_before = frontmatter.load(cd / "statements" / "base_bound.md").metadata
    assert run_apply(tmp_path, write_plan(tmp_path, FINDING_NOTES), now=APPLY_NOW) == 0
    out = capsys.readouterr().out
    assert "appended 2 unintegrated note(s) to thm.base_bound" in out
    assert "updated thm.base_bound" not in out    # no frontmatter merge happened
    assert frontmatter.load(cd / "statements" / "base_bound.md").metadata == fm_before
    # the landed canon is gate-clean: blocks warn (unintegrated-notes), never error
    rep = Report()
    canon = load_canon(cd)
    check_unintegrated(canon, rep)
    assert rep.ok()
    assert any(f.code == "unintegrated-notes" for f in rep.findings)
    assert build_unintegrated(canon) == {"thm.base_bound": [{
        "date": "2026-09-09", "source": "raw/extracted/chapter.md",
        "plan": "extraction/approved/chapter.md", "notes": 2, "spoiler_notes": 0}]}


def test_check_refuses_malformed_notes(tmp_path):
    base_world(tmp_path)
    bad = """
### Base Bound
```yaml
action: update
id: thm.base_bound
add:
  notes:
    - ""
    - {spoiler: true}
    - {text: "ok", extra: 1}
    - 42
decision: approved
```
"""
    plan = write_plan(tmp_path, bad)
    assert run_apply(tmp_path, plan) == 1
    problems = " ".join(ex.finding_problems(
        ex.parse_plan(plan).findings[0], canon=load_canon(tmp_path / "canon"),
        created_ids=set(), prose=None, claimed_anchors={}))
    assert "add.notes[1] is empty" in problems
    assert "add.notes[2] mapping needs a non-empty `text`" in problems
    assert "add.notes[3] has unknown key(s) extra" in problems
    assert "add.notes[4] must be a string or a {text, spoiler} mapping" in problems

    not_a_list = bad.replace(
        'notes:\n    - ""\n    - {spoiler: true}\n    - {text: "ok", extra: 1}\n    - 42',
        "notes: just a string")
    problems = " ".join(ex.finding_problems(
        ex.parse_plan(write_plan(tmp_path, not_a_list)).findings[0],
        canon=load_canon(tmp_path / "canon"), created_ids=set(),
        prose=None, claimed_anchors={}))
    assert "add.notes must be a list" in problems


def test_apply_refuses_notes_on_create(tmp_path):
    base_world(tmp_path)
    create_with_notes = FINDING_CREATE.replace(
        "body: |", 'add:\n  notes: ["a fact"]\nbody: |')
    plan = write_plan(tmp_path, create_with_notes)
    assert run_apply(tmp_path, plan) == 1
    problems = " ".join(ex.finding_problems(
        ex.parse_plan(plan).findings[0], canon=load_canon(tmp_path / "canon"),
        created_ids=set(), prose=RAW_PROSE, claimed_anchors={}))
    assert "add/set are for match/update, not create" in problems


def test_create_with_entity_notes_gets_a_pointed_error(tmp_path):
    """Observed model failure: notes nested under entity: on a create.

    The generic pydantic extra_forbidden error doesn't tell a triager where the
    stranded facts should go; the named check does.
    """
    base_world(tmp_path)
    nested = FINDING_CREATE.replace(
        "entity:\n  name: Sharp Bound",
        'entity:\n  name: Sharp Bound\n  notes: ["a stranded fact"]')
    plan = write_plan(tmp_path, nested)
    assert run_apply(tmp_path, plan) == 1
    problems = " ".join(ex.finding_problems(
        ex.parse_plan(plan).findings[0], canon=load_canon(tmp_path / "canon"),
        created_ids=set(), prose=RAW_PROSE, claimed_anchors={}))
    assert "entity.notes is not an entity field" in problems
    assert "woven into `body:`" in problems


# --- exposition emission -----------------------------------------------------------

def test_exposition_is_verbatim_plus_links_and_frontmatter(tmp_path):
    base_world(tmp_path)
    assert run_apply(tmp_path, write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)) == 0
    exp = (tmp_path / "expositions" / "chapter" / "001-chapter.md").read_text()

    post = frontmatter.loads(exp)
    assert post.metadata["extracted_from"] == "raw/extracted/chapter.md"
    assert post.metadata["plan"] == "extraction/approved/chapter.md"
    assert post.metadata["source_sha"]                 # provenance hash carried over
    assert "tags" not in post.metadata                 # untagged material -> no tags key
    # Link, don't replace: the anchored span survives as the link label, so the
    # sentence stays grammatical (a bare [[id]] used to swallow its subject).
    expected = RAW_PROSE.replace(
        "The Base Bound gives the Sharp Bound",
        "The [[thm.base_bound|Base Bound]] gives the [[thm.sharp_bound|Sharp Bound]]",
    ).strip()
    assert post.content.strip() == expected            # prose verbatim apart from the links

    expositions = load_expositions(tmp_path / "expositions")
    assert len(expositions) == 1
    assert expositions[0].mentions == ["thm.base_bound", "thm.sharp_bound"]
    assert expositions[0].tags == []


def test_exposition_numbering_continues_a_collection(tmp_path):
    base_world(tmp_path)
    write(tmp_path, "expositions/chapter/002-earlier.md", "old writeup\n")
    assert run_apply(tmp_path, write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)) == 0
    assert (tmp_path / "expositions" / "chapter" / "003-chapter.md").exists()


def test_no_exposition_flag_skips_emission_and_anchor_checks(tmp_path):
    base_world(tmp_path)
    bad_anchor = FINDING_CREATE.replace('anchors: ["Sharp Bound"]', 'anchors: ["Nobody"]')
    assert run_apply(tmp_path, write_plan(tmp_path, bad_anchor), scene=False) == 0
    assert not (tmp_path / "expositions").exists()


# --- plan generation (fake runner) ---------------------------------------------------

def test_make_plan_writes_frontmatter_and_reparses(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    seen = {}

    def runner(system, prompt):
        seen["system"], seen["prompt"] = system, prompt
        return "## Summary\nA test.\n\n## Findings\n" + FINDING_CREATE

    dest = ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    assert dest is not None
    plan = ex.parse_plan(dest)
    assert plan.meta["source"] == "raw/chapter.md"
    assert plan.meta["source_sha"] == ex.source_hash((tmp_path / "raw/chapter.md").read_bytes())
    assert plan.meta["status"] == "pending"
    assert plan.meta["collection"] == "chapter"
    assert plan.meta["title"] == "Chapter"           # from the raw file's H1
    assert [f.id for f in plan.findings] == ["thm.sharp_bound"]

    # the Extractor got its contract, the candidate index, and the raw prose
    assert "# Output contract" in seen["system"]
    assert "thm.base_bound" in seen["prompt"]        # candidate index line
    assert "The Base Bound gives" in seen["prompt"]  # raw prose
    # deterministic pre-match: Base Bound is named verbatim in the text
    assert "Canon entities named verbatim" in seen["prompt"]


def test_make_plan_fills_missing_decisions(tmp_path):
    """A model that drops `decision:` lines still yields a triageable plan.

    Only the hole is filled — an explicit decision the block already carries is never
    rewritten.
    """
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    no_decision = FINDING_CREATE.replace("decision: approved\n", "")
    assert "decision" not in no_decision

    runner = lambda s, p: ("## Summary\nA test.\n\n## Findings\n"
                           + no_decision + FINDING_REJECTED)
    dest = ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    assert dest is not None
    plan = ex.parse_plan(dest)
    assert [f.decision for f in plan.findings] == ["pending", "rejected"]
    assert not plan.structural_problems


def test_cli_plan_forwards_force_and_gates_on_check(tmp_path, monkeypatch):
    """Regression: `extract plan --force` parsed the flag but never passed it on,
    so a regeneration silently refused and the stale plan stayed in place. Also pins
    the failure posture: the CLI runs check_plans on the plan it just wrote and its
    exit code IS the check's — plan-time schema problems must not be scroll-past WARNs."""
    captured = {}

    def fake_make_plan(raw, *, runner, force=False):
        captured["force"] = force
        return Path(raw)

    def fake_check_plans(*, paths=None, **kwargs):
        captured["checked"] = paths
        return 3

    monkeypatch.setattr(ex, "make_plan", fake_make_plan)
    monkeypatch.setattr(ex, "check_plans", fake_check_plans)
    monkeypatch.setattr(ex, "make_runner", lambda *a, **k: None)
    assert ex.main(["plan", "raw/x.md", "--force"]) == 3
    assert captured["force"] is True
    assert captured["checked"] == [Path("raw/x.md")]


def test_make_plan_refuses_to_clobber_pending_plan(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    plans = tmp_path / "extraction" / "plans"
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + FINDING_CREATE
    assert ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=plans) is not None
    assert ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=plans) is None      # already exists, no --force


# --- raw kinds + author context ------------------------------------------------------

NOTES_RAW = """\
---
kind: notes
context: |
  The Sharp Bound is Theorem 3.2 of the paper already in canon as src.paper.
---
# Notes

The Sharp Bound follows from the Base Bound over any field.
"""


def test_make_plan_passes_context_and_notes_guidance(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    (tmp_path / "raw" / "notes.md").write_text(NOTES_RAW)
    seen = {}

    def runner(system, prompt):
        seen["prompt"] = prompt
        return "## Summary\nA test.\n\n## Findings\n" + FINDING_CREATE

    dest = ex.make_plan("raw/notes.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    assert dest is not None
    assert "# Author context (ground truth)" in seen["prompt"]
    assert "Theorem 3.2 of the paper" in seen["prompt"]
    assert "reference NOTES" in seen["prompt"]        # kind-specific task guidance
    assert ex.parse_plan(dest).meta["kind"] == "notes"


def test_make_plan_sceneless_kinds_demand_exhaustive_findings(tmp_path):
    # For notes/transcripts the findings are the only landing surface (no exposition is
    # emitted), so the task must push per-entity exhaustiveness; a prose task must not
    # (there the exposition carries the full text and findings distill identity).
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    (tmp_path / "raw" / "notes.md").write_text(NOTES_RAW)
    seen = {}

    def runner(system, prompt):
        seen["prompt"] = prompt
        return "## Summary\nA test.\n\n## Findings\n" + FINDING_CREATE

    assert ex.make_plan("raw/notes.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans") is not None
    assert "ONLY landing surface" in seen["prompt"]
    assert "ALL-CAPS markers" in seen["prompt"]

    assert ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans") is not None
    assert "ONLY landing surface" not in seen["prompt"]


def test_make_plan_defaults_to_prose_kind(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + FINDING_CREATE
    dest = ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    plan = ex.parse_plan(dest)
    assert plan.meta["kind"] == "prose"
    assert not plan.structural_problems


def test_make_plan_rejects_unknown_kind(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    (tmp_path / "raw" / "notes.md").write_text("---\nkind: chapter\n---\ntext\n")
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + FINDING_CREATE
    assert ex.make_plan("raw/notes.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans") is None


def test_make_plan_refuses_underscore_template_files(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    (tmp_path / "raw" / "_TEMPLATE.md").write_text("---\nkind: prose\n---\ntext\n")
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + FINDING_CREATE
    assert ex.make_plan("raw/_TEMPLATE.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans") is None


def test_apply_notes_plan_skips_exposition_and_anchor_checks(tmp_path):
    cd = base_world(tmp_path)
    bad_anchor = FINDING_CREATE.replace('anchors: ["Sharp Bound"]', 'anchors: ["Nobody"]')
    plan = write_plan(tmp_path, bad_anchor, meta_overrides={"kind": "notes"})
    # scene is not passed False here — the plan's kind alone must suppress emission
    assert run_apply(tmp_path, plan) == 0
    assert not (tmp_path / "expositions").exists()
    assert "thm.sharp_bound" in load_canon(cd).entities
    assert (tmp_path / "raw" / "extracted" / "chapter.md").exists()


def test_check_plans_skips_anchor_checks_for_notes(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    bad_anchor = FINDING_CREATE.replace('anchors: ["Sharp Bound"]', 'anchors: ["Nobody"]')
    write_plan(tmp_path, bad_anchor, meta_overrides={"kind": "notes"})
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 0


def test_check_plans_flags_unknown_kind(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    write_plan(tmp_path, FINDING_CREATE, meta_overrides={"kind": "chapter"})
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1


# --- buckets (folder -> kind), transcript kind, and tags -----------------------------

def test_default_kind_for_raw_maps_buckets(tmp_path):
    # Pure path logic: the first component under raw/ decides the default kind; a file
    # at raw/ root (or outside raw/) is unbucketed (None -> caller falls back to prose).
    root = tmp_path
    assert ex.default_kind_for_raw(root / "raw" / "drafts" / "x.md", root) == "prose"
    assert ex.default_kind_for_raw(root / "raw" / "papers" / "x.md", root) == "notes"
    assert ex.default_kind_for_raw(root / "raw" / "sessions" / "2026" / "x.md", root) == "transcript"
    assert ex.default_kind_for_raw(root / "raw" / "x.md", root) is None
    assert ex.default_kind_for_raw(root / "elsewhere" / "x.md", root) is None


def test_make_plan_infers_transcript_kind_and_records_tags(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    write(tmp_path, "raw/sessions/session.md",
          "---\ntags: [Literature, technique, technique]\n---\n# Session\n\nWe discussed the Base Bound.\n")
    seen = {}

    def runner(system, prompt):
        seen["prompt"] = prompt
        return "## Summary\nx.\n\n## Findings\n" + FINDING_CREATE

    dest = ex.make_plan("raw/sessions/session.md", runner=runner, canon=canon,
                        root=tmp_path, plans_dir=tmp_path / "extraction" / "plans")
    assert dest is not None
    plan = ex.parse_plan(dest)
    assert plan.kind == "transcript"                  # inferred from the bucket, no `kind:`
    assert plan.tags == ["literature", "technique"]   # normalized: lowercased, de-duped, sorted
    assert "TRANSCRIPT" in seen["prompt"]             # kind-specific extractor guidance


def test_apply_transcript_plan_skips_exposition(tmp_path):
    cd = base_world(tmp_path)
    # A transcript never emits an exposition, so a bad anchor is irrelevant (mirrors notes).
    bad_anchor = FINDING_CREATE.replace('anchors: ["Sharp Bound"]', 'anchors: ["Nobody"]')
    plan = write_plan(tmp_path, bad_anchor, meta_overrides={"kind": "transcript"})
    assert run_apply(tmp_path, plan) == 0
    assert not (tmp_path / "expositions").exists()
    assert "thm.sharp_bound" in load_canon(cd).entities


def test_apply_writes_tags_into_exposition_frontmatter_and_index(tmp_path):
    base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH,
                      meta_overrides={"tags": ["technique", "literature"]})
    assert run_apply(tmp_path, plan) == 0

    expositions = load_expositions(tmp_path / "expositions")
    assert expositions[0].tags == ["literature", "technique"]  # normalized + sorted

    from tools.build import build_tags
    assert build_tags(expositions) == {
        "literature": ["expositions/chapter/001-chapter.md"],
        "technique": ["expositions/chapter/001-chapter.md"],
    }


# --- check -----------------------------------------------------------------------------

def test_check_plans_reports_problems(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    bad = FINDING_CREATE.replace("action: create", "action: conjure")
    write_plan(tmp_path, bad)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1


def test_check_plans_passes_clean_plan(tmp_path):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 0


def test_check_plans_fails_out_of_vocabulary_status(tmp_path):
    # The gate-leg contract: a create proposing a status outside STATEMENT_STATUSES
    # must fail extract-check (the Pydantic dry-run in finding_problems), not surface
    # weeks later when apply instantiates the entity.
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    bad = FINDING_CREATE.replace("  name: Sharp Bound",
                                 "  name: Sharp Bound\n  status: stated")
    write_plan(tmp_path, bad)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1


FINDING_CREATE_PROOF = """
### Proof of the Base Bound
```yaml
action: create
type: proof
id: prf.base_bound_v1
evidence: ["The Base Bound gives the Sharp Bound at once."]
entity:
  name: Proof of the Base Bound
  proves: thm.base_bound
body: |
  Immediate.
decision: approved
```
"""


def test_check_plans_advises_on_omitted_defaulted_fields(tmp_path, capsys):
    # A proof create omitting `completeness` (and a statement create omitting
    # `status`) still gates green — leaning on a default is legitimate — but the
    # triager must see what silence materializes as: `note` lines, exit-neutral.
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    write_plan(tmp_path, FINDING_CREATE, FINDING_CREATE_PROOF)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 0
    out = capsys.readouterr().out
    assert "note  prf.base_bound_v1: `completeness` not set" in out
    assert 'defaults to "complete"' in out
    assert "note  thm.sharp_bound: `status` not set" in out


def test_check_plans_no_advisory_when_defaulted_field_is_explicit(tmp_path, capsys):
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    explicit = FINDING_CREATE_PROOF.replace(
        "  proves: thm.base_bound",
        "  proves: thm.base_bound\n  completeness: sketch")
    write_plan(tmp_path, explicit)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 0
    assert "completeness" not in capsys.readouterr().out


# --- claim check (deterministic recall net) --------------------------------------------

CLAIMY_PROSE = (
    "# Chapter\n"
    "\n"
    "The Base Bound meets the Sharp Bound at the boundary.\n"
    "They rest on the **Glass Concord** and on *shardwrights*, as all shardwrights must.\n"
    "The referee was *not* amused.\n"
    "The Weeping Stone lemma watched them go; so did Duke Bram.\n"
    "*A whole emphasized sentence is not a term.*\n"
    "They argued about Vintners and Tinkers on the road to High Varen.\n"
)


def claimy_world(tmp: Path) -> Path:
    cd = base_world(tmp)
    write(tmp, "raw/claimy.md", CLAIMY_PROSE)
    return cd


def test_normalize_term_folds_and_strips():
    assert ex._normalize_term("The Severed") == "severed"
    assert ex._normalize_term("Lanfrisuïm") == "lanfrisuim"
    assert ex._normalize_term("Ridilak Mudd's") == "ridilak mudd"
    assert ex._normalize_term("**Eukar River**") == "eukar river"


def test_harvest_terms_emphasis_titlecase_and_filters():
    terms = ex.harvest_terms(CLAIMY_PROSE)
    assert "Glass Concord" in terms          # bold multi-word
    assert "shardwrights" in terms           # lowercase italic, occurs twice
    assert "Weeping Stone" in terms          # TitleCase run, "The" trimmed
    assert "High Varen" in terms             # TitleCase run after lowercase words
    assert "Vintners and Tinkers" in terms   # coordinated run, kept whole
    assert "not" not in terms                # single stress italic, once + stopword
    assert "Chapter" not in terms            # headings are not harvested
    assert "Duke Bram" not in terms          # rank lead trimmed -> single word dropped
    assert all("emphasized sentence" not in t for t in terms)  # sentence punct filter
    # dedupe on normalized form: one entry per term
    assert len(terms) == len({ex._normalize_term(t) for t in terms})


STRUCTURAL_PROSE = """\
# Notes

**Arrival and mourning**

**Falling Reeds**

- **The rumours**: the field talks of the **Glasswork Concord** and nothing else.
- **Redvane Harbour**: a run-in label whose name still surfaces.
- *(CANON FIX NEEDED: current canon conflates the two lemmas.)* A fix EARMARK RESOLVED.
- TO FIGURE OUT: the base-change step. It went **silent for weeks** in review.
- The referee report *(fuller treatment deferred)* is the crux of the revision.
"""


def test_harvest_skips_structural_emphasis_and_allcaps_flags():
    terms = ex.harvest_terms(STRUCTURAL_PROSE)
    # A whole-line bold span is a pseudo-heading: the emphasis signal is structural...
    assert "Arrival and mourning" not in terms
    # ...but a name-shaped heading still surfaces via the TitleCase path.
    assert "Falling Reeds" in terms
    # Run-in label (bold opening the line, colon right after): structural...
    assert all("rumours" not in t for t in terms)
    # ...with the same TitleCase safety net for name-shaped labels.
    assert "Redvane Harbour" in terms
    # Emphasis anywhere mid-sentence stays a signal — bold is the author's
    # claim-bearing convention, whatever the casing.
    assert "Glasswork Concord" in terms
    assert "silent for weeks" in terms
    # ALL-CAPS process flags are never terms, via either harvest path.
    assert not any("FIGURE OUT" in t or "CANON FIX" in t or "EARMARK" in t
                   for t in terms)
    # A fully parenthesized emphasized span is an authorial aside, not a coinage.
    assert all("fuller treatment" not in t for t in terms)


def test_unclaimed_terms_canon_findings_and_compounds(tmp_path):
    cd = claimy_world(tmp_path)
    write(tmp_path, "canon/techniques/tinkers.md",
          "---\nid: tech.tinkers\ntype: technique\nname: Tinkers\n---\nbody\n")
    write(tmp_path, "canon/techniques/vintners.md",
          "---\nid: tech.vintners\ntype: technique\nname: Vintners\n---\nbody\n")
    canon = load_canon(cd)
    finding = """
### Glass Concord
```yaml
action: create
type: technique
id: tech.glass_concord
evidence: ["They rest on the **Glass Concord**"]
entity: {name: The Glass Concord}
body: |
  A pact of methods.
decision: pending
```
"""
    findings = ex.parse_findings("## Findings\n" + finding)
    un = ex.unclaimed_terms(CLAIMY_PROSE, findings, canon)
    assert "Glass Concord" not in un          # claimed by the finding (id slug + text)
    assert "Vintners and Tinkers" not in un   # compound: every part claimed by canon
    assert "shardwrights" in un
    assert "Weeping Stone" in un
    assert "High Varen" in un
    # an unclaimed part keeps a compound on the list
    un2 = ex.unclaimed_terms("The pair are *Mann and Umirn the Divine Twins*.\n",
                             findings, canon)
    assert un2 == ["Mann and Umirn the Divine Twins"]


def test_claim_variants_rank_and_plural():
    assert "lanfrisuim" in ex._claim_variants("emperor of lanfrisuim")
    assert "elthar" in ex._claim_variants("elthars")


def test_make_plan_appends_claim_stubs_that_self_claim(tmp_path):
    cd = claimy_world(tmp_path)
    canon = load_canon(cd)
    finding = FINDING_CREATE.replace("decision: approved", "decision: pending")
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + finding
    dest = ex.make_plan("raw/claimy.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    plan = ex.parse_plan(dest)
    stubs = [f for f in plan.findings if f.data.get("term")]
    assert {f.data["term"] for f in stubs} >= {"Glass Concord", "shardwrights",
                                               "Weeping Stone", "High Varen"}
    assert all(f.action == "skip" and f.decision == "pending" for f in stubs)
    assert all(ex.CLAIM_STUB_EVIDENCE in " ".join(f.data.get("evidence") or [])
               for f in stubs)
    # stubs claim their own terms: the check finds nothing further to demand
    prose = frontmatter.load(tmp_path / "raw" / "claimy.md").content
    assert ex.unclaimed_terms(prose, plan.findings, canon) == []
    # and check_plans agrees once decisions are ruled
    dest.write_text(dest.read_text().replace("decision: pending", "decision: approved"))
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 0


def test_check_plans_reports_unclaimed_term(tmp_path):
    cd = base_world(tmp_path)
    (tmp_path / "raw" / "chapter.md").write_text(
        RAW_PROSE + "\nAll feared the **Iron Tithe**.\n")
    canon = load_canon(cd)
    write_plan(tmp_path, FINDING_CREATE)   # covers Sharp Bound, not the Iron Tithe
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1


def test_apply_ignores_approved_claim_stub(tmp_path):
    cd = base_world(tmp_path)
    (tmp_path / "raw" / "chapter.md").write_text(
        RAW_PROSE + "\nAll feared the **Iron Tithe**.\n")
    stub = ex.claim_stub("Iron Tithe").replace("decision: pending", "decision: approved")
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH, stub)
    assert run_apply(tmp_path, plan) == 0
    assert not (cd / "techniques").exists()  # the stub created nothing
    archived = (tmp_path / "extraction" / "approved" / "chapter.md").read_text()
    assert "Iron Tithe" in archived          # but the ruling is in the audit trail


def test_compound_canon_name_claims_its_parts(tmp_path):
    cd = base_world(tmp_path)
    write(tmp_path, "canon/objects/twins.md",
          "---\nid: obj.mann_and_umirn\ntype: object\nname: Mann and Umirn\n---\nbody\n")
    canon = load_canon(cd)
    prose = "The pairing honoured *Mann* and *Umirn*, as *Mann* demands.\n"
    assert ex.unclaimed_terms(prose, [], canon) == []


# --- prompt contract: embedded record schemas + explicit completeness ------------------

def test_contract_embeds_real_record_field_names():
    # Field-tested Extractor failure: every notation entry guessed {symbol, meaning}
    # where the schema says {symbol, denotes}. The contract now carries the field
    # roster introspected from the models, so a rename can't leave the prompt lying.
    assert "a `notation:` entry has: symbol, denotes, notes (optional)" \
        in ex.PLAN_TEMPLATE_CONTRACT
    assert "a `relations:` entry has: type, target, note (optional)" \
        in ex.PLAN_TEMPLATE_CONTRACT
    # and a literal block-style example the model can imitate
    assert "denotes: incomparability of two elements of a poset" \
        in ex.PLAN_TEMPLATE_CONTRACT


def test_contract_demands_explicit_proof_completeness():
    # The advisory note in check is the backstop for hand-written plans; the Extractor
    # itself is instructed to always state completeness, so the note stops firing on
    # every gate run for every generated proof finding.
    assert "complete | sketch | gap" in ex.PLAN_TEMPLATE_CONTRACT
    assert "`completeness:` EXPLICITLY" in ex.PLAN_TEMPLATE_CONTRACT


def test_record_fields_tracks_the_schema():
    from schemas.base import Notation
    assert ex._record_fields(Notation) == "symbol, denotes, notes (optional)"


# --- check re-validates proposed entities (triage hand-edits included) ------------------

FINDING_BAD_NOTATION = """
### Example Term
```yaml
action: create
type: definition
id: def.example_term
evidence: ["The Base Bound gives the Sharp Bound at once."]
entity:
  name: Example term
  notation:
    - {symbol: parallel-sign, meaning: incomparability}
decision: pending
```
"""


def test_check_plans_fails_wrong_notation_field_names(tmp_path, capsys):
    # The triage gate is hand-editing; a plan whose proposed entities no longer
    # validate (here: a Notation entry with `meaning` instead of `denotes`) must FAIL
    # extract-check with the per-finding schema error, not surface at apply time.
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH, FINDING_BAD_NOTATION)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1
    out = capsys.readouterr().out
    assert "def.example_term" in out
    assert "fails the schema" in out


def test_check_plans_paths_narrows_to_given_plans(tmp_path):
    # `paths` is how the plan CLI gates the one plan it just wrote, sharing this exact
    # code path — so generation-time and gate-time validation cannot drift apart.
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    good = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)
    assert ex.check_plans(root=tmp_path, canon=canon, paths=[good]) == 0
    bad = good.parent / "bad.md"
    bad.write_text(good.read_text().replace("action: create", "action: conjure"))
    assert ex.check_plans(root=tmp_path, canon=canon, paths=[bad]) == 1
    # the bad sibling is invisible when paths points only at the good plan
    assert ex.check_plans(root=tmp_path, canon=canon, paths=[good]) == 0


def test_create_collision_message_names_the_remedy(tmp_path, capsys):
    # A plan overtaken by another apply fails correctly, but the failure must SAY the
    # remedy (regenerate with --force, or park via archive-plan) — the field-tested
    # alternative was a hand git mv discovered by spelunking.
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    collided = FINDING_CREATE.replace("id: thm.sharp_bound", "id: thm.base_bound")
    write_plan(tmp_path, collided, FINDING_MATCH)
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1
    out = capsys.readouterr().out
    assert "plan --force" in out
    assert "make archive-plan" in out


# --- plan restyle: block YAML, edit-safe during triage ----------------------------------

FLOW_STYLE_FINDING = (
    "### Example Term\n"
    "```yaml\n"
    "action: create\n"
    "type: definition\n"
    "id: def.example_term\n"
    'evidence: ["The Base Bound gives the Sharp Bound at once."]\n'
    'entity: {name: Example term, notation: [{symbol: "$x \\\\parallel y$", '
    'denotes: "incomparability"}]}\n'
    'anchors: ["Sharp Bound"]\n'
    "decision: pending\n"
    "```\n"
)


def test_make_plan_restyles_findings_to_block_yaml(tmp_path):
    """Plans are a hand-editing surface during triage: finding blocks are re-emitted as
    block-style YAML with plain scalars, so a LaTeX edit needs no double-quote escaping
    (`\\leq`, not `\\\\leq` — the field-tested way three findings broke)."""
    cd = base_world(tmp_path)
    canon = load_canon(cd)
    runner = lambda s, p: "## Summary\nx\n\n## Findings\n" + FLOW_STYLE_FINDING
    dest = ex.make_plan("raw/chapter.md", runner=runner, canon=canon, root=tmp_path,
                        plans_dir=tmp_path / "extraction" / "plans")
    text = dest.read_text()
    assert "entity: {" not in text                       # no flow mappings to hand-edit
    assert "symbol: $x \\parallel y$" in text            # plain scalar, single backslash
    assert '"$x \\\\parallel y$"' not in text            # the escaped form is gone
    # a pure restyle: parsed values are byte-identical to what the model proposed
    plan = ex.parse_plan(dest)
    assert plan.findings[0].entity["notation"][0]["symbol"] == "$x \\parallel y$"


def test_restyle_findings_leaves_broken_blocks_alone():
    body = "```yaml\naction: [unclosed\n```\n"
    out, restyled = ex._restyle_findings(body)
    assert out == body and restyled == 0


def test_restyle_findings_emits_literal_blocks_for_multiline():
    body = ('```yaml\n'
            'action: create\n'
            'entity: {statement: "Let $X$ be a poset.\\nThen $\\\\dim X \\\\le n$."}\n'
            'decision: pending\n'
            '```\n')
    out, restyled = ex._restyle_findings(body)
    assert restyled == 1
    assert "statement: |-" in out                        # literal block, LaTeX readable
    assert "\\dim X \\le n" in out


# --- plan lifecycle: archive + restamp ---------------------------------------------------

def test_archive_plan_moves_pending_plan_byte_identical(tmp_path):
    base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)
    original = plan.read_text()
    archived_dir = tmp_path / "extraction" / "archived"
    assert ex.archive_plan(plan, root=tmp_path, archived_dir=archived_dir) == 0
    assert not plan.exists()
    # a pure move, no status stamp: un-parking is the symmetric move back
    assert (archived_dir / "chapter.md").read_text() == original


def test_archive_plan_refuses_applied_missing_and_clobber(tmp_path):
    base_world(tmp_path)
    archived_dir = tmp_path / "extraction" / "archived"
    assert ex.archive_plan(tmp_path / "extraction" / "plans" / "nope.md",
                           root=tmp_path, archived_dir=archived_dir) == 1
    applied = write_plan(tmp_path, FINDING_CREATE,
                         meta_overrides={"status": "applied"})
    assert ex.archive_plan(applied, root=tmp_path, archived_dir=archived_dir) == 1
    assert applied.exists()                              # refused, not moved
    applied.unlink()
    plan = write_plan(tmp_path, FINDING_CREATE)
    archived_dir.mkdir(parents=True)
    (archived_dir / "chapter.md").write_text("already parked\n")
    assert ex.archive_plan(plan, root=tmp_path, archived_dir=archived_dir) == 1
    assert plan.exists()


def test_restamp_plan_after_deliberate_sync(tmp_path, capsys):
    # The plan-side twin of capsules --restamp: after triage amendments are synced back
    # into the raw source on purpose, restamp re-stamps source_sha so apply accepts the
    # pair again — body and decisions untouched, and the hazard stated loudly.
    base_world(tmp_path)
    plan = write_plan(tmp_path, FINDING_CREATE, FINDING_MATCH)
    raw = tmp_path / "raw" / "chapter.md"
    raw.write_text(RAW_PROSE)                            # byte-identical rewrite first
    assert ex.restamp_plan(plan, root=tmp_path) == 0
    assert "nothing to do" in capsys.readouterr().out    # fresh plan: no-op

    raw.write_text(RAW_PROSE + "\nAn amended closing line.\n")
    assert run_apply(tmp_path, plan) == 1                # stale: apply refuses
    assert ex.restamp_plan(plan, root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "HAZARD" in out and "deliberate, reviewed" in out
    reread = ex.parse_plan(plan)
    assert reread.meta["source_sha"] == ex.source_hash(raw.read_bytes())
    assert [f.decision for f in reread.findings] == ["approved", "approved"]
    assert run_apply(tmp_path, plan) == 0                # the pair is whole again


def test_restamp_plan_refuses_applied_and_missing_raw(tmp_path):
    base_world(tmp_path)
    applied = write_plan(tmp_path, FINDING_CREATE,
                         meta_overrides={"status": "applied"})
    assert ex.restamp_plan(applied, root=tmp_path) == 1
    applied.unlink()
    orphan = write_plan(tmp_path, FINDING_CREATE,
                        meta_overrides={"source": "raw/gone.md"})
    assert ex.restamp_plan(orphan, root=tmp_path) == 1


# --- labeled wiki-links ------------------------------------------------------------------

def test_check_refuses_anchor_containing_link_closer(tmp_path):
    base_world(tmp_path)
    (tmp_path / "raw" / "chapter.md").write_text(
        RAW_PROSE.replace("Sharp Bound at once", "Sharp Bound ]] at once"))
    bad = FINDING_CREATE.replace('anchors: ["Sharp Bound"]',
                                 'anchors: ["Sharp Bound ]] at once"]')
    plan = write_plan(tmp_path, bad, FINDING_MATCH)
    canon = load_canon(tmp_path / "canon")
    assert ex.check_plans(plans_dir=tmp_path / "extraction" / "plans",
                          root=tmp_path, canon=canon) == 1
