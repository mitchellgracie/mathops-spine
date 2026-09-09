# MathOps plan (the founding design for a research-mathematics canon-base)

Status: **executed through WP4** (this repo IS the fork the plan describes; the prune,
type system, validator/build retarget, tool + agent retarget, and extraction tuning all
landed at bootstrap). Written 2026-09-09 in the ancestor repo as the blueprint for this
one, and kept below as the founding record — the entity mapping, the settled
decisions, and the reasoning behind them. **Remaining: WP5 (seed canon from the actual
research project) and WP6's living-docs upkeep**; the bootstrap recipe was omitted from
this public copy (see the historical note at the bottom). Some names shifted slightly
in execution: the PR workflow is `pr-validation.yml`, the exposition tree is
`expositions/<collection>/NNN-slug.md` (no `scenes/` nesting), the tags index is
`tags.json`, and the raw buckets are `papers/ sessions/ drafts/`.

Read `CLAUDE.md` (hard rules) and `README.md` (mechanics) first; they are the living
successors to the sections below.

---

## The shape of the problem (what the user actually asked for)

Mathematics research across many agent sessions exhibits exactly the drift the
ancestor spine — a worldbuilding knowledge base of the same shape, from which this
repo was forked — was built to kill, under different names:

1. **Notation drift** — agents in different sessions coin different symbols/conventions
   for the same object. (Ancestor analogue: name/spelling drift across scenes; the
   `Lexeme` lexicon + capsule machinery.)
2. **Reproving known results** — an agent re-derives a lemma that is already proved (or
   already known false), because nothing surfaced it. (Analogue: redundant entities;
   killed by capsules + `assemble_context` + lifecycle `merge`.)
3. **Recreating case-check scripts** — computational verification code gets rewritten
   because prior scripts aren't discoverable as first-class knowledge. (No direct
   ancestor analogue; needs one new entity type, `computation`, riding entirely on
   existing machinery.)
4. **Paper ingestion** — claims/definitions/proofs from the literature need to be
   extracted, human-verified, and integrated with provenance. (Analogue: the extraction
   pipeline, near-verbatim — and the human-triage `decision:` gate matters *more* here,
   because an LLM-transcribed theorem with subtly wrong hypotheses is poison.)

