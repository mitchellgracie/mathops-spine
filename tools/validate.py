"""Consistency gate. Run as: python -m tools.validate

Checks, in order:
  1. Schema validation (done at load time; reported here).
  2. Referential integrity: every typed reference resolves to an existing entity of
     the expected type; relation targets exist (including `intentional_conflicts`
     targets, since IntentionalConflict.target is a typed ref like any other). A
     reference from a non-deprecated entity to a `canon_state: deprecated` one is
     flagged too (warning only — the target still fully exists, it has just been
     superseded; see schemas.base).
  3. Declared-relation semantics: domain/range and reciprocity (schemas/relations.py),
     and the field-as-relation guard (structural dependency must live in typed fields,
     never in `relations:` — see check_field_shadowing).
  4. Filename hygiene: file slug matches id slug (warning only).
  5. Statement shape: a definition/statement without its `statement` text is a stub
     (warning); `refuted` without `refuted_by` is an ERROR; `proved_here` without a
     recorded proof entity is an ERROR (the dependency graph is the point of the
     spine); `proved` without one only warns (`proof-unrecorded` — importing a
     paper's theorem without transcribing its proof structure is legitimate); a
     complete proof of a still-`conjectured` statement warns (`status-lags-proof`).
  6. The dependency DAG: projecting every proof's `uses` onto statements ("S depends
     on T when a proof of S uses T"), the graph must be acyclic — a statement that
     transitively depends on itself is a circular proof, the invariant no human
     reliably tracks by hand. A proof that uses a `refuted` statement is an ERROR; a
     proof of a proved/proved_here statement that uses a `conjectured` one is a
     WARNING (`unproved-dependency` — conditional results are legitimate, the
     conditionality just has to be visible). `computation.depends_on` gets the same
     acyclicity discipline separately.
  7. Exposition references: every [[id]] wiki-link in an expositions/ file resolves to
     an existing entity (Layer 2 has no frontmatter schema, so this link check is the
     whole gate for expositions).
  8. Intentional-conflict declarations: warns when one is one-sided. The declarations
     themselves don't waive anything by existing — waiving happens inline in the
     relation-reciprocity check via `is_declared_conflict`, downgrading that specific
     ERROR to a WARNING (`intentional-conflict-waived`) when the pair names each other.
  9. Unintegrated extraction blocks (tools.provenance): malformed/unpaired markers are
     ERRORs (a typo'd stamp would silently fall out of the unintegrated index);
     well-formed blocks are a WARNING per entity (`unintegrated-notes`) — landed facts
     awaiting integration are a legitimate, committable state, so they must never fail
     the gate, only stay visible until the integration pass deletes them.

Exits non-zero if any ERROR is found. WARNINGs do not fail the build.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import Optional

import networkx as nx

from schemas.introspect import ref_fields
from schemas.registry import model_for_type
from schemas.relations import relation_spec
from schemas.version import SCHEMA_VERSION

from .common import CANON_DIR
from .expositions import Exposition, load_expositions
from .loader import Canon, load_canon
from .migrate import read_canon_version
from .provenance import integrity_problems, parse_blocks


@dataclass
class Finding:
    """One validation result, structured so a machine can act on it.

    `code` is a stable machine-readable slug (the API of the gate — pre-commit hooks,
    editors, and the agent repair loop key off it, not the prose `message`). `id` is the
    offending entity and `file` its path when known; `line` is best-effort (most checks
    are cross-file and have no single line, so it is usually None). `suggestion` is a
    ready-to-apply YAML snippet, populated only for the narrow set of undeclared
    cross-entity disagreements that `intentional_conflicts` can waive (see
    `_conflict_suggestion`). Deterministic; never LLM-written.
    """

    level: str          # "error" | "warning"
    code: str           # stable slug, e.g. "dangling-reference"
    message: str        # human-readable prose (unchanged from the text report)
    id: Optional[str] = None
    field: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    suggestion: Optional[str] = None


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, msg: str, *, code: str = "error", id: str | None = None,
              field: str | None = None, file: str | None = None,
              line: int | None = None, suggestion: str | None = None) -> None:
        self.findings.append(Finding("error", code, msg, id, field, file, line, suggestion))

    def warn(self, msg: str, *, code: str = "warning", id: str | None = None,
             field: str | None = None, file: str | None = None,
             line: int | None = None, suggestion: str | None = None) -> None:
        self.findings.append(Finding("warning", code, msg, id, field, file, line, suggestion))

    # Back-compat string views: callers and tests still read rep.errors / rep.warnings
    # as lists of message strings.
    @property
    def errors(self) -> list[str]:
        return [f.message for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f.message for f in self.findings if f.level == "warning"]

    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def resolve_files(self, canon: Canon) -> None:
        """Fill each finding's `file` from its entity id where not already set."""
        for f in self.findings:
            if f.file is not None or f.id is None or f.id not in canon.paths:
                continue
            p = canon.paths[f.id]
            try:
                f.file = str(p.relative_to(CANON_DIR.parent))
            except ValueError:
                f.file = str(p)


