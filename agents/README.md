# Agent role prompts

Each file here is a focused system prompt for one specialized role in the
knowledge-base pipeline. They are deliberately short and stable: they act as
**cacheable prefixes**, so consistent wording matters more than exhaustive detail.
Every role shares the same operating protocol — input via
`python -m tools.assemble_context <id> --k 1` (escalate to `--k 2` / `--full-neighbors`
only with stated justification), one task per branch, minimal file-scoped diffs, and
"done" only when `make check` is green (regenerating the affected capsule with
`make build-capsules --only <id>` after any canon edit). One more boundary is shared:
**generated prose is raw input**. A writeup, proof draft, or set of notes an agent
writes — whatever role or session produced it — enters the spine through `raw/` and
the extraction pipeline (raw → `extract-plan` → human triage of every `decision:` →
`extract-apply` → canon updates + the emitted exposition → the gate), never straight
into `expositions/`. The canon-curating role (the Curator) still edits its `canon/`
files in place — that is typed-fact curation under PR review, a different lane from
net-new prose. What differs is scope and the boundaries below.

The roles are grouped by **stream** — the three write-surface lanes of
`.docs/TEAM.md` (the lane contract; read it for the ownership table, the jump rules,
and the enforcement map). A role's stream says which trees its diffs may ever touch;
the per-role boundaries below narrow further.

## AUTHOR stream — owns the canon, never the code

- **Curator** ([curator.md](curator.md)) — Guards `canon/` integrity: detects and
  resolves inconsistencies across entities and the dependency graph, enforces
  schema/reference hygiene, folds duplicates into canonical entities (`make merge`),
  and owns the integration pass over `status=unintegrated` blocks. **Must not** write
  exposition prose or touch `expositions/`, change a statement's mathematical content
  or status on its own judgement (it surfaces the gap instead), or hand-edit
  `derived/`.

- **Extractor** ([extractor.md](extractor.md)) — Ingests raw material: reads one
  document from `raw/` and emits a human-triaged **extraction plan** into
  `extraction/plans/` — findings that map the text onto entity types
  (`create`/`match`/`update`/`skip`) with evidence quotes, closest canon candidates,
  full proposed frontmatter/bodies (statements transcribed, never paraphrased), and
  the anchor phrases used to wiki-link the emitted exposition. **Must not** edit
  `canon/`, `expositions/`, or `derived/` (an approved plan is applied by the
  deterministic `tools/extract.py`, never by the agent), invent mathematics the
  material doesn't evidence, or flip a finding's `decision` itself. See README
  "Extraction pipeline".

- **Consistency Editor** ([consistency-editor.md](consistency-editor.md)) —
  Critiques exposition prose for consistency within/across writeups and against
  canon: statements quoted with hypotheses intact, notation per the index, no
  invocations of refuted results, no from-scratch reproofs of recorded ones. Its
  workspace is **critique-only by machinery**: `python -m tools.editor
  <exposition-path>` routes output to advisory
  `derived/editor-notes/<slug>--<timestamp>.md` (see README "Editor agent
  workspace"), never back into `expositions/` — that tree is written only by
  `make extract-apply` — and `tools.orchestrate.run_no_edit_guard` double-checks and
  auto-restores `canon/`/`expositions/` after any dispatched task from this role.
  **Must not** edit `canon/` (it flags a canon discrepancy to the Curator — never
  silently changes canon), rewrite writeups wholesale, or touch `derived/` outside
  its notes directory.

## RESEARCHER stream — owns nothing, synthesizes everything

- **Drafter** ([drafter.md](drafter.md)) — Assembles exposition prose (surveys, proof
  writeups, paper fragments) from canon + an outline; the Researcher's
  non-interactive counterpart. Its draft is **raw material**: it lands in
  `raw/drafts/<slug>.md` with seed frontmatter (an exhaustive `hints:` name→id map,
  since the Drafter knows exactly which entities it drew from), and reaches
  `expositions/` only through the extraction pipeline's human triage. **Must not**
  write into `expositions/` or edit `canon/` (canon is read-only truth — if a needed
  fact is missing it flags the Curator rather than inventing it), contradict canon or
  its notation, touch `derived/`, or push a plan past its human gate (flip a
  `decision:`, apply an untriaged plan).

- **Researcher** ([researcher.md](researcher.md)) — The stream's principal: answers
  questions from canon, explores attacks on open statements, proposes conjectures and
  connections, drafts survey material. Entire write surface `raw/` (plus
  `extraction/plans/` via its own `make extract-plan` runs); every keeper carries
  seed frontmatter with an exhaustive `hints:` map. **Must not** edit `canon/`,
  `expositions/`, `derived/`, or the machinery — a missing fact is a handoff to the
  Curator, tooling friction a handoff to the Sysadmin. Registered with
  `edits_canon=False`, so the orchestrator's no-edit guard mechanically reverts (and
  quarantines) any write outside that surface.

- **Referee** ([referee.md](referee.md)) — Adversarial proof-walker: checks a draft
  argument step-by-step against the canonical hypotheses of the results it invokes —
  the mathematical-level complement to the validator's structural DAG checks.
  Read-only by design; its critique is a findings note under `raw/sessions/`, never
  an edit, and it certifies nothing ("no gap found" is its strongest verdict). Also
  rides the no-edit guard via `edits_canon=False`.

## INFRA stream

No role prompts yet: the Sysadmin, Toolsmith, and Gatekeeper profiles live in the
interactive `/infra` hat (`.claude/commands/infra.md`) and `.docs/TEAM.md`; an
`agents/` prompt appears only if INFRA work ever needs orchestrated dispatch.

The orchestrator (`tools/orchestrate.py`) dispatches these roles as a task DAG; they
also remain plain prompts a human or a single Claude Code session can adopt one at a
time.
