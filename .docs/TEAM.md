# The three-stream team architecture (lanes, hats, guards)

The lane contract for operating a spine instance with many sessions: three
streams that partition the repo's *write* surfaces, worn as hats by interactive
sessions and enforced mechanically by hooks, orchestrator guards, and CI. Read
`.docs/PLAN.md` (the founding design) and `agents/README.md` (the role prompts)
alongside this; CLAUDE.md's hard rules bind throughout.

## The shape of the problem

The spine prevents *content* drift (notation, duplicate theorems, unrecorded
scripts). This architecture prevents **operator drift** — the failure modes of
running many sessions against one repo:

1. **Role bleed.** A session opened for tooling work "quickly fixes" a statement's
   hypothesis in passing, or a content session patches a validator that was
   correctly refusing its edit. A diff that crosses lanes is a diff nobody reviews
   with the right eyes.
2. **Permission creep.** A prose instruction is not a mechanical guarantee — the
   no-edit guard's own docstring says so, and the ancestor spine lost a fully
   drafted writeup to exactly the gap between "the prompt said don't" and "the
   tree shows it did" (`tools/orchestrate.py`, `_salvage_dirty_paths`).
3. **Register mismatch.** Cataloguing mathematics, engineering a validator, and
   attacking a research problem want different dispositions, different context
   bundles, and different definitions of "done".
4. **Unclear handoffs.** "The schema fights me" (author-side) and "this statement
   looks wrong" (infra-side) both need a named place to land.

## Settled principles

1. **Write surfaces partition the repo.** Each stream owns a disjoint set of
   trees; everything else is read-only truth to it. Reads are universal — every
   profile uses the assembler and the derived indices; ownership is only ever
   about *writes*.
2. **A hat is restrictive, never additive.** Wearing a hat means adopting its
   write surface *and its prohibitions*; switching hats swaps surfaces, never
   unions them. One session, one hat at a time.
3. **The branch is the hat.** Branch prefixes `infra/`, `author/`, `research/`
   declare the lane, and every mechanical guard (hook, CI lint) keys off the
   prefix — one stateless source of truth, no marker files. Hats switch at branch
   boundaries, never mid-branch.
4. **The human gate is stream-invariant.** No profile in any stream triages a
   finding's `decision:`, judges mathematical truth, writes git history, or merges
   a PR. The human maintainer is the correctness gate; the streams organize
   everything *around* that gate, and no hat confers it.
5. **Enforcement is mechanical, not prompted.** Every lane boundary gets at least
   one non-prompt guard: the orchestrator's `edits_canon` gate and no-edit guard,
   the PreToolUse hooks, the CI gate, the branch-prefix lint. Prompts set
   disposition; guards set limits.
6. **Generated content is raw material** (CLAUDE.md rule 9, restated as
   stream-invariant): a proof draft, survey, or observation produced under *any*
   hat enters the spine through `raw/` and the extraction pipeline. The RESEARCHER
   stream is not an exception to this rule — it is the rule's primary customer.
7. **Deterministic tool runs are not authorship.** `make fmt`, `make build`,
   `make migrate`, `python -m tools.capsules --restamp` rewrite bytes under
   content trees without authoring content; they are INFRA-safe by construction.
   Authoring canon or prose is never INFRA's, no matter how small the edit.

## The three streams

| Stream | Principal | Hat | Branch prefix | Owns (writes) | Never writes |
|---|---|---|---|---|---|
| **INFRA** | Sysadmin | `/infra` | `infra/` | `schemas/ tools/ tests/ Makefile requirements.txt .github/ .claude/ agents/ .docs/` | `canon/ raw/ expositions/ extraction/`; `derived/capsules/` content (restamp only) |
| **AUTHOR** | Curator | `/author` | `author/` | `canon/` (lifecycle + field edits), `derived/capsules/` (via `make build-capsules`), `extraction/` (plans via tool; apply after human triage), `raw/` | `schemas/ tools/ tests/ agents/ .claude/`; `expositions/` by hand (apply-only) |
| **RESEARCHER** | Researcher | `/research` | `research/` | `raw/`; `extraction/plans/` via `make extract-plan` only; `computations/` (Computationalist work) | everything else — canon and code are both read-only truth |

Two trees belong to no hand: the deterministic `derived/` is written only by
`make build` (run by whichever lane staled it; the gate enforces the commit), and
`expositions/` is written only by `make extract-apply` under the Author lane.

### INFRA — owns the machinery, never the mathematics

