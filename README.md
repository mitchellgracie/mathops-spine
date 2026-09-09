# MathOps — a research-mathematics data spine

The canonical, version-controlled knowledge base for **one research project**. **Canon**
(typed facts: definitions, statements, proofs, objects, sources, computations,
techniques) is normalized and authoritative; expositions, the dependency graph, the
notation index, and capsules are *derived projections* that are regenerated, never
hand-authored. Git is the transaction log; every state change is a reviewable commit.

This repo is a template: instantiate it (GitHub's *Use this template*, or
clone-and-push — not a public fork, so your canon can stay private) and seed it with
your own project's material. The founding design rationale — the entity mapping, the
settled decisions — lives in `.docs/PLAN.md`. The problems the spine exists to kill
are the drift modes of many-session agent-assisted research:

- **Notation drift** — agents coining new symbols for known objects. Killed by
  `notation:` records on definitions/objects, compiled into
  `derived/indices/notation.json` and injected into *every* context bundle.
- **Reproving known results** — killed by capsules + the context assembler (the
  relevant statements arrive as terse, faithful summaries) and by `make merge`
  (folding a rediscovered lemma into the literature's record).
- **Rewritten case-check scripts** — killed by `computation` entities: the code lives
  in `computations/`, its knowledge-graph face in `canon/computations/`, discoverable
  as a neighbour of every statement it `verifies`.
- **Unfaithful imports** — killed by the extraction pipeline's human triage gate:
  every LLM-transcribed statement is reviewed (hypotheses intact) before it lands.

## Quick start

```bash
make install                 # pip install -r requirements.txt
make check                   # the full gate: fmt-check + validate + extract-check + test + check-capsules

# create your first entities
make new TYPE=source    SLUG=serre1956  NAME="GAGA"
make new TYPE=statement SLUG=main_bound NAME="Main bound"
make new TYPE=proof     SLUG=main_bound_v1 NAME="Proof of the main bound" \
     SET="--set proves=thm.main_bound"

# after editing canon
make fmt && make build && make check
python -m tools.capsules --bootstrap          # placeholder capsules for new entities
make build-capsules                            # real LLM capsules (opt-in; costs)

# bounded context for one entity (what an agent session should read)
make context ID=thm.main_bound K=1
```

## The type system

Seven entity types, one Markdown file each under `canon/<plural>/`, YAML frontmatter +
prose body, ids `<prefix>.<slug>` (immutable):

| type | prefix | carries |
|---|---|---|
| `definition` | `def` | the defined content (`statement`, LaTeX) + `invokes` (prior notions it is stated in terms of) + its `notation` |
| `statement` | `thm` | theorem/lemma/…: `kind`, `statement`, `invokes` (the definitions/objects the formulation needs), epistemic `status`, `stated_in`, `refuted_by` |
| `proof` | `prf` | `proves` (required), `uses` (THE argumentative dependency edges), `strategy`, `completeness` |
| `object` | `obj` | a specific object of study: `instance_of`, `notation` |
| `source` | `src` | paper/book: `authors`, `year`, `bibkey`, `venue`, `url` |
| `computation` | `comp` | a script's record: `path`, `language`, `covers`, `conclusion`, `verifies`, `depends_on` |
| `technique` | `tech` | a named method; linked from proofs via `uses_technique` |

**Proofs are entities** (the spine's first settled decision): a theorem is not its
proof. Traversals and context bundles consume statements — hypotheses, conclusion,
status — plus the *capsules* of their proofs (strategy and dependency footprint); the
proof text itself is pulled only by targeting the `prf.*` id or `--full-neighbors`.
A statement with two proofs is two `prf.*` files, each with its own `uses` list.

**Two orthogonal tiers.** `status` is epistemic: `conjectured | proved` (literature)
`| proved_here` (this project — requires a recorded proof) `| folklore | refuted`
(requires `refuted_by`). `canon_state` is canonicity: `core | primary | apocryphal`
(sandbox material) `| deprecated` (superseded formulations) — a refuted conjecture is
still core canon, and `assemble_context --max-canon-weight` filters by tier.

**The dependency DAG.** Every proof's `uses` list projects onto statements ("S depends
on T when a proof of S uses T"), and the validator enforces acyclicity — **no circular
proofs**, however many files and sessions apart — plus `refuted-dependency` (ERROR:
building on a statement known false) and `unproved-dependency` (WARNING: a "proved"
result that is actually conditional on a conjecture). Both directions of the graph are
derived into `derived/indices/dependencies.json`; hand-authored reverse edges are
refused (`field-as-relation`).

**Relations** (`relations:` frontmatter) cover the non-structural links: declared types
(`equivalent_to`, `specializes`/`generalized_by`, `analogue_of`, `counterexample_to`,
`uses_technique`, `motivated_by`) get domain/range + reciprocity validation; undeclared
types are the loose escape hatch. Deliberate disagreements in the literature (two
papers defining a term incompatibly) are *tracked*, not silently resolved:
`intentional_conflicts` declarations downgrade the specific validator error to a
visible warning.

## The gate

```bash
make check     # fmt-check + validate + extract-check + test + check-capsules — what CI runs
```

`make validate` covers: schema + referential integrity (typed refs resolve to the right
types; tombstoned targets flagged), declared-relation semantics, field-as-relation,
statement shape (`statement-missing`, `refuted-without-refuter`, `status-unproved`,
`proof-unrecorded`, `status-lags-proof`), the dependency DAG, exposition wiki-links,
schema-version agreement, and unintegrated-block hygiene. `make validate-json` emits
machine-readable findings (stable `code` + `id`/`file`, with ready-to-paste
`suggestion` payloads where a declare-and-waive fix applies).

`make extract-check` (also a gate leg) structurally validates any pending extraction
plans: proposals must instantiate against the schema, references must resolve, and the
plan must still match its raw source (`source_sha`). It also prints exit-neutral
`note` lines when a create leans on a defaulted field (a proof's `completeness`, a
statement's `status`) so triage rules on the value, not the silence. A deliberately
parked plan belongs in `extraction/archived/`, which the check ignores.

CI additionally byte-diffs the committed `derived/` against a fresh build (the
derived-drift guard) and checks capsule freshness. The PR workflow
(`.github/workflows/pr-validation.yml`) posts a diff-scoped annotation comment
(`tools/pr_annotate.py` + `tools/impact.py`'s reverse-mentions impact section) and — when
`ANTHROPIC_API_KEY` is configured — an advisory LLM canon-check comment
(`tools/canon_check.py`: one bounded call per modified entity, asking whether the change
contradicts its neighbours or the writeups citing it).

## Context assembly (how agents read this repo)

```bash
python -m tools.assemble_context thm.main_bound --k 1 --stats
```

Returns: the target's full source → the **notation index** (always; inherit symbols,
don't coin) → k-hop neighbours as capsules, nearest first, under an optional
`--budget`. Capsules (`derived/capsules/<id>.md`) are LLM-written, faithful, terse
summaries with a semantic-content freshness hash — `make check-capsules` fails when a
capsule's source has changed; `make build-capsules --only <id>` regenerates one.

## Extraction pipeline (papers in, canon out)

```
raw/papers/x.md  --extract-plan-->  extraction/plans/x.md  --human triage-->  --extract-apply-->
   canon creates/updates + reciprocal edges + (for prose drafts) an exposition
   raw -> raw/extracted/ ; plan -> extraction/approved/
```

Raw material is bucketed by what it is: `raw/papers/` (your own writeups of literature
— statements transcribed faithfully, with citations; never PDFs), `raw/sessions/`
(transcripts of working sessions), `raw/drafts/` (prose bound for `expositions/`).
Copy `raw/_TEMPLATE.md` for the seed frontmatter (`hints:`, `context:`, `kind:`,
`tags:`). The Extractor proposes findings (`create`/`match`/`update`/`skip`, each with
evidence quotes and `decision: pending`); a deterministic claim check appends a pending
`skip` stub for every emphasized/TitleCase term no finding covers — the recall net that
surfaces named theorems; a human rules on **every** finding (in mathematics this triage
IS the correctness gate — review the transcribed statement, not just the name); apply
is deterministic and materializes exactly what was approved. Schema-less facts land as
stamped `status=unintegrated` body blocks awaiting the Curator's integration pass
(queue: `derived/indices/unintegrated.json`).

Expositions (`expositions/<collection>/NNN-slug.md`) are the prose layer — surveys,
proof writeups, paper fragments — wiki-linked (`[[thm.main_bound]]`) into canon and
reverse-indexed (`appearances.json`, `tags.json`). The tree is written only by
`extract-apply`; hand-placing a writeup there bypasses the triage that keeps canon and
prose in sync.

## Agents

`agents/` holds the role prompts (cacheable prefixes): **Curator** (canon integrity,
merges, the integration pass), **Drafter** (exposition prose as raw material),
**Consistency Editor** (notation-drift and restated-theorem police; advisory notes via
`make edit-notes`), **Extractor** (paper ingestion). `tools/orchestrate.py` dispatches
them as a task DAG with two mechanical guards: `make check` after every canon-editing
task, and a git-based read-only guard (with salvage quarantine) after every
proposal-only task. One LLM invocation path serves everything
(`tools/capsules.run_llm_{cli,api}`): `claude -p` on a subscription by default, the
metered API opt-in.

## Layout

```
schemas/       the type system (Pydantic)         tools/        the machinery
canon/         THE SOURCE OF TRUTH                tests/        the regression net
derived/       generated projections (committed)  agents/       role prompts
expositions/   prose layer (written by apply)     computations/ verification code
raw/           inbox (papers/ sessions/ drafts/)  extraction/   plans + audit trail
.docs/PLAN.md  the founding design doc            .schema-version
```

## What's next

**Seed your canon**: populate from your research project (sources, definitions,
statements with proofs, computations, real notation), then run one full extraction
cycle on a real paper as the acceptance test. Add your project's subfield tags to
`tools/taxonomy.py` as real material shows the useful splits. See CLAUDE.md "What's
built vs. what's next".
