"""Base types for the canon: the Entity base class, typed references, relations.

Design notes
------------
* Every entity is one Markdown file: YAML frontmatter (these models) + a prose body.
* References between entities are plain ID strings, but annotated with the entity
  type they must resolve to. `Ref("statement")` means "a string that must be the id
  of an existing statement". `Ref("*")` means "any existing entity". The validator
  reads these annotations to enforce referential integrity (foreign keys).
* Mathematical statements (a definition's content, a theorem's hypotheses and
  conclusion) are LaTeX-for-humans strings in a typed `statement` field, never parsed
  by machinery. The spine checks *structure* (refs, the dependency DAG, status
  shape), not mathematical truth — truth lives in the extraction triage gate and in
  the proofs themselves (see .docs/PLAN.md "Anti-goals").
"""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Typed references (foreign keys) -----------------------------------------
class Ref:
    """Annotation marker: this str field must resolve to an entity of `target_type`.

    Use target_type="*" for a reference to any entity type (existence-only check).
    """

    def __init__(self, target_type: str):
        self.target_type = target_type

    def __repr__(self) -> str:  # nicer error messages
        return f"Ref({self.target_type!r})"


# Convenience aliases used across the concrete schemas.
DefinitionRef = Annotated[str, Ref("definition")]
StatementRef = Annotated[str, Ref("statement")]
ProofRef = Annotated[str, Ref("proof")]
ObjectRef = Annotated[str, Ref("object")]
SourceRef = Annotated[str, Ref("source")]
ComputationRef = Annotated[str, Ref("computation")]
TechniqueRef = Annotated[str, Ref("technique")]
AnyRef = Annotated[str, Ref("*")]


# --- Epistemic status (statements) --------------------------------------------
# Whether a claim is settled, and by whom — deliberately a separate axis from the
# canonicity tier below (see .docs/PLAN.md "Settled decisions" #2): a refuted
# conjecture is still `core` canon, because knowing it is false is load-bearing.
#
#   conjectured  stated, believed plausible, not proved (open questions live here too:
#                "is X true?" is X with status conjectured)
#   proved       proved in the literature; a recorded proof entity is encouraged but
#                only warned about (`proof-unrecorded`) — importing a paper's theorem
#                without transcribing its proof structure is legitimate
#   proved_here  proved within this research project; REQUIRES a proof entity
#                (the dependency graph is the point of the spine)
#   folklore     "known", used freely, but with no citable proof anywhere — the
#                honest tier for community knowledge; visible so it can be audited
#   refuted      false; `refuted_by` must name the counterexample or argument
STATEMENT_STATUSES: tuple[str, ...] = (
    "conjectured", "proved", "proved_here", "folklore", "refuted",
)

# Statement kinds are advisory labels (they affect display and nothing else); the
# validator does not gate on them, so a new kind is a doc change, not a schema change.
STATEMENT_KINDS: tuple[str, ...] = (
    "theorem", "lemma", "proposition", "corollary", "claim", "question",
)


# --- Relations (typed graph edges) -------------------------------------------
class Relation(BaseModel):
    """A directed, typed edge from this entity to another (any type).

    The deliberately loose, generic edge: `type` is any string, so an author can add
    a one-off link without touching the schema. A type *declared* in
    schemas/relations.py additionally gets domain/range and reciprocity validation.
    Structural dependency (`proves`, `uses`, `verifies`, ...) is NOT a relation — it
    lives in typed fields, with the reverse direction derived
    (derived/indices/dependencies.json); see tools/validate.py's field-as-relation
    check.
    """

    model_config = ConfigDict(extra="forbid")

    type: str                          # e.g. equivalent_to, specializes, analogue_of
    target: AnyRef
    note: Optional[str] = None