Builds the guardrails everyone else runs inside. It treats everything under
`canon/`, `raw/`, `extraction/`, and `expositions/` as **opaque user data**: read
it to test behavior, never author, reword, or reinterpret it, never make a triage
decision. When a task turns out to require content judgement, it writes a handoff
and continues with the machinery half only.

- **Sysadmin** (principal). Owns architecture: schema evolution (`SCHEMA_VERSION`
  + migrations in the same PR), the gate itself, CI, and the role/permission
  machinery (`agents/`, `.claude/`, the hooks, the orchestrator). The meta-owner —
  changes here move every other profile's floor, so it measures twice, prefers a
  migration to a special case, and treats every guard as guilty until tested.
- **Toolsmith.** Day-to-day `tools/` work: new validators and indices, assembler
  and extraction-pipeline features, performance, test coverage. Scoped to
  additive, non-breaking changes; anything that touches `schemas/` semantics or
  the gate's meaning escalates to the Sysadmin.
- **Gatekeeper** (read-only). The diagnostician any stream can safely borrow:
  triages a red gate or CI run from `make validate-json` output, audits capsule
  freshness and the unintegrated queue, runs `tools.impact` and `tools.canon_check`
  reports on PRs. Writes nothing but its diagnosis — its deliverable is "which
  lane owns this failure and why", never a patch.

### AUTHOR — owns the canon, never the code

The cataloguers: mathematicians whose medium is *records* — typed statements,
dependency edges, provenance — not novel arguments. The stream's centre of
gravity is the extraction pipeline and the integrity of `canon/`. It trusts the
machinery as a black box: `make` targets and the assembler, never edits to
`schemas/` or `tools/` — friction with the tooling becomes a handoff note for an
`/infra` session, not a patch.

- **Curator** (principal — `agents/curator.md`). Canon hygiene: reference and
  status-shape integrity, duplicate folding (`make merge`), the integration pass
  over `status=unintegrated` blocks, capsule regeneration after every canon edit.
  Edits records, never claims — a hypothesis, conclusion, or status is the
  researcher's to decide; the Curator surfaces gaps rather than papering over
  them.
- **Extractor** (`agents/extractor.md`). Raw material → extraction plan.
  Transcription fidelity is its prime directive; it proposes, never decides. Its
  entire write surface is `extraction/plans/`.
- **Consistency Editor** (`agents/consistency-editor.md`). The notation-drift and
  restated-theorem police, checking prose against canon. Critique-only by
  machinery: `tools/editor.py` routes output to advisory `derived/editor-notes/`,
  and the no-edit guard reverts anything else.
- **Librarian** (deferred). Sources and notation steward: `src.*` entities,
  bibkeys/DOIs, the embedded `Notation` conventions, and proposals for the
  taxonomy *vocabulary* (the file `tools/taxonomy.py` stays INFRA's; its tag list
  is authored content — resolved by AUTHOR specifying and INFRA committing the
  edit). Fold into the Curator until source volume demands the split.

### RESEARCHER — owns nothing, synthesizes everything

The stream that lives on top: it reads the whole operation — canon via the
assembler, computations, the derived indices, the expositions — and produces
mathematics *with* the human. It writes no canon and no code; its entire output
is raw material with seed frontmatter, which is not a limitation but the design
(principle 6): the pipeline exists precisely so this stream's best ideas reach
canon through triage and its worst ones die in `raw/` harmlessly.

- **Researcher** (principal — `agents/researcher.md`). The mathematician at the
  desk and the expected default hat for daily interactive sessions: answers
  questions from canon, explores attacks on open statements, proposes conjectures
  and connections, drafts survey material live. Session keepers land in
  `raw/sessions/` or `raw/drafts/` with `kind:`, `tags:`, `context:`, and an
  exhaustive `hints:` map. Missing facts get flagged to the Curator; missing
  tooling to the Sysadmin; it never routes around either.
- **Drafter** (`agents/drafter.md`). The Researcher's non-interactive
  counterpart: canon + outline → exposition prose in `raw/drafts/`, closing its
  loop with `make extract-plan` and a stop-for-triage.
- **Referee** (`agents/referee.md`). Adversarial by design: walks a draft proof
  step-by-step against its `uses:` dependencies, hunting the gap, the circular
  step, the unearned "clearly" — the mathematical-level complement to the
  validator's structural DAG checks. Read-only; its critique is a `raw/sessions/`
  note, never an edit. Distinct from the Consistency Editor: the Editor checks
  prose *against canon*; the Referee attacks the argument *itself*.