def check_references(canon: Canon, rep: Report) -> None:
    ids = canon.ids()
    for eid, entity in canon.entities.items():
        model = model_for_type(entity.type)
        # typed scalar/list reference fields
        for rf in ref_fields(model):
            value = getattr(entity, rf.field)
            if rf.subfield is not None:
                # A ref nested inside a model field, e.g. IntentionalConflict.target
                # inside `intentional_conflicts: list[IntentionalConflict]`. Each list
                # item is a model instance, not a bare id, so pull the ref off it.
                items = value if rf.is_list else ([value] if value is not None else [])
                for item in items:
                    target = getattr(item, rf.subfield)
                    _check_one_ref(canon, ids, eid, f"{rf.field}.{rf.subfield}", target,
                                  rf.target_type, rep)
                continue
            targets = value if rf.is_list else ([value] if value is not None else [])
            for target in targets:
                _check_one_ref(canon, ids, eid, rf.field, target, rf.target_type, rep)
        # relations (target may be any type)
        for rel in entity.relations:
            _check_one_ref(canon, ids, eid, f"relations[{rel.type}]", rel.target, "*", rep)


def _check_one_ref(canon, ids, src, field, target, expected_type, rep) -> None:
    if target not in ids:
        rep.error(f"{src}: {field} -> {target!r} does not exist (dangling reference)",
                  code="dangling-reference", id=src, field=field)
        return
    entity = canon.entities[target]
    if expected_type != "*":
        actual = entity.type
        if actual != expected_type:
            rep.error(
                f"{src}: {field} -> {target!r} is a {actual}, expected a {expected_type}",
                code="type-mismatch", id=src, field=field,
            )
    # A reference to a tombstone is a redirect left unfollowed: the target was merged
    # away and this pointer should name its successor instead. (`merged_into` itself is
    # the redirect, so a tombstone pointing at a live entity is fine.)
    if entity.merged_into is not None and field != "merged_into":
        rep.error(f"{src}: {field} -> {target!r} points to a merged entity; "
                  f"retarget to {entity.merged_into}",
                  code="merged-reference", id=src, field=field)
    # Unlike a tombstone, a deprecated entity still fully exists and resolves — it has
    # just been superseded in canon (see schemas.base's CANON_STATES). Pointing at one
    # is often intentional (citing the superseded formulation), so this is advisory
    # only. A deprecated entity referencing another deprecated one is not flagged:
    # within the superseded layer, cross-references are expected and not noteworthy.
    if entity.canon_state == "deprecated" and canon.entities[src].canon_state != "deprecated":
        rep.warn(f"{src}: {field} -> {target!r} refers to a deprecated entity",
                 code="deprecated-reference", id=src, field=field)


