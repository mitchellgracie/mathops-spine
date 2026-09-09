"""Declared relation types: the semantics behind a Relation's free-form ``type`` string.

A Relation (schemas/base.py) is the deliberately loose, generic edge — its ``type`` is
any string, so an author can add a one-off link without touching the schema (CLAUDE.md
rule 3's "generic relations entry, looser, less checked"). This registry adds *optional*
teeth: a relation type listed here gains a domain/range (which entity types it may
connect) and an inverse (the edge that must exist in the other direction). The validator
enforces those only for declared types; an undeclared type stays the loose escape hatch
it always was.

Why bother: an asymmetric ``equivalent_to`` (A says equivalent to B, B is silent) or an
orphaned inverse is the single most common class of structural drift in a hand-edited
canon, and it is nearly free to catch once a relation's shape is declared. Declaring a
type is a promise that it recurs often enough to be worth validating — the same
judgement call as promoting a link to a typed schema field.

What deliberately does NOT live here: structural dependency. ``proves``, ``uses``,
``verifies``, ``depends_on`` and ``instance_of`` are typed fields on their entities
(schemas/entities.py) with the reverse direction *derived*
(derived/indices/dependencies.json) — hand-maintained reciprocal edges for facts that
change on every proof edit would be the drift machine this registry exists to prevent.
tools/validate.py refuses relation-shaped restatements of those fields
(field-as-relation).

To add a relation type: add one RelationType below. A symmetric relation
(``equivalent_to``) has ``inverse`` equal to its own name; an asymmetric one
(``specializes``) names its partner (``generalized_by``), which must also be declared
with the mirrored domain/range. `test_spine.py` asserts the registry is internally
consistent, so a half-declared pair fails the suite rather than silently mis-validating
canon.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelationType:
    name: str
    inverse: str          # relation type that must exist on the target, back to the source
    domain: str = "*"     # entity type the source must be ("*" == any)
    range: str = "*"      # entity type the target must be ("*" == any)

    @property
    def symmetric(self) -> bool:
        return self.inverse == self.name


# The declared set. Intentionally compact and general — project-specific relations
# should be added as they earn their place, not speculatively. Every asymmetric type
# appears twice (once from each side) so reciprocity is checkable in either direction.
_TYPES: list[RelationType] = [
    # Two formulations of the same content. Unrestricted domain/range on purpose:
    # statements are equivalent to statements, definitions to definitions, and
    # occasionally a definition to a statement-shaped characterization — tighten only
    # if real content shows mistargeting.
    RelationType("equivalent_to", inverse="equivalent_to"),
    # One statement as a special case of a stronger one. Statement-to-statement: that
    # is the shape that recurs; a definition specializing a definition can start as an
    # undeclared edge until it earns declaration.
    RelationType("specializes", inverse="generalized_by",
                 domain="statement", range="statement"),
    RelationType("generalized_by", inverse="specializes",
                 domain="statement", range="statement"),
    # The cross-area "this is the function-field version of that" edge. Symmetric and
    # unrestricted: analogy connects anything to anything.
    RelationType("analogue_of", inverse="analogue_of"),
    # An object that kills a statement. The statement side answers with
    # `refuted_by_example`; the statement's own `refuted_by` field still names its one
    # canonical refuter — this relation is for the (possibly several) recorded
    # counterexamples beyond it.
    RelationType("counterexample_to", inverse="refuted_by_example",
                 domain="object", range="statement"),
    RelationType("refuted_by_example", inverse="counterexample_to",
                 domain="statement", range="object"),
    # Proof <-> technique. A relation rather than a field because it is sparse (see
    # schemas/entities.py Technique); declared so reciprocity keeps the technique's
    # "where is this used" view honest.
    RelationType("uses_technique", inverse="used_by_proof",
                 domain="proof", range="technique"),
    RelationType("used_by_proof", inverse="uses_technique",
                 domain="technique", range="proof"),
    # The "why does this exist" edge: a definition motivated by a statement, a
    # conjecture motivated by a computation's data, a technique by a problem.
    RelationType("motivated_by", inverse="motivates"),
    RelationType("motivates", inverse="motivated_by"),
]

BY_NAME: dict[str, RelationType] = {t.name: t for t in _TYPES}


def relation_spec(name: str) -> RelationType | None:
    """The declared semantics for a relation type, or None if it is an undeclared edge."""
    return BY_NAME.get(name)


def registry_inconsistencies() -> list[str]:
    """Return reasons the registry is self-inconsistent (empty == consistent).

    A declared type must name an inverse that is also declared, whose own inverse points
    back, and whose domain/range are the mirror of this one's. Surfaced as data (not an
    import-time assert) so a test can report every problem at once.
    """
    problems: list[str] = []
    for t in _TYPES:
        inv = BY_NAME.get(t.inverse)
        if inv is None:
            problems.append(f"{t.name}: inverse {t.inverse!r} is not declared")
            continue
        if inv.inverse != t.name:
            problems.append(f"{t.name}: inverse {inv.name!r} points back to {inv.inverse!r}")
        if (inv.domain, inv.range) != (t.range, t.domain):
            problems.append(
                f"{t.name}: domain/range {(t.domain, t.range)} not mirrored by "
                f"{inv.name} {(inv.domain, inv.range)}"
            )
    return problems
