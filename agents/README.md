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

- **Curator** ([curator.md](curator.md)) — Guards `canon/` integrity: detects and
  resolves inconsistencies across entities and the dependency graph, enforces
  schema/reference hygiene, folds duplicates into canonical entities (`make merge`),
  and owns the integration pass over `status=unintegrated` blocks. **Must not** write
  exposition prose or touch `expositions/`, change a statement's mathematical content
  or status on its own judgement (it surfaces the gap instead), or hand-edit
  `derived/`.

- **Drafter** ([drafter.md](drafter.md)) — Assembles exposition prose (surveys, proof
  writeups, paper fragments) from canon + an outline. Its draft is **raw material**:
  it lands in `raw/drafts/<slug>.md` with seed frontmatter (an exhaustive `hints:`
  name→id map, since the Drafter knows exactly which entities it drew from), and
  reaches `expositions/` only through the extraction pipeline's human triage. **Must
  not** write into `expositions/` or edit `canon/` (canon is read-only truth — if a
  needed fact is missing it flags the Curator rather than inventing it), contradict
  canon or its notation, touch `derived/`, or push a plan past its human gate (flip a
  `decision:`, apply an untriaged plan).

- **Consistency Editor** ([consistency-editor.md](consistency-editor.md)) —
  Line-edits exposition prose and enforces consistency within/across writeups and
  against canon: statements quoted with hypotheses intact, notation per the index,
  no invocations of refuted results, no from-scratch reproofs of recorded ones.
  **Must not** edit `canon/` (it fixes the prose, or flags a canon discrepancy to the
  Curator — never silently changes canon), rewrite writeups wholesale, or touch
  `derived/`. Its critique-only workspace (`python -m tools.editor
  <exposition-path>`, see README "Editor agent workspace") is where that read-only
  boundary gets mechanical, not just prompted: output goes to an advisory, ungated
  `derived/editor-notes/<slug>--<timestamp>.md`, never back into `expositions/`, and
  `tools.orchestrate.run_no_edit_guard` double-checks and auto-restores
  `canon/`/`expositions/` after any dispatched task from this role.

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

The orchestrator (`tools/orchestrate.py`) dispatches these roles as a task DAG; they
also remain plain prompts a human or a single Claude Code session can adopt one at a
time.
