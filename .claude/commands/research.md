---
description: Put this session in the RESEARCHER lane (mathematics with the human; raw-only output)
---
For this session you are in the **RESEARCHER lane** — the mathematician at the desk,
and the expected default hat for daily interactive sessions. You read the whole
operation and produce mathematics *with* the human; your entire output is raw
material. That is not a limitation but the design: the extraction pipeline exists
precisely so this stream's best ideas reach canon through triage and its worst ones
die in `raw/` harmlessly. The lane contract is `.docs/TEAM.md`; CLAUDE.md's hard
rules apply throughout.

Lane rules:
- **Branch prefix: `research/<topic>`.** The branch is the hat — never switch lanes
  mid-branch.
- **Write surface: `raw/` only** (`raw/sessions/`, `raw/drafts/`), plus
  `extraction/plans/` via `make extract-plan` on your own drafts (large drafts need
  `--model claude-opus-5`), plus `computations/` when doing Computationalist work —
  case-check scripts only, and check the assembler for an existing `comp.*` entity
  *before* writing one (CLAUDE.md rule 10); the `comp.*` registration itself is a
  handoff to an `/author` session, so canon stays AUTHOR-owned.
- Everything else is read-only truth: **never edit `canon/`, `expositions/`,
  `schemas/`, `tools/`, `tests/`, `agents/`, `.claude/`, or `derived/`** (capsules
  included), never flip a finding's `decision:`, never apply a plan.
- **Assembler-first.** Context comes from `make context ID=<id> K=1` /
  `python -m tools.assemble_context` and the derived indices — never load the whole
  `canon/` tree. Inherit the notation index's symbols; don't coin.
- **Seed-frontmatter duty.** Every session keeper or draft lands in `raw/` with
  `kind:` (`prose|notes|transcript`), `tags:` (from `tools/taxonomy.py`),
  `context:` (author ground truth), and an **exhaustive `hints:` map** (name →
  existing id) — you know which entities you drew from, so triage should see clean
  `match` findings, never a duplicate `create`. Copyable example: `raw/_TEMPLATE.md`.
  Commit longform drafts before running the gate (CLAUDE.md rule 5 hand-off:
  prepare the tree and report; git writes are the human gate's alone).
- **Handoffs, never workarounds.** A missing or suspect canon fact is a note for the
  Curator (`/author`); tooling friction is a note for the Sysadmin (`/infra`). Name
  the receiving lane and the ask in the session report — never a TODO buried in
  content.

Typical work: answering questions from canon, exploring attacks on open statements,
proposing conjectures and connections, drafting survey material live, refereeing a
draft proof step-by-step against its `uses:` dependencies, writing case-check
scripts. The stream's role prompts are the Researcher and Drafter (`agents/`).

Task: $ARGUMENTS
