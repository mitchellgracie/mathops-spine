# Project memory for Claude Code

This repo is a mathematics research data spine — MathOps: **canon** (typed facts under
`canon/` — definitions, statements, proofs, objects, sources, computations,
techniques) is the normalized, authoritative source of truth. Everything else —
expositions, the dependency graph, the notation index, capsules — is a *derived
projection*, regenerated from canon, never hand-authored. The founding design doc
(entity mapping, settled decisions, the WP plan this spine was built from) is
`.docs/PLAN.md`; day-to-day mechanics are in `README.md`. Read both once at the start
of a new session if unsure.

This is a knowledge base for ONE research project, not a universe of mathematics: no
entity exists without a project-side reason to reference it. The spine checks
*structure* (references, the dependency DAG, status shape), never mathematical truth —
truth lives in the human triage gate and in the proofs themselves.

## Hard rules

1. **Never hand-edit anything under `derived/`.** It is compiled output. If it looks
   wrong, the bug is in `canon/` or in `tools/build.py` — fix the source, then
   `make build` to regenerate.
2. **Every entity is one Markdown file** under `canon/<type-plural>/`, with YAML
   frontmatter (structured, schema-validated) + a prose body. IDs are `<prefix>.<slug>`
   and are immutable — never rename an `id`, only `name`. Prefixes: `def thm prf obj
   src comp tech` (definition / statement / proof / object / source / computation /
   technique). **Proofs are entities**: a theorem is not its proof — the statement
   carries the claim and its epistemic `status`; each proof is its own `prf.*` file
   with `proves:` and the `uses:` dependency list, so traversals consume statements
   and pull proof text only on demand.
3. **Cross-references are always IDs**, never display names, and always validated
   against `schemas/entities.py`. Structural dependency (`proves`, `uses`,
   `verifies`, `depends_on`, `instance_of`, `invokes` — the last being what a
   statement's/definition's *formulation* is stated in terms of, as opposed to what
   a proof's argument needs) lives in typed fields, NEVER in
   `relations:` — the reverse direction is derived (`derived/indices/dependencies.json`)
   and the validator refuses relation-shaped restatements (`field-as-relation`). For
   anything else, a `relations:` entry whose `type` is *declared* in
   `schemas/relations.py` (e.g. `equivalent_to`, `specializes`, `analogue_of`) is
   validated for domain/range and **reciprocity** — add both directions. Undeclared
   types stay existence-only.
4. **Statements are LaTeX-for-humans in the `statement:` field** (a YAML literal
   block; `make fmt` keeps multi-line strings as readable literal blocks). Transcribe
   faithfully — hypotheses intact, the project's notation (see
   `derived/indices/notation.json`) preferred over a source's variant spelling.
   Epistemic `status` (`conjectured | proved | proved_here | folklore | refuted`) is a
   separate axis from `canon_state` (the canonicity tier): a refuted conjecture is
   still core canon. `proved_here` REQUIRES a recorded proof entity; `refuted`
   requires `refuted_by`; the dependency DAG must stay acyclic (no circular proofs)
   and never build on a refuted statement.
5. **Before considering any canon edit done, format and run the gate:**
   ```
   make fmt      # normalize canon frontmatter (key order, YAML style) — a pure restyle
   make build    # regenerate the deterministic derived/ (graph, indices)
   make check    # = fmt-check + validate + extract-check + test + check-capsules
                 #   (schema/refs/relations/dependency-DAG/status-shape/schema-version,
                 #   pending extraction plans' structure/staleness, the suite, capsule freshness)
   ```
   All must pass clean and any regenerated `derived/` must be committed. This is the
   CI gate — a PR won't merge if it fails. (`make fmt-check` / `make validate` /
   `make extract-check` / `make test` / `make check-capsules` are also runnable
   individually; `make validate-json`
   emits findings as machine-readable JSON with a stable `code` + offending `id`/`file`.)
   **Commit longform work (`expositions/`, `raw/`) *before* running the gate**, then
   amend/follow up if it finds something: parts of the suite exercise git-restore
   guards, and an uncommitted draft is the only kind a misfire can destroy. (The guard
   also quarantines anything it discards to `.orchestrate-salvage/` as a last-resort
   net — but commit-first is the rule, not the net.)