def _has_relation(entity, rtype: str, target_id: str) -> bool:
    return any(r.type == rtype and r.target == target_id for r in entity.relations)


# --- Intentional conflicts: declare-and-waive for on-purpose disagreements ---
#
# The validator only detects a narrow class of cross-entity disagreement: relation-
# reciprocity breaks. A general "these two statements disagree" engine doesn't exist —
# that's the advisory LLM canon-check's job (tools.canon_check). This is the exemption
# wired into the checks that DO exist: when a disagreement's two entities have declared
# it as intentional (competing definitions in the literature, a convention clash), the
# ERROR downgrades to a visible WARNING instead of silently passing or noisily failing.

def is_declared_conflict(canon: Canon, a_id: str, b_id: str) -> bool:
    """True if either a_id or b_id declares an `intentional_conflicts` entry naming the
    other.

    Deliberately symmetric and one-sided-sufficient — unlike relation reciprocity
    (schemas/relations.py), there is no "both sides must agree" invariant here to
    enforce; the whole point is a lightweight escape hatch, so the moment either author
    flags the pair the check should go quiet. (`check_intentional_conflicts` below
    separately nudges toward reciprocating the *declaration itself*, as a warning, since
    a fuller paper trail is still nice to have.)
    """
    for src, dst in ((a_id, b_id), (b_id, a_id)):
        entity = canon.entities.get(src)
        if entity is None:
            continue
        if any(ic.target == dst for ic in entity.intentional_conflicts):
            return True
    return False


def _conflict_suggestion(a_id: str, b_id: str, nature: str) -> str:
    """A ready-to-paste `intentional_conflicts` entry, for Finding.suggestion.

    Deterministic and template-only — the validator proposes the *shape* of a fix, never
    invents content (no LLM call here). Written from a_id's point of view; the same
    snippet works on b_id's file with target swapped, since either side waives it.
    """
    return (
        "intentional_conflicts:\n"
        f"  - target: {b_id}\n"
        f"    nature: \"{nature}\"\n"
        "    resolution_status: unresolved\n"
    )


def _report_contradiction(rep: Report, canon: Canon, a_id: str, b_id: str, msg: str,
                          error_code: str, *, id: str, field: str | None,
                          nature: str) -> None:
    """Emit one narrow disagreement check: ERROR, or WARNING if waived.

    The single choke point every waivable check goes through, so the declare-and-waive
    behavior and the suggestion payload only need to be right in one place.
    """
    if is_declared_conflict(canon, a_id, b_id):
        rep.warn(f"{msg} (waived: declared intentional conflict between {a_id} and {b_id})",
                 code="intentional-conflict-waived", id=id, field=field)
    else:
        rep.error(msg, code=error_code, id=id, field=field,
                  suggestion=_conflict_suggestion(a_id, b_id, nature))


def check_intentional_conflicts(canon: Canon, rep: Report) -> None:
    """Validate the `intentional_conflicts` declarations themselves.

    Dangling targets are already caught by check_references (IntentionalConflict.target
    is a typed AnyRef like any other reference). What's left: nudge toward reciprocating
    the declaration when only one side names the other. This is a WARNING, not an ERROR
    — mid-edit, an author may legitimately declare one side first, and (unlike a
    Relation) there's no domain invariant being broken by asymmetry, just an incomplete
    paper trail.
    """
    for eid, entity in canon.entities.items():
        for ic in entity.intentional_conflicts:
            target = canon.entities.get(ic.target)
            if target is None:
                continue  # dangling target already reported by check_references
            if not any(other.target == eid for other in target.intentional_conflicts):
                rep.warn(
                    f"{eid}: intentional_conflicts -> {ic.target} is not reciprocated "
                    f"(consider declaring the same conflict on {ic.target})",
                    code="intentional-conflict-unreciprocated", id=eid,
                    field="intentional_conflicts",
                )


