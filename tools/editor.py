"""The Consistency Editor workspace: LLM-written, advisory, ephemeral critique notes.

    python -m tools.editor <exposition-path>              # write a note
    python -m tools.editor <exposition-path> --patch      # + extract a sibling .patch
    python -m tools.editor <exposition-path> --backend api --model claude-opus-5

``agents/consistency-editor.md`` says the role must never edit ``canon/`` or
``expositions/``; this module gives it somewhere principled to put its output instead —
a structured note under ``derived/editor-notes/`` — plus an optional patch file a human
(or a Drafter task) can choose to apply by hand.

A third artifact class
-----------------------
This repo already has two derived-artifact regimes (see ``tools.build``'s module
docstring): deterministic (``tools.build``, byte-diffed by CI) and LLM-written-but-hash-
gated (``tools.capsules``, freshness-checked against a source hash). Editor notes are a
third, LLM-written *and ungated*:

* Like a capsule, an LLM writes it, so it can never ride the deterministic drift gate —
  there is no canon-derived byte sequence to regenerate-and-diff against.
* Unlike a capsule, it carries **no freshness hash and no check-* companion**. A capsule
  describes an entity's *current* state and goes stale the moment that state changes; a
  note describes a critique of a writeup *as it read at one point in time* — a dated
  opinion, not a claim that has to track a moving source. Re-running the editor over an
  unchanged writeup is expected to produce another, equally valid note, not to "refresh"
  a stale one — which is exactly why the filename embeds a UTC timestamp instead of
  being keyed by path alone (there is no single canonical note to go stale).

Because the filename is timestamped, the artifact is non-deterministic *by construction*.
Nothing in the gate may ever regenerate or byte-diff it: ``tools.build``'s module
docstring and the CI drift step both name an explicit file list rather than globbing all
of ``derived/``, so ``derived/editor-notes/`` was never in scope for either.

Read-only enforcement (layered)
--------------------------------
(a) **By construction**, nothing in this module writes outside ``derived/editor-notes/``
    (and, with ``--patch``, a sibling ``.patch`` file next to the note it was extracted
    from — never back into ``expositions/`` itself).
(b) **The orchestrator guard** (``tools.orchestrate.run_no_edit_guard``): when the
    orchestrator dispatches an editor-like role (``edits_canon=False``) through the
    agentic path — a real ``claude -p`` subprocess that could in principle use tools to
    write files, unlike the plain (system, prompt) -> text completion this module calls
    directly — it checks ``canon/`` and ``expositions/`` for any git change afterward
    and restores + fails the task if it finds one. See that function's docstring.
(c) **OS-level sandboxing** (Claude Code tool permissions restricting what the editor
    process can touch on disk) is a Claude-Code-settings concern, out of this repo's
    scope — flagged here, not built here.

One LLM invocation path
------------------------
Dispatch goes through ``tools.orchestrate.make_runner``, which itself delegates to
``tools.capsules.run_llm_{cli,api}`` — the same single backend every other model call in
this repo uses. This module adds no new client.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .capsules import capsule_body
from .common import AGENTS_DIR, CAPSULES_DIR, EDITOR_NOTES_DIR, INDICES_DIR, ROOT
from .loader import Canon, load_canon
from .orchestrate import ORCH_MODEL, Runner, make_runner

APPEARANCES_PATH = INDICES_DIR / "appearances.json"
CONSISTENCY_EDITOR_PROMPT = AGENTS_DIR / "consistency-editor.md"

# Consistency-checking is real reasoning over a whole writeup, not summarization, so
# this rides the orchestrator's general-role tier (tools.orchestrate.ORCH_MODEL) rather
# than the capsule tier (tools.capsules.CAPSULE_MODEL) — the same choice
# tools.orchestrate already makes for its own consistency-editor tasks.
EDITOR_MODEL = ORCH_MODEL

# --- the note template contract (fixed section order, machine-splittable) ----

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## Critique",
    "## Structure & Rigor",
    "## Canon Alignment Issues",
    "## Notation Issues",
)
OPTIONAL_SECTIONS: tuple[str, ...] = ("## Suggested Edits",)

NOTE_TEMPLATE_CONTRACT = """\
# Output contract