6. **Don't load the whole `canon/` tree into context.** Use the context assembler:
   ```
   python -m tools.assemble_context <entity-id> --k 1 --stats
   ```
   This returns the target entity's full source, the project-wide **notation index**
   (always — inherit symbols, don't coin), and its k-hop neighborhood as terse capsules
   (bounded, usually a few hundred tokens) — not the whole knowledge base. A
   statement's 1-hop neighbours include its proofs' capsules (strategy + dependencies,
   no proof text). Increase `--k` only if the task genuinely needs 2-hop context; add
   `--budget N` to cap tokens; add `--full-neighbors` only when you need a neighbour's
   actual text (e.g. a proof's argument), not just its capsule. Add
   `--max-canon-weight N` to exclude apocryphal/deprecated tiers from a working set.
7. **Capsules in `derived/capsules/` are LLM-written, not deterministic** — the one
   derived artifact `make build` does NOT produce (an LLM can't run in CI). Each embeds
   a hash of its source entity file; `make check-capsules` fails if a capsule is
   missing, stale (source changed since it was written), no-marker, or orphaned. After
   editing a canon file, regenerate its capsule with `make build-capsules --only <id>`
   (runs on the Claude subscription via `claude -p`; add `--backend api` for the metered
   API), or `python -m tools.capsules --bootstrap` to drop a placeholder for a brand-new
   entity. Keep the file location/contract (`derived/capsules/<id>.md`); logic lives in
   `tools/capsules.py`. The freshness hash is taken over the entity's *semantic content*
   (the frontmatter+body projection the capsule is generated from), not raw file bytes, so
   a pure `make fmt` reformat keeps capsules fresh while any change to a value or the prose
   marks them stale. Use `python -m tools.capsules --restamp` only to re-stamp markers
   after a reformat or a hash-basis change — never to silence a real content edit.
   A capsule states everything the source states, faithfully — for a statement that
   means hypotheses and conclusion, LaTeX kept exactly as written.
   Unintegrated extraction blocks in a body (rule 9) are excluded from the hash: an
   extraction append keeps the capsule fresh; *integrating* the block is what stales it.
8. **Don't create, merge, or delete an entity by hand — use the lifecycle tools**
   (`tools/lifecycle.py`, or `make new` / `make merge` / `make delete`). `new` scaffolds a
   schema-valid, id-unique file (a proof additionally needs `--set proves=thm.<slug>`);
   `delete` refuses while anything still references the entity; `merge` retargets every
   inbound reference and leaves the duplicate as a `merged_into` **tombstone** (ids are
   immutable, so the id stays reserved) — the move for "our lemma is actually Theorem
   3.2 of [X]". For a **schema change**: additive fields (with a default) need nothing;
   a breaking change bumps `SCHEMA_VERSION` (`schemas/version.py`) and ships a migration
   in `tools/migrate.py` in the same PR (`make migrate`). The validator refuses when the
   canon on disk (`.schema-version`) and the code's `SCHEMA_VERSION` disagree.
9. **Raw material goes through the extraction pipeline, never straight into canon —
   and *generated* content is raw material.** A writeup, proof draft, or set of notes
   you (or any agent role) write in-session is exactly as raw as a paper writeup a
   human wrote outside the spine: it lands in `raw/` with seed frontmatter and reaches
   `expositions/`/`canon/` only through this pipeline. Never write an exposition file
   into `expositions/` directly — that tree is written by `make extract-apply`, so a
   hand-placed writeup is prose whose implied facts no human ever triaged into canon.
   `make extract-plan FILE=raw/<bucket>/<f>.md` has the Extractor propose a plan into
   `extraction/plans/`; a deterministic claim check appends a pending `skip` stub for
   every emphasized/TitleCase term no finding covers (a recall net that catches named
   theorems — approve the skip or upgrade it to a real finding, never delete it;
   `extract-check` resurfaces deleted terms); a human triages every finding's
   `decision:` — **in mathematics this triage IS the correctness gate: review the
   transcribed statement, not just the name**; `make extract-apply PLAN=...` then
   deterministically materializes only the approved findings (entities, reciprocal
   edges for declared relation types, and a wiki-linked exposition with provenance
   frontmatter) and archives raw → `raw/extracted/`, plan → `extraction/approved/`.
   Schema-less facts about *existing* entities ride `add.notes` on update/match
   findings; apply appends the approved notes to the entity's body as one stamped
   `status=unintegrated` block per entity per apply (`tools/provenance.py`). The block
   flags a still-owed Curator integration pass (rewrite into settled prose, then delete
   the block); the queue is `derived/indices/unintegrated.json`, `make validate` warns
   per entity (`unintegrated-notes`) and hard-errors on marker debris
   (`provenance-marker`). Never flip a `decision:` yourself, never apply an untriaged
   plan, and ignore `raw/extracted/` entirely (a human moves a file back to `raw/` to
   re-review). The inbox is bucketed by kind: `raw/papers/` (paper writeups — your own
   words + citations, never PDFs; kind `notes`), `raw/sessions/` (transcripts),
   `raw/drafts/` (prose bound for an exposition). Raw files carry seed frontmatter
   (copyable example: `raw/_TEMPLATE.md`): `hints:` (name→existing id — "Theorem 3.2"
   → `thm.x`), `context:` (author ground truth, cited as `AUTHOR:` evidence lines),
   `kind: prose|notes|transcript` — notes/transcripts yield findings but no exposition
   — and `tags:` (topical labels from the controlled vocabulary in `tools/taxonomy.py`,
   indexed in `derived/indices/tags.json`). See README "Extraction pipeline".
10. **Computation code lives in `computations/`, its knowledge-graph face in
   `canon/computations/`.** Before writing a case-check script, check the canon for an
   existing `comp.*` entity covering those cases (the assembler surfaces them as
   neighbours of the statements they verify). A new script gets a `comp.*` entity
   recording `path`, `covers`, `conclusion`, and `verifies` — that record is what stops
   the next session from rewriting it.
11. **Git writes are the human maintainer's alone.** Never `git commit`, `git push`,
   `git merge`, `git rebase`, `git cherry-pick`, or `git revert`, and never create or
   merge PRs (via `gh` or GitHub tools), unless the maintainer explicitly asks for
   that specific action in the current conversation (worth enforcing mechanically
   with a PreToolUse permission gate in your Claude settings). Where other rules say
   to commit (e.g. rule 5's commit-longform-first), prepare everything, stop, and
   hand off: report exactly what is ready to commit and why, and leave the working
   tree in that state for the maintainer.

## Useful commands

```
make install                        # pip install -r requirements.txt
make fmt                            # normalize canon frontmatter (fmt-check verifies)
make validate                       # schema + refs + relations + dependency DAG + schema-version
make validate-json                  # same checks, machine-readable JSON (code/id/file)
make build                          # regenerate deterministic derived/ (graph, indices)
make build-capsules                 # (re)generate stale/missing capsules via LLM (opt-in; costs)
make check-capsules                 # verify capsules are present + fresh (no LLM)
make check                          # fmt-check + validate + extract-check + test + check-capsules (the CI gate)
make test                           # run the test suite
make context ID=thm.main_bound K=1  # shortcut for assemble_context
make new TYPE=statement SLUG=foo NAME="Foo"            # scaffold a valid, id-unique entity
make new TYPE=proof SLUG=foo_v1 NAME="Proof of Foo" SET="--set proves=thm.foo"
make merge DUP=thm.x CANON=thm.y                       # fold a duplicate (tombstones it)
make delete ID=thm.x                                   # delete if unreferenced (FORCE=1 to override)
make migrate                                           # apply pending schema migrations to canon
make extract-plan FILE=raw/papers/x.md                 # Extractor proposes an extraction plan (LLM)
make extract-check                                     # structural check of pending plans (no LLM; gate leg)
make extract-apply PLAN=extraction/plans/x.md          # materialize a human-triaged plan (no LLM)
make edit-notes FILE=expositions/survey/001-x.md       # advisory Consistency Editor critique (LLM)
```

## Where things live

```
schemas/     the type system (Pydantic) — base.py (Entity, Ref, Relation, Notation,
             STATEMENT_STATUSES, merged_into, canon_state/canon_weight — the canonicity
             tier), entities.py (the seven types), relations.py (declared relation
             types), introspect.py (reads Ref annotations), registry.py, version.py
canon/       source of truth, one file per entity: definitions/ statements/ proofs/
             objects/ sources/ computations/ techniques/
derived/     generated — graph.json, indices/ (deterministic, incl. notation.json —
             every declared symbol, injected into every assembler bundle — and
             dependencies.json — proofs/uses/used_by/verified_by both ways, derived
             from the typed fields); capsules/ (LLM-written, hash-gated — see rule 7)
tools/       loader.py, validate.py, build.py, graph.py, assemble_context.py, fmt.py
             (canonical formatter; multi-line strings emit as literal blocks so LaTeX
             stays readable), lifecycle.py (new/merge/delete), migrate.py, capsules.py
             (LLM capsule generation + freshness check), expositions.py, extract.py
             (raw-material ingestion — rule 9), taxonomy.py (the material-tag
             vocabulary), provenance.py, impact.py (reverse-mentions impact analysis),
             canon_check.py (advisory LLM contradiction pass), orchestrate.py,
             editor.py, pr_annotate.py, common.py
tests/       the regression net; extend when adding a new invariant or type
agents/      role system prompts (Curator, Drafter, Consistency Editor, Extractor) +
             README — the cacheable prefixes for each role
expositions/ writeup-per-file prose (Layer 2) — surveys, proof writeups, paper
             fragments — [[id]] wiki-links indexed into derived/indices/appearances.json
             and tags into tags.json; written only by extract-apply
computations/ the actual verification code the comp.* entities describe (rule 10)
raw/         inbox for material written outside the spine: papers/ (paper writeups),
             sessions/ (transcripts), drafts/ (prose); raw/extracted/ = applied archive
extraction/  plans/ (pending human triage), approved/ (applied audit trail), archived/
             (superseded plans that were never applied) — see rule 9
.docs/       PLAN.md — the founding design doc (entity mapping, settled decisions,
             the WP list this spine was built from)
.schema-version   the schema version the canon on disk is at (see tools/migrate.py)
```

## What's built vs. what's next

Built and tested: the seven-type schema with proofs as
entities and the epistemic status tier; validation including the dependency DAG (no
circular proofs, no building on refuted statements, status-shape checks); the
deterministic build/derive step with the notation and dependencies indices; the context
assembler with unconditional notation injection; LLM-written capsules with a hash-based
freshness gate; the canonical formatter with LaTeX-preserving literal blocks; the
entity lifecycle tools with tombstones; the schema-migration policy (restarted at v1);
the extraction pipeline retargeted at paper ingestion; the reverse-mentions impact
analysis and the advisory LLM canon-check wired into the PR workflow; the role
orchestrator with its canon-safety and read-only guards; and the agent role prompts.

Not yet done in a fresh instance: **seeding the canon** (2–3 sources, ~10 definitions,
~10 statements with proofs, 1–2 computations, real notation — from your research
project; a good acceptance test is one full extraction cycle on a real paper) and the
project-specific half of the tag vocabulary (`tools/taxonomy.py` — add subfield tags
when real material shows the useful splits). A `formal_ref` field (statement-level Lean anchors) is a
cheap additive move whenever formalization becomes worth it. Build further work
incrementally, keeping each piece independently useful.

## Style

Match the existing code: type hints, dataclasses/Pydantic over ad hoc dicts, small
single-purpose modules in `tools/`, docstrings that explain *why* a design choice was
made (not just what the code does) — see any existing file in `tools/` or `schemas/`
for the expected tone and density.