def check_relations(canon: Canon, rep: Report) -> None:
    """Enforce the semantics of *declared* relation types (schemas/relations.py).

    Only declared types are checked — an undeclared relation stays the loose, existence-
    only edge it always was. For a declared type this verifies domain (the source entity's
    type), range (the target's type), and reciprocity: the target must carry the inverse
    relation back to the source, so an asymmetric equivalence or an orphaned inverse fails
    the gate — unless the pair has declared the asymmetry as an intentional conflict, in
    which case it downgrades to a WARNING (see _report_contradiction). Dangling targets are
    left to check_references; here a missing target is skipped.
    """
    for eid, entity in canon.entities.items():
        for rel in entity.relations:
            spec = relation_spec(rel.type)
            if spec is None:
                continue
            field = f"relations[{rel.type}]"
            if spec.domain != "*" and entity.type != spec.domain:
                rep.error(f"{eid}: relation {rel.type!r} is only valid from a "
                          f"{spec.domain}, but {eid} is a {entity.type}",
                          code="relation-domain", id=eid, field=field)
            target = canon.entities.get(rel.target)
            if target is None:
                continue  # dangling target already reported by check_references
            if spec.range != "*" and target.type != spec.range:
                rep.error(f"{eid}: relation {rel.type!r} -> {rel.target} must target a "
                          f"{spec.range}, but {rel.target} is a {target.type}",
                          code="relation-range", id=eid, field=field)
            if not _has_relation(target, spec.inverse, eid):
                _report_contradiction(
                    rep, canon, eid, rel.target,
                    f"{eid}: relation {rel.type!r} -> {rel.target} is not reciprocated "
                    f"({rel.target} should declare {spec.inverse!r} -> {eid})",
                    "relation-not-reciprocated", id=eid, field=field,
                    nature=f"{eid} declares {rel.type!r} -> {rel.target} without a "
                           f"reciprocal {spec.inverse!r} back",
                )


# Relation-type spellings that would restate a typed dependency field as a hand-authored
# edge. Deliberately NOT declared in schemas/relations.py: declaring them would bless
# the second copy of the fact; guarding them by name keeps the typed field the single
# representation and derived/indices/dependencies.json the single reverse view. The set
# covers the names an author would plausibly reach for.
_FIELD_RELATION_TYPES: dict[str, str] = {
    "proves": "the proof's `proves:` field",
    "proved_by": "the proof entity's `proves:` field (the statement side is derived)",
    "uses": "the proof's `uses:` list",
    "used_by": "the proof's `uses:` list (the reverse view is derived)",
    "depends_on": "the computation's `depends_on:` list (or a proof's `uses:`)",
    "verifies": "the computation's `verifies:` list",
    "verified_by": "the computation's `verifies:` list (the reverse view is derived)",
    "instance_of": "the object's `instance_of:` list",
    "has_instance": "the object's `instance_of:` list (the reverse view is derived)",
    "invokes": "the statement's/definition's `invokes:` list",
    "invoked_by": "the statement's/definition's `invokes:` list (the reverse view is derived)",
}


def check_field_shadowing(canon: Canon, rep: Report) -> None:
    """Refuse relation-shaped dependency edges: the typed fields are the one source.

    Structural dependency is represented exactly once in canon — the typed field on the
    owning entity — and both directions ship as a derived projection
    (derived/indices/dependencies.json, tools.build.build_dependencies), never as a
    second canon edge. A `relations:` entry spelling the same fact would be an
    undeclared type: existence-only checked, so it would validate silently, flow into
    graph.json, and drift from both the field and the index the moment either changed.
    ERROR rather than WARNING: a dependency relation has no reading that isn't the
    field misplaced, and the finding names the field to use instead.
    """
    for eid, entity in canon.entities.items():
        for rel in entity.relations:
            where = _FIELD_RELATION_TYPES.get(rel.type)
            if where is not None:
                rep.error(
                    f"{eid}: relation {rel.type!r} -> {rel.target} restates a typed "
                    f"dependency field — put the fact in {where}; the reverse view is "
                    f"derived into derived/indices/dependencies.json",
                    code="field-as-relation", id=eid,
                    field=f"relations[{rel.type}]",
                )