**Scope guard:** this is a knowledge base for *one research project*, not a universe of
mathematics. The existing disciplines enforce that mechanically: no entity file exists
unless the project references it; no entity type or declared relation is added until the
shape recurs (CLAUDE.md rule 3's judgement call); `canon_state` + `--max-canon-weight`
keep speculative material out of default working sets.

## Settled decisions (from the design discussion — not open questions)

1. **Proofs are entities, not theorem bodies.** A theorem is not its proof: once a
   statement's status is `proved`, context sweeps and graph traversals need the
   *statement* and its dependency edges, never the proof text. Proofs get their own
   files, their own `uses:` dependency lists, their own capsules — and the assembler
   only pulls a proof when a task actually asks for one (`--full-neighbors`, or
   targeting the proof id directly). A theorem with two proofs is two `prf.*` files
   pointing at one `thm.*`, each with its own dependency footprint.
2. **Epistemic status is a typed field, not the canonicity tier.** `canon_state` keeps
   its ancestor meaning ("should an agent's default context include this file") —
   `apocryphal` = exploratory branch material, `deprecated` = superseded formulation.
   Whether a *claim is proved* is an orthogonal axis: a refuted conjecture is still
   `core` canon, because knowing it's false is load-bearing. So `Statement.status ∈
   {conjectured, proved, proved_here, refuted, folklore}` is a schema field with its
   own validator checks (WP2).
3. **Fork, don't generalize.** One codebase serving both domains would force every
   schema decision through two masters. The new repo starts as a clone of the ancestor
   (keeping git history for tool archaeology), takes one large "prune" commit (WP0),
   and diverges freely. `SCHEMA_VERSION` restarts at 1 in the fork.
4. **Statements live in frontmatter, discussion in the body.** The LaTeX statement of a
   theorem/definition is the semantic core that capsules, dependency checks, and the
   canon-check LLM pass reason over — so it is a typed `statement:` field (YAML literal
   block scalar, LaTeX inside), not prose buried in the body. The body holds context,
   intuition, relations to the literature, and `status=unintegrated` extraction blocks,
   exactly as in the ancestor.

## What carries over unchanged (the machinery inventory)

These modules are type-agnostic — they read the registry and the `Ref` annotations, and
come along untouched (minor renames aside):

| Module | Why it transfers as-is |
|---|---|
| `tools/loader.py`, `schemas/introspect.py`, `schemas/registry.py` | The type system *pattern*: registry + Ref-annotation walk gives referential integrity to any schema you register. |
| `tools/fmt.py` + `fmt-check` | Canonical frontmatter formatting is domain-blind. One check needed: it must pass LaTeX in literal block scalars through byte-identically (WP1 acceptance). |
| `tools/lifecycle.py` (`new`/`merge`/`delete` + tombstones) | "This lemma is actually Lemma 3.2 of [X]" is `make merge DUP=thm.our-lemma CANON=thm.x-3-2` — inbound refs retarget, the id tombstones. |
| `tools/capsules.py` + freshness hash | Theorem statements are naturally capsule-shaped (hypotheses → conclusion, a few hundred tokens). Semantic-content hashing, `--restamp`, staleness gating: all unchanged. |
| `tools/assemble_context.py` | The anti-"reprove it" weapon: target + k-hop capsule neighborhood. `--max-canon-weight` filters exploratory material. |
| Extraction pipeline (`tools/extract.py`, `raw/`, `extraction/`, `tools/provenance.py`) | Papers land in `raw/` (your own writeups of their content — not PDFs), Extractor proposes findings, **you triage every `decision:`** (the correctness gate), apply materializes entities + reciprocal edges + a wiki-linked writeup. The TitleCase recall net catches named theorems ("Serre Duality") no finding covered. |
| Canonicity tier, `IntentionalConflict` | Conflicts = incompatible definitions/conventions across papers you're deliberately tracking (`competing_account` fits verbatim). |
| Schema migration machinery (`schemas/version.py`, `tools/migrate.py`, `.schema-version`) | The fork's schema evolves on its own track; this is what makes that safe. |
| `tools/orchestrate.py`, `tools/canon_check.py`, `tools/impact.py`, `tools/pr_annotate.py` | The advisory LLM contradiction pass retargets naturally: "does this edited statement contradict its neighbors / the writeups that cite it." |
| `tools/build.py`, `tools/graph.py` (core) | Deterministic derived projections; the domain-specific index builders get swapped, the serialization/drift-gate skeleton stays. |
| The prose-layer tooling + its tree | The prose layer survives with a renamed meaning: `expositions/` — working notes, survey writeups, paper-draft sections — `[[id]]`-linked, indexed into an appearances/mentions index. Still written only by `extract-apply` or deliberate authoring, never a dumping ground. |
| Tests, CI skeleton, Makefile shape | Extend/retarget, don't restructure. |

## What gets deleted (WP0's prune)

All ancestor content (its canon, derived artifacts, raw inbox, extraction plans, and
prose layer); its domain-specific modules — the calendar/date machinery (`WorldDate`
— see WP2: the *DAG machinery* survives, the time axis does not), geography,
etymology/lexicon tooling, POV-voice machinery, divergence tracking; its
domain-specific agent roles and knowledge-gating docs (everything in `.docs/` except
this plan); and its domain-specific relation types and taxonomy entries (replaced in
WP1/WP3). Only the domain-agnostic spine — loader, validator, build, formatter,
lifecycle, capsules, assembler, extraction pipeline, migration machinery,
orchestrator, tests — survived.

---

## The type system (WP1)

### Entity types

| MathOps type | prefix | ancestor type | Key typed fields (beyond `Entity` base) |
|---|---|---|---|
| `definition` | `def` | Concept | `statement` (LaTeX, the definition itself); `notation: list[Notation]` (embedded, see below); `defined_in: Optional[SourceRef]` |
| `statement` | `thm` | Event (it inherits the DAG role) | `kind: theorem\|lemma\|proposition\|corollary\|claim`; `statement` (LaTeX); `status: conjectured\|proved\|proved_here\|refuted\|folklore`; `stated_in: Optional[SourceRef]`; `refuted_by: Optional[AnyRef]` (the counterexample/argument, required when `status=refuted`) |
| `proof` | `prf` | — (new; settled decision 1) | `proves: StatementRef` (required); `uses: list[AnyRef]` (statements/definitions/computations invoked — **the dependency edge set**); `strategy: Optional[str]` (one-line: induction on X, spectral sequence, …); `completeness: complete\|sketch\|gap`; `source: Optional[SourceRef]` (where the proof lives if from the literature) |
| `object` | `obj` | Character | A specific mathematical object of study (*the* group G, *the* moduli space M̄) — as opposed to a defined class. `instance_of: list[DefinitionRef]`; `constructed_in: Optional[AnyRef]` |
| `source` | `src` | Item | Paper/book/preprint: `authors: list[str]`, `year`, `bibkey`, `venue`, `url/doi`. The provenance anchor `stated_in`/`defined_in` point at. |
| `computation` | `comp` | — (new; pain point 3) | `path` (repo-relative location of the code), `language`, `covers` (plain text: exactly which cases/ranges it checks), `conclusion` (what it established), `verifies: list[StatementRef]`, `depends_on: list[ComputationRef]`. **The code itself lives in `computations/` in-repo**; the entity is its knowledge-graph face, so an agent asked to check cases finds the existing script *before* writing one. |
| `technique` | `tech` | Concept / MagicSystem | A named method ("the polynomial method", "descent"): `related: list[TechniqueRef]`. Proofs link to techniques via a declared relation, not a field, until the shape settles. |

Deferred until the shape recurs (the WP4-precedent rule): a `question` type for open
problems that aren't yet statements with a truth value (most "is X true?" questions are
just `thm.*` with `status=conjectured`); an `area`/`subfield` grouping type (tags cover
this initially).

### Notation (pain point 1) — embedded, not an entity

Following the `Lexeme` precedent exactly: a `Notation` embedded model on `definition`
(`symbol`, `denotes`, `convention_notes`), NOT a standalone type — nothing needs to point
*at* a notation by id. The build compiles every notation into
`derived/indices/notation.json`, and **`assemble_context` injects the notation index into
every assembly unconditionally** (it is small and is precisely the thing agents must
inherit rather than reinvent). Promote to an entity type only if free-floating
conventions unattached to any definition actually recur.

### Declared relation types (`schemas/relations.py`'s `_TYPES`, replaced)

All get domain/range + reciprocity validation for free from the existing machinery:

| relation | inverse | domain → range | notes |
|---|---|---|---|
| `equivalent_to` | itself (symmetric) | statement ↔ statement | also definition ↔ definition |
| `specializes` | `generalized_by` | statement → statement | |
| `analogue_of` | itself (symmetric) | `*` ↔ `*` | the cross-area "this is the function-field version of…" edge |
| `counterexample_to` | `refuted_by_example` | object → statement | |
| `instance_of` | `has_instance` | object → definition | *also* a typed field on `object`; keep the field, drop the relation, at WP1 — one mechanism per edge |
| `uses_technique` | `used_by_proof` | proof → technique | |
| `motivated_by` | `motivates` | `*` → `*` | the "why does this exist" edge |

`proves`/`uses` are deliberately **typed fields on `proof`**, not relations: they recur
constantly (rule 3 says promote), and their reverse direction is served by a derived
index (WP2) rather than hand-maintained reciprocal edges — the `participation.json`
precedent (event↔character both ways, derived from one authored side).

## The dependency DAG (WP2 — the repurposed timeline)

The most valuable inherited machinery is `check_timeline`'s DAG validation, retargeted
from chronology to **logical dependency**:

- Edge set: `proof.uses` ∪ `computation.depends_on`, projected onto statements (a
  statement depends on what its proofs use).
- **`dependency-cycle` (ERROR):** no statement may, through any chain of proofs, depend
  on itself. The circular-proof check no human reliably performs.
- **`unproved-dependency` (WARNING):** a `status=proved_here` statement whose proof
  `uses` a `conjectured` statement — it's actually conditional; either downgrade or
  record the conditionality. (Deliberately a warning: conditional results are legitimate,
  the tier just has to be *visible*.)
- **`refuted-dependency` (ERROR):** anything whose proof uses a `refuted` statement.
- **`status-shape` (ERROR):** `proved`/`proved_here` with zero `prf.*` entities pointing
  at it (folklore is the escape hatch, and is itself a visible tier); `refuted` without
  `refuted_by`.
- Derived: `derived/indices/dependencies.json` (both directions + per-statement
  transitive closure depth) replacing `participation.json`; `timeline.json`,
  `adjacency.json`, `etymology.json`, `date_inbox.json`, `scene_tags.json` retire
  (tags index stays, re-keyed to expositions).

`WorldDate` and the calendar machinery are deleted, not adapted — `source.year` is a
plain int; the DAG never needed dates for acyclicity (the undated-events design already
proved that).

## Taxonomy, agents, extraction tuning (WP3–WP4)

- **WP3 — taxonomy + roles.** `MATERIAL_TAGS` becomes the project's topical vocabulary
  (subfields, technique families — authored by the researcher at bootstrap, kept small).
  Agent roles: the ancestor's canon-keeper role → **curator.md** (integration passes
  over `status=unintegrated` blocks, merge/dedup judgement); `extractor.md` retargets to
  paper ingestion (statement-transcription fidelity is its prime directive — transcribe,
  never paraphrase, hypotheses); its continuity editor → **consistency-editor.md**
  (notation drift, restated-theorem detection, status-tier hygiene); `drafter.md` →
  exposition/proof drafting. The ancestor's layer-curator *pattern* (one agent policing
  one layer's invariants) returns later only if a layer needs it.
- **WP4 — extraction tuning.** `raw/` buckets become `raw/papers/` (per-source writeups;
  `kind: notes` — no exposition emitted unless wanted), `raw/sessions/` (transcripts of
  research conversations), `raw/drafts/` (your own prose). `_TEMPLATE.md` rewritten:
  `hints:` maps "Theorem 3.2" → `thm.existing-id`; `context:` carries the researcher's
  ground truth ("we only care about the char-p case"). The claim-check recall net
  already harvests TitleCase terms — named theorems surface for free. New finding
  affordance to verify at WP4: extraction of a `statement` finding should carry the
  transcribed LaTeX so triage reviews the *statement*, not just the name.

## WP list (executed in the NEW repo, in order; each independently useful)

- **WP0 — fork + prune.** Clone the ancestor with history; one commit deleting the
  prune list; CI green on an empty canon (validator: zero entities is clean, not an
  error).
- **WP1 — type system.** Rewrite `schemas/entities.py` + `relations.py` + Ref aliases;
  `registry.py` lists the seven types; `SCHEMA_VERSION = 1`; fmt round-trips LaTeX
  byte-identically (acceptance: a fixture `thm` with aligned envs, `\\`, `%`, unicode
  survives `fmt → fmt-check`). Rewrite `canon/` subdirs (`definitions/ statements/
  proofs/ objects/ sources/ computations/ techniques/`), add `computations/` +
  `expositions/` top-level trees.
- **WP2 — validator + build retarget.** Dependency DAG checks above; notation +
  dependency indices; retire the ancestor's domain indices; assembler injects
  `notation.json`; test suite retargeted (the existing tests are the spec of what must
  keep working).
- **WP3 — taxonomy + agents** (above).
- **WP4 — extraction tuning** (above).
- **WP5 — seed canon.** 2–3 sources, ~10 definitions, ~10 statements with proofs, 1–2
  computations, real notation — *from the actual research project*. This is the WP that
  validates every earlier one; run one full extraction cycle on a real paper as its
  acceptance test.
- **WP6 — CLAUDE.md + founding docs.** The fork's CLAUDE.md (hard rules survive nearly
  verbatim: never hand-edit `derived/`, IDs immutable, cross-refs are IDs, the gate,
  the assembler, lifecycle tools, the extraction pipeline, commit-longform-first);
  an architecture doc explaining the mapping (this doc seeds it).

## Bootstrapping (historical note)

The original bootstrap recipe — create the empty private repo, clone the ancestor
*with* its git history (tool archaeology: `git log`/`blame` on `tools/` stays useful),
detach and re-point the remote, then take WP0's one large prune commit — is omitted
from this public copy: it named private repositories and their internals, and this
template *is* its end result. A fresh instance today starts from the template instead
(GitHub's *Use this template*, or clone-and-push), with an empty canon and a green
gate. One rule from the recipe remains binding: keep a fork and its ancestor entirely
unlinked afterward — no shared-history maintenance, no cross-merges; if a later tool
improvement is wanted on either side, cherry-pick it consciously.

## Anti-goals

- **Not a formalization project.** Statements are LaTeX for humans+LLMs, not Lean/Coq.
  The validator checks *structure* (refs, DAG, status shape), never mathematical truth —
  truth lives in the human triage gate and the proofs themselves. (A `formal_ref` field
  pointing at a Lean artifact is a cheap additive move later, if ever.)
- **Not a universe of math.** No entity without a project-side reason to reference it.
- **No speculative types/relations.** The deferred list stays deferred until real
  content recurs — the discipline that kept the ancestor's schema honest.
