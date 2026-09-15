"""Tests for the entity lifecycle tools (tools.lifecycle): new, merge, delete.

Each builds a tiny canon in a temp dir and drives the tool functions directly (the CLI
is a thin wrapper). They pin the safety contract: new refuses invalid/duplicate ids,
delete refuses to strand references, and merge retargets every inbound edge and leaves a
loadable tombstone that the validator then treats as retired.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import lifecycle as lc
from tools.loader import load_canon
from tools.validate import Report, check_references, check_relations


def write(tmp: Path, relpath: str, body: str) -> None:
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def base_canon(tmp: Path) -> Path:
    write(tmp, "sources/paper.md", "---\nid: src.paper\ntype: source\nname: P\n---\nbody\n")
    write(tmp, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A
        stated_in: src.paper
        relations: [{type: equivalent_to, target: thm.b}]
        ---
        A and [[thm.b]] say the same thing.
    """)
    write(tmp, "statements/b.md", """
        ---
        id: thm.b
        type: statement
        name: B
        relations: [{type: equivalent_to, target: thm.a}]
        ---
        B body.
    """)
    return tmp / "canon"


# --- new ---------------------------------------------------------------------

def test_new_creates_loadable_entity(tmp_path):
    cd = base_canon(tmp_path)
    assert lc.new_entity("statement", "c", "C", sets={"stated_in": "src.paper"},
                         canon_dir=cd) == 0
    canon = load_canon(cd)
    assert "thm.c" in canon.entities
    assert not canon.errors


def test_new_rejects_duplicate_id(tmp_path):
    cd = base_canon(tmp_path)
    assert lc.new_entity("statement", "a", "Another A", canon_dir=cd) == 1


def test_new_rejects_schema_invalid(tmp_path):
    cd = base_canon(tmp_path)
    # a bad status fails the schema; scaffolding must refuse rather than write junk
    assert lc.new_entity("statement", "boom", "Boom", sets={"status": "true"},
                         canon_dir=cd) == 1
    assert not (cd / "statements" / "boom.md").exists()


def test_new_proof_requires_proves(tmp_path):
    # `proves` has no default, so scaffolding a proof without --set proves=... refuses;
    # with it, the file is born valid.
    cd = base_canon(tmp_path)
    assert lc.new_entity("proof", "orphan", "Orphan", canon_dir=cd) == 1
    assert not (cd / "proofs" / "orphan.md").exists()
    assert lc.new_entity("proof", "a_v1", "Proof of A", sets={"proves": "thm.a"},
                         canon_dir=cd) == 0
    canon = load_canon(cd)
    assert canon.entities["prf.a_v1"].proves == "thm.a"


# --- inbound references ------------------------------------------------------

def test_inbound_references_finds_field_relation_and_body(tmp_path):
    cd = base_canon(tmp_path)
    canon = load_canon(cd)
    wheres = {h.where for h in lc.inbound_references(canon, "thm.b")}
    assert "relations[equivalent_to]" in wheres   # a's relation
    assert "body" in wheres                        # a's prose [[thm.b]]


def test_inbound_references_finds_typed_dependency_fields(tmp_path):
    cd = base_canon(tmp_path)
    lc.new_entity("proof", "a_v1", "Proof of A",
                  sets={"proves": "thm.a"}, canon_dir=cd)
    canon = load_canon(cd)
    wheres = {h.where for h in lc.inbound_references(canon, "thm.a")}
    assert "proves" in wheres


# --- delete ------------------------------------------------------------------

def test_delete_refuses_when_referenced(tmp_path):
    cd = base_canon(tmp_path)
    canon = load_canon(cd)
    assert lc.delete_entity("thm.b", canon=canon, canon_dir=cd) == 1
    assert (cd / "statements" / "b.md").exists()


def test_delete_succeeds_when_unreferenced(tmp_path):
    cd = base_canon(tmp_path)
    lc.new_entity("statement", "lonely", "Lonely", canon_dir=cd)
    canon = load_canon(cd)
    assert lc.delete_entity("thm.lonely", canon=canon, canon_dir=cd) == 0
    assert not (cd / "statements" / "lonely.md").exists()


def test_delete_force_removes_despite_references(tmp_path):
    cd = base_canon(tmp_path)
    canon = load_canon(cd)
    assert lc.delete_entity("thm.b", force=True, canon=canon, canon_dir=cd) == 0
    assert not (cd / "statements" / "b.md").exists()