def check_statements(canon: Canon, rep: Report) -> None:
    """Statement/definition shape: statement text present, epistemic status coherent.

    Status *values* are already schema-validated at load (schemas.entities.Statement);
    this checks the cross-entity shape the schema can't see:

      * `statement-missing` (WARNING) — a definition or statement whose `statement`
        text is unset is a stub, legitimate mid-authoring but not settled canon.
      * `refuted-without-refuter` (ERROR) — status `refuted` must name `refuted_by`;
        an unexplained "false" is exactly the kind of claim this spine exists to pin.
      * `status-unproved` (ERROR) — `proved_here` with no proof entity pointing at it:
        the project claims a proof it hasn't recorded, so the dependency graph is
        lying by omission. `folklore` is the honest tier for "known, no citable proof".
      * `proof-unrecorded` (WARNING) — `proved` (literature) with no proof entity: the
        theorem is usable, but its dependency structure is invisible to the DAG until
        someone records at least a proof sketch with `uses`.
      * `status-lags-proof` (WARNING) — a `complete` proof exists but the statement
        still says `conjectured`: either promote the status or demote the proof's
        `completeness`.
    """
    proofs_of: dict[str, list[str]] = {}
    for eid, e in canon.entities.items():
        if e.type == "proof" and e.merged_into is None:
            proofs_of.setdefault(e.proves, []).append(eid)

    for eid in sorted(canon.entities):
        e = canon.entities[eid]
        if e.merged_into is not None:
            continue  # tombstones assert nothing
        if e.type in ("definition", "statement") and not (e.statement or "").strip():
            rep.warn(f"{eid}: no `statement` text yet (a {e.type} without its content "
                     f"is a stub)", code="statement-missing", id=eid, field="statement")
        if e.type != "statement":
            continue
        recorded = sorted(proofs_of.get(eid, []))
        if e.status == "refuted" and e.refuted_by is None:
            rep.error(f"{eid}: status is `refuted` but `refuted_by` names no "
                      f"counterexample or refuting argument",
                      code="refuted-without-refuter", id=eid, field="refuted_by")
        if e.status == "proved_here" and not recorded:
            rep.error(f"{eid}: status is `proved_here` but no proof entity proves it — "
                      f"record the proof (prf.*) or use status `folklore`/`conjectured`",
                      code="status-unproved", id=eid, field="status")
        if e.status == "proved" and not recorded:
            rep.warn(f"{eid}: status is `proved` but no proof entity records its "
                     f"dependency structure (at least a sketch with `uses` makes the "
                     f"DAG complete)", code="proof-unrecorded", id=eid, field="status")
        if e.status == "conjectured" and any(
                canon.entities[p].completeness == "complete" for p in recorded):
            rep.warn(f"{eid}: a complete proof exists ({', '.join(recorded)}) but "
                     f"status is still `conjectured` — promote the status or demote "
                     f"the proof's completeness", code="status-lags-proof", id=eid,
                     field="status")