- **Computationalist.** Believes small cases: writes the case-check script once
  and records it so no session writes it twice. Checks canon for an existing
  `comp.*` entity *before* writing (CLAUDE.md rule 10), writes only under
  `computations/`, and hands the `comp.*` registration (path, covers, conclusion,
  `verifies`) to the Curator so canon stays AUTHOR-owned.

### Content flow between the streams

```
RESEARCHER ──raw/──▶ extract-plan ──▶ HUMAN TRIAGE ──▶ extract-apply ──▶ canon/ + expositions/
    ▲                (Extractor)      (the human)      (Author lane)         │
    │                                                                        │
    └────────────── assemble_context / derived indices (read-only) ◀─────────┘

INFRA owns every arrow and no box's contents.
```

## Hats and the role prompts

Two layers, deliberately kept distinct:

- **`agents/*.md` role prompts** — the cacheable prefixes the orchestrator
  dispatches and a single session can adopt. These stay the sole source of role
  truth — no parallel `.claude/agents/` mirrors until something needs Agent-tool
  dispatch, because two copies of a role prompt is how personalities drift.
- **`.claude/commands/` hats** — session-mode skills that put an *interactive*
  session into a lane: name the write surface, the branch prefix, the handoff
  rule, and load the lane's disposition.

Generic engineering skills (`/code-review`, `/simplify`, `/security-review`) are
INFRA-lane tools: they modify or judge code, so content hats don't run them.

### Jumping lanes (wearing another hat)

The team is a handful of hats worn by a rotating cast, not an org chart — jumping
is expected, and the rules make it cheap and safe rather than forbidden:

1. **Declare the jump.** Park or finish the current branch, cut a new one with the
   target lane's prefix, invoke the target hat. Never switch hats mid-branch —
   the branch *is* the hat (principle 3), and the CI lint holds every PR to it.
2. **Restrictive semantics.** The borrowed hat's write surface replaces yours;
   your home surface goes read-only for the session (principle 2).
3. **Free jumps:** within a stream (Researcher ↔ Drafter, Sysadmin ↔ Toolsmith,
   Curator ↔ Extractor); and *anyone* → Gatekeeper, which is read-only and
   therefore always safe — the standard first move when a gate goes red.
4. **Deliberate jumps** (allowed, named in the session report): cross-stream into
   a *writing* hat, for mechanical, self-contained tasks only. Substantive
   cross-stream work is a **handoff, not a jump**: a note in the session report
   or PR body naming the receiving lane and the ask — never a new tree, never a
   TODO buried in content.
5. **Discouraged in both directions:** INFRA ↔ content hats. The register gap is
   the whole reason the streams exist; each side hands off to the other. The
   exception is principle 7's deterministic runs, which need no jump at all.
6. **Non-wearable:** the human gate. No hat triages, judges truth, or writes git
   history.

## Enforcement map

| Guard | Where | What it holds |
|---|---|---|
| Orchestrator canon gate (`make check` after `edits_canon` tasks) | `tools/orchestrate.py` | a canon-editing role cannot leave canon broken |
| Orchestrator no-edit guard + salvage, per-role watch surfaces | `tools/orchestrate.py` | a proposal-only role's writes outside its declared surface are reverted, quarantined to `.orchestrate-salvage/` |
| Consistency Editor workspace | `tools/editor.py` | critique lands in advisory notes, never in prose |
| CI gate (`make check` on every PR) | `.github/workflows/` | schema/refs/DAG/status/capsule freshness |
| Deterministic apply | `tools/extract.py` | only triaged findings materialize; `expositions/` has no hand-written files |
| Lane guard for interactive sessions | `tools/lanes.py` + `.claude/settings.json` PreToolUse hook | an `/author` session cannot edit `tools/` even by accident; warn-only on `main` and unprefixed branches |
| Branch-prefix ↔ diff-path lint | `tools.lanes --lint` in `pr-validation.yml` | a lane-crossing PR is flagged for splitting (advisory; `--strict` once tuned) |

## Anti-goals

- **No permission escalation via hats.** Jumping lanes never unions surfaces, and
  no sequence of hats reaches triage, mathematical judgement, or git history.
- **Not an org chart.** Profiles are prompts plus guards, worn on demand by
  sessions and the orchestrator; deferred profiles stay deferred until real work
  recurs.
- **No second orchestrator, no second LLM path.** New roles register in the
  existing `ROLES` table and dispatch through the existing runner; the lane guard
  reads the existing branch, not new state. One mechanism per boundary, reused
  everywhere.
