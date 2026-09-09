"""Tests for the data spine. Run: python -m pytest -q   (or: python tests/test_spine.py)

Each test writes a tiny canon into a temp dir, loads it, runs the checks, and asserts
the right error (or clean pass) results. This is the regression net for the invariants
the whole system relies on.
"""
from __future__ import annotations

import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic import ValidationError

from schemas.base import CANON_STATES, CANON_WEIGHT, STATEMENT_STATUSES, canon_weight
from schemas.entities import Statement
from schemas.relations import registry_inconsistencies
from tools.assemble_context import assemble
from tools.build import build_canonicity, build_dependencies, build_notation
from tools.loader import load_canon
from tools.validate import (
    Report,
    check_dependencies,
    check_field_shadowing,
    check_intentional_conflicts,
    check_references,
    check_relations,
    check_statements,
    is_declared_conflict,
)


def write(tmp: Path, relpath: str, body: str) -> None:
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def run_checks(tmp: Path) -> tuple[Report, object]:
    canon = load_canon(tmp / "canon")
    rep = Report()
    for path, msg in canon.errors:
        rep.error(f"{path}: {msg}")
    check_references(canon, rep)
    check_relations(canon, rep)
    check_field_shadowing(canon, rep)
    check_statements(canon, rep)
    check_dependencies(canon, rep)
    check_intentional_conflicts(canon, rep)
    return rep, canon


GOOD_SRC = """
    ---
    id: src.paper2024
    type: source
    name: Example Paper 2024
    year: 2024
    ---
    body
"""
GOOD_DEF = """
    ---
    id: def.widget
    type: definition
    name: Widget
    statement: |
      A widget over $k$ is a pair $(V, \\phi)$ with $\\phi^2 = \\mathrm{id}_V$.
    notation:
      - {symbol: '$\\mathcal{W}_k$', denotes: the category of widgets over $k$}
    defined_in: src.paper2024
    ---
    body
"""
GOOD_THM = """
    ---
    id: thm.bound
    type: statement
    name: Widget bound
    kind: theorem
    statement: |
      For every widget over $k$, $\\dim V \\le 2n$.
    invokes: [def.widget]
    status: proved_here
    ---
    body
"""
GOOD_PRF = """
    ---
    id: prf.bound_v1
    type: proof
    name: Proof of the widget bound
    proves: thm.bound
    uses: [def.widget]
    strategy: eigenspace decomposition
    ---
    body
"""


def write_good_canon(tmp_path) -> None:
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "statements/bound.md", GOOD_THM)
    write(tmp_path, "proofs/bound_v1.md", GOOD_PRF)


def test_clean_passes(tmp_path):
    write_good_canon(tmp_path)
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors


def test_dangling_reference(tmp_path):
    # def.widget references src.paper2024 which does not exist
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    rep, _ = run_checks(tmp_path)
    assert any("does not exist" in e for e in rep.errors), rep.errors


def test_type_mismatched_reference(tmp_path):
    # stated_in must point at a source, not a definition
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "statements/bad.md", """
        ---
        id: thm.bad
        type: statement
        name: Bad
        statement: x
        stated_in: def.widget
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any("expected a source" in e for e in rep.errors), rep.errors


def test_proof_proves_must_target_statement(tmp_path):
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "proofs/bad.md", """
        ---
        id: prf.bad
        type: proof
        name: Bad
        proves: def.widget
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any("expected a statement" in e for e in rep.errors), rep.errors


