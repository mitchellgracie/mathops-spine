"""Concrete entity types. Each adds its own typed fields and reference fields.

To add a new entity type: subclass Entity, set type_name + id_prefix, declare your
fields (using the *Ref aliases for cross-references), and register it in registry.py.

The type set (see .docs/PLAN.md "The type system"):

  definition   a defined term/structure, with its notation
  statement    a theorem/lemma/proposition/corollary/claim/question, with epistemic status
  proof        a proof of one statement — an entity of its own (settled decision:
               theorems are not their proofs; traversals consume statements, and proof
               text is pulled only when a task asks for it)
  object       a specific mathematical object of study (THE group G, THE moduli space)
  source       a paper/book/preprint — the provenance anchor
  computation  the knowledge-graph face of a case-check script living in computations/
  technique    a named method ("descent", "the polynomial method")

Structural dependency lives in typed fields (`proves`, `uses`, `verifies`,
`depends_on`, `instance_of`), never in `relations:` — the fields recur constantly
(promote-to-field rule), their reverse direction is served by
derived/indices/dependencies.json rather than hand-maintained reciprocal edges, and
tools/validate.py refuses relation-shaped restatements of them (field-as-relation).
"""
from __future__ import annotations

from typing import ClassVar, Optional

from pydantic import Field, field_validator

from .base import (
    AnyRef,
    ComputationRef,
    DefinitionRef,
    Entity,
    Notation,
    STATEMENT_STATUSES,
    SourceRef,
    StatementRef,
    TechniqueRef,
)


class Definition(Entity):
    type_name: ClassVar[str] = "definition"
    id_prefix: ClassVar[str] = "def"

    # The definition itself, LaTeX-for-humans (authored as a YAML literal block for
    # multi-line content; tools/fmt.py re-emits multi-line strings as literal blocks so
    # LaTeX stays readable). Optional so `make new` can scaffold a stub, but the
    # validator warns (`statement-missing`) until it is filled: a definition without
    # its content is a placeholder, not canon.
    statement: Optional[str] = None
    # The prior notions this definition is formulated in terms of — what a reader must
    # already hold for the `statement` to parse (a critical pair invokes the poset
    # vocabulary it lives in). Formulation-level and forward-owned, so it exists even
    # where no proof ever will; the reverse view (invoked_by) is derived into
    # derived/indices/dependencies.json, never hand-written.
    invokes: list[AnyRef] = Field(default_factory=list)
    notation: list[Notation] = Field(default_factory=list)
    defined_in: Optional[SourceRef] = None     # where this definition comes from


class Statement(Entity):
    type_name: ClassVar[str] = "statement"
    id_prefix: ClassVar[str] = "thm"

    # Advisory display label; not validated against a closed set (see
    # base.STATEMENT_KINDS) — a new kind is a doc change, not a schema change.
    kind: str = "theorem"
    statement: Optional[str] = None            # hypotheses + conclusion, LaTeX
    # The definitions/objects the statement's FORMULATION is stated in terms of — the
    # notions needed to parse the claim, distinct from what an argument for it needs
    # (that is a proof's `uses`). Forward-owned here because a conjecture with no
    # proof still hangs off the definitions it quantifies over; the reverse view
    # (invoked_by) is derived, never hand-maintained.
    invokes: list[AnyRef] = Field(default_factory=list)
    # Epistemic status (base.STATEMENT_STATUSES) — validated here so a typo'd status
    # fails at load, and shape-checked in tools/validate.py (status-shape: proved_here
    # needs a proof entity; refuted needs refuted_by).
    status: str = "conjectured"
    stated_in: Optional[SourceRef] = None      # where the statement comes from
    # Required when status == "refuted": the counterexample (usually an object or a
    # computation) or the refuting statement/source. AnyRef on purpose — refutations
    # come in every shape.
    refuted_by: Optional[AnyRef] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in STATEMENT_STATUSES:
            raise ValueError(
                f"status must be one of {list(STATEMENT_STATUSES)}, got {v!r}"
            )
        return v


class Proof(Entity):
    type_name: ClassVar[str] = "proof"
    id_prefix: ClassVar[str] = "prf"

    # The one required ref in the schema: a proof of nothing is nothing. A statement
    # with two proofs is two prf.* files pointing at it, each with its own `uses`
    # footprint. The proof text itself is the entity's prose body.
    proves: StatementRef
    # Everything this proof invokes: statements, definitions, computations. THE
    # dependency edge set — tools/validate.py projects it onto statements and requires
    # acyclicity (no circular proofs), and tools/build.py derives both directions into
    # derived/indices/dependencies.json.
    uses: list[AnyRef] = Field(default_factory=list)
    strategy: Optional[str] = None             # one line: "induction on n", "descent", ...
    completeness: str = "complete"             # complete | sketch | gap
    source: Optional[SourceRef] = None         # where the proof lives, if from the literature


class MathObject(Entity):
    type_name: ClassVar[str] = "object"
    id_prefix: ClassVar[str] = "obj"

    # A specific object of study, as opposed to the defined *class* it instantiates —
    # the analogue of a named character: it recurs across statements and computations
    # and accumulates facts.
    instance_of: list[DefinitionRef] = Field(default_factory=list)
    notation: list[Notation] = Field(default_factory=list)
    constructed_in: Optional[AnyRef] = None    # the source/proof/computation that builds it


class Source(Entity):
    type_name: ClassVar[str] = "source"
    id_prefix: ClassVar[str] = "src"

    # A paper/book/preprint. The `name` is the title; `bibkey` is the citation key the
    # project's LaTeX uses, so writeups and canon agree on one handle.
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    bibkey: Optional[str] = None
    venue: Optional[str] = None                # journal / arXiv / book series
    url: Optional[str] = None                  # DOI or arXiv link


class Computation(Entity):
    type_name: ClassVar[str] = "computation"
    id_prefix: ClassVar[str] = "comp"

    # The knowledge-graph face of verification code. The code itself lives in-repo
    # under computations/ (or wherever `path` points); this entity is what makes it
    # discoverable, so an agent asked to check cases finds the existing script before
    # writing a new one (.docs/PLAN.md pain point 3).
    path: Optional[str] = None                 # repo-relative code location
    language: Optional[str] = None             # python | sage | magma | lean | ...
    covers: Optional[str] = None               # exactly which cases/ranges it checks
    conclusion: Optional[str] = None           # what it established, one or two lines
    verifies: list[StatementRef] = Field(default_factory=list)
    depends_on: list[ComputationRef] = Field(default_factory=list)


class Technique(Entity):
    type_name: ClassVar[str] = "technique"
    id_prefix: ClassVar[str] = "tech"

    # A named method. Proofs link to techniques via the declared `uses_technique`
    # relation (schemas/relations.py), not a field — the link is real but sparse, so
    # it hasn't earned a typed field yet (promote if it recurs on most proofs).
    related: list[TechniqueRef] = Field(default_factory=list)