# --- merge -------------------------------------------------------------------

def test_merge_retargets_refs_and_tombstones(tmp_path):
    # The math-research bread-and-butter: "our lemma is actually the paper's" — fold
    # thm.b into thm.a and every inbound edge must follow.
    cd = base_canon(tmp_path)
    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "thm.b.md").write_text("<!-- capsule for thm.b | source-hash: x -->\nB.\n")

    assert lc.merge_entities("thm.b", "thm.a", canon_dir=cd, capsules_dir=caps) == 0

    # the tombstone loads, is marked merged, and its capsule is gone
    canon = load_canon(cd)
    assert not canon.errors, canon.errors
    assert canon.entities["thm.b"].merged_into == "thm.a"
    assert not (caps / "thm.b.md").exists()

    # a's self-reference (its old equivalent_to -> b, now -> a) was dropped, not dangling
    a = canon.entities["thm.a"]
    assert all(r.target != "thm.a" for r in a.relations)

    # validate is clean: nothing points at the tombstone anymore
    rep = Report()
    check_references(canon, rep)
    check_relations(canon, rep)
    assert not any("points to a merged entity" in e for e in rep.errors), rep.errors


def test_merge_preserves_required_fields_on_tombstone(tmp_path):
    # A Proof tombstone must keep its required `proves` so the tombstone still loads.
    cd = base_canon(tmp_path)
    lc.new_entity("proof", "a_v1", "Proof v1", sets={"proves": "thm.a"}, canon_dir=cd)
    lc.new_entity("proof", "a_v2", "Proof v2", sets={"proves": "thm.a"}, canon_dir=cd)
    assert lc.merge_entities("prf.a_v2", "prf.a_v1", canon_dir=cd,
                             capsules_dir=tmp_path / "caps") == 0
    canon = load_canon(cd)
    assert not canon.errors, canon.errors          # proves carried over -> tombstone loads
    assert canon.entities["prf.a_v2"].merged_into == "prf.a_v1"


def test_merge_retargets_typed_dependency_fields(tmp_path):
    # A proof whose `uses` names the duplicate must end up naming the canonical id.
    cd = base_canon(tmp_path)
    write(tmp_path, "proofs/a_v1.md", """
        ---
        id: prf.a_v1
        type: proof
        name: Proof of A
        proves: thm.a
        uses: [thm.b]
        ---
        body
    """)
    assert lc.merge_entities("thm.b", "thm.a", canon_dir=cd,
                             capsules_dir=tmp_path / "caps") == 0
    canon = load_canon(cd)
    assert canon.entities["prf.a_v1"].uses == ["thm.a"]


def test_reference_to_tombstone_is_flagged(tmp_path):
    cd = base_canon(tmp_path)
    lc.merge_entities("thm.b", "thm.a", canon_dir=cd, capsules_dir=tmp_path / "caps")
    # a fresh entity that references the now-merged thm.b
    write(tmp_path, "statements/d.md", """
        ---
        id: thm.d
        type: statement
        name: D
        relations: [{type: equivalent_to, target: thm.b}]
        ---
        body
    """)
    canon = load_canon(cd)
    rep = Report()
    check_references(canon, rep)
    assert any("points to a merged entity" in e for e in rep.errors), rep.errors


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_merge_retargets_labeled_wiki_links_keeping_labels(tmp_path):
    # extract-apply emits labeled links ([[id|anchor text]]); a merge must retarget
    # the id and leave the human-facing label — the prose — untouched, in canon
    # bodies and expositions alike.
    cd = base_canon(tmp_path)
    write(tmp_path, "statements/c.md", """
        ---
        id: thm.c
        type: statement
        name: C
        ---
        Compare [[thm.b|the second bound, $x \\le y$]] with [[thm.b]].
    """)
    exp = tmp_path / "expositions" / "survey" / "001-x.md"
    exp.parent.mkdir(parents=True)
    exp.write_text("Recall [[thm.b|the *second* bound]] once more.\n")

    assert lc.merge_entities("thm.b", "thm.a", canon_dir=cd,
                             capsules_dir=tmp_path / "caps",
                             expositions_dir=tmp_path / "expositions") == 0
    c_body = (cd / "statements" / "c.md").read_text()
    assert "[[thm.a|the second bound, $x \\le y$]]" in c_body
    assert "[[thm.a]]" in c_body and "thm.b" not in c_body
    assert exp.read_text() == "Recall [[thm.a|the *second* bound]] once more.\n"