# --- Notation ------------------------------------------------------------------
class Notation(BaseModel):
    """One symbol/convention this entity is denoted by, inside its `notation` list.

    An embedded record, deliberately NOT an entity type (a precedent inherited from
    the ancestor spine's lexicon machinery): a symbol is not an independently-referenced thing —
    nothing needs to point *at* one by id — so it lives inside the definition or
    object it denotes. The build compiles every notation into
    derived/indices/notation.json, and tools.assemble_context injects that index into
    every bundle, so agents inherit conventions instead of coining new ones (the
    anti-notation-drift mechanism, .docs/PLAN.md pain point 1). Promote to an entity
    type only if free-floating conventions unattached to any definition recur.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str                        # as written, LaTeX allowed: "\\langle a,b\\rangle"
    denotes: str                       # what it means, one line
    notes: Optional[str] = None        # scope, precedence, collision warnings, ...


# --- Canonicity tier -----------------------------------------------------------
# How authoritative an entity is, independent of whether it structurally resolves —
# and independent of a statement's epistemic `status` (see STATEMENT_STATUSES).
# `core`/`primary` are ordinary in-force canon; `apocryphal` is exploratory material
# that has landed on a real branch without being authoritative (a speculative
# development line, a working-session sandbox); `deprecated` is a real entity that has
# been superseded (an old formulation kept for the record) and should be excluded from
# an agent's *default* working set, without being deleted or folded into a
# `merged_into` tombstone (that redirect means "this id no longer denotes a distinct
# entity" — a deprecated entity still does).
#
# The order below is the single source of truth for both the enum and its numeric
# weight (core=0 ... deprecated=3, least to most "don't hand this to an agent by
# default"); nothing else should hardcode this list or its ordering.
CANON_STATES: tuple[str, ...] = ("core", "primary", "apocryphal", "deprecated")
CANON_WEIGHT: dict[str, int] = {state: weight for weight, state in enumerate(CANON_STATES)}


def canon_weight(entity: "Entity") -> int:
    """The numeric canonicity weight of an entity's `canon_state` (lower = more authoritative).

    A thin named wrapper around `CANON_WEIGHT[entity.canon_state]` so callers (the
    canonicity index, the assembler's `--max-canon-weight` filter) don't reach into the
    mapping directly and so the mapping stays the one place this ordering is defined.
    """
    return CANON_WEIGHT[entity.canon_state]


# --- Intentional conflicts (documented, on-purpose disagreements) ------------
class IntentionalConflict(BaseModel):
    """A declared, deliberate disagreement between this entity and another.

    The literature genuinely disagrees with itself: two papers define the same term
    incompatibly, two sources state a result with different constants, a survey's
    version of a theorem drops a hypothesis. The deterministic validator catches a
    narrow class of structural conflict (relation-reciprocity breaks); this is the
    documented-exemption channel for disagreements that are tracked on purpose rather
    than resolved. Declaring one doesn't silence the check it concerns —
    tools/validate.py downgrades the specific ERROR to a visible WARNING
    (`intentional-conflict-waived`) once *either* side of the pair names the other;
    see `is_declared_conflict`.
    """

    model_config = ConfigDict(extra="forbid")

    target: AnyRef                      # the other entity this knowingly conflicts with
    nature: str                         # free text: what disagrees, and why it's tracked
    resolution_status: Literal[
        "competing_definition", "convention_clash", "competing_account", "unresolved"
    ] = "unresolved"


# --- Entity base --------------------------------------------------------------
class Entity(BaseModel):
    """Fields common to every entity type."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    summary: Optional[str] = None      # optional author one-liner (not generated)
    # Canonicity tier (see CANON_STATES above).
    canon_state: str = "core"
    relations: list[Relation] = Field(default_factory=list)
    # Documented, on-purpose disagreements with other entities — see IntentionalConflict.
    intentional_conflicts: list[IntentionalConflict] = Field(default_factory=list)
    # Tombstone marker: when set, this entity was merged into another and the file exists
    # only to keep its (immutable) id reserved and redirect to its successor. tools/merge
    # sets it; the validator treats a tombstone as retired (no capsule, refs must retarget).
    merged_into: Optional[AnyRef] = None

    # Set on every concrete subclass; lets the loader check prefix<->type and route.
    type_name: ClassVar[str] = ""
    id_prefix: ClassVar[str] = ""

    @field_validator("canon_state")
    @classmethod
    def _validate_canon_state(cls, v: str) -> str:
        if v not in CANON_WEIGHT:
            raise ValueError(
                f"canon_state must be one of {list(CANON_STATES)}, got {v!r}"
            )
        return v