def test_id_prefix_must_match_type(tmp_path):
    # a statement given a def.* id
    write(tmp_path, "statements/bad.md", """
        ---
        id: def.bad
        type: statement
        name: Bad
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any("prefix" in e for e in rep.errors), rep.errors


def test_schema_rejects_unknown_field(tmp_path):
    write(tmp_path, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A
        nonsense_field: 7
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any("nonsense_field" in e for e in rep.errors), rep.errors


def test_proof_requires_proves(tmp_path):
    # `proves` is the schema's one required ref: a proof of nothing fails at load.
    write(tmp_path, "proofs/floating.md", """
        ---
        id: prf.floating
        type: proof
        name: Floating
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any("proves" in e for e in rep.errors), rep.errors


# --- epistemic status: values and shape -----------------------------------------

@pytest.mark.parametrize("status", STATEMENT_STATUSES)
def test_each_declared_status_validates(status):
    s = Statement(id="thm.a", type="statement", name="A", status=status)
    assert s.status == status


def test_bad_status_rejected():
    with pytest.raises(ValidationError):
        Statement(id="thm.a", type="statement", name="A", status="true")


def test_refuted_without_refuter_errors(tmp_path):
    write(tmp_path, "statements/dead.md", """
        ---
        id: thm.dead
        type: statement
        name: Dead conjecture
        statement: x
        status: refuted
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any(f.code == "refuted-without-refuter" and f.level == "error"
               for f in rep.findings), [f.code for f in rep.findings]


def test_proved_here_without_proof_errors(tmp_path):
    # The project claims a proof it hasn't recorded: the dependency graph is lying by
    # omission, so this is the hard half of the status-shape check.
    write(tmp_path, "statements/claim.md", """
        ---
        id: thm.claim
        type: statement
        name: Claim
        statement: x
        status: proved_here
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any(f.code == "status-unproved" and f.level == "error"
               for f in rep.findings), [f.code for f in rep.findings]


def test_proved_literature_without_proof_only_warns(tmp_path):
    # Importing a paper's theorem without transcribing its proof structure is
    # legitimate; the missing dependency structure is visible but non-blocking.
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "statements/imported.md", """
        ---
        id: thm.imported
        type: statement
        name: Imported
        statement: x
        status: proved
        stated_in: src.paper2024
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors
    assert any(f.code == "proof-unrecorded" and f.level == "warning"
               for f in rep.findings), [f.code for f in rep.findings]


def test_status_lags_complete_proof_warns(tmp_path):
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "statements/lagging.md", """
        ---
        id: thm.lagging
        type: statement
        name: Lagging
        statement: x
        status: conjectured
        ---
        body
    """)
    write(tmp_path, "proofs/lagging_v1.md", """
        ---
        id: prf.lagging_v1
        type: proof
        name: Proof
        proves: thm.lagging
        completeness: complete
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors
    assert any(f.code == "status-lags-proof" for f in rep.findings), \
        [f.code for f in rep.findings]


def test_statement_stub_warns(tmp_path):
    # A definition/statement without its `statement` text is a stub: visible, never
    # blocking (make new scaffolds exactly this shape).
    write(tmp_path, "definitions/stub.md", """
        ---
        id: def.stub
        type: definition
        name: Stub
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors
    assert any(f.code == "statement-missing" and f.id == "def.stub"
               for f in rep.findings), [f.code for f in rep.findings]


# --- the dependency DAG -----------------------------------------------------------

def _statement(tmp_path, slug: str, *, status: str = "conjectured",
               extra: str = "") -> None:
    write(tmp_path, f"statements/{slug}.md", f"""
        ---
        id: thm.{slug}
        type: statement
        name: {slug}
        statement: x
        status: {status}
        {extra}
        ---
        body
    """)


def _proof(tmp_path, slug: str, proves: str, uses: list[str],
           extra: str = "") -> None:
    uses_str = "[" + ", ".join(uses) + "]"
    write(tmp_path, f"proofs/{slug}.md", f"""
        ---
        id: prf.{slug}
        type: proof
        name: {slug}
        proves: {proves}
        uses: {uses_str}
        {extra}
        ---
        body
    """)


def test_dependency_cycle_detected(tmp_path):
    # A's proof uses B, B's proof uses A: a circular proof, however many files apart.
    _statement(tmp_path, "a", status="proved_here")
    _statement(tmp_path, "b", status="proved_here")
    _proof(tmp_path, "a_v1", "thm.a", ["thm.b"])
    _proof(tmp_path, "b_v1", "thm.b", ["thm.a"])
    rep, _ = run_checks(tmp_path)
    cycles = [f for f in rep.findings if f.code == "dependency-cycle"]
    assert cycles and cycles[0].level == "error", [f.code for f in rep.findings]
    assert "circular proof" in cycles[0].message


def test_dependency_cycle_reported_once_per_ring(tmp_path):
    _statement(tmp_path, "a", status="proved_here")
    _statement(tmp_path, "b", status="proved_here")
    _proof(tmp_path, "a_v1", "thm.a", ["thm.b"])
    _proof(tmp_path, "b_v1", "thm.b", ["thm.a"])
    rep, _ = run_checks(tmp_path)
    assert sum(1 for f in rep.findings if f.code == "dependency-cycle") == 1


def test_acyclic_dependency_chain_passes(tmp_path):
    _statement(tmp_path, "a", status="proved_here")
    _statement(tmp_path, "b", status="proved_here")
    _proof(tmp_path, "a_v1", "thm.a", [])
    _proof(tmp_path, "b_v1", "thm.b", ["thm.a"])
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors


def test_refuted_dependency_errors(tmp_path):
    _statement(tmp_path, "dead", status="refuted",
               extra="refuted_by: thm.killer")
    _statement(tmp_path, "killer", status="proved_here")
    _proof(tmp_path, "killer_v1", "thm.killer", [])
    _statement(tmp_path, "victim", status="proved_here")
    _proof(tmp_path, "victim_v1", "thm.victim", ["thm.dead"])
    rep, _ = run_checks(tmp_path)
    refuted = [f for f in rep.findings if f.code == "refuted-dependency"]
    assert refuted and refuted[0].level == "error", [f.code for f in rep.findings]
    assert refuted[0].id == "prf.victim_v1"


def test_conditional_result_warns_unproved_dependency(tmp_path):
    # A proved_here statement whose proof uses a conjecture is actually conditional:
    # a WARNING (conditional results are legitimate), never an ERROR.
    _statement(tmp_path, "hypothesis", status="conjectured")
    _statement(tmp_path, "conditional", status="proved_here")
    _proof(tmp_path, "conditional_v1", "thm.conditional", ["thm.hypothesis"])
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors
    cond = [f for f in rep.findings if f.code == "unproved-dependency"]
    assert cond and cond[0].id == "thm.conditional", [f.code for f in rep.findings]


def test_conjectured_statement_using_conjecture_does_not_warn(tmp_path):
    # A proof *sketch* toward a conjecture may lean on other conjectures freely — the
    # conditionality warning is only for statements claiming proved status.
    _statement(tmp_path, "hypothesis", status="conjectured")
    _statement(tmp_path, "target", status="conjectured")
    _proof(tmp_path, "target_v1", "thm.target", ["thm.hypothesis"],
           extra="completeness: sketch")
    rep, _ = run_checks(tmp_path)
    assert not any(f.code == "unproved-dependency" for f in rep.findings), \
        [f.code for f in rep.findings]


def test_computation_cycle_detected(tmp_path):
    write(tmp_path, "computations/a.md", """
        ---
        id: comp.a
        type: computation
        name: A
        depends_on: [comp.b]
        ---
        body
    """)
    write(tmp_path, "computations/b.md", """
        ---
        id: comp.b
        type: computation
        name: B
        depends_on: [comp.a]
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any(f.code == "computation-cycle" for f in rep.findings), \
        [f.code for f in rep.findings]


def test_definitions_in_uses_create_no_cycle(tmp_path):
    # A proof using a definition is the normal case, not an edge in the statement DAG.
    write_good_canon(tmp_path)
    rep, _ = run_checks(tmp_path)
    assert not any(f.code == "dependency-cycle" for f in rep.findings)


# --- field-as-relation guard ------------------------------------------------------

@pytest.mark.parametrize("rtype", ["proves", "uses", "used_by", "verifies",
                                   "depends_on", "instance_of", "invokes",
                                   "invoked_by"])
def test_dependency_relation_types_rejected(tmp_path, rtype):
    _statement(tmp_path, "a")
    _statement(tmp_path, "b", extra=f"relations: [{{type: {rtype}, target: thm.a}}]")
    rep, _ = run_checks(tmp_path)
    shadowed = [f for f in rep.findings if f.code == "field-as-relation"]
    assert shadowed and shadowed[0].level == "error", [f.code for f in rep.findings]
    assert shadowed[0].id == "thm.b"


# --- declared relations: registry + reciprocity ----------------------------------

def test_relation_registry_is_self_consistent():
    # Every declared relation type's inverse must be declared, point back, and mirror
    # its domain/range. A half-declared pair fails here instead of mis-validating canon.
    assert registry_inconsistencies() == []


def _two_statements(tmp_path, a_rels: str, b_rels: str) -> None:
    _statement(tmp_path, "a", extra=a_rels)
    _statement(tmp_path, "b", extra=b_rels)


def test_reciprocated_symmetric_relation_passes(tmp_path):
    _two_statements(
        tmp_path,
        "relations: [{type: equivalent_to, target: thm.b}]",
        "relations: [{type: equivalent_to, target: thm.a}]",
    )
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors


def test_non_reciprocated_relation_detected(tmp_path):
    # A declares equivalence with B, but B says nothing back — the classic asymmetric
    # edge this registry exists to catch.
    _two_statements(
        tmp_path,
        "relations: [{type: equivalent_to, target: thm.b}]",
        "",
    )
    rep, _ = run_checks(tmp_path)
    assert any("not reciprocated" in e for e in rep.errors), rep.errors


def test_asymmetric_relation_requires_correct_inverse(tmp_path):
    # specializes' inverse is generalized_by; equivalent_to back does not satisfy it.
    _two_statements(
        tmp_path,
        "relations: [{type: specializes, target: thm.b}]",
        "relations: [{type: equivalent_to, target: thm.a}]",
    )
    rep, _ = run_checks(tmp_path)
    assert any("not reciprocated" in e and "generalized_by" in e for e in rep.errors), \
        rep.errors


def test_relation_domain_and_range_checked(tmp_path):
    # counterexample_to runs object -> statement; a statement declaring it violates the
    # domain, and an object pointing it at a definition violates the range.
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    _statement(tmp_path, "a",
               extra="relations: [{type: counterexample_to, target: def.widget}]")
    rep, _ = run_checks(tmp_path)
    assert any(f.code == "relation-domain" for f in rep.findings), \
        [f.code for f in rep.findings]
    assert any(f.code == "relation-range" for f in rep.findings), \
        [f.code for f in rep.findings]


def test_undeclared_relation_type_is_not_reciprocity_checked(tmp_path):
    # An undeclared type stays the loose escape hatch: existence-only, no reciprocity.
    _two_statements(
        tmp_path,
        "relations: [{type: inspired_by, target: thm.b}]",
        "",
    )
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors


def test_findings_carry_machine_readable_code_and_id(tmp_path):
    # The JSON gate keys off Finding.code/id, not prose. A dangling reference must
    # surface as a typed finding naming the offending entity and field.
    write(tmp_path, "definitions/widget.md", GOOD_DEF)  # references missing src.*
    rep, _ = run_checks(tmp_path)
    dangling = [f for f in rep.findings if f.code == "dangling-reference"]
    assert dangling, [f.code for f in rep.findings]
    assert dangling[0].id == "def.widget" and dangling[0].field == "defined_in"
    assert dangling[0].level == "error"


# --- canonicity tier (canon_state / canon_weight) ---------------------------------

def test_canon_state_defaults_to_core(tmp_path):
    write_good_canon(tmp_path)
    canon = load_canon(tmp_path / "canon")
    assert canon.entities["thm.bound"].canon_state == "core"


@pytest.mark.parametrize("state", CANON_STATES)
def test_each_declared_canon_state_validates(state):
    s = Statement(id="thm.a", type="statement", name="A", canon_state=state)
    assert s.canon_state == state


def test_bad_canon_state_rejected():
    with pytest.raises(ValidationError):
        Statement(id="thm.a", type="statement", name="A", canon_state="speculative")


def test_canon_weight_helper_orders_core_to_deprecated():
    # core=0 .. deprecated=3, and the helper must agree with CANON_WEIGHT (the one
    # source of truth) rather than reimplementing the ordering.
    assert CANON_WEIGHT == {"core": 0, "primary": 1, "apocryphal": 2, "deprecated": 3}
    for state, weight in CANON_WEIGHT.items():
        s = Statement(id="thm.a", type="statement", name="A", canon_state=state)
        assert canon_weight(s) == weight


def test_deprecated_reference_warns_not_errors(tmp_path):
    # def.widget's defined_in points at a deprecated (but still fully existing) source.
    # Unlike a merged_into tombstone this must not error — the target resolves fine —
    # but it should surface as an advisory, machine-readable warning.
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "sources/paper2024.md", """
        ---
        id: src.paper2024
        type: source
        name: Superseded preprint
        canon_state: deprecated
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors  # still no errors: the reference resolves
    warnings = [f for f in rep.findings if f.code == "deprecated-reference"]
    assert warnings, [f.code for f in rep.findings]
    assert warnings[0].id == "def.widget" and warnings[0].field == "defined_in"
    assert warnings[0].level == "warning"


def test_deprecated_referencing_deprecated_is_not_flagged(tmp_path):
    write(tmp_path, "definitions/old.md", """
        ---
        id: def.old
        type: definition
        name: Old formulation
        statement: x
        canon_state: deprecated
        defined_in: src.oldpaper
        ---
        body
    """)
    write(tmp_path, "sources/oldpaper.md", """
        ---
        id: src.oldpaper
        type: source
        name: Old paper
        canon_state: deprecated
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert not any(f.code == "deprecated-reference" for f in rep.findings), rep.findings


def test_canonicity_index_content(tmp_path):
    write(tmp_path, "statements/a.md", """
        ---
        id: thm.a
        type: statement
        name: A
        statement: x
        ---
        body
    """)
    write(tmp_path, "statements/b.md", """
        ---
        id: thm.b
        type: statement
        name: B
        statement: x
        canon_state: apocryphal
        ---
        body
    """)
    canon = load_canon(tmp_path / "canon")
    index = build_canonicity(canon)
    assert index == {
        "thm.a": {"state": "core", "weight": 0},
        "thm.b": {"state": "apocryphal", "weight": 2},
    }
    assert list(index) == sorted(index)


# --- the assembler: canon-weight filter + notation injection ----------------------

def _assemble_fixture(tmp_path) -> Path:
    # thm.bound (core) is proved by prf.bound_v1 (apocryphal here), so the proof is a
    # hop-1 neighbour — the shape the --max-canon-weight filter targets.
    write(tmp_path, "sources/paper2024.md", GOOD_SRC)
    write(tmp_path, "definitions/widget.md", GOOD_DEF)
    write(tmp_path, "statements/bound.md", GOOD_THM)
    write(tmp_path, "proofs/bound_v1.md", """
        ---
        id: prf.bound_v1
        type: proof
        name: Sandbox proof
        proves: thm.bound
        canon_state: apocryphal
        ---
        body
    """)
    return tmp_path / "canon"


def test_max_canon_weight_filters_neighbor(tmp_path):
    canon_dir = _assemble_fixture(tmp_path)
    with_neighbor = assemble("thm.bound", k=1, full_neighbors=False, budget=None,
                             canon_dir=canon_dir)
    assert "context: prf.bound_v1" in with_neighbor

    filtered = assemble("thm.bound", k=1, full_neighbors=False, budget=None,
                        max_canon_weight=1, canon_dir=canon_dir)  # apocryphal weight is 2
    assert "context: prf.bound_v1" not in filtered
    assert "TARGET: thm.bound" in filtered  # target is unaffected


def test_max_canon_weight_never_filters_the_target(tmp_path):
    write(tmp_path, "statements/wild.md", """
        ---
        id: thm.wild
        type: statement
        name: Wild idea
        statement: x
        canon_state: apocryphal
        ---
        body
    """)
    bundle = assemble("thm.wild", k=1, full_neighbors=False, budget=None,
                      max_canon_weight=0, canon_dir=tmp_path / "canon")
    assert "TARGET: thm.wild" in bundle
    assert "id: thm.wild" in bundle        # full source still present, not dropped
    assert "max-canon-weight" in bundle    # the required notice


def test_notation_index_injected_into_every_bundle(tmp_path):
    # The anti-notation-drift mechanism: def.widget's symbol must appear in the bundle
    # even when the target (src.paper2024's statement chain aside) is elsewhere in the
    # graph — the index is project-wide, not neighbourhood-scoped.
    write_good_canon(tmp_path)
    bundle = assemble("src.paper2024", k=1, full_neighbors=False, budget=None,
                      canon_dir=tmp_path / "canon")
    assert "notation index" in bundle
    assert "$\\mathcal{W}_k$" in bundle


def test_notation_block_survives_budget_trim(tmp_path):
    # The notation block sits right after the target, so a tight budget drops far
    # neighbours before it.
    write_good_canon(tmp_path)
    bundle = assemble("thm.bound", k=2, full_neighbors=False, budget=250,
                      canon_dir=tmp_path / "canon")
    assert "notation index" in bundle


def test_proof_text_not_in_statement_bundle_by_default(tmp_path):
    # The settled decision: traversals consume statements; a statement's bundle carries
    # its proof's *capsule* (placeholder here), never the proof body, unless
    # --full-neighbors asks for it.
    write_good_canon(tmp_path)
    canon_dir = tmp_path / "canon"
    (tmp_path / "canon" / "proofs" / "bound_v1.md").write_text(
        (tmp_path / "canon" / "proofs" / "bound_v1.md").read_text().replace(
            "body", "THE-FULL-PROOF-TEXT"))
    default_bundle = assemble("thm.bound", k=1, full_neighbors=False, budget=None,
                              canon_dir=canon_dir)
    assert "THE-FULL-PROOF-TEXT" not in default_bundle
    full = assemble("thm.bound", k=1, full_neighbors=True, budget=None,
                    canon_dir=canon_dir)
    assert "THE-FULL-PROOF-TEXT" in full


# --- intentional_conflicts declarations + waiver semantics ------------------------

def test_intentional_conflict_waives_relation_reciprocity(tmp_path):
    # A declares equivalence with B and also declares the disagreement (B's author
    # defines equivalence differently): the reciprocity ERROR downgrades to a visible
    # WARNING instead of failing the gate or vanishing.
    _two_statements(
        tmp_path,
        ("relations: [{type: equivalent_to, target: thm.b}]\n"
         "        intentional_conflicts: [{target: thm.b, "
         'nature: "competing definitions of equivalence", '
         "resolution_status: competing_definition}]"),
        "",
    )
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors
    waived = [f for f in rep.findings if f.code == "intentional-conflict-waived"]
    assert waived and waived[0].level == "warning", [f.code for f in rep.findings]
    assert not any(f.code == "relation-not-reciprocated" for f in rep.findings)


def test_intentional_conflict_dangling_target_errors(tmp_path):
    # intentional_conflicts.target is a typed AnyRef like any other; a target that
    # doesn't exist must fail referential integrity, same as any dangling reference.
    _statement(tmp_path, "a",
               extra='intentional_conflicts: [{target: thm.ghost, nature: "disputed"}]')
    rep, _ = run_checks(tmp_path)
    dangling = [f for f in rep.findings if f.code == "dangling-reference"]
    assert dangling, [f.code for f in rep.findings]
    assert dangling[0].id == "thm.a"
    assert dangling[0].field == "intentional_conflicts.target"


def test_intentional_conflict_one_sided_declaration_warns(tmp_path):
    _statement(tmp_path, "a",
               extra='intentional_conflicts: [{target: thm.b, nature: "convention clash"}]')
    _statement(tmp_path, "b")
    rep, _ = run_checks(tmp_path)
    assert rep.ok(), rep.errors  # a warning only, never fails the gate
    unrecip = [f for f in rep.findings if f.code == "intentional-conflict-unreciprocated"]
    assert unrecip and unrecip[0].level == "warning" and unrecip[0].id == "thm.a"


def test_is_declared_conflict_is_symmetric(tmp_path):
    _statement(tmp_path, "a",
               extra='intentional_conflicts: [{target: thm.b, nature: "convention clash"}]')
    _statement(tmp_path, "b")
    canon = load_canon(tmp_path / "canon")
    assert is_declared_conflict(canon, "thm.a", "thm.b")
    assert is_declared_conflict(canon, "thm.b", "thm.a")
    assert not is_declared_conflict(canon, "thm.a", "thm.nonexistent")


def test_findings_carry_suggestion_for_undeclared_disagreement(tmp_path):
    # The repair payload: an undeclared reciprocity break's Finding carries a
    # ready-to-apply intentional_conflicts snippet, and it survives the asdict()
    # serialization that `validate --json` uses.
    _two_statements(
        tmp_path,
        "relations: [{type: equivalent_to, target: thm.b}]",
        "",
    )
    rep, _ = run_checks(tmp_path)
    broken = [f for f in rep.findings if f.code == "relation-not-reciprocated"]
    assert broken, [f.code for f in rep.findings]
    payload = asdict(broken[0])
    assert payload["suggestion"] is not None
    assert "intentional_conflicts" in payload["suggestion"]
    assert "target: thm.b" in payload["suggestion"]


# --- the dependencies index (both directions, derived) ----------------------------

def _dependencies_fixture(tmp_path):
    write_good_canon(tmp_path)
    write(tmp_path, "computations/cases.md", """
        ---
        id: comp.cases
        type: computation
        name: Case check
        covers: n <= 12
        verifies: [thm.bound]
        ---
        body
    """)
    return load_canon(tmp_path / "canon")


def test_dependencies_index_content(tmp_path):
    canon = _dependencies_fixture(tmp_path)
    index = build_dependencies(canon)
    assert index["proofs"] == {"thm.bound": ["prf.bound_v1"]}
    assert index["uses"] == {"thm.bound": ["def.widget"]}
    assert index["used_by"] == {"def.widget": ["thm.bound"]}
    assert index["verified_by"] == {"thm.bound": ["comp.cases"]}
    # GOOD_THM invokes def.widget; the formulation edge derives its own reverse view,
    # separate from used_by (which only proofs' `uses` feed).
    assert index["invoked_by"] == {"def.widget": ["thm.bound"]}


def test_dependencies_index_omits_empty_and_is_deterministic(tmp_path):
    canon = _dependencies_fixture(tmp_path)
    index = build_dependencies(canon)
    # entities with no entry get no key at all, so "nothing" is distinguishable from
    # "not indexed"
    assert "src.paper2024" not in index["used_by"]
    assert build_dependencies(canon) == index
    for key in ("proofs", "uses", "used_by", "verified_by", "invoked_by"):
        assert list(index[key]) == sorted(index[key])


def test_dangling_invokes_reference(tmp_path):
    write_good_canon(tmp_path)
    write(tmp_path, "statements/orphan.md", """
        ---
        id: thm.orphan
        type: statement
        name: Orphaned claim
        statement: |
          Nothing holds.
        invokes: [def.nonexistent]
        ---
        body
    """)
    rep, _ = run_checks(tmp_path)
    assert any(f.code == "dangling-reference" and f.id == "thm.orphan"
               for f in rep.findings), [f.code for f in rep.findings]


# --- the notation index -----------------------------------------------------------

def test_notation_index_content_and_determinism(tmp_path):
    write_good_canon(tmp_path)
    write(tmp_path, "objects/g.md", """
        ---
        id: obj.g
        type: object
        name: The group G
        instance_of: [def.widget]
        notation:
          - {symbol: $G$, denotes: the running example group, notes: fixed throughout}
        ---
        body
    """)
    canon = load_canon(tmp_path / "canon")
    rows = build_notation(canon)
    assert rows == sorted(rows, key=lambda r: (r["symbol"], r["entity"]))
    symbols = {r["symbol"]: r for r in rows}
    assert symbols["$G$"]["entity"] == "obj.g"
    assert symbols["$G$"]["notes"] == "fixed throughout"
    assert symbols["$\\mathcal{W}_k$"]["entity"] == "def.widget"


def test_tombstone_notation_excluded(tmp_path):
    write_good_canon(tmp_path)
    write(tmp_path, "definitions/old_widget.md", """
        ---
        id: def.old_widget
        type: definition
        name: Old widget
        notation:
          - {symbol: $W$, denotes: retired symbol}
        merged_into: def.widget
        ---
        Merged into [[def.widget]].
    """)
    canon = load_canon(tmp_path / "canon")
    assert not any(r["symbol"] == "$W$" for r in build_notation(canon))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