Write ONE critique note in exactly this section order, using exactly these Markdown \
headings (a section's body may say "None found." if genuinely inapplicable, but the \
heading itself is never omitted — the note must stay machine-splittable by heading):

## Critique
Prose assessment of the writeup as mathematical exposition: clarity, motivation, \
whether the argument's shape is visible.

## Structure & Rigor
How the writeup moves: where it hand-waves, where it belabors, gaps between what is \
claimed and what is shown.

## Canon Alignment Issues
One bullet per issue. Each bullet MUST cite the entity id(s) involved (as [[id]]) and \
quote or closely paraphrase the exact capsule/context line the writeup conflicts with, \
e.g.:
- [[thm.main_bound]] is invoked without its integrality hypothesis, but its capsule \
states the bound requires it.
Statements quoted with weakened hypotheses, invocations of refuted results, and \
reproofs of things canon already records all belong here. Write "None found." if there \
are none.

## Notation Issues
Places the writeup departs from the project's declared notation (the notation index in \
your context) or coins a symbol canon already assigns. Write "None found." if there \
are none.

## Suggested Edits
OPTIONAL — include this heading only if you have one concrete, minimal fix. If included, \
it must contain exactly one fenced unified diff against the exposition file, in this \
exact form:

```diff
--- a/<exposition-path>
+++ b/<exposition-path>
@@ ...
```

Never emit a diff you are not confident applies cleanly against the text you were \
given. If you have no concrete edit to propose, omit this section entirely — do not emit \
an empty or placeholder diff."""


def missing_sections(note_text: str) -> list[str]:
    """Which :data:`REQUIRED_SECTIONS` headings are absent from a note (advisory only).

    The template is a prompt contract, not a parser-enforced schema — a model can still
    drift from it. This is used to print a visible warning, never to reject or block a
    note from being written; a slightly malformed note is still useful advisory output.
    """
    return [h for h in REQUIRED_SECTIONS if h not in note_text]


# --- context assembly: exposition text + capsules of entities it links --------

def load_appearances(path: Path = APPEARANCES_PATH) -> dict[str, list[str]]:
    """Read ``derived/indices/appearances.json`` (entity id -> exposition paths mentioning it)."""
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def entities_in_exposition(exp_path: str, appearances: dict[str, list[str]]) -> list[str]:
    """Which entity ids an exposition links, by inverting the appearances index.

    ``appearances.json`` is keyed by entity (built for "every writeup touching this
    entity", the build side's need) — this is the one-time inversion the editor needs in
    the other direction, kept here rather than re-parsing ``[[id]]`` links out of the
    file body a second time, so both consumers agree on what a "mention" is (see
    ``tools.expositions``, which already makes that same argument for build vs.
    validate). Sorted for determinism.
    """
    return sorted(eid for eid, paths in appearances.items() if exp_path in paths)


def _capsule_block(entity_id: str, entity_type: str | None, *, capsules_dir: Path) -> str:
    text = capsule_body(entity_id, capsules_dir=capsules_dir).strip()
    label = f", {entity_type}" if entity_type else ""
    return f"# --- context: {entity_id}{label} ---\n{text or '(no capsule available)'}\n"


def assemble_editor_context(
    exp_path: str,
    exp_text: str,
    *,
    appearances: dict[str, list[str]] | None = None,
    capsules_dir: Path = CAPSULES_DIR,
    canon: Canon | None = None,
) -> str:
    """Exposition text + a capsule block per entity the writeup wiki-links.

    Mirrors ``tools.assemble_context.assemble``'s multi-resolution shape (full text of
    the thing under review, capsules for everything it references) but scoped to an
    exposition instead of an entity. ``canon`` is optional and used only to label each
    block with the entity's type; omit it (as tests do) to skip that cosmetic lookup
    entirely.
    """
    if appearances is None:
        appearances = load_appearances()
    entity_ids = entities_in_exposition(exp_path, appearances)
    types: dict[str, str] = {}
    if canon is not None:
        types = {eid: canon.entities[eid].type for eid in entity_ids if eid in canon.entities}

    blocks = [f"# === EXPOSITION: {exp_path} ===\n{exp_text.rstrip()}\n"]
    for eid in entity_ids:
        blocks.append(_capsule_block(eid, types.get(eid), capsules_dir=capsules_dir))
    return "\n".join(blocks)


# --- filenames: derived/editor-notes/<path-slug>--<UTC timestamp>.md ----------

def slugify_exposition_path(exp_path: str) -> str:
    """``expositions/survey/001-x.md`` -> ``expositions--survey--001-x``."""
    return Path(exp_path).with_suffix("").as_posix().replace("/", "--")


def utc_timestamp(now: datetime | None = None) -> str:
    """Compact, filename-safe UTC timestamp; injectable ``now`` for deterministic tests."""
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def note_filename(exp_path: str, *, now: datetime | None = None) -> str:
    return f"{slugify_exposition_path(exp_path)}--{utc_timestamp(now)}.md"


# --- write the note -----------------------------------------------------------

def write_note(
    exp_path: str,
    *,
    runner: Runner,
    root: Path = ROOT,
    out_dir: Path = EDITOR_NOTES_DIR,
    capsules_dir: Path = CAPSULES_DIR,
    appearances_path: Path = APPEARANCES_PATH,
    role_prompt_path: Path = CONSISTENCY_EDITOR_PROMPT,
    model: str | None = None,
    now: datetime | None = None,
    canon: Canon | None = None,
) -> Path:
    """Run the editor over one exposition and write its critique note. Returns the path.

    ``runner`` is the injected ``(system, prompt) -> text`` backend (see
    ``tools.orchestrate.Runner`` / ``make_runner``) — this is the only place an LLM is
    invoked, and it goes through the one shared path. Every other input is a plain
    injectable parameter (path or object) so this runs fully offline under test, the same
    pattern as ``tools.capsules`` and ``tools.orchestrate``. ``model`` is cosmetic — it
    only affects the note's provenance header, since the actual model is already bound
    into ``runner``; pass the same one used to build it for an accurate header.
    """
    root = Path(root)
    exp_file = Path(exp_path) if Path(exp_path).is_absolute() else root / exp_path
    exp_text = exp_file.read_text()

    context = assemble_editor_context(
        exp_path, exp_text,
        appearances=load_appearances(appearances_path),
        capsules_dir=capsules_dir, canon=canon,
    )

    system = f"{Path(role_prompt_path).read_text()}\n\n{NOTE_TEMPLATE_CONTRACT}"
    user = (
        "# Task\nCritique this exposition for mathematical exposition quality, rigor, "
        "canon alignment, and notation consistency.\n\n" + context
    )
    note_text = runner(system, user)

    missing = missing_sections(note_text)
    if missing:
        print(f"WARNING: editor note for {exp_path} is missing section(s): "
              f"{', '.join(missing)}", file=sys.stderr)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    note_path = out_dir / note_filename(exp_path, now=now)

    fields = [f"editor note for {exp_path}", f"generated: {utc_timestamp(now)}"]
    if model:
        fields.append(f"model: {model}")
    header = f"<!-- {' | '.join(fields)} -->\n"
    note_path.write_text(header + note_text.rstrip() + "\n")
    return note_path


# --- optional patch extraction -------------------------------------------------

# Non-greedy: stops at the FIRST closing fence after "## Suggested Edits", so a note with
# exactly one diff (the contract asks for exactly one) extracts cleanly even if earlier
# sections also happen to contain fenced code blocks (Canon Alignment Issues bullets can
# quote text that includes backticks).
_DIFF_FENCE_RE = re.compile(r"## Suggested Edits.*?```(?:diff)?\n(.*?)```", re.DOTALL)


def extract_suggested_diff(note_text: str) -> str | None:
    """Pull the fenced unified diff out of a note's ``## Suggested Edits`` section.

    Returns ``None`` if there is no such section, no fenced block, or the block is
    blank — all of which mean "nothing to extract", not an error.
    """
    m = _DIFF_FENCE_RE.search(note_text)
    if not m:
        return None
    diff = m.group(1)
    return diff if diff.strip() else None


def check_patch_applies(diff_text: str, *, cwd: Path = ROOT,
                        run: Callable = None) -> tuple[bool, str]:
    """``git apply --check`` a diff without touching the working tree. ``run`` is injected."""
    _run = run or subprocess.run
    result = _run(["git", "apply", "--check", "-"], input=diff_text, cwd=str(cwd),
                  capture_output=True, text=True)
    ok = result.returncode == 0
    return ok, "" if ok else (result.stderr or "").strip()


def write_patch_if_applies(note_path: Path, diff_text: str, *, cwd: Path = ROOT,
                           run: Callable = None) -> tuple[Path | None, str]:
    """Write the sibling ``.patch`` file iff the diff applies clean; else report, don't write.

    A diff that fails ``git apply --check`` is reported (the message carries git's own
    explanation) and no file is written — a broken suggestion must never silently land
    next to the note looking like a usable one.
    """
    ok, detail = check_patch_applies(diff_text, cwd=cwd, run=run)
    if not ok:
        return None, f"suggested diff does not apply cleanly, not written: {detail}"
    patch_path = Path(note_path).with_suffix(".patch")
    patch_path.write_text(diff_text if diff_text.endswith("\n") else diff_text + "\n")
    return patch_path, f"wrote {patch_path}"


# --- CLI ------------------------------------------------------------------------

def run_editor(exp_path: str, *, patch: bool = False, backend: str = "cli",
              model: str = EDITOR_MODEL, run=None, binary: str | None = None,
              client=None) -> int:
    canon = load_canon()
    if canon.errors:
        print("Refusing to run: canon has load/validation errors. Run validate first.")
        for path, msg in canon.errors:
            print(f"  {path}: {msg}")
        return 1

    runner = make_runner(backend, model=model, client=client, run=run, binary=binary)
    note_path = write_note(exp_path, runner=runner, model=model, canon=canon)
    try:
        shown = note_path.relative_to(ROOT)
    except ValueError:
        shown = note_path
    print(f"wrote {shown}")

    if patch:
        diff = extract_suggested_diff(note_path.read_text())
        if diff is None:
            print("no '## Suggested Edits' fenced diff found; nothing to check.")
        else:
            _patch_path, message = write_patch_if_applies(note_path, diff)
            print(message)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the Consistency Editor over one exposition; write an advisory "
                    "critique note to derived/editor-notes/.")
    ap.add_argument("exposition", help="path to an expositions/**.md file")
    ap.add_argument("--patch", action="store_true",
                    help="extract a Suggested Edits diff to a sibling .patch, if it applies clean")
    ap.add_argument("--backend", choices=("cli", "api"), default="cli",
                    help="cli: `claude -p` on your subscription (default); api: metered API")
    ap.add_argument("--model", default=EDITOR_MODEL, help=f"model (default: {EDITOR_MODEL})")
    args = ap.parse_args(argv)
    return run_editor(args.exposition, patch=args.patch, backend=args.backend, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