def check_dependencies(canon: Canon, rep: Report) -> None:
    """The dependency DAG: no circular proofs, no building on refuted ground.

    The statement-level dependency graph is the projection of every proof's `uses`
    list: S depends on T when some proof of S uses T (definitions and computations in
    `uses` create no statement-level edge — nothing in this schema lets them depend
    back on statements, so they cannot close a cycle). Acyclicity is the invariant:
    a cycle means the project has, across possibly many files and sessions, proved
    something from itself. Deliberately NOT waivable via `intentional_conflicts` —
    unlike a tracked disagreement, a circular proof has no legitimate on-purpose
    reading.

    Alongside the cycle check, two edge-quality checks ride the same walk:
    `refuted-dependency` (ERROR — a proof invokes a statement known false) and
    `unproved-dependency` (WARNING — a proof backing a proved/proved_here statement
    invokes a `conjectured` one, i.e. the result is actually conditional; make the
    conditionality visible or re-tier the status). `computation.depends_on` gets its
    own acyclicity sweep (`computation-cycle`), separate because those edges live on a
    different field and can never mix into statement cycles.
    """
    statements = {eid for eid, e in canon.entities.items() if e.type == "statement"}
    proofs = {eid: e for eid, e in canon.entities.items()
              if e.type == "proof" and e.merged_into is None}

    g = nx.DiGraph()
    g.add_nodes_from(statements)
    for pid in sorted(proofs):
        p = proofs[pid]
        if p.proves not in statements:
            continue  # dangling `proves` is check_references' finding
        target_status = canon.entities[p.proves].status
        for used in p.uses:
            used_entity = canon.entities.get(used)
            if used_entity is None or used_entity.type != "statement":
                continue
            g.add_edge(used, p.proves, proof=pid)
            if used_entity.status == "refuted":
                rep.error(
                    f"{pid}: uses {used}, which is refuted — a proof cannot build on "
                    f"a statement known to be false",
                    code="refuted-dependency", id=pid, field="uses")
            elif used_entity.status == "conjectured" and \
                    target_status in ("proved", "proved_here"):
                rep.warn(
                    f"{p.proves} is {target_status} but its proof {pid} uses "
                    f"conjectured {used} — the result is conditional; record that "
                    f"visibly or re-tier the status",
                    code="unproved-dependency", id=p.proves, field="status")

    if not nx.is_directed_acyclic_graph(g):
        reported: set[str] = set()
        for cycle in nx.simple_cycles(g):
            key = min(cycle)
            if key in reported:
                continue
            reported.add(key)
            ring = cycle + [cycle[0]]
            rep.error(
                "dependency cycle (circular proof): " + " -> ".join(ring) +
                " — some statement in this ring is proved, transitively, from itself",
                code="dependency-cycle", id=key, field="uses")

    # computation.depends_on: each computation has a list of computation parents; a
    # cycle makes "run the dependencies first" unanswerable. Same walk shape as the
    # statement check, on its own field.
    comps = {eid: e for eid, e in canon.entities.items() if e.type == "computation"}
    cg = nx.DiGraph()
    cg.add_nodes_from(comps)
    for cid in sorted(comps):
        for dep in comps[cid].depends_on:
            if dep in comps:
                cg.add_edge(dep, cid)
    if not nx.is_directed_acyclic_graph(cg):
        reported = set()
        for cycle in nx.simple_cycles(cg):
            key = min(cycle)
            if key in reported:
                continue
            reported.add(key)
            ring = cycle + [cycle[0]]
            rep.error("computation dependency cycle: " + " -> ".join(ring),
                      code="computation-cycle", id=key, field="depends_on")


def check_exposition_references(canon: Canon, rep: Report,
                                expositions: list[Exposition] | None = None) -> None:
    """Every [[id]] in an exposition must resolve to an existing entity.

    Expositions are prose with no schema, so a dangling wiki-link is the one way one
    can be structurally wrong — and the same failure mode as a dangling canon
    reference, so it is reported the same way. `expositions` is injectable for tests;
    None loads the tree.
    """
    ids = canon.ids()
    expositions = load_expositions() if expositions is None else expositions
    for exp in expositions:
        for target in exp.mentions:
            if target not in ids:
                rep.error(f"{exp.path}: [[{target}]] does not exist (dangling reference)",
                          code="exposition-dangling-reference", file=exp.path)


def check_schema_version(rep: Report, *, recorded: int | None = None) -> None:
    """Refuse the gate when canon and code disagree on schema version.

    A behind canon needs `make migrate`; an ahead canon means the checkout's code is too
    old. Either way the entities on disk may not mean what the schema thinks, so this
    fails fast rather than validating against the wrong shape.
    """
    recorded = read_canon_version() if recorded is None else recorded
    if recorded != SCHEMA_VERSION:
        verb = ("run `make migrate`" if recorded < SCHEMA_VERSION
                else "update the code to match canon")
        rep.error(f"schema version mismatch: canon records v{recorded}, code expects "
                  f"v{SCHEMA_VERSION} ({verb})", code="schema-version")


def check_filenames(canon: Canon, rep: Report) -> None:
    for eid, path in canon.paths.items():
        slug = eid.split(".", 1)[1] if "." in eid else eid
        if path.stem != slug:
            rep.warn(f"{eid}: file is {path.name} but id slug is {slug!r} "
                     f"(consider renaming to {slug}.md)",
                     code="filename-slug", id=eid)


def check_unintegrated(canon: Canon, rep: Report) -> None:
    """Audit the unintegrated extraction blocks in entity bodies (tools.provenance).

    Two tiers, deliberately split: marker *debris* (unpaired or typo'd stamps) is an
    ERROR because a malformed block silently vanishes from the unintegrated index —
    the exact quiet-loss failure the stamps exist to prevent; a *well-formed* block is
    a WARNING because "extracted but not yet integrated" is the pipeline's designed
    intermediate state — committable and mergeable — that should stay loudly visible
    without ever blocking the gate.
    """
    for eid in sorted(canon.bodies):
        body = canon.bodies[eid]
        for msg in integrity_problems(body):
            rep.error(f"{eid}: {msg}", code="provenance-marker", id=eid)
        blocks = parse_blocks(body)
        if blocks:
            n_notes = sum(len(b.notes) for b in blocks)
            rep.warn(
                f"{eid}: {len(blocks)} unintegrated extraction block(s) "
                f"({n_notes} note(s)) awaiting integration — see "
                f"derived/indices/unintegrated.json",
                code="unintegrated-notes", id=eid,
            )


def validate(canon: Canon | None = None) -> Report:
    if canon is None:
        canon = load_canon()
    rep = Report()
    check_schema_version(rep)
    for path, msg in canon.errors:
        rep.error(f"{path}: {msg}", code="load-error", file=path)
    # only run cross-entity checks if all files at least loaded into models
    check_references(canon, rep)
    check_relations(canon, rep)
    check_field_shadowing(canon, rep)
    check_statements(canon, rep)
    check_dependencies(canon, rep)
    check_filenames(canon, rep)
    check_exposition_references(canon, rep)
    check_intentional_conflicts(canon, rep)
    check_unintegrated(canon, rep)
    rep.resolve_files(canon)
    return rep


def _emit_json(rep: Report, n_ent: int) -> None:
    """The machine-readable form: the API the pre-commit hooks, editors, and the agent
    repair loop consume. Stable top-level shape; each finding carries its code + id."""
    payload = {
        "ok": rep.ok(),
        "entities": n_ent,
        "counts": {"errors": len(rep.errors), "warnings": len(rep.warnings)},
        "findings": [asdict(f) for f in rep.findings],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    as_json = "--json" in (argv if argv is not None else sys.argv[1:])
    canon = load_canon()
    rep = validate(canon)
    n_ent = len(canon.entities)
    if as_json:
        _emit_json(rep, n_ent)
        return 0 if rep.ok() else 1

    for w in rep.warnings:
        print(f"WARN  {w}")
    for e in rep.errors:
        print(f"ERROR {e}")
    if rep.ok():
        print(f"\nOK — {n_ent} entities, {len(rep.warnings)} warning(s), 0 errors.")
        return 0
    print(f"\nFAILED — {len(rep.errors)} error(s), {len(rep.warnings)} warning(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
